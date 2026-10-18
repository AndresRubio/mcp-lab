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
    if isinstance(block, mt.EmbeddedResource) and isinstance(block.resource, mt.TextResourceContents):
        resource = block.resource.model_copy(
            update={"text": redact(block.resource.text), "uri": redact(block.resource.uri)}
        )
        return block.model_copy(update={"resource": resource})
    if isinstance(block, mt.ResourceLink):
        fields = ("name", "title", "description", "uri")
        return block.model_copy(update={field: redact(getattr(block, field)) for field in fields})
    return block


class GatewayPolicyMiddleware(Middleware):
    """Policy for one backend server, applied to every request that reaches its proxy.

    - Calls `holder.refresh()` first, so registry edits apply from the next request on.
    - Redacts e-mail addresses in tool results (text and structured content), resource reads, and
      prompt messages and descriptions. Inside content blocks that covers `TextContent`, the text and
      URI of an `EmbeddedResource` holding `TextResourceContents`, and a `ResourceLink`'s name,
      title, description and URI. `InputRequired*` results are MRTR legs with nothing to redact:
      they pass through untouched, or the front client would never see the question.
    - Not redacted: `meta` fields, blob resources, list results (tools, resources, prompts),
      completions, and JSON-RPC error messages coming back from a backend.
    - Limits each caller to `rate_limit.calls_per_minute` executed tool calls per fixed one-minute
      window. Every round is checked: a call is rejected once the caller's count for the current
      window has reached the limit. A slot is reserved before the call is forwarded, so concurrent
      calls cannot all pass the check, and handed back only when the backend answers with an
      `InputRequiredToolResult`. A whole MRTR exchange therefore counts once, while every replay of
      a sealed `requestState` that executes counts again. Results with `is_error=True` and calls
      that raise still count, because the call reached the backend.

    Rate-limit trade-offs: fixed windows let a caller make up to 2N calls across a window boundary;
    calls to unknown or hidden tools count; an MRTR first round briefly holds a slot while it is in
    flight, and rounds that end in `InputRequiredToolResult` are checked but never counted, so a
    caller below the limit can make any number of them. A clock that runs backwards keeps the
    current window rather than opening a new one. Only the current window's counts are kept, so
    memory is bounded by the callers seen in one window. The caller name is self-asserted
    (`clientInfo.name`) until M2b replaces it with a verified identity.
    """

    def __init__(
        self, holder: RegistryHolder, server_name: str, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.holder = holder
        self.server_name = server_name
        self.clock = clock
        self._window: int | None = None  # index of the window `_calls` belongs to
        self._calls: dict[str, int] = {}  # caller -> calls counted in `_window`

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
        caller = caller_of(context.fastmcp_context)
        reserved = self._reserve(caller)
        result = await call_next(context)
        if isinstance(result, InputRequiredToolResult):
            if reserved is not None:
                self._release(caller, reserved)
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
            description=redact(result.description),
            meta=result.meta,
        )

    def call_counts(self) -> dict[str, int]:
        """A copy of the calls counted per caller in the current window."""
        return dict(self._calls)

    def _current_window(self) -> int:
        window = int(self.clock() // WINDOW_SECONDS)
        if self._window is None or window > self._window:
            self._window = window
            self._calls.clear()
        return self._window

    def _reserve(self, caller: str) -> int | None:
        """Take one of `caller`'s slots in the current window and return that window's index.

        Returns None when the server has no rate limit. Raises `ToolError` when the slots are used up.
        Check and increment happen with no `await` between them, so concurrent calls cannot race.
        """
        entry = self.holder.current.servers.get(self.server_name)
        if entry is None or entry.rate_limit is None:
            return None
        limit = entry.rate_limit.calls_per_minute
        window = self._current_window()
        calls = self._calls.get(caller, 0)
        if calls >= limit:
            raise ToolError(
                f"Rate limit exceeded: caller '{caller}' may make {limit} calls per minute "
                f"to server '{self.server_name}'."
            )
        self._calls[caller] = calls + 1
        return window

    def _release(self, caller: str, window: int) -> None:
        """Hand back a slot taken in `window`, unless that window has since rolled over."""
        if window != self._window:
            return
        calls = self._calls.get(caller, 0) - 1
        if calls > 0:
            self._calls[caller] = calls
        else:
            self._calls.pop(caller, None)
