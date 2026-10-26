import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

import jieba
from qdrant_client import QdrantClient, models
from rank_bm25 import BM25Okapi

from app.config import Settings, get_settings
from app.policy_chunks import Chunk, load_policy_chunks

jieba.setLogLevel(logging.WARNING)
# jieba 自带词典里没有的电商说法。不加的话「买多少钱包邮」会被切成「钱包」「邮」；
# 词频给高一点，才抢得过词典里本来就有的「钱包」
for word in ["包邮", "运费险", "专票", "无理由"]:
    jieba.add_word(word, freq=1000)

CANDIDATES = 10  # 向量和关键词每一路取前 10 名，两路合并时用得上
RRF_K = 60  # RRF 公式里的常数，用提出 RRF 的论文里的取值
PUNCTUATION = re.compile(r"[\W_]+")
STOPWORDS = set(
    "的 了 吗 呢 吧 啊 呀 嘛 我 你 我们 你们 是 在 有 和 就 都 也 还 能 会 要 想 可以 怎么 怎么办 什么 多少 哪些 哪里 这个 那个 一下 请问 现在".split()
)


class Embedder(Protocol):
    """把文字变成向量。真的用 FastEmbedEmbedder，单元测试用假的。"""

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class FastEmbedEmbedder:
    """在本机 CPU 上用 ONNX Runtime 跑向量模型，不用装 PyTorch。第一次用时把模型下载到 cache_dir。"""

    def __init__(self, model_name: str, cache_dir: str):
        from fastembed import TextEmbedding  # 用到才导入：单元测试用假的向量模型，不加载它

        self.model = TextEmbedding(model_name, cache_dir=cache_dir)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self.model.embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        return next(iter(self.model.query_embed(text))).tolist()


def tokenize(text: str) -> list[str]:
    """关键词检索用的分词：jieba 的搜索引擎模式会把长词再切一遍，比如「增值税专用发票」也切出「发票」。去掉标点和虚词。"""
    return [w for w in jieba.lcut_for_search(text) if w.strip() and w not in STOPWORDS and not PUNCTUATION.fullmatch(w)]


def rrf(rankings: list[list[int]]) -> list[int]:
    """倒数排名融合（RRF）：在每一路里排第 r 名，得 1/(60+r) 分，各路加起来再排序。

    只看名次，不看分数，所以不用管向量的相似度和 BM25 的分数是不是一个尺度。
    """
    scores: dict[int, float] = defaultdict(float)
    for ranking in rankings:
        for rank, i in enumerate(ranking, start=1):
            scores[i] += 1 / (RRF_K + rank)
    return sorted(scores, key=scores.__getitem__, reverse=True)


@dataclass(frozen=True)
class SearchResult:
    chunks: list[Chunk]  # 最相关的几段；证据不够时为空
    top_score: float  # 向量那一路的最高相似度，拒答就看它


class PolicyIndex:
    """售后政策的索引。建（__init__）和查（search）分开：以后政策多了，建索引可以挪到离线脚本里。"""

    COLLECTION = "policies"

    def __init__(self, chunks: list[Chunk], embedder: Embedder, *, top_k: int, min_score: float, hybrid: bool = False):
        self.chunks = chunks
        self.embedder = embedder
        self.top_k = top_k
        self.min_score = min_score
        self.hybrid = hybrid

        texts = [c.indexed_text for c in chunks]
        vectors = embedder.embed_documents(texts)
        # Qdrant 的本地内存模式：不用起服务。点的编号就是段落在 chunks 里的下标
        self.qdrant = QdrantClient(":memory:")
        self.qdrant.create_collection(
            self.COLLECTION,
            vectors_config=models.VectorParams(size=len(vectors[0]), distance=models.Distance.COSINE),
        )
        self.qdrant.upsert(self.COLLECTION, points=[models.PointStruct(id=i, vector=v) for i, v in enumerate(vectors)])
        # 关键词检索默认不参与回答，评测拿它做对照，见 ADR 0013
        self.bm25 = BM25Okapi([tokenize(t) for t in texts])

    def vector_search(self, query: str) -> list[tuple[int, float]]:
        """向量检索：返回 (段落下标, 余弦相似度)，最像的在前。"""
        query_vector = self.embedder.embed_query(query)
        points = self.qdrant.query_points(self.COLLECTION, query=query_vector, limit=CANDIDATES).points
        return [(p.id, p.score) for p in points]

    def keyword_search(self, query: str) -> list[int]:
        """关键词检索：BM25 打分，返回段落下标。一个词都没对上的段落不算进来。"""
        scores = self.bm25.get_scores(tokenize(query))
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        return [i for i in ranked[:CANDIDATES] if scores[i] > 0]

    def search(self, query: str) -> SearchResult:
        vector_hits = self.vector_search(query)
        top_score = vector_hits[0][1] if vector_hits else 0.0
        # 证据不够就一段都不给：模型手里没有条款，就没法照着编
        if top_score < self.min_score:
            return SearchResult(chunks=[], top_score=top_score)
        ranking = [i for i, _ in vector_hits]
        if self.hybrid:
            ranking = rrf([ranking, self.keyword_search(query)])
        return SearchResult(chunks=[self.chunks[i] for i in ranking[: self.top_k]], top_score=top_score)


def build_policy_index(settings: Settings) -> PolicyIndex:
    embedder = FastEmbedEmbedder(settings.embedding_model, cache_dir=str(settings.embedding_cache_dir))
    return PolicyIndex(
        load_policy_chunks(settings.policy_dir),
        embedder,
        top_k=settings.policy_top_k,
        min_score=settings.policy_min_score,
        hybrid=settings.policy_hybrid,
    )


@lru_cache
def get_policy_index() -> PolicyIndex:
    """整个进程共用一份索引，服务启动时就建好，见 main.py 的 lifespan。"""
    return build_policy_index(get_settings())
