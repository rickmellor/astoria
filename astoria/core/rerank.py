"""Rerank — optional cross-encoder stage over recall's top-N candidates (TEI `POST /rerank`).

Motivation: nomic cosine + BM25 rank a signal generator above the user's spouse for "tell me about my
family" — bi-encoder noise over short personal queries vs long spec-laden hooks. A cross-encoder reads
(query, hook) jointly and fixes that class of error cheaply: ms-marco-MiniLM-L-6-v2 (22M params) reranks
30 hooks in ~50-150 ms on the NAS CPU (`astoria-rerank`, :8935, deploy/nas/docker-compose.yml).

Same shape as embed.py: a prioritized endpoint list (`ASTORIA_RERANK_URLS="url|model,url|model"`), a
served-model assertion via GET /info (model id / served name mentions rerank|minilm|bge, or TEI reports a
`reranker` model_type), a 60 s cooldown on failure, and degrade-don't-fail: `rerank()` NEVER raises —
None means "skip the stage, keep the base ranking" and recall reports `health.rerank="down"`.

Scores are the cross-encoder's raw logits (TEI `raw_scores=true`; this model's activation is Identity,
so it is what sentence-transformers' CrossEncoder.predict returns). recall.py blends sigmoid(logit),
min-max normalised over the reranked set, with the normalised base score — see `blend()`.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import OrderedDict

import httpx

from astoria.config import settings

log = logging.getLogger("astoria.rerank")

COOLDOWN_S = 60.0
WRONG_MODEL_COOLDOWN_S = 600.0
MODEL_HINTS = ("rerank", "minilm", "bge")
# Cross-encoder cost is token-linear and CPU-bound on the NAS (~0.3 ms/token, all cores): 30 mixed hooks of
# which 12 are 350-char episode hooks = ~700 ms. Capped at 240 chars (≈60 tokens, still the whole gist of a
# hook) and with recall limiting episodes to 6, top_n=30 facts is ~300-350 ms. Bump only with a GPU reranker.
MAX_TEXT_CHARS = 240
CACHE_MAX = 4096                # (query, text) → logit; repeated prompts (ambient-memory clients) rerank for free
# A candidate set whose logits all sit within this spread is "the reranker has no opinion" (every hook
# ~-11 for ms-marco MiniLM); min-max normalising that would amplify noise into the ranking, so blend()
# leaves the base order alone.
MIN_LOGIT_SPREAD = 1.0

_lock = threading.Lock()
_state: dict[str, dict] = {}     # url -> {"fail_until": t, "verified": bool, "model": str, "last_ms": float, "error": str}
_cache: "OrderedDict[tuple[str, str], float]" = OrderedDict()


# ---------------------------------------------------------------------------
# endpoints / state (mirrors embed.py)

def endpoints() -> list[tuple[str, str]]:
    """[(base_url, model), ...] in priority order from ASTORIA_RERANK_URLS; [] → stage off."""
    raw = (getattr(settings(), "rerank_urls", "") or "").strip()
    out: list[tuple[str, str]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        url, _, model = part.partition("|")
        url = url.strip().rstrip("/")
        if url:
            out.append((url, (model or "").strip() or "reranker"))
    return out


def enabled() -> bool:
    """Stage configured and not kill-switched (per-request `rerank=False` is handled by recall)."""
    return bool(getattr(settings(), "rerank_enabled", True)) and bool(endpoints())


def _usable(base: str) -> bool:
    st = _state.get(base)
    return not st or st.get("fail_until", 0) <= time.time()


def _mark_fail(base: str, err: str, cooldown: float = COOLDOWN_S) -> None:
    with _lock:
        st = _state.setdefault(base, {})
        st["fail_until"] = time.time() + cooldown
        st["error"] = err
    log.warning("rerank endpoint %s failed (%s); cooling down %ss", base, err, int(cooldown))


def _verify(client: httpx.Client, base: str, model: str) -> bool:
    """Served-model assertion: GET /info must describe a reranker (TEI `model_type: {reranker: ...}`) or
    name one (model_id / served_model_name mentions rerank|MiniLM|bge). The configured model name is
    deliberately NOT consulted — it is what we expect, not what is served."""
    info = client.get(f"{base}/info", timeout=4).json()
    names = " ".join(str(info.get(k) or "") for k in ("model_id", "served_model_name"))
    mt = info.get("model_type")
    is_reranker = isinstance(mt, dict) and "reranker" in mt
    name_ok = any(h in names.lower() for h in MODEL_HINTS)
    if not (is_reranker or name_ok):
        log.error("rerank endpoint %s: served model %r is not a reranker — disabled", base, names.strip())
        return False
    return True


def _post(client: httpx.Client, base: str, query: str, texts: list[str]) -> list[float | None]:
    r = client.post(f"{base}/rerank", json={"query": query, "texts": texts, "truncate": True, "raw_scores": True})
    r.raise_for_status()
    out: list[float | None] = [None] * len(texts)
    for row in r.json():
        i = int(row["index"])
        if 0 <= i < len(texts):
            out[i] = float(row["score"])
    return out


# ---------------------------------------------------------------------------
# public API

def rerank(query: str, docs: list[str]) -> list[float | None] | None:
    """Cross-encoder logits for (query, doc) pairs, one per doc, in input order — from the first usable
    endpoint. None (never raises) when the stage is disabled or every endpoint is down/cooling."""
    if not docs:
        return []
    query = (query or "").strip()
    if not query or not enabled():
        return None
    s = settings()
    texts = [(d or "")[:MAX_TEXT_CHARS] or " " for d in docs]
    out: list[float | None] = [None] * len(texts)
    todo: list[int] = []
    with _lock:
        for i, t in enumerate(texts):
            v = _cache.get((query, t))
            if v is not None:
                _cache.move_to_end((query, t))
                out[i] = v
            else:
                todo.append(i)
    if not todo:
        return out
    timeout = float(getattr(s, "rerank_timeout_s", 3.0) or 3.0)
    for base, model in endpoints():
        if not _usable(base):
            continue
        try:
            with httpx.Client(timeout=timeout) as client:
                st = _state.setdefault(base, {})
                if not st.get("verified"):
                    if not _verify(client, base, model):
                        _mark_fail(base, "not a reranker", WRONG_MODEL_COOLDOWN_S)
                        continue
                    st["verified"] = True
                    st["model"] = model
                t0 = time.time()
                # TEI caps client batches (max_client_batch_size, 32 on the NAS); 16 keeps us under any config.
                for j in range(0, len(todo), 16):
                    idx = todo[j:j + 16]
                    for i, v in zip(idx, _post(client, base, query, [texts[i] for i in idx])):
                        out[i] = v
                st["last_ms"] = (time.time() - t0) * 1000
                st["fail_until"] = 0
                st["error"] = None
            with _lock:
                for i in todo:
                    if out[i] is not None:
                        _cache[(query, texts[i])] = out[i]
                while len(_cache) > CACHE_MAX:
                    _cache.popitem(last=False)
            return out
        except Exception as e:  # noqa: BLE001
            _mark_fail(base, f"{type(e).__name__}: {e}")
            continue
    log.warning("all rerank endpoints unavailable (keeping base ranking)")
    return None


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def _minmax(xs: list[float]) -> list[float]:
    lo, hi = min(xs), max(xs)
    if hi - lo <= 1e-12:
        return [1.0] * len(xs)
    return [(x - lo) / (hi - lo) for x in xs]


def blend(base_scores: list[float], logits: list[float | None], weight: float) -> list[float]:
    """final = (1-w)·norm(score) + w·norm(sigmoid(logit)), both min-max normalised over the set, then mapped
    back into the base-score range so items below the reranked window keep a comparable scale and sort
    below it. Items without a logit keep their base score. If the reranker's logit spread is below
    MIN_LOGIT_SPREAD (no opinion), the base scores are returned unchanged."""
    n = len(base_scores)
    if n == 0 or len(logits) != n:
        return list(base_scores)
    have = [i for i in range(n) if logits[i] is not None]
    if len(have) < 2:
        return list(base_scores)
    w = min(1.0, max(0.0, float(weight)))
    lg = [float(logits[i]) for i in have]  # type: ignore[arg-type]
    if max(lg) - min(lg) < MIN_LOGIT_SPREAD:
        return list(base_scores)
    base = [float(base_scores[i]) for i in have]
    lo, hi = min(base), max(base)
    nb = _minmax(base)
    nr = _minmax([sigmoid(x) for x in lg])
    out = list(base_scores)
    for k, i in enumerate(have):
        f = (1.0 - w) * nb[k] + w * nr[k]
        out[i] = lo + (hi - lo) * f
    return out


def rerank_health() -> dict:
    """Per-endpoint status for /health; 'ok' = stage enabled and at least one endpoint usable (probes
    unverified ones). `status` is the same word recall reports: on | off | down."""
    s = settings()
    eps = []
    active = None
    on = bool(getattr(s, "rerank_enabled", True))
    for base, model in endpoints():
        st = _state.get(base, {})
        usable = _usable(base)
        if usable and not st.get("verified"):
            try:
                with httpx.Client(timeout=4) as client:
                    if _verify(client, base, model):
                        _state.setdefault(base, {}).update(verified=True, model=model, fail_until=0, error=None)
                    else:
                        _mark_fail(base, "not a reranker", WRONG_MODEL_COOLDOWN_S)
                        usable = False
            except Exception as e:  # noqa: BLE001
                _mark_fail(base, f"{type(e).__name__}")
                usable = False
        st = _state.get(base, {})
        eps.append({"url": base, "model": model, "usable": usable, "verified": bool(st.get("verified")),
                    "last_ms": round(st.get("last_ms", 0) or 0, 1), "error": st.get("error") if not usable else None})
        if usable and active is None:
            active = base
    status = "off" if not (on and eps) else ("on" if active else "down")
    return {"ok": status == "on", "status": status, "enabled": on, "active": active, "endpoints": eps,
            "top_n": int(getattr(s, "rerank_top_n", 20)), "weight": float(getattr(s, "rerank_weight", 0.6)),
            "cache": len(_cache)}


def reset_state() -> None:  # tests
    with _lock:
        _state.clear()
        _cache.clear()
