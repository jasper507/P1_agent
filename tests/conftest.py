import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.llm import get_llm
from app.main import app
from tests.fakes import FakeLLM


@pytest.fixture
def client():
    # 测试不读真的 .env：用假 key，最多跑 3 轮
    app.dependency_overrides[get_settings] = lambda: Settings(
        deepseek_api_key="test-key", agent_max_steps=3, _env_file=None
    )
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def fake_llm():
    """用法：fake = fake_llm([回复1, 回复2])。把接口里的 LLM 换成假的。"""

    def use(replies):
        fake = FakeLLM(replies)
        app.dependency_overrides[get_llm] = lambda: fake
        return fake

    return use
