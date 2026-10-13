import pytest
from mcp.client.session import ClientRequestContext, ElicitationFnT
from mcp.server.mcpserver import MCPServer
from mcp.types import ElicitRequestParams, ElicitResult

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


async def _accept(context: ClientRequestContext, params: ElicitRequestParams) -> ElicitResult:
    return ElicitResult(action="accept", content={"confirm": True})


async def _decline(context: ClientRequestContext, params: ElicitRequestParams) -> ElicitResult:
    return ElicitResult(action="decline")


@pytest.fixture
def accept_restart() -> ElicitationFnT:
    """Elicitation callback that confirms any restart."""
    return _accept


@pytest.fixture
def decline() -> ElicitationFnT:
    """Elicitation callback that declines every question."""
    return _decline
