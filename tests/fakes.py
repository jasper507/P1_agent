"""假的 LLM：按顺序返回预设好的回复，不连网、不花钱。"""
from types import SimpleNamespace


def tool_call(call_id, name, arguments):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def assistant(content=None, tool_calls=None):
    return SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls)


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []  # 每次请求时发过去的 messages
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.requests.append(list(kwargs["messages"]))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(choices=[SimpleNamespace(message=reply)])
