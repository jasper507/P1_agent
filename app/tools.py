import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

OrderStatus = Literal["PENDING_PAYMENT", "PAID", "SHIPPED", "COMPLETED", "CANCELED"]

# 第 2 步换成调用 Django 的订单接口
FAKE_ORDERS = {
    "u1": [
        {"order_id": "A1001", "status": "PENDING_PAYMENT", "total": "199.00"},
        {"order_id": "A1002", "status": "SHIPPED", "total": "89.00"},
    ],
    "u2": [
        {"order_id": "B2001", "status": "PAID", "total": "59.00"},
    ],
}


# 模型能传的参数，只有这些。extra="forbid"：多传一个字段（比如 user_id）就算参数错误。
# 注意这里用注释而不是 docstring：docstring 会进到 JSON Schema 里，被发给模型看。
class ListMyOrdersArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: OrderStatus | None = Field(default=None, description="订单状态；不传则返回全部订单")


async def list_my_orders(user_id: str, args: ListMyOrdersArgs) -> list[dict]:
    # user_id 由服务端传入，来自登录态，不在模型能填的参数里
    orders = FAKE_ORDERS.get(user_id, [])
    if args.status:
        orders = [o for o in orders if o["status"] == args.status]
    return orders


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    func: Callable[[str, Any], Awaitable[Any]]

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
        Tool("list_my_orders", "查询当前登录用户自己的订单，可以按状态过滤", ListMyOrdersArgs, list_my_orders),
    ]
}
TOOL_SCHEMAS = [tool.schema() for tool in TOOLS.values()]


async def execute_tool(name: str, raw_args: str | None, user_id: str) -> tuple[str, bool]:
    """执行一次工具调用，返回 (交给模型的内容, 是否成功)。

    出错时不抛异常，而是把错误写成文字交还给模型，让它自己改正后重试。
    """
    tool = TOOLS.get(name)
    if tool is None:
        return f"错误：没有名为 {name} 的工具", False
    try:
        args = tool.args_model.model_validate_json(raw_args or "{}")
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, err['loc'])) or '参数'}: {err['msg']}" for err in e.errors())
        return f"参数错误：{problems}", False
    result = await tool.func(user_id, args)
    return json.dumps(result, ensure_ascii=False), True
