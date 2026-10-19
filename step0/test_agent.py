"""离线测试：用一个假的 LLM 代替 DeepSeek，不连网、不花钱，也不需要真 key。"""
import json
import os
from types import SimpleNamespace

os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")  # 让 import agent 时不报 KeyError

import agent


def tool_call(call_id, name, arguments):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def assistant(content=None, tool_calls=None):
    return SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls)


class FakeLLM:
    """按顺序返回预设好的回复，并记下每次请求时发过去的 messages。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.requests.append(list(kwargs["messages"]))
        return SimpleNamespace(choices=[SimpleNamespace(message=self.replies.pop(0))])


def test_tool_only_returns_current_user_orders():
    orders = json.loads(agent.list_my_orders())
    assert [o["order_id"] for o in orders] == ["A1001", "A1002"]


def test_tool_filters_by_status():
    orders = json.loads(agent.list_my_orders(status="PENDING_PAYMENT"))
    assert [o["order_id"] for o in orders] == ["A1001"]


def test_calls_tool_then_answers(monkeypatch):
    fake = FakeLLM([
        assistant(tool_calls=[tool_call("call_1", "list_my_orders", '{"status": "PENDING_PAYMENT"}')]),
        assistant(content="你有一张待付款订单 A1001。"),
    ])
    monkeypatch.setattr(agent, "client", fake)

    assert agent.run_agent("我有哪些还没付款的订单？") == "你有一张待付款订单 A1001。"
    second = fake.requests[1]
    # 第二次请求里：先是模型要调工具的那条消息，紧跟着是工具结果，并且 id 对得上
    assert second[-2].tool_calls[0].id == "call_1"
    assert second[-1]["role"] == "tool"
    assert second[-1]["tool_call_id"] == "call_1"
    assert "A1001" in second[-1]["content"]


def test_answers_directly_without_tool(monkeypatch):
    fake = FakeLLM([assistant(content="抱歉，我只能帮你查订单。")])
    monkeypatch.setattr(agent, "client", fake)

    assert agent.run_agent("今天天气怎么样？") == "抱歉，我只能帮你查订单。"
    assert len(fake.requests) == 1


def test_stops_at_max_steps(monkeypatch):
    # 模型每一轮都要调工具、永远不给最终回答
    fake = FakeLLM([assistant(tool_calls=[tool_call("call_x", "list_my_orders", "{}")])] * 3)
    monkeypatch.setattr(agent, "client", fake)

    assert agent.run_agent("一直查", max_steps=3) == "达到最大步数，停止"
    assert len(fake.requests) == 3
