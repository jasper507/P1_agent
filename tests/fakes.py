"""测试用的假货：假 LLM 按顺序返回预设的回复，假电商后端照着 Django 视图的行为回 JSON，假向量模型按关键词出向量。都不连网。"""
import asyncio
import re
from http.cookies import SimpleCookie
from types import SimpleNamespace

import httpx2 as httpx

from app.django_client import DjangoClient, make_django_client
from app.policy_chunks import chunk_markdown
from app.policy_search import PolicyIndex


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


CSRF_TOKEN = "c" * 32  # 浏览器里 csrftoken cookie 的值。前端把它原样放进 X-CSRFToken 头


class FakeDjango:
    """只模拟 agent 用到的四个接口：/api/auth/me/、订单列表（分页）、订单详情、取消订单。"""

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
        self.payment_unresolved: set[str] = set()  # 这些订单有一笔支付还没出结果，商城不让取消
        self.cancel_hangs = False  # True：取消请求发出去了，但等不到回复
        self.refunds: list[str] = []  # 已付款订单取消时退了款的订单号

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
        if request.method == "POST":
            if match := re.fullmatch(r"/api/orders/([0-9a-f-]{36})/cancel/", path):
                return self._cancel(request, cookie, user, match.group(1))
            return httpx.Response(405, json={"detail": "Method not allowed."})
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

    def _cancel(self, request, cookie, user, public_id):
        """照着商城的 OrderCancelView 和 cancel_order 写：先过 CSRF，再按订单状态决定。"""
        csrf_cookie = cookie["csrftoken"].value if "csrftoken" in cookie else ""
        if not csrf_cookie or request.headers.get("x-csrftoken") != csrf_cookie:
            return httpx.Response(403, json={"detail": "CSRF Failed: CSRF token missing or incorrect."})
        if self.cancel_hangs:
            raise httpx.ReadTimeout("timed out", request=request)
        order = next((o for o in self.orders.get(user["id"], []) if o["public_id"] == public_id), None)
        if order is None:
            return httpx.Response(404, json={"code": "order_not_found", "detail": "订单不存在，或你无权取消。"})
        if public_id in self.payment_unresolved:
            return httpx.Response(409, json={"code": "payment_unresolved", "detail": "支付结果尚未确认，请先查询原支付结果，再关闭订单。"})
        if order["status"] == "PAID":
            self.refunds.append(public_id)
        elif order["status"] not in ("PENDING_PAYMENT", "CANCELED"):
            return httpx.Response(409, json={"code": "order_not_cancelable", "detail": "这张订单当前不能取消。"})
        order["status"] = "CANCELED"  # 已经取消的再取消一次：原样返回，不重复退款
        return httpx.Response(200, json=order)

    def cancels(self):
        """收到的取消请求。"""
        return [r for r in self.requests if r.method == "POST"]

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


class FakeEmbedder:
    """假的向量模型：每组近义词占一维，文本里出现几次就记几；一个都没出现，就落在最后的「其他」那一维。

    所以「坏了能修吗」和写着「保修」「维修」的条款方向一致，和别的条款不沾边，够用来测检索流程，又不用下载模型。
    """

    GROUPS = [("退货", "退"), ("运费", "寄回"), ("发票",), ("保修", "维修", "修", "坏", "故障")]

    def embed_documents(self, texts):
        return [self._vector(t) for t in texts]

    def embed_query(self, text):
        return self._vector(text)

    def _vector(self, text):
        counts = [float(sum(text.count(word) for word in group)) for group in self.GROUPS]
        return counts + [0.0 if any(counts) else 1.0]


# 测试用的小政策：三篇、六条。真正的政策在 policies/ 里，改了它也不影响这些测试
TEST_POLICIES = {
    "returns": """# 退换货政策

示例说明，不进索引。

## 1. 七天无理由退货

### 1.1 适用条件

签收之日起七天内，商品完好的，可以申请无理由退货。

### 1.2 不支持的商品

定制商品不支持无理由退货。

## 2. 退货运费

无理由退货的寄回运费由买家承担。
""",
    "invoices": """# 发票政策

## 1. 普通发票

所有订单都可以开电子普通发票。

## 2. 增值税专用发票

企业用户可以申请增值税专用发票，需要提供税号。
""",
    "warranty": """# 保修政策

## 1. 保修期限

数码配件签收后保修一年，期间出现故障免费维修。
""",
}


def make_policy_index(top_k=4, min_score=0.5, hybrid=False):
    chunks = [c for doc_id, markdown in TEST_POLICIES.items() for c in chunk_markdown(doc_id, markdown)]
    return PolicyIndex(chunks, FakeEmbedder(), top_k=top_k, min_score=min_score, hybrid=hybrid)
