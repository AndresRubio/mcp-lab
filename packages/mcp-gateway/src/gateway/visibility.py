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
