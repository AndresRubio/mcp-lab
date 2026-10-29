# M2a — MCP Gateway Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `mcp-gateway`, the core of the M2 gateway. It serves every registered MCP server behind one endpoint per server (`/<name>/mcp`). A reviewed YAML registry decides which tools exist (unknown tools are hidden, listed tools are off unless enabled) and reloads live. Middleware redacts e-mail addresses and rate-limits each caller. Multi Round-Trip Requests (MRTR) and progress pass through the proxy. Auth (OAuth 2.1, CIMD, `iss` validation) is M2b; Omni discovery, response projection and OTel are M2c.

**Architecture:** A second `uv` workspace member, `packages/mcp-gateway` (import name `gateway`), next to `opsdesk-server`.
- `gateway.registry` is the control plane: pydantic models parsed from YAML, and `RegistryHolder`, which reloads the file when its mtime changes and keeps the last good registry on errors.
- `gateway.redaction.redact()` is a pure function that replaces e-mail addresses in JSON-shaped values.
- `gateway.pager` is a demo OpenAPI backend: a plain Starlette app, a hand-written OpenAPI 3.1 spec, and `pager_mcp()` built with `OpenAPIProvider`.
- `gateway.visibility.RegistryVisibility` is a FastMCP `Transform` that reads the registry on every lookup, so a reload needs no rebuild.
- `gateway.policies.GatewayPolicyMiddleware` refreshes the registry per request, redacts results, and enforces a per-caller fixed-window rate limit that counts only the first MRTR round.
- `gateway.app` builds one `create_proxy(...)` per backend (`build_gateway_servers`), turns registry sources into proxy targets (`resolve_targets`), and mounts everything in one Starlette app with `/healthz` (`create_app`).
- `gateway.__main__` is the `mcp-gateway` CLI: `serve` and `audit`.
- Tests run in memory (fastmcp `Client(server)`), over in-process Streamable HTTP (`run_asgi_lifespan` + `StreamingASGITransport`), and once end to end against a `serve` subprocess.

**Tech Stack:** Python 3.13, `uv` 0.11, `fastmcp==4.1.0`, `mcp==2.3.0`, `pydantic==2.14.1`, `pyyaml==6.0.3` (+ `types-pyyaml==6.0.12.20260906` for pyright), `starlette==1.8.0`, `uvicorn==0.54.0`, `httpx2==2.13.1`, `pytest==9.1.1` with the `anyio` plugin, `ruff==0.17.0`, `pyright==1.1.414`.

**Spec:** `docs/superpowers/specs/2026-10-12-mcp-lab-design.md` (section "M2"), narrowed to the M2a design decided by the controller (registry, proxy, redaction, rate limits, OpenAPI backend; no auth).

> Every code block in this plan was dry-run on 2026-10-14 in a scratch clone and is copied byte for byte from the commit that task produced. Final state: 120 tests passing (47 M1 + 73 gateway), ruff clean, pyright strict clean for both packages, and every gateway test checked red against a deliberate mutation of the code it guards.

## Global Constraints

- Python `>=3.13`. Pin every dependency exactly (`uv add --bounds exact`): `fastmcp==4.1.0`, `mcp==2.3.0`, `pydantic==2.14.1`, `pyyaml==6.0.3`, `starlette==1.8.0` (the version fastmcp 4.1 resolves), `uvicorn==0.54.0`, `httpx2==2.13.1`; dev `types-pyyaml==6.0.12.20260906`. `opsdesk-server` is a workspace **dev** dependency of the gateway (tests only); the CLI only launches it as a subprocess.
- pyright runs in **strict** mode for `packages/mcp-gateway/src` and `packages/opsdesk-server/src`. ruff line length is 110 and `docs/` is excluded. pytest uses `--import-mode=importlib`, so tests cannot import each other or `conftest.py`: shared helpers are fixtures, and small type aliases are repeated per test file.
- No `type: ignore`, `noqa`, skips, or `pyright: ignore`. The private `_mcp_server` is never needed: the official `mcp.Client` reaches the gateway over in-process Streamable HTTP instead.
- No sleeps that race. Subprocess tests bind `--port 0`, wait for the announced URL with a deadline, and clean up with terminate → wait → kill.
- Git identity: `AndresRubio <2109109+AndresRubio@users.noreply.github.com>`. Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- FastMCP 4.1 gotchas that bite (all verified):
  - **Use `create_proxy(target, name=...)`, never a hand-built `ProxyClient`.** `create_proxy` makes each backend request mirror the front client's era. A hand-built `ProxyClient` defaults to the legacy era, and a 2026-07-28 front then fails MRTR with `Cannot send 'elicitation/create': this transport context has no back-channel`.
  - **stdio backends need `keep_alive=True`** (set explicitly in `resolve_targets`). MRTR's sealed `requestState` key is per backend process; with `keep_alive=False` each round starts a new process and the retry fails with `Invalid or expired requestState`. The price: every caller shares one backend process.
  - **Starlette does not run mounted apps' lifespans.** The parent lifespan must enter each `http_app().lifespan`, or every request returns 500 (`Task group is not initialized`). Yield `None`: `fastmcp.utilities.lifespan.combine_lifespans` yields `{}`, which in-process ASGI harnesses reject (`The server does not support "state" in the lifespan scope`).
  - **An `httpx_client_factory` must accept `**kwargs`** besides the Protocol's `headers`, `timeout`, `auth`: fastmcp also passes `follow_redirects=`.
  - **`InputRequired*` results must pass through middleware untouched** (`InputRequiredToolResult`, `InputRequiredResourceResult`, `InputRequiredPromptResult`). Their content is empty; rebuilding one as a plain result swallows the question.
  - **MRTR rounds double-count.** On a 2026-07-28 front each round is a separate `tools/call` through `on_call_tool`. Count only calls without `request_state`. This is safe because the gateway's own `RequestStateBoundary` seals and verifies `requestState`, so a forged retry is rejected before middleware runs.
  - The caller's name is `ctx.request_context.session.client_params.client_info.name` (both eras). `Context.client_id` is something else.
  - Under pytest, use `capfd`, not `capsys`, around code that spawns a stdio backend: the child inherits stderr and needs a real file descriptor (`fileno`).

## File Structure

```
mcp-lab/
  pyproject.toml                          # + mcp-gateway member dep, types-pyyaml, ruff first-party, pyright strict path
  uv.lock
  packages/mcp-gateway/
    pyproject.toml                        # deps (exact pins), console script mcp-gateway, uv_build module-name gateway
    registry.yaml                         # demo registry: opsdesk (stdio) + pager (openapi)
    src/gateway/__init__.py               # __version__
    src/gateway/py.typed
    src/gateway/registry.py               # control plane: models, load_registry, RegistryHolder
    src/gateway/redaction.py              # redact()
    src/gateway/pager.py                  # demo OpenAPI backend: create_pager_app, SPEC, pager_mcp, CLI
    src/gateway/visibility.py             # RegistryVisibility transform
    src/gateway/policies.py               # GatewayPolicyMiddleware, caller_of
    src/gateway/app.py                    # build_gateway_servers, resolve_targets, load_openapi_spec, create_app
    src/gateway/__main__.py               # mcp-gateway serve | audit
    tests/conftest.py                     # registry/holder/store/pager/servers fixtures, subprocess helper
    tests/test_registry.py
    tests/test_redaction.py
    tests/test_pager.py
    tests/test_gateway.py                 # visibility, hot reload, MRTR, progress (in memory)
    tests/test_policies.py                # refresh, redaction, rate limits
    tests/test_targets.py
    tests/test_http.py                    # ASGI app in-process, official SDK client, known gaps
    tests/test_cli.py                     # audit, serve end to end
  README.md                               # M2a row + Gateway section
  docs/notes/m2a.md
```

`.github/workflows/ci.yml` needs no change: `uv sync --locked` installs the new member (the root project depends on it), `uv run pytest` collects `packages/` (`testpaths`), and `uv run pyright` reads the strict list in the root `pyproject.toml`. The CI runner has `uv` on `PATH`, which `test_audit_of_the_demo_registry` needs.

---

### Task 1: Package scaffold and the YAML registry (control plane)

**Files:**
- Create: `packages/mcp-gateway/pyproject.toml`, `packages/mcp-gateway/src/gateway/__init__.py`, `packages/mcp-gateway/src/gateway/py.typed`, `packages/mcp-gateway/src/gateway/registry.py`, `packages/mcp-gateway/tests/conftest.py`
- Modify: `pyproject.toml`, `uv.lock`
- Test: `packages/mcp-gateway/tests/test_registry.py`

**Interfaces:**
- Consumes: the M1 workspace.
- Produces:
  - `gateway.__version__: str`
  - Models (pydantic, frozen, `extra="forbid"`): `StdioSource(kind="stdio", command: list[str] (min 1))`, `HttpSource(kind="http", url: str)`, `OpenAPISource(kind="openapi", spec: str, base_url: str, mcp_names: dict[str, str] = {})`, `Source` (discriminated on `kind`), `RateLimit(calls_per_minute: int > 0)`, `ToolPolicy(enabled: bool = False, owner: str | None = None)`, `ServerEntry(owner: str, source: Source, rate_limit: RateLimit | None = None, tools: dict[str, ToolPolicy] = {})` with `.is_enabled(tool: str) -> bool`, `Registry(servers: dict[str, ServerEntry])`
  - `RegistryError(ValueError)`
  - `load_registry(path: Path) -> Registry` (raises `RegistryError` naming the path)
  - `RegistryHolder(path: Path)` with `.path`, `.current: Registry`, `.last_error: str | None`, `.refresh() -> bool`
  - Test fixtures: `anyio_backend`, `registry_path`, `write_registry` (a `Callable[[Any], Path]` that writes a dict as YAML, or raw text, and bumps the mtime by one second per write).

`OpenAPISource.mcp_names` is an addition to the design's `{spec, base_url}`: an OpenAPI backend's tool names are its operationIds unless renamed, and the registry keys tools by name, so the rename must live next to the source for `resolve_targets` (Task 6) to produce the names the registry lists.

- [ ] **Step 1: Create the package skeleton**

`packages/mcp-gateway/pyproject.toml` (dependencies are added in Step 2):

```toml
[project]
name = "mcp-gateway"
version = "0.1.0"
description = "An MCP gateway: registry-governed proxy with redaction and per-caller rate limits"
requires-python = ">=3.13"
dependencies = []

[project.scripts]
mcp-gateway = "gateway.__main__:main"

[build-system]
requires = ["uv_build>=0.11,<0.12"]
build-backend = "uv_build"

[tool.uv.build-backend]
module-name = "gateway"
```

`packages/mcp-gateway/src/gateway/__init__.py`:

```python
"""mcp-gateway: one front door for many MCP servers, governed by a reviewed registry."""

__version__ = "0.1.0"
```

`packages/mcp-gateway/src/gateway/py.typed`: an empty file.

- [ ] **Step 2: Pin dependencies and add the member to the workspace**

Run (from the repo root):

```bash
uv add --bounds exact --package mcp-gateway fastmcp==4.1.0 mcp==2.3.0 pydantic==2.14.1 pyyaml==6.0.3 starlette==1.8.0 uvicorn==0.54.0 httpx2==2.13.1
uv add --bounds exact --package mcp-gateway --dev opsdesk-server
uv add mcp-gateway
uv add --bounds exact --dev types-pyyaml==6.0.12.20260906
```

Then, in the root `pyproject.toml`, add `"gateway"` to ruff's `known-first-party` and `packages/mcp-gateway/src` to pyright's `strict` list. The two files must end up as:

`packages/mcp-gateway/pyproject.toml`:

```toml
[project]
name = "mcp-gateway"
version = "0.1.0"
description = "An MCP gateway: registry-governed proxy with redaction and per-caller rate limits"
requires-python = ">=3.13"
dependencies = [
    "fastmcp==4.1.0",
    "httpx2==2.13.1",
    "mcp==2.3.0",
    "pydantic==2.14.1",
    "pyyaml==6.0.3",
    "starlette==1.8.0",
    "uvicorn==0.54.0",
]

[project.scripts]
mcp-gateway = "gateway.__main__:main"

[build-system]
requires = ["uv_build>=0.11,<0.12"]
build-backend = "uv_build"

[tool.uv.build-backend]
module-name = "gateway"

[tool.uv.sources]
opsdesk-server = { workspace = true }

[dependency-groups]
dev = [
    "opsdesk-server",
]
```

`pyproject.toml`:

```toml
[project]
name = "mcp-lab"
version = "0.1.0"
description = "A living lab for MCP 2026-07-28, agent protocols and how to test them"
requires-python = ">=3.13"
dependencies = [
    "mcp-gateway",
    "opsdesk-server",
]

[tool.uv.workspace]
members = ["packages/*"]

[tool.uv.sources]
opsdesk-server = { workspace = true }
mcp-gateway = { workspace = true }

[dependency-groups]
dev = [
    "anyio==4.15.1",
    "httpx2==2.13.1",
    "pyright==1.1.414",
    "pytest==9.1.1",
    "ruff==0.17.0",
    "types-pyyaml==6.0.12.20260906",
]

[tool.ruff]
line-length = 110
target-version = "py313"
extend-exclude = ["docs"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "RUF"]

[tool.ruff.lint.isort]
known-first-party = ["gateway", "opsdesk"]

[tool.pyright]
pythonVersion = "3.13"
include = ["packages"]
strict = ["packages/mcp-gateway/src", "packages/opsdesk-server/src"]

[tool.pytest.ini_options]
testpaths = ["packages"]
addopts = "-ra --strict-markers --import-mode=importlib"
```

- [ ] **Step 3: Write the fixtures**

