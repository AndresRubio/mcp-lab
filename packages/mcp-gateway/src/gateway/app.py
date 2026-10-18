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
