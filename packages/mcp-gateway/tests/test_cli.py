"""The `mcp-gateway` CLI: `audit` in-process, and `serve` end to end as a real subprocess."""

import os
import re
import shutil
import socket
import sys
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import anyio
import httpx2
import mcp_types as mt
import pytest
import yaml
from mcp import Client
from mcp.client.session import ClientRequestContext

from gateway.__main__ import audit, main
from gateway.app import ResolvedTargets, resolve_targets
from gateway.registry import Registry, load_registry

Announced = Callable[..., AbstractContextManager[re.Match[str]]]
WriteRegistry = Callable[[Any], Path]

DEMO_REGISTRY = Path(__file__).resolve().parents[1] / "registry.yaml"
OPSDESK_STDIO = [sys.executable, "-m", "opsdesk"]


async def _accept(context: ClientRequestContext, params: mt.ElicitRequestParams) -> mt.ElicitResult:
    return mt.ElicitResult(action="accept", content={"confirm": True})


def _pid_recording_opsdesk(pid_file: Path) -> list[str]:
    """An OpsDesk stdio command that writes its PID first: the transport exposes no public pid."""
    boot = (
        f"import os; open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "from opsdesk.__main__ import main; main([])"
    )
    return [sys.executable, "-c", boot]


def _stdio_registry(registry_data: dict[str, Any]) -> dict[str, Any]:
    """The test registry with OpsDesk launched from this interpreter and the Pager dropped."""
    opsdesk = registry_data["servers"]["opsdesk"]
    opsdesk["source"]["command"] = OPSDESK_STDIO
    return {"servers": {"opsdesk": opsdesk}}


def test_demo_registry_matches_the_walkthrough() -> None:
    registry = load_registry(DEMO_REGISTRY)
    opsdesk, pager = registry.servers["opsdesk"], registry.servers["pager"]
    assert opsdesk.source.kind == "stdio"
    assert opsdesk.tools["update_runbook"].enabled is False
    assert "run_diagnostics" not in opsdesk.tools
    assert pager.source.kind == "openapi"
    assert pager.is_enabled("send_page")


# capfd, not capsys: the stdio backend inherits stderr, which needs a real file descriptor.
def test_audit_reports_unlisted_backend_tools(
    write_registry: WriteRegistry, registry_data: dict[str, Any], capfd: pytest.CaptureFixture[str]
) -> None:
    data = _stdio_registry(registry_data)
    del data["servers"]["opsdesk"]["tools"]["run_diagnostics"]
    data["servers"]["down"] = {"owner": "nobody", "source": {"kind": "http", "url": "http://127.0.0.1:9/mcp"}}
    main(["audit", "--registry", str(write_registry(data))])
    lines = capfd.readouterr().out.splitlines()
    assert "opsdesk (owner: oncall-team): pending review: run_diagnostics" in lines
    assert any(re.fullmatch(r"down \(owner: nobody\): unreachable: .+", line) for line in lines)


@pytest.mark.skipif(shutil.which("uv") is None, reason="the demo registry launches OpsDesk with `uv run`")
def test_audit_of_the_demo_registry(capfd: pytest.CaptureFixture[str]) -> None:
    main(["audit", "--registry", str(DEMO_REGISTRY)])
    assert capfd.readouterr().out.splitlines() == [
        "opsdesk (owner: oncall-team): pending review: run_diagnostics",
        "pager (owner: sre): every tool is reviewed",
    ]


@pytest.mark.anyio
async def test_serve_end_to_end_over_a_stdio_backend(
    announced: Announced, write_registry: WriteRegistry, registry_data: dict[str, Any], tmp_path: Path
) -> None:
    data = _stdio_registry(registry_data)
    pid_file = tmp_path / "pid"
    data["servers"]["opsdesk"]["source"]["command"] = _pid_recording_opsdesk(pid_file)
    path = write_registry(data)
    command = [sys.executable, "-m", "gateway", "serve", "--registry", str(path), "--port", "0"]
    with announced(command, r"Gateway listening on (http://\S+)\n\s+opsdesk: (http://\S+/opsdesk/mcp)") as m:
        base, url = m.group(1), m.group(2)
        health = httpx2.get(f"{base}/healthz")
        async with Client(url, elicitation_callback=_accept) as client:
            assert client.protocol_version == "2026-07-28"
            tools = {t.name for t in (await client.list_tools()).tools}
            first = await client.call_tool("restart_service", {"service": "search"})
            again = await client.call_tool("get_service_status", {"service": "search"})
        pid = int(pid_file.read_text())
        assert _alive(pid)
    # `announced` has sent SIGTERM and waited for the gateway: its stdio backend must be gone too.
    deadline = time.monotonic() + 10
    while _alive(pid) and time.monotonic() < deadline:
        await anyio.sleep(0.05)
    assert not _alive(pid)
    assert health.json() == {"status": "ok", "servers": ["opsdesk"]}
    assert tools == {"list_incidents", "get_service_status", "restart_service", "run_diagnostics"}
    assert first.structured_content == {"service": "search", "restarted": True, "state": "healthy"}
    # One backend process serves every round and every call (stdio keep_alive=True).
    assert again.structured_content == {"name": "search", "state": "healthy", "restarts": 1}


def test_serve_refuses_an_invalid_registry(tmp_path: Path) -> None:
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump({"servers": {"x": {"owner": "t"}}}))
    with pytest.raises(SystemExit, match=r"registry\.yaml"):
        main(["serve", "--registry", str(path), "--port", "0"])