`packages/mcp-gateway/tests/conftest.py`:

```python
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.yaml"


@pytest.fixture
def write_registry(registry_path: Path) -> Callable[[Any], Path]:
    """Write `data` (a dict as YAML, or raw text) and move the mtime forward by a whole second.

    Filesystem timestamps can be coarse, so two quick writes may share an mtime; bumping it
    explicitly makes every write visible to `RegistryHolder.refresh()` without sleeping.
    """
    generation = [0]

    def write(data: Any) -> Path:
        text = data if isinstance(data, str) else yaml.safe_dump(data)
        registry_path.write_text(text)
        generation[0] += 1
        stamp = 1_700_000_000_000_000_000 + generation[0] * 1_000_000_000
        os.utime(registry_path, ns=(stamp, stamp))
        return registry_path

    return write
```

- [ ] **Step 4: Write the failing tests**

`packages/mcp-gateway/tests/test_registry.py`:

```python
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from gateway.registry import (
    HttpSource,
    OpenAPISource,
    Registry,
    RegistryError,
    RegistryHolder,
    StdioSource,
    load_registry,
)

WriteRegistry = Callable[[Any], Path]

OPSDESK = {
    "owner": "oncall-team",
    "source": {"kind": "stdio", "command": ["uv", "run", "opsdesk-server"]},
    "rate_limit": {"calls_per_minute": 30},
    "tools": {
        "list_incidents": {"enabled": True, "owner": "oncall-team"},
        "restart_service": {"enabled": False, "owner": "oncall-team"},
    },
}


def test_parses_every_source_kind(write_registry: WriteRegistry) -> None:
    path = write_registry(
        {
            "servers": {
                "opsdesk": OPSDESK,
                "remote": {"owner": "x", "source": {"kind": "http", "url": "http://127.0.0.1:9/mcp"}},
                "pager": {
                    "owner": "y",
                    "source": {
                        "kind": "openapi",
                        "spec": "gateway.pager:SPEC",
                        "base_url": "http://p",
                        "mcp_names": {"listPages": "list_pages"},
                    },
                },
            }
        }
    )
    registry = load_registry(path)
    servers = registry.servers
    assert servers["opsdesk"].source == StdioSource(kind="stdio", command=["uv", "run", "opsdesk-server"])
    assert servers["remote"].source == HttpSource(kind="http", url="http://127.0.0.1:9/mcp")
    assert servers["pager"].source == OpenAPISource(
        kind="openapi", spec="gateway.pager:SPEC", base_url="http://p", mcp_names={"listPages": "list_pages"}
    )
    assert servers["opsdesk"].rate_limit is not None
    assert servers["opsdesk"].rate_limit.calls_per_minute == 30


def test_defaults_are_closed() -> None:
    registry = Registry.model_validate(
        {
            "servers": {
                "s": {
                    "owner": "team",
                    "source": {"kind": "http", "url": "http://x/mcp"},
                    "tools": {"listed_without_flag": {}},
                }
            }
        }
    )
    entry = registry.servers["s"]
    assert entry.rate_limit is None
    assert entry.tools["listed_without_flag"].enabled is False
    assert entry.tools["listed_without_flag"].owner is None


def test_only_listed_and_enabled_tools_are_enabled() -> None:
    entry = Registry.model_validate({"servers": {"opsdesk": OPSDESK}}).servers["opsdesk"]
    assert entry.is_enabled("list_incidents") is True
    assert entry.is_enabled("restart_service") is False
    assert entry.is_enabled("not_in_registry") is False


@pytest.mark.parametrize(
    "server",
    [
        pytest.param({"owner": "t", "source": {"kind": "ftp", "url": "x"}}, id="unknown-kind"),
        pytest.param({"owner": "t", "source": {"kind": "stdio", "command": []}}, id="empty-command"),
        pytest.param(
            {"owner": "t", "source": {"kind": "http", "url": "x"}, "rate_limit": {"calls_per_minute": 0}},
            id="zero-rate",
        ),
        pytest.param({"owner": "t", "source": {"kind": "http", "url": "x"}, "tols": {}}, id="typo-field"),
    ],
)
def test_invalid_entries_are_rejected(server: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Registry.model_validate({"servers": {"s": server}})


def test_refresh_reloads_when_mtime_changes(write_registry: WriteRegistry) -> None:
    holder = RegistryHolder(write_registry({"servers": {"opsdesk": OPSDESK}}))
    assert holder.current.servers["opsdesk"].is_enabled("restart_service") is False

    flipped = {**OPSDESK, "tools": {"restart_service": {"enabled": True}}}
    write_registry({"servers": {"opsdesk": flipped}})
    assert holder.refresh() is True
    assert holder.current.servers["opsdesk"].is_enabled("restart_service") is True
    assert holder.refresh() is False


def test_refresh_ignores_content_change_with_same_mtime(write_registry: WriteRegistry) -> None:
    path = write_registry({"servers": {"opsdesk": OPSDESK}})
    holder = RegistryHolder(path)
    stamp = path.stat().st_mtime_ns
    path.write_text("servers: {}\n")
    os.utime(path, ns=(stamp, stamp))
    assert holder.refresh() is False
    assert "opsdesk" in holder.current.servers


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("servers: [unclosed\n", id="yaml-syntax"),
        pytest.param("servers:\n  s: {owner: t}\n", id="schema"),
        pytest.param("- just\n- a list\n", id="not-a-mapping"),
    ],
)
def test_invalid_reload_keeps_last_good_registry(
    write_registry: WriteRegistry, bad: str, caplog: pytest.LogCaptureFixture
) -> None:
    holder = RegistryHolder(write_registry({"servers": {"opsdesk": OPSDESK}}))
    good = holder.current
    write_registry(bad)
    with caplog.at_level(logging.ERROR, logger="gateway.registry"):
        assert holder.refresh() is False
    assert holder.current is good
    assert holder.last_error is not None
    assert "keeping the last good registry" in caplog.text

    write_registry({"servers": {"opsdesk": OPSDESK, "x": {**OPSDESK, "owner": "z"}}})
    assert holder.refresh() is True
    assert holder.last_error is None
    assert set(holder.current.servers) == {"opsdesk", "x"}


def test_deleted_file_keeps_last_good_registry(write_registry: WriteRegistry) -> None:
    path = write_registry({"servers": {"opsdesk": OPSDESK}})
    holder = RegistryHolder(path)
    path.unlink()
    assert holder.refresh() is False
    assert "opsdesk" in holder.current.servers
    assert holder.last_error is not None


def test_initial_load_failure_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("servers: [unclosed\n")
    with pytest.raises(RegistryError, match=r"bad\.yaml"):
        RegistryHolder(path)
    with pytest.raises(RegistryError, match=r"missing\.yaml"):
        RegistryHolder(tmp_path / "missing.yaml")
```

- [ ] **Step 5: Run the tests to verify they fail**

Run: `uv run pytest packages/mcp-gateway/tests/test_registry.py -v`
Expected: ERROR at collection with `ModuleNotFoundError: No module named 'gateway.registry'`.

- [ ] **Step 6: Implement the registry**

`packages/mcp-gateway/src/gateway/registry.py`:

```python
"""The gateway's control plane: a reviewed YAML registry of servers and the tools they may expose.

A tool the registry does not list is hidden, and a listed tool is disabled unless it says
`enabled: true`. Enabling a tool is therefore a reviewed edit to the YAML file.

`RegistryHolder.refresh()` reloads the file when its mtime changes, so tool enablement, owners
and rate limits change live. The *set* of servers does not: each server is a mounted proxy built
at startup, so adding or removing a server needs a gateway restart.
"""

import logging
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)


class _Strict(BaseModel):
    # A typo such as `tols:` must fail review loudly instead of silently disabling everything.
    model_config = ConfigDict(extra="forbid", frozen=True)


class StdioSource(_Strict):
    kind: Literal["stdio"]
    command: list[str] = Field(min_length=1)


class HttpSource(_Strict):
    kind: Literal["http"]
    url: str


class OpenAPISource(_Strict):
    kind: Literal["openapi"]
    spec: str = Field(description="`package.module:ATTR` holding a spec dict, or a path to a JSON file")
    base_url: str
    # operationId -> MCP tool name, so registry tool keys can stay snake_case and stable.
    mcp_names: dict[str, str] = Field(default_factory=dict[str, str])


Source = Annotated[StdioSource | HttpSource | OpenAPISource, Field(discriminator="kind")]


class RateLimit(_Strict):
    calls_per_minute: int = Field(gt=0)


class ToolPolicy(_Strict):
    enabled: bool = False
    owner: str | None = None


class ServerEntry(_Strict):
    owner: str
    source: Source
    rate_limit: RateLimit | None = None
    tools: dict[str, ToolPolicy] = Field(default_factory=dict[str, ToolPolicy])

    def is_enabled(self, tool: str) -> bool:
        """True only for a tool that is listed *and* enabled: unknown tools stay hidden."""
        policy = self.tools.get(tool)
        return policy is not None and policy.enabled


class Registry(_Strict):
    servers: dict[str, ServerEntry]


class RegistryError(ValueError):
    """The registry file could not be read, parsed or validated."""


def load_registry(path: Path) -> Registry:
    try:
        data = yaml.safe_load(path.read_text())
        return Registry.model_validate(data)
    except (OSError, yaml.YAMLError, ValidationError) as err:
        raise RegistryError(f"{path}: {err}") from err


class RegistryHolder:
    """Holds the last good registry and reloads it when the file's `st_mtime_ns` changes.

    Live: tool enablement, owners and rate limits. Restart required: adding or removing servers.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self._mtime_ns = path.stat().st_mtime_ns  # stat first: a write after this is seen later
        except OSError as err:
            raise RegistryError(f"{path}: {err}") from err
        self.current: Registry = load_registry(path)
        self.last_error: str | None = None

    def refresh(self) -> bool:
        """Reload if the file changed. Returns True only when a new registry was installed."""
        try:
            mtime_ns = self.path.stat().st_mtime_ns
        except OSError as err:
            return self._reject(str(err))
        if mtime_ns == self._mtime_ns:
            return False
        self._mtime_ns = mtime_ns
        try:
            registry = load_registry(self.path)
        except RegistryError as err:
            return self._reject(str(err))
        self.current = registry
        self.last_error = None
        logger.info("registry %s reloaded", self.path)
        return True

    def _reject(self, error: str) -> bool:
        if error != self.last_error:
            logger.error("registry reload failed, keeping the last good registry: %s", error)
        self.last_error = error
        return False
```

- [ ] **Step 7: Run tests, lint and types**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run pyright`
Expected: `61 passed` (the 47 M1 tests plus the gateway's), `All checks passed!`, `18 files already formatted`, `0 errors, 0 warnings, 0 informations`.

- [ ] **Step 8: Commit**

```bash
git add packages/mcp-gateway/pyproject.toml packages/mcp-gateway/src/gateway/__init__.py packages/mcp-gateway/src/gateway/py.typed packages/mcp-gateway/src/gateway/registry.py packages/mcp-gateway/tests/conftest.py packages/mcp-gateway/tests/test_registry.py pyproject.toml uv.lock
git commit -m "feat(gateway): scaffold mcp-gateway and the YAML registry control plane

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: E-mail redaction

**Files:**
- Create: `packages/mcp-gateway/src/gateway/redaction.py`
- Test: `packages/mcp-gateway/tests/test_redaction.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `gateway.redaction.REDACTED_EMAIL = "[REDACTED:email]"`, `EMAIL: re.Pattern[str]`, `redact(value: Any) -> Any` (pure; recurses through `str`, dict values and list items; other types and dict keys unchanged).

- [ ] **Step 1: Write the failing tests**

`packages/mcp-gateway/tests/test_redaction.py`:

```python
import pytest

from gateway.redaction import REDACTED_EMAIL, redact


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("mail jane.doe@example.com now", "mail [REDACTED:email] now", id="dotted-local"),
        pytest.param("a+tag@sub.example.co.uk", "[REDACTED:email]", id="plus-and-subdomains"),
        pytest.param("x@a.io, y@b.io", "[REDACTED:email], [REDACTED:email]", id="several"),
        pytest.param("ping @ops.team on chat", "ping @ops.team on chat", id="handle-not-email"),
        pytest.param("", "", id="empty"),
    ],
)
def test_redacts_email_addresses_in_strings(text: str, expected: str) -> None:
    assert redact(text) == expected


def test_recurses_through_dicts_and_lists_and_keeps_other_types() -> None:
    value = {
        "primary": "ops@example.com",
        "nested": [{"who": "a@b.io"}, "c@d.io", 3, None, True, 1.5],
        "count": 2,
    }
    assert redact(value) == {
        "primary": REDACTED_EMAIL,
        "nested": [{"who": REDACTED_EMAIL}, REDACTED_EMAIL, 3, None, True, 1.5],
        "count": 2,
    }


def test_is_pure() -> None:
    original = {"k": ["a@b.io"]}
    redact(original)
    assert original == {"k": ["a@b.io"]}


