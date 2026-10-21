"""One way to turn an OpenAPI spec into an MCP server, for `resolve_targets` and the Pager demo alike."""

from collections.abc import Mapping
from typing import Any

import httpx2
from fastmcp import FastMCP


def openapi_server(
    spec: dict[str, Any], client: httpx2.AsyncClient, *, name: str, mcp_names: Mapping[str, str]
) -> FastMCP:
    """An MCP server with one tool per operation, named by `mcp_names` (operationId -> tool name).

    fastmcp raises `ValueError` for a spec that is present but malformed.
    """
    return FastMCP.from_openapi(spec, client=client, name=name, mcp_names=dict(mcp_names))
