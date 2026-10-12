import pytest
from mcp.server.mcpserver import MCPServer

from opsdesk.data import OpsDeskStore, seed_store
from opsdesk.server import build_server


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def store() -> OpsDeskStore:
    return seed_store()


@pytest.fixture
def server(store: OpsDeskStore) -> MCPServer:
    return build_server(store)