def test_serve_reports_a_bad_openapi_spec_without_a_traceback(write_registry: WriteRegistry) -> None:
    source = {"kind": "openapi", "spec": "no_such_module:SPEC", "base_url": "http://127.0.0.1:9"}
    path = write_registry({"servers": {"pager": {"owner": "t", "source": source}}})
    with pytest.raises(SystemExit, match=r"^mcp-gateway: Cannot load OpenAPI spec 'no_such_module:SPEC'"):
        main(["serve", "--registry", str(path), "--port", "0"])


def test_serve_reports_a_port_in_use_without_a_traceback(write_registry: WriteRegistry) -> None:
    source = {"kind": "http", "url": "http://127.0.0.1:9/mcp"}
    path = write_registry({"servers": {"remote": {"owner": "t", "source": source}}})
    with socket.create_server(("127.0.0.1", 0)) as taken:
        port = taken.getsockname()[1]
        with pytest.raises(SystemExit, match=r"^mcp-gateway: .*[Aa]ddress already in use"):
            main(["serve", "--registry", str(path), "--port", str(port)])


@pytest.mark.parametrize("error", [ValueError("bug"), OSError("bug")], ids=["ValueError", "OSError"])
def test_serve_keeps_the_traceback_of_an_unexpected_error(
    write_registry: WriteRegistry, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """Only the three operator errors become one-line exits; a bug elsewhere in serve propagates."""
    source = {"kind": "http", "url": "http://127.0.0.1:9/mcp"}
    path = write_registry({"servers": {"remote": {"owner": "t", "source": source}}})

    def explode(*args: object, **kwargs: object) -> dict[str, Any]:
        raise error

    monkeypatch.setattr("gateway.__main__.build_gateway_servers", explode)
    with pytest.raises(type(error), match="bug"):
        main(["serve", "--registry", str(path), "--port", "0"])


def test_audit_reports_a_bad_openapi_spec_without_a_traceback(write_registry: WriteRegistry) -> None:
    source = {"kind": "openapi", "spec": "no_such_module:SPEC", "base_url": "http://127.0.0.1:9"}
    path = write_registry({"servers": {"pager": {"owner": "t", "source": source}}})
    with pytest.raises(SystemExit, match=r"^mcp-gateway: Cannot load OpenAPI spec 'no_such_module:SPEC'"):
        main(["audit", "--registry", str(path)])


def test_audit_refuses_an_invalid_registry(tmp_path: Path) -> None:
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump({"servers": {"x": {"owner": "t"}}}))
    with pytest.raises(SystemExit, match=r"^mcp-gateway: .*registry\.yaml"):
        main(["audit", "--registry", str(path)])


def _malformed_spec_registry(
    write_registry: WriteRegistry, tmp_path: Path, spec_text: str = '{"openapi": "3.1.0", "paths": 5}'
) -> Path:
    spec = tmp_path / "bad.json"
    spec.write_text(spec_text)
    source = {"kind": "openapi", "spec": str(spec), "base_url": "http://127.0.0.1:9"}
    return write_registry({"servers": {"pager": {"owner": "t", "source": source}}})


# serve and audit share resolve_targets, so one fix covers both; each gets its own test.
@pytest.mark.parametrize("command", ["serve", "audit"])
def test_a_malformed_openapi_spec_exits_cleanly(
    write_registry: WriteRegistry, tmp_path: Path, command: str
) -> None:
    path = _malformed_spec_registry(write_registry, tmp_path)
    extra = ["--port", "0"] if command == "serve" else []
    with pytest.raises(SystemExit, match=r"^mcp-gateway: .*bad\.json"):
        main([command, "--registry", str(path), *extra])


@pytest.mark.parametrize("command", ["serve", "audit"])
@pytest.mark.parametrize(
    "spec_text", ['{"openapi": 3, "paths": []}', '{"openapi": null}'], ids=["int", "null"]
)
def test_an_openapi_spec_with_a_non_string_version_exits_cleanly(
    write_registry: WriteRegistry, tmp_path: Path, command: str, spec_text: str
) -> None:
    path = _malformed_spec_registry(write_registry, tmp_path, spec_text)
    extra = ["--port", "0"] if command == "serve" else []
    with pytest.raises(SystemExit, match=r"^mcp-gateway: .*bad\.json.* is not an OpenAPI spec"):
        main([command, "--registry", str(path), *extra])


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.anyio
async def test_audit_leaves_no_backend_process_behind(
    write_registry: WriteRegistry,
    registry_data: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_file = tmp_path / "pid"
    data = _stdio_registry(registry_data)
    data["servers"]["opsdesk"]["source"]["command"] = _pid_recording_opsdesk(pid_file)
    # Hold the targets so garbage collection cannot stop the backend for `audit`: only `aclose` may.
    held: list[ResolvedTargets] = []

    def resolve_and_hold(registry: Registry) -> ResolvedTargets:
        held.append(resolve_targets(registry))
        return held[-1]

    monkeypatch.setattr("gateway.__main__.resolve_targets", resolve_and_hold)
    # Run in this event loop: tearing a loop down would reap a leaked backend and hide the leak.
    await audit(write_registry(data))
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 10
    while _alive(pid) and time.monotonic() < deadline:
        await anyio.sleep(0.05)
    assert not _alive(pid)
