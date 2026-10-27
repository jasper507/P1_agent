"""把客服的三个只读工具做成 MCP 服务，别的 AI 应用（Claude、Cursor……）也能接。

这里不另写一套工具：查订单、查政策都走 app/tools.py 里和 agent 同一个 execute_tool，
参数白名单、错误转成文字、订单归属由商城判断，这些规矩一条不少。

和 agent 的区别只有两点：
1. 身份：MCP 服务跑在用户自己的电脑上，一个进程只代表一个人。商城的 sessionid 从环境变量
   P1_SHOP_SESSIONID 读（用户自己从浏览器复制），第一次调用工具时问一次商城「这是谁」，问不出来就如实报错。
2. 不提供取消订单：取消必须用户在页面上点「确认取消」，这个确认流程只有 agent 的接口里有。
   MCP 客户端那边的确认怎么做我们管不了，所以写操作干脆不对外，见 ADR 0020。

运行（stdio，由 MCP 客户端拉起）：python -m app.mcp_server
"""

import json
import os
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from app.auth import SESSION_ID_RE, CurrentUser
from app.django_client import DjangoClient, DjangoError, get_django
from app.config import get_settings
from app.policy_search import PolicyIndex, get_policy_index
from app.tools import ToolContext, execute_tool

OrderStatus = Literal["PENDING_PAYMENT", "PAID", "SHIPPED", "COMPLETED", "CANCELED", "EXPIRED"]
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

INSTRUCTIONS = (
    "这是一家网店的客服工具：只能查询当前登录用户自己的订单和本店售后政策，不能修改或取消订单。"
    "回答售后问题时只依据 search_policy 查到的条款，并注明出处；found 为 false 就说政策里没有规定。"
)


class LoginFailed(Exception):
    """sessionid 没设置、格式不对，或者商城不认。message 直接交给客户端的模型。"""


class Shop:
    """MCP 服务每次调用工具时需要的东西：当前用户、商城客户端、政策索引。

    政策索引第一次用到才建（要加载向量模型，一两秒），只查订单的会话不用等。
    """

    def __init__(self, user: CurrentUser, django: DjangoClient, policies: Callable[[], PolicyIndex]):
        self.user = user
        self.django = django
        self._policies = policies
        self._index: PolicyIndex | None = None

    def context(self) -> ToolContext:
        if self._index is None:
            self._index = self._policies()
        return ToolContext(user=self.user, django=self.django, policies=self._index)


async def login(django: DjangoClient, session_id: str) -> CurrentUser:
    """和 agent 的 get_current_user 一样：格式不对直接拒绝，对的话问商城「这是谁」。"""
    if not SESSION_ID_RE.fullmatch(session_id):
        raise LoginFailed("P1_SHOP_SESSIONID 没设置或格式不对：请从浏览器复制商城的 sessionid 填进去")
    try:
        me = await django.me(session_id)
    except DjangoError as e:
        raise LoginFailed(f"商城登录校验没通过：{e.message}") from e
    return CurrentUser(id=me["id"], username=me["username"], session_id=session_id)


def build_server(get_shop: Callable[[], Awaitable[Shop]], policy_dir) -> MCPServer:
    server = MCPServer(name="p1-shop", title="P1 网店客服工具", instructions=INSTRUCTIONS)

    async def run(name: str, args: dict) -> str:
        try:
            shop = await get_shop()
        except LoginFailed as e:
            return f"未登录：{e}"
        content, _ok = await execute_tool(name, json.dumps(args, ensure_ascii=False), shop.context())
        # 出错时 execute_tool 已经写成一句中文说明，原样交给客户端的模型，让它如实告诉用户
        return content

    @server.tool(description="查询当前登录用户最近的订单（最多 10 张，最新的在前），可以按状态过滤", annotations=READ_ONLY)
    async def list_my_orders(status: OrderStatus | None = None) -> str:
        return await run("list_my_orders", {} if status is None else {"status": status})

    @server.tool(description="查询当前登录用户某一张订单的详情：金额明细、商品、收货地区。order_id 是 list_my_orders 返回的订单号", annotations=READ_ONLY)
    async def get_order_detail(order_id: str) -> str:
        return await run("get_order_detail", {"order_id": order_id})

    @server.tool(
        description="查询本店售后政策（退换货、退款、取消订单、运费、发票、保修），返回最相关的条款和出处；政策里没有相关规定时 found 为 false",
        annotations=READ_ONLY,
    )
    async def search_policy(query: str) -> str:
        return await run("search_policy", {"query": query})

    # 资源和工具的区别：工具是「让模型去做一件事」，资源是「一份可以直接读的文档」，由客户端决定要不要放进上下文
    docs = {p.stem: p for p in sorted(policy_dir.glob("*.md"))}

    @server.resource("policy://{doc_id}", name="售后政策原文", description=f"本店售后政策全文，doc_id 可选：{'、'.join(docs)}", mime_type="text/markdown")
    def policy_doc(doc_id: str) -> str:
        # 只认 policies 目录里已有的文件名，不拿 doc_id 去拼路径
        if doc_id not in docs:
            raise ValueError(f"没有这份政策：{doc_id}。可选：{'、'.join(docs)}")
        return docs[doc_id].read_text(encoding="utf-8")

    return server


def main() -> None:
    # MCP 客户端从哪个目录拉起服务都不一定，先回到项目根目录，.env 和 policies/ 才找得到
    os.chdir(Path(__file__).resolve().parents[1])
    settings = get_settings()
    django = get_django()
    session_id = os.environ.get("P1_SHOP_SESSIONID", "").strip()
    shop: Shop | None = None

    async def get_shop() -> Shop:
        # 第一次调用工具时才登录：MCP 客户端启动服务时还没有事件循环里的请求
        nonlocal shop
        if shop is None:
            shop = Shop(await login(django, session_id), django, get_policy_index)
        return shop

    build_server(get_shop, settings.policy_dir).run("stdio")


if __name__ == "__main__":
    main()
