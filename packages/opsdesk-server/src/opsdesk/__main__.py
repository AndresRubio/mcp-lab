"""Run OpsDesk over stdio (default) or Streamable HTTP: `opsdesk-server [--transport http]`."""

import argparse

import uvicorn

from opsdesk.server import build_server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="opsdesk-server", description=__doc__)
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    server = build_server()
    if args.transport == "stdio":
        server.run()
    else:
        app = server.streamable_http_app(host=args.host)
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
