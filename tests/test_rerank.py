"""Rerank stage tests.

  * unit: astoria.core.rerank.rerank() against a monkeypatched TEI endpoint (no network) — endpoint
    verification, ordering, cooldown, never-raises; blend() math.
  * integration: recall() against the local dev DB (postgresql://astoria:astoria@127.0.0.1:55432/astoria)
    with rerank.rerank monkeypatched (no reranker network; embeddings still use the configured TEI like
    tests/test_recall.py). Skips when the DB is down.
"""
from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timezone

import httpx
import pytest

from astoria.core import rerank as RR

NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# fake TEI

class _FakeTEI:
    """Minimal stand-in for httpx.Client: GET /info + POST /rerank. Scores = +10 for docs containing a
    keyword of the query, else -11 (shape of ms-marco MiniLM logits)."""

    def __init__(self, *, info=None, fail=False, model_id="/models/rerank", reranker=True, log=None):
        self.info = info if info is not None else {
            "model_id": model_id, "model_type": ({"reranker": {"id2label": {"0": "LABEL_0"}}} if reranker else {"embedding": {}}),
        }
        self.fail = fail
        self.log = log if log is not None else []

    # httpx.Client protocol subset
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, timeout=None):
        self.log.append(("GET", url))
        if self.fail:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json=self.info, request=httpx.Request("GET", url))

    def post(self, url, json=None, **kw):
        self.log.append(("POST", url, json))
        if self.fail:
            raise httpx.ConnectError("boom")
        assert url.endswith("/rerank")
        assert json["truncate"] is True and json["raw_scores"] is True
        q = set(json["query"].lower().split())
        rows = []
        for i, t in enumerate(json["texts"]):
            hit = any(w in t.lower() for w in q if len(w) > 2)
            rows.append({"index": i, "score": 10.0 if hit else -11.0})
        rows.sort(key=lambda r: -r["score"])  # TEI returns sorted by score desc
        return httpx.Response(200, json=rows, request=httpx.Request("POST", url))


@pytest.fixture
def rerank_env(monkeypatch):
    """Point settings at one fake endpoint; reset endpoint state."""
    from astoria.config import reset_settings_cache
    monkeypatch.setenv("ASTORIA_RERANK_URLS", "http://fake:1|cross-encoder/ms-marco-MiniLM-L-6-v2")
    monkeypatch.setenv("ASTORIA_RERANK_ENABLED", "true")
    monkeypatch.setenv("ASTORIA_WORKER_ENABLED", "false")
    reset_settings_cache()
    RR.reset_state()
    yield
    reset_settings_cache()
    RR.reset_state()


def _use(monkeypatch, fake: _FakeTEI):
    monkeypatch.setattr(RR.httpx, "Client", lambda *a, **k: fake)
    return fake


# ---------------------------------------------------------------------------
# unit: rerank()

def test_endpoints_parse(rerank_env, monkeypatch):
    from astoria.config import reset_settings_cache
    assert RR.endpoints() == [("http://fake:1", "cross-encoder/ms-marco-MiniLM-L-6-v2")]
    monkeypatch.setenv("ASTORIA_RERANK_URLS", " http://a:1/ |m1 , http://b:2 ,, ")
    reset_settings_cache()
    assert RR.endpoints() == [("http://a:1", "m1"), ("http://b:2", "reranker")]
    monkeypatch.setenv("ASTORIA_RERANK_URLS", "")
    reset_settings_cache()
    assert RR.endpoints() == [] and RR.enabled() is False
    assert RR.rerank("q", ["a"]) is None


def test_rerank_scores_in_input_order(rerank_env, monkeypatch):
    fake = _use(monkeypatch, _FakeTEI())
    out = RR.rerank("tell me about my family", ["rick spouse: Jennifer", "rick family: son Paxton", "rick owns equipment: Fluke 87V"])
    assert out == [-11.0, 10.0, -11.0]
    # verified via /info once, then a single /rerank POST
    assert [m for m, *_ in fake.log] == ["GET", "POST"]
    # second call: no re-verify
    RR.rerank("spouse", ["rick spouse: Jennifer"])
    assert [m for m, *_ in fake.log] == ["GET", "POST", "POST"]
    # (query, text) LRU: identical pairs never hit the endpoint again; a partial overlap posts only the misses
    assert RR.rerank("spouse", ["rick spouse: Jennifer"]) == [10.0]
    assert [m for m, *_ in fake.log] == ["GET", "POST", "POST"]
    assert RR.rerank("spouse", ["rick spouse: Jennifer", "rick has pet: Pineapple"]) == [10.0, -11.0]
    assert fake.log[-1][2]["texts"] == ["rick has pet: Pineapple"]
    assert RR.rerank_health()["cache"] == 5
    assert RR.rerank("q", []) == []


def test_rerank_batches_large_sets(rerank_env, monkeypatch):
    fake = _use(monkeypatch, _FakeTEI())
    docs = [f"doc {i}" for i in range(40)]
    out = RR.rerank("doc", docs)
    assert out is not None and len(out) == 40
    posts = [x for x in fake.log if x[0] == "POST"]
    assert len(posts) == 3 and [len(p[2]["texts"]) for p in posts] == [16, 16, 8]


