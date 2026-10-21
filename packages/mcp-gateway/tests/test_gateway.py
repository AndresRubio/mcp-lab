"""The gateway's data plane in memory: a registry-filtered proxy per backend."""

import re
import sys
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import mcp_types as mt
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from gateway.app import build_gateway_servers, resolve_targets
from gateway.registry import RegistryHolder
from opsdesk.data import OpsDeskStore

pytestmark = pytest.mark.anyio

Announced = Callable[..., AbstractContextManager[re.Match[str]]]
WriteRegistry = Callable[[Any], Path]


async def _confirm(message: str, response_type: Any, params: Any, context: Any) -> dict[str, Any]:
    return {"confirm": True}


async def _names(server: FastMCP) -> set[str]:
    async with Client(server) as client:
        return {t.name for t in await client.list_tools()}


async def test_proxies_are_named_and_speak_2026_07_28(opsdesk: FastMCP) -> None:
    async with Client(opsdesk) as client:
        assert client.protocol_version == "2026-07-28"
        assert client.server_info is not None
        assert client.server_info.name == "gateway/opsdesk"


async def test_only_enabled_tools_are_listed(servers: dict[str, FastMCP]) -> None:
    assert await _names(servers["opsdesk"]) == {
        "list_incidents",
        "get_service_status",
        "restart_service",
        "run_diagnostics",
    }
    assert await _names(servers["pager"]) == {"oncall_for", "send_page", "list_pages"}


async def test_disabled_tool_is_not_callable(opsdesk: FastMCP, store: OpsDeskStore) -> None:
    async with Client(opsdesk) as client:
        result = await client.call_tool_mcp("update_runbook", {"service": "api", "markdown": "# pwned"})
    assert result.is_error is True
    assert result.content == [mt.TextContent(type="text", text="Unknown tool: 'update_runbook'")]
    assert store.runbooks["api"].startswith("# api runbook")


async def test_unlisted_tool_is_hidden_and_unknown(
    opsdesk: FastMCP, holder: RegistryHolder, write_registry: WriteRegistry, registry_data: dict[str, Any]
) -> None:
    del registry_data["servers"]["opsdesk"]["tools"]["run_diagnostics"]
    write_registry(registry_data)
    assert holder.refresh() is True
    assert "run_diagnostics" not in await _names(opsdesk)
    async with Client(opsdesk) as client:
        with pytest.raises(ToolError, match="Unknown tool: 'run_diagnostics'"):
            await client.call_tool("run_diagnostics", {"service": "api"})


async def test_hot_reload_flips_visibility_without_rebuild(
    opsdesk: FastMCP, holder: RegistryHolder, write_registry: WriteRegistry, registry_data: dict[str, Any]
) -> None:
    assert "update_runbook" not in await _names(opsdesk)
    registry_data["servers"]["opsdesk"]["tools"]["update_runbook"]["enabled"] = True
    write_registry(registry_data)
    assert holder.refresh() is True
    assert "update_runbook" in await _names(opsdesk)
    async with Client(opsdesk) as client:
        result = await client.call_tool("update_runbook", {"service": "api", "markdown": "# v2"})
    assert result.is_error is False


async def test_tool_disabled_after_use_is_no_longer_callable(
    opsdesk: FastMCP, holder: RegistryHolder, write_registry: WriteRegistry, registry_data: dict[str, Any]
) -> None:
    async with Client(opsdesk) as client:
        warm = await client.call_tool("get_service_status", {"service": "api"})
        assert warm.is_error is False
        registry_data["servers"]["opsdesk"]["tools"]["get_service_status"]["enabled"] = False
        write_registry(registry_data)
        assert holder.refresh() is True
        result = await client.call_tool_mcp("get_service_status", {"service": "api"})
        assert result.is_error is True
        assert result.content == [mt.TextContent(type="text", text="Unknown tool: 'get_service_status'")]
        assert "get_service_status" not in {t.name for t in await client.list_tools()}


async def test_server_removed_from_registry_exposes_nothing_until_restart(
    opsdesk: FastMCP, holder: RegistryHolder, write_registry: WriteRegistry, registry_data: dict[str, Any]
) -> None:
    del registry_data["servers"]["opsdesk"]
    write_registry(registry_data)
    assert holder.refresh() is True
    assert await _names(opsdesk) == set()
    async with Client(opsdesk) as client:
        result = await client.call_tool_mcp("list_incidents", {})
    assert result.is_error is True
    assert result.content == [mt.TextContent(type="text", text="Unknown tool: 'list_incidents'")]


async def test_mrtr_confirmation_crosses_the_gateway(opsdesk: FastMCP, store: OpsDeskStore) -> None:
    async with Client(opsdesk, elicitation_handler=_confirm) as client:
        result = await client.call_tool("restart_service", {"service": "search"})
    assert result.structured_content == {"service": "search", "restarted": True, "state": "healthy"}
    assert store.service("search").restarts == 1


async def test_progress_crosses_the_gateway(opsdesk: FastMCP) -> None:
    seen: list[float] = []

    async def on_progress(progress: float, total: float | None, message: str | None) -> None:
        seen.append(progress)

    async with Client(opsdesk, progress_handler=on_progress) as client:
        await client.call_tool("run_diagnostics", {"service": "api"})
    assert seen == [1, 2, 3]


async def test_pager_tools_reach_the_http_api(servers: dict[str, FastMCP]) -> None:
    async with Client(servers["pager"]) as client:
        sent = await client.call_tool("send_page", {"service": "api", "message": "5xx"})
    assert sent.structured_content == {"id": 1, "service": "api", "message": "5xx"}


@pytest.fixture
def opsdesk_over_http(announced: Announced) -> Iterator[str]:
    """OpsDesk as a Streamable HTTP subprocess: the URL a `kind: http` source would name."""
    command = [sys.executable, "-m", "opsdesk", "--transport", "http", "--port", "0"]
    with announced(command, r"OpsDesk listening on (http://\S+/mcp)") as match:
        yield match.group(1)


async def test_an_http_source_carries_mrtr_and_redaction_to_a_live_backend(
    opsdesk_over_http: str, write_registry: WriteRegistry, registry_data: dict[str, Any]
) -> None:
    # The other two source kinds have end-to-end tests; this is the one for `kind: http`.
    registry_data["servers"]["opsdesk"]["source"] = {"kind": "http", "url": opsdesk_over_http}
    holder = RegistryHolder(write_registry(registry_data))
    targets = resolve_targets(holder.current)
    try:
        server = build_gateway_servers(holder, {"opsdesk": targets["opsdesk"]})["opsdesk"]
        async with Client(server, elicitation_handler=_confirm) as client:
            assert "update_runbook" not in {t.name for t in await client.list_tools()}
            restarted = await client.call_tool("restart_service", {"service": "search"})
            incidents = await client.call_tool("list_incidents", {})
            status = await client.call_tool("get_service_status", {"service": "search"})
    finally:
        await targets.aclose()
    assert restarted.structured_content == {"service": "search", "restarted": True, "state": "healthy"}
    assert status.structured_content == {"name": "search", "state": "healthy", "restarts": 1}
    assert incidents.structured_content is not None
    assert "@example.com" not in str(incidents.structured_content)
    assert "[REDACTED:email]" in str(incidents.structured_content)
