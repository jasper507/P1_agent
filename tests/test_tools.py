import asyncio
import json

import pytest

from app.auth import CurrentUser
from app.tools import TOOLS, ToolContext, execute_tool
from tests.fakes import SESSION_U1, FakeDjango, make_order, make_policy_index, uuid_for


@pytest.fixture
def fake_django():
    return FakeDjango()


def make_ctx(fake_django):
    user = CurrentUser(id=1, username="xiaoming", session_id=SESSION_U1)
    return ToolContext(user=user, django=fake_django.client(), policies=make_policy_index())


def run(fake_django, name, raw_args):
    """以小明的身份执行一次工具调用。"""
    return asyncio.run(execute_tool(name, raw_args, make_ctx(fake_django)))


def test_schemas_do_not_expose_identity():
    for name, expected in [
        ("list_my_orders", ["status"]),
        ("get_order_detail", ["order_id"]),
        ("search_policy", ["query"]),
        ("cancel_order", ["order_id"]),
    ]:
        params = TOOLS[name].schema()["function"]["parameters"]
        assert list(params["properties"]) == expected
        assert params["additionalProperties"] is False


def test_filters_by_status(fake_django):
    content, ok = run(fake_django, "list_my_orders", '{"status": "SHIPPED"}')
    assert ok is True
    assert [o["order_id"] for o in json.loads(content)["orders"]] == [uuid_for(102)]


def test_expired_status_is_accepted(fake_django):
    fake_django.orders[1].append(make_order(uuid_for(103), "EXPIRED"))
    content, ok = run(fake_django, "list_my_orders", '{"status": "EXPIRED"}')
    assert ok is True
    assert [o["order_id"] for o in json.loads(content)["orders"]] == [uuid_for(103)]


def test_looks_past_first_page_when_filtering(fake_django):
    # 25 张订单，只有第 22 张（在第 2 页）是已完成
    fake_django.orders[1] = [make_order(uuid_for(i), "COMPLETED" if i == 22 else "CANCELED") for i in range(1, 26)]
    content, ok = run(fake_django, "list_my_orders", '{"status": "COMPLETED"}')
    assert [o["order_id"] for o in json.loads(content)["orders"]] == [uuid_for(22)]
    assert fake_django.pages_requested() == ["1", "2"]


def test_returns_at_most_ten_orders(fake_django):
    fake_django.orders[1] = [make_order(uuid_for(i), "PAID") for i in range(1, 26)]
    content, ok = run(fake_django, "list_my_orders", "{}")
    result = json.loads(content)
    assert len(result["orders"]) == 10 and result["has_more"] is True
    assert fake_django.pages_requested() == ["1"]  # 第一页就够了，不多翻


def test_stops_after_max_pages(fake_django):
    fake_django.orders[1] = [make_order(uuid_for(i), "CANCELED") for i in range(1, 201)]
    content, ok = run(fake_django, "list_my_orders", '{"status": "PAID"}')
    assert json.loads(content) == {"orders": [], "has_more": True}
    assert fake_django.pages_requested() == ["1", "2", "3", "4", "5"]


def test_personal_info_is_not_sent_to_model(fake_django):
    for name, raw_args in [("list_my_orders", "{}"), ("get_order_detail", f'{{"order_id": "{uuid_for(101)}"}}')]:
        content, ok = run(fake_django, name, raw_args)
        assert ok is True
        assert "13800001111" not in content and "王小明" not in content and "文三路" not in content


def test_order_detail(fake_django):
    content, ok = run(fake_django, "get_order_detail", f'{{"order_id": "{uuid_for(101)}"}}')
    detail = json.loads(content)
    assert detail["status"] == "PENDING_PAYMENT"
    assert detail["ship_to_region"] == "浙江省 杭州市 西湖区"
    assert detail["items"][0]["line_total"] == "199.00"


def test_order_id_must_be_uuid(fake_django):
    # 模型给的参数会拼进 URL；不是 UUID 就在本地挡掉，不会变成 /api/orders/../auth/me/
    content, ok = run(fake_django, "get_order_detail", '{"order_id": "../auth/me"}')
    assert ok is False and content.startswith("参数错误：order_id")
    assert fake_django.requests == []


def test_rejects_unknown_status(fake_django):
    content, ok = run(fake_django, "list_my_orders", '{"status": "LOST"}')
    assert ok is False and content.startswith("参数错误：status")


def test_rejects_broken_json(fake_django):
    content, ok = run(fake_django, "list_my_orders", '{"status": ')
    assert ok is False and content.startswith("参数错误")


def test_cancel_order_never_runs_directly(fake_django):
    # 取消要先经过用户确认。万一哪段代码绕过确认直接执行它，这里也只会报错
    content, ok = run(fake_django, "cancel_order", f'{{"order_id": "{uuid_for(101)}"}}')
    assert ok is False and content == "错误：cancel_order 要等用户确认，不能直接执行"
    assert fake_django.cancels() == []
