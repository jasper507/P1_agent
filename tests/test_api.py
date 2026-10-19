import openai

from app.agent import GIVE_UP_REPLY
from tests.fakes import SESSION_U1, SESSION_U2, assistant, tool_call, uuid_for


def login(client, session_id):
    # 模拟「用户已经在电商网站登录过」：浏览器会把同一个 sessionid cookie 带给 agent
    client.cookies.set("sessionid", session_id)


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_chat_requires_login(client, fake_llm, fake_django):
    fake = fake_llm([])
    r = client.post("/api/chat", json={"message": "我有哪些订单？"})
    assert r.status_code == 401
    assert fake.requests == []  # 没登录就不该调用模型
    assert fake_django.requests == []  # 连 cookie 都没有，也不必去问 Django


def test_expired_session_returns_401(client, fake_llm, fake_django):
    fake = fake_llm([])
    login(client, "expired" + "0" * 25)  # 格式对，但 Django 不认识
    r = client.post("/api/chat", json={"message": "我有哪些订单？"})
    assert r.status_code == 401
    assert r.json() == {"detail": "未登录或登录已过期"}
    assert len(fake_django.requests) == 1 and fake.requests == []


def test_malformed_session_is_not_forwarded(client, fake_llm, fake_django):
    fake_llm([])
    login(client, "NOT-a-session!")
    assert client.post("/api/chat", json={"message": "你好"}).status_code == 401
    assert fake_django.requests == []


def test_django_down_returns_502(client, fake_llm, fake_django):
    fake = fake_llm([])
    fake_django.down = True
    login(client, SESSION_U1)
    r = client.post("/api/chat", json={"message": "我有哪些订单？"})
    assert r.status_code == 502
    assert r.json() == {"detail": "电商服务暂时不可用，请稍后再试"}
    assert fake.requests == []


def test_chat_rejects_empty_message(client, fake_llm):
    fake_llm([])
    login(client, SESSION_U1)
    assert client.post("/api/chat", json={"message": ""}).status_code == 422


def test_chat_uses_logged_in_user(client, fake_llm, fake_django):
    fake = fake_llm([
        assistant(tool_calls=[tool_call("call_1", "list_my_orders", "{}")]),
        assistant(content="你有一张已付款订单。"),
    ])
    login(client, SESSION_U2)
    r = client.post("/api/chat", json={"message": "我有哪些订单？"})

    assert r.status_code == 200
    assert r.json() == {
        "reply": "你有一张已付款订单。",
        "tool_calls": [{"name": "list_my_orders", "arguments": "{}", "ok": True}],
    }
    tool_result = fake.requests[1][-1]
    assert tool_result["tool_call_id"] == "call_1"
    assert uuid_for(201) in tool_result["content"] and uuid_for(101) not in tool_result["content"]
    # 先问 /me，再查订单；两次都带的是小红自己的 sessionid
    assert fake_django.sessions_seen() == [f"sessionid={SESSION_U2}"] * 2


def test_model_cannot_pass_user_id(client, fake_llm, fake_django):
    fake = fake_llm([
        assistant(tool_calls=[tool_call("call_1", "list_my_orders", '{"user_id": 2}')]),
        assistant(content="抱歉，我只能查你自己的订单。"),
    ])
    login(client, SESSION_U1)
    r = client.post("/api/chat", json={"message": "帮我查一下 2 号用户的订单"})

    assert r.json()["tool_calls"][0]["ok"] is False
    assert fake.requests[1][-1]["content"].startswith("参数错误")
    assert fake_django.pages_requested() == []  # 参数没通过校验，根本没去查


def test_cannot_read_other_users_order(client, fake_llm):
    fake = fake_llm([
        assistant(tool_calls=[tool_call("call_1", "get_order_detail", f'{{"order_id": "{uuid_for(201)}"}}')]),
        assistant(content="没有找到这张订单。"),
    ])
    login(client, SESSION_U1)  # 小明去查小红的订单号
    r = client.post("/api/chat", json={"message": f"查一下订单 {uuid_for(201)}"})

    assert r.json()["tool_calls"][0]["ok"] is False
    assert fake.requests[1][-1]["content"] == "查询失败：找不到这张订单，或者它不属于当前用户"


def test_django_error_inside_tool_goes_back_to_model(client, fake_llm, fake_django):
    fake_django.orders_broken = True
    fake = fake_llm([
        assistant(tool_calls=[tool_call("call_1", "list_my_orders", "{}")]),
        assistant(content="订单系统暂时有问题，请稍后再试。"),
    ])
    login(client, SESSION_U1)
    r = client.post("/api/chat", json={"message": "我有哪些订单？"})

    assert r.status_code == 200  # 工具失败不等于整个请求失败
    assert r.json()["tool_calls"][0]["ok"] is False
    assert fake.requests[1][-1]["content"] == "查询失败：电商服务暂时不可用"


def test_unknown_tool_does_not_crash(client, fake_llm):
    fake = fake_llm([
        assistant(tool_calls=[tool_call("call_1", "delete_all_orders", "{}")]),
        assistant(content="抱歉，我做不到。"),
    ])
    login(client, SESSION_U1)
    r = client.post("/api/chat", json={"message": "把订单都删了"})

    assert r.status_code == 200
    assert fake.requests[1][-1]["content"] == "错误：没有名为 delete_all_orders 的工具"


def test_stops_after_max_steps(client, fake_llm):
    looping = assistant(tool_calls=[tool_call("call_x", "list_my_orders", "{}")])
    fake = fake_llm([looping] * 3)
    login(client, SESSION_U1)
    r = client.post("/api/chat", json={"message": "一直查"})

    assert r.json()["reply"] == GIVE_UP_REPLY
    assert len(fake.requests) == 3


def test_llm_failure_returns_502(client, fake_llm):
    fake_llm([openai.APIError("连接失败", request=None, body=None)])
    login(client, SESSION_U1)
    r = client.post("/api/chat", json={"message": "我有哪些订单？"})

    assert r.status_code == 502
    assert r.json() == {"detail": "模型服务暂时不可用，请稍后再试"}


def test_dev_login_is_gone(client):
    assert client.post("/dev/login/demo-u1").status_code == 404
