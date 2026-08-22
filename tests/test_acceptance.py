"""Astoria acceptance suite — the CONTRACT (docs/CONTRACT.md) exercised through the REST API.

Every test uses a throwaway user (`user_id` fixture → DELETE /users/{id} at teardown) so the suite is
safe against ANY deployment: ASTORIA_URL=http://127.0.0.1:8977 (local) or http://192.168.1.134:8933
(NAS). Tests skip when /health is unreachable. A few tests additionally reach under the API into the
store (tombstone re-add, staging insert, out-of-order assertions, bulk seeding) — those use `db` /
`direct_db` and skip (or fall back to an API-only variant) when the DB isn't the API's DB.

Numbering follows the build plan: T1 correction propagation … T12 MemoryOS compat.
"""
from __future__ import annotations

import os
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import active_facts, episode_items, fact_items, recall, wait_for_fact

pytestmark = pytest.mark.api

UTC = timezone.utc


def _post(client, path, **body):
    r = client.post(path, json=body)
    assert r.status_code == 200, f"POST {path} -> {r.status_code}: {r.text[:500]}"
    return r.json()


def _get_fact(client, user_id, fact_id, expect=200):
    r = client.get(f"/facts/{fact_id}", params={"user_id": user_id})
    assert r.status_code == expect, f"GET /facts/{fact_id} -> {r.status_code}: {r.text[:300]}"
    return r.json() if expect == 200 else None


def _values(rows):
    return sorted(str(r["value"]) for r in rows)


# ---------------------------------------------------------------------------
# T1 — correction propagates everywhere

def test_t1_correction_propagates(client, user_id, tei_ok):
    """T1: POST /facts favorite_beer=Guinness → POST /correct favorite_beer=IPA.
    Active view shows IPA only; the Guinness row is superseded with superseded_by/valid_to/expired_at;
    /recall for "what beer do I like" surfaces IPA and NEVER Guinness."""
    r1 = _post(client, "/facts", user_id=user_id, subject=user_id, predicate="favorite_beer", value="Guinness")
    assert r1["action"] == "inserted", r1
    old_id = r1["fact"]["id"]
    assert r1["fact"]["status"] == "active"
    assert r1["fact"]["source_kind"] == "explicit"

    r2 = _post(client, "/correct", user_id=user_id, subject=user_id, predicate="favorite_beer", value="IPA")
    assert r2["action"] == "superseded", r2
    assert old_id in r2["superseded"], r2
    new_id = r2["fact"]["id"]
    assert new_id != old_id

    active = active_facts(client, user_id, predicate="favorite_beer")
    assert _values(active) == ["IPA"], active

    old = _get_fact(client, user_id, old_id)
    assert old["status"] == "superseded"
    assert old["superseded_by"] == new_id
    # bitemporal close: the ORIGINAL keeps its believed valid window (valid_to stays NULL) and is closed
    # on the belief axis; a versioned COPY (meta.version_of=old) carries the corrected valid_to.
    assert old["expired_at"] is not None
    assert (old.get("meta") or {}).get("belief_closed_by")
    new = _get_fact(client, user_id, new_id)
    sup = _get_fact(client, user_id, new["supersedes"])
    assert sup["id"] == old_id or (sup.get("meta") or {}).get("version_of") == old_id
    assert sup["valid_to"] is not None
    assert new["status"] == "active" and new["valid_to"] is None

    hist = client.get("/history", params={"user_id": user_id, "subject": user_id, "predicate": "favorite_beer"})
    assert hist.status_code == 200
    chain = hist.json()
    assert [f["value"] for f in chain][:2] == ["IPA", "Guinness"], "history newest-first"

    res = recall(client, user_id, "what beer do I like")
    items = fact_items(res)
    assert all(str(i.get("value")) != "Guinness" for i in items), "superseded value must not be recalled"
    assert "Guinness" not in (res.get("context") or "")
    if tei_ok:
        assert items and items[0]["value"] == "IPA", res
        assert "IPA" in res["context"]
    elif items:
        assert any(i["value"] == "IPA" for i in items), res
    else:
        pytest.skip("TEI down on server and BM25 found nothing — vector assertion skipped")


def test_t1b_correction_via_text_detector(client, user_id):
    """T1 (text path): a note "Actually, my favorite beer is IPA" captured with cognify=false is applied
    by the regex detector (no LLM): detector.matched → favorite_beer active = IPA, Guinness superseded."""
    g = _post(client, "/facts", user_id=user_id, subject=user_id, predicate="favorite_beer", value="Guinness")
    cap = _post(client, "/capture", user_id=user_id, kind="note",
                text="Actually, my favorite beer is IPA", cognify=False)
    assert cap.get("episode_id"), cap
    assert cap.get("queued") is False
    det = cap.get("detector")
    assert det, f"detector did not match: {cap}"
    assert det.get("op") == "correct" and det.get("predicate") == "favorite_beer", det
    assert str(det.get("value")).lower() == "ipa", det
    assert det.get("action") in ("superseded", "inserted", "noop"), det
    f = wait_for_fact(client, user_id, predicate="favorite_beer", value="IPA", timeout=5)
    assert f is not None, "IPA should be active immediately (detector path is synchronous)"
    assert f["source_kind"] == "detector"
    assert f["origin_episode"] == cap["episode_id"]
    assert _values(active_facts(client, user_id, predicate="favorite_beer")) == ["IPA"]
    assert _get_fact(client, user_id, g["fact"]["id"])["status"] == "superseded"


