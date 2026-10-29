"""The gateway's control plane: a reviewed YAML registry of servers and the tools they may expose.

A tool the registry does not list is hidden, and a listed tool is disabled unless it says
`enabled: true`. Enabling a tool is therefore a reviewed edit to the YAML file.

`RegistryHolder.refresh()` reloads the file when its mtime changes, so tool enablement, owners
and rate limits change live. The set of servers and their sources do not: each server is a mounted
proxy built at startup, so adding or removing a server, or changing its `source`, needs a gateway
restart. A reload that makes such an edit logs a warning naming the servers.
"""

import hashlib
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
    owner: str  # every listed tool names who reviews it, as every server does


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
    while a later duplicate of the same key says `true`. That includes a mapping reached only as a
    `<<` value: the base class copies its pairs into the consumer and never constructs it alone.
    """

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        # Every mapping's keys as written, taken on its first flatten. Flattening rewrites
        # `node.value` to hold the merged pairs too, and an explicit key may override a merged one.
        self._keys_as_written: dict[yaml.MappingNode, list[yaml.Node]] = {}
        self._unchecked: list[yaml.MappingNode] = []  # recorded by the flatten in progress

    def flatten_mapping(self, node: yaml.MappingNode) -> None:
        # The base class calls this for the mapping being built and, recursively, for every mapping
        # it merges, so each one is recorded here before any of them is rewritten.
        if node not in self._keys_as_written:
            self._keys_as_written[node] = [key for key, _ in node.value if key.tag != _MERGE_TAG]
            self._unchecked.append(node)
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
        # The base class flattens `node` and then constructs every key in the flattened mapping,
        # merged ones included, so each mapping that this flatten recorded has its keys built.
        outer, self._unchecked = self._unchecked, []
        try:
            mapping = super().construct_mapping(node, deep=deep)
            recorded = self._unchecked
        finally:
            self._unchecked = outer
        for checked in recorded:
            self._reject_duplicate_keys(checked, self._keys_as_written[checked])
        return mapping

    def _reject_duplicate_keys(self, node: yaml.MappingNode, key_nodes: list[yaml.Node]) -> None:
        seen: set[object] = set()
        for key_node in key_nodes:
            key = self.constructed_objects[key_node]
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            seen.add(key)


def load_registry(path: Path) -> Registry:
    try:
        raw = path.read_bytes()
    except OSError as err:
        raise RegistryError(f"{path}: {err}") from err
    return _parse_registry(path, raw)


def _parse_registry(path: Path, raw: bytes) -> Registry:
    try:
        data = yaml.load(raw.decode("utf-8"), Loader=_UniqueKeyLoader)
        return Registry.model_validate(data)
    except (UnicodeDecodeError, yaml.YAMLError, ValidationError) as err:
        raise RegistryError(f"{path}: {err}") from err


class RegistryHolder:
    """Holds the last good registry and reloads it when the file's `st_mtime_ns` changes.

    Live: tool enablement, owners and rate limits. Restart required: adding or removing servers,
    or changing a server's `source`. The servers and sources of the first load are what is mounted.

    The mtime is recorded only once a load succeeds. A file read mid-write can be broken and then
    finish under the same mtime, so while an error is pending the file is read again whenever its
    stat (mtime and size) differs from the content last read and not installed; the same stat is
    not read again, since `refresh()` runs in the event loop on every request. A read that fails
    to open the file is the exception: it is retried on every request, since the fix (a permission
    or the file put back) changes neither mtime nor size. The digest of the
    rejected bytes is kept too, so the same broken bytes are never parsed or logged twice. While an
    error is pending, a file whose stat and bytes match the last good load again clears the error
    without installing anything.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self._mtime_ns = path.stat().st_mtime_ns  # stat first: a write after this is seen later
        except OSError as err:
            raise RegistryError(f"{path}: {err}") from err
        try:
            raw = path.read_bytes()
        except OSError as err:
            raise RegistryError(f"{path}: {err}") from err
        self.current: Registry = _parse_registry(path, raw)
        self.last_error: str | None = None
        self._loaded_digest = hashlib.sha256(raw).digest()  # sha256 of the bytes `current` came from
        self._rejected_digest: bytes | None = None  # sha256 of the file content that last failed to load
        # (mtime_ns, size) of the content last read and not installed: not read again until it changes.
        self._rejected_stat: tuple[int, int] | None = None
        self._mounted = {name: entry.source for name, entry in self.current.servers.items()}

    def refresh(self) -> bool:
        """Reload if the file changed. Returns True only when a new registry was installed."""
        try:
            stat = self.path.stat()  # stat first: a write after this is seen later
            mtime_ns = stat.st_mtime_ns
            if mtime_ns == self._mtime_ns and self.last_error is None:
                return False
            if (mtime_ns, stat.st_size) == self._rejected_stat:  # already read, not installed
                return False
            raw = self.path.read_bytes()
        except OSError as err:
            self._rejected_stat = None
            self._rejected_digest = None
            return self._reject(str(err))
        self._rejected_stat = (mtime_ns, stat.st_size)
        digest = hashlib.sha256(raw).digest()
        if mtime_ns == self._mtime_ns:
            # An error is pending, and the stat matches the last good load: if the good bytes are
            # back (`cp -p`, an editor's backup), the running registry is right and the error is stale.
            # Other content under that mtime is ignored, as it is when no error is pending.
            if digest == self._loaded_digest:
                self._rejected_stat = None
                self._rejected_digest = None
                self.last_error = None
                logger.info("registry %s matches the running registry again", self.path)
            return False
        if digest == self._rejected_digest:  # the same broken file: already parsed and logged
            return False
        try:
            registry = _parse_registry(self.path, raw)
        except RegistryError as err:
            self._rejected_digest = digest
            return self._reject(str(err))
        self._mtime_ns = mtime_ns
        self._loaded_digest = digest
        self._rejected_stat = None
        self._rejected_digest = None
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
