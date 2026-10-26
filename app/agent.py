import logging
from dataclasses import dataclass

from openai import AsyncOpenAI

from app.conversations import Round
from app.schemas import ToolCallRecord
from app.tools import TOOL_SCHEMAS, ToolContext, execute_tool

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是电商网站的客服。涉及订单信息时必须调用工具查询，不要编造。"
    "订单状态的含义：PENDING_PAYMENT 待付款，PAID 已付款待发货，SHIPPED 已发货，"
    "COMPLETED 已完成，CANCELED 已取消，EXPIRED 超时未付款已关闭。"
    "回答用户时用中文说状态，不要直接念英文状态码。"
    "对话里之前查到的订单状态可能已经变了：用户问到状态时，即使前面查过，也要重新调用工具。"
    "售后问题（退换货、退款、取消订单、运费、发票、保修）必须先调用 search_policy，只根据查到的条款回答，"
    "并在用到的内容后面用括号注明出处，比如（《退换货政策》1.1）。"
    "search_policy 返回 found 为 false，或者查到的条款没有回答用户问的事时，告诉用户政策里没有相关规定，"
    "建议联系人工客服，不要凭常识补充，也不要拿意思相近的条款推断。"
    "问题和某张订单有关时，先查订单，再查政策。订单数据里没有签收日期，判断是否还在期限内时，要问用户哪天签收的。"
)
GIVE_UP_REPLY = "抱歉，这个问题我暂时处理不了，请换个说法再试试。"


@dataclass
class AgentResult:
    reply: str
    tool_calls: list[ToolCallRecord]
    new_round: Round  # 这一轮发给模型的全部消息，跑完以后整轮存进对话历史


def assistant_message(msg) -> dict:
    """把模型的回复转成可以存进 Redis、下一轮再原样发回去的 dict。"""
    message = {"role": "assistant", "content": msg.content}
    if msg.tool_calls:
        message["tool_calls"] = [
            {"id": call.id, "type": "function", "function": {"name": call.function.name, "arguments": call.function.arguments}}
            for call in msg.tool_calls
        ]
    return message


async def run_agent(
    llm: AsyncOpenAI, *, model: str, ctx: ToolContext, history: list[Round], message: str, max_steps: int
) -> AgentResult:
    """和第 2 步同一个循环。区别是每次都把之前几轮的消息一起发给模型，并把这一轮的消息交回去保存。"""
    past = [m for r in history for m in r]
    new_round: Round = [{"role": "user", "content": message}]
    records: list[ToolCallRecord] = []

    for step in range(1, max_steps + 1):
        resp = await llm.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, *past, *new_round],
            tools=TOOL_SCHEMAS,
            extra_body={"thinking": {"type": "disabled"}},
        )
        msg = resp.choices[0].message
        new_round.append(assistant_message(msg))

        if not msg.tool_calls:
            return AgentResult(reply=msg.content or "", tool_calls=records, new_round=new_round)

        for call in msg.tool_calls:
            content, ok = await execute_tool(call.function.name, call.function.arguments, ctx)
            logger.info(
                "user=%s step=%s tool=%s args=%s ok=%s",
                ctx.user.id, step, call.function.name, call.function.arguments, ok,
            )
            records.append(ToolCallRecord(name=call.function.name, arguments=call.function.arguments or "{}", ok=ok))
            new_round.append({"role": "tool", "tool_call_id": call.id, "content": content})

    logger.warning("agent stopped after max_steps=%s", max_steps)
    # 停不下来的这一轮，中间的工具调用不存，只留用户的话和兜底回复，免得把历史塞满
    give_up_round = [new_round[0], {"role": "assistant", "content": GIVE_UP_REPLY}]
    return AgentResult(reply=GIVE_UP_REPLY, tool_calls=records, new_round=give_up_round)
