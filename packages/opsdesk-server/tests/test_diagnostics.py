import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer

pytestmark = pytest.mark.anyio


async def test_run_diagnostics_reports_progress_for_each_check(server: MCPServer) -> None:
    seen: list[tuple[float, float | None, str | None]] = []

    # The parameter must be named `progress`: ProgressFnT is a Protocol and pyright checks names.
    async def on_progress(progress: float, total: float | None, message: str | None) -> None:
        seen.append((progress, total, message))

    async with Client(server) as client:
        result = await client.call_tool("run_diagnostics", {"service": "api"}, progress_callback=on_progress)

    assert seen == [
        (1, 3, "checking connectivity"),
        (2, 3, "checking error-rate"),
        (3, 3, "checking latency"),
    ]
    assert result.structured_content == {
        "service": "api",
        "state": "degraded",
        "checks": [
            "connectivity: needs attention",
            "error-rate: needs attention",
            "latency: needs attention",
        ],
    }
