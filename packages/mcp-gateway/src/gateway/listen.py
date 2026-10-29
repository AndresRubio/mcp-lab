"""Bind a listening socket before announcing it. `mcp-gateway serve` and the Pager demo share it."""

import socket


class BindError(Exception):
    """The listening socket could not be bound, for example because the port is in use."""


def listen(host: str, port: int) -> socket.socket:
    """A bound, listening TCP socket on `host:port`; port 0 picks a free port.

    The address family follows the host: `::1` is IPv6, `127.0.0.1` is IPv4. A name such as
    `localhost` binds IPv4 when it has an IPv4 address, so clients that connect to 127.0.0.1 are
    served even where the name resolves to `::1` first; a name with only IPv6 addresses binds
    IPv6. A bind that fails for any reason is a `BindError` naming the address.
    """
    try:
        infos = socket.getaddrinfo(host or None, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
        family = next((f for f, *_ in infos if f == socket.AF_INET), infos[0][0])
        return socket.create_server((host, port), family=family)
    except OSError as err:
        raise BindError(f"cannot listen on {host}:{port}: {err}") from err


def base_url(host: str, port: int) -> str:
    """`http://host:port`, with an IPv6 literal in brackets."""
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"
