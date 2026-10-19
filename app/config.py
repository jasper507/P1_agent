from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """从环境变量和 .env 读取配置。字段名不区分大小写：deepseek_api_key 对应 DEEPSEEK_API_KEY。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    deepseek_api_key: str
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    llm_timeout_seconds: float = 30
    agent_max_steps: int = 5


@lru_cache
def get_settings() -> Settings:
    return Settings()
