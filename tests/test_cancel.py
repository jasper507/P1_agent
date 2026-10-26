"""取消订单：模型只能提出，用户在页面上点了确认，agent 才去商城取消。"""
import json
import logging

from fastapi.testclient import TestClient

from app.main import app
from tests.fakes import CSRF_TOKEN, SESSION_U1, SESSION_U2, assistant, make_order, tool_call, uuid_for


def login(client, session_id):
    # 浏览器在商城登录后有两个 cookie：sessionid 认人，csrftoken 给写操作做校验
    client.cookies.set("sessionid", session_id)
    client.cookies.set("csrftoken", CSRF_TOKEN)


def chat(client, message, conversation_id=None):
    body = {"message": message}
    if conversation_id:
        body["conversation_id"] = conversation_id
    return client.post("/api/chat", json=body)


def confirm(client, first, decision="confirm", csrf=CSRF_TOKEN, action_id=None):
    """在 first（停下来的那次回复）的确认卡片上点按钮。前端把 csrftoken cookie 的值放进 X-CSRFToken 头。"""
    body = {
        "conversation_id": first["conversation_id"],
        "action_id": action_id or first["pending_action"]["action_id"],
        "decision": decision,
    }
    return client.post("/api/chat/confirm", json=body, headers={"X-CSRFToken": csrf} if csrf else {})


def cancel(order_n, call_id="call_c", content=None):
    """模型的一次回复：调用 cancel_order 取消第 order_n 号订单。"""
    return assistant(content=content, tool_calls=[tool_call(call_id, "cancel_order", f'{{"order_id": "{uuid_for(order_n)}"}}')])


def tool_result(fake, request_index=1):
    """第 request_index 次发给模型的请求里，最后一条工具结果。"""
    return json.loads(fake.requests[request_index][-1]["content"])


def stored_rounds(redis_db, conversation_id):
    return json.loads(redis_db.get(f"conversation:{conversation_id}"))["rounds"]


# ---- 正常流程 ----


def test_model_can_only_propose(client, fake_llm, fake_django, redis_db):
    fake = fake_llm([cancel(101, content="好的，我来取消那张 999 元的订单。")])
    login(client, SESSION_U1)
    body = chat(client, "把那张没付款的订单取消了").json()

    card = body["pending_action"]
    # 卡片上的字来自商城的订单，不是模型说的「999 元」
    assert card["order_id"] == uuid_for(101)
    assert card["items"] == ["T恤 x1"] and card["total"] == "199.00" and card["status_text"] == "待付款"
    assert card["effect"] == "取消后订单关闭，不会扣款。"
    assert body["reply"].startswith("请确认是否取消这张订单")
    assert fake_django.cancels() == []  # 只查了订单，没有取消
    assert len(fake.requests) == 1  # 停在这里，不再问模型
    assert stored_rounds(redis_db, body["conversation_id"]) == []  # 第一轮就停下来，对话也已经记在小明名下


def test_confirm_cancels_and_model_reports(client, fake_llm, fake_django, redis_db):
    fake = fake_llm([cancel(101), assistant(content="已经帮你取消了。")])
    login(client, SESSION_U1)
    first = chat(client, "取消那张没付款的").json()
    r = confirm(client, first)

    assert r.status_code == 200
    body = r.json()
    assert body["reply"] == "已经帮你取消了。" and body["pending_action"] is None
    assert body["tool_calls"] == [{"name": "cancel_order", "arguments": f'{{"order_id": "{uuid_for(101)}"}}', "ok": True}]
    assert fake_django.orders[1][0]["status"] == "CANCELED" and len(fake_django.cancels()) == 1
    assert tool_result(fake)["canceled"] is True
    # 停下前和恢复后的消息合成完整的一轮存进历史，工具调用和结果配得上对
    [saved] = stored_rounds(redis_db, first["conversation_id"])
    assert [m["role"] for m in saved] == ["user", "assistant", "tool", "assistant"]
    assert saved[1]["tool_calls"][0]["id"] == saved[2]["tool_call_id"] == "call_c"


def test_paid_order_card_mentions_refund(client, fake_llm, fake_django):
    fake_llm([cancel(201), assistant(content="已取消，钱会原路退回。")])
    login(client, SESSION_U2)
    first = chat(client, "帮我取消帆布鞋那单").json()
    assert first["pending_action"]["effect"] == "取消后全额退款 ¥59.00，按原支付方式退回。"
    confirm(client, first)
    assert fake_django.refunds == [uuid_for(201)]


