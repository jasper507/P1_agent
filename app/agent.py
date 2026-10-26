"""agent 的循环。第 5 步起用 LangGraph 写成一张图，前两个节点就是原来手写的循环：

    model ──有工具调用──▶ tools ──模型要取消订单──▶ prepare ──能取消──▶ confirm ──▶ execute
      ▲                    │                          │                 （暂停，等用户点）    │
      └────────────────────┴──────────────────────────┴──────────────────────────────────────┘

换成图，是为了 confirm：跑到一半停下来，等用户在页面上点「确认取消」，再从停下的地方接着跑。
停下时的状态由 checkpointer 存盘，所以确认可以是另一个 HTTP 请求，中间服务重启了也接得上。
"""
import json
import logging
import operator
from dataclasses import dataclass
from typing import Annotated, TypedDict
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt
from openai import AsyncOpenAI

from app.cancel_order import audit, confirm_prompt, execute_cancel, prepare_cancel
from app.conversations import Round
from app.django_client import DjangoError, DjangoOutcomeUnknown
from app.schemas import ToolCallRecord
from app.tools import TOOL_SCHEMAS, CancelOrderArgs, ToolContext, execute_tool, parse_args

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是电商网站的客服。涉及订单信息时必须调用工具查询，不要编造。"
    "订单状态的含义：PENDING_PAYMENT 待付款，PAID 已付款待发货，SHIPPED 已发货，"
    "COMPLETED 已完成，CANCELED 已取消，EXPIRED 超时未付款已关闭。"
    "回答用户时用中文说状态，不要直接念英文状态码。"
    "对话里之前查到的订单状态可能已经变了：用户问到状态时，即使前面查过，也要重新调用工具。"
    "售后问题（退换货、退款、取消订单的规则、运费、发票、保修）必须先调用 search_policy，只根据查到的条款回答，"
    "并在用到的内容后面用括号注明出处，比如（《退换货政策》1.1）。"
    "search_policy 返回 found 为 false，或者查到的条款没有回答用户问的事时，告诉用户政策里没有相关规定，"
    "建议联系人工客服，不要凭常识补充，也不要拿意思相近的条款推断。"
    "问题和某张订单有关时，先查订单，再查政策。订单数据里没有签收日期，判断是否还在期限内时，要问用户哪天签收的。"
    "用户明确要取消某张订单时，先弄清是哪一张（需要的话先查订单），再调用 cancel_order，不用先查政策。"
    "cancel_order 会先请用户在页面上确认：你不要自己追问「确定吗」，拿到结果之前也不要说已经取消。一次只取消一张。"
    "cancel_order 返回 canceled 为 true 才是取消成功；为 false 或 null 时，把 reason 照实告诉用户。"
    "返回 declined 为 true 是用户自己点了「不取消」，不是出错：告诉用户订单保持原样就行，不要说成取消失败。"
)
GIVE_UP_REPLY = "抱歉，这个问题我暂时处理不了，请换个说法再试试。"
ONE_AT_A_TIME = "错误：一次只能取消一张订单。等前一张处理完，再取消这一张"


class RoundState(TypedDict, total=False):
    """一轮对话跑到哪了。暂停时整个存进 checkpoint，所以只放能存成 JSON 的东西。

    带 operator.add 的字段，节点返回的列表会接在后面，而不是把原来的换掉。
    """

    messages: Annotated[list[dict], operator.add]  # 这一轮的消息，第一条是用户的话
    tool_calls: Annotated[list[dict], operator.add]  # 给接口返回的工具调用记录
    sources: Annotated[list[dict], operator.add]  # 这一轮 search_policy 真正查到的条款
    steps: int  # 已经调了几次模型
    gave_up: bool  # 调够了次数还没答完
    cancel_call: dict | None  # 模型发起、还没处理完的那次取消：{"id": 工具调用编号, "order_id", "arguments"}
    card: dict | None  # 给用户看的确认卡片
    decision: str | None  # 用户点的：confirm 或 reject


@dataclass(frozen=True)
class AgentContext:
    """每次请求由接口传进来，不进 checkpoint：里面有连接池和登录凭证，不能也不该存盘。"""

    llm: AsyncOpenAI
    model: str
    max_steps: int
    tools: ToolContext
    history: list[Round]
    conversation_id: UUID


