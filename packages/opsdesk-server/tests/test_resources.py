import anyio
import pytest
from mcp import Client, MCPError
from mcp.server.mcpserver import MCPServer
from mcp.shared.subscriptions import ResourceUpdated
from mcp.types import TextResourceContents

from opsdesk.data import OpsDeskStore

pytestmark = pytest.mark.anyio


async def test_runbook_template_is_listed_with_cache_hints(server: MCPServer) -> None:
    async with Client(server) as client:
        result = await client.list_resource_templates()
    assert [t.uri_template for t in result.resource_templates] == ["runbook://{service}"]
    assert (result.ttl_ms, result.cache_scope) == (60_000, "public")


async def test_read_runbook_returns_markdown_with_private_cache_hint(server: MCPServer) -> None:
    async with Client(server) as client:
        result = await client.read_resource("runbook://api")
    [content] = result.contents
    assert isinstance(content, TextResourceContents)
    assert content.mime_type == "text/markdown"
    assert content.text.startswith("# api runbook")
    assert (result.ttl_ms, result.cache_scope) == (5_000, "private")


async def test_missing_runbook_is_invalid_params_32602(server: MCPServer) -> None:
    # 2026-07-28 moved "resource not found" from -32002 to -32602 (Invalid Params).
    async with Client(server) as client:
        with pytest.raises(MCPError) as exc:
            await client.read_resource("runbook://nope")
    assert exc.value.error.code == -32602


async def test_update_runbook_notifies_listeners_and_evicts_client_cache(server: MCPServer) -> None:
    async with Client(server) as client:
        before = await client.read_resource("runbook://api")  # now cached for 5s
        async with client.listen(resource_subscriptions=["runbook://api"]) as sub:
            assert sub.honored.resource_subscriptions == ["runbook://api"]
            await client.call_tool("update_runbook", {"service": "api", "markdown": "# api v2"})
            with anyio.fail_after(3):
                event = await anext(aiter(sub))
        after = await client.read_resource("runbook://api")
    assert event == ResourceUpdated(uri="runbook://api")
    assert isinstance(before.contents[0], TextResourceContents)
    assert isinstance(after.contents[0], TextResourceContents)
    assert before.contents[0].text != after.contents[0].text == "# api v2"


async def test_update_runbook_for_unknown_service_changes_nothing(
    server: MCPServer, store: OpsDeskStore
) -> None:
    before = dict(store.runbooks)
    async with Client(server) as client:
        result = await client.call_tool("update_runbook", {"service": "nope", "markdown": "# hijack"})
    assert result.is_error is True
    assert store.runbooks == before
