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
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return Registry.model_validate(data)
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValidationError) as err:
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
