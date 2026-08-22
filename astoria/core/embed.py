"""Embeddings — nomic-embed-text-v1.5 (768-d) from a prioritized list of OpenAI-compatible endpoints.

Primary = the workstation nomic seat via SAINT (`saint-local-embed`, Threadripper — ~4× faster than the
NAS), fallback = the always-on NAS TEI (`memoryos-tei`, :8931). Both were verified to produce the SAME
vector space (same-text cosine 1.0000); a canary cross-check re-verifies that at runtime and disables a
drifted endpoint rather than split the index. Vectors are L2-normalised client-side (vLLM returns raw
pooled vectors; TEI returns unit vectors) so every stored vector is comparable.

Degrade-don't-fail: every endpoint down → None (BM25-only recall; curator backfills later). A failing
endpoint is cooled down for ~60 s so the workstation is picked up again within a minute of power-on.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import OrderedDict

import httpx

from astoria.config import settings

log = logging.getLogger("astoria.embed")

PREFIX_DOC = "search_document: "
PREFIX_QUERY = "search_query: "
CANARY_TEXT = "search_document: astoria embedding canary — nomic vector space check"
CANARY_MIN_COS = 0.98
COOLDOWN_S = 60.0
CACHE_MAX = 1024

_lock = threading.Lock()
_state: dict[str, dict] = {}          # url -> {"fail_until": t, "model": str, "last_ms": float, "verified": bool}
_canary_ref: list[float] | None = None
_canary_src: str | None = None
_cache: "OrderedDict[str, list[float]]" = OrderedDict()


def endpoints() -> list[tuple[str, str]]:
    """[(base_url, model), ...] in priority order from ASTORIA_EMBED_URLS ("url|model,url|model"),
    else the single ASTORIA_EMBED_URL."""
    s = settings()
    out: list[tuple[str, str]] = []
    raw = (getattr(s, "embed_urls", "") or "").strip()
    if raw:
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            url, _, model = part.partition("|")
            out.append((url.rstrip("/"), model or "nomic"))
    if not out:
        out.append((s.embed_url.rstrip("/"), "nomic"))
    return out


def _norm(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b)) / ((math.sqrt(sum(x * x for x in a)) or 1) * (math.sqrt(sum(x * x for x in b)) or 1))


def _post(client: httpx.Client, base: str, model: str, texts: list[str]) -> tuple[list[list[float]], str]:
    r = client.post(f"{base}/v1/embeddings", json={"input": texts, "model": model})
    r.raise_for_status()
    data = r.json()
    vecs = [d["embedding"] for d in sorted(data["data"], key=lambda x: x["index"])]
    return vecs, str(data.get("model") or "")


def _verify(client: httpx.Client, base: str, model: str) -> bool:
    """Served-model assertion (response/model listing must mention nomic) + canary cross-check."""
    global _canary_ref, _canary_src
    s = settings()
    need = s.embed_require_substring.split("-")[0]  # "nomic"
    vecs, served = _post(client, base, model, [CANARY_TEXT])
    name_ok = need in served.lower() or need in model.lower()
    if not name_ok:
        try:  # TEI exposes /info; vLLM/llama.cpp expose /v1/models
            info = client.get(f"{base}/info", timeout=4).json()
            name_ok = need in str(info.get("served_model_name") or info.get("model_id") or "").lower()
        except Exception:  # noqa: BLE001
            try:
                ids = [m.get("id", "") + m.get("root", "") for m in client.get(f"{base}/v1/models", timeout=4).json().get("data", [])]
                name_ok = any(need in x.lower() for x in ids)
            except Exception:  # noqa: BLE001
                name_ok = False
    if not name_ok:
        log.error("embedding endpoint %s: served model %r is not nomic — disabled", base, served or model)
        return False
    vec = _norm(vecs[0])
    if len(vec) != s.embed_dim:
        log.error("embedding endpoint %s: dim %d != %d — disabled", base, len(vec), s.embed_dim)
        return False
    with _lock:
        if _canary_ref is None:
            _canary_ref, _canary_src = vec, base
        else:
            c = _cos(_canary_ref, vec)
            if c < CANARY_MIN_COS:
                log.error("embedding endpoint %s: canary cosine %.4f vs %s — DIFFERENT vector space, disabled",
                          base, c, _canary_src)
                return False
    return True


def _usable(base: str) -> bool:
    st = _state.get(base)
    return not st or st.get("fail_until", 0) <= time.time()


def _mark_fail(base: str, err: str) -> None:
    with _lock:
        _state.setdefault(base, {})["fail_until"] = time.time() + COOLDOWN_S
        _state[base]["error"] = err
    log.warning("embedding endpoint %s failed (%s); cooling down %ss", base, err, int(COOLDOWN_S))


def embed_texts(texts: list[str], *, query: bool = False) -> list[list[float] | None]:
    """Embed a batch; one unit vector (or None) per input. Never raises."""
    s = settings()
    if not texts:
        return []
    prefix = PREFIX_QUERY if query else PREFIX_DOC
    full = [prefix + (t or "")[: s.embed_max_chars] for t in texts]
    out: list[list[float] | None] = [None] * len(texts)
    todo: list[int] = []
    with _lock:
        for i, t in enumerate(full):
            v = _cache.get(t)
            if v is not None:
                _cache.move_to_end(t)
                out[i] = v
            else:
                todo.append(i)
    if not todo:
        return out
    for base, model in endpoints():
        if not _usable(base):
            continue
        try:
            with httpx.Client(timeout=s.embed_timeout_s) as client:
                st = _state.setdefault(base, {})
                if not st.get("verified"):
                    if not _verify(client, base, model):
                        st["fail_until"] = time.time() + 600  # wrong model/space: long cooldown
                        continue
                    st["verified"] = True
                    st["model"] = model
                t0 = time.time()
                for j in range(0, len(todo), 8):  # NAS TEI max-client-batch-size is 8
                    idx = todo[j:j + 8]
                    vecs, _ = _post(client, base, model, [full[i] for i in idx])
                    for i, v in zip(idx, vecs):
                        if len(v) == s.embed_dim:
                            out[i] = _norm(v)
                st["last_ms"] = (time.time() - t0) * 1000 / max(1, len(todo))
                st["fail_until"] = 0
            with _lock:
                for i in todo:
                    if out[i] is not None:
                        _cache[full[i]] = out[i]
                while len(_cache) > CACHE_MAX:
                    _cache.popitem(last=False)
            return out
        except Exception as e:  # noqa: BLE001
            _mark_fail(base, f"{type(e).__name__}: {e}")
            continue
    log.warning("all embedding endpoints unavailable (degrading to BM25-only)")
    return out


def embed_one(text: str, *, query: bool = False) -> list[float] | None:
    return embed_texts([text], query=query)[0]


def embed_health() -> dict:
    """Per-endpoint status; 'ok' = at least one endpoint currently usable (probes unverified ones)."""
    eps = []
    active = None
    for base, model in endpoints():
        st = _state.get(base, {})
        usable = _usable(base)
        if usable and not st.get("verified"):
            try:
                with httpx.Client(timeout=6) as client:
                    if _verify(client, base, model):
                        _state.setdefault(base, {}).update(verified=True, model=model, fail_until=0)
                    else:
                        _state.setdefault(base, {})["fail_until"] = time.time() + 600
                        usable = False
            except Exception as e:  # noqa: BLE001
                _mark_fail(base, f"{type(e).__name__}")
                usable = False
        st = _state.get(base, {})
        eps.append({"url": base, "model": model, "usable": usable, "verified": bool(st.get("verified")),
                    "last_ms": round(st.get("last_ms", 0) or 0, 1), "error": st.get("error") if not usable else None})
        if usable and active is None:
            active = base
    return {"ok": active is not None, "active": active, "model": settings().embed_require_substring,
            "endpoints": eps, "cache": len(_cache)}
