"""OpsDesk as a real subprocess over stdio, the way Claude Code launches it."""

import sys

import pytest
from mcp import Client, StdioServerParameters

pytestmark = pytest.mark.anyio

STDIO = StdioServerParameters(command=sys.executable, args=["-m", "opsdesk"])


async def test_modern_client_uses_2026_07_28_and_sees_server_identity() -> None:
    async with Client(STDIO) as client:
        assert client.protocol_version == "2026-07-28"
        assert client.server_info is not None
        assert client.server_info.name == "opsdesk"
        discovered = await client.session.discover()
        tools = await client.list_tools()
    assert "2026-07-28" in discovered.supported_versions
    assert len(tools.tools) == 5


async def test_legacy_client_negotiates_2025_11_25_handshake() -> None:
    async with Client(STDIO, mode="legacy") as client:
        assert client.protocol_version == "2025-11-25"
        result = await client.call_tool("get_service_status", {"service": "payments"})
    assert result.structured_content == {"name": "payments", "state": "healthy", "restarts": 0}
