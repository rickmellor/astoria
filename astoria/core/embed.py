"""Embeddings via the pinned NAS TEI nomic endpoint (768-d, OpenAI-compatible /v1/embeddings).

Degrade-don't-fail: callers treat None as "no vector available" (BM25-only path); the
curator's embed_backfill later fills NULLs. Served-model assertion refuses a mismatched
model so we never mix vector spaces (the MemoryOS contract).
"""
from __future__ import annotations

import logging
import threading
import time

import httpx

from astoria.config import settings

log = logging.getLogger("astoria.embed")
_checked = {"ok": False, "at": 0.0, "model": None}
_lock = threading.Lock()

# nomic-embed-text-v1.5 expects task prefixes; TEI pools mean-wise over the text.
PREFIX_DOC = "search_document: "
PREFIX_QUERY = "search_query: "


def _assert_model(client: httpx.Client, base: str) -> bool:
    now = time.time()
    with _lock:
        if _checked["ok"] and now - _checked["at"] < 600:
            return True
    try:
        info = client.get(f"{base}/info", timeout=5).json()
        served = str(info.get("served_model_name") or info.get("model_id") or "")
        ok = settings().embed_require_substring in served
        with _lock:
            _checked.update(ok=ok, at=now, model=served)
        if not ok:
            log.error("embedding model mismatch: served=%r (need %r)", served, settings().embed_require_substring)
        return ok
    except Exception as e:  # noqa: BLE001
        log.warning("TEI /info unreachable: %s", e)
        return False


def embed_texts(texts: list[str], *, query: bool = False) -> list[list[float] | None]:
    """Embed a batch; returns one vector (or None) per input. Never raises."""
    s = settings()
    if not texts:
        return []
    base = s.embed_url.rstrip("/")
    prefix = PREFIX_QUERY if query else PREFIX_DOC
    out: list[list[float] | None] = [None] * len(texts)
    try:
        with httpx.Client(timeout=s.embed_timeout_s) as client:
            if not _assert_model(client, base):
                return out
            # TEI max-client-batch-size is small (8) on the NAS; chunk to be safe.
            for i in range(0, len(texts), 8):
                chunk = [prefix + (t or "")[: s.embed_max_chars] for t in texts[i:i + 8]]
                r = client.post(f"{base}/v1/embeddings", json={"input": chunk, "model": "nomic"})
                r.raise_for_status()
                data = r.json()["data"]
                for j, d in enumerate(sorted(data, key=lambda x: x["index"])):
                    vec = d["embedding"]
                    if len(vec) == s.embed_dim:
                        out[i + j] = vec
    except Exception as e:  # noqa: BLE001
        log.warning("embedding failed (degrading): %s", e)
    return out


def embed_one(text: str, *, query: bool = False) -> list[float] | None:
    return embed_texts([text], query=query)[0]


def embed_health() -> dict:
    s = settings()
    try:
        with httpx.Client(timeout=5) as client:
            ok = _assert_model(client, s.embed_url.rstrip("/"))
        return {"ok": ok, "model": _checked["model"], "url": s.embed_url}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "url": s.embed_url}
