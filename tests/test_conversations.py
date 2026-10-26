import asyncio
import json
from uuid import uuid4

import fakeredis
import openai
import pytest

from app.agent import GIVE_UP_REPLY
from app.config import Settings, get_settings
from app.conversations import ConversationBusy, ConversationNotFound, ConversationStore
from app.main import app
from tests.fakes import HANG, SESSION_U1, SESSION_U1_AGAIN, SESSION_U2, assistant, tool_call, uuid_for


def login(client, session_id):
    client.cookies.set("sessionid", session_id)


def chat(client, message, conversation_id=None):
    body = {"message": message}
    if conversation_id:
        body["conversation_id"] = conversation_id
    return client.post("/api/chat", json=body)


def stored_rounds(redis_db, conversation_id):
    return json.loads(redis_db.get(f"conversation:{conversation_id}"))["rounds"]


def test_second_message_sees_first_round(client, fake_llm):
    fake = fake_llm([
        assistant(tool_calls=[tool_call("call_1", "list_my_orders", "{}")]),
        assistant(content="你有两张订单。"),
        assistant(content="第二张是已发货的那张。"),
    ])
    login(client, SESSION_U1)
    first = chat(client, "我有哪些订单？")
    conversation_id = first.json()["conversation_id"]
    second = chat(client, "第二张呢？", conversation_id)

    assert second.status_code == 200
    assert second.json()["conversation_id"] == conversation_id
    sent = fake.requests[2]  # 第二轮发给模型的消息
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert sent[-1]["content"] == "第二张呢？"
    # 工具调用和它的结果一起留着，并且配得上对，模型接口才不会报错
    assert sent[2]["tool_calls"][0]["id"] == sent[3]["tool_call_id"] == "call_1"
    assert uuid_for(102) in sent[3]["content"]  # 上一轮查到的订单号还在


def test_new_conversation_gets_its_own_id(client, fake_llm):
    fake_llm([assistant(content="你好"), assistant(content="你好")])
    login(client, SESSION_U1)
    a = chat(client, "你好").json()["conversation_id"]
    b = chat(client, "你好").json()["conversation_id"]
    assert a != b


def test_cannot_continue_other_users_conversation(client, fake_llm, redis_db):
    fake = fake_llm([assistant(content="你的订单号是……")])
    login(client, SESSION_U1)
    conversation_id = chat(client, "我有哪些订单？").json()["conversation_id"]

    login(client, SESSION_U2)  # 小红拿着小明的对话编号来
    r = chat(client, "上面说了什么？", conversation_id)
    assert r.status_code == 404
    assert r.json() == {"detail": "对话不存在或已过期"}
    assert len(fake.requests) == 1  # 根本没走到模型
    assert len(stored_rounds(redis_db, conversation_id)) == 1  # 小明的历史没被动过


def test_unknown_or_expired_conversation_is_404(client, fake_llm):
    fake = fake_llm([])
    login(client, SESSION_U1)
    r = chat(client, "还在吗？", str(uuid4()))
    assert r.status_code == 404 and fake.requests == []


def test_conversation_belongs_to_user_not_login_session(client, fake_llm):
    fake_llm([assistant(content="好的"), assistant(content="接着聊")])
    login(client, SESSION_U1)
    conversation_id = chat(client, "你好").json()["conversation_id"]
    login(client, SESSION_U1_AGAIN)  # 重新登录，sessionid 变了，但还是小明
    assert chat(client, "我回来了", conversation_id).status_code == 200


def test_bad_conversation_id_is_rejected(client, fake_llm):
    fake_llm([])
    login(client, SESSION_U1)
    assert chat(client, "你好", "../../etc").status_code == 422


def test_busy_conversation_returns_409(client, fake_llm, redis_db):
    fake = fake_llm([assistant(content="好的")])
    login(client, SESSION_U1)
    conversation_id = chat(client, "你好").json()["conversation_id"]

    redis_db.set(f"conversation:{conversation_id}:lock", "another-request", ex=120)  # 上一条还在跑
    r = chat(client, "再问一句", conversation_id)
    assert r.status_code == 409
    assert r.json() == {"detail": "上一条还在处理，请稍后再发"}
    assert len(fake.requests) == 1
    assert redis_db.get(f"conversation:{conversation_id}:lock") == "another-request"  # 别人的锁没被删


def test_lock_is_released_after_each_request(client, fake_llm, redis_db):
    fake_llm([assistant(content="一"), openai.APIError("连接失败", request=None, body=None), assistant(content="三")])
    login(client, SESSION_U1)
    conversation_id = chat(client, "第一句").json()["conversation_id"]
    assert chat(client, "第二句", conversation_id).status_code == 502
    assert redis_db.keys("*:lock") == []  # 出错也要放锁，不然这段对话就卡住了
    assert chat(client, "第三句", conversation_id).status_code == 200


def test_failed_round_is_not_saved(client, fake_llm, redis_db):
    fake = fake_llm([
        assistant(content="你好"),
        assistant(tool_calls=[tool_call("call_1", "list_my_orders", "{}")]),
        openai.APIError("连接失败", request=None, body=None),  # 第二轮跑到一半，模型挂了
        assistant(content="你好，请问有什么可以帮你？"),
    ])
    login(client, SESSION_U1)
    conversation_id = chat(client, "你好").json()["conversation_id"]
    assert chat(client, "我有哪些订单？", conversation_id).status_code == 502
    assert len(stored_rounds(redis_db, conversation_id)) == 1

    chat(client, "在吗？", conversation_id)
    sent = fake.requests[3]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]  # 没有半截的工具调用


