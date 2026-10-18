import os
import re
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

import httpx2
import pytest
import yaml
from fastmcp import FastMCP

from gateway.app import build_gateway_servers
from gateway.pager import MCP_NAMES, create_pager_app, pager_mcp
from gateway.registry import RegistryHolder
from opsdesk.data import OpsDeskStore, seed_store
from opsdesk.server import build_server

Announced = Callable[..., AbstractContextManager[re.Match[str]]]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.yaml"


@pytest.fixture
def write_registry(registry_path: Path) -> Callable[[Any], Path]:
    """Write `data` (a dict as YAML, or raw text) and move the mtime forward by a whole second.

    Filesystem timestamps can be coarse, so two quick writes may share an mtime; bumping it
    explicitly makes every write visible to `RegistryHolder.refresh()` without sleeping.
    """
    generation = [0]

    def write(data: Any) -> Path:
        text = data if isinstance(data, str) else yaml.safe_dump(data)
        registry_path.write_text(text)
        generation[0] += 1
        stamp = 1_700_000_000_000_000_000 + generation[0] * 1_000_000_000
        os.utime(registry_path, ns=(stamp, stamp))
        return registry_path

    return write


@pytest.fixture
def registry_data() -> dict[str, Any]:
    """The test registry: every OpsDesk tool enabled except `update_runbook`, all Pager tools enabled."""
    opsdesk_tools = ["list_incidents", "get_service_status", "restart_service", "run_diagnostics"]
    return {
        "servers": {
            "opsdesk": {
                "owner": "oncall-team",
                "source": {"kind": "stdio", "command": ["uv", "run", "opsdesk-server"]},
                "tools": {
                    **{name: {"enabled": True, "owner": "oncall-team"} for name in opsdesk_tools},
                    "update_runbook": {"enabled": False, "owner": "oncall-team"},
                },
            },
            "pager": {
                "owner": "sre",
                "source": {
                    "kind": "openapi",
                    "spec": "gateway.pager:SPEC",
                    "base_url": "http://pager.local",
                    "mcp_names": MCP_NAMES,
                },
                "tools": {name: {"enabled": True} for name in MCP_NAMES.values()},
            },
        }
    }


@pytest.fixture
def holder(write_registry: Callable[[Any], Path], registry_data: dict[str, Any]) -> RegistryHolder:
    return RegistryHolder(write_registry(registry_data))


@pytest.fixture
def store() -> OpsDeskStore:
    return seed_store()


@pytest.fixture
async def pager_http() -> AsyncIterator[httpx2.AsyncClient]:
    transport = httpx2.ASGITransport(app=create_pager_app())
    async with httpx2.AsyncClient(transport=transport, base_url="http://pager.local") as client:
        yield client


@pytest.fixture
def servers(
    holder: RegistryHolder, store: OpsDeskStore, pager_http: httpx2.AsyncClient
) -> dict[str, FastMCP]:
    """The gateway's per-backend servers over an in-process OpsDesk and an in-process Pager."""
    return build_gateway_servers(holder, {"opsdesk": build_server(store), "pager": pager_mcp(pager_http)})


@pytest.fixture
def opsdesk(servers: dict[str, FastMCP]) -> FastMCP:
    return servers["opsdesk"]


@contextmanager
def _announced(args: list[str], pattern: str, timeout: float = 20) -> Iterator[re.Match[str]]:
    """Run `args`, wait until its stderr matches `pattern`, yield the match, then stop it.

    stderr goes to a file, not a PIPE: an undrained pipe blocks the child once it fills.
    """
    with tempfile.TemporaryFile(mode="w+") as log:
        proc = subprocess.Popen(args, stderr=log, stdout=subprocess.DEVNULL, text=True)
        try:
            deadline = time.monotonic() + timeout
            while True:
                log.seek(0)
                output = log.read()
                if match := re.search(pattern, output):
                    break
                if proc.poll() is not None:
                    raise RuntimeError(f"{args} exited with code {proc.returncode}: {output}")
                if time.monotonic() > deadline:
                    raise RuntimeError(f"{args} did not announce itself: {output}")
                time.sleep(0.05)
            yield match
        finally:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


@pytest.fixture
def announced() -> Announced:
    """`with announced([cmd...], r"regex") as match:` runs a server subprocess for the block."""
    return _announced
