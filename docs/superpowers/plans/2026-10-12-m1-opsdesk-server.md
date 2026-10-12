# M1 — OpsDesk Reference MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `opsdesk-server`, a reference MCP server for spec revision 2026-07-28 that also serves 2025-11-25 clients. It comes with a full test pyramid (in-memory, stdio, HTTP, conformance, Inspector) running in CI.

**Architecture:** A `uv` workspace with one package, `packages/opsdesk-server`.
- `opsdesk.data` holds a deterministic in-memory domain: services, incidents and runbooks.
- `opsdesk.server.build_server(store)` registers tools, a resource template and a prompt on the official SDK's `MCPServer`.
- `opsdesk.__main__` runs the server over stdio or Streamable HTTP.
- Tests use the SDK's unified `Client`: in-memory for most tests, then a stdio subprocess and an HTTP subprocess.

**Tech Stack:** Python 3.13, `uv` 0.11, `mcp==2.3.0` (official SDK v2), `uvicorn==0.54.0`, `pytest==9.1.1` with the `anyio` plugin, `ruff==0.17.0`, `pyright==1.1.414`. Node 22 for `@modelcontextprotocol/conformance@0.1.16` and `@modelcontextprotocol/inspector@2.10.1`.

**Spec:** `docs/superpowers/specs/2026-10-12-mcp-lab-design.md` (section "M1").

> Every code block in this plan was dry-run against `mcp==2.3.0` on 2026-10-12. Final state: 31 tests passing, ruff clean, pyright strict clean, 6/6 conformance scenarios passing, Inspector smoke OK.

## Global Constraints

- Python `>=3.13`; `.python-version` is `3.13`.
- Pin every dependency exactly (`uv add --bounds exact`). Runtime: `mcp==2.3.0`, `uvicorn==0.54.0`.
- Target MCP revision **2026-07-28**. The server must also serve **2025-11-25** clients; the SDK handles that negotiation.
- Do not use Roots, Sampling or Logging (deprecated in 2026-07-28), the HTTP+SSE transport, or `resources/subscribe`. Use `Client.listen()` / `subscriptions/listen` instead.
- **Never put `from __future__ import annotations` in `server.py`.** `MCPServer` evaluates tool signatures when they are registered, and string annotations cannot see resolvers defined inside `build_server`. Registration fails with `InvalidSignature`.
- `ToolAnnotations` takes **snake_case** fields in SDK v2: `read_only_hint`, `destructive_hint`, `idempotent_hint`. The camelCase names are the wire format only.
- pyright runs in **strict** mode for `packages/opsdesk-server/src`. ruff line length is 110.
- Git identity: `AndresRubio <2109109+AndresRubio@users.noreply.github.com>`. Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Error codes asserted in tests:

  | Code | Meaning |
  |---|---|
  | `-32602` | Resource not found (changed in 2026-07-28) |
  | `-32603` | Generic `ResourceError` |
  | `-32020` | HeaderMismatch |
  | `-32021` | MissingRequiredClientCapability |
  | `-32022` | UnsupportedProtocolVersion |

## File Structure

```
mcp-lab/
  pyproject.toml                         # workspace root: members, dev deps, ruff/pyright/pytest config
  .python-version                        # 3.13
  .gitignore
  uv.lock
  packages/opsdesk-server/
    pyproject.toml                       # package: deps, console script, uv_build module-name
    src/opsdesk/__init__.py              # __version__
    src/opsdesk/py.typed                 # PEP 561 marker (needed for pyright strict)
    src/opsdesk/data.py                  # domain models + OpsDeskStore + seed_store()
    src/opsdesk/server.py                # build_server(): tools, resource, prompt, cache hints
    src/opsdesk/__main__.py              # CLI: --transport stdio|http
    tests/conftest.py                    # store/server fixtures, elicitation callbacks
    tests/test_data.py
    tests/test_tools.py
    tests/test_restart_mrtr.py
    tests/test_diagnostics.py
    tests/test_resources.py
    tests/test_prompts.py
    tests/test_transport_stdio.py
    tests/test_transport_http.py
  scripts/conformance.sh                 # official conformance suite against HTTP server
  scripts/inspector-smoke.sh             # Inspector CLI smoke over stdio
  tests/conformance/.gitignore
  .github/workflows/ci.yml
  README.md
  docs/notes/m1.md                       # notes on 2026-07-28 to rehearse from
```

---

### Task 1: Workspace scaffold and OpsDesk domain data

**Files:**
- Create: `pyproject.toml`, `.python-version`, `.gitignore`, `packages/opsdesk-server/pyproject.toml`, `packages/opsdesk-server/src/opsdesk/__init__.py`, `packages/opsdesk-server/src/opsdesk/py.typed`, `packages/opsdesk-server/src/opsdesk/data.py`
- Test: `packages/opsdesk-server/tests/test_data.py`

**Interfaces:**
- Produces:
  - `opsdesk.__version__: str`
  - `ServiceState = Literal["healthy","degraded","down"]`
  - `IncidentStatus = Literal["open","resolved"]`
  - `Service(name, state, restarts=0)` (pydantic)
  - `Incident(id, service, title, status, description)` (pydantic)
  - `UnknownServiceError(LookupError)`
  - `OpsDeskStore` with:
    - `.services: dict[str, Service]`
    - `.incidents: list[Incident]`
    - `.runbooks: dict[str, str]`
    - `.service(name) -> Service` (raises `UnknownServiceError`)
    - `.restart(name) -> Service`
    - `.list_incidents(status: IncidentStatus | None = None) -> list[Incident]`
    - `.incident(id) -> Incident | None`
  - `seed_store() -> OpsDeskStore`

- [ ] **Step 1: Create the workspace root files**

`pyproject.toml`:

