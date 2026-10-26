"""检索评测：不经过大模型，只看检索这一步查得准不准、该拒的拒没拒。结果每次都一样。

在项目根目录运行：.venv\\Scripts\\python -m evals.eval_retrieval
第一次运行会下载向量模型。题目在 policy_questions.jsonl，expected 为空的是政策里没有的题。
"""
import json
import time
from pathlib import Path

from app.config import Settings
from app.policy_search import build_policy_index, rrf

QUESTIONS = Path(__file__).with_name("policy_questions.jsonl")
THRESHOLDS = [round(0.30 + 0.05 * i, 2) for i in range(11)]  # 0.30 到 0.80


def recall(ranked_ids: list[str], expected: list[str], k: int) -> float:
    """前 k 名里，应该查到的段落占了几成。"""
    return len(set(ranked_ids[:k]) & set(expected)) / len(expected)


def main():
    settings = Settings(deepseek_api_key="评测用不到")
    started = time.perf_counter()
    index = build_policy_index(settings)
    build_seconds = time.perf_counter() - started
    k = settings.policy_top_k
    questions = [json.loads(line) for line in QUESTIONS.read_text(encoding="utf-8").splitlines() if line.strip()]
    covered = [q for q in questions if q["expected"]]
    uncovered = [q for q in questions if not q["expected"]]

    def to_ids(ranking):
        return [index.chunks[i].id for i in ranking]

    recalls = {"只用向量": [], "只用关键词": [], "两个一起": []}
    misses = {"只用向量": [], "两个一起": []}  # 应该查到的条款没进前 k 的题
    top_scores = {}  # 每道题向量那一路的最高相似度
    started = time.perf_counter()
    for q in questions:
        vector_hits = index.vector_search(q["question"])
        vector_ranking = [i for i, _ in vector_hits]
        keyword_ranking = index.keyword_search(q["question"])
        top_scores[q["question"]] = vector_hits[0][1]
        if not q["expected"]:
            continue
        rankings = {
            "只用向量": to_ids(vector_ranking),
            "只用关键词": to_ids(keyword_ranking),
            "两个一起": to_ids(rrf([vector_ranking, keyword_ranking])),
        }
        for name, ranking in rankings.items():
            recalls[name].append(recall(ranking, q["expected"], k))
            if name in misses and recalls[name][-1] < 1:
                misses[name].append((q["question"], q["expected"], ranking[:k]))
    query_ms = (time.perf_counter() - started) / len(questions) * 1000

    print(f"{len(index.chunks)} 段政策，建索引 {build_seconds:.1f} 秒；每道题检索约 {query_ms:.0f} 毫秒")
    print(f"{len(covered)} 道政策里有的题，{len(uncovered)} 道政策里没有的题\n")

    print(f"一、查得准不准（Recall@{k}，不算拒答）")
    in_use = "两个一起" if settings.policy_hybrid else "只用向量"
    for name, values in recalls.items():
        mark = "  ← 当前设置" if name == in_use else ""
        print(f"  {name}：{sum(values) / len(values):.2f}{mark}")
    for name, missed in misses.items():
        print(f"  {name}时没进前 {k} 的题：{'没有' if not missed else ''}")
        for question, expected, got in missed:
            print(f"    {question}  应为 {expected}  实际 {got}")

    print("\n二、该拒的拒没拒（看向量那一路的最高相似度）")
    lowest = min(covered, key=lambda q: top_scores[q["question"]])
    highest = max(uncovered, key=lambda q: top_scores[q["question"]])
    print(f"  有政策的题里最低：{top_scores[lowest['question']]:.3f}  {lowest['question']}")
    print(f"  没政策的题里最高：{top_scores[highest['question']]:.3f}  {highest['question']}")
    print("  阈值   误拒（有政策却拒了）   漏拒（没政策却没拒）")
    for t in sorted(set(THRESHOLDS + [settings.policy_min_score])):
        wrong = sum(top_scores[q["question"]] < t for q in covered)
        missed = sum(top_scores[q["question"]] >= t for q in uncovered)
        mark = "  ← 当前设置" if t == settings.policy_min_score else ""
        print(f"  {t:.2f}   {wrong:>2}/{len(covered)}                {missed:>2}/{len(uncovered)}{mark}")
    print("\n  没政策的题各自的最高相似度：")
    for q in sorted(uncovered, key=lambda q: -top_scores[q["question"]]):
        print(f"    {top_scores[q['question']]:.3f}  {q['question']}")


if __name__ == "__main__":
    main()
