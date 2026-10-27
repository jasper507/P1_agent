"""MCP 服务的离线测试：用 MCP 官方客户端在进程内连上服务，走一遍真的协议（列工具、调工具、读资源）。

商城是假的（tests/fakes.py），政策索引用测试小政策和假向量模型，不连网。
"""

import asyncio
import json
from pathlib import Path

from mcp import Client

from app.mcp_server import Shop, build_server, login
from app.tools import ToolContext
from tests.fakes import SESSION_U1, FakeDjango, make_policy_index, uuid_for

POLICY_DIR = Path(__file__).resolve().parents[1] / "policies"


def make_server(fake, session_id=SESSION_U1):
    django = fake.client()
    shop = None

    async def get_shop():
        nonlocal shop
        if shop is None:
            shop = Shop(await login(django, session_id), django, make_policy_index)
        return shop

    return build_server(get_shop, POLICY_DIR)


def text(result):
    return "".join(c.text for c in result.content if getattr(c, "text", None))


def run(coro):
    return asyncio.run(coro)


def test_lists_only_the_three_read_only_tools():
    async def go():
        async with Client(make_server(FakeDjango())) as client:
            return (await client.list_tools()).tools

    tools = {t.name: t for t in run(go())}
    assert set(tools) == {"list_my_orders", "get_order_detail", "search_policy"}
    assert "cancel_order" not in tools  # 写操作不对外，见 ADR 0020
    assert all(t.annotations.read_only_hint for t in tools.values())
    assert "user_id" not in json.dumps([t.input_schema for t in tools.values()])


def test_list_my_orders_returns_only_the_logged_in_users_orders():
    fake = FakeDjango()

    async def go():
        async with Client(make_server(fake)) as client:
            return await client.call_tool("list_my_orders", {})

    data = json.loads(text(run(go())))
    assert [o["order_id"] for o in data["orders"]] == [uuid_for(101), uuid_for(102)]
    assert uuid_for(201) not in json.dumps(data)  # 小红的订单


def test_filter_by_status():
    async def go():
        async with Client(make_server(FakeDjango())) as client:
            return await client.call_tool("list_my_orders", {"status": "SHIPPED"})

    data = json.loads(text(run(go())))
    assert [o["status"] for o in data["orders"]] == ["SHIPPED"]


def test_someone_elses_order_is_not_found():
    async def go():
        async with Client(make_server(FakeDjango())) as client:
            return await client.call_tool("get_order_detail", {"order_id": uuid_for(201)})

    reply = text(run(go()))
    assert "找不到这张订单" in reply


def test_bad_order_id_is_explained_not_crashed():
    async def go():
        async with Client(make_server(FakeDjango())) as client:
            return await client.call_tool("get_order_detail", {"order_id": "../../admin"})

    assert text(run(go())).startswith("参数错误")


def test_search_policy_returns_clauses_with_citations():
    async def go():
        async with Client(make_server(FakeDjango())) as client:
            return await client.call_tool("search_policy", {"query": "签收后几天内可以无理由退货"})

    data = json.loads(text(run(go())))
    assert data["found"] is True
    assert all(c["citation"] for c in data["clauses"])


def test_without_a_valid_login_tools_say_so():
    fake = FakeDjango()

    async def go():
        async with Client(make_server(fake, session_id="")) as client:
            return await client.call_tool("list_my_orders", {})

    assert text(run(go())).startswith("未登录")
    assert fake.requests == []  # 格式都不对，就不去问商城


def test_unknown_session_is_rejected_by_the_shop():
    async def go():
        async with Client(make_server(FakeDjango(), session_id="nobody" + "0" * 26)) as client:
            return await client.call_tool("list_my_orders", {})

    assert "未登录" in text(run(go()))


def test_policy_documents_are_readable_resources():
    async def go():
        async with Client(make_server(FakeDjango())) as client:
            templates = (await client.list_resource_templates()).resource_templates
            doc = await client.read_resource("policy://returns")
            return templates, doc

    templates, doc = run(go())
    assert [t.uri_template for t in templates] == ["policy://{doc_id}"]
    assert "退换货政策" in doc.contents[0].text


def test_unknown_policy_document_is_refused():
    async def go():
        async with Client(make_server(FakeDjango())) as client:
            try:
                await client.read_resource("policy://..%2F.env")
            except Exception as e:  # 客户端把服务端的拒绝变成异常
                return str(e)
            return "read"

    assert run(go()) != "read"


def test_shop_builds_policy_index_only_once_and_lazily():
    calls = []

    def factory():
        calls.append(1)
        return make_policy_index()

    fake = FakeDjango()
    shop = Shop(run(login(fake.client(), SESSION_U1)), fake.client(), factory)
    assert calls == []
    assert isinstance(shop.context(), ToolContext)
    shop.context()
    assert calls == [1]