def test_reject_leaves_order_alone(client, fake_llm, fake_django):
    fake = fake_llm([cancel(101), assistant(content="好的，订单给你留着。")])
    login(client, SESSION_U1)
    first = chat(client, "取消那张没付款的").json()
    r = confirm(client, first, decision="reject")

    assert r.json()["reply"] == "好的，订单给你留着。"
    # 联调时模型把「不取消」说成了「没有取消成功」，所以结果里明说这是用户自己选的
    assert tool_result(fake) == {"canceled": False, "declined": True, "reason": "用户点了「不取消」，订单保持原样"}
    assert fake_django.cancels() == [] and fake_django.orders[1][0]["status"] == "PENDING_PAYMENT"


def test_shipped_order_gets_no_card(client, fake_llm, fake_django):
    fake = fake_llm([cancel(102), assistant(content="这张已经发货了，不能取消。")])
    login(client, SESSION_U1)
    body = chat(client, "取消那张耳机").json()

    assert body["pending_action"] is None and body["reply"] == "这张已经发货了，不能取消。"
    assert tool_result(fake)["reason"].startswith("这张订单已经发货")
    assert fake_django.cancels() == []


def test_waiting_survives_restart(client, fake_llm, fake_django):
    fake_llm([cancel(101), assistant(content="已取消。")])
    login(client, SESSION_U1)
    first = chat(client, "取消那张没付款的").json()

    with TestClient(app) as restarted:  # 服务重启：新的进程、新的图，读的还是同一个 checkpoint 文件
        login(restarted, SESSION_U1)
        assert confirm(restarted, first).json()["reply"] == "已取消。"
    assert fake_django.orders[1][0]["status"] == "CANCELED"


# ---- 等确认的时候 ----


def test_new_message_while_waiting_is_409(client, fake_llm):
    fake = fake_llm([cancel(101)])
    login(client, SESSION_U1)
    first = chat(client, "取消那张没付款的").json()
    r = chat(client, "算了，先问别的", first["conversation_id"])

    assert r.status_code == 409
    assert r.json() == {"detail": "请先处理上面的确认：点「确认取消」或「不取消」"}
    assert len(fake.requests) == 1


def test_double_click_cancels_once(client, fake_llm, fake_django):
    fake_llm([cancel(101), assistant(content="已取消。")])
    login(client, SESSION_U1)
    first = chat(client, "取消那张没付款的").json()
    assert confirm(client, first).status_code == 200
    r = confirm(client, first)  # 连点了第二次

    assert r.status_code == 404 and r.json() == {"detail": "这个确认已经失效，可能已经处理过了"}
    assert len(fake_django.cancels()) == 1


def test_wrong_action_id_is_404(client, fake_llm, fake_django):
    fake_llm([cancel(101), assistant(content="已取消。")])
    login(client, SESSION_U1)
    first = chat(client, "取消那张没付款的").json()
    assert confirm(client, first, action_id="0" * 32).status_code == 404
    assert fake_django.cancels() == []
    assert confirm(client, first).status_code == 200  # 卡片还在，点对的那个照样能用


def test_other_user_cannot_confirm(client, fake_llm, fake_django):
    fake_llm([cancel(101)])
    login(client, SESSION_U1)
    first = chat(client, "取消那张没付款的").json()
    login(client, SESSION_U2)  # 小红拿着小明的对话编号和操作编号来点确认
    r = confirm(client, first)

    assert r.status_code == 404 and r.json() == {"detail": "对话不存在或已过期"}
    assert fake_django.cancels() == []


def test_order_changed_before_confirm_is_not_cancelled(client, fake_llm, fake_django):
    fake = fake_llm([cancel(101), assistant(content="订单状态变了，没有取消。")])
    login(client, SESSION_U1)
    first = chat(client, "取消那张没付款的").json()
    fake_django.orders[1][0]["status"] = "PAID"  # 看卡片的时候，在另一个页面付了款
    confirm(client, first)

    assert fake_django.cancels() == []  # 用户同意的是「不扣钱地关掉」，现在取消会变成退款，不是一回事
    reason = tool_result(fake)["reason"]
    assert "「待付款」" in reason and "「已付款待发货」" in reason


def test_one_cancel_at_a_time(client, fake_llm, fake_django):
    fake_django.orders[1].append(make_order(uuid_for(103), "PENDING_PAYMENT"))
    two_at_once = assistant(tool_calls=[
        tool_call("c1", "cancel_order", f'{{"order_id": "{uuid_for(101)}"}}'),
        tool_call("c2", "cancel_order", f'{{"order_id": "{uuid_for(103)}"}}'),
    ])
    fake = fake_llm([two_at_once, assistant(content="第一张取消了，第二张要再确认一次。")])
    login(client, SESSION_U1)
    first = chat(client, "两张都取消").json()
    assert first["pending_action"]["order_id"] == uuid_for(101)
    confirm(client, first)

    sent = fake.requests[1]
    results = {m["tool_call_id"]: m["content"] for m in sent if m["role"] == "tool"}
    assert results["c2"].startswith("错误：一次只能取消一张订单")  # 每个调用都有结果，模型接口才不会报错
    assert json.loads(results["c1"])["canceled"] is True
    assert [r.url.path for r in fake_django.cancels()] == [f"/api/orders/{uuid_for(101)}/cancel/"]


