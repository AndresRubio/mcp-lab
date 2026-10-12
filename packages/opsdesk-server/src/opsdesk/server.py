"""OpsDesk MCP server, built directly on the official MCP Python SDK v2 (`MCPServer`).

No `from __future__ import annotations` here: MCPServer evaluates tool signatures when they are
registered, and stringified annotations cannot see resolvers local to `build_server`.
"""

from typing import Annotated

from mcp.server.caching import CacheableMethod, CacheHint
from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from opsdesk import __version__
from opsdesk.data import (
    Incident,
    IncidentStatus,
    OpsDeskStore,
    Service,
    ServiceState,
    UnknownServiceError,
    seed_store,
)

# 2026-07-28 CacheableResult hints (SEP-2549). Catalogue listings change rarely and are the
# same for every caller ("public"); resource reads are per-caller and short-lived ("private").
CACHE_HINTS: dict[CacheableMethod, CacheHint] = {
    "tools/list": CacheHint(ttl_ms=60_000, scope="public"),
    "prompts/list": CacheHint(ttl_ms=60_000, scope="public"),
    "resources/templates/list": CacheHint(ttl_ms=60_000, scope="public"),
    "resources/read": CacheHint(ttl_ms=5_000, scope="private"),
}


class IncidentList(BaseModel):
    incidents: list[Incident]


class RestartConfirmation(BaseModel):
    confirm: bool


class RestartOutcome(BaseModel):
    service: str
    restarted: bool
    state: ServiceState


class DiagnosticsReport(BaseModel):
    service: str
    state: ServiceState
    checks: list[str]


DIAGNOSTIC_CHECKS = ("connectivity", "error-rate", "latency")


def build_server(store: OpsDeskStore | None = None) -> MCPServer:
    """Build an OpsDesk server over `store` (a fresh seed store by default)."""
    store = store if store is not None else seed_store()
    server = MCPServer(
        name="opsdesk",
        title="OpsDesk",
        version=__version__,
        instructions="Incident and on-call tools. Always check service status before restarting.",
        cache_hints=CACHE_HINTS,
    )

    def require_service(name: str) -> Service:
        try:
            return store.service(name)
        except UnknownServiceError:
            known = ", ".join(sorted(store.services))
            raise ToolError(f"Unknown service '{name}'. Known services: {known}.") from None

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def list_incidents(status: IncidentStatus | None = None) -> IncidentList:
        """List incidents, optionally filtered by status ('open' or 'resolved')."""
        return IncidentList(incidents=store.list_incidents(status))

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def get_service_status(service: str) -> Service:
        """Get the current state of a service (healthy, degraded or down)."""
        return require_service(service)

    def confirm_restart(service: str) -> Elicit[RestartConfirmation]:
        # Resolver: runs before the tool body. Validating here means we never ask the user
        # to confirm a restart of a service that does not exist.
        svc = require_service(service)
        message = f"Restart '{service}' (currently {svc.state})? This interrupts traffic."
        return Elicit(message, RestartConfirmation)

    @server.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=False))
    def restart_service(
        service: str,
        confirmation: Annotated[ElicitationResult[RestartConfirmation], Resolve(confirm_restart)],
    ) -> RestartOutcome:
        """Restart a service. The user is asked to confirm first (multi round-trip elicitation)."""
        if isinstance(confirmation, AcceptedElicitation) and confirmation.data.confirm:
            svc = store.restart(service)
            return RestartOutcome(service=service, restarted=True, state=svc.state)
        return RestartOutcome(service=service, restarted=False, state=require_service(service).state)

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def run_diagnostics(service: str, ctx: Context) -> DiagnosticsReport:
        """Run connectivity, error-rate and latency checks on a service, reporting progress."""
        svc = require_service(service)
        verdict = "ok" if svc.state == "healthy" else "needs attention"
        checks: list[str] = []
        for step, check in enumerate(DIAGNOSTIC_CHECKS, start=1):
            await ctx.report_progress(step, len(DIAGNOSTIC_CHECKS), f"checking {check}")
            checks.append(f"{check}: {verdict}")
        return DiagnosticsReport(service=service, state=svc.state, checks=checks)

    @server.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=True))
    async def update_runbook(service: str, markdown: str, ctx: Context) -> str:
        """Replace a service's runbook. Subscribers of runbook://{service} are notified."""
        require_service(service)
        store.runbooks[service] = markdown
        await ctx.notify_resource_updated(f"runbook://{service}")
        return f"Runbook for '{service}' updated."

    @server.resource(
        "runbook://{service}", name="runbook", title="Service runbook", mime_type="text/markdown"
    )
    def runbook(service: str) -> str:
        """Operational runbook for a service."""
        try:
            return store.runbooks[service]
        except KeyError:
            raise ResourceNotFoundError(f"No runbook for service '{service}'.") from None

    return server
