"""restart_service: confirmation via Multi Round-Trip Requests (SEP-2322, MCP 2026-07-28)."""

import pytest
from mcp import Client, MCPError
from mcp.client.session import ClientRequestContext, ElicitationFnT
from mcp.server.mcpserver import MCPServer
from mcp.types import ElicitRequest, ElicitRequestParams, ElicitResult, InputRequiredResult

from opsdesk.data import OpsDeskStore

pytestmark = pytest.mark.anyio


async def test_confirmation_is_not_part_of_the_input_schema(server: MCPServer) -> None:
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools["restart_service"].input_schema["properties"]) == {"service"}


async def test_accepted_confirmation_restarts_the_service(
    server: MCPServer, store: OpsDeskStore, accept_restart: ElicitationFnT
) -> None:
    async with Client(server, elicitation_callback=accept_restart) as client:
        result = await client.call_tool("restart_service", {"service": "search"})
    assert result.structured_content == {"service": "search", "restarted": True, "state": "healthy"}
    assert store.service("search").restarts == 1


async def test_declined_confirmation_leaves_the_service_alone(
    server: MCPServer, store: OpsDeskStore, decline: ElicitationFnT
) -> None:
    async with Client(server, elicitation_callback=decline) as client:
        result = await client.call_tool("restart_service", {"service": "search"})
    assert result.structured_content == {"service": "search", "restarted": False, "state": "down"}
    assert store.service("search").restarts == 0


async def test_first_round_returns_input_required_then_retry_completes(
    server: MCPServer, decline: ElicitationFnT
) -> None:
    # Drive the MRTR loop by hand to see the wire-level shape.
    async with Client(server, elicitation_callback=decline) as client:
        first = await client.session.call_tool(
            "restart_service", {"service": "api"}, allow_input_required=True
        )
        assert isinstance(first, InputRequiredResult)
        assert first.result_type == "input_required"
        assert first.request_state  # sealed by the SDK; opaque to the client
        assert first.input_requests is not None
        [(key, request)] = first.input_requests.items()
        assert isinstance(request, ElicitRequest)
        assert "Restart 'api'" in request.params.message

        second = await client.session.call_tool(
            "restart_service",
            {"service": "api"},
            allow_input_required=True,
            input_responses={key: ElicitResult(action="accept", content={"confirm": True})},
            request_state=first.request_state,
        )
    assert not isinstance(second, InputRequiredResult)
    assert second.structured_content == {"service": "api", "restarted": True, "state": "healthy"}


async def test_unknown_service_fails_before_asking_the_user(server: MCPServer) -> None:
    asked: list[str] = []

    async def record(context: ClientRequestContext, params: ElicitRequestParams) -> ElicitResult:
        asked.append(params.message)
        return ElicitResult(action="accept", content={"confirm": True})

    async with Client(server, elicitation_callback=record) as client:
        result = await client.call_tool("restart_service", {"service": "nope"})
    assert result.is_error is True
    assert asked == []


async def test_client_without_elicitation_capability_is_rejected(server: MCPServer) -> None:
    async with Client(server) as client:
        with pytest.raises(MCPError) as exc:
            await client.call_tool("restart_service", {"service": "api"})
    assert exc.value.error.code == -32021  # MissingRequiredClientCapability


async def test_legacy_2025_11_25_client_gets_a_standalone_elicitation(
    server: MCPServer, accept_restart: ElicitationFnT
) -> None:
    async with Client(server, mode="legacy", elicitation_callback=accept_restart) as client:
        assert client.protocol_version == "2025-11-25"
        result = await client.call_tool("restart_service", {"service": "api"})
    assert result.structured_content == {"service": "api", "restarted": True, "state": "healthy"}
