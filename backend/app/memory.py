"""语义记忆（RAG）：ChromaDB 持久化 + embedding 检索（对齐业界主流）。

- 每个 session 一个 collection，按内容块 (nodeId, seq) 存 chunk。
- embedding 默认用「离线哈希向量」（零下载、无网络依赖）；若装了 sentence-transformers
  则自动切到本地语义模型（多语 MiniLM）。
- 后续可切换为模型厂商的 embeddings API（OpenAI-compatible embeddings）。
"""
from __future__ import annotations

import hashlib
import math
import os
import re

# 关闭 chromadb 遥测（减少噪音/网络请求）
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("CHROMA_TELEMETRY_IMPL", "none")
# HuggingFace 国内镜像（加速模型下载；可被外部覆盖）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROMA_PATH = os.path.join(_BACKEND_DIR, "chroma_store")
_MODEL_NAME = "BAAI/bge-small-zh-v1.5"
_LOCAL_MODEL_DIR = os.path.join(_BACKEND_DIR, "..", "models", "bge-small-zh-v1.5")

_client = None
_ef = None
_EF_READY = False


def _get_client():
    global _client
    if _client is None:
        import chromadb
        _client = chromadb.PersistentClient(path=CHROMA_PATH)
    return _client


def _get_embedding_fn():
    """返回 embedding 函数；未装 sentence-transformers 时用离线哈希。"""
    global _ef, _EF_READY
    if _EF_READY:
        return _ef
    _EF_READY = True
    try:
        from chromadb.utils import embedding_functions as ef
        # 优先用本地已下载模型目录；否则用模型名（会尝试联网下载）
        if os.path.isdir(_LOCAL_MODEL_DIR) and os.path.exists(os.path.join(_LOCAL_MODEL_DIR, "model.safetensors")):
            _ef = ef.SentenceTransformerEmbeddingFunction(_LOCAL_MODEL_DIR)
        else:
            _ef = ef.SentenceTransformerEmbeddingFunction(_MODEL_NAME)
    except Exception:
        _ef = None
    return _ef


def _hash_embed(text: str, dim: int = 384) -> list[float]:
    """离线哈希向量：分词 → 加权哈希 → L2 归一化。零下载、确定、近似语义。"""
    vec = [0.0] * dim
    words = re.findall(r"[\w\u4e00-\u9fff]+", text.lower())
    for i, w in enumerate(words):
        h = hashlib.md5(w.encode("utf-8")).digest()
        for j in range(dim):
            idx = h[j % len(h)] % dim
            sign = 1.0 if (h[(j * 7) % len(h)] & 1) else -1.0
            vec[idx] += sign * (1.0 / (1.0 + math.log(2 + i)))
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _embed_texts(texts: list[str], dim: int = 384) -> list[list[float]]:
    ef = _get_embedding_fn()
    if ef is not None:
        try:
            return list(ef(texts))
        except Exception:
            pass
    return [_hash_embed(t, dim) for t in texts]


def _collection(session_id: str):
    client = _get_client()
    name = f"session_{session_id}"
    return client.get_or_create_collection(name=name, embedding_function=None)


def add_block(session_id: str, node_id: str, seq: int, title: str, text: str) -> None:
    """把一个内容块写入向量库（离线哈希 embedding，零下载）。"""
    if not text.strip():
        return
    col = _collection(session_id)
    doc_id = f"{node_id}:{seq}"
    emb = _embed_texts([text[:6000]])[0]
    col.upsert(
        ids=[doc_id],
        documents=[text[:6000]],
        embeddings=[emb],
        metadatas=[{"nodeId": node_id, "seq": seq, "title": title}],
    )


def search(session_id: str, query: str, parent_ids: list[str], top_k: int = 4) -> list[dict]:
    """语义检索召回与 query 最相关的内容块（限定在父节点集合内）。"""
    col = _collection(session_id)
    qemb = _embed_texts([query])[0]
    res = col.query(query_embeddings=[qemb], n_results=min(top_k, 20))
    ids = res.get("ids", [[]])[0]
    docs = res.get("documents", [[]])[0] if res.get("documents") else []
    metas = res.get("metadatas", [[]])[0] if res.get("metadatas") else []
    out: list[dict] = []
    for i, doc_id in enumerate(ids):
        meta = metas[i] if i < len(metas) else {}
        node_id = meta.get("nodeId", "")
        if parent_ids and node_id not in parent_ids:
            continue
        out.append(
            {
                "id": doc_id,
                "nodeId": node_id,
                "seq": meta.get("seq"),
                "title": meta.get("title", ""),
                "text": (docs[i] if i < len(docs) else ""),
            }
        )
    return out
