"""测试用的假货：假 LLM 按顺序返回预设的回复，假电商后端照着 Django 视图的行为回 JSON。都不连网。"""
import asyncio
import re
from http.cookies import SimpleCookie
from types import SimpleNamespace

import httpx2 as httpx

from app.django_client import DjangoClient, make_django_client


def tool_call(call_id, name, arguments):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def assistant(content=None, tool_calls=None):
    return SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls)


HANG = object()  # 放进 FakeLLM 的回复列表里，表示这次请求卡住不返回


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []  # 每次请求时发过去的 messages
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.requests.append(list(kwargs["messages"]))
        reply = self.replies.pop(0)
        if reply is HANG:
            await asyncio.sleep(5)
            raise AssertionError("这次请求应该已经被超时打断了")
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(choices=[SimpleNamespace(message=reply)])


# 两个已登录用户的 sessionid（真实的 Django session key 也是 32 位小写字母和数字）
SESSION_U1 = "xiaoming" + "1" * 24
SESSION_U2 = "xiaohong" + "2" * 24
SESSION_U1_AGAIN = "xiaoming" + "3" * 24  # 小明重新登录后拿到的新 sessionid


def make_order(public_id, status, total="99.00", name="T恤", quantity=1):
    """字段和 Django 的 OrderSerializer 一模一样，包括不该发给模型的收件人信息。"""
    return {
        "public_id": public_id,
        "status": status,
        "currency": "CNY",
        "subtotal": total,
        "discount_total": "0.00",
        "shipping_fee": "0.00",
        "total": total,
        "address": {
            "recipient_name": "王小明",
            "phone": "13800001111",
            "province": "浙江省",
            "city": "杭州市",
            "district": "西湖区",
            "detail": "文三路 100 号",
        },
        "items": [
            {
                "product_name_snapshot": name,
                "sku_code_snapshot": "SKU-1",
                "color_snapshot": "黑色",
                "attributes_snapshot": {},
                "unit_price": total,
                "quantity": quantity,
                "discount_amount": "0.00",
                "line_total": total,
            }
        ],
        "created_at": "2026-10-01T10:00:00+08:00",
    }


def uuid_for(n):
    return f"00000000-0000-4000-8000-{n:012d}"


class FakeDjango:
    """只模拟 agent 用到的三个接口：/api/auth/me/、订单列表（分页）、订单详情。"""

    PAGE_SIZE = 20

    def __init__(self):
        self.users = {
            SESSION_U1: {"id": 1, "username": "xiaoming", "is_staff": False},
            SESSION_U2: {"id": 2, "username": "xiaohong", "is_staff": False},
            SESSION_U1_AGAIN: {"id": 1, "username": "xiaoming", "is_staff": False},
        }
        # 每个用户的订单，最新的在前（和 Django 的 order_by("-created_at") 一致）
        self.orders = {
            1: [make_order(uuid_for(101), "PENDING_PAYMENT", "199.00"), make_order(uuid_for(102), "SHIPPED", "89.00")],
            2: [make_order(uuid_for(201), "PAID", "59.00", name="帆布鞋")],
        }
        self.requests: list[httpx.Request] = []
        self.down = False  # True：连不上，像 Docker 没启动
        self.orders_broken = False  # True：订单接口返回 500

    def client(self) -> DjangoClient:
        return make_django_client("http://django.test", timeout=5, transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)

        cookie = SimpleCookie(request.headers.get("cookie", ""))
        user = self.users.get(cookie["sessionid"].value if "sessionid" in cookie else "")
        if user is None:
            return httpx.Response(403, json={"detail": "Authentication credentials were not provided."})

        path = request.url.path
        if path == "/api/auth/me/":
            return httpx.Response(200, json=user)
        if path == "/api/orders/":
            if self.orders_broken:
                return httpx.Response(500, text="Server Error")
            return self._order_page(user, int(request.url.params.get("page", "1")))
        if match := re.fullmatch(r"/api/orders/([0-9a-f-]{36})/", path):
            for order in self.orders.get(user["id"], []):
                if order["public_id"] == match.group(1):
                    return httpx.Response(200, json=order)
            return httpx.Response(404, json={"detail": "订单不存在，或你无权查看这张订单。"})
        return httpx.Response(404, text="Not Found")

    def _order_page(self, user, page):
        orders = self.orders.get(user["id"], [])
        start = (page - 1) * self.PAGE_SIZE
        has_next = start + self.PAGE_SIZE < len(orders)
        return httpx.Response(200, json={
            "count": len(orders),
            "next": f"http://django.test/api/orders/?page={page + 1}" if has_next else None,
            "previous": None,
            "results": orders[start:start + self.PAGE_SIZE],
        })

    def sessions_seen(self):
        """Django 依次收到的 Cookie 头，用来检查有没有串号。"""
        return [r.headers.get("cookie") for r in self.requests]

    def pages_requested(self):
        return [r.url.params.get("page") for r in self.requests if r.url.path == "/api/orders/"]
