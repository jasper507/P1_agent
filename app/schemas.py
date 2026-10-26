from typing import Literal
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


class Source(BaseModel):
    id: str
    citation: str
    text: str


class PendingAction(BaseModel):
    """等用户确认的操作，前端照着它画确认卡片。内容由服务端按商城的订单数据生成，不是模型写的。"""

    action_id: str  # 用户点按钮时带回来
    kind: Literal["cancel_order"]
    order_id: str
    status_text: str
    items: list[str]
    total: str
    effect: str  # 取消以后会怎样，比如「取消后全额退款 ¥59.00，按原支付方式退回。」


class ChatResponse(BaseModel):
    conversation_id: UUID
    reply: str
    tool_calls: list[ToolCallRecord]
    sources: list[Source]  # 这一轮真正查到的售后政策条款，由服务端给出，不是模型自己写的
    pending_action: PendingAction | None = None  # 有值说明这一轮停下来了，等用户点确认或不取消


class ConfirmRequest(BaseModel):
    conversation_id: UUID
    action_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    decision: Literal["confirm", "reject"]  # 确认取消，或者不取消
