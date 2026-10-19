from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """从环境变量和 .env 读取配置。字段名不区分大小写：deepseek_api_key 对应 DEEPSEEK_API_KEY。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    deepseek_api_key: str
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    llm_timeout_seconds: float = 30
    agent_max_steps: int = 5

    # 电商项目（Django）的地址。本地 docker compose 起的栈默认在 8080
    django_base_url: str = "http://127.0.0.1:8080"
    django_timeout_seconds: float = 5

    # 多轮对话的历史存在 Redis。本地用 compose.yaml 起一个，见 README 第 3 步
    redis_url: str = "redis://127.0.0.1:6379/0"
    conversation_ttl_seconds: int = 30 * 60  # 30 分钟没有新消息就过期
    conversation_max_rounds: int = 10  # 只留最近 10 轮
    conversation_lock_seconds: int = 120  # 一轮最多占着对话这么久，进程崩了锁也会自己消失
    round_timeout_seconds: float = 90  # 一轮最多跑这么久，超时就放弃。必须比对话锁短，见 ADR 0009

    @model_validator(mode="after")
    def round_ends_before_lock_expires(self):
        # 锁要是先过期，下一条消息就能拿到锁，两轮同时跑，后存的那轮会把先存的覆盖掉
        if self.round_timeout_seconds >= self.conversation_lock_seconds:
            raise ValueError("round_timeout_seconds 必须小于 conversation_lock_seconds")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
