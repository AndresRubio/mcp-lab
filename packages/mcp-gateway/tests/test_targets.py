import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport

from gateway.app import SpecError, load_openapi_spec, resolve_targets
from gateway.pager import SPEC
from gateway.registry import Registry

pytestmark = pytest.mark.anyio


def _registry(source: dict[str, Any]) -> Registry:
    return Registry.model_validate({"servers": {"s": {"owner": "t", "source": source}}})


def test_stdio_becomes_a_kept_alive_stdio_transport() -> None:
    [target] = resolve_targets(
        _registry({"kind": "stdio", "command": ["uv", "run", "opsdesk-server"]})
    ).values()
    assert isinstance(target, StdioTransport)
    assert (target.command, target.args) == ("uv", ["run", "opsdesk-server"])
    # MRTR needs one backend process across rounds: its sealed requestState key is per process.
    assert target.keep_alive is True


def test_http_becomes_its_url() -> None:
    targets = resolve_targets(_registry({"kind": "http", "url": "http://127.0.0.1:9/mcp"}))
    assert targets == {"s": "http://127.0.0.1:9/mcp"}


async def test_openapi_becomes_an_mcp_server_with_the_registry_names() -> None:
    source = {
        "kind": "openapi",
        "spec": "gateway.pager:SPEC",
        "base_url": "http://pager.invalid",
        "mcp_names": {"getOncall": "oncall_for"},
    }
    [target] = resolve_targets(_registry(source)).values()
    assert isinstance(target, FastMCP)
    async with Client(target) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == {"oncall_for", "createPage", "listPages"}


def test_spec_loads_from_module_attribute_or_json_file(tmp_path: Path) -> None:
    assert load_openapi_spec("gateway.pager:SPEC") is SPEC
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(SPEC), encoding="utf-8")
    assert load_openapi_spec(str(path)) == SPEC


@pytest.mark.parametrize(
    ("ref", "message"),
    [
        pytest.param("gateway.pager:NOPE", "gateway.pager:NOPE", id="missing-attr"),
        pytest.param("gateway.pager:ROTA", "is not an OpenAPI spec", id="not-a-spec"),
        pytest.param("no-colon", "module:ATTR", id="malformed"),
        pytest.param("/nonexistent/dir/spec.json", "/nonexistent/dir/spec.json", id="missing-file"),
    ],
)
def test_bad_spec_references_are_reported(ref: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load_openapi_spec(ref)


def test_invalid_json_and_non_object_json_are_reported(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match=r"broken\.json"):
        load_openapi_spec(str(broken))
    array = tmp_path / "array.json"
    array.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="is not an OpenAPI spec"):
        load_openapi_spec(str(array))


async def test_aclose_closes_the_http_client_and_is_idempotent() -> None:
    source = {"kind": "openapi", "spec": "gateway.pager:SPEC", "base_url": "http://pager.invalid"}
    targets = resolve_targets(_registry(source))
    [client] = targets.clients
    assert not client.is_closed
    await targets.aclose()
    assert client.is_closed
    await targets.aclose()


def test_a_failed_resolve_spawns_no_backend(tmp_path: Path) -> None:
    """Resolving only builds transports: a stdio command runs when the app does, not before."""
    pid_file = tmp_path / "pid"
    spec = tmp_path / "bad.json"
    spec.write_text('{"openapi": "3.1.0", "paths": 5}', encoding="utf-8")
    boot = f"import os; open({str(pid_file)!r}, 'w', encoding='utf-8').write(str(os.getpid()))"
    registry = Registry.model_validate(
        {
            "servers": {
                "first": {"owner": "t", "source": {"kind": "stdio", "command": [sys.executable, "-c", boot]}},
                "second": {
                    "owner": "t",
                    "source": {"kind": "openapi", "spec": str(spec), "base_url": "http://127.0.0.1:9"},
                },
            }
        }
    )
    with pytest.raises(SpecError, match=r"bad\.json"):
        resolve_targets(registry)
    assert not pid_file.exists()


def test_a_relative_spec_path_is_resolved_against_the_registry_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The operator writes `spec: pager.json` next to registry.yaml and starts the gateway from
    # anywhere; the working directory must not decide which file is loaded.
    registry_dir = tmp_path / "etc"
    registry_dir.mkdir()
    (registry_dir / "pager.json").write_text(json.dumps(SPEC), encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    source = {"kind": "openapi", "spec": "pager.json", "base_url": "http://pager.invalid"}
    [target] = resolve_targets(_registry(source), spec_dir=registry_dir).values()
    assert isinstance(target, FastMCP)


def test_a_spec_file_is_read_as_utf8_whatever_the_locale_says(tmp_path: Path) -> None:
    # A spec written in UTF-8 on one machine is loaded on another whose locale is not UTF-8.
    # The read happens in a subprocess forced to an ASCII locale: in-process the test's own
    # interpreter already runs under UTF-8 and could not fail.
    path = tmp_path / "pager.json"
    spec = {"openapi": "3.1.0", "info": {"title": "Pager", "version": "1", "description": "Páginas"}}
    path.write_bytes(json.dumps(spec, ensure_ascii=False).encode("utf-8"))
    env = os.environ | {"LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
    code = (
        "import json, locale, sys\n"
        "from gateway.app import load_openapi_spec\n"
        "assert locale.getencoding().lower() not in ('utf-8', 'utf8'), locale.getencoding()\n"
        "print(json.dumps(load_openapi_spec(sys.argv[1])['info']['description']))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code, str(path)], env=env, capture_output=True, text=True, encoding="utf-8"
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == "Páginas"


def test_a_missing_relative_spec_is_reported_with_its_resolved_path(tmp_path: Path) -> None:
    source = {"kind": "openapi", "spec": "missing.json", "base_url": "http://pager.invalid"}
    with pytest.raises(SpecError, match=re.escape(str(tmp_path / "missing.json"))):
        resolve_targets(_registry(source), spec_dir=tmp_path)
