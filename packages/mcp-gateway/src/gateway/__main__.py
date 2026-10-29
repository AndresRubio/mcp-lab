"""mcp-gateway: serve every registered MCP server behind one endpoint, or audit the registry.

mcp-gateway serve --registry registry.yaml [--host 127.0.0.1] [--port 8080]
mcp-gateway audit --registry registry.yaml

`audit` exits with status 1 when the registry enables a tool that its backend does not offer.
"""

import argparse
import logging
import sys
from pathlib import Path

import anyio
import uvicorn
from fastmcp import Client

from gateway.app import SpecError, build_gateway_servers, create_app, resolve_targets
from gateway.listen import BindError, base_url, listen
from gateway.registry import RegistryError, RegistryHolder, load_registry


def serve(registry_path: Path, host: str, port: int) -> None:
    holder = RegistryHolder(registry_path)
    targets = resolve_targets(holder.current, spec_dir=registry_path.parent)
    app = create_app(build_gateway_servers(holder, targets), on_shutdown=targets.aclose)
    # Bind before announcing, so `--port 0` reports the real port and nobody races for it.
    sock = listen(host, port)
    base = base_url(host, sock.getsockname()[1])
    lines = [
        f"Gateway listening on {base}",
        *(f"  {name}: {base}/{name}/mcp" for name in holder.current.servers),
    ]
    print("\n".join(lines), file=sys.stderr, flush=True)
    uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[sock])


async def audit(registry_path: Path) -> bool:
    """Print, per server, the backend tools the registry does not list: they await review.

    Also print the listed tools the backend does not offer (a stale name, a typo, a tool listed
    before it ships), marking the enabled ones: such a tool goes live unreviewed the moment a
    backend ships it under that name. Returns True when any such entry is enabled.
    """
    registry = load_registry(registry_path)
    targets = resolve_targets(registry, spec_dir=registry_path.parent)
    enabled_but_not_offered = False
    try:
        for name, target in targets.items():
            entry = registry.servers[name]
            label = f"{name} (owner: {entry.owner})"
            try:
                async with Client(target) as client:
                    offered = [tool.name for tool in await client.list_tools()]
            except Exception as err:  # one unreachable backend must not hide the others' findings
                print(f"{label}: unreachable: {err}")
                continue
            pending = [tool for tool in offered if tool not in entry.tools]
            if pending:
                print(f"{label}: pending review: {', '.join(pending)}")
            else:
                print(f"{label}: every tool is reviewed")
            stale = [tool for tool in entry.tools if tool not in offered]
            if stale:
                marked = [f"{tool} (enabled)" if entry.is_enabled(tool) else tool for tool in stale]
                print(f"{label}: listed but not offered: {', '.join(marked)}")
                enabled_but_not_offered |= any(entry.is_enabled(tool) for tool in stale)
    finally:
        await targets.aclose()
    return enabled_but_not_offered


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="mcp-gateway", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve_cmd = commands.add_parser("serve", help="serve every registered server at /<name>/mcp")
    serve_cmd.add_argument("--registry", type=Path, required=True)
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8080, help="0 picks a free port")
    audit_cmd = commands.add_parser(
        "audit", help="report unlisted backend tools and listed tools no backend offers"
    )
    audit_cmd.add_argument("--registry", type=Path, required=True)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        if args.command == "serve":
            serve(args.registry, args.host, args.port)
        elif anyio.run(audit, args.registry):
            raise SystemExit(1)  # an enabled tool that no backend offers would go live unreviewed
    except (RegistryError, SpecError, BindError) as err:  # operator errors only
        raise SystemExit(f"mcp-gateway: {err}") from err


if __name__ == "__main__":
    main()
