from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)


class ToolCallRecord(BaseModel):
    name: str
    arguments: str
    ok: bool


class ChatResponse(BaseModel):
    reply: str
    tool_calls: list[ToolCallRecord]
