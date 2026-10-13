"""Run OpsDesk over stdio (default) or Streamable HTTP: `opsdesk-server [--transport http]`."""

import argparse
import socket
import sys

import uvicorn

from opsdesk.server import build_server


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
        sock = socket.create_server((args.host, args.port))
        port = sock.getsockname()[1]
        print(f"OpsDesk listening on http://{args.host}:{port}/mcp", file=sys.stderr, flush=True)
        uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[sock])


if __name__ == "__main__":
    main()
