"""
semantic_search.py — OpenAI embedding-based chunk search

Drop-in upgrade over BM25 keyword scoring. Zero new infrastructure —
embeddings are stored as a field in the existing chunks.json.

Enable:      SEARCH_BACKEND=semantic  in .env
Switch back: SEARCH_BACKEND=bm25      (or remove the var)

Model:   text-embedding-3-small  (1536 dims, ~$0.02/M tokens)
Compute: numpy vectorised cosine similarity (fast for 1 k–10 k chunks)

Backfill existing chunks (one-time, after enabling):
  POST /api/project/{project_id}/embed-chunks
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("SemanticSearch")

EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small")


# ─────────────────────────────────────────────────────────────────────────────
# Feature flag
# ─────────────────────────────────────────────────────────────────────────────

def is_semantic_enabled() -> bool:
    """True when SEARCH_BACKEND=semantic and an OpenAI API key is present."""
    return (
        os.getenv("SEARCH_BACKEND", "bm25").lower() == "semantic"
        and bool(os.getenv("OPENAI_API_KEY", "").strip())
    )


# ─────────────────────────────────────────────────────────────────────────────
# OpenAI client (SSL-safe for corporate proxies)
# ─────────────────────────────────────────────────────────────────────────────

def _openai_client():
    import httpx
    from openai import OpenAI
    ssl_verify = os.getenv("OPENAI_SSL_VERIFY", "false").lower() not in ("false", "0", "no")
    return OpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        http_client=httpx.Client(verify=ssl_verify),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Embedding generation
# ─────────────────────────────────────────────────────────────────────────────

def embed_texts(texts: List[str], batch_size: int = 100) -> List[List[float]]:
    """
    Embed a list of texts using OpenAI embeddings.
    Batched to stay under API limits. Batches run in parallel (up to 4 threads)
    so large imports don't block sequentially on each round-trip.
    Returns one float vector per input, in input order.
    """
    if not texts:
        return []

    batches = [
        [t.replace("\n", " ").strip()[:8000] for t in texts[i : i + batch_size]]
        for i in range(0, len(texts), batch_size)
    ]

    def _embed_batch(batch: List[str]) -> List[List[float]]:
        client = _openai_client()  # one client per thread — httpx.Client is not thread-safe
        response = client.embeddings.create(input=batch, model=EMBED_MODEL)
        return [d.embedding for d in sorted(response.data, key=lambda x: x.index)]

    if len(batches) == 1:
        return _embed_batch(batches[0])

    from concurrent.futures import ThreadPoolExecutor
    ordered: List[Optional[List[List[float]]]] = [None] * len(batches)
    with ThreadPoolExecutor(max_workers=min(4, len(batches))) as pool:
        for i, result in enumerate(pool.map(_embed_batch, batches)):
            ordered[i] = result

    return [emb for batch_result in ordered for emb in batch_result]


@lru_cache(maxsize=512)
def _embed_query_cached(query: str) -> Tuple[float, ...]:
    """Cache query embeddings as a tuple (hashable) to avoid repeat API calls."""
    return tuple(embed_texts([query])[0])


def embed_query(query: str) -> List[float]:
    """Embed a single query string, served from cache when the same query repeats."""
    return list(_embed_query_cached(query.strip()))


# ─────────────────────────────────────────────────────────────────────────────
# Cosine similarity (numpy fast-path, pure-Python fallback)
# ─────────────────────────────────────────────────────────────────────────────

# Module-level cache: maps a chunks fingerprint → pre-built normalized numpy matrix.
# Avoids rebuilding the (N, D) float32 matrix from Python lists on every search call.
_matrix_cache: Dict[str, object] = {}  # fingerprint → np.ndarray (N, D), L2-normalised


def _chunks_fingerprint(chunk_ids: List) -> str:
    return f"{len(chunk_ids)}:{chunk_ids[0] if chunk_ids else ''}:{chunk_ids[-1] if chunk_ids else ''}"


def _get_normalised_matrix(vecs: List[List[float]], chunk_ids: List):
    """Return a cached, L2-normalised numpy matrix for the given chunk vectors."""
    try:
        import numpy as np
    except ImportError:
        return None

    key = _chunks_fingerprint(chunk_ids)
    cached = _matrix_cache.get(key)
    if cached is not None:
        return cached

    C = np.array(vecs, dtype=np.float32)         # (N, D)
    norms = np.linalg.norm(C, axis=1, keepdims=True)
    norms = np.where(norms > 0, norms, 1.0)
    C_norm = C / norms                            # pre-normalised rows
    _matrix_cache[key] = C_norm
    if len(_matrix_cache) > 16:                   # cap memory: keep last 16 chunk sets
        _matrix_cache.pop(next(iter(_matrix_cache)))
    return C_norm


def _cosine_scores(query_vec: List[float], vecs: List[List[float]], chunk_ids: List = None) -> List[float]:
    """
    Compute cosine similarity between query_vec and every vector in vecs.
    Uses numpy when available (strongly recommended for >100 chunks).
    When chunk_ids are provided the normalised matrix is cached across calls.
    """
    try:
        import numpy as np

        q = np.array(query_vec, dtype=np.float32)
        q_norm = float(np.linalg.norm(q))
        if q_norm == 0:
            return [0.0] * len(vecs)
        q_unit = q / q_norm

        if chunk_ids is not None:
            C_norm = _get_normalised_matrix(vecs, chunk_ids)
            if C_norm is not None:
                return (C_norm @ q_unit).tolist()

        # Fallback: build matrix inline (no cache)
        C = np.array(vecs, dtype=np.float32)
        c_norms = np.linalg.norm(C, axis=1)
        dots = C @ q
        with np.errstate(divide="ignore", invalid="ignore"):
            scores = np.where(c_norms > 0, dots / (c_norms * q_norm), 0.0)
        return scores.tolist()

    except ImportError:
        import math

        q_norm = math.sqrt(sum(x * x for x in query_vec))
        if q_norm == 0:
            return [0.0] * len(vecs)
        out = []
        for vec in vecs:
            dot = sum(x * y for x, y in zip(query_vec, vec))
            c_norm = math.sqrt(sum(x * x for x in vec))
            out.append(dot / (c_norm * q_norm) if c_norm > 0 else 0.0)
        return out


# ─────────────────────────────────────────────────────────────────────────────
# Main search entry point
# ─────────────────────────────────────────────────────────────────────────────

def semantic_search(
    query: str,
    chunks: List[Dict],
    top_k: int = 8,
    min_score: float = 0.25,
    exclude_ids: Optional[List] = None,
) -> List[Tuple[float, Dict]]:
    """
    Search chunks by semantic (embedding) similarity.

    Args:
        query:       Natural language query — free text or joined keywords.
        chunks:      Chunk dicts. Only those with an 'embedding' field are scored;
                     chunks without embeddings are skipped with a warning.
        top_k:       Max results.
        min_score:   Cosine similarity threshold (0–1). Default 0.25.
        exclude_ids: chunk_id values to hard-exclude (e.g. section 2.x filter).

    Returns:
        List of (score, chunk) tuples, sorted by score descending.
    """
    if not chunks or not query.strip():
        return []

    exclude = set(exclude_ids or [])
    with_emb = [
        c for c in chunks
        if c.get("embedding") and c.get("chunk_id") not in exclude
    ]
    without_emb = [c for c in chunks if not c.get("embedding")]
    logger.info(
        "semantic_search request: query_len=%d total_chunks=%d with_emb=%d without_emb=%d top_k=%d min_score=%.3f",
        len(query.strip()), len(chunks), len(with_emb), len(without_emb), top_k, min_score,
    )

    if without_emb:
        logger.warning(
            "semantic_search: %d / %d chunks have no embedding — they are excluded. "
            "Call POST /api/project/{id}/embed-chunks to backfill.",
            len(without_emb), len(chunks),
        )

    if not with_emb:
        logger.warning("semantic_search: no embedded chunks available — returning empty.")
        return []

    try:
        q_vec = embed_query(query)
        logger.info(
            "semantic_search query embedding ok: query=%r vector_len=%d",
            (query.strip()[:80] + ("…" if len(query.strip()) > 80 else "")), len(q_vec),
        )
    except Exception as e:
        logger.error(
            "semantic_search: query embedding failed: %s — returning empty; "
            "chunks are indexed/embedded but the query embedding call did not complete",
            e,
        )
        return []

    vecs = [c["embedding"] for c in with_emb]
    ids  = [c.get("chunk_id") for c in with_emb]
    scores = _cosine_scores(q_vec, vecs, chunk_ids=ids)

    results = [
        (float(s), c)
        for s, c in zip(scores, with_emb)
        if float(s) >= min_score
    ]
    results.sort(key=lambda x: -x[0])
    logger.info(
        "semantic_search score summary: scores_over_threshold=%d returned=%d max_score=%.4f",
        len(results), min(len(results), top_k), max(scores) if scores else 0.0,
    )
    return results[:top_k]


# ─────────────────────────────────────────────────────────────────────────────
# Backfill utility — embed existing chunks and save to disk
# ─────────────────────────────────────────────────────────────────────────────

def embed_and_save_chunks(chunks_path: str, chunks: List[Dict]) -> int:
    """
    Generate embeddings for all chunks that are missing them, then save
    the updated list back to chunks_path.

    Returns the count of newly embedded chunks (0 if all already had embeddings).
    Call this once per project after enabling SEARCH_BACKEND=semantic.
    """
    missing = [c for c in chunks if not c.get("embedding")]
    if not missing:
        logger.info("embed_and_save_chunks: all %d chunks already embedded.", len(chunks))
        return 0

    logger.info(
        "embed_and_save_chunks: embedding %d chunks in %s …", len(missing), chunks_path
    )
    try:
        texts = [c.get("chunk_text", "") for c in missing]
        embeddings = embed_texts(texts)
        for chunk, emb in zip(missing, embeddings):
            chunk["embedding"] = emb
    except Exception as e:
        logger.error("embed_and_save_chunks: API error: %s", e)
        return 0

    Path(chunks_path).write_text(
        json.dumps(chunks, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info(
        "embed_and_save_chunks: saved %d new embeddings → %s", len(missing), chunks_path
    )
    return len(missing)
