"""HybridRetriever — 纯 BM25 检索（无 vector/graph）。

吸收 Poirot `memory/strategies/default/retriever.py`：
- 纯 BM25，无 vector/graph 依赖
- forgotten 过滤（retrieve 时排除 metadata.forgotten=True）
- 增量索引（on_trace_added/updated/removed）
- retrieve 强化写回（命中后 store.update 提升 strength）
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections import defaultdict
from typing import Callable

from ...memory_store import MemoryStore
from ...retriever import Retriever
from ...schema import MemoryTrace
from ...types import MemoryQuery, RetrievalResult
from ._constants import BM25_PARAMS
from .decay import EbbinghausDecayPolicy

logger = logging.getLogger(__name__)

# 中文分词（可选依赖）：jieba 缺失时回退空格分词
try:  # pragma: no cover - 依赖是否安装决定分支
    import jieba as _jieba
except ImportError:  # pragma: no cover
    _jieba = None

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


class HybridRetriever:
    """纯 BM25 检索器。检索强化写回 + forgotten 过滤 + 增量索引。"""

    def __init__(
        self,
        store: MemoryStore,
        decay_policy: EbbinghausDecayPolicy,
        *,
        tokenize: Callable[[str], list[str]] | None = None,
    ) -> None:
        self._store = store
        self._decay_policy = decay_policy
        self._tokenize = tokenize or self._default_tokenize
        self._inverted_index: dict[str, dict[str, int]] = defaultdict(dict)
        self._trace_lengths: dict[str, int] = {}
        self._build_index_from_store()

    def _build_index_from_store(self) -> None:
        for trace in self._store.list_all():
            if trace.metadata.get("forgotten"):
                continue
            self._index_trace(trace)

    def _index_trace(self, trace: MemoryTrace) -> None:
        tokens = self._tokenize(trace.content)
        self._trace_lengths[trace.id] = len(tokens)
        tf: dict[str, int] = defaultdict(int)
        for token in tokens:
            tf[token] += 1
        for token, count in tf.items():
            self._inverted_index[token][trace.id] = count

    def _remove_trace_from_index(self, trace_id: str) -> None:
        self._trace_lengths.pop(trace_id, None)
        for token in list(self._inverted_index.keys()):
            self._inverted_index[token].pop(trace_id, None)
            if not self._inverted_index[token]:
                del self._inverted_index[token]

    def retrieve(self, query: MemoryQuery) -> list[RetrievalResult]:
        now = time.time()
        query_tokens = self._tokenize(query.text)

        candidates = [t for t in self._store.list_all() if not t.metadata.get("forgotten")]
        if query.type_filter is not None:
            type_key = (
                query.type_filter.value
                if isinstance(query.type_filter, type(query.type_filter))
                else str(query.type_filter)
            )
            candidates = [t for t in candidates if t.type.value == type_key]

        scores: list[tuple[MemoryTrace, float]] = []
        avgdl = sum(self._trace_lengths.values()) / max(1, len(self._trace_lengths))
        k1 = BM25_PARAMS["k1"]
        b = BM25_PARAMS["b"]

        for trace in candidates:
            similarity = self._bm25_score(query_tokens, trace.id, avgdl, k1, b)
            if similarity <= 0:
                continue
            current_strength = self._decay_policy.compute_strength(trace, now)
            if current_strength < query.min_strength:
                continue
            result = RetrievalResult.compute_score(trace, similarity, current_strength)
            scores.append((trace, result.score))

        scores.sort(key=lambda x: x[1], reverse=True)
        top = scores[: query.top_k]

        results: list[RetrievalResult] = []
        for trace, score in top:
            new_strength = self._decay_policy.compute_strength(trace, now)
            strengthened = trace.with_strength(new_strength, now)
            self._store.update(strengthened)  # 强化写回
            results.append(RetrievalResult.compute_score(strengthened, score, new_strength))

        return results

    def _bm25_score(self, query_tokens: list[str], trace_id: str, avgdl: float, k1: float, b: float) -> float:
        score = 0.0
        trace_len = self._trace_lengths.get(trace_id, 0)
        if trace_len == 0:
            return 0.0
        for token in query_tokens:
            postings = self._inverted_index.get(token, {})
            tf = postings.get(trace_id, 0)
            if tf == 0:
                continue
            df = len(postings)
            n = len(self._trace_lengths)
            idf = math.log((n - df + 0.5) / (df + 0.5) + 1)
            tf_norm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * trace_len / avgdl))
            score += idf * tf_norm
        return score

    @staticmethod
    def _default_tokenize(text: str) -> list[str]:
        # 含中文且 jieba 可用时用 jieba 分词；否则空格分词（英文/回退）
        if _jieba is not None and _CJK_RE.search(text):
            return [t.strip().lower() for t in _jieba.lcut(text) if t.strip()]
        return text.lower().split()

    def on_trace_added(self, trace: MemoryTrace) -> None:
        if not trace.metadata.get("forgotten"):
            self._index_trace(trace)

    def on_trace_updated(self, trace: MemoryTrace) -> None:
        self._remove_trace_from_index(trace.id)
        if not trace.metadata.get("forgotten"):
            self._index_trace(trace)

    def on_trace_removed(self, trace_id: str) -> None:
        self._remove_trace_from_index(trace_id)
