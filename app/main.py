import asyncio
import logging
from collections.abc import Awaitable
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph.state import CompiledStateGraph
from openai import APIError, AsyncOpenAI

from app.agent import AgentContext, RoundResult, build_graph, pending_card, resume_round, start_round
from app.auth import CsrfTokens, CurrentUser, get_csrf_tokens, get_current_user
from app.config import Settings, get_settings
from app.conversations import ConversationError, ConversationStore, Round, get_conversations
from app.django_client import DjangoClient, get_django
from app.llm import get_llm
from app.policy_search import PolicyIndex, get_policy_index
from app.schemas import ChatRequest, ChatResponse, ConfirmRequest, PendingAction, Source
from app.tools import ToolContext

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时就把政策索引建好：第一个用户不用等，模型下载失败也在启动时就暴露。
    # 测试里 get_policy_index 被换成了假的，这里拿到的也是假的
    app.dependency_overrides.get(get_policy_index, get_policy_index)()
    settings = app.dependency_overrides.get(get_settings, get_settings)()
    settings.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_path)) as checkpointer:
        await checkpointer.setup()  # 建表。不先建的话，删除断点会报「没有这张表」
        app.state.graph = build_graph(checkpointer)
        yield
    # 服务关闭时关掉三个连接池。只关真正创建过的：测试里它们被换成了假的，从没创建
    if get_django.cache_info().currsize:
        await get_django().aclose()
        get_django.cache_clear()
    if get_llm.cache_info().currsize:
        await get_llm().close()
        get_llm.cache_clear()
    if get_conversations.cache_info().currsize:
        await get_conversations().aclose()
        get_conversations.cache_clear()


app = FastAPI(title="P1 Agent", lifespan=lifespan)


@app.exception_handler(ConversationError)
async def conversation_error(request: Request, exc: ConversationError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


def get_graph(request: Request) -> CompiledStateGraph:
    return request.app.state.graph


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    user: CurrentUser = Depends(get_current_user),
    llm: AsyncOpenAI = Depends(get_llm),
    django: DjangoClient = Depends(get_django),
    conversations: ConversationStore = Depends(get_conversations),
    policies: PolicyIndex = Depends(get_policy_index),
    settings: Settings = Depends(get_settings),
    graph: CompiledStateGraph = Depends(get_graph),
):
    # 没带编号就是新对话：编号由服务端生成，第一轮跑完才真正存进 Redis
    conversation_id = body.conversation_id or uuid4()
    async with conversations.lock(conversation_id):
        history = [] if body.conversation_id is None else await conversations.load(conversation_id, user.id)
        if body.conversation_id is not None and await pending_card(graph, conversation_id):
            raise HTTPException(status_code=409, detail="请先处理上面的确认：点「确认取消」或「不取消」")
        ctx = agent_context(llm, settings, ToolContext(user=user, django=django, policies=policies), history, conversation_id)
        result = await run_round(start_round(graph, ctx, body.message), settings)
        await save(conversations, conversation_id, user.id, history, result)
    return chat_response(conversation_id, result)


@app.post("/api/chat/confirm", response_model=ChatResponse)
async def confirm(
    body: ConfirmRequest,
    user: CurrentUser = Depends(get_current_user),
    csrf: CsrfTokens = Depends(get_csrf_tokens),
    llm: AsyncOpenAI = Depends(get_llm),
    django: DjangoClient = Depends(get_django),
    conversations: ConversationStore = Depends(get_conversations),
    policies: PolicyIndex = Depends(get_policy_index),
    settings: Settings = Depends(get_settings),
    graph: CompiledStateGraph = Depends(get_graph),
):
    """用户在确认卡片上点了「确认取消」或「不取消」。接着跑停下的那一轮，跑完和 /api/chat 返回的一样。"""
    async with conversations.lock(body.conversation_id):
        # 不是自己的对话、对话已经过期，都是 404，和 /api/chat 一样
        history = await conversations.load(body.conversation_id, user.id)
        card = await pending_card(graph, body.conversation_id)
        # 编号对不上：比如连点了两次，第一次已经处理完了
        if card is None or card["action_id"] != body.action_id:
            raise HTTPException(status_code=404, detail="这个确认已经失效，可能已经处理过了")
        tools = ToolContext(user=user, django=django, policies=policies, csrf=csrf)
        ctx = agent_context(llm, settings, tools, history, body.conversation_id)
        result = await run_round(resume_round(graph, ctx, body.decision), settings)
        await save(conversations, body.conversation_id, user.id, history, result)
    return chat_response(body.conversation_id, result)


def agent_context(llm, settings: Settings, tools: ToolContext, history: list[Round], conversation_id: UUID) -> AgentContext:
    return AgentContext(
        llm=llm,
        model=settings.deepseek_model,
        max_steps=settings.agent_max_steps,
        tools=tools,
        history=history,
        conversation_id=conversation_id,
    )


async def run_round(round_: Awaitable[RoundResult], settings: Settings) -> RoundResult:
    try:
        # 一轮必须在锁过期之前结束，所以给它一个总的时限
        async with asyncio.timeout(settings.round_timeout_seconds):
            return await round_
    except APIError as e:
        # 这一轮只跑了一半，不存，用户重发即可
        logger.exception("调用模型失败")
        raise HTTPException(status_code=502, detail="模型服务暂时不可用，请稍后再试") from e
    except TimeoutError as e:
        logger.warning("一轮超过 %s 秒，放弃", settings.round_timeout_seconds)
        raise HTTPException(status_code=504, detail="这次处理太久了，请稍后再试") from e


async def save(conversations: ConversationStore, conversation_id: UUID, user_id: int, history: list[Round], result: RoundResult):
    if result.new_round is None:
        await conversations.touch(conversation_id, user_id, history)  # 停下来等确认，这一轮还没完
    else:
        await conversations.save_round(conversation_id, user_id, history, result.new_round)


def chat_response(conversation_id: UUID, result: RoundResult) -> ChatResponse:
    # 同一条款被查到两次，只返回一次
    sources = {(s["id"], s["text"]): Source(**s) for s in result.sources}
    pending = PendingAction.model_validate(result.pending) if result.pending else None
    return ChatResponse(
        conversation_id=conversation_id,
        reply=result.reply,
        tool_calls=result.tool_calls,
        sources=list(sources.values()),
        pending_action=pending,
    )
