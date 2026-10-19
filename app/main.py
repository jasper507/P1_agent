import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from openai import APIError, AsyncOpenAI

from app.agent import run_agent
from app.auth import CurrentUser, get_current_user
from app.config import Settings, get_settings
from app.django_client import DjangoClient, get_django
from app.llm import get_llm
from app.schemas import ChatRequest, ChatResponse
from app.tools import ToolContext

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # 服务关闭时关掉两个连接池。只关真正创建过的：测试里它们被换成了假的，从没创建
    if get_django.cache_info().currsize:
        await get_django().aclose()
        get_django.cache_clear()
    if get_llm.cache_info().currsize:
        await get_llm().close()
        get_llm.cache_clear()


app = FastAPI(title="P1 Agent", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    user: CurrentUser = Depends(get_current_user),
    llm: AsyncOpenAI = Depends(get_llm),
    django: DjangoClient = Depends(get_django),
    settings: Settings = Depends(get_settings),
):
    try:
        return await run_agent(
            llm,
            model=settings.deepseek_model,
            ctx=ToolContext(user=user, django=django),
            message=body.message,
            max_steps=settings.agent_max_steps,
        )
    except APIError as e:
        logger.exception("调用模型失败")
        raise HTTPException(status_code=502, detail="模型服务暂时不可用，请稍后再试") from e