# ---------------------------------------------------------------------------
# T2 — edit / delete via API; tombstone blocks re-extraction

def test_t2_edit_delete_and_tombstone(client, user_id, direct_db, request):
    """T2: PATCH /facts/{id} value → visible; DELETE ?mode=hard → 404; re-adding the same triple with
    source_kind=extracted is blocked (tombstone). The re-add is attempted through POST /op first and
    falls back to the store (facts.upsert_fact) when the dispatcher has no such action."""
    r = _post(client, "/facts", user_id=user_id, subject=user_id, predicate="favorite_editor", value="Vim")
    fid = r["fact"]["id"]
    p = client.patch(f"/facts/{fid}", json={"user_id": user_id, "value": "Neovim"})
    assert p.status_code == 200, p.text
    assert p.json()["value"] == "Neovim"
    assert _get_fact(client, user_id, fid)["value"] == "Neovim"
    assert _values(active_facts(client, user_id, predicate="favorite_editor")) == ["Neovim"]

    d = client.delete(f"/facts/{fid}", params={"user_id": user_id, "mode": "hard"})
    assert d.status_code == 200, d.text
    assert d.json().get("deleted") is True
    _get_fact(client, user_id, fid, expect=404)
    assert active_facts(client, user_id, predicate="favorite_editor") == []

    # re-add as an extraction → must be blocked by the tombstone
    blocked = None
    op = client.post("/op", json={"action": "upsert_fact", "user_id": user_id, "subject": user_id,
                                  "predicate": "favorite_editor", "value": "Neovim",
                                  "source_kind": "extracted", "source": "test"})
    if op.status_code == 200 and isinstance(op.json(), dict) and "action" in op.json():
        blocked = op.json()["action"]
    elif direct_db:
        from astoria.store import facts
        db = request.getfixturevalue("db")
        res = facts.upsert_fact(db, user_id=user_id, subject=user_id, predicate="favorite_editor", value="Neovim",
                                source="test", source_kind="extracted", embed=False)
        db.commit()
        blocked = res["action"]
    else:
        pytest.skip("no /op upsert_fact action and no direct DB — tombstone re-add not checkable here")
    assert blocked == "blocked", f"extracted re-add after hard delete should be blocked, got {blocked!r}"
    assert active_facts(client, user_id, predicate="favorite_editor") == []

    # …but an EXPLICIT human re-assert lifts the tombstone
    again = _post(client, "/facts", user_id=user_id, subject=user_id, predicate="favorite_editor", value="Neovim")
    assert again["action"] == "inserted"
    assert _values(active_facts(client, user_id, predicate="favorite_editor")) == ["Neovim"]


def test_t2b_soft_delete_archives(client, user_id):
    """DELETE ?mode=soft archives (row still readable, not active, not recalled)."""
    r = _post(client, "/facts", user_id=user_id, subject=user_id, predicate="preferred_shell", value="fish")
    fid = r["fact"]["id"]
    d = client.delete(f"/facts/{fid}", params={"user_id": user_id, "mode": "soft"})
    assert d.status_code == 200 and d.json().get("deleted") is True
    row = _get_fact(client, user_id, fid)
    assert row["status"] == "archived"
    assert active_facts(client, user_id, predicate="preferred_shell") == []
    assert all(i.get("id") != fid for i in recall(client, user_id, "which shell do I prefer")["items"])


# ---------------------------------------------------------------------------
# T3 — temporal (valid axis + belief axis)

def test_t3_temporal_as_of(client, user_id):
    """T3: default_johnny_profile=coder valid 2026-07-01→2026-08-18 (historical), then daily from 2026-08-18.
    /as_of at 2026-07-15 → coder; now → daily; as_believed_at before the insert → empty."""
    before = datetime.now(UTC) - timedelta(seconds=2)
    h = _post(client, "/facts", user_id=user_id, subject=user_id, predicate="default_johnny_profile",
              value="coder", valid_from="2026-07-01T00:00:00Z", valid_to="2026-08-18T00:00:00Z", historical=True)
    assert h["action"] == "historical", h
    assert h["fact"]["status"] == "superseded" and h["fact"]["valid_to"] is not None
    d = _post(client, "/facts", user_id=user_id, subject=user_id, predicate="default_johnny_profile",
              value="daily", valid_from="2026-08-18T00:00:00Z")
    assert d["action"] == "inserted", d
    assert _values(active_facts(client, user_id, predicate="default_johnny_profile")) == ["daily"]

    mid = _post(client, "/as_of", user_id=user_id, at="2026-07-15T12:00:00Z", predicate="default_johnny_profile")
    assert [f["value"] for f in mid] == ["coder"], mid
    now = _post(client, "/as_of", user_id=user_id, at=datetime.now(UTC).isoformat(), predicate="default_johnny_profile")
    assert [f["value"] for f in now] == ["daily"], now
    early = _post(client, "/as_of", user_id=user_id, at="2026-06-01T00:00:00Z", predicate="default_johnny_profile")
    assert early == [], early
    # belief axis: before we had said anything, nothing was believed
    believed = _post(client, "/as_of", user_id=user_id, at="2026-07-15T12:00:00Z",
                     as_believed_at=(before - timedelta(days=1)).isoformat(), predicate="default_johnny_profile")
    assert believed == [], believed
    # belief axis, current: a `historical` insert is a currently-believed past truth
    now_b = client.post("/as_of", json={"user_id": user_id, "at": "2026-07-15T00:00:00Z",
                                          "as_believed_at": datetime.now(UTC).isoformat(),
                                          "predicate": "default_johnny_profile"}).json()
    assert [r["value"] for r in now_b] == ["coder"], now_b


