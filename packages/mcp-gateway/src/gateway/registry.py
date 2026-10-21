"""The gateway's control plane: a reviewed YAML registry of servers and the tools they may expose.

A tool the registry does not list is hidden, and a listed tool is disabled unless it says
`enabled: true`. Enabling a tool is therefore a reviewed edit to the YAML file.

`RegistryHolder.refresh()` reloads the file when its mtime changes, so tool enablement, owners
and rate limits change live. The set of servers and their sources do not: each server is a mounted
proxy built at startup, so adding or removing a server, or changing its `source`, needs a gateway
restart. A reload that makes such an edit logs a warning naming the servers.
"""

import logging
from collections.abc import Hashable
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

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


# A server name becomes the mount path `/<name>/mcp`, so it is limited to lowercase URL-safe characters.
ServerName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]*$")]


class Registry(_Strict):
    servers: dict[ServerName, ServerEntry]


class RegistryError(ValueError):
    """The registry file could not be read, parsed or validated."""


_MERGE_TAG = "tag:yaml.org,2002:merge"


class _UniqueKeyLoader(yaml.SafeLoader):
    """A `SafeLoader` that rejects a mapping key repeated at any level.

    `yaml.safe_load` lets the last duplicate win, so a reviewer could approve `enabled: false`
    while a later duplicate of the same key says `true`.
    """

    def flatten_mapping(self, node: yaml.MappingNode) -> None:
        # Count `<<` here, before the base class strips them: flattening a mapping that merges an
        # anchor removes the anchor's own `<<` pairs before the anchor itself is constructed.
        merge_nodes = [key_node for key_node, _ in node.value if key_node.tag == _MERGE_TAG]
        if len(merge_nodes) > 1:  # the base class would merge both, one silently overriding the other
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found duplicate key '<<'",
                merge_nodes[1].start_mark,
            )
        super().flatten_mapping(node)

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        # The keys as written: the base class rewrites `node.value` when it expands `<<` merges.
        key_nodes = [key_node for key_node, _ in node.value]
        mapping = super().construct_mapping(node, deep=deep)
        seen: set[object] = set()
        for key_node in key_nodes:
            if key_node not in self.constructed_objects:  # a `<<` merge key, consumed by the merge
                continue
            key = self.constructed_objects[key_node]
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            seen.add(key)
        return mapping


def load_registry(path: Path) -> Registry:
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
        return Registry.model_validate(data)
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValidationError) as err:
        raise RegistryError(f"{path}: {err}") from err


class RegistryHolder:
    """Holds the last good registry and reloads it when the file's `st_mtime_ns` changes.

    Live: tool enablement, owners and rate limits. Restart required: adding or removing servers,
    or changing a server's `source`. The servers and sources of the first load are what is mounted.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self._mtime_ns = path.stat().st_mtime_ns  # stat first: a write after this is seen later
        except OSError as err:
            raise RegistryError(f"{path}: {err}") from err
        self.current: Registry = load_registry(path)
        self.last_error: str | None = None
        self._mounted = {name: entry.source for name, entry in self.current.servers.items()}

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
        self._warn_about_restart_only_edits(registry)
        return True

    def _warn_about_restart_only_edits(self, registry: Registry) -> None:
        """Log a warning when the reload adds, removes or re-sources a server: those need a restart."""
        loaded = {name: entry.source for name, entry in registry.servers.items()}
        changes = {
            "source changed": [n for n in loaded if n in self._mounted and loaded[n] != self._mounted[n]],
            "added": [n for n in loaded if n not in self._mounted],
            "removed": [n for n in self._mounted if n not in loaded],
        }
        found = "; ".join(f"{kind}: {', '.join(names)}" for kind, names in changes.items() if names)
        if found:
            logger.warning("registry %s: these edits take effect only after a restart (%s)", self.path, found)

    def _reject(self, error: str) -> bool:
        if error != self.last_error:
            logger.error("registry reload failed, keeping the last good registry: %s", error)
        self.last_error = error
        return False
