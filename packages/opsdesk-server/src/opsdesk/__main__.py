"""Run OpsDesk over stdio (default) or Streamable HTTP: `opsdesk-server [--transport http]`."""

import argparse
import socket
import sys

import uvicorn

from opsdesk.server import build_server


class BindError(Exception):
    """The listening socket could not be bound, for example because the port is in use."""


def listen(host: str, port: int) -> socket.socket:
    """A bound, listening TCP socket on `host:port`; port 0 picks a free port.

    The address family follows the host (`::1` is IPv6); a name binds IPv4 when it has an IPv4
    address, so `localhost` serves clients of 127.0.0.1 where it resolves to `::1` first. A failed
    bind is a `BindError` naming the address. The gateway package has the same helper; the two
    packages share no code.
    """
    try:
        infos = socket.getaddrinfo(host or None, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
        family = next((f for f, *_ in infos if f == socket.AF_INET), infos[0][0])
        return socket.create_server((host, port), family=family)
    except OSError as err:
        raise BindError(f"cannot listen on {host}:{port}: {err}") from err


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="opsdesk-server", description=__doc__)
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000, help="0 picks a free port")
    args = parser.parse_args(argv)

    server = build_server()
    if args.transport == "stdio":
        server.run()
    else:
        app = server.streamable_http_app(host=args.host)
        # Bind before announcing, so `--port 0` reports the real port and callers never race
        # for a port that something else might grab between "pick" and "bind".
        try:
            sock = listen(args.host, args.port)
        except BindError as err:
            raise SystemExit(f"opsdesk-server: {err}") from err
        port = sock.getsockname()[1]
        host = f"[{args.host}]" if ":" in args.host else args.host
        print(f"OpsDesk listening on http://{host}:{port}/mcp", file=sys.stderr, flush=True)
        uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[sock])


if __name__ == "__main__":
    main()
