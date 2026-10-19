from uuid import UUID

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    # 第一次提问不传，服务端会新建一段对话；之后每次带上回复里的 conversation_id
    conversation_id: UUID | None = None


class ToolCallRecord(BaseModel):
    name: str
    arguments: str
    ok: bool


class ChatResponse(BaseModel):
    conversation_id: UUID
    reply: str
    tool_calls: list[ToolCallRecord]
