"""The gateway as one ASGI app, driven in-process over Streamable HTTP (no sockets)."""

import json
import os
import sys
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import anyio
import httpx2
import mcp_types as mt
import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.utilities.asgi_transport import StreamingASGITransport, run_asgi_lifespan
from mcp import Client as SDKClient
from mcp import MCPError
from mcp.client.session import ClientRequestContext
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import McpHttpClientFactory
from starlette.applications import Starlette

from gateway.app import build_gateway_servers, create_app, resolve_targets
from gateway.registry import Registry, RegistryHolder
from opsdesk.data import OpsDeskStore
from opsdesk.server import build_server

pytestmark = pytest.mark.anyio

BASE = "http://gateway.test"
PROTOCOL = "2026-07-28"

WriteRegistry = Callable[[Any], Path]


def _asgi_client_factory(app: Starlette) -> McpHttpClientFactory:
    # The three named parameters satisfy the McpHttpClientFactory Protocol for pyright;
    # **kwargs is required at runtime because fastmcp also passes follow_redirects=...
    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx2.Timeout | None = None,
        auth: httpx2.Auth | None = None,
        **kwargs: Any,
    ) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=StreamingASGITransport(app),
            base_url=BASE,
            headers=headers,
            timeout=timeout,
            auth=auth,
            **kwargs,
        )

    return factory


@pytest.fixture
async def app(servers: dict[str, FastMCP]) -> AsyncIterator[Starlette]:
    gateway = create_app(servers)
    async with run_asgi_lifespan(gateway):
        yield gateway


@pytest.fixture
async def raw(app: Starlette) -> AsyncIterator[httpx2.AsyncClient]:
    async with httpx2.AsyncClient(transport=StreamingASGITransport(app), base_url=BASE) as client:
        yield client


def _client(app: Starlette, server: str, **kwargs: Any) -> Client[StreamableHttpTransport]:
    transport = StreamableHttpTransport(
        f"{BASE}/{server}/mcp", httpx_client_factory=_asgi_client_factory(app)
    )
    return Client(transport, **kwargs)


async def _confirm(message: str, response_type: Any, params: Any, context: Any) -> dict[str, Any]:
    return {"confirm": True}


async def _sdk_accept(context: ClientRequestContext, params: mt.ElicitRequestParams) -> mt.ElicitResult:
    return mt.ElicitResult(action="accept", content={"confirm": True})


def _payload(response: httpx2.Response) -> dict[str, Any]:
    # A JSON body, or a single SSE `data:` event, depending on how the server chose to answer.
    text = response.text
    body = text.split("data: ", 1)[1].strip() if "data: " in text else text
    return json.loads(body)


def _raw_request(method: str, **params: Any) -> tuple[dict[str, Any], dict[str, str]]:
    meta = {
        "io.modelcontextprotocol/protocolVersion": PROTOCOL,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "raw", "version": "0"},
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {"_meta": meta, **params}}
    headers = {
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": PROTOCOL,
        "mcp-method": method,
    }
    return body, headers


async def test_healthz_lists_the_mounted_servers(raw: httpx2.AsyncClient) -> None:
    response = await raw.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "servers": ["opsdesk", "pager"]}


async def test_both_endpoints_answer(app: Starlette) -> None:
    async with _client(app, "opsdesk") as opsdesk, _client(app, "pager") as pager:
        assert opsdesk.protocol_version == PROTOCOL
        assert "list_incidents" in {t.name for t in await opsdesk.list_tools()}
        oncall = await pager.call_tool("oncall_for", {"service": "search"})
    assert oncall.structured_content is not None
    assert oncall.structured_content["primary"] == "[REDACTED:email]"


async def test_unknown_path_is_404(raw: httpx2.AsyncClient) -> None:
    body, headers = _raw_request("tools/list")
    response = await raw.post("/nope/mcp", json=body, headers=headers)
    assert response.status_code == 404


async def test_raw_2026_request_needs_no_session(raw: httpx2.AsyncClient) -> None:
    body, headers = _raw_request("tools/list")
    response = await raw.post("/opsdesk/mcp", json=body, headers=headers)
    assert response.status_code == 200
    assert "mcp-session-id" not in response.headers
    tools = {t["name"] for t in _payload(response)["result"]["tools"]}
    assert tools == {"list_incidents", "get_service_status", "restart_service", "run_diagnostics"}


