"""Registry-driven visibility, applied as a FastMCP transform on each backend's proxy."""

from collections.abc import Sequence

from fastmcp.prompts import Prompt
from fastmcp.resources import Resource, ResourceTemplate
from fastmcp.server.transforms import (
    GetPromptNext,
    GetResourceNext,
    GetResourceTemplateNext,
    GetToolNext,
    Transform,
)
from fastmcp.tools import Tool
from fastmcp.utilities.versions import VersionSpec

from gateway.registry import RegistryHolder


class RegistryVisibility(Transform):
    """Lists and resolves only the tools the registry enables for `server_name`.

    It reads `holder.current` on every list and get, so a registry reload takes effect on the
    next request with nothing to rebuild or mutate. (`FastMCP.enable()` / `disable()` would only
    *append* transforms, with no way to remove one.) A hidden tool resolves to None, so calling
    it fails exactly like a tool that does not exist.

    Resources, resource templates and prompts are ungoverned while the server is listed. Once the
    registry drops the server they are hidden too, so its proxy exposes nothing until the restart
    that unmounts it.
    """

    def __init__(self, holder: RegistryHolder, server_name: str) -> None:
        self.holder = holder
        self.server_name = server_name

    def __repr__(self) -> str:
        return f"RegistryVisibility(server_name={self.server_name!r})"

    def _listed(self) -> bool:
        return self.server_name in self.holder.current.servers

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

    async def list_resources(self, resources: Sequence[Resource]) -> Sequence[Resource]:
        return resources if self._listed() else []

    async def get_resource(
        self, uri: str, call_next: GetResourceNext, *, version: VersionSpec | None = None
    ) -> Resource | None:
        if not self._listed():
            return None
        return await call_next(uri, version=version)

    async def list_resource_templates(
        self, templates: Sequence[ResourceTemplate]
    ) -> Sequence[ResourceTemplate]:
        return templates if self._listed() else []

    async def get_resource_template(
        self, uri: str, call_next: GetResourceTemplateNext, *, version: VersionSpec | None = None
    ) -> ResourceTemplate | None:
        if not self._listed():
            return None
        return await call_next(uri, version=version)

    async def list_prompts(self, prompts: Sequence[Prompt]) -> Sequence[Prompt]:
        return prompts if self._listed() else []

    async def get_prompt(
        self, name: str, call_next: GetPromptNext, *, version: VersionSpec | None = None
    ) -> Prompt | None:
        if not self._listed():
            return None
        return await call_next(name, version=version)
