"""OpsDesk MCP server, built directly on the official MCP Python SDK v2 (`MCPServer`).

No `from __future__ import annotations` here: MCPServer evaluates tool signatures when they are
registered, and stringified annotations cannot see resolvers local to `build_server`.
"""

from mcp.server.caching import CacheableMethod, CacheHint
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from opsdesk import __version__
from opsdesk.data import (
    Incident,
    IncidentStatus,
    OpsDeskStore,
    Service,
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

    return server