def test_rerank_rejects_non_reranker_and_cools_down(rerank_env, monkeypatch):
    fake = _use(monkeypatch, _FakeTEI(model_id="/models/nomic-embed-text-v1.5", reranker=False))
    assert RR.rerank("q", ["a", "b"]) is None
    st = RR._state["http://fake:1"]
    assert st["fail_until"] > RR.time.time() + 300  # long cooldown for wrong model
    assert RR.rerank_health()["status"] == "down"


def test_rerank_never_raises_and_cools_down_on_error(rerank_env, monkeypatch):
    fake = _use(monkeypatch, _FakeTEI(fail=True))
    assert RR.rerank("q", ["a", "b"]) is None
    st = RR._state["http://fake:1"]
    assert 0 < st["fail_until"] - RR.time.time() <= RR.COOLDOWN_S
    assert not RR._usable("http://fake:1")
    # while cooling, no network call is attempted
    n = len(fake.log)
    assert RR.rerank("q", ["a"]) is None and len(fake.log) == n
    # cooldown elapsed → tried again, now healthy
    fake.fail = False
    st["fail_until"] = 0
    assert RR.rerank("one", ["q one", "two"]) == [10.0, -11.0]
    assert RR._state["http://fake:1"]["fail_until"] == 0
    h = RR.rerank_health()
    assert h["status"] == "on" and h["ok"] and h["active"] == "http://fake:1"
    assert h["endpoints"][0]["verified"] is True and h["endpoints"][0]["usable"] is True


def test_rerank_health_off_when_disabled(rerank_env, monkeypatch):
    from astoria.config import reset_settings_cache
    monkeypatch.setenv("ASTORIA_RERANK_ENABLED", "false")
    reset_settings_cache()
    _use(monkeypatch, _FakeTEI())
    h = RR.rerank_health()
    assert h["status"] == "off" and h["ok"] is False and h["enabled"] is False
    assert RR.rerank("q", ["a"]) is None


# ---------------------------------------------------------------------------
# unit: blend()

def test_sigmoid():
    assert RR.sigmoid(0) == 0.5
    assert math.isclose(RR.sigmoid(-11), 1 / (1 + math.exp(11)))
    assert 0.0 < RR.sigmoid(-800) < 1e-300 or RR.sigmoid(-800) == 0.0  # no overflow
    assert RR.sigmoid(800) == 1.0


def test_blend_math_and_scale():
    base = [0.030, 0.020, 0.010, 0.005]          # base order: 0 > 1 > 2 > 3
    logits = [-11.0, -11.2, 8.0, -10.5]           # reranker loves #2
    out = RR.blend(base, logits, 0.6)
    # reranked window stays inside the base-score range (min..max), so non-reranked items below stay below
    assert min(out) >= min(base) - 1e-12 and max(out) <= max(base) + 1e-12
    # expected: #2 → (1-w)·norm(0.010) + w·1.0 ; #0 → (1-w)·1.0 + w·norm(sig(-11))
    nb = [(b - 0.005) / 0.025 for b in base]
    sig = [RR.sigmoid(x) for x in logits]
    nr = [(x - min(sig)) / (max(sig) - min(sig)) for x in sig]
    exp = [0.005 + 0.025 * (0.4 * a + 0.6 * b) for a, b in zip(nb, nr)]
    assert all(math.isclose(o, e, rel_tol=1e-9) for o, e in zip(out, exp))
    assert out[2] > out[0] > out[1] > out[3]
    # weight 0 → pure base order/values; weight 1 → pure reranker order
    exp_w0 = [0.005 + 0.025 * a for a in nb]
    assert all(math.isclose(o, e) for o, e in zip(RR.blend(base, logits, 0.0), exp_w0))
    w1 = RR.blend(base, logits, 1.0)
    assert sorted(range(4), key=lambda i: -w1[i]) == [2, 3, 0, 1]


def test_blend_no_opinion_keeps_base_order():
    base = [0.03, 0.02, 0.01]
    # all logits within MIN_LOGIT_SPREAD → untouched (no noise amplification)
    assert RR.blend(base, [-11.0, -11.3, -11.1], 0.6) == base
    # missing logits keep their base score; fewer than 2 scored → untouched
    assert RR.blend(base, [None, 5.0, None], 0.6) == base
    out = RR.blend(base, [None, 5.0, -5.0], 0.6)
    assert out[0] == 0.03 and out[1] > out[2]
    # length mismatch / empty → untouched
    assert RR.blend(base, [1.0], 0.6) == base and RR.blend([], [], 0.6) == []


# ---------------------------------------------------------------------------
# integration: recall() with a monkeypatched reranker against the local dev DB

