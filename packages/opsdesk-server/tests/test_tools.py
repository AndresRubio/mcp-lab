import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import TextContent

pytestmark = pytest.mark.anyio


async def test_tools_list_carries_public_cache_hint(server: MCPServer) -> None:
    async with Client(server) as client:
        result = await client.list_tools()
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


async def test_list_incidents_filters_by_status(server: MCPServer) -> None:
    async with Client(server) as client:
        result = await client.call_tool("list_incidents", {"status": "open"})
    assert result.is_error is False
    assert result.structured_content is not None
    assert [i["id"] for i in result.structured_content["incidents"]] == ["INC-101", "INC-102"]


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
