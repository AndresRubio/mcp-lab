import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import TextContent

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


async def test_run_diagnostics_on_healthy_service_reports_ok(server: MCPServer) -> None:
    async with Client(server) as client:
        result = await client.call_tool("run_diagnostics", {"service": "payments"})
    assert result.structured_content == {
        "service": "payments",
        "state": "healthy",
        "checks": ["connectivity: ok", "error-rate: ok", "latency: ok"],
    }


async def test_run_diagnostics_on_unknown_service_errors_without_progress(server: MCPServer) -> None:
    seen: list[float] = []

    async def on_progress(progress: float, total: float | None, message: str | None) -> None:
        seen.append(progress)

    async with Client(server) as client:
        result = await client.call_tool("run_diagnostics", {"service": "nope"}, progress_callback=on_progress)
    assert result.is_error is True
    assert isinstance(result.content[0], TextContent)
    assert "Unknown service 'nope'" in result.content[0].text
    assert seen == []