```toml
[project]
name = "mcp-lab"
version = "0.1.0"
description = "A living lab for MCP 2026-07-28, agent protocols and how to test them"
requires-python = ">=3.13"
dependencies = []

[tool.uv.workspace]
members = ["packages/*"]

[dependency-groups]
dev = []

[tool.ruff]
line-length = 110
target-version = "py313"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "RUF"]

[tool.ruff.lint.isort]
known-first-party = ["opsdesk"]

[tool.pyright]
pythonVersion = "3.13"
include = ["packages"]
strict = ["packages/opsdesk-server/src"]

[tool.pytest.ini_options]
testpaths = ["packages"]
addopts = "-ra --strict-markers --import-mode=importlib"
```

`.python-version`:

```
3.13
```

`.gitignore`:

```
.venv/
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
dist/
```

- [ ] **Step 2: Create the package skeleton**

`packages/opsdesk-server/pyproject.toml`:

```toml
[project]
name = "opsdesk-server"
version = "0.1.0"
description = "OpsDesk: a reference MCP server for the 2026-07-28 spec"
requires-python = ">=3.13"
dependencies = []

[project.scripts]
opsdesk-server = "opsdesk.__main__:main"

[build-system]
requires = ["uv_build>=0.11,<0.12"]
build-backend = "uv_build"

[tool.uv.build-backend]
module-name = "opsdesk"
```

`packages/opsdesk-server/src/opsdesk/__init__.py`:

```python
"""OpsDesk: a reference MCP server for the 2026-07-28 spec."""

__version__ = "0.1.0"
```

Create an empty `packages/opsdesk-server/src/opsdesk/py.typed`.

- [ ] **Step 3: Pin dependencies and install the workspace**

Run:

```bash
cd packages/opsdesk-server && uv add --bounds exact "mcp==2.3.0" "uvicorn==0.54.0" && cd ../..
uv add opsdesk-server
uv add --dev --bounds exact pytest==9.1.1 ruff==0.17.0 pyright==1.1.414 anyio==4.15.1 httpx2==2.13.1
uv run python -c "import opsdesk, mcp; print('ok')"
```

Expected:
- The final command prints `ok`.
- The root `pyproject.toml` now has `dependencies = ["opsdesk-server"]` and `[tool.uv.sources] opsdesk-server = { workspace = true }`.
- A `uv.lock` file exists.

- [ ] **Step 4: Write the failing test**

`packages/opsdesk-server/tests/test_data.py`:

```python
import pytest

from opsdesk.data import UnknownServiceError, seed_store


def test_seed_store_has_three_services_and_incidents() -> None:
    store = seed_store()
    assert set(store.services) == {"api", "payments", "search"}
    assert [i.id for i in store.list_incidents("open")] == ["INC-101", "INC-102"]
    assert [i.id for i in store.list_incidents()] == ["INC-101", "INC-102", "INC-099"]


def test_restart_marks_service_healthy_and_counts() -> None:
    store = seed_store()
    svc = store.restart("search")
    assert (svc.state, svc.restarts) == ("healthy", 1)


def test_unknown_service_raises() -> None:
    with pytest.raises(UnknownServiceError):
        seed_store().service("nope")


def test_seed_store_returns_independent_copies() -> None:
    a, b = seed_store(), seed_store()
    a.restart("api")
    assert b.service("api").state == "degraded"
```

- [ ] **Step 5: Run the test to verify it fails**

Run: `uv run pytest packages/opsdesk-server/tests/test_data.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'opsdesk.data'`.

- [ ] **Step 6: Implement `data.py`**

`packages/opsdesk-server/src/opsdesk/data.py`:

```python
"""Deterministic in-memory seed data for the OpsDesk demo domain."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

ServiceState = Literal["healthy", "degraded", "down"]
IncidentStatus = Literal["open", "resolved"]


class Service(BaseModel):
    name: str
    state: ServiceState
    restarts: int = 0


class Incident(BaseModel):
    id: str
    service: str
    title: str
    status: IncidentStatus
    description: str


class UnknownServiceError(LookupError):
    """Raised when a service name is not in the store."""


class OpsDeskStore:
    """Mutable in-memory state. Each test gets a fresh copy via `seed_store()`."""

    def __init__(self, services: list[Service], incidents: list[Incident], runbooks: dict[str, str]) -> None:
        self.services = {s.name: s for s in services}
        self.incidents = incidents
        self.runbooks = runbooks

    def service(self, name: str) -> Service:
        try:
            return self.services[name]
        except KeyError:
            raise UnknownServiceError(name) from None

    def restart(self, name: str) -> Service:
        svc = self.service(name)
        svc.state = "healthy"
        svc.restarts += 1
        return svc

    def list_incidents(self, status: IncidentStatus | None = None) -> list[Incident]:
        return [i for i in self.incidents if status is None or i.status == status]

    def incident(self, incident_id: str) -> Incident | None:
        return next((i for i in self.incidents if i.id == incident_id), None)


def seed_store() -> OpsDeskStore:
    return OpsDeskStore(
        services=[
            Service(name="api", state="degraded"),
            Service(name="payments", state="healthy"),
            Service(name="search", state="down"),
        ],
        incidents=[
            Incident(
                id="INC-101",
                service="api",
                title="Elevated 5xx on /checkout",
                status="open",
                description="Reported by customer jane.doe@example.com: checkout fails intermittently.",
            ),
            Incident(
                id="INC-102",
                service="search",
                title="Search cluster unreachable",
                status="open",
                description="All search nodes failing health checks since 09:12 UTC.",
            ),
            Incident(
                id="INC-099",
                service="payments",
                title="Delayed payout batch",
                status="resolved",
                description="Batch delayed 40 minutes; contact ops-lead@example.com for details.",
            ),
        ],
        runbooks={
            "api": "# api runbook\n\n1. Check error rate dashboard.\n2. Restart if 5xx > 5% for 10 min.",
            "payments": "# payments runbook\n\n1. Never restart during payout window (00:00-02:00 UTC).",
            "search": "# search runbook\n\n1. Check node health.\n2. Restart the cluster.",
        },
    )
```

The email addresses in incident descriptions are intentional; M2 uses them to test PII redaction.

