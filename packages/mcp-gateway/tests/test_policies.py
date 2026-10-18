"""GatewayPolicyMiddleware: live registry refresh, redaction and per-caller rate limits."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import mcp_types as mt
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from mcp import MCPError

from gateway.app import build_gateway_servers
from gateway.policies import caller_of
from gateway.registry import RegistryHolder
from opsdesk.data import OpsDeskStore
from opsdesk.server import build_server

pytestmark = pytest.mark.anyio

WriteRegistry = Callable[[Any], Path]
EMAIL_TAIL = "@example.com"


class FakeClock:
    def __init__(self) -> None:
        self.now = 600.0  # the start of a one-minute window

    def __call__(self) -> float:
        return self.now


async def _confirm(message: str, response_type: Any, params: Any, context: Any) -> dict[str, Any]:
    return {"confirm": True}


def _as(name: str) -> mt.Implementation:
    return mt.Implementation(name=name, version="1.0")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def limited(
    write_registry: WriteRegistry, registry_data: dict[str, Any], store: OpsDeskStore, clock: FakeClock
) -> FastMCP:
    """OpsDesk behind the gateway with a limit of 2 calls per minute per caller."""
    registry_data["servers"]["opsdesk"]["rate_limit"] = {"calls_per_minute": 2}
    holder = RegistryHolder(write_registry(registry_data))
    return build_gateway_servers(holder, {"opsdesk": build_server(store)}, clock=clock)["opsdesk"]


async def test_each_request_refreshes_the_registry(
    opsdesk: FastMCP, write_registry: WriteRegistry, registry_data: dict[str, Any]
) -> None:
    async with Client(opsdesk) as client:
        assert "update_runbook" not in {t.name for t in await client.list_tools()}
        registry_data["servers"]["opsdesk"]["tools"]["update_runbook"]["enabled"] = True
        write_registry(registry_data)  # no holder.refresh() here: the middleware does it
        assert "update_runbook" in {t.name for t in await client.list_tools()}


async def test_tool_text_and_structured_content_are_redacted(opsdesk: FastMCP) -> None:
    async with Client(opsdesk) as client:
        result = await client.call_tool("list_incidents", {})
    [text] = result.content
    assert isinstance(text, mt.TextContent)
    assert EMAIL_TAIL not in text.text
    assert "[REDACTED:email]" in text.text
    assert result.structured_content is not None
    descriptions = [i["description"] for i in result.structured_content["incidents"]]
    assert descriptions[0] == "Reported by customer [REDACTED:email]: checkout fails intermittently."
    assert EMAIL_TAIL not in str(result.data)


async def test_resource_reads_are_redacted(opsdesk: FastMCP, store: OpsDeskStore) -> None:
    store.runbooks["api"] += "\nEscalate to lead@example.com."
    async with Client(opsdesk) as client:
        [content] = await client.read_resource("runbook://api")
    assert isinstance(content, mt.TextResourceContents)
    assert content.text.endswith("Escalate to [REDACTED:email].")
    assert content.mime_type == "text/markdown"


async def test_prompt_messages_are_redacted(opsdesk: FastMCP) -> None:
    async with Client(opsdesk) as client:
        result = await client.get_prompt("postmortem", {"incident_id": "INC-101"})
    [message] = result.messages
    assert isinstance(message.content, mt.TextContent)
    assert "Reported by customer [REDACTED:email]" in message.content.text
    assert message.role == "user"


async def test_openapi_backend_results_are_redacted(servers: dict[str, FastMCP]) -> None:
    async with Client(servers["pager"]) as client:
        result = await client.call_tool("oncall_for", {"service": "api"})
    assert result.structured_content == {
        "service": "api",
        "primary": "[REDACTED:email]",
        "secondary": "[REDACTED:email]",
    }


async def test_input_required_round_passes_through_untouched(opsdesk: FastMCP, store: OpsDeskStore) -> None:
    async with Client(opsdesk, elicitation_handler=_confirm) as client:
        first = await client.session.call_tool(
            "restart_service", {"service": "api"}, allow_input_required=True
        )
    assert isinstance(first, mt.InputRequiredResult)
    assert first.input_requests
    assert first.request_state
    assert store.service("api").restarts == 0


async def test_confirmed_restart_counts_as_one_call(limited: FastMCP, store: OpsDeskStore) -> None:
    async with Client(limited, elicitation_handler=_confirm, client_info=_as("alice")) as client:
        restarted = await client.call_tool("restart_service", {"service": "search"})
        second = await client.call_tool("get_service_status", {"service": "search"})
        with pytest.raises(ToolError) as exceeded:
            await client.call_tool("get_service_status", {"service": "search"})
    assert restarted.structured_content == {"service": "search", "restarted": True, "state": "healthy"}
    assert second.is_error is False
    assert str(exceeded.value) == (
        "Rate limit exceeded: caller 'alice' may make 2 calls per minute to server 'opsdesk'."
    )
    assert store.service("search").restarts == 1


async def test_callers_are_limited_independently(limited: FastMCP) -> None:
    async with Client(limited, client_info=_as("alice")) as alice:
        for _ in range(2):
            await alice.call_tool("list_incidents", {})
        with pytest.raises(ToolError, match="caller 'alice'"):
            await alice.call_tool("list_incidents", {})
    async with Client(limited, client_info=_as("bob")) as bob:
        result = await bob.call_tool("list_incidents", {})
    assert result.is_error is False


async def test_the_window_resets_with_the_clock(limited: FastMCP, clock: FakeClock) -> None:
    async with Client(limited, client_info=_as("alice")) as client:
        for _ in range(2):
            await client.call_tool("list_incidents", {})
        clock.now += 30
        with pytest.raises(ToolError, match="Rate limit exceeded"):
            await client.call_tool("list_incidents", {})
        clock.now += 30
        result = await client.call_tool("list_incidents", {})
    assert result.is_error is False


async def test_rate_limit_changes_apply_live(
    limited: FastMCP, write_registry: WriteRegistry, registry_data: dict[str, Any]
) -> None:
    async with Client(limited, client_info=_as("alice")) as client:
        for _ in range(2):
            await client.call_tool("list_incidents", {})
        registry_data["servers"]["opsdesk"]["rate_limit"] = {"calls_per_minute": 3}
        write_registry(registry_data)
        result = await client.call_tool("list_incidents", {})
    assert result.is_error is False


async def test_servers_without_a_rate_limit_are_unlimited(opsdesk: FastMCP) -> None:
    async with Client(opsdesk) as client:
        for _ in range(5):
            await client.call_tool("list_incidents", {})


async def test_forged_request_state_is_rejected_before_it_reaches_the_backend(
    limited: FastMCP, store: OpsDeskStore
) -> None:
    # The gateway seals request state with its own key, so a client cannot skip the rate limit
    # by sending a made-up retry: the boundary rejects it before any middleware runs.
    async with Client(limited, client_info=_as("mallory")) as client:
        for _ in range(3):
            with pytest.raises(MCPError, match="Invalid or expired requestState"):
                await client.session.call_tool(
                    "restart_service",
                    {"service": "search"},
                    allow_input_required=True,
                    input_responses={"x": mt.ElicitResult(action="accept", content={"confirm": True})},
                    request_state="forged",
                )
        allowed = await client.call_tool("list_incidents", {})  # the forged retries were not counted
    assert allowed.is_error is False
    assert store.service("search").restarts == 0


def test_caller_without_a_request_context_is_anonymous() -> None:
    assert caller_of(None) == "anonymous"
