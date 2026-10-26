"""取消订单：agent 唯一的写操作。

能不能取消由商城判断，这里只管两件事：用户确认之前，做一张什么样的确认卡片；用户确认之后，怎么安全地执行。
"""
import json
import logging
import uuid
from datetime import UTC, datetime
from uuid import UUID

from app.django_client import DjangoRefused
from app.tools import ToolContext

audit_logger = logging.getLogger("audit")

STATUS_TEXT = {
    "PENDING_PAYMENT": "待付款",
    "PAID": "已付款待发货",
    "SHIPPED": "已发货",
    "COMPLETED": "已完成",
    "CANCELED": "已取消",
    "EXPIRED": "已关闭",
}

# 能取消的状态，和取消以后会怎样。和商城的取消规则一致：待付款的直接关闭，已付款没发货的全额退款
EFFECTS = {
    "PENDING_PAYMENT": "取消后订单关闭，不会扣款。",
    "PAID": "取消后全额退款 ¥{total}，按原支付方式退回。",
}

# 不能取消的状态。直接把原因告诉模型，不弹确认卡片
REFUSALS = {
    "CANCELED": "这张订单已经取消了，不用再取消。",
    "SHIPPED": "这张订单已经发货，不能取消。可以拒收快递，或者签收后按退换货政策申请退货。",
    "COMPLETED": "这张订单已经完成，不能取消。需要的话可以按退换货政策申请售后。",
    "EXPIRED": "这张订单超时没有付款，已经被系统关闭，不用再取消。",
}


async def prepare_cancel(ctx: ToolContext, order_id: UUID) -> tuple[dict | None, dict | None]:
    """用户确认之前先查一次订单。能取消就返回确认卡片，不能取消就返回原因。

    卡片上的字全部来自商城的数据，不用模型写的：模型可能把订单号、金额说错，用户要看的是真实的订单。
    这里只是让明显不能取消的订单少弹一次卡片，最终能不能取消，还是以商城的返回为准。
    """
    order = await ctx.django.get_order(ctx.user.session_id, order_id)
    status = order["status"]
    if status not in EFFECTS:
        return None, {"canceled": False, "reason": REFUSALS.get(status, "这张订单当前不能取消。")}
    card = {
        "action_id": uuid.uuid4().hex,  # 这一次操作的编号，用户点确认时要带回来
        "kind": "cancel_order",
        "order_id": order["public_id"],
        "status": status,
        "status_text": STATUS_TEXT[status],
        "items": [f"{item['product_name_snapshot']} x{item['quantity']}" for item in order["items"]],
        "total": order["total"],
        "effect": EFFECTS[status].format(total=order["total"]),
        "proposed_at": now(),
    }
    return card, None


async def execute_cancel(ctx: ToolContext, card: dict) -> tuple[dict, str]:
    """用户点了确认以后执行。返回 (交给模型的结果, 审计里记的结果)。

    先重新查一次订单：用户看卡片的时候可能在别的页面付了款，这时取消就会变成退款，
    和用户同意的不是一回事。状态和卡片上的不一样，就不取消。
    """
    order_id = UUID(card["order_id"])
    order = await ctx.django.get_order(ctx.user.session_id, order_id)
    if order["status"] != card["status"]:
        now_text = STATUS_TEXT.get(order["status"], order["status"])
        reason = f"订单状态变了：确认时是「{card['status_text']}」，现在是「{now_text}」，所以没有取消。需要的话请重新发起。"
        return {"canceled": False, "reason": reason}, "status_changed"
    try:
        await ctx.django.cancel_order(ctx.user.session_id, ctx.csrf, order_id)
    except DjangoRefused as e:
        return {"canceled": False, "reason": e.message}, f"refused:{e.code}"
    return {"canceled": True, "order_id": card["order_id"], "effect": card["effect"]}, "canceled"


def audit(event: str, *, user_id: int, conversation_id: UUID, card: dict, **fields) -> None:
    """审计日志：一行一个 JSON，谁、哪张订单、什么时候、点了什么、结果如何。

    提出时记一条 cancel_proposed，用户点了以后记一条 cancel_decided。只有前一条没有后一条的，就是用户没点。
    不记用户的原话和登录凭证。
    """
    record = {
        "event": event,
        "at": now(),
        "user_id": user_id,
        "conversation_id": str(conversation_id),
        "order_id": card["order_id"],
        "action_id": card["action_id"],
        **fields,
    }
    audit_logger.info(json.dumps(record, ensure_ascii=False))


def confirm_prompt(card: dict) -> str:
    """暂停时给用户看的那句话。"""
    return f"请确认是否取消这张订单：{'、'.join(card['items'])}，共 ¥{card['total']}，{card['status_text']}。{card['effect']}"


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