- [ ] **Step 7: Run tests, lint and types**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run pyright`
Expected: `4 passed`, `All checks passed!`, files already formatted, `0 errors`.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml .python-version .gitignore uv.lock packages/opsdesk-server
git commit -m "feat(opsdesk): scaffold uv workspace and OpsDesk seed data

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Server skeleton with read-only tools and cache hints

**Files:**
- Create: `packages/opsdesk-server/src/opsdesk/server.py`
- Create: `packages/opsdesk-server/tests/conftest.py`
- Test: `packages/opsdesk-server/tests/test_tools.py`

**Interfaces:**
- Consumes: everything from Task 1.
- Produces:
  - `opsdesk.server.build_server(store: OpsDeskStore | None = None) -> MCPServer`
  - `opsdesk.server.CACHE_HINTS: dict[CacheableMethod, CacheHint]`
  - `IncidentList(incidents: list[Incident])`
  - Tools: `list_incidents(status=None) -> IncidentList` and `get_service_status(service) -> Service`
  - Inside `build_server`: `require_service(name) -> Service`, which raises `ToolError`; later tasks reuse it.
  - Test fixtures: `anyio_backend`, `store`, `server`.

- [ ] **Step 1: Write the fixtures**

`packages/opsdesk-server/tests/conftest.py`:

```python
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
```

- [ ] **Step 2: Write the failing tests**

`packages/opsdesk-server/tests/test_tools.py`:

```python
import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import TextContent

pytestmark = pytest.mark.anyio


async def test_tools_list_carries_public_cache_hint(server: MCPServer) -> None:
    async with Client(server) as client:
        result = await client.list_tools()
    assert (result.ttl_ms, result.cache_scope) == (60_000, "public")


async def test_every_tool_has_description_and_output_schema(server: MCPServer) -> None:
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
    for tool in tools:
        assert tool.description, tool.name
    assert all(t.output_schema is not None for t in tools if t.name != "update_runbook")


async def test_get_service_status_is_annotated_read_only(server: MCPServer) -> None:
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert tools["get_service_status"].annotations is not None
    assert tools["get_service_status"].annotations.read_only_hint is True


async def test_list_incidents_filters_by_status(server: MCPServer) -> None:
    async with Client(server) as client:
        result = await client.call_tool("list_incidents", {"status": "open"})
    assert result.is_error is False
    assert result.structured_content is not None
    assert [i["id"] for i in result.structured_content["incidents"]] == ["INC-101", "INC-102"]


async def test_get_service_status_returns_structured_content(server: MCPServer) -> None:
    async with Client(server) as client:
        result = await client.call_tool("get_service_status", {"service": "search"})
    assert result.structured_content == {"name": "search", "state": "down", "restarts": 0}


