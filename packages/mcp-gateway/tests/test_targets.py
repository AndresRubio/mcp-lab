import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport

from gateway.app import load_openapi_spec, resolve_targets
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
    path.write_text(json.dumps(SPEC))
    assert load_openapi_spec(str(path)) == SPEC


@pytest.mark.parametrize(
    ("ref", "message"),
    [
        pytest.param("gateway.pager:NOPE", "gateway.pager:NOPE", id="missing-attr"),
        pytest.param("gateway.pager:ROTA", "is not an OpenAPI spec", id="not-a-spec"),
        pytest.param("no-colon", "module:ATTR", id="malformed"),
    ],
)
def test_bad_spec_references_are_reported(ref: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load_openapi_spec(ref)
