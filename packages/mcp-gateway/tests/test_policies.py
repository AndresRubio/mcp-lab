"""GatewayPolicyMiddleware: live registry refresh, redaction and per-caller rate limits."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import mcp_types as mt
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from mcp import MCPError
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.prompts.base import UserMessage

from gateway.app import build_gateway_servers
from gateway.policies import GatewayPolicyMiddleware, caller_of
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
        results = [await client.call_tool("list_incidents", {}) for _ in range(5)]
    assert all(result.is_error is False for result in results)


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


async def test_replayed_request_state_cannot_exceed_the_rate_limit(
    limited: FastMCP, store: OpsDeskStore
) -> None:
    # A sealed requestState stays valid for its whole lifetime and is not bound to the caller, so
    # replaying one is possible. Each replay that executes must count; once the window is used up
    # the replays are rejected before they reach the backend. `payments` is already healthy, so its
    # confirmation question stays the same across restarts.
    async with Client(limited, client_info=_as("mallory")) as client:
        first = await client.session.call_tool(
            "restart_service", {"service": "payments"}, allow_input_required=True
        )
        assert isinstance(first, mt.InputRequiredResult)
        assert first.input_requests and first.request_state
        accept: mt.InputResponses = {
            key: mt.ElicitResult(action="accept", content={"confirm": True}) for key in first.input_requests
        }
        replays = [
            await client.session.call_tool(
                "restart_service",
                {"service": "payments"},
                allow_input_required=True,
                input_responses=accept,
                request_state=first.request_state,
            )
            for _ in range(5)
        ]
    assert store.service("payments").restarts == 2
    executed, rejected = replays[:2], replays[2:]
    for result in executed:
        assert isinstance(result, mt.CallToolResult)
        assert result.is_error is not True
        assert result.structured_content == {"service": "payments", "restarted": True, "state": "healthy"}
    for result in rejected:
        assert isinstance(result, mt.CallToolResult)
        assert result.is_error is True
        [text] = result.content
        assert isinstance(text, mt.TextContent)
        assert (
            text.text
            == "Rate limit exceeded: caller 'mallory' may make 2 calls per minute to server 'opsdesk'."
        )


def _contact_backend() -> MCPServer:
    """A backend whose tool and prompt hand back an address in an embedded resource and a link."""
    backend = MCPServer(name="contacts")
    embedded = mt.EmbeddedResource(
        type="resource",
        resource=mt.TextResourceContents(
            uri="contact://jane@example.com", mime_type="text/plain", text="Owner: jane@example.com"
        ),
    )
    link = mt.ResourceLink(
        type="resource_link",
        name="jane@example.com",
        title="Card for jane@example.com",
        description="Write to jane@example.com",
        uri="mailto:jane@example.com",
    )

    @backend.tool()
    def owner_card() -> list[mt.ContentBlock]:
        """The service owner's contact card."""
        return [embedded, link]

    @backend.prompt()
    def escalate() -> list[UserMessage]:
        """Escalate to the service owner, jane@example.com."""
        return [UserMessage(embedded), UserMessage(link)]

    return backend


@pytest.fixture
def contacts(write_registry: WriteRegistry, registry_data: dict[str, Any]) -> FastMCP:
    registry_data["servers"]["contacts"] = {
        "owner": "sre",
        "source": {"kind": "stdio", "command": ["contacts"]},
        "tools": {"owner_card": {"enabled": True}},
    }
    holder = RegistryHolder(write_registry(registry_data))
    return build_gateway_servers(holder, {"contacts": _contact_backend()})["contacts"]


async def test_embedded_resources_and_resource_links_in_tool_results_are_redacted(contacts: FastMCP) -> None:
    async with Client(contacts) as client:
        result = await client.call_tool("owner_card", {})
    embedded, link = result.content
    assert isinstance(embedded, mt.EmbeddedResource)
    assert isinstance(embedded.resource, mt.TextResourceContents)
    assert embedded.resource.text == "Owner: [REDACTED:email]"
    assert embedded.resource.uri == "contact:[REDACTED:email]"  # `/` is atext, see gateway.redaction
    assert isinstance(link, mt.ResourceLink)
    assert (link.name, link.title, link.description, link.uri) == (
        "[REDACTED:email]",
        "Card for [REDACTED:email]",
        "Write to [REDACTED:email]",
        "mailto:[REDACTED:email]",
    )
    assert EMAIL_TAIL not in str(result)