async def test_unknown_service_is_a_tool_error_not_a_protocol_error(server: MCPServer) -> None:
    async with Client(server) as client:
        result = await client.call_tool("get_service_status", {"service": "nope"})
    assert result.is_error is True
    [content] = result.content
    assert isinstance(content, TextContent)
    assert "Unknown service 'nope'" in content.text
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest packages/opsdesk-server/tests/test_tools.py -v`
Expected: ERROR at collection with `ModuleNotFoundError: No module named 'opsdesk.server'`.

- [ ] **Step 4: Implement the server skeleton**

`packages/opsdesk-server/src/opsdesk/server.py`:

```python
"""OpsDesk MCP server, built directly on the official MCP Python SDK v2 (`MCPServer`).

No `from __future__ import annotations` here: MCPServer evaluates tool signatures at
registration, and stringified annotations cannot see resolvers local to `build_server`.
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
```

- [ ] **Step 5: Run tests, lint and types**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run pyright`
Expected: `10 passed`, ruff clean, `0 errors`.

- [ ] **Step 6: Commit**

```bash
git add packages/opsdesk-server
git commit -m "feat(opsdesk): read-only incident and status tools with 2026-07-28 cache hints

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: `restart_service` with Multi Round-Trip confirmation (MRTR)

**Files:**
- Modify: `packages/opsdesk-server/src/opsdesk/server.py`
- Modify: `packages/opsdesk-server/tests/conftest.py`
- Modify: `packages/opsdesk-server/tests/test_tools.py`
- Test: `packages/opsdesk-server/tests/test_restart_mrtr.py`

**Interfaces:**
- Consumes: `build_server` and `require_service` (Task 2), and `OpsDeskStore.restart` (Task 1).
- Produces:
  - Models: `RestartConfirmation(confirm: bool)` and `RestartOutcome(service: str, restarted: bool, state: ServiceState)`.
  - Tool `restart_service(service) -> RestartOutcome`.
  - Test fixtures `accept_restart` and `decline`, both of type `mcp.client.session.ElicitationFnT`.

How MRTR works in SDK v2: a parameter annotated `Annotated[ElicitationResult[T], Resolve(fn)]` is filled by running the resolver `fn` before the tool body.
- **2026-07-28 clients:** the SDK returns an `InputRequiredResult` (`resultType: "input_required"`). The client's `Client.call_tool` answers it with its `elicitation_callback` and retries the original request, carrying `inputResponses` and the sealed `requestState`.
- **2025-11-25 clients:** the SDK sends a standalone `elicitation/create` request in the middle of the call instead.

The `confirmation` parameter never appears in the tool's input schema.

- [ ] **Step 1: Add elicitation callback fixtures**

Replace `packages/opsdesk-server/tests/conftest.py` with:

```python
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
```

- [ ] **Step 2: Write the failing tests**

`packages/opsdesk-server/tests/test_restart_mrtr.py`:

```python
"""restart_service: confirmation via Multi Round-Trip Requests (SEP-2322, MCP 2026-07-28)."""

import pytest
from mcp import Client, MCPError
from mcp.client.session import ClientRequestContext, ElicitationFnT
from mcp.server.mcpserver import MCPServer
from mcp.types import ElicitRequest, ElicitRequestParams, ElicitResult, InputRequiredResult

from opsdesk.data import OpsDeskStore

pytestmark = pytest.mark.anyio


async def test_confirmation_is_not_part_of_the_input_schema(server: MCPServer) -> None:
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools["restart_service"].input_schema["properties"]) == {"service"}


async def test_accepted_confirmation_restarts_the_service(
    server: MCPServer, store: OpsDeskStore, accept_restart: ElicitationFnT
) -> None:
    async with Client(server, elicitation_callback=accept_restart) as client:
        result = await client.call_tool("restart_service", {"service": "search"})
    assert result.structured_content == {"service": "search", "restarted": True, "state": "healthy"}
    assert store.service("search").restarts == 1


async def test_declined_confirmation_leaves_the_service_alone(
    server: MCPServer, store: OpsDeskStore, decline: ElicitationFnT
) -> None:
    async with Client(server, elicitation_callback=decline) as client:
        result = await client.call_tool("restart_service", {"service": "search"})
    assert result.structured_content == {"service": "search", "restarted": False, "state": "down"}
    assert store.service("search").restarts == 0


async def test_first_round_returns_input_required_then_retry_completes(
    server: MCPServer, decline: ElicitationFnT
) -> None:
    # Drive the MRTR loop by hand to see the wire-level shape.
    async with Client(server, elicitation_callback=decline) as client:
        first = await client.session.call_tool(
            "restart_service", {"service": "api"}, allow_input_required=True
        )
        assert isinstance(first, InputRequiredResult)
        assert first.result_type == "input_required"
        assert first.request_state  # sealed by the SDK; opaque to the client
        assert first.input_requests is not None
        [(key, request)] = first.input_requests.items()
        assert isinstance(request, ElicitRequest)
        assert "Restart 'api'" in request.params.message

        second = await client.session.call_tool(
            "restart_service",
            {"service": "api"},
            allow_input_required=True,
            input_responses={key: ElicitResult(action="accept", content={"confirm": True})},
            request_state=first.request_state,
        )
    assert not isinstance(second, InputRequiredResult)
    assert second.structured_content == {"service": "api", "restarted": True, "state": "healthy"}


async def test_unknown_service_fails_before_asking_the_user(server: MCPServer) -> None:
    asked: list[str] = []

    async def record(context: ClientRequestContext, params: ElicitRequestParams) -> ElicitResult:
        asked.append(params.message)
        return ElicitResult(action="accept", content={"confirm": True})

    async with Client(server, elicitation_callback=record) as client:
        result = await client.call_tool("restart_service", {"service": "nope"})
    assert result.is_error is True
    assert asked == []


async def test_client_without_elicitation_capability_is_rejected(server: MCPServer) -> None:
    async with Client(server) as client:
        with pytest.raises(MCPError) as exc:
            await client.call_tool("restart_service", {"service": "api"})
    assert exc.value.error.code == -32021  # MissingRequiredClientCapability


async def test_legacy_2025_11_25_client_gets_a_standalone_elicitation(
    server: MCPServer, accept_restart: ElicitationFnT
) -> None:
    async with Client(server, mode="legacy", elicitation_callback=accept_restart) as client:
        assert client.protocol_version == "2025-11-25"
        result = await client.call_tool("restart_service", {"service": "api"})
    assert result.structured_content == {"service": "api", "restarted": True, "state": "healthy"}
```

Also append to `packages/opsdesk-server/tests/test_tools.py`:

```python
async def test_restart_service_is_annotated_destructive(server: MCPServer) -> None:
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert tools["restart_service"].annotations is not None
    assert tools["restart_service"].annotations.destructive_hint is True
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest packages/opsdesk-server/tests/test_restart_mrtr.py -v`
Expected: FAIL. `restart_service` is unknown, so the tests hit `KeyError: 'restart_service'` or get an `is_error` result.

- [ ] **Step 4: Implement `restart_service`**

In `server.py`, replace the import block (everything from `from mcp.server.caching ...` down to the closing `)` of the `opsdesk.data` import) with:

```python
from typing import Annotated

from mcp.server.caching import CacheableMethod, CacheHint
from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
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
```

Add these models directly after `class IncidentList`:

```python
class RestartConfirmation(BaseModel):
    confirm: bool


class RestartOutcome(BaseModel):
    service: str
    restarted: bool
    state: ServiceState
```

Insert this inside `build_server`, directly before `return server`:

```python
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
```

- [ ] **Step 5: Run tests, lint and types**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run pyright`
Expected: `18 passed`, ruff clean, `0 errors`.

If you see `InvalidSignature: Unable to evaluate type annotations for callable 'restart_service'`, someone added `from __future__ import annotations` to `server.py`. Remove it.

- [ ] **Step 6: Commit**

```bash
git add packages/opsdesk-server
git commit -m "feat(opsdesk): restart_service with MRTR confirmation (SEP-2322)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: `run_diagnostics` with progress notifications

**Files:**
- Modify: `packages/opsdesk-server/src/opsdesk/server.py`
- Test: `packages/opsdesk-server/tests/test_diagnostics.py`

**Interfaces:**
- Consumes: `require_service` (Task 2).
- Produces:
  - `DiagnosticsReport(service: str, state: ServiceState, checks: list[str])`
  - `DIAGNOSTIC_CHECKS = ("connectivity", "error-rate", "latency")`
  - Tool `run_diagnostics(service) -> DiagnosticsReport`, which sends one `notifications/progress` per check.

Spec note: the design suggested the **tasks extension** (`io.modelcontextprotocol/tasks`) for this tool. `mcp` 2.3.0 does not implement it (the release notes list SEP-2663 as pending). This task uses request-scoped progress notifications instead. Task 9's notes record that tasks are deferred.

- [ ] **Step 1: Write the failing test**

`packages/opsdesk-server/tests/test_diagnostics.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest packages/opsdesk-server/tests/test_diagnostics.py -v`
Expected: FAIL. `seen == []` and the result is an error for the unknown tool.

- [ ] **Step 3: Implement `run_diagnostics`**

In `server.py`:
- Change `from mcp.server.mcpserver import MCPServer` to `from mcp.server.mcpserver import Context, MCPServer`.
- Add after `class RestartOutcome`:

```python
class DiagnosticsReport(BaseModel):
    service: str
    state: ServiceState
    checks: list[str]


DIAGNOSTIC_CHECKS = ("connectivity", "error-rate", "latency")
```

Insert this inside `build_server`, directly before `return server`:

```python
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
```

- [ ] **Step 4: Run tests, lint and types**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run pyright`
Expected: `19 passed`, ruff clean, `0 errors`.

- [ ] **Step 5: Commit**

```bash
git add packages/opsdesk-server
git commit -m "feat(opsdesk): run_diagnostics with progress notifications

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Runbook resource, `update_runbook`, and `subscriptions/listen`

**Files:**
- Modify: `packages/opsdesk-server/src/opsdesk/server.py`
- Modify: `packages/opsdesk-server/tests/test_tools.py`
- Test: `packages/opsdesk-server/tests/test_resources.py`

**Interfaces:**
- Consumes: `require_service` and `CACHE_HINTS` (Task 2), and `Context` imported in Task 4.
- Produces:
  - Resource template `runbook://{service}` (`text/markdown`). A missing runbook raises `ResourceNotFoundError`, which maps to `-32602`.
  - Tool `update_runbook(service, markdown) -> str`, which calls `ctx.notify_resource_updated(uri)`.
  - The final tool order: `list_incidents`, `get_service_status`, `restart_service`, `run_diagnostics`, `update_runbook`.

- [ ] **Step 1: Write the failing tests**

`packages/opsdesk-server/tests/test_resources.py`:

```python
import anyio
import pytest
from mcp import Client, MCPError
from mcp.server.mcpserver import MCPServer
from mcp.shared.subscriptions import ResourceUpdated
from mcp.types import TextResourceContents

pytestmark = pytest.mark.anyio


async def test_runbook_template_is_listed_with_cache_hints(server: MCPServer) -> None:
    async with Client(server) as client:
        result = await client.list_resource_templates()
    assert [t.uri_template for t in result.resource_templates] == ["runbook://{service}"]
    assert (result.ttl_ms, result.cache_scope) == (60_000, "public")


async def test_read_runbook_returns_markdown_with_private_cache_hint(server: MCPServer) -> None:
    async with Client(server) as client:
        result = await client.read_resource("runbook://api")
    [content] = result.contents
    assert isinstance(content, TextResourceContents)
    assert content.mime_type == "text/markdown"
    assert content.text.startswith("# api runbook")
    assert (result.ttl_ms, result.cache_scope) == (5_000, "private")


async def test_missing_runbook_is_invalid_params_32602(server: MCPServer) -> None:
    # 2026-07-28 moved "resource not found" from -32002 to -32602 (Invalid Params).
    async with Client(server) as client:
        with pytest.raises(MCPError) as exc:
            await client.read_resource("runbook://nope")
    assert exc.value.error.code == -32602


async def test_update_runbook_notifies_listeners_and_evicts_client_cache(server: MCPServer) -> None:
    async with Client(server) as client:
        before = await client.read_resource("runbook://api")  # now cached for 5s
        async with client.listen(resource_subscriptions=["runbook://api"]) as sub:
            assert sub.honored.resource_subscriptions == ["runbook://api"]
            await client.call_tool("update_runbook", {"service": "api", "markdown": "# api v2"})
            with anyio.fail_after(3):
                event = await anext(aiter(sub))
        after = await client.read_resource("runbook://api")
    assert event == ResourceUpdated(uri="runbook://api")
    assert isinstance(before.contents[0], TextResourceContents)
    assert isinstance(after.contents[0], TextResourceContents)
    assert before.contents[0].text != after.contents[0].text == "# api v2"
```

The last test shows two 2026-07-28 features working together: the client cached the first read because of `ttlMs`, and the `subscriptions/listen` event cleared that cache entry. Without the event, the second read would return the stale cached copy.

Append to `packages/opsdesk-server/tests/test_tools.py`:

```python
async def test_tools_are_listed_in_deterministic_order(server: MCPServer) -> None:
    # 2026-07-28: servers SHOULD return tools/list in a stable order (client and LLM prompt caching).
    async with Client(server) as client:
        first = [t.name for t in (await client.list_tools()).tools]
    async with Client(server) as client:
        second = [t.name for t in (await client.list_tools()).tools]
    assert first == second == [
        "list_incidents",
        "get_service_status",
        "restart_service",
        "run_diagnostics",
        "update_runbook",
    ]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest packages/opsdesk-server/tests/test_resources.py packages/opsdesk-server/tests/test_tools.py -v`
Expected: FAIL. No resource templates are listed, and the tool order lacks `update_runbook`.

- [ ] **Step 3: Implement the resource and tool**

In `server.py`, change `from mcp.server.mcpserver.exceptions import ToolError` to:

```python
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
```

Insert this inside `build_server`, directly before `return server`:

```python
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
```

`ResourceNotFoundError` maps to `-32602`, while the generic `ResourceError` maps to `-32603`. Use the specific one.

- [ ] **Step 4: Run tests, lint and types**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run pyright`
Expected: `24 passed`, ruff clean, `0 errors`.

- [ ] **Step 5: Commit**

```bash
git add packages/opsdesk-server
git commit -m "feat(opsdesk): runbook resource, update_runbook and subscriptions/listen

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: `postmortem` prompt

**Files:**
- Modify: `packages/opsdesk-server/src/opsdesk/server.py`
- Test: `packages/opsdesk-server/tests/test_prompts.py`

**Interfaces:**
- Consumes: `OpsDeskStore.incident` (Task 1).
- Produces: prompt `postmortem(incident_id)`, which returns one user message. An unknown incident is a protocol error.

- [ ] **Step 1: Write the failing tests**

`packages/opsdesk-server/tests/test_prompts.py`:

```python
import pytest
from mcp import Client, MCPError
from mcp.server.mcpserver import MCPServer
from mcp.types import TextContent

pytestmark = pytest.mark.anyio


async def test_postmortem_prompt_is_listed_with_its_argument(server: MCPServer) -> None:
    async with Client(server) as client:
        result = await client.list_prompts()
    [prompt] = result.prompts
    assert prompt.name == "postmortem"
    assert prompt.description
    assert [a.name for a in prompt.arguments or []] == ["incident_id"]


async def test_postmortem_prompt_embeds_incident_context(server: MCPServer) -> None:
    async with Client(server) as client:
        result = await client.get_prompt("postmortem", {"incident_id": "INC-101"})
    [message] = result.messages
    assert message.role == "user"
    assert isinstance(message.content, TextContent)
    assert "INC-101" in message.content.text
    assert "Root cause" in message.content.text


async def test_unknown_incident_is_an_error(server: MCPServer) -> None:
    async with Client(server) as client:
        with pytest.raises(MCPError):
            await client.get_prompt("postmortem", {"incident_id": "INC-404"})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest packages/opsdesk-server/tests/test_prompts.py -v`
Expected: FAIL. `ValueError: not enough values to unpack` because the prompt list is empty, and `MCPError` for the unknown prompt.

- [ ] **Step 3: Implement the prompt**

Insert this inside `build_server`, directly before `return server`:

```python
    @server.prompt(title="Postmortem draft")
    def postmortem(incident_id: str) -> str:
        """Draft a blameless postmortem for an incident."""
        incident = store.incident(incident_id)
        if incident is None:
            raise ValueError(f"Unknown incident '{incident_id}'.")
        return (
            f"Write a blameless postmortem for {incident.id} ({incident.title}) on service "
            f"'{incident.service}'. Status: {incident.status}.\n\nContext: {incident.description}\n\n"
            "Sections: Summary, Impact, Timeline, Root cause, Action items."
        )
```

- [ ] **Step 4: Run tests, lint and types**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run pyright`
Expected: `27 passed`, ruff clean, `0 errors`.

- [ ] **Step 5: Commit**

```bash
git add packages/opsdesk-server
git commit -m "feat(opsdesk): postmortem prompt

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: CLI entry point with stdio and Streamable HTTP transport tests

**Files:**
- Create: `packages/opsdesk-server/src/opsdesk/__main__.py`
- Test: `packages/opsdesk-server/tests/test_transport_stdio.py`
- Test: `packages/opsdesk-server/tests/test_transport_http.py`

**Interfaces:**
- Consumes: `build_server()` (Tasks 2–6).
- Produces: `opsdesk.__main__.main(argv: list[str] | None = None) -> None`.
  - CLI: `opsdesk-server [--transport stdio|http] [--host 127.0.0.1] [--port 8000]`, also runnable as `python -m opsdesk`.
  - The HTTP endpoint is `http://<host>:<port>/mcp`.

- [ ] **Step 1: Write the failing tests**

`packages/opsdesk-server/tests/test_transport_stdio.py`:

```python
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
```

`packages/opsdesk-server/tests/test_transport_http.py`:

```python
"""OpsDesk over Streamable HTTP, including raw requests that check 2026-07-28 wire rules."""

import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
from mcp import Client

PROTOCOL = "2026-07-28"
ACCEPT = "application/json, text/event-stream"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def http_url() -> Iterator[str]:
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-m", "opsdesk", "--transport", "http", "--port", str(port)])
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("opsdesk HTTP server did not start")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def _tools_list_body(version: str = PROTOCOL) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": version,
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }


@pytest.mark.anyio
async def test_modern_and_legacy_clients_both_work_over_http(http_url: str) -> None:
    async with Client(http_url) as modern:
        assert modern.protocol_version == PROTOCOL
        assert len((await modern.list_tools()).tools) == 5
    async with Client(http_url, mode="legacy") as legacy:
        assert legacy.protocol_version == "2025-11-25"
        assert len((await legacy.list_tools()).tools) == 5


def test_stateless_request_with_required_headers_succeeds(http_url: str) -> None:
    headers = {"Accept": ACCEPT, "MCP-Protocol-Version": PROTOCOL, "Mcp-Method": "tools/list"}
    response = httpx2.post(http_url, json=_tools_list_body(), headers=headers)
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["resultType"] == "complete"
    assert result["cacheScope"] == "public"
    assert "Mcp-Session-Id" not in response.headers  # no protocol-level sessions in 2026-07-28


def test_missing_mcp_method_header_is_rejected_with_32020(http_url: str) -> None:
    headers = {"Accept": ACCEPT, "MCP-Protocol-Version": PROTOCOL}
    response = httpx2.post(http_url, json=_tools_list_body(), headers=headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020  # HeaderMismatch


def test_unsupported_version_is_rejected_with_32022_and_lists_supported(http_url: str) -> None:
    headers = {"Accept": ACCEPT, "MCP-Protocol-Version": "1999-01-01", "Mcp-Method": "tools/list"}
    response = httpx2.post(http_url, json=_tools_list_body("1999-01-01"), headers=headers)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == -32022  # UnsupportedProtocolVersion
    assert PROTOCOL in error["data"]["supported"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest packages/opsdesk-server/tests/test_transport_stdio.py packages/opsdesk-server/tests/test_transport_http.py -v`
Expected:
- The stdio tests fail with a connection or closed-stream error, because `No module named opsdesk.__main__`.
- The HTTP fixture raises `RuntimeError: opsdesk HTTP server did not start`.

- [ ] **Step 3: Implement the entry point**

`packages/opsdesk-server/src/opsdesk/__main__.py`:

```python
"""Run OpsDesk over stdio (default) or Streamable HTTP: `opsdesk-server [--transport http]`."""

import argparse

import uvicorn

from opsdesk.server import build_server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="opsdesk-server", description=__doc__)
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    server = build_server()
    if args.transport == "stdio":
        server.run()
    else:
        app = server.streamable_http_app(host=args.host)
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests, lint and types**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run pyright`
Expected: `33 passed`, ruff clean, `0 errors`.

- [ ] **Step 5: Commit**

```bash
git add packages/opsdesk-server
git commit -m "feat(opsdesk): stdio/HTTP entry point with transport-level tests

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Conformance suite, Inspector smoke test, and CI

**Files:**
- Create: `scripts/conformance.sh`, `scripts/inspector-smoke.sh`, `tests/conformance/.gitignore`, `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: the `opsdesk-server` console script (Task 7).
- Produces: two CI jobs, `python` (lint, types, tests) and `protocol` (conformance and Inspector).

Context: `@modelcontextprotocol/conformance@0.1.16` has **no 2026-07-28 scenarios**. Every server scenario is tagged 2025-06-18 or 2025-11-25. So this suite checks OpsDesk's backward-compatible path, while the pytest suite covers 2026-07-28. Many scenarios call fixture tools that only exist in the suite's reference server (`tools-call-image`, `tools-call-sampling`, ...), so we run the six scenarios that apply to any server. Each scenario exits 1 on failure.

- [ ] **Step 1: Write the conformance script**

`scripts/conformance.sh`:

```bash
#!/usr/bin/env bash
# Run the official MCP conformance suite against OpsDesk over Streamable HTTP.
# conformance 0.1.16 has no 2026-07-28 scenarios yet, so this exercises OpsDesk's
# backward-compatible 2025-11-25 path. Scenarios that need the suite's own fixture
# tools (images, sampling, ...) are not applicable to a domain server and are skipped.
set -euo pipefail

CONFORMANCE_VERSION="0.1.16"
PORT="${PORT:-18765}"
URL="http://localhost:${PORT}/mcp"
SCENARIOS=(
  server-initialize
  ping
  tools-list
  resources-list
  prompts-list
  dns-rebinding-protection
)

uv run opsdesk-server --transport http --port "$PORT" &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 50); do
  nc -z 127.0.0.1 "$PORT" 2>/dev/null && break
  sleep 0.2
done

mkdir -p tests/conformance/results
for scenario in "${SCENARIOS[@]}"; do
  echo "== ${scenario}"
  npx -y "@modelcontextprotocol/conformance@${CONFORMANCE_VERSION}" server \
    --url "$URL" --scenario "$scenario" -o tests/conformance/results
done
```

`tests/conformance/.gitignore`:

```
results/
```

- [ ] **Step 2: Write the Inspector smoke script**

`scripts/inspector-smoke.sh`:

```bash
#!/usr/bin/env bash
# Smoke-test OpsDesk over stdio with the MCP Inspector CLI: list tools, call one.
set -euo pipefail

INSPECTOR_VERSION="2.10.1"
inspect() {
  npx -y "@modelcontextprotocol/inspector@${INSPECTOR_VERSION}" --cli uv run opsdesk-server "$@"
}

tools=$(inspect --method tools/list)
for name in list_incidents get_service_status restart_service run_diagnostics update_runbook; do
  grep -q "\"name\": \"${name}\"" <<<"$tools" || { echo "missing tool: ${name}"; exit 1; }
done

status=$(inspect --method tools/call --tool-name get_service_status --tool-arg service=search)
grep -q '"state": "down"' <<<"$status" || { echo "unexpected status: ${status}"; exit 1; }

echo "inspector smoke: OK"
```

Run: `chmod +x scripts/*.sh`

- [ ] **Step 3: Run both scripts locally**

Run: `./scripts/conformance.sh 2>&1 | grep -E "^==|Passed" && ./scripts/inspector-smoke.sh`
Expected: each scenario prints `Passed: N/N, 0 failed`, and the script ends with `inspector smoke: OK`.

If `tools-list` or `prompts-list` fails with `missing description`, a tool or prompt is missing its docstring.

- [ ] **Step 4: Write the CI workflow**

`.github/workflows/ci.yml`:

```yaml
name: ci

on:
  push:
    branches: [main]
  pull_request:

jobs:
  python:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v10
      - run: uv sync --locked
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run pyright
      - run: uv run pytest

  protocol:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v10
      - uses: actions/setup-node@v7
        with:
          node-version: "22"
      - run: uv sync --locked
      - run: ./scripts/conformance.sh
      - run: ./scripts/inspector-smoke.sh
```

- [ ] **Step 5: Commit**

```bash
git add scripts tests/conformance/.gitignore .github/workflows/ci.yml
git commit -m "ci: conformance suite, Inspector smoke test and GitHub Actions

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: README, M1 notes, Claude Code hookup, and the `m1` tag

**Files:**
- Create: `README.md`, `docs/notes/m1.md`

**Interfaces:**
- Consumes: everything above.
- Produces: the documentation and the `m1` git tag.

- [ ] **Step 1: Verify the server works in Claude Code**

Run (from the repo root):

```bash
claude mcp add opsdesk -- uv run --directory "$(pwd)" opsdesk-server
claude mcp list
```

Expected: `opsdesk` is listed as connected. In a Claude Code session, ask "which services are down?". It should call `get_service_status` or `list_incidents`.

Then ask "restart search". Claude Code should ask you to confirm before the restart runs; this is the MRTR elicitation. If your Claude Code version does not support form elicitation yet, the call fails with `-32021`. Record which happened in the README. Afterwards, remove the server with `claude mcp remove opsdesk` if you don't want it registered globally.

- [ ] **Step 2: Write `README.md`**

```markdown
# mcp-lab

A living lab for the newest agent-protocol stack, built milestone by milestone with a real test pyramid.
Targets **MCP spec 2026-07-28** (stateless MCP) while staying compatible with 2025-11-25 clients.

| Milestone | Status | What it shows |
|---|---|---|
| M1 OpsDesk server | ✅ `m1` | Official Python SDK v2, MRTR elicitation, `server/discover`, cache hints, `subscriptions/listen` |
| M2 MCP gateway | planned | Registry (tools off by default), proxy, OAuth (CIMD), PII redaction, Omni discovery, OTel |
| M3 Evals | planned | Tool-selection accuracy: direct vs discovery vs Code Mode |
| M4 A2A agents | planned | Triage → Remediation delegation over A2A, tools via MCP |
| M5 UI | planned | AG-UI approval flow, MCP App dashboard |

## Quick start

    uv sync
    uv run opsdesk-server                      # stdio
    uv run opsdesk-server --transport http     # http://127.0.0.1:8000/mcp

Use it from Claude Code:

    claude mcp add opsdesk -- uv run --directory /path/to/mcp-lab opsdesk-server

## OpsDesk

| Kind | Name | 2026-07-28 feature on display |
|---|---|---|
| tool | `list_incidents`, `get_service_status` | `outputSchema` + `structuredContent`, `readOnlyHint` |
| tool | `restart_service` | Multi Round-Trip Request: `input_required` → retry with `inputResponses` |
| tool | `run_diagnostics` | request-scoped progress notifications |
| tool | `update_runbook` | `subscriptions/listen` resource-updated events |
| resource | `runbook://{service}` | `ttlMs`/`cacheScope` caching, `-32602` on not-found |
| prompt | `postmortem` | prompt arguments |

## Test pyramid

| Layer | Command | What it proves |
|---|---|---|
| Contract (in-memory) | `uv run pytest` | every tool/resource/prompt, MRTR rounds, error codes |
| Transport | `uv run pytest -k transport` | stdio + Streamable HTTP, version negotiation, required headers |
| Conformance | `./scripts/conformance.sh` | official suite (2025-11-25 scenarios; none exist yet for 2026-07-28) |
| Smoke | `./scripts/inspector-smoke.sh` | MCP Inspector CLI over stdio |

Notes for each milestone are in [`docs/notes/`](docs/notes/).
```

(When you write the file, use fenced code blocks for the commands; they are indented above only to keep this plan readable.)

- [ ] **Step 3: Write `docs/notes/m1.md`**

```markdown
# M1 notes — what changed in MCP 2026-07-28 and why

## The one-line version
MCP became stateless: no `initialize` handshake, no `Mcp-Session-Id`. Every request carries its own
protocol version and client capabilities in `_meta`, so any server replica can answer any request.

## Changes OpsDesk demonstrates (and the test that proves each)
- **`server/discover`** replaces the handshake for up-front negotiation → `test_transport_stdio.py`.
- **Version errors are per request**: `UnsupportedProtocolVersionError` (-32022) lists supported versions
  → `test_unsupported_version_is_rejected_with_32022_and_lists_supported`.
- **Required HTTP headers** `Mcp-Method`/`Mcp-Name` let gateways route without parsing bodies; a
  mismatch is -32020 → `test_missing_mcp_method_header_is_rejected_with_32020`.
- **Multi Round-Trip Requests (SEP-2322)** replace server→client requests. The server returns
  `resultType: "input_required"` with `inputRequests` + an opaque, SDK-sealed `requestState`; the client
  retries the *original* request with `inputResponses`. Why: server→client requests need a held-open
  stream and sticky sessions, which statelessness removes → `test_restart_mrtr.py`.
- **Capability enforcement**: a client that does not declare elicitation gets -32021 instead of a
  hang → `test_client_without_elicitation_capability_is_rejected`.
- **Caching (SEP-2549)**: list/read results carry `ttlMs` + `cacheScope` (`public` = shareable by
  intermediaries) → `test_tools.py`, `test_resources.py`.
- **`subscriptions/listen`** replaces `resources/subscribe` and the GET stream; one opt-in stream, and
  events evict the client cache → `test_update_runbook_notifies_listeners_and_evicts_client_cache`.
- **Resource not found is -32602** (was -32002) → `test_missing_runbook_is_invalid_params_32602`.
- **Deterministic `tools/list` order** helps client and LLM prompt-cache hit rates.

## Deprecated, deliberately not used
Roots, Sampling, Logging (SEP-2577); HTTP+SSE transport; Dynamic Client Registration (→ Client ID
Metadata Documents, M2).

## Deferred
- **Tasks extension** (`io.modelcontextprotocol/tasks`, SEP-2663): not implemented in `mcp` 2.3.0.
  `run_diagnostics` uses progress notifications; revisit when the SDK ships it.
- **Conformance for 2026-07-28**: conformance 0.1.16 only has ≤2025-11-25 scenarios, so CI runs it
  against the legacy path; the pytest suite covers 2026-07-28.

## SDK v2 gotchas found while building
- `FastMCP` is now `MCPServer`; `ToolAnnotations` takes snake_case (`read_only_hint`).
- No `from __future__ import annotations` in modules that define tools with local resolvers.
- `ResourceNotFoundError` → -32602, generic `ResourceError` → -32603.
- A `ProgressFnT` callback's first parameter must be named `progress` (Protocol, checked by pyright).

## Interview questions to rehearse
1. Why did MCP drop the initialize handshake? What breaks for load-balanced servers without it?
2. Walk through an MRTR round trip. Why must `requestState` be sealed?
3. `public` vs `private` cache scope — what goes wrong if you get it backwards?
4. A server passes conformance. Why might an agent still use it badly? (→ M3)
```

- [ ] **Step 4: Run the full verification**

Run:

```bash
uv sync --locked && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q && ./scripts/conformance.sh 2>&1 | grep -E "Passed|failed" && ./scripts/inspector-smoke.sh
```

Expected: ruff clean, `0 errors`, `33 passed`, every conformance line shows `0 failed`, and `inspector smoke: OK`.

- [ ] **Step 5: Commit and tag**

```bash
git add README.md docs/notes/m1.md
git commit -m "docs: M1 README and 2026-07-28 notes

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git tag -a m1 -m "M1: OpsDesk reference MCP server (spec 2026-07-28)"
```