def test_first_round_failure_saves_nothing(client, fake_llm, redis_db):
    fake_llm([openai.APIError("连接失败", request=None, body=None)])
    login(client, SESSION_U1)
    assert chat(client, "你好").status_code == 502
    assert redis_db.keys("conversation:*") == []


def test_keeps_only_latest_rounds(client, fake_llm, redis_db):
    # 测试里最多留 3 轮（见 conftest）
    fake = fake_llm([assistant(content=f"回复{i}") for i in range(1, 6)])
    login(client, SESSION_U1)
    conversation_id = chat(client, "第1句").json()["conversation_id"]
    for i in range(2, 6):
        chat(client, f"第{i}句", conversation_id)

    assert [r[0]["content"] for r in stored_rounds(redis_db, conversation_id)] == ["第3句", "第4句", "第5句"]
    sent = fake.requests[4]  # 第 5 轮发出去的：只有第 2–4 轮，再加这一句
    assert [m["content"] for m in sent if m["role"] == "user"] == ["第2句", "第3句", "第4句", "第5句"]


def test_each_message_restarts_the_expiry_timer(client, fake_llm, redis_db):
    fake_llm([assistant(content="一"), assistant(content="二")])
    login(client, SESSION_U1)
    conversation_id = chat(client, "第一句").json()["conversation_id"]
    key = f"conversation:{conversation_id}"
    redis_db.expire(key, 60)  # 假装快过期了
    chat(client, "第二句", conversation_id)
    assert redis_db.ttl(key) > 1700


def test_give_up_round_keeps_only_question_and_reply(client, fake_llm, redis_db):
    looping = assistant(tool_calls=[tool_call("call_x", "list_my_orders", "{}")])
    fake_llm([looping] * 3)
    login(client, SESSION_U1)
    conversation_id = chat(client, "一直查").json()["conversation_id"]
    assert stored_rounds(redis_db, conversation_id) == [[
        {"role": "user", "content": "一直查"},
        {"role": "assistant", "content": GIVE_UP_REPLY},
    ]]


def test_round_that_runs_too_long_is_abandoned(client, fake_llm, redis_db):
    fake_llm([assistant(content="你好"), HANG, assistant(content="在的")])
    login(client, SESSION_U1)
    conversation_id = chat(client, "你好").json()["conversation_id"]
    # 只给卡住的这一轮 0.2 秒。其他轮不限：第一次跑要加载 LangGraph，慢的电脑上会超过 0.2 秒
    normal = app.dependency_overrides[get_settings]
    app.dependency_overrides[get_settings] = lambda: normal().model_copy(update={"round_timeout_seconds": 0.2})
    r = chat(client, "我有哪些订单？", conversation_id)
    assert r.status_code == 504
    assert r.json() == {"detail": "这次处理太久了，请稍后再试"}
    assert len(stored_rounds(redis_db, conversation_id)) == 1  # 没跑完的这轮不存
    assert redis_db.keys("*:lock") == []
    app.dependency_overrides[get_settings] = normal
    assert chat(client, "在吗？", conversation_id).status_code == 200


def test_round_timeout_must_be_shorter_than_lock():
    with pytest.raises(ValueError, match="round_timeout_seconds"):
        Settings(deepseek_api_key="test-key", round_timeout_seconds=120, conversation_lock_seconds=120, _env_file=None)


def test_redis_down_returns_503(client, fake_llm, redis_server):
    fake = fake_llm([])
    redis_server.connected = False
    login(client, SESSION_U1)
    r = chat(client, "你好")
    assert r.status_code == 503
    assert r.json() == {"detail": "对话服务暂时不可用，请稍后再试"}
    assert fake.requests == []


# ---- 直接测存储本身 ----

@pytest.fixture
def store_and_db():
    server = fakeredis.FakeServer()
    store = ConversationStore(
        fakeredis.FakeAsyncRedis(server=server, decode_responses=True),
        ttl_seconds=1800, max_rounds=3, lock_seconds=120,
    )
    return store, fakeredis.FakeRedis(server=server, decode_responses=True)


def test_lock_does_not_delete_someone_elses_lock(store_and_db):
    store, db = store_and_db
    conversation_id = uuid4()
    key = f"conversation:{conversation_id}:lock"

    async def scenario():
        async with store.lock(conversation_id):
            # 这一轮跑得太久，锁过期了，下一条消息拿到了新锁
            db.set(key, "next-request", ex=120)
        assert db.get(key) == "next-request"  # 退出时不能把别人的锁删掉

    asyncio.run(scenario())


def test_second_lock_on_same_conversation_fails(store_and_db):
    store, db = store_and_db
    conversation_id = uuid4()

    async def scenario():
        async with store.lock(conversation_id):
            with pytest.raises(ConversationBusy):
                async with store.lock(conversation_id):
                    pass
        async with store.lock(conversation_id):  # 第一把放掉以后又能拿到
            pass

    asyncio.run(scenario())


def test_load_checks_owner(store_and_db):
    store, db = store_and_db
    conversation_id = uuid4()

    async def scenario():
        await store.save_round(conversation_id, 1, [], [{"role": "user", "content": "你好"}])
        assert await store.load(conversation_id, 1) == [[{"role": "user", "content": "你好"}]]
        with pytest.raises(ConversationNotFound):
            await store.load(conversation_id, 2)

    asyncio.run(scenario())
