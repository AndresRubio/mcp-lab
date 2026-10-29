from collections.abc import Awaitable, Callable

import pytest
from mcp import Client
from mcp.server.caching import CACHEABLE_METHODS
from mcp.server.mcpserver import MCPServer
from mcp.types import CacheableResult, TextContent

pytestmark = pytest.mark.anyio

LIST_CALLS: dict[str, Callable[[Client], Awaitable[CacheableResult]]] = {
    "tools/list": lambda client: client.list_tools(),
    "prompts/list": lambda client: client.list_prompts(),
    "resources/list": lambda client: client.list_resources(),
    "resources/templates/list": lambda client: client.list_resource_templates(),
}


# Parametrized over the SDK's cacheable list methods, so a list method without an entry in
# LIST_CALLS fails with a KeyError instead of going untested.
@pytest.mark.parametrize("method", sorted(m for m in CACHEABLE_METHODS if m.endswith("/list")))
async def test_every_list_result_carries_public_60s_cache_hint(server: MCPServer, method: str) -> None:
    async with Client(server) as client:
        result = await LIST_CALLS[method](client)
    assert (result.ttl_ms, result.cache_scope) == (60_000, "public")


async def test_every_tool_has_description_and_output_schema(server: MCPServer) -> None:
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
    for tool in tools:
        assert tool.description, tool.name
    assert all(t.output_schema is not None for t in tools if t.name != "update_runbook")


async def test_get_service_status_is_annotated_read_only(server: MCPServer) -> None:
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert tools["get_service_status"].annotations is not None
    assert tools["get_service_status"].annotations.read_only_hint is True


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"status": "open"}, ["INC-101", "INC-102"]),
        ({"status": "resolved"}, ["INC-099"]),
        ({}, ["INC-101", "INC-102", "INC-099"]),
    ],
)
async def test_list_incidents_filters_by_status(
    server: MCPServer, arguments: dict[str, str], expected: list[str]
) -> None:
    async with Client(server) as client:
        result = await client.call_tool("list_incidents", arguments)
    assert result.is_error is False
    assert result.structured_content is not None
    assert [i["id"] for i in result.structured_content["incidents"]] == expected


async def test_get_service_status_returns_structured_content(server: MCPServer) -> None:
    async with Client(server) as client:
        result = await client.call_tool("get_service_status", {"service": "search"})
    assert result.structured_content == {"name": "search", "state": "down", "restarts": 0}


async def test_unknown_service_is_a_tool_error_not_a_protocol_error(server: MCPServer) -> None:
    async with Client(server) as client:
        result = await client.call_tool("get_service_status", {"service": "nope"})
    assert result.is_error is True
    [content] = result.content
    assert isinstance(content, TextContent)
    assert "Unknown service 'nope'" in content.text


async def test_restart_service_is_annotated_destructive(server: MCPServer) -> None:
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert tools["restart_service"].annotations is not None
    assert tools["restart_service"].annotations.destructive_hint is True


async def test_tools_are_listed_in_deterministic_order(server: MCPServer) -> None:
    # 2026-07-28: servers SHOULD return tools/list in a stable order (client and LLM prompt caching).
    async with Client(server) as client:
        first = [t.name for t in (await client.list_tools()).tools]
    async with Client(server) as client:
        second = [t.name for t in (await client.list_tools()).tools]
    assert (
        first
        == second
        == [
            "list_incidents",
            "get_service_status",
            "restart_service",
            "run_diagnostics",
            "update_runbook",
        ]
    )


async def test_all_tools_are_closed_world_and_update_runbook_is_destructive(server: MCPServer) -> None:
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert all(t.annotations is not None and t.annotations.open_world_hint is False for t in tools.values())
    assert tools["update_runbook"].annotations is not None
    assert tools["update_runbook"].annotations.destructive_hint is True