@pytest.fixture(scope="module")
def uid():
    from astoria.store import db, facts
    try:
        with db.conn() as c:
            c.execute("SELECT 1 FROM schema_migrations LIMIT 1")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"dev DB not reachable: {e}")
    user_id = f"t_rerank_{uuid.uuid4().hex[:8]}"
    with db.conn() as c:
        facts.upsert_fact(c, user_id=user_id, subject="I", predicate="spouse", value="Jennifer — wife, married 1998", source="cli")
        facts.upsert_fact(c, user_id=user_id, subject="I", predicate="has_pet", value="Portuguese water dog Pineapple", source="cli")
        facts.upsert_fact(c, user_id=user_id, subject="I", predicate="owns_equipment",
                          value="[signal generator] Siglent SDG1032X — 2-ch 30 MHz arbitrary/function generator", source="cli")
        facts.upsert_fact(c, user_id=user_id, subject="I", predicate="owns_equipment",
                          value="[multimeter] Fluke 87V — industrial true-RMS DMM", source="cli")
        facts.upsert_fact(c, user_id=user_id, subject="I", predicate="favorite_beer", value="IPA", source="cli")
    yield user_id
    with db.conn() as c:
        for t in ("snapshot", "audit", "tombstone", "fact", "episode", "profile"):
            c.execute(f"DELETE FROM {t} WHERE user_id=%s", (user_id,))


def _recall(uid, query, **kw):
    from astoria.store import db
    from astoria.retrieval import recall as R
    with db.conn() as c:
        return R.recall(c, user_id=uid, query=query, **kw)


def test_recall_rerank_reorders_and_reports(uid, monkeypatch):
    from astoria.retrieval import recall as R
    calls: list[tuple[str, list[str]]] = []

    def fake(query, docs):
        calls.append((query, list(docs)))
        # spouse/pet hooks win, equipment loses hard; everything else mildly negative
        out = []
        for d in docs:
            dl = d.lower()
            out.append(6.0 if ("spouse" in dl or "pet" in dl) else (-11.0 if "equipment" in dl else -8.0))
        return out

    monkeypatch.setattr(R._rerank, "rerank", fake)
    monkeypatch.setattr(R._rerank, "enabled", lambda: True)
    res = _recall(uid, "tell me about my family", limit=12)
    assert res["health"]["rerank"] == "on"
    assert calls and calls[0][0] == "tell me about my family"
    # reranker saw the hooks of the candidates (≤ top_n), in one call
    assert len(calls) == 1 and all(isinstance(d, str) and d for d in calls[0][1])
    items = res["items"]
    assert items, res
    preds = [it["predicate"] for it in items]
    top2 = set(preds[:2])
    assert top2 <= {"spouse", "has_pet"}, preds
    if "owns_equipment" in preds:
        assert preds.index("owns_equipment") > max(preds.index("spouse"), preds.index("has_pet"))
    # rerank_score surfaced (the raw logit), scores stay JSON-safe and sorted
    assert all("rerank_score" in it for it in items)
    assert [it["score"] for it in items] == sorted((it["score"] for it in items), reverse=True)
    json.dumps(res)

    # bypass flag: no reranker call, no rerank_score, health "off"
    n = len(calls)
    res_off = _recall(uid, "tell me about my family", rerank=False)
    assert len(calls) == n and res_off["health"]["rerank"] == "off"
    assert all("rerank_score" not in it for it in res_off["items"])


def test_recall_rerank_down_keeps_base_ranking(uid, monkeypatch):
    from astoria.retrieval import recall as R
    monkeypatch.setattr(R._rerank, "enabled", lambda: True)
    monkeypatch.setattr(R._rerank, "rerank", lambda q, d: None)
    res_down = _recall(uid, "what multimeters do I have")
    assert res_down["health"]["rerank"] == "down"
    res_off = _recall(uid, "what multimeters do I have", rerank=False)
    assert [it["id"] for it in res_down["items"]] == [it["id"] for it in res_off["items"]]
    assert [it["score"] for it in res_down["items"]] == [it["score"] for it in res_off["items"]]
    assert all("rerank_score" not in it for it in res_down["items"])


def test_recall_rerank_disabled_by_settings(uid, monkeypatch):
    from astoria.retrieval import recall as R
    monkeypatch.setattr(R._rerank, "enabled", lambda: False)
    called = []
    monkeypatch.setattr(R._rerank, "rerank", lambda q, d: called.append(1) or [])
    res = _recall(uid, "favorite beer")
    assert res["health"]["rerank"] == "off" and not called
    assert res["items"] and res["items"][0]["value"] == "IPA"


def test_recall_rerank_respects_top_n(uid, monkeypatch):
    from astoria.retrieval import recall as R
    from astoria.config import reset_settings_cache
    monkeypatch.setenv("ASTORIA_RERANK_TOP_N", "2")
    monkeypatch.setenv("ASTORIA_WORKER_ENABLED", "false")
    reset_settings_cache()
    try:
        seen = []
        monkeypatch.setattr(R._rerank, "enabled", lambda: True)
        monkeypatch.setattr(R._rerank, "rerank", lambda q, d: seen.append(len(d)) or [0.0] * len(d))
        res = _recall(uid, "equipment multimeter signal generator beer spouse dog")
        assert res["health"]["rerank"] == "on"
        assert seen == [2]   # 2 fact candidates (no episodes for this user)
    finally:
        reset_settings_cache()
