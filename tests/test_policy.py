import asyncio
import json
from pathlib import Path

import pytest

from app.config import Settings
from app.policy_chunks import chunk_markdown, load_policy_chunks
from app.policy_search import rrf
from app.tools import execute_tool
from tests.fakes import SESSION_U1, TEST_POLICIES, FakeDjango, assistant, make_policy_index, tool_call
from tests.test_api import login
from tests.test_tools import make_ctx

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def ids(chunks):
    return [c.id for c in chunks]


# ---- 切段 ----

def test_one_clause_per_chunk_with_heading_path():
    chunks = chunk_markdown("returns", TEST_POLICIES["returns"])
    assert ids(chunks) == ["returns-1.1", "returns-1.2", "returns-2"]
    first = chunks[0]
    assert first.citation == "《退换货政策》1.1 适用条件"
    assert first.path == "《退换货政策》 > 1. 七天无理由退货 > 1.1 适用条件"
    assert first.text == "签收之日起七天内，商品完好的，可以申请无理由退货。"
    assert first.indexed_text.startswith(first.path)
    # 文档标题下面的说明文字不进索引
    assert all("示例说明" not in c.indexed_text for c in chunks)


def test_long_clause_is_split_by_sentence():
    markdown = "# 运费政策\n\n## 1. 运费\n\n第一句话有十个字。第二句话有十个字。第三句话有十个字。\n"
    chunks = chunk_markdown("shipping", markdown, max_chars=20)
    assert [c.text for c in chunks] == ["第一句话有十个字。第二句话有十个字。", "第三句话有十个字。"]
    # 拆开的几段还是同一条条款，出处一样
    assert {(c.id, c.citation) for c in chunks} == {("shipping-1", "《运费政策》1. 运费")}


def test_clause_heading_must_be_numbered():
    with pytest.raises(ValueError, match="条款标题要以编号开头"):
        chunk_markdown("bad", "# 坏政策\n\n## 没有编号\n\n正文。\n")


# ---- 合并 ----

def test_rrf_favors_what_both_lists_agree_on():
    # 2 在两路里都靠前，排第一；1 只在一路里第一，排第二
    assert rrf([[1, 2, 3], [2, 4]]) == [2, 1, 4, 3]


# ---- 检索 ----

def test_vector_search_finds_reworded_question():
    index = make_policy_index()
    # 「坏」「修」这两个字条款里都没有，关键词检索一条都对不上
    assert index.keyword_search("东西坏了能修吗") == []
    result = index.search("东西坏了能修吗")
    assert ids(result.chunks)[0] == "warranty-1"


def test_keyword_search_finds_exact_term():
    index = make_policy_index()
    # 假向量模型只认得「发票」，分不出两种发票；关键词检索能分出来
    scores = dict(index.vector_search("增值税专用发票要提供什么"))
    special, normal = (ids(index.chunks).index(i) for i in ["invoices-2", "invoices-1"])
    assert scores[special] == pytest.approx(scores[normal])
    assert index.keyword_search("增值税专用发票要提供什么")[0] == special


def test_default_uses_vector_search_only():
    # 合并时，两路都排得上的 invoices-2 会挤掉向量排第一的 warranty-1。评测里丢的两题就是这么丢的
    question = "坏了能修吗？专票要提供税号吗"
    assert ids(make_policy_index(top_k=1).search(question).chunks) == ["warranty-1"]
    assert ids(make_policy_index(top_k=1, hybrid=True).search(question).chunks) == ["invoices-2"]


def test_hybrid_keeps_what_only_keywords_found():
    # 前半句只有向量对得上（坏、修），后半句只有关键词对得上（提供、税号），合并后两条都在
    result = make_policy_index(top_k=2, hybrid=True).search("坏了能修吗？专票要提供税号吗")
    assert ids(result.chunks) == ["invoices-2", "warranty-1"]


def test_search_returns_at_most_top_k():
    assert len(make_policy_index(top_k=2).search("退货的运费谁出").chunks) == 2


def test_refuses_when_nothing_is_close_enough():
    result = make_policy_index().search("可以分期付款吗")
    assert result.chunks == [] and result.top_score < 0.5


# ---- 工具 ----

def test_search_policy_returns_clauses_and_records_sources():
    ctx = make_ctx(FakeDjango())
    content, ok = asyncio.run(execute_tool("search_policy", '{"query": "东西坏了能修吗"}', ctx))
    result = json.loads(content)
    assert ok is True and result["found"] is True
    assert result["clauses"][0] == {"citation": "《保修政策》1. 保修期限", "text": "数码配件签收后保修一年，期间出现故障免费维修。"}
    assert ids(ctx.sources)[0] == "warranty-1"


def test_search_policy_says_so_when_nothing_found():
    ctx = make_ctx(FakeDjango())
    content, ok = asyncio.run(execute_tool("search_policy", '{"query": "可以分期付款吗"}', ctx))
    assert ok is True
    assert json.loads(content) == {"found": False, "message": "售后政策里没有找到和这个问题相关的规定"}
    assert ctx.sources == []


# ---- 接口 ----

def test_sources_come_from_what_was_retrieved(client, fake_llm):
    fake = fake_llm([
        assistant(tool_calls=[tool_call("call_1", "search_policy", '{"query": "东西坏了能修吗"}')]),
        # 模型标错了出处，接口返回的出处不受影响
        assistant(content="保修期内免费维修（《退换货政策》9.9）。"),
    ])
    login(client, SESSION_U1)
    body = client.post("/api/chat", json={"message": "耳机坏了能修吗"}).json()

    assert body["sources"][0] == {
        "id": "warranty-1",
        "citation": "《保修政策》1. 保修期限",
        "text": "数码配件签收后保修一年，期间出现故障免费维修。",
    }
    assert "数码配件签收后保修一年" in fake.requests[1][-1]["content"]


def test_same_clause_found_twice_is_returned_once(client, fake_llm):
    search = tool_call("call_1", "search_policy", '{"query": "东西坏了能修吗"}')
    fake_llm([assistant(tool_calls=[search]), assistant(tool_calls=[search]), assistant(content="可以保修。")])
    login(client, SESSION_U1)
    body = client.post("/api/chat", json={"message": "坏了能修吗"}).json()

    assert len(body["sources"]) == len({s["id"] for s in body["sources"]})


def test_no_sources_when_nothing_found(client, fake_llm):
    fake_llm([
        assistant(tool_calls=[tool_call("call_1", "search_policy", '{"query": "可以分期付款吗"}')]),
        assistant(content="政策里没有分期付款的规定，建议联系人工客服。"),
    ])
    login(client, SESSION_U1)
    body = client.post("/api/chat", json={"message": "可以分期吗"}).json()

    assert body["sources"] == []


# ---- 真正的政策文档和评测题 ----

def test_real_policies_are_chunked_cleanly():
    chunks = load_policy_chunks(Settings(deepseek_api_key="test-key", _env_file=None).policy_dir)
    assert len(chunks) == len(set(ids(chunks)))  # 每条条款都没超长，没被拆开
    assert all(c.citation.startswith("《") and len(c.text) <= 300 for c in chunks)


def test_eval_questions_point_at_real_clauses():
    chunk_ids = set(ids(load_policy_chunks(PROJECT_ROOT / "policies")))
    lines = (PROJECT_ROOT / "evals" / "policy_questions.jsonl").read_text(encoding="utf-8").splitlines()
    questions = [json.loads(line) for line in lines if line.strip()]
    assert {i for q in questions for i in q["expected"]} <= chunk_ids
    assert any(not q["expected"] for q in questions)  # 有政策里没有的题，才测得了拒答