# The in-process transport reports the URL's host as the ASGI `server`, which is what uvicorn
# reports for a socket bound to 127.0.0.1. That makes FastMCP's "auto" Host/Origin guard apply.
LOOPBACK = "http://127.0.0.1:8080"


@pytest.fixture
async def loopback(app: Starlette) -> AsyncIterator[httpx2.AsyncClient]:
    async with httpx2.AsyncClient(transport=StreamingASGITransport(app), base_url=LOOPBACK) as client:
        yield client


async def test_a_loopback_request_with_its_own_host_is_served(loopback: httpx2.AsyncClient) -> None:
    body, headers = _raw_request("tools/list")
    for host in ("127.0.0.1:8080", "localhost:8080"):
        response = await loopback.post("/opsdesk/mcp", json=body, headers={**headers, "host": host})
        assert response.status_code == 200
        assert "list_incidents" in {t["name"] for t in _payload(response)["result"]["tools"]}


async def test_a_forged_host_is_rejected(loopback: httpx2.AsyncClient) -> None:
    # DNS rebinding: a page on evil.example re-resolves its name to 127.0.0.1 and posts to the gateway.
    body, headers = _raw_request("tools/list")
    response = await loopback.post(
        "/opsdesk/mcp", json=body, headers={**headers, "host": "evil.example:8080"}
    )
    assert response.status_code == 421


async def test_a_forged_origin_is_rejected(loopback: httpx2.AsyncClient) -> None:
    body, headers = _raw_request("tools/list")
    response = await loopback.post(
        "/opsdesk/mcp", json=body, headers={**headers, "origin": "http://evil.example"}
    )
    assert response.status_code == 403


async def test_healthz_serves_a_loopback_request_with_its_own_host(loopback: httpx2.AsyncClient) -> None:
    for host in ("127.0.0.1:8080", "localhost:8080"):
        response = await loopback.get("/healthz", headers={"host": host})
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "servers": ["opsdesk", "pager"]}


async def test_healthz_rejects_a_forged_host(loopback: httpx2.AsyncClient) -> None:
    # A rebound page could otherwise read the server names.
    response = await loopback.get("/healthz", headers={"host": "evil.example:8080"})
    assert response.status_code == 421


async def test_healthz_rejects_a_forged_origin(loopback: httpx2.AsyncClient) -> None:
    response = await loopback.get("/healthz", headers={"origin": "http://evil.example"})
    assert response.status_code == 403


async def test_mrtr_restart_with_the_fastmcp_client(app: Starlette, store: OpsDeskStore) -> None:
    async with _client(app, "opsdesk", elicitation_handler=_confirm) as client:
        result = await client.call_tool("restart_service", {"service": "search"})
    assert result.structured_content == {"service": "search", "restarted": True, "state": "healthy"}
    assert store.service("search").restarts == 1


async def test_mrtr_restart_with_the_official_sdk_client(app: Starlette, store: OpsDeskStore) -> None:
    async with (
        httpx2.AsyncClient(transport=StreamingASGITransport(app), base_url=BASE) as http,
        SDKClient(
            streamable_http_client(f"{BASE}/opsdesk/mcp", http_client=http), elicitation_callback=_sdk_accept
        ) as client,
    ):
        assert client.protocol_version == PROTOCOL
        result = await client.call_tool("restart_service", {"service": "api"})
    assert result.structured_content == {"service": "api", "restarted": True, "state": "healthy"}
    assert store.service("api").restarts == 1


async def test_legacy_client_gets_the_confirmation_as_a_push(app: Starlette, store: OpsDeskStore) -> None:
    async with _client(app, "opsdesk", elicitation_handler=_confirm, mode="legacy") as client:
        assert client.protocol_version == "2025-11-25"
        result = await client.call_tool("restart_service", {"service": "search"})
    assert result.structured_content == {"service": "search", "restarted": True, "state": "healthy"}
    assert store.service("search").restarts == 1