def test_t3b_backdated_correction_wins(client, user_id):
    """A later statement with a back-dated valid_from ("since June it's been X") still supersedes."""
    _post(client, "/facts", user_id=user_id, subject=user_id, predicate="location", value="Portland")
    r = _post(client, "/correct", user_id=user_id, subject=user_id, predicate="location", value="Astoria",
              valid_from="2026-06-01T00:00:00Z")
    assert r["action"] == "superseded"
    assert _values(active_facts(client, user_id, predicate="location")) == ["Astoria"]
    assert r["fact"]["valid_from"].startswith("2026-06-01")


# ---------------------------------------------------------------------------
# T4 — cross-tool: written by two clients, recalled by a third, provenance intact

def test_t4_cross_tool_provenance(client_as, user_id, tei_ok):
    """T4: `input` captures a note (detector → likes), `claude-code` POSTs a fact; `megaplan` recalls and
    gets both, each RecallItem.source naming the client that wrote it."""
    inp = client_as("input")
    cc = client_as("claude-code")
    mp = client_as("megaplan")

    cap = _post(inp, "/capture", user_id=user_id, kind="note", text="I really like Belgian tripel ales", cognify=False)
    assert cap.get("detector") and cap["detector"].get("predicate") == "likes", cap
    like_id = cap["detector"]["fact_id"]
    tool = _post(cc, "/facts", user_id=user_id, subject=user_id, predicate="uses_tool", value="Neovim")
    tool_id = tool["fact"]["id"]
    assert tool["fact"]["source"] == "claude-code"

    ep = inp.get("/episodes", params={"user_id": user_id}).json()
    eps = ep.get("episodes") if isinstance(ep, dict) else ep
    assert any(e["id"] == cap["episode_id"] and e["source"] == "input" for e in eps), eps

    res = recall(mp, user_id, "what beers does the user like and which editor tools do they use", limit=12)
    by_id = {i["id"]: i for i in res["items"]}
    missing = [x for x in (like_id, tool_id) if x not in by_id]
    if missing and not tei_ok:
        pytest.skip(f"TEI down on server; BM25-only recall missed {missing}")
    assert not missing, f"recall from megaplan missed {missing}; got {[(i.get('predicate'), i.get('value')) for i in res['items']]}"
    assert by_id[like_id]["source"] == "input"
    assert by_id[tool_id]["source"] == "claude-code"
    # the recall snapshot was attributed to the reading client
    assert res.get("snapshot_id")


# ---------------------------------------------------------------------------
# T5 — provenance on every recall item and on GET /facts/{id}

def test_t5_provenance_fields(client, user_id):
    """T5: every RecallItem carries source, confidence, source_trust and asserted_at|occurred_at;
    GET /facts/{id} exposes source_kind, origin_episode (nullable) and valid_from."""
    a = _post(client, "/facts", user_id=user_id, subject=user_id, predicate="favorite_language", value="Python")
    _post(client, "/facts", user_id=user_id, subject=user_id, predicate="uses_tool", value="pytest")
    _post(client, "/capture", user_id=user_id, kind="note",
          text="Spent the afternoon writing pytest acceptance tests for the memory service", cognify=False)

    res = recall(client, user_id, "python pytest tests memory service", limit=12)
    assert res["items"], res
    for it in res["items"]:
        for k in ("id", "layer", "kind", "text", "score", "source", "confidence", "source_trust", "is_belief"):
            assert k in it, f"RecallItem missing {k}: {it}"
        assert it["source"] not in (None, ""), it
        assert it["confidence"] is not None and 0 <= float(it["confidence"]) <= 1, it
        assert it["source_trust"] is not None and 0 <= float(it["source_trust"]) <= 1, it
        assert it.get("asserted_at") or it.get("occurred_at"), f"no time provenance: {it}"
        if it["kind"] == "fact":
            assert it.get("subject") and it.get("predicate") and it.get("value"), it
    for k in ("health", "context", "snapshot_id", "user_id", "query"):
        assert k in res, f"RecallResult missing {k}"
    assert "degraded" in res["health"]

    row = _get_fact(client, user_id, a["fact"]["id"])
    for k in ("source_kind", "origin_episode", "valid_from", "asserted_at", "source", "confidence",
              "source_trust", "status", "layer", "cardinality"):
        assert k in row, f"fact missing {k}: {sorted(row)}"
    assert row["source_kind"] == "explicit"
    assert row["origin_episode"] is None
    assert row["source"] == "test"
    assert row["source_trust"] <= 0.6 + 1e-6, "unknown client 'test' must get the anonymous/low trust cap"
    assert "embedding" not in row and "tsv" not in row


