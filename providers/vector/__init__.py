# -*- coding: utf-8 -*-
"""向量检索记忆 provider：文本 → 向量 → 余弦相似度召回。

对齐 Hermes 的 plugins/memory/* 插件结构——实现 MemoryProvider 抽象，
和 mem0/honcho 一样用向量检索（而不是 keyword 插件的字符重叠）。

嵌入后端用环境变量 EMBEDDING_BACKEND 切换：
    tfidf   词频哈希向量（默认，零依赖——走完整向量管道，但语义能力弱）
    local   本地 sentence-transformers（语义检索，离线，需 pip install）
    api     OpenAI 兼容 embeddings API（语义检索，需 API key）

API 后端需要的环境变量：
    EMBEDDING_API_KEY   供应商 API Key（也支持 DASHSCOPE_API_KEY）
    EMBEDDING_BASE_URL  OpenAI 兼容地址，默认 https://dashscope.aliyuncs.com/compatible-mode/v1
    EMBEDDING_MODEL     模型名，默认 qwen3.7-text-embedding（阿里云百炼 Qwen）
"""

import json
import math
import os
import re
import zlib
from pathlib import Path

from memory_provider import MemoryProvider, extract_facts_with_llm, is_worth_memorizing

STORE_FILE = Path(__file__).parent / "vectors.json"
TOP_K = 3

SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "vector_search",
        "description": "用向量相似度搜索记忆库，返回语义上最匹配的条目。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要搜索的内容"},
            },
            "required": ["query"],
        },
    },
}


# ---------------- 嵌入后端（可插拔） ----------------
class TfidfEmbedder:
    """词频哈希向量（零依赖）：token 哈希到固定维度计数。

    走完整的"向量化 → 余弦相似度"管道，但本质仍偏字面匹配。
    """

    DIM = 512

    @staticmethod
    def name() -> str:
        return "tfidf"

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.DIM
        for token in self._tokenize(text):
            vec[zlib.crc32(token.encode("utf-8")) % self.DIM] += 1.0
        return vec

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        tokens = []
        for word in re.findall(r"[a-zA-Z0-9_]+", text.lower()):
            tokens.append(word)
        for run in re.findall(r"[\u4e00-\u9fff]+", text):
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
        return tokens


class LocalEmbedder:
    """本地语义向量（sentence-transformers，离线，无需 API）。"""

    @staticmethod
    def name() -> str:
        return "local"

    def __init__(self) -> None:
        self._model = None

    def _load(self) -> None:
        from sentence_transformers import SentenceTransformer

        model_name = os.environ.get(
            "EMBEDDING_MODEL", "paraphrase-multilingual-MiniLM-L12-v2"
        )
        self._model = SentenceTransformer(model_name)

    def embed(self, text: str) -> list[float]:
        if self._model is None:
            self._load()
        return self._model.encode(text).tolist()


class ApiEmbedder:
    """OpenAI 兼容 embeddings API（默认指向阿里云百炼 DashScope 的 Qwen）。"""

    @staticmethod
    def name() -> str:
        return "api"

    def __init__(self) -> None:
        import urllib.request

        self._urlopen = urllib.request.urlopen
        self._key = (
            os.environ.get("EMBEDDING_API_KEY", "")
            or os.environ.get("DASHSCOPE_API_KEY", "")
        )
        self._base = os.environ.get(
            "EMBEDDING_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        self._model = os.environ.get("EMBEDDING_MODEL", "qwen3.7-text-embedding")

    def embed(self, text: str) -> list[float]:
        import urllib.request

        body = json.dumps({"model": self._model, "input": text}).encode("utf-8")
        req = urllib.request.Request(
            f"{self._base}/embeddings",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._key}",
            },
        )
        with self._urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return list(data["data"][0]["embedding"])


def _make_embedder():
    """按 EMBEDDING_BACKEND 选择嵌入后端。"""
    backend = os.environ.get("EMBEDDING_BACKEND", "tfidf").strip().lower()
    if backend == "local":
        return LocalEmbedder()
    if backend == "api":
        return ApiEmbedder()
    return TfidfEmbedder()


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------- Provider ----------------
class Provider(MemoryProvider):
    """vector：记忆条目 → 向量库，按余弦相似度召回语义相近内容。"""

    @property
    def name(self) -> str:
        return "vector"

    def is_available(self) -> bool:
        backend = os.environ.get("EMBEDDING_BACKEND", "tfidf").strip().lower()
        if backend == "api":
            return bool(
                (
                    os.environ.get("EMBEDDING_API_KEY", "")
                    or os.environ.get("DASHSCOPE_API_KEY", "")
                ).strip()
            )
        if backend == "local":
            try:
                import sentence_transformers  # noqa: F401

                return True
            except Exception:
                return False
        return True

    def initialize(self, session_id: str = "", **kwargs) -> None:
        self._embedder = _make_embedder()
        self._vectors: dict[str, list[float]] = {}
        if STORE_FILE.exists():
            try:
                data = json.loads(STORE_FILE.read_text(encoding="utf-8"))
                self._vectors = (
                    {str(k): list(v) for k, v in data.items()} if isinstance(data, dict) else {}
                )
            except Exception:
                self._vectors = {}

    def system_prompt_block(self) -> str:
        return f"## 外部记忆（vector provider，backend={self._embedder.name()}）\n你可以使用我召回的向量记忆。"

    def _search(self, query: str, top_k: int) -> list[str]:
        qv = self._embedder.embed(query)
        scored = []
        for text, vec in self._vectors.items():
            score = _cosine(qv, vec)
            if score > 0.05:  # 相似度阈值
                scored.append((score, text))
        scored.sort(key=lambda x: -x[0])
        return [text for _, text in scored[:top_k]]

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        hits = self._search(query, TOP_K)
        return "\n".join(f"- {hit}" for hit in hits)

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict] | None = None,
        client=None,
    ) -> None:
        """对话结束同步：用 LLM 提取事实 → 向量化入库（对齐 Hermes mem0 的 infer=True）。

        有 client 时走 LLM 提取；没有时退化为启发式过滤（只存值得记住的原话）。
        """
        if client is not None:
            facts = extract_facts_with_llm(
                client, messages or [], existing=list(self._vectors.keys())
            )
        else:
            text = (user_content or "").strip()
            facts = [text] if is_worth_memorizing(text) else []
        changed = False
        for fact in facts:
            if fact and fact not in self._vectors:
                self._vectors[fact] = self._embedder.embed(fact)
                changed = True
        if changed:
            STORE_FILE.write_text(
                json.dumps(self._vectors, ensure_ascii=False), encoding="utf-8"
            )

    def get_tool_schemas(self) -> list[dict]:
        return [SEARCH_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if tool_name != "vector_search":
            return json.dumps({"success": False, "error": f"未知工具 {tool_name}"}, ensure_ascii=False)
        query = str((args or {}).get("query", ""))
        hits = self._search(query, 10)
        return json.dumps(
            {"success": True, "query": query, "count": len(hits), "results": hits},
            ensure_ascii=False,
        )
