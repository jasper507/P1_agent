import logging

from openai import AsyncOpenAI

from app.schemas import ChatResponse, ToolCallRecord
from app.tools import TOOL_SCHEMAS, execute_tool

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = "你是电商客服。涉及订单信息时必须调用工具查询，不要编造。"
GIVE_UP_REPLY = "抱歉，这个问题我暂时处理不了，请换个说法再试试。"


async def run_agent(llm: AsyncOpenAI, *, model: str, user_id: str, message: str, max_steps: int) -> ChatResponse:
    """和第 0 步同一个循环，区别是：异步、用户由调用方传入、工具出错不会让整个请求崩掉。"""
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
            content, ok = await execute_tool(call.function.name, call.function.arguments, user_id)
            logger.info("step=%s tool=%s args=%s ok=%s", step, call.function.name, call.function.arguments, ok)
            records.append(ToolCallRecord(name=call.function.name, arguments=call.function.arguments or "{}", ok=ok))
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})

    logger.warning("agent stopped after max_steps=%s", max_steps)
    return ChatResponse(reply=GIVE_UP_REPLY, tool_calls=records)