async def test_rate_limits_are_per_caller_over_http(
    write_registry: WriteRegistry, registry_data: dict[str, Any], store: OpsDeskStore
) -> None:
    registry_data["servers"]["opsdesk"]["rate_limit"] = {"calls_per_minute": 2}
    holder = RegistryHolder(write_registry(registry_data))
    # A fixed clock, so a minute boundary cannot fall between the calls.
    servers = build_gateway_servers(holder, {"opsdesk": build_server(store)}, clock=lambda: 600.0)
    gateway = create_app(servers)
    results: dict[str, list[mt.CallToolResult]] = {}
    async with run_asgi_lifespan(gateway):
        for name in ("alice", "bob"):
            info = mt.Implementation(name=name, version="1.0")
            async with _client(gateway, "opsdesk", client_info=info) as client:
                assert client.protocol_version == PROTOCOL
                results[name] = [await client.call_tool_mcp("list_incidents", {}) for _ in range(3)]
    for name, (first, second, third) in results.items():
        assert (first.is_error, second.is_error) == (False, False)
        assert third.is_error is True
        assert third.content == [
            mt.TextContent(
                type="text",
                text=f"Rate limit exceeded: caller '{name}' may make 2 calls per minute to server 'opsdesk'.",
            )
        ]


# Known gaps of FastMCP 4.1's proxy. These pin today's behaviour so docs/notes/m2a.md cannot
# silently go stale: if one starts failing, the gap closed and the notes need an update.


async def test_known_gap_subscriptions_listen_is_not_proxied(app: Starlette) -> None:
    async with (
        httpx2.AsyncClient(transport=StreamingASGITransport(app), base_url=BASE) as http,
        SDKClient(streamable_http_client(f"{BASE}/opsdesk/mcp", http_client=http)) as client,
    ):
        with pytest.raises(MCPError, match="Method not found"):
            async with client.listen(resource_subscriptions=["runbook://api"]):
                pass


async def test_known_gap_upstream_cache_hints_are_dropped(raw: httpx2.AsyncClient) -> None:
    # OpsDesk itself answers tools/list with ttlMs=60000 and cacheScope "public".
    body, headers = _raw_request("tools/list")
    async with Client(build_server()) as upstream:
        listed = await upstream.list_tools_mcp()
    assert (listed.ttl_ms, listed.cache_scope) == (60_000, "public")
    result = _payload(await raw.post("/opsdesk/mcp", json=body, headers=headers))["result"]
    assert (result["cacheScope"], result["ttlMs"]) == ("private", 0)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_shutdown_stops_the_stdio_backend_and_closes_the_http_client(tmp_path: Path) -> None:
    # The transport exposes no public pid, so the backend records its own before serving.
    pid_file = tmp_path / "pid"
    boot = (
        f"import os; open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "from opsdesk.__main__ import main; main([])"
    )
    registry = Registry.model_validate(
        {
            "servers": {
                "opsdesk": {
                    "owner": "t",
                    "source": {"kind": "stdio", "command": [sys.executable, "-c", boot]},
                    "tools": {"list_incidents": {"enabled": True}},
                },
                "pager": {
                    "owner": "t",
                    "source": {
                        "kind": "openapi",
                        "spec": "gateway.pager:SPEC",
                        "base_url": "http://pager.invalid",
                    },
                },
            }
        }
    )
    registry_file = tmp_path / "registry.yaml"
    registry_file.write_text(json.dumps(registry.model_dump(mode="json")))
    targets = resolve_targets(registry)
    [client] = targets.clients
    gateway = create_app(
        build_gateway_servers(RegistryHolder(registry_file), targets), on_shutdown=targets.aclose
    )
    async with run_asgi_lifespan(gateway), _client(gateway, "opsdesk") as mcp:
        assert "list_incidents" in {t.name for t in await mcp.list_tools()}
        pid = int(pid_file.read_text())
        assert _alive(pid)
        assert not client.is_closed
    deadline = time.monotonic() + 10
    while _alive(pid) and time.monotonic() < deadline:
        await anyio.sleep(0.05)
    assert not _alive(pid)
    assert client.is_closed
