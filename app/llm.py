from functools import lru_cache

from openai import AsyncOpenAI

from app.config import get_settings


@lru_cache
def get_llm() -> AsyncOpenAI:
    """整个进程共用一个客户端：它内部有连接池，每个请求都新建一个会白白浪费连接。"""
    settings = get_settings()
    return AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        timeout=settings.llm_timeout_seconds,
        max_retries=2,
    )
