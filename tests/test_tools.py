import asyncio
import json

from app.tools import TOOL_SCHEMAS, execute_tool


def test_schema_does_not_expose_user_id():
    params = TOOL_SCHEMAS[0]["function"]["parameters"]
    assert list(params["properties"]) == ["status"]
    assert params["additionalProperties"] is False


def test_filters_by_status():
    content, ok = asyncio.run(execute_tool("list_my_orders", '{"status": "SHIPPED"}', "u1"))
    assert ok is True
    assert [o["order_id"] for o in json.loads(content)] == ["A1002"]


def test_rejects_unknown_status():
    content, ok = asyncio.run(execute_tool("list_my_orders", '{"status": "LOST"}', "u1"))
    assert ok is False and content.startswith("参数错误：status")


def test_rejects_broken_json():
    content, ok = asyncio.run(execute_tool("list_my_orders", '{"status": ', "u1"))
    assert ok is False and content.startswith("参数错误")
