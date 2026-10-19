import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from openai import APIError, AsyncOpenAI

from app.agent import run_agent
from app.auth import CurrentUser, get_current_user
from app.config import Settings, get_settings
from app.conversations import ConversationError, ConversationStore, get_conversations
from app.django_client import DjangoClient, get_django
from app.llm import get_llm
from app.schemas import ChatRequest, ChatResponse
from app.tools import ToolContext

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
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
    settings: Settings = Depends(get_settings),
):
    # 没带编号就是新对话：编号由服务端生成，第一轮跑完才真正存进 Redis
    conversation_id = body.conversation_id or uuid4()
    async with conversations.lock(conversation_id):
        history = [] if body.conversation_id is None else await conversations.load(conversation_id, user.id)
        try:
            # 一轮必须在锁过期之前结束，所以给它一个总的时限
            async with asyncio.timeout(settings.round_timeout_seconds):
                result = await run_agent(
                    llm,
                    model=settings.deepseek_model,
                    ctx=ToolContext(user=user, django=django),
                    history=history,
                    message=body.message,
                    max_steps=settings.agent_max_steps,
                )
        except APIError as e:
            # 这一轮只跑了一半，不存，用户重发即可
            logger.exception("调用模型失败")
            raise HTTPException(status_code=502, detail="模型服务暂时不可用，请稍后再试") from e
        except TimeoutError as e:
            logger.warning("一轮超过 %s 秒，放弃", settings.round_timeout_seconds)
            raise HTTPException(status_code=504, detail="这次处理太久了，请稍后再试") from e
        await conversations.save_round(conversation_id, user.id, history, result.new_round)

    return ChatResponse(conversation_id=conversation_id, reply=result.reply, tool_calls=result.tool_calls)
