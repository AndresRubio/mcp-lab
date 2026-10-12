import pytest
from mcp import Client, MCPError
from mcp.server.mcpserver import MCPServer
from mcp.types import TextContent

pytestmark = pytest.mark.anyio


async def test_postmortem_prompt_is_listed_with_its_argument(server: MCPServer) -> None:
    async with Client(server) as client:
        result = await client.list_prompts()
    [prompt] = result.prompts
    assert prompt.name == "postmortem"
    assert prompt.description
    assert [a.name for a in prompt.arguments or []] == ["incident_id"]


async def test_postmortem_prompt_embeds_incident_context(server: MCPServer) -> None:
    async with Client(server) as client:
        result = await client.get_prompt("postmortem", {"incident_id": "INC-101"})
    [message] = result.messages
    assert message.role == "user"
    assert isinstance(message.content, TextContent)
    assert "INC-101" in message.content.text
    assert "Root cause" in message.content.text


async def test_unknown_incident_is_invalid_params_32602(server: MCPServer) -> None:
    async with Client(server) as client:
        with pytest.raises(MCPError, match="INC-404") as exc:
            await client.get_prompt("postmortem", {"incident_id": "INC-404"})
    assert exc.value.error.code == -32602
