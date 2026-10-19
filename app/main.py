import logging

from fastapi import Depends, FastAPI, HTTPException, Response
from openai import APIError, AsyncOpenAI

from app.agent import run_agent
from app.auth import FAKE_SESSIONS, CurrentUser, get_current_user
from app.config import Settings, get_settings
from app.llm import get_llm
from app.schemas import ChatRequest, ChatResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="P1 Agent")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    user: CurrentUser = Depends(get_current_user),
    llm: AsyncOpenAI = Depends(get_llm),
    settings: Settings = Depends(get_settings),
):
    try:
        return await run_agent(
            llm,
            model=settings.deepseek_model,
            user_id=user.id,
            message=body.message,
            max_steps=settings.agent_max_steps,
        )
    except APIError as e:
        logger.exception("调用模型失败")
        raise HTTPException(status_code=502, detail="模型服务暂时不可用，请稍后再试") from e


@app.post("/dev/login/{session_id}", tags=["dev"])
def dev_login(session_id: str, response: Response):
    """只用于本地调试：给浏览器种一个假 cookie，方便在 /docs 页面直接试 /api/chat。第 2 步接入 Django 登录后删掉。"""
    if session_id not in FAKE_SESSIONS:
        raise HTTPException(status_code=404, detail="没有这个测试账号")
    response.set_cookie("sessionid", session_id, httponly=True, samesite="lax")
    return {"logged_in_as": FAKE_SESSIONS[session_id].username}
