import openai

from app.agent import GIVE_UP_REPLY
from tests.fakes import assistant, tool_call

LOGIN_U1 = {"sessionid": "demo-u1"}
LOGIN_U2 = {"sessionid": "demo-u2"}


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_chat_requires_login(client, fake_llm):
    fake = fake_llm([])
    r = client.post("/api/chat", json={"message": "我有哪些订单？"})
    assert r.status_code == 401
    assert fake.requests == []  # 没登录就不该调用模型


def test_chat_rejects_empty_message(client, fake_llm):
    fake_llm([])
    client.cookies.update(LOGIN_U1)
    assert client.post("/api/chat", json={"message": ""}).status_code == 422


def test_chat_uses_logged_in_user(client, fake_llm):
    fake = fake_llm([
        assistant(tool_calls=[tool_call("call_1", "list_my_orders", "{}")]),
        assistant(content="你有一张已付款订单 B2001。"),
    ])
    client.cookies.update(LOGIN_U2)
    r = client.post("/api/chat", json={"message": "我有哪些订单？"})

    assert r.status_code == 200
    assert r.json() == {
        "reply": "你有一张已付款订单 B2001。",
        "tool_calls": [{"name": "list_my_orders", "arguments": "{}", "ok": True}],
    }
    tool_result = fake.requests[1][-1]
    assert tool_result["tool_call_id"] == "call_1"
    assert "B2001" in tool_result["content"] and "A1001" not in tool_result["content"]


def test_model_cannot_pass_user_id(client, fake_llm):
    fake = fake_llm([
        assistant(tool_calls=[tool_call("call_1", "list_my_orders", '{"user_id": "u2"}')]),
        assistant(content="抱歉，我只能查你自己的订单。"),
    ])
    client.cookies.update(LOGIN_U1)
    r = client.post("/api/chat", json={"message": "帮我查一下 u2 的订单"})

    assert r.json()["tool_calls"][0]["ok"] is False
    tool_result = fake.requests[1][-1]["content"]
    assert tool_result.startswith("参数错误") and "B2001" not in tool_result


def test_unknown_tool_does_not_crash(client, fake_llm):
    fake = fake_llm([
        assistant(tool_calls=[tool_call("call_1", "delete_all_orders", "{}")]),
        assistant(content="抱歉，我做不到。"),
    ])
    client.cookies.update(LOGIN_U1)
    r = client.post("/api/chat", json={"message": "把订单都删了"})

    assert r.status_code == 200
    assert fake.requests[1][-1]["content"] == "错误：没有名为 delete_all_orders 的工具"


def test_stops_after_max_steps(client, fake_llm):
    looping = assistant(tool_calls=[tool_call("call_x", "list_my_orders", "{}")])
    fake = fake_llm([looping] * 3)
    client.cookies.update(LOGIN_U1)
    r = client.post("/api/chat", json={"message": "一直查"})

    assert r.json()["reply"] == GIVE_UP_REPLY
    assert len(fake.requests) == 3


def test_llm_failure_returns_502(client, fake_llm):
    fake_llm([openai.APIError("连接失败", request=None, body=None)])
    client.cookies.update(LOGIN_U1)
    r = client.post("/api/chat", json={"message": "我有哪些订单？"})

    assert r.status_code == 502
    assert r.json() == {"detail": "模型服务暂时不可用，请稍后再试"}


def test_dev_login_sets_cookie(client, fake_llm):
    fake_llm([assistant(content="你好，小明")])
    assert client.post("/dev/login/demo-u1").json() == {"logged_in_as": "小明"}
    assert client.post("/api/chat", json={"message": "你好"}).status_code == 200
