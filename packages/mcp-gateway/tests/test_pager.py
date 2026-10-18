import re
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager

import httpx2
import pytest
from fastmcp import Client

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
