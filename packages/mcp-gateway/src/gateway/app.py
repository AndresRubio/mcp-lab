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
