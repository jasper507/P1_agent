import json
import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com")
MODEL = "deepseek-flash"

# 假数据：两个用户的订单。CURRENT_USER 模拟“当前登录的人”
FAKE_ORDERS = {
    "u1": [
        {"order_id": "A1001", "status": "PENDING_PAYMENT", "total": "199.00"},
        {"order_id": "A1002", "status": "SHIPPED", "total": "89.00"},
    ],
    "u2": [
        {"order_id": "B2001", "status": "PAID", "total": "59.00"},
    ],
}
CURRENT_USER = "u1"


def list_my_orders(status: str | None = None) -> str:
    """工具本身就是普通函数。返回字符串，因为要塞回 messages 给模型看。"""
    # A：用户是谁由代码决定（CURRENT_USER），不是模型传进来的参数
    orders = FAKE_ORDERS.get(CURRENT_USER, [])
    if status:
        orders = [o for o in orders if o["status"] == status]
    return json.dumps(orders, ensure_ascii=False)


# 告诉模型“有这个工具、怎么用”。模型只看得到这段描述，看不到上面的函数代码
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_my_orders",
            "description": "查询当前登录用户自己的订单，可以按状态过滤",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["PENDING_PAYMENT", "PAID", "SHIPPED", "COMPLETED", "CANCELED"],
                        "description": "订单状态；不传则返回全部订单",
                    }
                },
                "required": [],
            },
        },
    }
]
TOOL_MAP = {"list_my_orders": list_my_orders}


def run_agent(user_input: str, max_steps: int = 5) -> str:
    messages = [
        {"role": "system", "content": "你是电商客服。涉及订单信息时必须调用工具查询，不要编造。"},
        {"role": "user", "content": user_input},
    ]
    for step in range(1, max_steps + 1):
        resp = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            extra_body={"thinking": {"type": "disabled"}},
        )
        msg = resp.choices[0].message

        # B：模型这一轮说的话（包括它要调哪个工具）也是对话历史的一部分
        messages.append(msg)

        # C：没有要调用的工具，说明这就是最终回答
        if not msg.tool_calls:
            return msg.content

        # D：执行模型要求的每个工具，把结果用 tool_call_id 对上号再交回给模型
        for call in msg.tool_calls:
            name = call.function.name
            args = json.loads(call.function.arguments or "{}")
            print(f"[第{step}轮] 调用 {name} 参数 {args}")
            result = TOOL_MAP[name](**args)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

    return "达到最大步数，停止"


if __name__ == "__main__":
    for q in ["我有哪些订单？", "我有哪些还没付款的订单？", "今天天气怎么样？", "帮我查一下 u2 的订单"]:
        print("用户：", q)
        print("客服：", run_agent(q))
        print("-" * 40)