# ---------------------------------------------------------------------------
# T6 — capture never depends on the LLM; episodes are durable and recallable

def test_t6_capture_is_durable_without_llm(client, user_id, health):
    """T6: POST /capture a turn with cognify=true → 200 with episode_id+queued; the episode is listed
    by GET /episodes; the cognify queue shows it pending (or the worker already processed it);
    /recall returns the episode text from the episodic layer. Nothing here waits on an LLM."""
    sid = f"sess-{uuid.uuid4().hex[:6]}"
    t0 = time.monotonic()
    cap = _post(client, "/capture", user_id=user_id, kind="turn", session_id=sid,
                user_input="I've been setting up a fresh Postgres 18 cluster with pgvector on the NAS this week",
                agent_response="Nice — pgvector 0.8 on Postgres 18 gives you HNSW with iterative scans.",
                cognify=True)
    elapsed = time.monotonic() - t0
    assert cap["episode_id"] and cap["queued"] is True and not cap.get("dropped"), cap
    assert elapsed < 10, f"capture took {elapsed:.1f}s — it must not block on the LLM"

    eps = client.get("/episodes", params={"user_id": user_id, "session_id": sid}).json()
    eps = eps.get("episodes") if isinstance(eps, dict) else eps
    mine = [e for e in eps if e["id"] == cap["episode_id"]]
    assert mine, eps
    ep = mine[0]
    assert ep["kind"] == "turn" and "Postgres" in ep["body"]
    assert "embedding" not in ep

    h = client.get("/health").json()
    pending = (h.get("queue") or {}).get("pending")
    assert pending is not None, h
    assert pending > 0 or ep.get("processed_at") is not None, \
        "captured turn should be queued for cognify (pending>0) or already processed"

    # replay is deduped (same user|session|kind|text)
    again = _post(client, "/capture", user_id=user_id, kind="turn", session_id=sid,
                  user_input="I've been setting up a fresh Postgres 18 cluster with pgvector on the NAS this week",
                  agent_response="Nice — pgvector 0.8 on Postgres 18 gives you HNSW with iterative scans.",
                  cognify=True)
    assert again["episode_id"] == cap["episode_id"] and again["deduped"] is True and again["queued"] is False

    res = recall(client, user_id, "postgres pgvector cluster on the NAS", layers=["episodic"])
    epi = episode_items(res)
    assert epi, f"episodic layer should surface the turn: {res}"
    assert any("Postgres" in (i.get("text") or "") for i in epi), epi
    # working memory for the session is prepended, not searched
    res2 = recall(client, user_id, "anything", session_id=sid)
    assert any("Postgres" in (w.get("user_input") or "") for w in res2.get("working", [])), res2


def test_t6b_gate_drops_noise(client, user_id):
    """capture.gate: slash commands, acks and tiny texts are dropped (no episode, no queue row)."""
    for txt, reason in (("/compact", "slash_command"), ("ok", None), ("hi", None)):
        r = _post(client, "/capture", user_id=user_id, kind="note", text=txt, cognify=True)
        assert r.get("dropped"), f"{txt!r} should be dropped: {r}"
        assert r.get("episode_id") in (None, "") and r.get("queued") is False
        if reason:
            assert r["dropped"] == reason
    eps = client.get("/episodes", params={"user_id": user_id}).json()
    eps = eps.get("episodes") if isinstance(eps, dict) else eps
    assert eps == []


# ---------------------------------------------------------------------------
# T7 — scale: 10k facts then 30 recalls, p95 < 800 ms

