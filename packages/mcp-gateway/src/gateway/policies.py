"""Per-request gateway policy: live registry refresh, PII redaction and per-caller rate limits."""

import time
from collections.abc import Callable
from typing import Any

import mcp_types as mt
from fastmcp.exceptions import ToolError
from fastmcp.prompts.base import InputRequiredPromptResult, Message, PromptResult
from fastmcp.resources.base import InputRequiredResourceResult, ResourceContent, ResourceResult
from fastmcp.server.context import Context
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import InputRequiredToolResult, ToolResult

from gateway.redaction import redact
from gateway.registry import RegistryHolder

WINDOW_SECONDS = 60


def caller_of(ctx: Context | None) -> str:
    """The caller's identity: `clientInfo.name`, from the legacy handshake or the 2026 `_meta`.

    This is self-asserted and header-free, which is enough to show per-caller policy but not
    to enforce it. M2b replaces it with the subject of a verified OAuth access token.
    """
    request = ctx.request_context if ctx is not None else None
    params = request.session.client_params if request is not None else None
    return params.client_info.name if params is not None else "anonymous"


def _redact_content(block: mt.ContentBlock) -> mt.ContentBlock:
    if isinstance(block, mt.TextContent):
        return block.model_copy(update={"text": redact(block.text)})
    return block


class GatewayPolicyMiddleware(Middleware):
    """Policy for one backend server, applied to every request that reaches its proxy.

    - Calls `holder.refresh()` first, so registry edits apply from the next request on.
    - Redacts e-mail addresses in tool results (text and structured), resource reads and prompt
      messages. `InputRequired*` results are MRTR legs with nothing to redact: they pass through
      untouched, or the front client would never see the question.
    - Limits each caller to `rate_limit.calls_per_minute` tool calls per fixed one-minute window.
      On a 2026-07-28 front every MRTR round is its own `tools/call`, so only the first round is
      counted: a retry carries `request_state`, which the gateway sealed itself.
    """

    def __init__(
        self, holder: RegistryHolder, server_name: str, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.holder = holder
        self.server_name = server_name
        self.clock = clock
        self._windows: dict[str, tuple[int, int]] = {}  # caller -> (window index, calls in it)

    async def on_request(
        self, context: MiddlewareContext[mt.Request[Any, Any]], call_next: CallNext[mt.Request[Any, Any], Any]
    ) -> Any:
        self.holder.refresh()
        return await call_next(context)

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        if context.message.request_state is None:
            self._count(caller_of(context.fastmcp_context))
        result = await call_next(context)
        if isinstance(result, InputRequiredToolResult):
            return result
        return ToolResult(
            content=[_redact_content(block) for block in result.content],
            structured_content=redact(result.structured_content),
            meta=result.meta,
            is_error=result.is_error,
        )

    async def on_read_resource(
        self,
        context: MiddlewareContext[mt.ReadResourceRequestParams],
        call_next: CallNext[mt.ReadResourceRequestParams, ResourceResult],
    ) -> ResourceResult:
        result = await call_next(context)
        if isinstance(result, InputRequiredResourceResult):
            return result
        return ResourceResult(
            contents=[
                ResourceContent(redact(c.content), mime_type=c.mime_type, meta=c.meta)
                if isinstance(c.content, str)
                else c
                for c in result.contents
            ],
            meta=result.meta,
        )

    async def on_get_prompt(
        self,
        context: MiddlewareContext[mt.GetPromptRequestParams],
        call_next: CallNext[mt.GetPromptRequestParams, PromptResult],
    ) -> PromptResult:
        result = await call_next(context)
        if isinstance(result, InputRequiredPromptResult):
            return result
        return PromptResult(
            [Message(_redact_content(m.content), role=m.role) for m in result.messages],
            description=result.description,
            meta=result.meta,
        )

    def _count(self, caller: str) -> None:
        entry = self.holder.current.servers.get(self.server_name)
        if entry is None or entry.rate_limit is None:
            return
        limit = entry.rate_limit.calls_per_minute
        window = int(self.clock() // WINDOW_SECONDS)
        start, calls = self._windows.get(caller, (window, 0))
        calls = calls + 1 if start == window else 1
        self._windows[caller] = (window, calls)
        if calls > limit:
            raise ToolError(
                f"Rate limit exceeded: caller '{caller}' may make {limit} calls per minute "
                f"to server '{self.server_name}'."
            )
