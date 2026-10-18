import re
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

import httpx2
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from starlette.routing import Route

from gateway.pager import MCP_NAMES, SPEC, create_pager_app, pager_mcp

pytestmark = pytest.mark.anyio

Announced = Callable[..., AbstractContextManager[re.Match[str]]]


async def test_oncall_returns_primary_and_secondary(pager_http: httpx2.AsyncClient) -> None:
    response = await pager_http.get("/oncall/api")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "api"
    assert body["primary"].endswith("@example.com")
    assert body["secondary"].endswith("@example.com")


async def test_unknown_service_is_404(pager_http: httpx2.AsyncClient) -> None:
    response = await pager_http.get("/oncall/nope")
    assert response.status_code == 404
    assert response.json() == {"detail": "No rota for service 'nope'."}


async def test_pages_are_created_and_listed(pager_http: httpx2.AsyncClient) -> None:
    assert (await pager_http.get("/pages")).json() == []
    first = await pager_http.post("/pages", json={"service": "api", "message": "5xx spike"})
    second = await pager_http.post("/pages", json={"service": "search", "message": "down"})
    assert first.status_code == 201
    assert first.json() == {"id": 1, "service": "api", "message": "5xx spike"}
    assert second.json()["id"] == 2
    assert [p["id"] for p in (await pager_http.get("/pages")).json()] == [1, 2]


async def test_each_app_instance_has_its_own_pages() -> None:
    a, b = create_pager_app(), create_pager_app()
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=a), base_url="http://a") as ca:
        await ca.post("/pages", json={"service": "api", "message": "x"})
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=b), base_url="http://b") as cb:
        assert (await cb.get("/pages")).json() == []


def test_spec_names_every_operation() -> None:
    operation_ids = {op["operationId"] for path in SPEC["paths"].values() for op in path.values()}
    assert operation_ids == set(MCP_NAMES)
    assert SPEC["openapi"].startswith("3.1")


async def test_mcp_surface_has_the_three_named_tools(pager_http: httpx2.AsyncClient) -> None:
    async with Client(pager_mcp(pager_http)) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == {"oncall_for", "send_page", "list_pages"}


async def test_mcp_tools_call_the_http_api(pager_http: httpx2.AsyncClient) -> None:
    async with Client(pager_mcp(pager_http)) as client:
        oncall = await client.call_tool("oncall_for", {"service": "payments"})
        sent = await client.call_tool("send_page", {"service": "payments", "message": "payout late"})
        listed = await client.call_tool("list_pages", {})
    assert oncall.structured_content is not None
    assert oncall.structured_content["primary"].endswith("@example.com")
    assert sent.structured_content == {"id": 1, "service": "payments", "message": "payout late"}
    assert listed.structured_content == {
        "result": [{"id": 1, "service": "payments", "message": "payout late"}]
    }


def test_cli_binds_then_announces_and_serves(announced: Announced) -> None:
    with announced(
        [sys.executable, "-m", "gateway.pager", "--port", "0"], r"Pager listening on (http://\S+)"
    ) as m:
        response = httpx2.get(f"{m.group(1)}/oncall/search")
    assert response.status_code == 200
    assert response.json()["service"] == "search"


@pytest.mark.parametrize(
    ("kwargs", "status"),
    [
        ({"content": b"not json"}, 422),
        ({"json": {}}, 422),
        ({"json": {"service": "api"}}, 422),
        ({"json": {"service": "api", "message": 5}}, 422),
        ({"json": {"service": None, "message": "x"}}, 422),
        ({"json": ["api", "x"]}, 422),
        ({"json": {"service": "nope", "message": "x"}}, 404),
    ],
)
async def test_bad_page_requests_get_a_detail_and_create_nothing(
    pager_http: httpx2.AsyncClient, kwargs: dict[str, Any], status: int
) -> None:
    response = await pager_http.post("/pages", **kwargs)
    assert response.status_code == status
    assert isinstance(response.json()["detail"], str)
    assert (await pager_http.get("/pages")).json() == []


async def test_send_page_with_a_missing_argument_is_an_error_and_creates_no_page(
    pager_http: httpx2.AsyncClient,
) -> None:
    async with Client(pager_mcp(pager_http)) as client:
        with pytest.raises(ToolError):
            await client.call_tool("send_page", {"service": "api"})
    assert (await pager_http.get("/pages")).json() == []


async def test_oncall_for_an_unknown_service_is_an_error(pager_http: httpx2.AsyncClient) -> None:
    async with Client(pager_mcp(pager_http)) as client:
        with pytest.raises(ToolError):
            await client.call_tool("oncall_for", {"service": "nope"})


async def test_spec_matches_the_routes_and_success_statuses(pager_http: httpx2.AsyncClient) -> None:
    documented = {(m.upper(), path) for path, ops in SPEC["paths"].items() for m in ops}
    actual = {
        (m, route.path)
        for route in create_pager_app().routes
        if isinstance(route, Route)
        for m in (route.methods or set())
        if m != "HEAD"
    }
    assert documented == actual
    get = await pager_http.get("/oncall/api")
    post = await pager_http.post("/pages", json={"service": "api", "message": "x"})
    listing = await pager_http.get("/pages")
    assert str(get.status_code) in SPEC["paths"]["/oncall/{service}"]["get"]["responses"]
    assert str(post.status_code) in SPEC["paths"]["/pages"]["post"]["responses"]
    assert str(listing.status_code) in SPEC["paths"]["/pages"]["get"]["responses"]