async def test_embedded_resources_and_resource_links_in_prompts_are_redacted(contacts: FastMCP) -> None:
    async with Client(contacts) as client:
        result = await client.get_prompt("escalate", {})
    assert result.description == "Escalate to the service owner, [REDACTED:email]."
    embedded, link = result.messages
    assert isinstance(embedded.content, mt.EmbeddedResource)
    assert isinstance(embedded.content.resource, mt.TextResourceContents)
    assert embedded.content.resource.text == "Owner: [REDACTED:email]"
    # FastMCP's prompt Message has no ResourceLink arm, so the proxy flattens the link to JSON text.
    assert isinstance(link.content, mt.TextContent)
    assert '"uri":"mailto:[REDACTED:email]"' in link.content.text
    assert EMAIL_TAIL not in result.model_dump_json()


async def test_the_call_counts_hold_only_the_current_window(limited: FastMCP, clock: FakeClock) -> None:
    [policy] = [m for m in limited.middleware if isinstance(m, GatewayPolicyMiddleware)]
    for name in ("alice", "bob", "carol"):
        async with Client(limited, client_info=_as(name)) as client:
            await client.call_tool("list_incidents", {})
    assert set(policy.call_counts()) == {"alice", "bob", "carol"}
    clock.now += 60
    async with Client(limited, client_info=_as("dave")) as client:
        await client.call_tool("list_incidents", {})
    assert policy.call_counts() == {"dave": 1}


async def test_a_huge_caller_name_is_held_as_a_bounded_key(limited: FastMCP) -> None:
    # The name is self-asserted and unbounded, and the table keeps its keys for the whole window,
    # so each caller may pin only a small, fixed amount of memory there.
    [policy] = [m for m in limited.middleware if isinstance(m, GatewayPolicyMiddleware)]
    huge = "x" * 1_000_000
    async with Client(limited, client_info=_as(huge)) as client:
        await client.call_tool("list_incidents", {})
    [key] = policy.call_counts()
    assert len(key) <= 128
    assert key.startswith("x" * 32)  # still recognisable in logs and tests


@pytest.mark.parametrize("shared", [31, 32, 33, 63, 64, 65, 100_000])
async def test_names_that_differ_only_late_get_separate_limits(limited: FastMCP, shared: int) -> None:
    # Guards against a bound that truncates: names sharing their first `shared` characters must
    # not share a budget.
    [policy] = [m for m in limited.middleware if isinstance(m, GatewayPolicyMiddleware)]
    first, second = "a" * shared + "1", "a" * shared + "2"
    async with Client(limited, client_info=_as(first)) as client:
        for _ in range(2):
            await client.call_tool("list_incidents", {})
        with pytest.raises(ToolError, match="Rate limit exceeded"):
            await client.call_tool("list_incidents", {})
    async with Client(limited, client_info=_as(second)) as client:
        result = await client.call_tool("list_incidents", {})
    assert result.is_error is False
    assert sorted(policy.call_counts().values()) == [1, 2]


async def test_concurrent_replays_cannot_race_past_the_rate_limit(
    limited: FastMCP, store: OpsDeskStore
) -> None:
    # The slot is taken before the call is forwarded, so replays in flight together cannot all
    # pass the check before any of them is counted.
    async with Client(limited, client_info=_as("mallory")) as client:
        first = await client.session.call_tool(
            "restart_service", {"service": "payments"}, allow_input_required=True
        )
        assert isinstance(first, mt.InputRequiredResult)
        assert first.input_requests and first.request_state
        accept: mt.InputResponses = {
            key: mt.ElicitResult(action="accept", content={"confirm": True}) for key in first.input_requests
        }
        replays = await asyncio.gather(
            *(
                client.session.call_tool(
                    "restart_service",
                    {"service": "payments"},
                    allow_input_required=True,
                    input_responses=accept,
                    request_state=first.request_state,
                )
                for _ in range(6)
            )
        )
    assert store.service("payments").restarts == 2
    assert sum(isinstance(r, mt.CallToolResult) and r.is_error is True for r in replays) == 4


async def test_a_clock_running_backwards_keeps_the_current_window(limited: FastMCP, clock: FakeClock) -> None:
    async with Client(limited, client_info=_as("alice")) as client:
        for _ in range(2):
            await client.call_tool("list_incidents", {})
        clock.now -= 60
        with pytest.raises(ToolError, match="Rate limit exceeded"):
            await client.call_tool("list_incidents", {})


async def test_calls_that_fail_at_the_backend_still_count(limited: FastMCP) -> None:
    async with Client(limited, client_info=_as("alice")) as client:
        for _ in range(2):
            with pytest.raises(ToolError, match="Unknown service 'nope'"):
                await client.call_tool("get_service_status", {"service": "nope"})
        with pytest.raises(ToolError, match="Rate limit exceeded"):
            await client.call_tool("list_incidents", {})
