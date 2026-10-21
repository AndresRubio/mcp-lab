"""Pager: a tiny on-call paging HTTP API, the gateway's demo OpenAPI backend.

`GET /oncall/{service}` returns e-mail addresses, so redaction has a second, non-MCP backend to
work on. State lives in each app instance (`create_pager_app()`), so tests never share pages.
Run it with `python -m gateway.pager --port 8100`.
"""

import argparse
import sys
from typing import Any, cast

import httpx2
import uvicorn
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from gateway.listen import BindError, base_url, listen
from gateway.openapi import openapi_server

ROTA: dict[str, tuple[str, str]] = {
    "api": ("dana.lee@example.com", "sam.ortiz@example.com"),
    "payments": ("priya.n@example.com", "dana.lee@example.com"),
    "search": ("sam.ortiz@example.com", "priya.n@example.com"),
}

MCP_NAMES = {"getOncall": "oncall_for", "createPage": "send_page", "listPages": "list_pages"}

_STRING = {"type": "string"}
_ONCALL: dict[str, Any] = {
    "type": "object",
    "properties": {"service": _STRING, "primary": _STRING, "secondary": _STRING},
    "required": ["service", "primary", "secondary"],
}
_DETAIL: dict[str, Any] = {
    "type": "object",
    "properties": {"detail": _STRING},
    "required": ["detail"],
}
_NEW_PAGE: dict[str, Any] = {
    "type": "object",
    "properties": {"service": _STRING, "message": _STRING},
    "required": ["service", "message"],
}
_PAGE: dict[str, Any] = {
    "type": "object",
    "properties": {"id": {"type": "integer"}, "service": _STRING, "message": _STRING},
    "required": ["id", "service", "message"],
}

SPEC: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "Pager", "version": "1.0.0"},
    "servers": [{"url": "http://127.0.0.1:8100"}],
    "paths": {
        "/oncall/{service}": {
            "get": {
                "operationId": "getOncall",
                "summary": "Who is on call for a service (primary and secondary contacts).",
                "parameters": [
                    {"name": "service", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {
                    "200": {"description": "The rota", "content": {"application/json": {"schema": _ONCALL}}},
                    "404": {
                        "description": "No rota for that service",
                        "content": {"application/json": {"schema": _DETAIL}},
                    },
                },
            }
        },
        "/pages": {
            "get": {
                "operationId": "listPages",
                "summary": "List the pages sent so far.",
                "responses": {
                    "200": {
                        "description": "All pages",
                        "content": {"application/json": {"schema": {"type": "array", "items": _PAGE}}},
                    }
                },
            },
            "post": {
                "operationId": "createPage",
                "summary": "Page the on-call engineer for a service.",
                "requestBody": {"required": True, "content": {"application/json": {"schema": _NEW_PAGE}}},
                "responses": {
                    "201": {"description": "The page", "content": {"application/json": {"schema": _PAGE}}},
                    "404": {
                        "description": "No rota for that service",
                        "content": {"application/json": {"schema": _DETAIL}},
                    },
                    "422": {
                        "description": "The body is not a JSON object with string service and message",
                        "content": {"application/json": {"schema": _DETAIL}},
                    },
                },
            },
        },
    },
}


def create_pager_app() -> Starlette:
    """A fresh Pager API with its own, empty list of pages."""
    pages: list[dict[str, Any]] = []

    async def oncall(request: Request) -> JSONResponse:
        service = request.path_params["service"]
        if service not in ROTA:
            return JSONResponse({"detail": f"No rota for service '{service}'."}, status_code=404)
        primary, secondary = ROTA[service]
        return JSONResponse({"service": service, "primary": primary, "secondary": secondary})

    async def list_pages(request: Request) -> JSONResponse:
        return JSONResponse(pages)

    async def create_page(request: Request) -> JSONResponse:
        try:
            body: Any = await request.json()
        except ValueError:
            return JSONResponse({"detail": "The body is not valid JSON."}, status_code=422)
        if not isinstance(body, dict):
            return JSONResponse({"detail": "The body must be a JSON object."}, status_code=422)
        fields = cast(dict[str, Any], body)
        for key in ("service", "message"):
            if not isinstance(fields.get(key), str):
                return JSONResponse({"detail": f"'{key}' is required and must be a string."}, status_code=422)
        service, message = str(fields["service"]), str(fields["message"])
        if service not in ROTA:
            return JSONResponse({"detail": f"No rota for service '{service}'."}, status_code=404)
        page = {"id": len(pages) + 1, "service": service, "message": message}
        pages.append(page)
        return JSONResponse(page, status_code=201)

    return Starlette(
        routes=[
            Route("/oncall/{service}", oncall, methods=["GET"]),
            Route("/pages", list_pages, methods=["GET"]),
            Route("/pages", create_page, methods=["POST"]),
        ]
    )


def pager_mcp(client: httpx2.AsyncClient) -> FastMCP:
    """The Pager API as an MCP server, built the way `resolve_targets` builds an `openapi` source."""
    return openapi_server(SPEC, client, name="pager", mcp_names=MCP_NAMES)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m gateway.pager", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100, help="0 picks a free port")
    args = parser.parse_args(argv)
    # Bind before announcing, so `--port 0` reports the real port and nobody races for it.
    try:
        sock = listen(args.host, args.port)
    except BindError as err:
        raise SystemExit(f"pager: {err}") from err
    print(f"Pager listening on {base_url(args.host, sock.getsockname()[1])}", file=sys.stderr, flush=True)
    uvicorn.Server(uvicorn.Config(create_pager_app(), log_level="warning")).run(sockets=[sock])


if __name__ == "__main__":
    main()
