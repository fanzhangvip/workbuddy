"""Reciprocal Rank Fusion (RRF) —— 倒数排名融合的最小实现。

用途：把多路检索结果（向量检索 / BM25 / 图谱 / 多路 query 改写等）
      按「排名」融合成一个统一的排序列表。

为什么用排名而不是分数：不同检索器的分数不可比
（BM25 得分是大于 0 的实数，向量相似度通常在 0~1），
直接相加或归一化都容易失真。排名天然可比。

参考：Cormack, Clarke & Buettcher, "Reciprocal Rank Fusion outperforms
      Condorcet and individual Rank Learning Methods", SIGIR 2009.
"""

from collections import defaultdict
from typing import Hashable, Iterable, Sequence


def rrf_fuse(
    result_lists: Sequence[Sequence[Hashable]],
    k: int = 60,
    top_n: int | None = None,
) -> list[tuple[Hashable, float]]:
    """融合多路有序检索结果。

    Args:
        result_lists: 多路检索结果。每一路是一个「文档 id 列表」，
                      已按相关性从高到低排好序（rank 从 1 开始）。
        k:            平滑常数，惯例取 60。k 越小，头部排名的区分度越大。
        top_n:        只返回前 N 条；None 表示全部返回。

    Returns:
        [(doc_id, rrf_score), ...]，按融合分数降序。
    """
    scores: dict[Hashable, float] = defaultdict(float)

    # 核心公式：score(d) = Σ 1 / (k + rank_i(d))
    for docs in result_lists:
        for rank, doc_id in enumerate(docs, start=1):
            scores[doc_id] += 1.0 / (k + rank)

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return ranked[:top_n] if top_n is not None else ranked


def to_ranked(scored: Iterable[tuple[Hashable, float]]) -> list[Hashable]:
    """便利函数：把 [(doc_id, score), ...] 按分数降序转成纯排名列表。

    单路检索器内部分数是自洽的，可以直接排序；跨路才需要 RRF。
    """
    return [doc_id for doc_id, _ in sorted(scored, key=lambda x: x[1], reverse=True)]


if __name__ == "__main__":
    # 模拟两路召回：左边是向量检索的排名，右边是 BM25 的排名
    vector_hits = ["d3", "d7", "d1"]
    bm25_hits = ["d1", "d3", "d9"]

    print("向量检索 :", vector_hits)
    print("BM25 检索:", bm25_hits)
    print("-" * 32)
    print("RRF 融合后：")
    for i, (doc_id, score) in enumerate(rrf_fuse([vector_hits, bm25_hits], k=60), 1):
        print(f"  {i}. {doc_id}  {score:.5f}")

    # 实际用法：检索器返回 (id, score) 时，先用 to_ranked 转成排名列表
    vec_scored = [("d3", 0.91), ("d7", 0.88), ("d1", 0.72)]
    bm25_scored = [("d1", 12.7), ("d3", 9.4), ("d9", 3.1)]
    fused = rrf_fuse([to_ranked(vec_scored), to_ranked(bm25_scored)], k=60, top_n=2)
    print("\ntop_n=2 的用法：", fused)