@dataclass
class RoundResult:
    reply: str
    tool_calls: list[ToolCallRecord]
    sources: list[dict]
    new_round: Round | None  # 这一轮跑完了，整轮存进对话历史；暂停时为 None
    pending: dict | None = None  # 暂停时等用户确认的卡片


def assistant_message(msg) -> dict:
    """把模型的回复转成可以存进 Redis、下一轮再原样发回去的 dict。"""
    message = {"role": "assistant", "content": msg.content}
    if msg.tool_calls:
        message["tool_calls"] = [
            {"id": call.id, "type": "function", "function": {"name": call.function.name, "arguments": call.function.arguments}}
            for call in msg.tool_calls
        ]
    return message


def tool_message(call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


# ---- 节点：每个节点读 state，返回要改的那几个字段 ----


async def call_model(state: RoundState, runtime: Runtime[AgentContext]) -> dict:
    ctx = runtime.context
    steps = state.get("steps", 0)
    if steps >= ctx.max_steps:
        logger.warning("agent stopped after max_steps=%s", ctx.max_steps)
        return {"gave_up": True}
    past = [m for r in ctx.history for m in r]
    resp = await ctx.llm.chat.completions.create(
        model=ctx.model,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, *past, *state["messages"]],
        tools=TOOL_SCHEMAS,
        extra_body={"thinking": {"type": "disabled"}},
    )
    return {"messages": [assistant_message(resp.choices[0].message)], "steps": steps + 1}


async def run_tools(state: RoundState, runtime: Runtime[AgentContext]) -> dict:
    """执行模型要的工具，和以前一样。只有 cancel_order 不执行，记下来交给后面的节点。"""
    ctx = runtime.context
    messages, records, cancel_call = [], [], None
    first_new_source = len(ctx.tools.sources)
    for call in state["messages"][-1]["tool_calls"]:
        name, raw_args = call["function"]["name"], call["function"]["arguments"] or "{}"
        if name == "cancel_order":
            args, error = parse_args(CancelOrderArgs, raw_args)
            if error is None and cancel_call is None:
                # 结果要等用户确认以后才有，那时再补上这条调用对应的工具消息
                cancel_call = {"id": call["id"], "order_id": str(args.order_id), "arguments": raw_args}
                continue
            content, ok = error or ONE_AT_A_TIME, False
        else:
            content, ok = await execute_tool(name, raw_args, ctx.tools)
        logger.info("user=%s step=%s tool=%s args=%s ok=%s", ctx.tools.user.id, state["steps"], name, raw_args, ok)
        records.append({"name": name, "arguments": raw_args, "ok": ok})
        messages.append(tool_message(call["id"], content))
    sources = [{"id": c.id, "citation": c.citation, "text": c.text} for c in ctx.tools.sources[first_new_source:]]
    return {"messages": messages, "tool_calls": records, "sources": sources, "cancel_call": cancel_call}


async def prepare(state: RoundState, runtime: Runtime[AgentContext]) -> dict:
    """查一次订单，能取消就做确认卡片；不能取消就直接把原因交还给模型。"""
    ctx, call = runtime.context, state["cancel_call"]
    try:
        card, refusal = await prepare_cancel(ctx.tools, UUID(call["order_id"]))
    except DjangoError as e:
        card, refusal = None, {"canceled": False, "reason": e.message}
    if card is None:
        return {
            "messages": [tool_message(call["id"], json.dumps(refusal, ensure_ascii=False))],
            "tool_calls": [{"name": "cancel_order", "arguments": call["arguments"], "ok": False}],
            "cancel_call": None,
        }
    audit("cancel_proposed", user_id=ctx.tools.user.id, conversation_id=ctx.conversation_id, card=card)
    return {"card": card}


async def confirm(state: RoundState) -> dict:
    """在这里停下，把卡片交给接口，等用户点。

    恢复时 LangGraph 会把这个节点从头再跑一遍，interrupt() 这次直接返回用户的选择。
    所以 interrupt 之前不能有任何副作用，查订单、取消订单都放在别的节点里。
    """
    return {"decision": interrupt(state["card"])}


