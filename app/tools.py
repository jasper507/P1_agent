import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.auth import CsrfTokens, CurrentUser
from app.django_client import DjangoClient, DjangoError
from app.policy_chunks import Chunk
from app.policy_search import PolicyIndex

# 和 Django 的 Order.Status 保持一致
OrderStatus = Literal["PENDING_PAYMENT", "PAID", "SHIPPED", "COMPLETED", "CANCELED", "EXPIRED"]

MAX_ORDERS = 10  # 一次最多给模型看几张订单，太多既费 token 又没用
MAX_PAGES = 5  # 最多翻几页（每页 20 张），也就是只在最近 100 张订单里找


@dataclass(frozen=True)
class ToolContext:
    """工具执行时由服务端提供的东西。模型碰不到这里的任何字段。"""

    user: CurrentUser
    django: DjangoClient
    policies: PolicyIndex
    csrf: CsrfTokens | None = None  # 只有用户点确认的那个请求才有，取消订单要用
    sources: list[Chunk] = field(default_factory=list)  # 这一轮 search_policy 真正查到的条款，跑完由接口返回


# 模型能传的参数，只有这些。extra="forbid"：多传一个字段（比如 user_id）就算参数错误。
# 注意这里用注释而不是 docstring：docstring 会进到 JSON Schema 里，被发给模型看。
class ListMyOrdersArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: OrderStatus | None = Field(default=None, description="订单状态；不传则返回全部订单")


class GetOrderDetailArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: UUID = Field(description="订单号，即 list_my_orders 返回的 order_id")


class CancelOrderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: UUID = Field(description="要取消的订单号，即 list_my_orders 返回的 order_id")


class SearchPolicyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=2, max_length=100, description="要查的售后问题，写成一句完整的话，比如「签收后几天内可以无理由退货」")


def summarize_order(order: dict) -> dict:
    # Django 返回的订单里有收件人姓名、电话和详细地址。模型服务在第三方，这些用不上就不发
    return {
        "order_id": order["public_id"],
        "status": order["status"],
        "total": order["total"],
        "currency": order["currency"],
        "created_at": order["created_at"],
        "items": [f"{item['product_name_snapshot']} x{item['quantity']}" for item in order["items"]],
    }


async def list_my_orders(ctx: ToolContext, args: ListMyOrdersArgs) -> dict:
    # Django 的列表接口只能翻页，不能按状态筛选，所以在这边一页页取、一边筛
    matched: list[dict] = []
    page = 1
    while True:
        data = await ctx.django.list_orders(ctx.user.session_id, page)
        matched += [o for o in data["results"] if args.status in (None, o["status"])]
        if len(matched) > MAX_ORDERS or not data["next"] or page >= MAX_PAGES:
            break
        page += 1
    return {
        "orders": [summarize_order(o) for o in matched[:MAX_ORDERS]],
        "has_more": len(matched) > MAX_ORDERS or bool(data["next"]),
    }


async def get_order_detail(ctx: ToolContext, args: GetOrderDetailArgs) -> dict:
    # 订单是不是这个用户的，由 Django 判断（它用 orders_for(request.user) 过滤）；不是就 404
    order = await ctx.django.get_order(ctx.user.session_id, args.order_id)
    address = order["address"]
    return {
        **summarize_order(order),
        "subtotal": order["subtotal"],
        "discount_total": order["discount_total"],
        "shipping_fee": order["shipping_fee"],
        "ship_to_region": f"{address['province']} {address['city']} {address['district']}",
        "items": [
            {
                "name": item["product_name_snapshot"],
                "sku": item["sku_code_snapshot"],
                "unit_price": item["unit_price"],
                "quantity": item["quantity"],
                "line_total": item["line_total"],
            }
            for item in order["items"]
        ],
    }


async def search_policy(ctx: ToolContext, args: SearchPolicyArgs) -> dict:
    # 向量模型在 CPU 上算，放到线程里跑，免得卡住同时在处理的其他请求
    result = await asyncio.to_thread(ctx.policies.search, args.query)
    if not result.chunks:
        return {"found": False, "message": "售后政策里没有找到和这个问题相关的规定"}
    # 出处以这里记下的为准，接口原样返回给前端；模型在回答里标错了，也不影响这份列表
    ctx.sources.extend(result.chunks)
    return {"found": True, "clauses": [{"citation": c.citation, "text": c.text} for c in result.chunks]}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    # 要用户确认的工具没有 func：模型调用它时 agent 先暂停，用户点了确认再由 app/cancel_order.py 执行
    func: Callable[[ToolContext, Any], Awaitable[Any]] | None

    def schema(self) -> dict:
        """给模型看的工具说明，参数部分直接由 Pydantic 模型生成，不用手写第二份。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_model.model_json_schema(),
            },
        }


# 加新工具：写好参数模型和函数，再在这里登记一行
TOOLS = {
    tool.name: tool
    for tool in [
        Tool(
            "list_my_orders",
            "查询当前登录用户最近的订单（最多 10 张，最新的在前），可以按状态过滤",
            ListMyOrdersArgs,
            list_my_orders,
        ),
        Tool(
            "get_order_detail",
            "查询当前登录用户某一张订单的详情：金额明细、商品、收货地区",
            GetOrderDetailArgs,
            get_order_detail,
        ),
        Tool(
            "search_policy",
            "查询本店的售后政策（退换货、退款、取消订单、运费、发票、保修），返回最相关的几条条款和出处；"
            "政策里没有相关规定时 found 为 false",
            SearchPolicyArgs,
            search_policy,
        ),
        Tool(
            "cancel_order",
            "取消当前登录用户的一张订单。调用后不会马上取消：页面会先请用户确认，用户点了确认才执行。"
            "返回 canceled 为 true 才是取消成功",
            CancelOrderArgs,
            None,
        ),
    ]
}
TOOL_SCHEMAS = [tool.schema() for tool in TOOLS.values()]


def parse_args(args_model: type[BaseModel], raw_args: str | None) -> tuple[Any, str | None]:
    """校验模型传来的参数。不对的话返回一段说明，交还给模型让它改。"""
    try:
        return args_model.model_validate_json(raw_args or "{}"), None
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, err['loc'])) or '参数'}: {err['msg']}" for err in e.errors())
        return None, f"参数错误：{problems}"


async def execute_tool(name: str, raw_args: str | None, ctx: ToolContext) -> tuple[str, bool]:
    """执行一次工具调用，返回 (交给模型的内容, 是否成功)。

    预料之中的错误（参数不对、订单不存在、电商服务挂了）不抛异常，而是写成文字交还给模型，
    让它改正后重试或者如实告诉用户。预料之外的错误（代码 bug）照常抛出，不在这里吞掉。
    """
    tool = TOOLS.get(name)
    if tool is None:
        return f"错误：没有名为 {name} 的工具", False
    if tool.func is None:
        # 走到这里说明调用方漏掉了确认这一步。宁可报错，也不能不经确认就执行
        return f"错误：{name} 要等用户确认，不能直接执行", False
    args, error = parse_args(tool.args_model, raw_args)
    if error:
        return error, False
    try:
        result = await tool.func(ctx, args)
    except DjangoError as e:
        return f"查询失败：{e.message}", False
    return json.dumps(result, ensure_ascii=False), True