def test_bad_order_id_never_pauses(client, fake_llm, fake_django):
    fake = fake_llm([
        assistant(tool_calls=[tool_call("c1", "cancel_order", '{"order_id": "../../auth/me"}')]),
        assistant(content="订单号不对。"),
    ])
    login(client, SESSION_U1)
    body = chat(client, "取消订单 ../../auth/me").json()
    assert body["pending_action"] is None
    assert fake.requests[1][-1]["content"].startswith("参数错误：order_id")
    assert fake_django.requests[1:] == []  # 除了认人的 /me，什么都没查


# ---- CSRF ----


def test_confirm_needs_csrf_header(client, fake_llm, fake_django):
    fake_llm([cancel(101), assistant(content="已取消。")])
    login(client, SESSION_U1)
    first = chat(client, "取消那张没付款的").json()
    r = confirm(client, first, csrf=None)

    assert r.status_code == 403 and r.json() == {"detail": "缺少页面校验信息，请刷新页面后再试"}
    assert fake_django.cancels() == []
    assert confirm(client, first).status_code == 200  # 刷新页面拿到 token 以后，卡片还能用


def test_csrf_tokens_are_forwarded_to_shop(client, fake_llm, fake_django):
    fake_llm([cancel(101), assistant(content="已取消。")])
    login(client, SESSION_U1)
    confirm(client, chat(client, "取消那张没付款的").json())

    [request] = fake_django.cancels()
    assert request.headers["x-csrftoken"] == CSRF_TOKEN
    assert request.headers["cookie"] == f"sessionid={SESSION_U1}; csrftoken={CSRF_TOKEN}"


def test_shop_decides_whether_csrf_token_is_right(client, fake_llm, fake_django):
    fake = fake_llm([cancel(101), assistant(content="请刷新页面再试。")])
    login(client, SESSION_U1)
    first = chat(client, "取消那张没付款的").json()
    confirm(client, first, csrf="d" * 32)  # 格式对，但和 cookie 不一致：agent 不判断，交给商城

    assert tool_result(fake) == {"canceled": False, "reason": "页面校验没有通过，请刷新页面后再试"}
    assert fake_django.orders[1][0]["status"] == "PENDING_PAYMENT"


# ---- 商城拒绝、没有回复 ----


def test_shop_refusal_goes_back_to_model(client, fake_llm, fake_django):
    fake_django.payment_unresolved.add(uuid_for(101))
    fake = fake_llm([cancel(101), assistant(content="支付结果还没确认，请稍后再试。")])
    login(client, SESSION_U1)
    confirm(client, chat(client, "取消那张没付款的").json())

    assert tool_result(fake) == {"canceled": False, "reason": "支付结果尚未确认，请先查询原支付结果，再关闭订单。"}


def test_no_reply_from_shop_is_reported_as_unknown(client, fake_llm, fake_django):
    fake_django.cancel_hangs = True
    fake = fake_llm([cancel(101), assistant(content="不确定取消成功没有，请稍后查一下订单。")])
    login(client, SESSION_U1)
    body = confirm(client, chat(client, "取消那张没付款的").json()).json()

    result = tool_result(fake)
    assert result["canceled"] is None and "不确定取消有没有成功" in result["reason"]  # 不能说成「取消失败」
    assert body["tool_calls"][0]["ok"] is False


# ---- 审计 ----


def test_audit_log(client, fake_llm, caplog):
    caplog.set_level(logging.INFO, logger="audit")
    fake_llm([cancel(101), assistant(content="已取消。")])
    login(client, SESSION_U1)
    first = chat(client, "取消那张没付款的").json()
    confirm(client, first)

    records = [json.loads(r.getMessage()) for r in caplog.records if r.name == "audit"]
    assert [r["event"] for r in records] == ["cancel_proposed", "cancel_decided"]
    proposed, decided = records
    assert proposed["action_id"] == decided["action_id"] == first["pending_action"]["action_id"]
    assert decided["user_id"] == 1 and decided["order_id"] == uuid_for(101)
    assert decided["conversation_id"] == first["conversation_id"]
    assert decided["decision"] == "confirm" and decided["result"] == "canceled"
    assert SESSION_U1 not in caplog.text and CSRF_TOKEN not in caplog.text  # 不记登录凭证