async def execute(state: RoundState, runtime: Runtime[AgentContext]) -> dict:
    ctx, card, call = runtime.context, state["card"], state["cancel_call"]
    if state["decision"] == "confirm":
        try:
            result, outcome = await execute_cancel(ctx.tools, card)
        except DjangoError as e:
            # 没等到商城回复时，不知道取消了没有，canceled 写 None，让模型照实说
            canceled = None if isinstance(e, DjangoOutcomeUnknown) else False
            result, outcome = {"canceled": canceled, "reason": e.message}, f"error:{type(e).__name__}"
    else:
        result, outcome = {"canceled": False, "declined": True, "reason": "用户点了「不取消」，订单保持原样"}, "rejected"
    audit(
        "cancel_decided",
        user_id=ctx.tools.user.id,
        conversation_id=ctx.conversation_id,
        card=card,
        decision=state["decision"],
        result=outcome,
    )
    return {
        "messages": [tool_message(call["id"], json.dumps(result, ensure_ascii=False))],
        "tool_calls": [{"name": "cancel_order", "arguments": call["arguments"], "ok": result["canceled"] is True}],
        "cancel_call": None,
        "card": None,
        "decision": None,
    }


# ---- 边：根据 state 决定下一步去哪 ----


def after_model(state: RoundState) -> str:
    if state.get("gave_up"):
        return END
    return "tools" if state["messages"][-1].get("tool_calls") else END


def after_tools(state: RoundState) -> str:
    return "prepare" if state.get("cancel_call") else "model"


def after_prepare(state: RoundState) -> str:
    return "confirm" if state.get("card") else "model"


def build_graph(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
    graph = StateGraph(RoundState, context_schema=AgentContext)
    graph.add_node("model", call_model)
    graph.add_node("tools", run_tools)
    graph.add_node("prepare", prepare)
    graph.add_node("confirm", confirm)
    graph.add_node("execute", execute)
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", after_model, ["tools", END])
    graph.add_conditional_edges("tools", after_tools, ["prepare", "model"])
    graph.add_conditional_edges("prepare", after_prepare, ["confirm", "model"])
    graph.add_edge("confirm", "execute")
    graph.add_edge("execute", "model")
    return graph.compile(checkpointer=checkpointer)


# ---- 接口调用的三个函数 ----


def thread_config(conversation_id: UUID) -> dict:
    # 一段对话同时最多只有一轮停着等确认，所以 checkpoint 直接用对话编号区分
    return {"configurable": {"thread_id": str(conversation_id)}}


async def pending_card(graph: CompiledStateGraph, conversation_id: UUID) -> dict | None:
    """这段对话有没有停着等用户确认的操作。"""
    snapshot = await graph.aget_state(thread_config(conversation_id))
    return snapshot.interrupts[0].value if snapshot.interrupts else None


async def start_round(graph: CompiledStateGraph, ctx: AgentContext, message: str) -> RoundResult:
    config = thread_config(ctx.conversation_id)
    # 上一轮要是跑到一半出错了，checkpoint 里会留着半截。新的一轮从空白开始
    await graph.checkpointer.adelete_thread(config["configurable"]["thread_id"])
    state = await graph.ainvoke({"messages": [{"role": "user", "content": message}]}, config, context=ctx)
    return await _finish(graph, config, state)


async def resume_round(graph: CompiledStateGraph, ctx: AgentContext, decision: str) -> RoundResult:
    config = thread_config(ctx.conversation_id)
    state = await graph.ainvoke(Command(resume=decision), config, context=ctx)
    return await _finish(graph, config, state)


async def _finish(graph: CompiledStateGraph, config: dict, state: dict) -> RoundResult:
    records = [ToolCallRecord(**r) for r in state.get("tool_calls", [])]
    sources = state.get("sources", [])
    if "__interrupt__" in state:
        card = state["__interrupt__"][0].value
        return RoundResult(reply=confirm_prompt(card), tool_calls=records, sources=sources, new_round=None, pending=card)
    # 这一轮跑完了，断点没用了。历史由接口存进 Redis
    await graph.checkpointer.adelete_thread(config["configurable"]["thread_id"])
    if state.get("gave_up"):
        # 停不下来的这一轮，中间的工具调用不存，只留用户的话和兜底回复，免得把历史塞满
        give_up_round = [state["messages"][0], {"role": "assistant", "content": GIVE_UP_REPLY}]
        return RoundResult(reply=GIVE_UP_REPLY, tool_calls=records, sources=sources, new_round=give_up_round)
    return RoundResult(
        reply=state["messages"][-1]["content"] or "", tool_calls=records, sources=sources, new_round=state["messages"]
    )
