import logging

from openai import AsyncOpenAI

from app.schemas import ChatResponse, ToolCallRecord
from app.tools import TOOL_SCHEMAS, ToolContext, execute_tool

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是电商网站的客服。涉及订单信息时必须调用工具查询，不要编造。"
    "订单状态的含义：PENDING_PAYMENT 待付款，PAID 已付款待发货，SHIPPED 已发货，"
    "COMPLETED 已完成，CANCELED 已取消，EXPIRED 超时未付款已关闭。"
    "回答用户时用中文说状态，不要直接念英文状态码。"
)
GIVE_UP_REPLY = "抱歉，这个问题我暂时处理不了，请换个说法再试试。"


async def run_agent(llm: AsyncOpenAI, *, model: str, ctx: ToolContext, message: str, max_steps: int) -> ChatResponse:
    """和第 1 步同一个循环。区别只是工具拿到的从 user_id 变成了 ctx（当前用户 + 电商后端客户端）。"""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": message},
    ]
    records: list[ToolCallRecord] = []

    for step in range(1, max_steps + 1):
        resp = await llm.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOL_SCHEMAS,
            extra_body={"thinking": {"type": "disabled"}},
        )
        msg = resp.choices[0].message
        messages.append(msg)

        if not msg.tool_calls:
            return ChatResponse(reply=msg.content or "", tool_calls=records)

        for call in msg.tool_calls:
            content, ok = await execute_tool(call.function.name, call.function.arguments, ctx)
            logger.info(
                "user=%s step=%s tool=%s args=%s ok=%s",
                ctx.user.id, step, call.function.name, call.function.arguments, ok,
            )
            records.append(ToolCallRecord(name=call.function.name, arguments=call.function.arguments or "{}", ok=ok))
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})

    logger.warning("agent stopped after max_steps=%s", max_steps)
    return ChatResponse(reply=GIVE_UP_REPLY, tool_calls=records)