def test_dict_keys_are_left_alone() -> None:
    assert redact({"a@b.io": "x"}) == {"a@b.io": "x"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest packages/mcp-gateway/tests/test_redaction.py -v`
Expected: ERROR at collection with `ModuleNotFoundError: No module named 'gateway.redaction'`.

- [ ] **Step 3: Implement `redact`**

`packages/mcp-gateway/src/gateway/redaction.py`:

```python
"""PII redaction for everything the gateway returns: a pure function over JSON-shaped values."""

import re
from typing import Any, cast

REDACTED_EMAIL = "[REDACTED:email]"
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def redact(value: Any) -> Any:
    """Return a copy of `value` with e-mail addresses replaced by `[REDACTED:email]`.

    Recurses through str, dict values and list items; every other type is returned unchanged.
    Dict keys are schema, not data, so they are left alone.
    """
    if isinstance(value, str):
        return EMAIL.sub(REDACTED_EMAIL, value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in cast(dict[Any, Any], value).items()}
    if isinstance(value, list):
        return [redact(v) for v in cast(list[Any], value)]
    return value
```

- [ ] **Step 4: Run tests, lint and types**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run pyright`
Expected: `69 passed` (the 47 M1 tests plus the gateway's), `All checks passed!`, `20 files already formatted`, `0 errors, 0 warnings, 0 informations`.

- [ ] **Step 5: Commit**

```bash
git add packages/mcp-gateway/src/gateway/redaction.py packages/mcp-gateway/tests/test_redaction.py
git commit -m "feat(gateway): e-mail redaction over JSON-shaped values

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Pager, the demo OpenAPI backend

**Files:**
- Create: `packages/mcp-gateway/src/gateway/pager.py`
- Modify: `packages/mcp-gateway/tests/conftest.py` (adds the `pager_http` fixture)
- Test: `packages/mcp-gateway/tests/test_pager.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `gateway.pager.ROTA: dict[str, tuple[str, str]]` (service → primary, secondary e-mails)
  - `MCP_NAMES = {"getOncall": "oncall_for", "createPage": "send_page", "listPages": "list_pages"}`
  - `SPEC: dict[str, Any]` (OpenAPI 3.1, servers `http://127.0.0.1:8100`)
  - `create_pager_app() -> Starlette`: `GET /oncall/{service}` → `{service, primary, secondary}` or 404 `{"detail": ...}`; `POST /pages` `{service, message}` → 201 `{id, service, message}`; `GET /pages` → list. State per app instance.
  - `pager_mcp(client: httpx2.AsyncClient) -> FastMCP` (tools `oncall_for`, `send_page`, `list_pages`; `list_pages` returns `{"result": [...]}` because an array response is wrapped)
  - `main(argv: list[str] | None = None) -> None`: `python -m gateway.pager [--host 127.0.0.1] [--port 8100]`; binds first, then prints `Pager listening on http://H:P` to stderr.
  - Test fixtures: `pager_http` (an `httpx2.AsyncClient` over `httpx2.ASGITransport(create_pager_app())`, base URL `http://pager.local`), `announced` (`with announced([cmd...], r"regex") as match:` runs a subprocess until the block ends).

FastAPI is not needed: `OpenAPIProvider` works from a plain dict spec, and `httpx2.ASGITransport` drives the Starlette app in-process (plain request/response, so the non-streaming transport is fine here).

- [ ] **Step 1: Add the fixtures**

Replace `packages/mcp-gateway/tests/conftest.py` with:

`packages/mcp-gateway/tests/conftest.py`:

```python
import os
import re
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

import httpx2
import pytest
import yaml

from gateway.pager import create_pager_app

Announced = Callable[..., AbstractContextManager[re.Match[str]]]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.yaml"


@pytest.fixture
def write_registry(registry_path: Path) -> Callable[[Any], Path]:
    """Write `data` (a dict as YAML, or raw text) and move the mtime forward by a whole second.

    Filesystem timestamps can be coarse, so two quick writes may share an mtime; bumping it
    explicitly makes every write visible to `RegistryHolder.refresh()` without sleeping.
    """
    generation = [0]

    def write(data: Any) -> Path:
        text = data if isinstance(data, str) else yaml.safe_dump(data)
        registry_path.write_text(text)
        generation[0] += 1
        stamp = 1_700_000_000_000_000_000 + generation[0] * 1_000_000_000
        os.utime(registry_path, ns=(stamp, stamp))
        return registry_path

    return write


@pytest.fixture
async def pager_http() -> AsyncIterator[httpx2.AsyncClient]:
    transport = httpx2.ASGITransport(app=create_pager_app())
    async with httpx2.AsyncClient(transport=transport, base_url="http://pager.local") as client:
        yield client


@contextmanager
def _announced(args: list[str], pattern: str, timeout: float = 20) -> Iterator[re.Match[str]]:
    """Run `args`, wait until its stderr matches `pattern`, yield the match, then stop it.

    stderr goes to a file, not a PIPE: an undrained pipe blocks the child once it fills.
    """
    with tempfile.TemporaryFile(mode="w+") as log:
        proc = subprocess.Popen(args, stderr=log, stdout=subprocess.DEVNULL, text=True)
        try:
            deadline = time.monotonic() + timeout
            while True:
                log.seek(0)
                output = log.read()
                if match := re.search(pattern, output):
                    break
                if proc.poll() is not None:
                    raise RuntimeError(f"{args} exited with code {proc.returncode}: {output}")
                if time.monotonic() > deadline:
                    raise RuntimeError(f"{args} did not announce itself: {output}")
                time.sleep(0.05)
            yield match
        finally:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


@pytest.fixture
def announced() -> Announced:
    """`with announced([cmd...], r"regex") as match:` runs a server subprocess for the block."""
    return _announced
```

- [ ] **Step 2: Write the failing tests**

`packages/mcp-gateway/tests/test_pager.py`:

```python
import re
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager

import httpx2
import pytest
from fastmcp import Client

from gateway.pager import MCP_NAMES, SPEC, create_pager_app, pager_mcp

pytestmark = pytest.mark.anyio

Announced = Callable[..., AbstractContextManager[re.Match[str]]]


async def test_oncall_returns_primary_and_secondary(pager_http: httpx2.AsyncClient) -> None:
    response = await pager_http.get("/oncall/api")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "api"
    assert body["primary"].endswith("@example.com")
    assert body["secondary"].endswith("@example.com")


async def test_unknown_service_is_404(pager_http: httpx2.AsyncClient) -> None:
    response = await pager_http.get("/oncall/nope")
    assert response.status_code == 404
    assert response.json() == {"detail": "No rota for service 'nope'."}


async def test_pages_are_created_and_listed(pager_http: httpx2.AsyncClient) -> None:
    assert (await pager_http.get("/pages")).json() == []
    first = await pager_http.post("/pages", json={"service": "api", "message": "5xx spike"})
    second = await pager_http.post("/pages", json={"service": "search", "message": "down"})
    assert first.status_code == 201
    assert first.json() == {"id": 1, "service": "api", "message": "5xx spike"}
    assert second.json()["id"] == 2
    assert [p["id"] for p in (await pager_http.get("/pages")).json()] == [1, 2]


async def test_each_app_instance_has_its_own_pages() -> None:
    a, b = create_pager_app(), create_pager_app()
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=a), base_url="http://a") as ca:
        await ca.post("/pages", json={"service": "api", "message": "x"})
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=b), base_url="http://b") as cb:
        assert (await cb.get("/pages")).json() == []


def test_spec_names_every_operation() -> None:
    operation_ids = {op["operationId"] for path in SPEC["paths"].values() for op in path.values()}
    assert operation_ids == set(MCP_NAMES)
    assert SPEC["openapi"].startswith("3.1")


async def test_mcp_surface_has_the_three_named_tools(pager_http: httpx2.AsyncClient) -> None:
    async with Client(pager_mcp(pager_http)) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == {"oncall_for", "send_page", "list_pages"}


async def test_mcp_tools_call_the_http_api(pager_http: httpx2.AsyncClient) -> None:
    async with Client(pager_mcp(pager_http)) as client:
        oncall = await client.call_tool("oncall_for", {"service": "payments"})
        sent = await client.call_tool("send_page", {"service": "payments", "message": "payout late"})
        listed = await client.call_tool("list_pages", {})
    assert oncall.structured_content is not None
    assert oncall.structured_content["primary"].endswith("@example.com")
    assert sent.structured_content == {"id": 1, "service": "payments", "message": "payout late"}
    assert listed.structured_content == {
        "result": [{"id": 1, "service": "payments", "message": "payout late"}]
    }


def test_cli_binds_then_announces_and_serves(announced: Announced) -> None:
    with announced(
        [sys.executable, "-m", "gateway.pager", "--port", "0"], r"Pager listening on (http://\S+)"
    ) as m:
        response = httpx2.get(f"{m.group(1)}/oncall/search")
    assert response.status_code == 200
    assert response.json()["service"] == "search"
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest packages/mcp-gateway/tests/test_pager.py -v`
Expected: ERROR with `ImportError while loading conftest` ... `ModuleNotFoundError: No module named 'gateway.pager'`.

- [ ] **Step 4: Implement the Pager**

`packages/mcp-gateway/src/gateway/pager.py`:

```python
"""Pager: a tiny on-call paging HTTP API, the gateway's demo OpenAPI backend.

`GET /oncall/{service}` returns e-mail addresses, so redaction has a second, non-MCP backend to
work on. State lives in each app instance (`create_pager_app()`), so tests never share pages.
Run it with `python -m gateway.pager --port 8100`.
"""

import argparse
import socket
import sys
from typing import Any

import httpx2
import uvicorn
from fastmcp import FastMCP
from fastmcp.server.providers.openapi import OpenAPIProvider
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

ROTA: dict[str, tuple[str, str]] = {
    "api": ("dana.lee@example.com", "sam.ortiz@example.com"),
    "payments": ("priya.n@example.com", "dana.lee@example.com"),
    "search": ("sam.ortiz@example.com", "priya.n@example.com"),
}

MCP_NAMES = {"getOncall": "oncall_for", "createPage": "send_page", "listPages": "list_pages"}

_STRING = {"type": "string"}
_ONCALL: dict[str, Any] = {
    "type": "object",
    "properties": {"service": _STRING, "primary": _STRING, "secondary": _STRING},
    "required": ["service", "primary", "secondary"],
}
_NEW_PAGE: dict[str, Any] = {
    "type": "object",
    "properties": {"service": _STRING, "message": _STRING},
    "required": ["service", "message"],
}
_PAGE: dict[str, Any] = {
    "type": "object",
    "properties": {"id": {"type": "integer"}, "service": _STRING, "message": _STRING},
    "required": ["id", "service", "message"],
}

SPEC: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "Pager", "version": "1.0.0"},
    "servers": [{"url": "http://127.0.0.1:8100"}],
    "paths": {
        "/oncall/{service}": {
            "get": {
                "operationId": "getOncall",
                "summary": "Who is on call for a service (primary and secondary contacts).",
                "parameters": [
                    {"name": "service", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {
                    "200": {"description": "The rota", "content": {"application/json": {"schema": _ONCALL}}},
                    "404": {"description": "No rota for that service"},
                },
            }
        },
        "/pages": {
            "get": {
                "operationId": "listPages",
                "summary": "List the pages sent so far.",
                "responses": {
                    "200": {
                        "description": "All pages",
                        "content": {"application/json": {"schema": {"type": "array", "items": _PAGE}}},
                    }
                },
            },
            "post": {
                "operationId": "createPage",
                "summary": "Page the on-call engineer for a service.",
                "requestBody": {"required": True, "content": {"application/json": {"schema": _NEW_PAGE}}},
                "responses": {
                    "201": {"description": "The page", "content": {"application/json": {"schema": _PAGE}}}
                },
            },
        },
    },
}


def create_pager_app() -> Starlette:
    """A fresh Pager API with its own, empty list of pages."""
    pages: list[dict[str, Any]] = []

    async def oncall(request: Request) -> JSONResponse:
        service = request.path_params["service"]
        if service not in ROTA:
            return JSONResponse({"detail": f"No rota for service '{service}'."}, status_code=404)
        primary, secondary = ROTA[service]
        return JSONResponse({"service": service, "primary": primary, "secondary": secondary})

    async def list_pages(request: Request) -> JSONResponse:
        return JSONResponse(pages)

    async def create_page(request: Request) -> JSONResponse:
        body = await request.json()
        page = {"id": len(pages) + 1, "service": str(body["service"]), "message": str(body["message"])}
        pages.append(page)
        return JSONResponse(page, status_code=201)

    return Starlette(
        routes=[
            Route("/oncall/{service}", oncall, methods=["GET"]),
            Route("/pages", list_pages, methods=["GET"]),
            Route("/pages", create_page, methods=["POST"]),
        ]
    )


def pager_mcp(client: httpx2.AsyncClient) -> FastMCP:
    """The Pager API as an MCP server: one tool per operation, with stable snake_case names."""
    return FastMCP("pager", providers=[OpenAPIProvider(SPEC, client, mcp_names=MCP_NAMES)])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m gateway.pager", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100, help="0 picks a free port")
    args = parser.parse_args(argv)
    # Bind before announcing, so `--port 0` reports the real port and nobody races for it.
    sock = socket.create_server((args.host, args.port))
    port = sock.getsockname()[1]
    print(f"Pager listening on http://{args.host}:{port}", file=sys.stderr, flush=True)
    uvicorn.Server(uvicorn.Config(create_pager_app(), log_level="warning")).run(sockets=[sock])


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run tests, lint and types**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run pyright`
Expected: `77 passed` (the 47 M1 tests plus the gateway's), `All checks passed!`, `22 files already formatted`, `0 errors, 0 warnings, 0 informations`.

- [ ] **Step 6: Commit**

```bash
git add packages/mcp-gateway/src/gateway/pager.py packages/mcp-gateway/tests/conftest.py packages/mcp-gateway/tests/test_pager.py
git commit -m "feat(gateway): Pager demo HTTP API with a hand-written OpenAPI 3.1 spec

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Registry-filtered proxy per backend

**Files:**
- Create: `packages/mcp-gateway/src/gateway/visibility.py`, `packages/mcp-gateway/src/gateway/app.py`
- Modify: `packages/mcp-gateway/tests/conftest.py` (gateway fixtures)
- Test: `packages/mcp-gateway/tests/test_gateway.py`

**Interfaces:**
- Consumes: `RegistryHolder` (Task 1), `pager_mcp`, `MCP_NAMES`, `create_pager_app` (Task 3), `opsdesk.data.seed_store` / `OpsDeskStore`, `opsdesk.server.build_server` (M1).
- Produces:
  - `gateway.visibility.RegistryVisibility(holder: RegistryHolder, server_name: str)`, a `fastmcp.server.transforms.Transform` overriding `list_tools(tools) -> Sequence[Tool]` and `get_tool(name, call_next, *, version=None) -> Tool | None`.
  - `gateway.app.ProxyTarget = FastMCP[Any] | MCPServer | ClientTransport | str`
  - `gateway.app.build_gateway_servers(holder: RegistryHolder, targets: Mapping[str, ProxyTarget]) -> dict[str, FastMCP]` (Task 5 adds a keyword-only `clock`).
  - Test fixtures: `registry_data` (dict: OpsDesk with `list_incidents`, `get_service_status`, `restart_service`, `run_diagnostics` enabled and `update_runbook` disabled; Pager with all three tools enabled), `holder`, `store`, `servers` (`{"opsdesk", "pager"}` gateway servers over an in-process OpsDesk and Pager), `opsdesk`.

Why a transform: it reads `holder.current` on every lookup, so a reload changes visibility with nothing rebuilt or mutated, and it hides the tool from `get_tool`, which the call path uses. `FastMCP.enable()` / `disable()` would only append `Visibility` transforms, with no API to remove one. A hidden tool resolves to `None`, so a call gets the same `Unknown tool: '<name>'` tool error as a tool that does not exist. In this task the tests call `holder.refresh()` themselves; Task 5's middleware does it per request.

- [ ] **Step 1: Add the gateway fixtures**

Replace `packages/mcp-gateway/tests/conftest.py` with:

`packages/mcp-gateway/tests/conftest.py`:

```python
import os
import re
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

import httpx2
import pytest
import yaml
from fastmcp import FastMCP

from gateway.app import build_gateway_servers
from gateway.pager import MCP_NAMES, create_pager_app, pager_mcp
from gateway.registry import RegistryHolder
from opsdesk.data import OpsDeskStore, seed_store
from opsdesk.server import build_server

Announced = Callable[..., AbstractContextManager[re.Match[str]]]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.yaml"


@pytest.fixture
def write_registry(registry_path: Path) -> Callable[[Any], Path]:
    """Write `data` (a dict as YAML, or raw text) and move the mtime forward by a whole second.

    Filesystem timestamps can be coarse, so two quick writes may share an mtime; bumping it
    explicitly makes every write visible to `RegistryHolder.refresh()` without sleeping.
    """
    generation = [0]

    def write(data: Any) -> Path:
        text = data if isinstance(data, str) else yaml.safe_dump(data)
        registry_path.write_text(text)
        generation[0] += 1
        stamp = 1_700_000_000_000_000_000 + generation[0] * 1_000_000_000
        os.utime(registry_path, ns=(stamp, stamp))
        return registry_path

    return write


@pytest.fixture
def registry_data() -> dict[str, Any]:
    """The test registry: every OpsDesk tool enabled except `update_runbook`, all Pager tools enabled."""
    opsdesk_tools = ["list_incidents", "get_service_status", "restart_service", "run_diagnostics"]
    return {
        "servers": {
            "opsdesk": {
                "owner": "oncall-team",
                "source": {"kind": "stdio", "command": ["uv", "run", "opsdesk-server"]},
                "tools": {
                    **{name: {"enabled": True, "owner": "oncall-team"} for name in opsdesk_tools},
                    "update_runbook": {"enabled": False, "owner": "oncall-team"},
                },
            },
            "pager": {
                "owner": "sre",
                "source": {
                    "kind": "openapi",
                    "spec": "gateway.pager:SPEC",
                    "base_url": "http://pager.local",
                    "mcp_names": MCP_NAMES,
                },
                "tools": {name: {"enabled": True} for name in MCP_NAMES.values()},
            },
        }
    }


@pytest.fixture
def holder(write_registry: Callable[[Any], Path], registry_data: dict[str, Any]) -> RegistryHolder:
    return RegistryHolder(write_registry(registry_data))


@pytest.fixture
def store() -> OpsDeskStore:
    return seed_store()


@pytest.fixture
async def pager_http() -> AsyncIterator[httpx2.AsyncClient]:
    transport = httpx2.ASGITransport(app=create_pager_app())
    async with httpx2.AsyncClient(transport=transport, base_url="http://pager.local") as client:
        yield client


@pytest.fixture
def servers(
    holder: RegistryHolder, store: OpsDeskStore, pager_http: httpx2.AsyncClient
) -> dict[str, FastMCP]:
    """The gateway's per-backend servers over an in-process OpsDesk and an in-process Pager."""
    return build_gateway_servers(holder, {"opsdesk": build_server(store), "pager": pager_mcp(pager_http)})


@pytest.fixture
def opsdesk(servers: dict[str, FastMCP]) -> FastMCP:
    return servers["opsdesk"]


@contextmanager
def _announced(args: list[str], pattern: str, timeout: float = 20) -> Iterator[re.Match[str]]:
    """Run `args`, wait until its stderr matches `pattern`, yield the match, then stop it.

    stderr goes to a file, not a PIPE: an undrained pipe blocks the child once it fills.
    """
    with tempfile.TemporaryFile(mode="w+") as log:
        proc = subprocess.Popen(args, stderr=log, stdout=subprocess.DEVNULL, text=True)
        try:
            deadline = time.monotonic() + timeout
            while True:
                log.seek(0)
                output = log.read()
                if match := re.search(pattern, output):
                    break
                if proc.poll() is not None:
                    raise RuntimeError(f"{args} exited with code {proc.returncode}: {output}")
                if time.monotonic() > deadline:
                    raise RuntimeError(f"{args} did not announce itself: {output}")
                time.sleep(0.05)
            yield match
        finally:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


@pytest.fixture
def announced() -> Announced:
    """`with announced([cmd...], r"regex") as match:` runs a server subprocess for the block."""
    return _announced
```

- [ ] **Step 2: Write the failing tests**

`packages/mcp-gateway/tests/test_gateway.py`:

```python
"""The gateway's data plane in memory: a registry-filtered proxy per backend."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import mcp_types as mt
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from gateway.registry import RegistryHolder
from opsdesk.data import OpsDeskStore

pytestmark = pytest.mark.anyio

WriteRegistry = Callable[[Any], Path]


async def _confirm(message: str, response_type: Any, params: Any, context: Any) -> dict[str, Any]:
    return {"confirm": True}


async def _names(server: FastMCP) -> set[str]:
    async with Client(server) as client:
        return {t.name for t in await client.list_tools()}


async def test_proxies_are_named_and_speak_2026_07_28(opsdesk: FastMCP) -> None:
    async with Client(opsdesk) as client:
        assert client.protocol_version == "2026-07-28"
        assert client.server_info is not None
        assert client.server_info.name == "gateway/opsdesk"


async def test_only_enabled_tools_are_listed(servers: dict[str, FastMCP]) -> None:
    assert await _names(servers["opsdesk"]) == {
        "list_incidents",
        "get_service_status",
        "restart_service",
        "run_diagnostics",
    }
    assert await _names(servers["pager"]) == {"oncall_for", "send_page", "list_pages"}


async def test_disabled_tool_is_not_callable(opsdesk: FastMCP, store: OpsDeskStore) -> None:
    async with Client(opsdesk) as client:
        result = await client.call_tool_mcp("update_runbook", {"service": "api", "markdown": "# pwned"})
    assert result.is_error is True
    assert result.content == [mt.TextContent(type="text", text="Unknown tool: 'update_runbook'")]
    assert store.runbooks["api"].startswith("# api runbook")


async def test_unlisted_tool_is_hidden_and_unknown(
    opsdesk: FastMCP, holder: RegistryHolder, write_registry: WriteRegistry, registry_data: dict[str, Any]
) -> None:
    del registry_data["servers"]["opsdesk"]["tools"]["run_diagnostics"]
    write_registry(registry_data)
    holder.refresh()
    assert "run_diagnostics" not in await _names(opsdesk)
    async with Client(opsdesk) as client:
        with pytest.raises(ToolError, match="Unknown tool: 'run_diagnostics'"):
            await client.call_tool("run_diagnostics", {"service": "api"})


async def test_hot_reload_flips_visibility_without_rebuild(
    opsdesk: FastMCP, holder: RegistryHolder, write_registry: WriteRegistry, registry_data: dict[str, Any]
) -> None:
    assert "update_runbook" not in await _names(opsdesk)
    registry_data["servers"]["opsdesk"]["tools"]["update_runbook"]["enabled"] = True
    write_registry(registry_data)
    assert holder.refresh() is True
    assert "update_runbook" in await _names(opsdesk)
    async with Client(opsdesk) as client:
        result = await client.call_tool("update_runbook", {"service": "api", "markdown": "# v2"})
    assert result.is_error is False


async def test_server_removed_from_registry_exposes_nothing_until_restart(
    opsdesk: FastMCP, holder: RegistryHolder, write_registry: WriteRegistry, registry_data: dict[str, Any]
) -> None:
    del registry_data["servers"]["opsdesk"]
    write_registry(registry_data)
    holder.refresh()
    assert await _names(opsdesk) == set()


async def test_mrtr_confirmation_crosses_the_gateway(opsdesk: FastMCP, store: OpsDeskStore) -> None:
    async with Client(opsdesk, elicitation_handler=_confirm) as client:
        result = await client.call_tool("restart_service", {"service": "search"})
    assert result.structured_content == {"service": "search", "restarted": True, "state": "healthy"}
    assert store.service("search").restarts == 1


async def test_progress_crosses_the_gateway(opsdesk: FastMCP) -> None:
    seen: list[float] = []

    async def on_progress(progress: float, total: float | None, message: str | None) -> None:
        seen.append(progress)

    async with Client(opsdesk, progress_handler=on_progress) as client:
        await client.call_tool("run_diagnostics", {"service": "api"})
    assert seen == [1, 2, 3]


async def test_pager_tools_reach_the_http_api(servers: dict[str, FastMCP]) -> None:
    async with Client(servers["pager"]) as client:
        sent = await client.call_tool("send_page", {"service": "api", "message": "5xx"})
    assert sent.structured_content == {"id": 1, "service": "api", "message": "5xx"}
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest packages/mcp-gateway/tests/test_gateway.py -v`
Expected: ERROR with `ImportError while loading conftest` ... `ModuleNotFoundError: No module named 'gateway.app'`.

- [ ] **Step 4: Implement the transform**

`packages/mcp-gateway/src/gateway/visibility.py`:

```python
"""Registry-driven tool visibility, applied as a FastMCP transform on each backend's proxy."""

from collections.abc import Sequence

from fastmcp.server.transforms import GetToolNext, Transform
from fastmcp.tools import Tool
from fastmcp.utilities.versions import VersionSpec

from gateway.registry import RegistryHolder


class RegistryVisibility(Transform):
    """Lists and resolves only the tools the registry enables for `server_name`.

    It reads `holder.current` on every `list_tools` / `get_tool`, so a registry reload takes
    effect on the next request with nothing to rebuild or mutate. (`FastMCP.enable()` /
    `disable()` would only *append* transforms, with no way to remove one.) A hidden tool
    resolves to None, so calling it fails exactly like a tool that does not exist.
    """

    def __init__(self, holder: RegistryHolder, server_name: str) -> None:
        self.holder = holder
        self.server_name = server_name

    def __repr__(self) -> str:
        return f"RegistryVisibility(server_name={self.server_name!r})"

    def _enabled(self, tool: str) -> bool:
        entry = self.holder.current.servers.get(self.server_name)
        return entry is not None and entry.is_enabled(tool)

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        return [tool for tool in tools if self._enabled(tool.name)]

    async def get_tool(
        self, name: str, call_next: GetToolNext, *, version: VersionSpec | None = None
    ) -> Tool | None:
        if not self._enabled(name):
            return None
        return await call_next(name, version=version)
```

- [ ] **Step 5: Build one proxy per backend**

`packages/mcp-gateway/src/gateway/app.py`:

```python
"""Assembles the gateway: one registry-governed proxy per backend server."""

from collections.abc import Mapping
from typing import Any

from fastmcp import FastMCP
from fastmcp.client.transports import ClientTransport
from fastmcp.server import create_proxy
from mcp.server.mcpserver import MCPServer

from gateway.registry import RegistryHolder
from gateway.visibility import RegistryVisibility

# What `create_proxy` can front: an in-process server, a client transport (stdio) or a URL.
ProxyTarget = FastMCP[Any] | MCPServer | ClientTransport | str


def build_gateway_servers(holder: RegistryHolder, targets: Mapping[str, ProxyTarget]) -> dict[str, FastMCP]:
    """One proxy per backend, named `gateway/<name>`, filtered by the registry.

    `create_proxy` (not a hand-built `ProxyClient`, which defaults to the legacy era) makes each
    backend request mirror the front client's protocol era, so MRTR rounds pass straight through.
    """
    servers: dict[str, FastMCP] = {}
    for name, target in targets.items():
        server = create_proxy(target, name=f"gateway/{name}")
        server.add_transform(RegistryVisibility(holder, name))
        servers[name] = server
    return servers
```

- [ ] **Step 6: Run tests, lint and types**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run pyright`
Expected: `86 passed` (the 47 M1 tests plus the gateway's), `All checks passed!`, `25 files already formatted`, `0 errors, 0 warnings, 0 informations`.

- [ ] **Step 7: Commit**

```bash
git add packages/mcp-gateway/src/gateway/app.py packages/mcp-gateway/src/gateway/visibility.py packages/mcp-gateway/tests/conftest.py packages/mcp-gateway/tests/test_gateway.py
git commit -m "feat(gateway): registry-filtered proxy per backend via a hot-reloadable transform

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Policy middleware: live reload, redaction, per-caller rate limits

**Files:**
- Create: `packages/mcp-gateway/src/gateway/policies.py`
- Modify: `packages/mcp-gateway/src/gateway/app.py`
- Test: `packages/mcp-gateway/tests/test_policies.py`

**Interfaces:**
- Consumes: `RegistryHolder` (Task 1), `redact` (Task 2), `build_gateway_servers` and the fixtures (Task 4).
- Produces:
  - `gateway.policies.WINDOW_SECONDS = 60`
  - `caller_of(ctx: fastmcp.server.context.Context | None) -> str` (`clientInfo.name`, else `"anonymous"`)
  - `GatewayPolicyMiddleware(holder: RegistryHolder, server_name: str, clock: Callable[[], float] = time.monotonic)`, a `fastmcp.server.middleware.Middleware` overriding `on_request`, `on_call_tool`, `on_read_resource`, `on_get_prompt`.
  - Over the limit: `ToolError("Rate limit exceeded: caller '<caller>' may make <N> calls per minute to server '<server>'.")`.
  - `build_gateway_servers(holder, targets, *, clock: Callable[[], float] = time.monotonic) -> dict[str, FastMCP]` now also adds the middleware.

Rate limiting: fixed windows aligned to `clock() // 60`, per caller, per server; `calls_per_minute` is read from `holder.current` on each call, so limit changes apply live. Only a `tools/call` without `request_state` counts, because each MRTR round is its own `tools/call` on a 2026-07-28 front. FastMCP's built-in `RateLimitingMiddleware` is not used: it hooks every request (discover, lists, every MRTR round) and raises a JSON-RPC error (-32000) rather than a tool error the model can read. `test_forged_request_state_is_rejected_before_it_reaches_the_backend` pins why skipping retries is safe: the gateway front seals `requestState` with its own key and rejects a forged one before middleware runs.

- [ ] **Step 1: Write the failing tests**

`packages/mcp-gateway/tests/test_policies.py`:

```python
"""GatewayPolicyMiddleware: live registry refresh, redaction and per-caller rate limits."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import mcp_types as mt
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from mcp import MCPError

from gateway.app import build_gateway_servers
from gateway.policies import caller_of
from gateway.registry import RegistryHolder
from opsdesk.data import OpsDeskStore
from opsdesk.server import build_server

pytestmark = pytest.mark.anyio

WriteRegistry = Callable[[Any], Path]
EMAIL_TAIL = "@example.com"


class FakeClock:
    def __init__(self) -> None:
        self.now = 600.0  # the start of a one-minute window

    def __call__(self) -> float:
        return self.now


async def _confirm(message: str, response_type: Any, params: Any, context: Any) -> dict[str, Any]:
    return {"confirm": True}


def _as(name: str) -> mt.Implementation:
    return mt.Implementation(name=name, version="1.0")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def limited(
    write_registry: WriteRegistry, registry_data: dict[str, Any], store: OpsDeskStore, clock: FakeClock
) -> FastMCP:
    """OpsDesk behind the gateway with a limit of 2 calls per minute per caller."""
    registry_data["servers"]["opsdesk"]["rate_limit"] = {"calls_per_minute": 2}
    holder = RegistryHolder(write_registry(registry_data))
    return build_gateway_servers(holder, {"opsdesk": build_server(store)}, clock=clock)["opsdesk"]


async def test_each_request_refreshes_the_registry(
    opsdesk: FastMCP, write_registry: WriteRegistry, registry_data: dict[str, Any]
) -> None:
    async with Client(opsdesk) as client:
        assert "update_runbook" not in {t.name for t in await client.list_tools()}
        registry_data["servers"]["opsdesk"]["tools"]["update_runbook"]["enabled"] = True
        write_registry(registry_data)  # no holder.refresh() here: the middleware does it
        assert "update_runbook" in {t.name for t in await client.list_tools()}


async def test_tool_text_and_structured_content_are_redacted(opsdesk: FastMCP) -> None:
    async with Client(opsdesk) as client:
        result = await client.call_tool("list_incidents", {})
    [text] = result.content
    assert isinstance(text, mt.TextContent)
    assert EMAIL_TAIL not in text.text
    assert "[REDACTED:email]" in text.text
    assert result.structured_content is not None
    descriptions = [i["description"] for i in result.structured_content["incidents"]]
    assert descriptions[0] == "Reported by customer [REDACTED:email]: checkout fails intermittently."
    assert EMAIL_TAIL not in str(result.data)


async def test_resource_reads_are_redacted(opsdesk: FastMCP, store: OpsDeskStore) -> None:
    store.runbooks["api"] += "\nEscalate to lead@example.com."
    async with Client(opsdesk) as client:
        [content] = await client.read_resource("runbook://api")
    assert isinstance(content, mt.TextResourceContents)
    assert content.text.endswith("Escalate to [REDACTED:email].")
    assert content.mime_type == "text/markdown"


async def test_prompt_messages_are_redacted(opsdesk: FastMCP) -> None:
    async with Client(opsdesk) as client:
        result = await client.get_prompt("postmortem", {"incident_id": "INC-101"})
    [message] = result.messages
    assert isinstance(message.content, mt.TextContent)
    assert "Reported by customer [REDACTED:email]" in message.content.text
    assert message.role == "user"


async def test_openapi_backend_results_are_redacted(servers: dict[str, FastMCP]) -> None:
    async with Client(servers["pager"]) as client:
        result = await client.call_tool("oncall_for", {"service": "api"})
    assert result.structured_content == {
        "service": "api",
        "primary": "[REDACTED:email]",
        "secondary": "[REDACTED:email]",
    }


async def test_input_required_round_passes_through_untouched(opsdesk: FastMCP, store: OpsDeskStore) -> None:
    async with Client(opsdesk, elicitation_handler=_confirm) as client:
        first = await client.session.call_tool(
            "restart_service", {"service": "api"}, allow_input_required=True
        )
    assert isinstance(first, mt.InputRequiredResult)
    assert first.input_requests
    assert first.request_state
    assert store.service("api").restarts == 0


async def test_confirmed_restart_counts_as_one_call(limited: FastMCP, store: OpsDeskStore) -> None:
    async with Client(limited, elicitation_handler=_confirm, client_info=_as("alice")) as client:
        restarted = await client.call_tool("restart_service", {"service": "search"})
        second = await client.call_tool("get_service_status", {"service": "search"})
        with pytest.raises(ToolError) as exceeded:
            await client.call_tool("get_service_status", {"service": "search"})
    assert restarted.structured_content == {"service": "search", "restarted": True, "state": "healthy"}
    assert second.is_error is False
    assert str(exceeded.value) == (
        "Rate limit exceeded: caller 'alice' may make 2 calls per minute to server 'opsdesk'."
    )
    assert store.service("search").restarts == 1


async def test_callers_are_limited_independently(limited: FastMCP) -> None:
    async with Client(limited, client_info=_as("alice")) as alice:
        for _ in range(2):
            await alice.call_tool("list_incidents", {})
        with pytest.raises(ToolError, match="caller 'alice'"):
            await alice.call_tool("list_incidents", {})
    async with Client(limited, client_info=_as("bob")) as bob:
        result = await bob.call_tool("list_incidents", {})
    assert result.is_error is False


async def test_the_window_resets_with_the_clock(limited: FastMCP, clock: FakeClock) -> None:
    async with Client(limited, client_info=_as("alice")) as client:
        for _ in range(2):
            await client.call_tool("list_incidents", {})
        clock.now += 30
        with pytest.raises(ToolError, match="Rate limit exceeded"):
            await client.call_tool("list_incidents", {})
        clock.now += 30
        result = await client.call_tool("list_incidents", {})
    assert result.is_error is False


async def test_rate_limit_changes_apply_live(
    limited: FastMCP, write_registry: WriteRegistry, registry_data: dict[str, Any]
) -> None:
    async with Client(limited, client_info=_as("alice")) as client:
        for _ in range(2):
            await client.call_tool("list_incidents", {})
        registry_data["servers"]["opsdesk"]["rate_limit"] = {"calls_per_minute": 3}
        write_registry(registry_data)
        result = await client.call_tool("list_incidents", {})
    assert result.is_error is False


async def test_servers_without_a_rate_limit_are_unlimited(opsdesk: FastMCP) -> None:
    async with Client(opsdesk) as client:
        for _ in range(5):
            await client.call_tool("list_incidents", {})


async def test_forged_request_state_is_rejected_before_it_reaches_the_backend(
    limited: FastMCP, store: OpsDeskStore
) -> None:
    # The gateway seals request state with its own key, so a client cannot skip the rate limit
    # by sending a made-up retry: the boundary rejects it before any middleware runs.
    async with Client(limited, client_info=_as("mallory")) as client:
        for _ in range(3):
            with pytest.raises(MCPError, match="Invalid or expired requestState"):
                await client.session.call_tool(
                    "restart_service",
                    {"service": "search"},
                    allow_input_required=True,
                    input_responses={"x": mt.ElicitResult(action="accept", content={"confirm": True})},
                    request_state="forged",
                )
        allowed = await client.call_tool("list_incidents", {})  # the forged retries were not counted
    assert allowed.is_error is False
    assert store.service("search").restarts == 0


def test_caller_without_a_request_context_is_anonymous() -> None:
    assert caller_of(None) == "anonymous"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest packages/mcp-gateway/tests/test_policies.py -v`
Expected: ERROR at collection with `ModuleNotFoundError: No module named 'gateway.policies'`.

- [ ] **Step 3: Implement the middleware**

`packages/mcp-gateway/src/gateway/policies.py`:

```python
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
    return block


class GatewayPolicyMiddleware(Middleware):
    """Policy for one backend server, applied to every request that reaches its proxy.

    - Calls `holder.refresh()` first, so registry edits apply from the next request on.
    - Redacts e-mail addresses in tool results (text and structured), resource reads and prompt
      messages. `InputRequired*` results are MRTR legs with nothing to redact: they pass through
      untouched, or the front client would never see the question.
    - Limits each caller to `rate_limit.calls_per_minute` tool calls per fixed one-minute window.
      On a 2026-07-28 front every MRTR round is its own `tools/call`, so only the first round is
      counted: a retry carries `request_state`, which the gateway sealed itself.
    """

    def __init__(
        self, holder: RegistryHolder, server_name: str, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.holder = holder
        self.server_name = server_name
        self.clock = clock
        self._windows: dict[str, tuple[int, int]] = {}  # caller -> (window index, calls in it)

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
        if context.message.request_state is None:
            self._count(caller_of(context.fastmcp_context))
        result = await call_next(context)
        if isinstance(result, InputRequiredToolResult):
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
            description=result.description,
            meta=result.meta,
        )

    def _count(self, caller: str) -> None:
        entry = self.holder.current.servers.get(self.server_name)
        if entry is None or entry.rate_limit is None:
            return
        limit = entry.rate_limit.calls_per_minute
        window = int(self.clock() // WINDOW_SECONDS)
        start, calls = self._windows.get(caller, (window, 0))
        calls = calls + 1 if start == window else 1
        self._windows[caller] = (window, calls)
        if calls > limit:
            raise ToolError(
                f"Rate limit exceeded: caller '{caller}' may make {limit} calls per minute "
                f"to server '{self.server_name}'."
            )
```

- [ ] **Step 4: Install it on every proxy**

Replace `packages/mcp-gateway/src/gateway/app.py` with:

`packages/mcp-gateway/src/gateway/app.py`:

```python
"""Assembles the gateway: one registry-governed proxy per backend server."""

import time
from collections.abc import Callable, Mapping
from typing import Any

from fastmcp import FastMCP
from fastmcp.client.transports import ClientTransport
from fastmcp.server import create_proxy
from mcp.server.mcpserver import MCPServer

from gateway.policies import GatewayPolicyMiddleware
from gateway.registry import RegistryHolder
from gateway.visibility import RegistryVisibility

# What `create_proxy` can front: an in-process server, a client transport (stdio) or a URL.
ProxyTarget = FastMCP[Any] | MCPServer | ClientTransport | str


def build_gateway_servers(
    holder: RegistryHolder,
    targets: Mapping[str, ProxyTarget],
    *,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, FastMCP]:
    """One proxy per backend, named `gateway/<name>`, filtered and policed by the registry.

    `create_proxy` (not a hand-built `ProxyClient`, which defaults to the legacy era) makes each
    backend request mirror the front client's protocol era, so MRTR rounds pass straight through.
    """
    servers: dict[str, FastMCP] = {}
    for name, target in targets.items():
        server = create_proxy(target, name=f"gateway/{name}")
        server.add_transform(RegistryVisibility(holder, name))
        server.add_middleware(GatewayPolicyMiddleware(holder, name, clock=clock))
        servers[name] = server
    return servers
```

- [ ] **Step 5: Run tests, lint and types**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run pyright`
Expected: `99 passed` (the 47 M1 tests plus the gateway's), `All checks passed!`, `27 files already formatted`, `0 errors, 0 warnings, 0 informations`.

- [ ] **Step 6: Commit**

```bash
git add packages/mcp-gateway/src/gateway/app.py packages/mcp-gateway/src/gateway/policies.py packages/mcp-gateway/tests/test_policies.py
git commit -m "feat(gateway): policy middleware for live reload, redaction and per-caller rate limits

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Registry sources to proxy targets, and one ASGI app

**Files:**
- Modify: `packages/mcp-gateway/src/gateway/app.py`
- Test: `packages/mcp-gateway/tests/test_targets.py`, `packages/mcp-gateway/tests/test_http.py`

**Interfaces:**
- Consumes: `Registry` and the source models (Task 1), `build_gateway_servers` (Task 5), the fixtures (Task 4).
- Produces:
  - `gateway.app.load_openapi_spec(ref: str) -> dict[str, Any]`: a `*.json` path (relative to the working directory), or `package.module:ATTR`; raises `ValueError` for a malformed reference, a missing module/attribute, or a value that is not a dict with an `openapi` key.
  - `resolve_targets(registry: Registry) -> dict[str, ProxyTarget]`: stdio → `StdioTransport(command=cmd[0], args=cmd[1:], keep_alive=True)`; http → the URL string; openapi → `FastMCP.from_openapi(spec, client=httpx2.AsyncClient(base_url=..., timeout=30), name=<name>, mcp_names=source.mcp_names)`.
  - `create_app(servers: Mapping[str, FastMCP]) -> Starlette`: each server's `http_app(path="/mcp")` mounted at `/<name>`, a hand-written lifespan that enters every sub-app lifespan and yields `None`, and `GET /healthz` → `{"status": "ok", "servers": [<names in order>]}`.

`test_http.py` drives the app with `run_asgi_lifespan` and `StreamingASGITransport` (MCP needs streaming SSE responses, which `httpx2.ASGITransport` does not provide). The official `mcp.Client` reaches the app through `streamable_http_client(url, http_client=...)`, which avoids fastmcp's private `_mcp_server`. The two `test_known_gap_*` tests pin FastMCP 4.1 behaviour that `docs/notes/m2a.md` describes: `subscriptions/listen` is answered with `Method not found`, and the backend's `ttlMs: 60000, public` comes out as `ttlMs: 0, private`.

- [ ] **Step 1: Write the failing tests**

`packages/mcp-gateway/tests/test_targets.py`:

```python
import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport

from gateway.app import load_openapi_spec, resolve_targets
from gateway.pager import SPEC
from gateway.registry import Registry

pytestmark = pytest.mark.anyio


def _registry(source: dict[str, Any]) -> Registry:
    return Registry.model_validate({"servers": {"s": {"owner": "t", "source": source}}})


def test_stdio_becomes_a_kept_alive_stdio_transport() -> None:
    [target] = resolve_targets(
        _registry({"kind": "stdio", "command": ["uv", "run", "opsdesk-server"]})
    ).values()
    assert isinstance(target, StdioTransport)
    assert (target.command, target.args) == ("uv", ["run", "opsdesk-server"])
    # MRTR needs one backend process across rounds: its sealed requestState key is per process.
    assert target.keep_alive is True


def test_http_becomes_its_url() -> None:
    targets = resolve_targets(_registry({"kind": "http", "url": "http://127.0.0.1:9/mcp"}))
    assert targets == {"s": "http://127.0.0.1:9/mcp"}


async def test_openapi_becomes_an_mcp_server_with_the_registry_names() -> None:
    source = {
        "kind": "openapi",
        "spec": "gateway.pager:SPEC",
        "base_url": "http://pager.invalid",
        "mcp_names": {"getOncall": "oncall_for"},
    }
    [target] = resolve_targets(_registry(source)).values()
    assert isinstance(target, FastMCP)
    async with Client(target) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == {"oncall_for", "createPage", "listPages"}


def test_spec_loads_from_module_attribute_or_json_file(tmp_path: Path) -> None:
    assert load_openapi_spec("gateway.pager:SPEC") is SPEC
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(SPEC))
    assert load_openapi_spec(str(path)) == SPEC


@pytest.mark.parametrize(
    ("ref", "message"),
    [
        pytest.param("gateway.pager:NOPE", "gateway.pager:NOPE", id="missing-attr"),
        pytest.param("gateway.pager:ROTA", "is not an OpenAPI spec", id="not-a-spec"),
        pytest.param("no-colon", "module:ATTR", id="malformed"),
    ],
)
def test_bad_spec_references_are_reported(ref: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load_openapi_spec(ref)
```

`packages/mcp-gateway/tests/test_http.py`:

```python
"""The gateway as one ASGI app, driven in-process over Streamable HTTP (no sockets)."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import mcp_types as mt
import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.utilities.asgi_transport import StreamingASGITransport, run_asgi_lifespan
from mcp import Client as SDKClient
from mcp import MCPError
from mcp.client.session import ClientRequestContext
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import McpHttpClientFactory
from starlette.applications import Starlette

from gateway.app import create_app
from opsdesk.data import OpsDeskStore

pytestmark = pytest.mark.anyio

BASE = "http://gateway.test"
PROTOCOL = "2026-07-28"


def _asgi_client_factory(app: Starlette) -> McpHttpClientFactory:
    # The three named parameters satisfy the McpHttpClientFactory Protocol for pyright;
    # **kwargs is required at runtime because fastmcp also passes follow_redirects=...
    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx2.Timeout | None = None,
        auth: httpx2.Auth | None = None,
        **kwargs: Any,
    ) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=StreamingASGITransport(app),
            base_url=BASE,
            headers=headers,
            timeout=timeout,
            auth=auth,
            **kwargs,
        )

    return factory


@pytest.fixture
async def app(servers: dict[str, FastMCP]) -> AsyncIterator[Starlette]:
    gateway = create_app(servers)
    async with run_asgi_lifespan(gateway):
        yield gateway


@pytest.fixture
async def raw(app: Starlette) -> AsyncIterator[httpx2.AsyncClient]:
    async with httpx2.AsyncClient(transport=StreamingASGITransport(app), base_url=BASE) as client:
        yield client


def _client(app: Starlette, server: str, **kwargs: Any) -> Client[StreamableHttpTransport]:
    transport = StreamableHttpTransport(
        f"{BASE}/{server}/mcp", httpx_client_factory=_asgi_client_factory(app)
    )
    return Client(transport, **kwargs)


async def _confirm(message: str, response_type: Any, params: Any, context: Any) -> dict[str, Any]:
    return {"confirm": True}


async def _sdk_accept(context: ClientRequestContext, params: mt.ElicitRequestParams) -> mt.ElicitResult:
    return mt.ElicitResult(action="accept", content={"confirm": True})


def _payload(response: httpx2.Response) -> dict[str, Any]:
    # A JSON body, or a single SSE `data:` event, depending on how the server chose to answer.
    text = response.text
    body = text.split("data: ", 1)[1].strip() if "data: " in text else text
    return json.loads(body)


def _raw_request(method: str, **params: Any) -> tuple[dict[str, Any], dict[str, str]]:
    meta = {
        "io.modelcontextprotocol/protocolVersion": PROTOCOL,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "raw", "version": "0"},
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {"_meta": meta, **params}}
    headers = {
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": PROTOCOL,
        "mcp-method": method,
    }
    return body, headers


async def test_healthz_lists_the_mounted_servers(raw: httpx2.AsyncClient) -> None:
    response = await raw.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "servers": ["opsdesk", "pager"]}


async def test_both_endpoints_answer(app: Starlette) -> None:
    async with _client(app, "opsdesk") as opsdesk, _client(app, "pager") as pager:
        assert opsdesk.protocol_version == PROTOCOL
        assert "list_incidents" in {t.name for t in await opsdesk.list_tools()}
        oncall = await pager.call_tool("oncall_for", {"service": "search"})
    assert oncall.structured_content is not None
    assert oncall.structured_content["primary"] == "[REDACTED:email]"


async def test_unknown_path_is_404(raw: httpx2.AsyncClient) -> None:
    body, headers = _raw_request("tools/list")
    response = await raw.post("/nope/mcp", json=body, headers=headers)
    assert response.status_code == 404


async def test_raw_2026_request_needs_no_session(raw: httpx2.AsyncClient) -> None:
    body, headers = _raw_request("tools/list")
    response = await raw.post("/opsdesk/mcp", json=body, headers=headers)
    assert response.status_code == 200
    assert "mcp-session-id" not in response.headers
    tools = {t["name"] for t in _payload(response)["result"]["tools"]}
    assert tools == {"list_incidents", "get_service_status", "restart_service", "run_diagnostics"}


async def test_mrtr_restart_with_the_fastmcp_client(app: Starlette, store: OpsDeskStore) -> None:
    async with _client(app, "opsdesk", elicitation_handler=_confirm) as client:
        result = await client.call_tool("restart_service", {"service": "search"})
    assert result.structured_content == {"service": "search", "restarted": True, "state": "healthy"}
    assert store.service("search").restarts == 1


async def test_mrtr_restart_with_the_official_sdk_client(app: Starlette, store: OpsDeskStore) -> None:
    async with (
        httpx2.AsyncClient(transport=StreamingASGITransport(app), base_url=BASE) as http,
        SDKClient(
            streamable_http_client(f"{BASE}/opsdesk/mcp", http_client=http), elicitation_callback=_sdk_accept
        ) as client,
    ):
        assert client.protocol_version == PROTOCOL
        result = await client.call_tool("restart_service", {"service": "api"})
    assert result.structured_content == {"service": "api", "restarted": True, "state": "healthy"}
    assert store.service("api").restarts == 1


async def test_legacy_client_gets_the_confirmation_as_a_push(app: Starlette, store: OpsDeskStore) -> None:
    async with _client(app, "opsdesk", elicitation_handler=_confirm, mode="legacy") as client:
        assert client.protocol_version == "2025-11-25"
        result = await client.call_tool("restart_service", {"service": "search"})
    assert result.structured_content == {"service": "search", "restarted": True, "state": "healthy"}
    assert store.service("search").restarts == 1


# Known gaps of FastMCP 4.1's proxy. These pin today's behaviour so docs/notes/m2a.md cannot
# silently go stale: if one starts failing, the gap closed and the notes need an update.


async def test_known_gap_subscriptions_listen_is_not_proxied(app: Starlette) -> None:
    async with (
        httpx2.AsyncClient(transport=StreamingASGITransport(app), base_url=BASE) as http,
        SDKClient(streamable_http_client(f"{BASE}/opsdesk/mcp", http_client=http)) as client,
    ):
        with pytest.raises(MCPError, match="Method not found"):
            async with client.listen(resource_subscriptions=["runbook://api"]):
                pass


async def test_known_gap_upstream_cache_hints_are_dropped(raw: httpx2.AsyncClient) -> None:
    # OpsDesk itself answers tools/list with ttlMs=60000 and cacheScope "public".
    body, headers = _raw_request("tools/list")
    result = _payload(await raw.post("/opsdesk/mcp", json=body, headers=headers))["result"]
    assert (result["cacheScope"], result["ttlMs"]) == ("private", 0)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest packages/mcp-gateway/tests/test_targets.py packages/mcp-gateway/tests/test_http.py -v`
Expected: 2 ERRORs at collection: `ImportError: cannot import name 'create_app' from 'gateway.app'` and `ImportError: cannot import name 'load_openapi_spec' from 'gateway.app'`.

- [ ] **Step 3: Implement targets and the app**

Replace `packages/mcp-gateway/src/gateway/app.py` with:

`packages/mcp-gateway/src/gateway/app.py`:

```python
"""Assembles the gateway: one registry-governed proxy per backend, mounted at `/<name>/mcp`."""

import importlib
import json
import time
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx2
from fastmcp import FastMCP
from fastmcp.client.transports import ClientTransport, StdioTransport
from fastmcp.server import create_proxy
from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Mount, Route

from gateway.policies import GatewayPolicyMiddleware
from gateway.registry import HttpSource, OpenAPISource, Registry, RegistryHolder, StdioSource
from gateway.visibility import RegistryVisibility

# What `create_proxy` can front: an in-process server, a client transport (stdio) or a URL.
ProxyTarget = FastMCP[Any] | MCPServer | ClientTransport | str


def build_gateway_servers(
    holder: RegistryHolder,
    targets: Mapping[str, ProxyTarget],
    *,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, FastMCP]:
    """One proxy per backend, named `gateway/<name>`, filtered and policed by the registry.

    `create_proxy` (not a hand-built `ProxyClient`, which defaults to the legacy era) makes each
    backend request mirror the front client's protocol era, so MRTR rounds pass straight through.
    """
    servers: dict[str, FastMCP] = {}
    for name, target in targets.items():
        server = create_proxy(target, name=f"gateway/{name}")
        server.add_transform(RegistryVisibility(holder, name))
        server.add_middleware(GatewayPolicyMiddleware(holder, name, clock=clock))
        servers[name] = server
    return servers


def load_openapi_spec(ref: str) -> dict[str, Any]:
    """Load a spec from a `*.json` file path, or from a `package.module:ATTR` dict."""
    if ref.endswith(".json"):
        return json.loads(Path(ref).read_text())
    module_name, sep, attr = ref.partition(":")
    if not sep:
        raise ValueError(f"OpenAPI spec reference {ref!r} is neither a .json path nor module:ATTR")
    try:
        spec: object = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as err:
        raise ValueError(f"Cannot load OpenAPI spec {ref!r}: {err}") from err
    if not isinstance(spec, dict) or "openapi" not in spec:
        raise ValueError(f"{ref!r} is not an OpenAPI spec")
    return cast(dict[str, Any], spec)


def resolve_targets(registry: Registry) -> dict[str, ProxyTarget]:
    """Turn each registry source into something `create_proxy` can front.

    stdio keeps one backend process alive for all callers (`keep_alive=True`): MRTR needs every
    round to reach the process that sealed the `requestState`.
    """
    targets: dict[str, ProxyTarget] = {}
    for name, entry in registry.servers.items():
        source = entry.source
        match source:
            case StdioSource():
                command, *args = source.command
                targets[name] = StdioTransport(command=command, args=args, keep_alive=True)
            case HttpSource():
                targets[name] = source.url
            case OpenAPISource():
                client = httpx2.AsyncClient(base_url=source.base_url, timeout=30)
                targets[name] = FastMCP.from_openapi(
                    load_openapi_spec(source.spec), client=client, name=name, mcp_names=source.mcp_names
                )
    return targets


def create_app(servers: Mapping[str, FastMCP]) -> Starlette:
    """One Starlette app: each server at `/<name>/mcp`, plus `GET /healthz`."""
    apps = {name: server.http_app(path="/mcp") for name, server in servers.items()}

    # Starlette does not run a mounted app's lifespan, and each FastMCP app needs its own to
    # start its session manager. Run them all here, and yield None: fastmcp's combine_lifespans
    # yields a dict, which needs lifespan-state support that in-process ASGI harnesses lack.
    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncGenerator[None]:
        async with AsyncExitStack() as stack:
            for sub_app in apps.values():
                await stack.enter_async_context(sub_app.lifespan(sub_app))
            yield

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "servers": list(apps)})

    routes: list[BaseRoute] = [Route("/healthz", healthz, methods=["GET"])]
    routes += [Mount(f"/{name}", app=sub_app) for name, sub_app in apps.items()]
    return Starlette(routes=routes, lifespan=lifespan)
```

- [ ] **Step 4: Run tests, lint and types**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run pyright`
Expected: `115 passed` (the 47 M1 tests plus the gateway's), `All checks passed!`, `29 files already formatted`, `0 errors, 0 warnings, 0 informations`.

- [ ] **Step 5: Commit**

```bash
git add packages/mcp-gateway/src/gateway/app.py packages/mcp-gateway/tests/test_http.py packages/mcp-gateway/tests/test_targets.py
git commit -m "feat(gateway): resolve registry sources and serve every backend from one ASGI app

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: `mcp-gateway` CLI, the demo registry, and an end-to-end test

**Files:**
- Create: `packages/mcp-gateway/src/gateway/__main__.py`, `packages/mcp-gateway/registry.yaml`
- Test: `packages/mcp-gateway/tests/test_cli.py`

**Interfaces:**
- Consumes: everything above. `test_audit_of_the_demo_registry` launches `uv run opsdesk-server` (needs `uv` on `PATH`); the other subprocess tests use `sys.executable -m opsdesk` / `-m gateway`.
- Produces:
  - `gateway.__main__.serve(registry_path: Path, host: str, port: int) -> None`: binds first, then prints `Gateway listening on http://H:P` and one `  <name>: http://H:P/<name>/mcp` line per server to stderr, then runs uvicorn on the bound socket.
  - `async audit(registry_path: Path) -> None`: per server, prints `<name> (owner: <owner>): pending review: <tools>` for backend tools the registry does not list, `... every tool is reviewed`, or `... unreachable: <error>`; it never stops at the first unreachable backend.
  - `main(argv: list[str] | None = None) -> None`: `mcp-gateway serve --registry PATH [--host 127.0.0.1] [--port 8080]` and `mcp-gateway audit --registry PATH`; a `RegistryError` becomes `SystemExit("mcp-gateway: <error>")`.
  - `packages/mcp-gateway/registry.yaml`: opsdesk via stdio `uv run opsdesk-server` (`update_runbook` listed but disabled, `run_diagnostics` deliberately unlisted), pager via openapi `gateway.pager:SPEC` at `http://127.0.0.1:8100` (all three tools enabled).

- [ ] **Step 1: Write the failing tests**

`packages/mcp-gateway/tests/test_cli.py`:

```python
"""The `mcp-gateway` CLI: `audit` in-process, and `serve` end to end as a real subprocess."""

import re
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import httpx2
import mcp_types as mt
import pytest
import yaml
from mcp import Client
from mcp.client.session import ClientRequestContext

from gateway.__main__ import main
from gateway.registry import load_registry

Announced = Callable[..., AbstractContextManager[re.Match[str]]]
WriteRegistry = Callable[[Any], Path]

DEMO_REGISTRY = Path(__file__).resolve().parents[1] / "registry.yaml"
OPSDESK_STDIO = [sys.executable, "-m", "opsdesk"]


async def _accept(context: ClientRequestContext, params: mt.ElicitRequestParams) -> mt.ElicitResult:
    return mt.ElicitResult(action="accept", content={"confirm": True})


def _stdio_registry(registry_data: dict[str, Any]) -> dict[str, Any]:
    """The test registry with OpsDesk launched from this interpreter and the Pager dropped."""
    opsdesk = registry_data["servers"]["opsdesk"]
    opsdesk["source"]["command"] = OPSDESK_STDIO
    return {"servers": {"opsdesk": opsdesk}}


def test_demo_registry_matches_the_walkthrough() -> None:
    registry = load_registry(DEMO_REGISTRY)
    opsdesk, pager = registry.servers["opsdesk"], registry.servers["pager"]
    assert opsdesk.source.kind == "stdio"
    assert opsdesk.tools["update_runbook"].enabled is False
    assert "run_diagnostics" not in opsdesk.tools
    assert pager.source.kind == "openapi"
    assert pager.is_enabled("send_page")


# capfd, not capsys: the stdio backend inherits stderr, which needs a real file descriptor.
def test_audit_reports_unlisted_backend_tools(
    write_registry: WriteRegistry, registry_data: dict[str, Any], capfd: pytest.CaptureFixture[str]
) -> None:
    data = _stdio_registry(registry_data)
    del data["servers"]["opsdesk"]["tools"]["run_diagnostics"]
    data["servers"]["down"] = {"owner": "nobody", "source": {"kind": "http", "url": "http://127.0.0.1:9/mcp"}}
    main(["audit", "--registry", str(write_registry(data))])
    lines = capfd.readouterr().out.splitlines()
    assert "opsdesk (owner: oncall-team): pending review: run_diagnostics" in lines
    assert any(re.fullmatch(r"down \(owner: nobody\): unreachable: .+", line) for line in lines)


def test_audit_of_the_demo_registry(capfd: pytest.CaptureFixture[str]) -> None:
    main(["audit", "--registry", str(DEMO_REGISTRY)])
    assert capfd.readouterr().out.splitlines() == [
        "opsdesk (owner: oncall-team): pending review: run_diagnostics",
        "pager (owner: sre): every tool is reviewed",
    ]


@pytest.mark.anyio
async def test_serve_end_to_end_over_a_stdio_backend(
    announced: Announced, write_registry: WriteRegistry, registry_data: dict[str, Any]
) -> None:
    path = write_registry(_stdio_registry(registry_data))
    command = [sys.executable, "-m", "gateway", "serve", "--registry", str(path), "--port", "0"]
    with announced(command, r"Gateway listening on (http://\S+)\n\s+opsdesk: (http://\S+/opsdesk/mcp)") as m:
        base, url = m.group(1), m.group(2)
        health = httpx2.get(f"{base}/healthz")
        async with Client(url, elicitation_callback=_accept) as client:
            assert client.protocol_version == "2026-07-28"
            tools = {t.name for t in (await client.list_tools()).tools}
            first = await client.call_tool("restart_service", {"service": "search"})
            again = await client.call_tool("get_service_status", {"service": "search"})
    assert health.json() == {"status": "ok", "servers": ["opsdesk"]}
    assert tools == {"list_incidents", "get_service_status", "restart_service", "run_diagnostics"}
    assert first.structured_content == {"service": "search", "restarted": True, "state": "healthy"}
    # One backend process serves every round and every call (stdio keep_alive=True).
    assert again.structured_content == {"name": "search", "state": "healthy", "restarts": 1}


def test_serve_refuses_an_invalid_registry(tmp_path: Path) -> None:
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump({"servers": {"x": {"owner": "t"}}}))
    with pytest.raises(SystemExit, match=r"registry\.yaml"):
        main(["serve", "--registry", str(path), "--port", "0"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest packages/mcp-gateway/tests/test_cli.py -v`
Expected: ERROR at collection with `ModuleNotFoundError: No module named 'gateway.__main__'`.

- [ ] **Step 3: Write the demo registry**

`packages/mcp-gateway/registry.yaml`:

```yaml
# The gateway's reviewed registry (the control plane).
# A tool that is not listed here is hidden, and a listed tool stays disabled unless it says
# `enabled: true`: enabling a tool is a reviewed edit to this file.
# Tool enablement, owners and rate limits reload live; adding or removing a server needs a restart.
servers:
  opsdesk:
    owner: oncall-team
    source: {kind: stdio, command: [uv, run, opsdesk-server]}
    rate_limit: {calls_per_minute: 30}
    tools:
      list_incidents: {enabled: true, owner: oncall-team}
      get_service_status: {enabled: true, owner: oncall-team}
      restart_service: {enabled: true, owner: oncall-team}
      update_runbook: {enabled: false, owner: oncall-team}
      # run_diagnostics is deliberately unlisted: `mcp-gateway audit` reports it as pending review.
  pager:
    owner: sre
    source:
      kind: openapi
      spec: gateway.pager:SPEC
      base_url: http://127.0.0.1:8100
      mcp_names: {getOncall: oncall_for, createPage: send_page, listPages: list_pages}
    rate_limit: {calls_per_minute: 10}
    tools:
      oncall_for: {enabled: true, owner: sre}
      send_page: {enabled: true, owner: sre}
      list_pages: {enabled: true, owner: sre}
```

- [ ] **Step 4: Implement the CLI**

`packages/mcp-gateway/src/gateway/__main__.py`:

```python
"""mcp-gateway: serve every registered MCP server behind one endpoint, or audit the registry.

mcp-gateway serve --registry registry.yaml [--host 127.0.0.1] [--port 8080]
mcp-gateway audit --registry registry.yaml
"""

import argparse
import logging
import socket
import sys
from pathlib import Path

import anyio
import uvicorn
from fastmcp import Client

from gateway.app import build_gateway_servers, create_app, resolve_targets
from gateway.registry import RegistryError, RegistryHolder, load_registry


def serve(registry_path: Path, host: str, port: int) -> None:
    holder = RegistryHolder(registry_path)
    app = create_app(build_gateway_servers(holder, resolve_targets(holder.current)))
    # Bind before announcing, so `--port 0` reports the real port and nobody races for it.
    sock = socket.create_server((host, port))
    base = f"http://{host}:{sock.getsockname()[1]}"
    lines = [
        f"Gateway listening on {base}",
        *(f"  {name}: {base}/{name}/mcp" for name in holder.current.servers),
    ]
    print("\n".join(lines), file=sys.stderr, flush=True)
    uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[sock])


async def audit(registry_path: Path) -> None:
    """Print, per server, the backend tools the registry does not list: they await review."""
    registry = load_registry(registry_path)
    for name, target in resolve_targets(registry).items():
        entry = registry.servers[name]
        label = f"{name} (owner: {entry.owner})"
        try:
            async with Client(target) as client:
                offered = [tool.name for tool in await client.list_tools()]
        except Exception as err:  # one unreachable backend must not hide the others' findings
            print(f"{label}: unreachable: {err}")
            continue
        pending = [tool for tool in offered if tool not in entry.tools]
        if pending:
            print(f"{label}: pending review: {', '.join(pending)}")
        else:
            print(f"{label}: every tool is reviewed")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="mcp-gateway", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve_cmd = commands.add_parser("serve", help="serve every registered server at /<name>/mcp")
    serve_cmd.add_argument("--registry", type=Path, required=True)
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8080, help="0 picks a free port")
    audit_cmd = commands.add_parser("audit", help="list backend tools the registry does not cover")
    audit_cmd.add_argument("--registry", type=Path, required=True)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        if args.command == "serve":
            serve(args.registry, args.host, args.port)
        else:
            anyio.run(audit, args.registry)
    except RegistryError as err:
        raise SystemExit(f"mcp-gateway: {err}") from err


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run tests, lint and types**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run pyright`
Expected: `120 passed` (the 47 M1 tests plus the gateway's), `All checks passed!`, `31 files already formatted`, `0 errors, 0 warnings, 0 informations`. The `test_cli.py` subprocess tests take a few seconds.

- [ ] **Step 6: Commit**

```bash
git add packages/mcp-gateway/registry.yaml packages/mcp-gateway/src/gateway/__main__.py packages/mcp-gateway/tests/test_cli.py
git commit -m "feat(gateway): mcp-gateway serve/audit CLI, demo registry and an end-to-end test

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: README, M2a notes, and final verification

**Files:**
- Modify: `README.md`
- Create: `docs/notes/m2a.md`

**Interfaces:**
- Consumes: everything above.
- Produces: the documentation. The controller tags `m2a` after review; this task does not tag and does not push.

- [ ] **Step 1: Run the documented commands**

Every command in the README section below must be run as written, from the repo root, with ports 8100 and 8080 free:

```bash
uv run python -m gateway.pager --port 8100                                          # terminal 1
uv run mcp-gateway serve --registry packages/mcp-gateway/registry.yaml --port 8080   # terminal 2
curl -s http://127.0.0.1:8080/healthz
uv run mcp-gateway audit --registry packages/mcp-gateway/registry.yaml
```

Expected: the Pager prints `Pager listening on http://127.0.0.1:8100`. The gateway prints `Gateway listening on http://127.0.0.1:8080`, `  opsdesk: http://127.0.0.1:8080/opsdesk/mcp` and `  pager: http://127.0.0.1:8080/pager/mcp`. `/healthz` returns `{"status":"ok","servers":["opsdesk","pager"]}`, and `audit` prints the two lines shown in the README. Run the Python snippet from the README and check its two output lines. Then replace the `# run_diagnostics is deliberately unlisted` comment in `registry.yaml` with `run_diagnostics: {enabled: true, owner: oncall-team}`, run the snippet again, and check that `run_diagnostics` is now listed and the gateway logged `INFO gateway.registry: registry packages/mcp-gateway/registry.yaml reloaded`. Restore `registry.yaml` (`git checkout packages/mcp-gateway/registry.yaml`) and stop both servers.

- [ ] **Step 2: Update `README.md`**

In the milestone table, replace the row

````markdown
| M2 MCP gateway | planned | Registry (tools off by default), proxy, OAuth (CIMD), PII redaction, Omni discovery, OTel |
````

with

````markdown
| M2a MCP gateway core | ✅ `m2a` | FastMCP 4.1 proxy per backend, YAML registry (tools off by default, hot reload), PII redaction, per-caller rate limits, OpenAPI backend |
| M2b MCP gateway auth | planned | OAuth 2.1 with a local auth server, CIMD clients, `iss` validation, per-server/per-tool access, token-subject callers |
| M2c MCP gateway scale & observability | planned | Omni discovery meta-tools, response projection, OTel traces to Jaeger |
````

Insert this section directly above `## Test pyramid`:

````markdown
## Gateway

`packages/mcp-gateway` puts every registered MCP server behind one endpoint, `/<name>/mcp`, built on
FastMCP 4.1's proxy. `packages/mcp-gateway/registry.yaml` is the control plane: a tool the registry
does not list is hidden, and a listed tool stays disabled unless it says `enabled: true`. The demo
registry fronts OpsDesk (stdio) and Pager, a small OpenAPI HTTP API whose on-call contacts are e-mail
addresses that the gateway redacts.

```bash
uv run python -m gateway.pager --port 8100                                          # terminal 1: Pager API
uv run mcp-gateway serve --registry packages/mcp-gateway/registry.yaml --port 8080   # terminal 2
curl -s http://127.0.0.1:8080/healthz          # {"status":"ok","servers":["opsdesk","pager"]}
uv run mcp-gateway audit --registry packages/mcp-gateway/registry.yaml
```

`serve` prints one endpoint per server (`http://127.0.0.1:8080/opsdesk/mcp`, `.../pager/mcp`).
`audit` connects to each backend and lists the tools the registry does not cover yet:

```
opsdesk (owner: oncall-team): pending review: run_diagnostics
pager (owner: sre): every tool is reviewed
```

Call it like any MCP server:

```bash
uv run python - <<'EOF'
import anyio
from mcp import Client


async def main() -> None:
    async with Client("http://127.0.0.1:8080/opsdesk/mcp") as opsdesk:
        print(sorted(t.name for t in (await opsdesk.list_tools()).tools))
    async with Client("http://127.0.0.1:8080/pager/mcp") as pager:
        print((await pager.call_tool("oncall_for", {"service": "api"})).structured_content)


anyio.run(main)
EOF
```

```
['get_service_status', 'list_incidents', 'restart_service']
{'service': 'api', 'primary': '[REDACTED:email]', 'secondary': '[REDACTED:email]'}
```

Registry edits apply on the next request, with no restart: replace the `# run_diagnostics is
deliberately unlisted` comment with `run_diagnostics: {enabled: true, owner: oncall-team}` and run
the snippet again; `run_diagnostics` is now listed. Adding or removing a *server* needs a restart.
See [`docs/notes/m2a.md`](docs/notes/m2a.md) for the design and its known gaps.

````

In the test pyramid table, add this row after the `Transport` row:

````markdown
| Gateway | `uv run pytest packages/mcp-gateway` | registry, redaction, rate limits, MRTR and progress through the proxy, in-process HTTP, a `serve` subprocess end to end |
````

- [ ] **Step 3: Write `docs/notes/m2a.md`**

`docs/notes/m2a.md`:

```markdown
# M2a notes: the gateway core

## The one-line version
One endpoint per backend (`/<name>/mcp`), each a FastMCP 4.1 proxy. A reviewed YAML registry decides
which tools exist, and middleware redacts e-mail addresses and rate-limits each caller.

## Control plane vs data plane
- **Control plane:** `registry.yaml`, parsed into pydantic models by `gateway.registry`. It records
  who owns each server and tool, which tools are enabled, and each server's rate limit. Unknown keys
  are rejected (`extra="forbid"`), so a typo such as `tols:` fails review instead of silently hiding
  everything → `test_invalid_entries_are_rejected`.
- **Data plane:** `create_proxy(target)` per backend, plus a `RegistryVisibility` transform and a
  `GatewayPolicyMiddleware`, all mounted in one Starlette app (`gateway.app`). The data plane only
  *reads* the control plane: `RegistryHolder.current`.
- **`mcp-gateway audit`** connects to every backend and prints the tools the registry does not list
  ("pending review"). That is how a newly shipped tool reaches a reviewer → `test_audit_reports_unlisted_backend_tools`.

## Disabled by default
A tool the registry does not list is hidden, and a listed tool is disabled unless it says
`enabled: true`. Enabling a tool is a reviewed edit to the YAML. A hidden tool is absent from
`tools/list`, and calling it returns the same `Unknown tool: '<name>'` tool error as a tool that does
not exist, so a caller cannot probe for it → `test_disabled_tool_is_not_callable`,
`test_unlisted_tool_is_hidden_and_unknown`. A server removed from the YAML exposes nothing until the
restart that unmounts it → `test_server_removed_from_registry_exposes_nothing_until_restart`.

Only tools are governed. Resources and prompts pass through (redacted).

## Why hot reload goes through a transform
- `RegistryHolder.refresh()` reloads when the file's `st_mtime_ns` changes. If the new YAML is
  invalid, it keeps the last good registry, logs the error and records it in `.last_error`
  → `test_invalid_reload_keeps_last_good_registry`.
- The middleware calls `refresh()` at the start of every request → `test_each_request_refreshes_the_registry`.
- `RegistryVisibility` reads `holder.current` on every `list_tools` / `get_tool`, so a reload needs
  nothing rebuilt or mutated → `test_hot_reload_flips_visibility_without_rebuild`.
- The alternative, `FastMCP.enable()` / `disable()`, only *appends* `Visibility` transforms, and there
  is no API to remove one. Toggling a tool ten times would stack ten transforms.
- A transform rather than middleware: a transform also hides the tool from `get_tool`, which is what
  the call path and any catalog/search transform use. Middleware would only filter the wire methods.
- Live: tool enablement, owners and rate limits. Restart: adding or removing servers, because each
  server is a mounted sub-app with its own lifespan, built at startup.

## MRTR through a proxy
- `create_proxy` makes each backend request **mirror the front client's era**. A hand-built
  `ProxyClient` defaults to the legacy era, and then a 2026-07-28 front breaks: the backend tries to
  push `elicitation/create` with no back-channel. Mutating the gateway to pin `mode="legacy"` turns
  `test_mrtr_confirmation_crosses_the_gateway` red; pinning `"2026-07-28"` breaks the legacy front
  (`test_legacy_client_gets_the_confirmation_as_a_push`).
- **The stateless front drives the rounds.** On a 2026-07-28 front, the backend's
  `InputRequiredResult` goes back to the client as-is, and the client retries the *whole* `tools/call`
  with `inputResponses` and `requestState`. The proxy keeps nothing in memory between rounds: what
  the backend needs to resume travels inside the sealed `requestState`. A legacy front gets a pushed
  `elicitation/create` instead.
- Both clients work: the fastmcp `Client` (`test_mrtr_restart_with_the_fastmcp_client`) and the
  official `mcp.Client` (`test_mrtr_restart_with_the_official_sdk_client`), the latter over
  in-process Streamable HTTP, which also avoids touching fastmcp's private `_mcp_server`.
- **requestState is sealed twice.** The backend seals its state; the gateway's front (FastMCP
  installs the SDK's `RequestStateBoundary`) seals that again with a per-process ephemeral key.
  A forged `requestState` is rejected at the gateway before any middleware runs
  → `test_forged_request_state_is_rejected_before_it_reaches_the_backend`. Consequence: several
  gateway replicas need a shared `request_state_security` key, or a retry that lands on another
  replica fails with `Invalid or expired requestState`.
- **stdio needs `keep_alive=True`**: the backend's key is per process, so every round must reach the
  same process (`resolve_targets` sets it explicitly). The price: all callers share one backend
  process and its state → `test_stdio_becomes_a_kept_alive_stdio_transport`, and the e2e test sees
  `restarts: 1` on a second call → `test_serve_end_to_end_over_a_stdio_backend`.

## Rate limits and MRTR rounds
- Fixed one-minute windows per caller (`clientInfo.name`) and per server, `calls_per_minute` read
  live from the registry → `test_rate_limit_changes_apply_live`.
- On a 2026-07-28 front, **every MRTR round is a separate `tools/call` through the middleware**, so a
  confirmed restart would count twice. The middleware counts only a call without `requestState`
  → `test_confirmed_restart_counts_as_one_call`. That is safe only because the gateway seals the
  state itself (above): a client cannot skip counting by inventing a retry.
- Over the limit, the call fails as a tool error (`isError: true`) naming the caller, the server
  and the limit, so the model sees why. FastMCP's built-in `RateLimitingMiddleware` was not used: it
  hooks every request (discover, lists, each MRTR round) and raises a JSON-RPC error (-32000).
- Calls to hidden or unknown tools count too: the limit applies before the tool is resolved
  (checked with a probe, not pinned by a test).
- Counters live in process memory: they reset on restart and are not shared between replicas.
- Caller identity is self-asserted `clientInfo.name`. **M2b** replaces it with the subject of a
  verified OAuth token.

## Redaction scope
- `redact()` replaces e-mail addresses with `[REDACTED:email]` in strings, dict values and list items.
- Applied to tool results (text content *and* `structuredContent`, so the client's typed `.data` is
  redacted too), resource reads (text) and prompt messages (text) → `test_policies.py`.
- Two backends: OpsDesk (MCP over stdio) and Pager (OpenAPI over HTTP)
  → `test_openapi_backend_results_are_redacted`.
- `InputRequired*` results pass through untouched. Their content is always empty, and rebuilding
  them as a plain result would swallow the question → `test_input_required_round_passes_through_untouched`.
- Not redacted: image/audio/embedded-resource blocks, binary resource contents, dict keys, tool and
  prompt descriptions, error messages raised as JSON-RPC errors, and anything the client *sends*.

## Known gaps (each pinned by a characterization test)
- **`subscriptions/listen` is not proxied.** FastMCP 4.1's low-level server registers no handler, so
  the client gets `Method not found`, and OpsDesk's runbook-updated events never reach it
  → `test_known_gap_subscriptions_listen_is_not_proxied`. A workaround exists (register the SDK's
  `ListenHandler` on the private `_mcp_server` and bridge an upstream listen); mutating the gateway
  that way turns the test red.
- **Upstream cache hints are dropped.** OpsDesk answers `tools/list` with `ttlMs: 60000, public`;
  the gateway answers `ttlMs: 0, private` → `test_known_gap_upstream_cache_hints_are_dropped`. The
  gateway could declare its *own* hint (`create_proxy(..., cache_ttl=60, cache_scope="public")`;
  that mutation turns the test red), but it cannot forward the backend's.
- **stdio shares one backend process** across every caller (see MRTR above).
- If one of these tests fails after an upgrade, the gap closed: update this file.

## FastMCP 4.1 gotchas found while building
- Use `create_proxy`, not a hand-built `ProxyClient` (legacy era by default).
- Starlette does not run mounted apps' lifespans: the parent must enter each `http_app().lifespan`,
  or every request returns 500 (`Task group is not initialized`). Yield `None`, not a dict:
  `combine_lifespans` yields `{}`, which in-process ASGI harnesses reject.
- An `httpx_client_factory` needs the three Protocol parameters *and* `**kwargs` (fastmcp passes
  `follow_redirects`).
- A stdio backend inherits stderr; under pytest use `capfd`, not `capsys` (`fileno` error).

## Interview questions to rehearse
1. Why is "unknown tool" the right answer for a disabled tool, rather than "forbidden"?
2. Walk an MRTR call through the gateway. Who holds state between rounds, and what breaks with two
   gateway replicas?
3. Why count only the first MRTR round, and why can't a client abuse that?
4. A transform vs middleware vs `disable()` for hot reload: what does each cost?
5. What does the gateway's `cacheScope: private` cost clients, and when would `public` be wrong?
```

- [ ] **Step 4: Run the full verification**

Run: `uv sync --locked && uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q -k transport`
Expected: `120 passed`, `All checks passed!`, `31 files already formatted`, `0 errors, 0 warnings, 0 informations`, and `13 passed, 107 deselected` for the transport subset. `uv lock --check` passes. `.github/workflows/ci.yml` needs no change (see File Structure).

- [ ] **Step 5: Commit**

```bash
git add README.md docs/notes/m2a.md
git commit -m "docs: M2a gateway README section and notes

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

Do not tag and do not push: the controller reviews the branch and tags `m2a`.

## Corrections (2026-10-29)

The architecture summary says the rate limit "counts only the first MRTR round", and the gotcha list says to count only calls without `request_state`; the code does neither. `GatewayPolicyMiddleware.on_call_tool` reserves a slot on every round before forwarding it and hands the slot back only when the backend answers with an `InputRequiredToolResult`, so a confirmation exchange counts once and a replayed `requestState` still counts. `docs/notes/m2a.md`, section "Rate limits and MRTR rounds", explains why and names the tests.
