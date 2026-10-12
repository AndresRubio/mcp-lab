"""OpsDesk over Streamable HTTP, including raw requests that check 2026-07-28 wire rules."""

import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
from mcp import Client

PROTOCOL = "2026-07-28"
ACCEPT = "application/json, text/event-stream"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def http_url() -> Iterator[str]:
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-m", "opsdesk", "--transport", "http", "--port", str(port)])
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("opsdesk HTTP server did not start")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def _tools_list_body(version: str = PROTOCOL) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": version,
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }


@pytest.mark.anyio
async def test_modern_and_legacy_clients_both_work_over_http(http_url: str) -> None:
    async with Client(http_url) as modern:
        assert modern.protocol_version == PROTOCOL
        assert len((await modern.list_tools()).tools) == 5
    async with Client(http_url, mode="legacy") as legacy:
        assert legacy.protocol_version == "2025-11-25"
        assert len((await legacy.list_tools()).tools) == 5


def test_stateless_request_with_required_headers_succeeds(http_url: str) -> None:
    headers = {"Accept": ACCEPT, "MCP-Protocol-Version": PROTOCOL, "Mcp-Method": "tools/list"}
    response = httpx2.post(http_url, json=_tools_list_body(), headers=headers)
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["resultType"] == "complete"
    assert result["cacheScope"] == "public"
    assert "Mcp-Session-Id" not in response.headers  # no protocol-level sessions in 2026-07-28


def test_missing_mcp_method_header_is_rejected_with_32020(http_url: str) -> None:
    headers = {"Accept": ACCEPT, "MCP-Protocol-Version": PROTOCOL}
    response = httpx2.post(http_url, json=_tools_list_body(), headers=headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020  # HeaderMismatch


def test_unsupported_version_is_rejected_with_32022_and_lists_supported(http_url: str) -> None:
    headers = {"Accept": ACCEPT, "MCP-Protocol-Version": "1999-01-01", "Mcp-Method": "tools/list"}
    response = httpx2.post(http_url, json=_tools_list_body("1999-01-01"), headers=headers)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == -32022  # UnsupportedProtocolVersion
    assert PROTOCOL in error["data"]["supported"]