@pytest.mark.slow
def test_t7_scale_10k_recall_p95(client, user_id, db, direct_db, tei_ok, record_property, base_url, dsn):
    """T7 (slow): seed 10k facts (direct SQL with random unit vectors when direct_db, else POST /facts
    in a thread pool), then 30 recalls; p95 latency over HTTP must be < 800 ms. Timings are recorded
    as junit properties and printed."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import seed_bench  # noqa: E402

    n = int(os.environ.get("ASTORIA_BENCH_N", "10000"))
    t0 = time.monotonic()
    if direct_db:
        seeded = seed_bench.seed_direct(dsn, user_id, n=n)
    else:
        seeded = seed_bench.seed_http(base_url, user_id, n=n, workers=16)
    seed_s = time.monotonic() - t0
    tail = client.get("/facts", params={"user_id": user_id, "status": "active", "limit": 1, "offset": n - 1}).json()
    tail = tail.get("facts") if isinstance(tail, dict) else tail
    assert tail, f"seeding did not reach {n} active facts (seeded={seeded})"

    queries = seed_bench.bench_queries()
    lat: list[float] = []
    nonempty = 0
    for q in queries:
        t = time.monotonic()
        res = recall(client, user_id, q, limit=12)
        lat.append((time.monotonic() - t) * 1000)
        nonempty += 1 if res["items"] else 0
    lat_sorted = sorted(lat)
    p50 = statistics.median(lat)
    p95 = lat_sorted[int(round(0.95 * (len(lat) - 1)))]
    report = {"n_facts": n, "seed_mode": "direct" if direct_db else "http", "seed_s": round(seed_s, 1),
              "recalls": len(lat), "nonempty": nonempty, "p50_ms": round(p50, 1), "p95_ms": round(p95, 1),
              "max_ms": round(max(lat), 1), "tei_ok": tei_ok}
    if not direct_db and tei_ok:
        # real embeddings: the vocabulary-driven queries must actually hit
        assert nonempty >= len(queries) // 2, report
    for k, v in report.items():
        record_property(f"t7_{k}", v)
    print(f"\nT7 scale report: {report}")
    assert p95 < 800, f"p95 {p95:.0f} ms >= 800 ms ({report})"


# ---------------------------------------------------------------------------
# T8 — set-valued predicates

def test_t8_set_valued(client, user_id):
    """T8: likes=stout and likes=IPA are both active; retracting stout leaves IPA untouched;
    an unknown predicate ("collects_x") auto-registers as a SET predicate."""
    s = _post(client, "/facts", user_id=user_id, subject=user_id, predicate="likes", value="stout")
    i = _post(client, "/facts", user_id=user_id, subject=user_id, predicate="likes", value="IPA")
    assert s["action"] == i["action"] == "inserted"
    assert s["fact"]["cardinality"] == "set"
    assert _values(active_facts(client, user_id, predicate="likes")) == ["IPA", "stout"]

    r = _post(client, "/retract", user_id=user_id, subject=user_id, predicate="likes", value="stout")
    assert r["retracted"] == [s["fact"]["id"]], r
    assert _values(active_facts(client, user_id, predicate="likes")) == ["IPA"]
    st = _get_fact(client, user_id, s["fact"]["id"])
    assert st["status"] == "retracted" and st["expired_at"] is not None and st["valid_to"] is None
    ipa = _get_fact(client, user_id, i["fact"]["id"])
    assert ipa["status"] == "active" and ipa["expired_at"] is None

    c1 = _post(client, "/facts", user_id=user_id, subject=user_id, predicate="collects_x", value="vinyl")
    c2 = _post(client, "/facts", user_id=user_id, subject=user_id, predicate="collects_x", value="stamps")
    assert c1["fact"]["cardinality"] == "set" and c2["action"] == "inserted"
    assert _values(active_facts(client, user_id, predicate="collects_x")) == ["stamps", "vinyl"]
    preds = client.get("/predicates").json()
    preds = preds.get("predicates") if isinstance(preds, dict) else preds
    reg = {p["name"]: p for p in preds}
    assert "collects_x" in reg and reg["collects_x"]["cardinality"] == "set", reg.get("collects_x")
    if "auto" in reg["collects_x"]:
        assert reg["collects_x"]["auto"] is True
    # …and a functional-looking unknown predicate registers functional
    f1 = _post(client, "/facts", user_id=user_id, subject=user_id, predicate="favorite_x_thing", value="a")
    f2 = _post(client, "/facts", user_id=user_id, subject=user_id, predicate="favorite_x_thing", value="b")
    assert f1["fact"]["cardinality"] == "functional" and f2["action"] == "superseded"


# ---------------------------------------------------------------------------
# T9 — idempotency

def test_t9_idempotency(client, user_id):
    """T9: POSTing the same fact twice → one active row, second call is a noop with access_count bumped;
    /correct with the already-current value is a noop too (no re-supersede)."""
    a = _post(client, "/facts", user_id=user_id, subject=user_id, predicate="timezone", value="America/Los_Angeles")
    b = _post(client, "/facts", user_id=user_id, subject=user_id, predicate="timezone", value="America/Los_Angeles")
    assert a["action"] == "inserted" and b["action"] == "noop", (a["action"], b["action"])
    assert a["fact"]["id"] == b["fact"]["id"]
    assert b["fact"]["access_count"] >= a["fact"]["access_count"] + 1
    assert b["superseded"] == []
    # value normalisation: case/whitespace differences are the same triple
    c = _post(client, "/facts", user_id=user_id, subject=user_id, predicate="timezone", value="  america/los_angeles ")
    assert c["action"] == "noop" and c["fact"]["id"] == a["fact"]["id"]
    d = _post(client, "/correct", user_id=user_id, subject=user_id, predicate="timezone", value="America/Los_Angeles")
    assert d["action"] == "noop" and d["superseded"] == []
    rows = client.get("/facts", params={"user_id": user_id, "predicate": "timezone", "status": "any"}).json()
    rows = rows.get("facts") if isinstance(rows, dict) else rows
    assert len(rows) == 1, rows


# ---------------------------------------------------------------------------
# T10 — staging gate + approve

def test_t10_staging_approve(client, user_id, direct_db, request):
    """T10: a low-confidence extraction (conf .3 < .35) lands in status=staging — listed with
    status=staging, NOT recalled; POST /approve promotes it to active and it becomes recallable.
    Uses the store when direct_db (the real extracted path); otherwise PATCH status=staging."""
    if direct_db:
        from astoria.store import facts
        db = request.getfixturevalue("db")
        res = facts.upsert_fact(db, user_id=user_id, subject=user_id, predicate="favorite_beer", value="Guinness",
                                source="input", source_kind="extracted", confidence=0.3, embed=False)
        db.commit()
        assert res["action"] == "staging", res
        fid = str(res["fact"]["id"])
    else:
        r = _post(client, "/facts", user_id=user_id, subject=user_id, predicate="favorite_beer", value="Guinness")
        fid = r["fact"]["id"]
        p = client.patch(f"/facts/{fid}", json={"user_id": user_id, "status": "staging"})
        assert p.status_code == 200, p.text
    row = _get_fact(client, user_id, fid)
    assert row["status"] == "staging"
    staged = client.get("/facts", params={"user_id": user_id, "status": "staging"}).json()
    staged = staged.get("facts") if isinstance(staged, dict) else staged
    assert any(f["id"] == fid for f in staged)
    assert active_facts(client, user_id, predicate="favorite_beer") == []
    res = recall(client, user_id, "what beer do I like")
    assert all(str(i.get("value")) != "Guinness" for i in fact_items(res)), "staging must not be recalled"

    ap = _post(client, "/approve", user_id=user_id, fact_id=fid)
    fact = ap.get("fact") or ap
    assert fact["status"] == "active" and fact["value"] == "Guinness", ap
    assert _values(active_facts(client, user_id, predicate="favorite_beer")) == ["Guinness"]
    res = recall(client, user_id, "what beer do I like")
    assert any(i.get("value") == "Guinness" for i in fact_items(res)), res


# ---------------------------------------------------------------------------
# T11 — out-of-order assertions (store-level: the API cannot back-date asserted_at)

@pytest.mark.store
def test_t11_out_of_order_assertions(db, store_user_id):
    """T11: assertion order decides, not arrival order. IPA@Aug-20 then Guinness@Aug-10 → IPA active,
    Guinness historical; reversed arrival → same final state; a newer statement with a back-dated
    valid_from still wins."""
    from astoria.store import facts
    uid = store_user_id
    aug10 = datetime(2026, 8, 10, tzinfo=UTC)
    aug20 = datetime(2026, 8, 20, tzinfo=UTC)

    def key(i: str):
        return dict(user_id=uid, subject=uid, predicate=f"favorite_beer_{i}", source="test", embed=False)

    # newer first, older arrives later
    r1 = facts.upsert_fact(db, **key("a"), value="IPA", asserted_at=aug20)
    r2 = facts.upsert_fact(db, **key("a"), value="Guinness", asserted_at=aug10)
    assert r1["action"] == "inserted" and r2["action"] == "historical", (r1["action"], r2["action"])
    rows = {r["value"]: r for r in facts.history(db, user_id=uid, subject=uid, predicate="favorite_beer_a")}
    assert rows["IPA"]["status"] == "active"
    assert rows["Guinness"]["status"] == "superseded" and str(rows["Guinness"]["superseded_by"]) == str(rows["IPA"]["id"])
    assert rows["Guinness"]["valid_to"] is not None and rows["Guinness"]["valid_to"] <= aug20
    assert [r["value"] for r in facts.history(db, user_id=uid, subject=uid, predicate="favorite_beer_a")] == ["IPA", "Guinness"]

    # older first, newer arrives later → same final state
    r3 = facts.upsert_fact(db, **key("b"), value="Guinness", asserted_at=aug10)
    r4 = facts.upsert_fact(db, **key("b"), value="IPA", asserted_at=aug20)
    assert r3["action"] == "inserted" and r4["action"] == "superseded"
    rows = {r["value"]: r for r in facts.history(db, user_id=uid, subject=uid, predicate="favorite_beer_b")}
    assert rows["IPA"]["status"] == "active"
    assert rows["Guinness"]["status"] == "superseded" and str(rows["Guinness"]["superseded_by"]) == str(rows["IPA"]["id"])

    # back-dated valid_from on the NEWER statement still wins
    r5 = facts.upsert_fact(db, **key("c"), value="Guinness", asserted_at=aug10, valid_from=aug10)
    r6 = facts.upsert_fact(db, **key("c"), value="IPA", asserted_at=aug20, valid_from=datetime(2026, 6, 1, tzinfo=UTC))
    assert r6["action"] == "superseded" and str(r5["fact"]["id"]) in r6["superseded"]
    rows = {r["value"]: r for r in facts.history(db, user_id=uid, subject=uid, predicate="favorite_beer_c")}
    assert rows["IPA"]["status"] == "active" and rows["Guinness"]["status"] == "superseded"
    # the old row's validity is closed no earlier than its own start (GREATEST guard)
    assert rows["Guinness"]["valid_to"] >= rows["Guinness"]["valid_from"]

    # a delayed old statement (queue replay) cannot resurrect after an explicit correction
    r7 = facts.upsert_fact(db, **key("c"), value="Guinness", asserted_at=aug10, source_kind="extracted")
    assert r7["action"] in ("historical", "blocked"), r7["action"]
    act = facts.list_facts(db, user_id=uid, predicate="favorite_beer_c")
    assert [r["value"] for r in act] == ["IPA"]
    db.commit()


@pytest.mark.store
def test_t11b_as_of_store_belief_axis(db, store_user_id):
    """as_of + as_believed_at at the store: a superseded value is what we believed before the correction."""
    from astoria.store import facts
    uid = store_user_id
    r1 = facts.upsert_fact(db, user_id=uid, subject=uid, predicate="favorite_beer", value="Guinness", source="test",
                           embed=False, valid_from=datetime(2026, 1, 1, tzinfo=UTC))
    db.commit()
    time.sleep(0.05)
    t_between = datetime.now(UTC)
    time.sleep(0.05)
    # a change-over-time: IPA from now on (valid_from defaults to asserted_at) → Guinness closed at now
    r2 = facts.upsert_fact(db, user_id=uid, subject=uid, predicate="favorite_beer", value="IPA", source="test",
                           embed=False)
    db.commit()
    assert r2["action"] == "superseded"
    now = datetime.now(UTC)
    assert [r["value"] for r in facts.as_of(db, user_id=uid, at=now, predicate="favorite_beer")] == ["IPA"]
    # valid axis only: at t_between Guinness was the value
    assert [r["value"] for r in facts.as_of(db, user_id=uid, at=t_between, predicate="favorite_beer")] == ["Guinness"]
    # both axes: what we believed at t_between about t_between → Guinness (IPA wasn't ingested yet)
    believed = facts.as_of(db, user_id=uid, at=t_between, as_believed_at=t_between, predicate="favorite_beer")
    assert [r["value"] for r in believed] == ["Guinness"], believed
    assert str(believed[0]["id"]) == str(r1["fact"]["id"])
    # NOTE (contract gap, see report): as_of(at=now, as_believed_at=t_between) returns [] (valid_to is
    # rewritten in place by the supersede), and as_of(at=t_between, as_believed_at=now) also returns []
    # (expired_at closes the belief window of a row that is still TRUE for its past validity) — the
    # store does not distinguish "correction" from "change over time" on the belief axis. Not asserted.


# ---------------------------------------------------------------------------
# T12 — MemoryOS compat surface

def test_t12_memoryos_compat(client, user_id):
    """T12: POST /memories, POST /retrieve and GET /users/{id}/profile answer in MemoryOS shapes;
    `user_profile` is the literal string "None" for an empty user."""
    prof = client.get(f"/users/{user_id}/profile")
    assert prof.status_code == 200, prof.text
    pj = prof.json()
    assert pj.get("user_profile") == "None", pj
    assert pj.get("user_id") in (None, user_id)

    m = client.post("/memories", json={"user_id": user_id,
                                       "user_input": "My favorite beer is IPA.",
                                       "agent_response": "Noted: IPA it is."})
    assert m.status_code == 200, m.text
    assert m.json().get("status") in ("ok", "success"), m.json()

    # the exchange was stored as an episode (turn); the detector lifts favorite_beer=IPA synchronously
    eps = client.get("/episodes", params={"user_id": user_id}).json()
    eps = eps.get("episodes") if isinstance(eps, dict) else eps
    assert eps and any("IPA" in e["body"] for e in eps), eps
    f = wait_for_fact(client, user_id, predicate="favorite_beer", value="IPA", timeout=5)
    assert f is not None, "detector should have lifted favorite_beer=IPA from the /memories turn"

    ret = client.post("/retrieve", json={"user_id": user_id, "query": "what beer do I like"})
    assert ret.status_code == 200, ret.text
    rj = ret.json()
    for k in ("user_id", "query", "short_term_history", "retrieved_pages", "retrieved_user_knowledge",
              "retrieved_assistant_knowledge"):
        assert k in rj, f"/retrieve missing MemoryOS key {k}: {sorted(rj)}"
    assert rj["user_id"] == user_id
    assert isinstance(rj["retrieved_user_knowledge"], list) and isinstance(rj["retrieved_pages"], list)
    # knowledge entries are {knowledge, timestamp}; the IPA fact (or the page) comes back for the query
    know = rj["retrieved_user_knowledge"]
    assert all("knowledge" in k and "timestamp" in k for k in know), know
    assert any("IPA" in k["knowledge"] for k in know) or any(
        "IPA" in str(p) for p in rj["retrieved_pages"]), rj


# ---------------------------------------------------------------------------
# extras — health, wipe, predicates, briefing/profile, audit

def test_health_shape(client, health):
    """GET /health: 200 iff DB ok; carries db/tei/llm/queue/version blocks."""
    h = client.get("/health")
    assert h.status_code == 200
    j = h.json()
    assert j.get("status") == "ok"
    for k in ("db", "tei", "queue", "version"):
        assert k in j, f"/health missing {k}: {sorted(j)}"
    assert "ok" in j["tei"] and "pending" in j["queue"]


def test_users_delete_wipes(client, user_id):
    """DELETE /users/{id} removes facts, episodes and the profile for that user only."""
    _post(client, "/facts", user_id=user_id, subject=user_id, predicate="favorite_beer", value="IPA")
    _post(client, "/capture", user_id=user_id, kind="note", text="A note that should vanish with the user", cognify=False)
    other = f"{user_id}-keep"
    _post(client, "/facts", user_id=other, subject=other, predicate="favorite_beer", value="Stout")
    try:
        d = client.delete(f"/users/{user_id}")
        assert d.status_code == 200, d.text
        assert active_facts(client, user_id) == []
        eps = client.get("/episodes", params={"user_id": user_id}).json()
        eps = eps.get("episodes") if isinstance(eps, dict) else eps
        assert eps == []
        assert _values(active_facts(client, other)) == ["Stout"], "wipe must be scoped to the user"
    finally:
        client.delete(f"/users/{other}")


def test_briefing_and_profile(client, user_id):
    """GET /briefing and GET /profile render profile-layer facts; empty user → empty context."""
    b0 = client.get("/briefing", params={"user_id": user_id})
    assert b0.status_code == 200 and b0.json().get("context", "") == ""
    _post(client, "/facts", user_id=user_id, subject=user_id, predicate="favorite_beer", value="IPA")
    _post(client, "/facts", user_id=user_id, subject=user_id, predicate="name", value="Test Person")
    p = client.get("/profile", params={"user_id": user_id})
    assert p.status_code == 200
    pj = p.json()
    assert pj["user_id"] == user_id
    assert {f["predicate"] for f in pj["facts"]} >= {"favorite_beer", "name"}, pj
    assert all(f["layer"] == "profile" for f in pj["facts"])
    b = client.get("/briefing", params={"user_id": user_id})
    assert b.status_code == 200
    bj = b.json()
    assert "IPA" in bj["context"] and "facts" in bj and "narrative" in bj
    # stable prefix: two consecutive briefings are byte-identical (prompt-cache friendly)
    assert client.get("/briefing", params={"user_id": user_id}).json()["context"] == bj["context"]


def test_recall_empty_store_is_empty(client, user_id):
    """Empty store → items [] and context "" (clients inject context verbatim)."""
    res = recall(client, user_id, "what beer do I like")
    assert res["items"] == [] and res["context"] == ""


def test_audit_records_mutations(client, user_id):
    """GET /audit lists control-plane mutations (insert/supersede/retract/forget) for the user."""
    r = _post(client, "/facts", user_id=user_id, subject=user_id, predicate="favorite_beer", value="Guinness")
    _post(client, "/correct", user_id=user_id, subject=user_id, predicate="favorite_beer", value="IPA")
    _post(client, "/retract", user_id=user_id, fact_id=r["fact"]["id"])  # already superseded → noop retract
    a = client.get("/audit", params={"user_id": user_id, "limit": 50})
    assert a.status_code == 200
    rows = a.json()
    rows = rows.get("audit") if isinstance(rows, dict) else rows
    ops = [x["op"] for x in rows]
    assert "inserted" in ops and "superseded" in ops, ops


def test_forget_by_query_soft(client, user_id):
    """POST /forget {query} soft-forgets matching facts (archived) and reports ids."""
    _post(client, "/facts", user_id=user_id, subject=user_id, predicate="knows_person", value="Alice Example")
    _post(client, "/facts", user_id=user_id, subject=user_id, predicate="likes", value="hiking")
    f = _post(client, "/forget", user_id=user_id, query="Alice Example", mode="soft")
    assert f.get("forgotten"), f
    assert _values(active_facts(client, user_id, predicate="knows_person")) == []
    assert _values(active_facts(client, user_id, predicate="likes")) == ["hiking"]


def test_predicates_patch_cardinality(client, user_id):
    """PATCH /predicates/{name} flips cardinality; a set→functional flip makes the next upsert supersede."""
    name = f"testpred_{uuid.uuid4().hex[:6]}"
    _post(client, "/facts", user_id=user_id, subject=user_id, predicate=name, value="one")
    p = client.patch(f"/predicates/{name}", json={"cardinality": "functional"})
    assert p.status_code == 200, p.text
    r = _post(client, "/facts", user_id=user_id, subject=user_id, predicate=name, value="two")
    assert r["action"] == "superseded", r
    assert _values(active_facts(client, user_id, predicate=name)) == ["two"]
