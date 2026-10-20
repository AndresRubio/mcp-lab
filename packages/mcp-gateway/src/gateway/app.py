"""Assembles the gateway: one registry-governed proxy per backend, mounted at `/<name>/mcp`."""

import importlib
import json
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx2
from fastmcp import FastMCP
from fastmcp.client.transports import ClientTransport, StdioTransport
from fastmcp.server import create_proxy
from fastmcp.server.http import HostOriginGuardMiddleware
from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Mount, Route, request_response

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


class SpecError(ValueError):
    """An OpenAPI `spec:` in the registry could not be loaded."""


def load_openapi_spec(ref: str) -> dict[str, Any]:
    """Load a spec from a `*.json` file path, or from a `package.module:ATTR` dict.

    The `module:ATTR` form imports operator-named code, so the registry is trusted config.
    Every failure is a `SpecError` naming `ref`.
    """
    spec: object
    if ref.endswith(".json"):
        try:
            spec = json.loads(Path(ref).read_text())
        except (OSError, ValueError) as err:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
            raise SpecError(f"Cannot load OpenAPI spec {ref!r}: {err}") from err
    else:
        module_name, sep, attr = ref.partition(":")
        if not sep:
            raise SpecError(f"OpenAPI spec reference {ref!r} is neither a .json path nor module:ATTR")
        try:
            spec = getattr(importlib.import_module(module_name), attr)
        except (ImportError, AttributeError) as err:
            raise SpecError(f"Cannot load OpenAPI spec {ref!r}: {err}") from err
    if not isinstance(spec, dict) or "openapi" not in spec:
        raise SpecError(f"{ref!r} is not an OpenAPI spec")
    return cast(dict[str, Any], spec)


class ResolvedTargets(dict[str, ProxyTarget]):
    """The proxy targets of a registry, plus the resources `resolve_targets` opened for them."""

    def __init__(self) -> None:
        super().__init__()
        self.transports: list[StdioTransport] = []
        self.clients: list[httpx2.AsyncClient] = []

    async def aclose(self) -> None:
        """Stop every stdio backend and close every HTTP client. Idempotent; closes all, then raises."""
        errors: list[Exception] = []
        for transport in self.transports:
            try:
                await transport.close()
            except Exception as err:
                errors.append(err)
        for client in self.clients:
            try:
                await client.aclose()
            except Exception as err:
                errors.append(err)
        self.transports.clear()
        self.clients.clear()
        if errors:
            raise ExceptionGroup("closing gateway targets failed", errors)


def resolve_targets(registry: Registry) -> ResolvedTargets:
    """Turn each registry source into something `create_proxy` can front.

    stdio keeps one backend process alive for all callers (`keep_alive=True`): MRTR needs every
    round to reach the process that sealed the `requestState`. Such processes outlive the app
    until `aclose()` is called on the result.
    """
    targets = ResolvedTargets()
    for name, entry in registry.servers.items():
        source = entry.source
        match source:
            case StdioSource():
                command, *args = source.command
                transport = StdioTransport(command=command, args=args, keep_alive=True)
                targets.transports.append(transport)
                targets[name] = transport
            case HttpSource():
                targets[name] = source.url
            case OpenAPISource():
                client = httpx2.AsyncClient(base_url=source.base_url, timeout=30)
                targets.clients.append(client)
                targets[name] = FastMCP.from_openapi(
                    load_openapi_spec(source.spec), client=client, name=name, mcp_names=source.mcp_names
                )
    return targets


def create_app(
    servers: Mapping[str, FastMCP], *, on_shutdown: Callable[[], Awaitable[None]] | None = None
) -> Starlette:
    """One Starlette app: each server at `/<name>/mcp`, plus `GET /healthz`.

    `on_shutdown` runs after every mounted app's lifespan has exited (e.g. `targets.aclose`).
    Every `/<name>/mcp` endpoint and `/healthz` validate `Host` and `Origin` against DNS rebinding
    with FastMCP's `HostOriginGuardMiddleware` in "auto" mode, which checks only requests that
    arrive on a loopback address.
    """
    apps = {
        name: server.http_app(path="/mcp", host_origin_protection="auto") for name, server in servers.items()
    }

    # Starlette does not run a mounted app's lifespan, and each FastMCP app needs its own to
    # start its session manager. Run them all here, and yield None: fastmcp's combine_lifespans
    # yields a dict, which needs lifespan-state support that in-process ASGI harnesses lack.
    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncGenerator[None]:
        async with AsyncExitStack() as stack:
            if on_shutdown is not None:
                stack.push_async_callback(on_shutdown)
            for sub_app in apps.values():
                await stack.enter_async_context(sub_app.lifespan(sub_app))
            yield

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "servers": list(apps)})

    # /healthz names the servers, so it gets the same guard the MCP endpoints get.
    guarded_healthz = HostOriginGuardMiddleware(request_response(healthz), mode="auto")
    routes: list[BaseRoute] = [Route("/healthz", guarded_healthz, methods=["GET"])]
    routes += [Mount(f"/{name}", app=sub_app) for name, sub_app in apps.items()]
    return Starlette(routes=routes, lifespan=lifespan)
