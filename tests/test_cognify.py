"""Cognify pipeline tests — run against the local dev DB (postgresql://astoria:astoria@127.0.0.1:55432/astoria).

    pytest tests/test_cognify.py -q            # deterministic (no LLM)
    pytest tests/test_cognify.py -q -m llm     # + one real extraction through SAINT
"""
from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

os.environ.setdefault("ASTORIA_DB_DSN", "postgresql://astoria:astoria@127.0.0.1:55432/astoria")

from astoria.cognify import resolver, worker
from astoria.cognify.resolver import ExtractedFact, Extraction
from astoria.core.llm import LLMUnavailable
from astoria.curator import maintenance as curator
from astoria.store import db, facts


@pytest.fixture(scope="session", autouse=True)
def _migrated():
    db.migrate()
    yield
    db.close_pool()


@pytest.fixture
def user():
    uid = f"t_cog_{uuid.uuid4().hex[:8]}"
    yield uid
    with db.conn() as c:
        for t in ("cognify_queue", "fact", "episode", "tombstone", "audit", "snapshot", "profile_history", "profile"):
            c.execute(f"DELETE FROM {t} WHERE user_id=%s", (uid,))


def add_turn(c, user_id, body, session_id="s1", occurred_at=None, source="input", kind="turn"):
    occ = occurred_at or datetime.now(UTC)
    idem = hashlib.sha256(f"{user_id}|{session_id}|{kind}|{body}".encode()).hexdigest()
    r = c.execute(
        "INSERT INTO episode(user_id, kind, hook, body, occurred_at, source, session_id, idem_key) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id", (user_id, kind, body[:400], body, occ, source, session_id, idem)
    ).fetchone()
    return str(r["id"])


def enqueue(c, user_id, episode_id, session_id="s1", priority=5, attempts=0, max_attempts=5):
    r = c.execute(
        "INSERT INTO cognify_queue(user_id, episode_id, session_id, priority, attempts, max_attempts) "
        "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id", (user_id, episode_id, session_id, priority, attempts, max_attempts)
    ).fetchone()
    return r["id"]


def q_row(c, qid):
    return c.execute("SELECT * FROM cognify_queue WHERE id=%s", (qid,)).fetchone()


def active(c, user_id, predicate):
    return c.execute("SELECT * FROM fact WHERE user_id=%s AND predicate=%s AND status='active' ORDER BY value",
                     (user_id, predicate)).fetchall()


# ---------------------------------------------------------------------------
# apply()

def test_apply_assert_contradicts_retract_summary(user):
    with db.conn() as c:
        guinness = facts.upsert_fact(c, user_id=user, subject=user, predicate="favorite_beer", value="Guinness",
                                     source="cli", source_kind="explicit", embed=False)["fact"]
        vim = facts.upsert_fact(c, user_id=user, subject=user, predicate="uses_tool", value="vim",
                                source="cli", source_kind="explicit", embed=False)["fact"]
        facts.upsert_fact(c, user_id=user, subject=user, predicate="likes", value="lager",
                          source="cli", source_kind="explicit", embed=False)
        ep1 = add_turn(c, user, "User: hi\nAssistant: hello")
        ep2 = add_turn(c, user, "User: my favorite beer is IPA now, I switched from vim to neovim, "
                                "and I don't like lager anymore\nAssistant: noted")

    parsed = Extraction(
        summary=f"{user} prefers IPA, moved from vim to neovim, no longer likes lager.",
        nothing_durable=False,
        facts=[
            ExtractedFact(subject="I", predicate="favorite_beer", value="IPA", layer="profile", confidence=0.95,
                          action="assert", contradicts=[str(guinness["id"])], evidence="my favorite beer is IPA"),
            ExtractedFact(subject="me", predicate="uses_tool", value="neovim", layer="semantic", confidence=0.8,
                          action="assert", contradicts=[str(vim["id"]), "not-a-uuid"]),
            ExtractedFact(subject=user, predicate="likes", value="lager", action="retract", confidence=0.8),
            ExtractedFact(subject=user, predicate="learned_howto", value="restart saint: systemctl --user restart saint",
                          layer="procedural", confidence=0.2, valid_from="2026-06-01", is_belief=True),
        ])
    occ = datetime.now(UTC) + timedelta(seconds=1)   # must be newer than the explicit Guinness assertion
    with db.conn() as c:
        res = resolver.apply(c, user_id=user, episode_ids=[ep1, ep2], parsed=parsed, source="input",
                             session_id="s1", occurred_at=occ)

    assert [f["predicate"] for f in res["facts"]] == ["favorite_beer", "uses_tool", "learned_howto"]
    assert res["retracted"] and res["retracted"][0]["value"] == "lager"
    assert res["summary_episode"] is not None

    with db.conn() as c:
        fb = active(c, user, "favorite_beer")
        assert len(fb) == 1 and fb[0]["value"] == "IPA"
        assert fb[0]["source_kind"] == "extracted" and fb[0]["layer"] == "profile"
        assert abs(fb[0]["confidence"] - 0.85) < 1e-6          # clamped to .85
        assert fb[0]["asserted_at"] == occ
        assert str(fb[0]["origin_episode"]) == ep2
        old = c.execute("SELECT * FROM fact WHERE id=%s", (guinness["id"],)).fetchone()
        assert old["status"] == "superseded" and str(old["superseded_by"]) == str(fb[0]["id"])

        # set predicate: contradicts closed the named row, new member active
        ut = active(c, user, "uses_tool")
        assert [r["value"] for r in ut] == ["neovim"]
        oldvim = c.execute("SELECT status, superseded_by FROM fact WHERE id=%s", (vim["id"],)).fetchone()
        assert oldvim["status"] == "superseded" and str(oldvim["superseded_by"]) == ut[0]["id"].__str__()

        # retract path: retracted + tombstone that does NOT block explicit re-asserts
        lager = c.execute("SELECT status FROM fact WHERE user_id=%s AND predicate='likes' AND value='lager'",
                          (user,)).fetchone()
        assert lager["status"] == "retracted"
        ts = c.execute("SELECT * FROM tombstone WHERE user_id=%s AND predicate='likes'", (user,)).fetchone()
        assert ts and ts["reason"] == "extracted-retract" and ts["blocks"] == "none"

        # low confidence clamps to .3 (< staging threshold .35 → staging), dates parsed, belief kept
        how = c.execute("SELECT * FROM fact WHERE user_id=%s AND predicate='learned_howto'", (user,)).fetchone()
        assert how["status"] == "staging" and how["layer"] == "procedural" and how["is_belief"] is True
        assert how["valid_from"] == datetime(2026, 6, 1, tzinfo=UTC)

        # summary episode + turn demotion + processed_at
        summ = c.execute("SELECT * FROM episode WHERE id=%s", (res["summary_episode"],)).fetchone()
        assert summ["kind"] == "summary" and summ["session_id"] == "s1" and abs(summ["importance"] - 0.6) < 1e-6
        assert summ["idem_key"] == hashlib.sha256(f"{user}|s1|summary|{parsed.summary}".encode()).hexdigest()
        turns = c.execute("SELECT importance, processed_at FROM episode WHERE id = ANY(%s::uuid[])", ([ep1, ep2],)).fetchall()
        assert all(t["processed_at"] is not None for t in turns)
        assert all(abs(t["importance"] - 0.3) < 1e-6 for t in turns)

    # idempotent replay: same summary → same episode, facts NOOP
    with db.conn() as c:
        res2 = resolver.apply(c, user_id=user, episode_ids=[ep1, ep2], parsed=parsed, source="input",
                              session_id="s1", occurred_at=occ)
        assert res2["summary_episode"] == res["summary_episode"]
        assert res2["facts"][0]["action"] == "noop"
        assert len(active(c, user, "favorite_beer")) == 1


def test_apply_nothing_durable_marks_processed(user):
    with db.conn() as c:
        ep = add_turn(c, user, "User: ok\nAssistant: sure")
        res = resolver.apply(c, user_id=user, episode_ids=[ep], parsed=Extraction(nothing_durable=True),
                             source="input", session_id="s1")
        assert res == {"facts": [], "retracted": [], "summary_episode": None}
        row = c.execute("SELECT processed_at FROM episode WHERE id=%s", (ep,)).fetchone()
        assert row["processed_at"] is not None
        assert c.execute("SELECT count(*) AS n FROM episode WHERE user_id=%s AND kind='summary'", (user,)).fetchone()["n"] == 0


def test_near_duplicate_functional_value_noops(user, monkeypatch):
    # pretend the embedder says "IPA" ≈ "an IPA" (cosine 1.0) so the functional key is not flip-flopped
    monkeypatch.setattr(resolver, "embed_texts", lambda texts, **kw: [[1.0, 0.0]] * len(texts))
    with db.conn() as c:
        facts.upsert_fact(c, user_id=user, subject=user, predicate="favorite_beer", value="IPA",
                          source="cli", source_kind="explicit", embed=False)
        ep = add_turn(c, user, "User: I love an IPA\nAssistant: ok")
        res = resolver.apply(c, user_id=user, episode_ids=[ep], source="input", session_id="s1",
                             parsed=Extraction(facts=[ExtractedFact(subject=user, predicate="favorite_beer", value="an IPA")]))
        assert res["facts"][0]["action"] == "noop" and res["facts"][0]["value"] == "IPA"
        assert [r["value"] for r in active(c, user, "favorite_beer")] == ["IPA"]


def test_gather_context_and_messages(user):
    with db.conn() as c:
        facts.upsert_fact(c, user_id=user, subject="johnny", predicate="runs_service", value="vllm",
                          source="cli", source_kind="explicit", embed=False)
        facts.upsert_fact(c, user_id=user, subject=user, predicate="favorite_beer", value="Guinness",
                          source="cli", source_kind="explicit", embed=False)
        cands, registry = resolver.gather_context(c, user_id=user, job_text="Today johnny crashed again")
    assert any(cf["subject"] == "johnny" for cf in cands)
    assert all(set(cf) == {"id", "subject", "predicate", "value", "valid_from"} for cf in cands)
    assert len(registry) <= 60 and {"name", "cardinality"} <= set(registry[0])
    msgs = resolver.build_messages("Today johnny crashed again", datetime.now(UTC), user, cands, registry)
    assert msgs[0]["role"] == "system" and "nothing_durable" in msgs[0]["content"]
    assert f"USER_ID: {user}" in msgs[1]["content"] and "johnny" in msgs[1]["content"]


# ---------------------------------------------------------------------------
# worker

def _canned(user):
    return Extraction(summary=f"{user} lives in El Cerrito.", facts=[
        ExtractedFact(subject="I", predicate="location", value="El Cerrito", layer="profile", confidence=0.85,
                      evidence="I live in El Cerrito")])


def test_worker_process_done(user, monkeypatch):
    seen = []

    def fake_extract(job_text, occurred_at, user_id, candidates, registry):
        seen.append({"job_text": job_text, "user_id": user_id, "candidates": candidates, "registry": registry})
        return _canned(user)

    monkeypatch.setattr(resolver, "extract", fake_extract)
    t0 = datetime(2026, 8, 21, 9, 30, tzinfo=UTC)
    with db.conn() as c:
        e1 = add_turn(c, user, "User: hello\nAssistant: hi", occurred_at=t0)
        e2 = add_turn(c, user, "User: I live in El Cerrito\nAssistant: noted", occurred_at=t0 + timedelta(minutes=1))
        e3 = add_turn(c, user, "User: unrelated\nAssistant: ok", session_id="s2", occurred_at=t0)
        q1, q2, q3 = enqueue(c, user, e1), enqueue(c, user, e2), enqueue(c, user, e3, session_id="s2")

    out = worker.drain_once(limit=4)
    assert out["processed"] == 3 and out["failed"] == 0 and out["dead"] == 0
    assert len(seen) == 2 and all(x["user_id"] == user for x in seen)       # one LLM call per session group
    texts = sorted(x["job_text"] for x in seen)
    assert texts[0].startswith("[2026-08-21 09:30] User: hello") and "[2026-08-21 09:31] User: I live" in texts[0]
    assert texts[1] == "[2026-08-21 09:30] User: unrelated\nAssistant: ok"

    with db.conn() as c:
        for q in (q1, q2, q3):
            r = q_row(c, q)
            assert r["state"] == "done" and r["finished_at"] is not None and r["attempts"] == 1
            assert r["payload"]["result"]["facts"] == 1
        loc = active(c, user, "location")
        assert len(loc) == 1 and loc[0]["value"] == "El Cerrito" and loc[0]["layer"] == "profile"
        assert loc[0]["corroborations"] == 0   # same source → not independent
        summaries = c.execute("SELECT session_id FROM episode WHERE user_id=%s AND kind='summary' ORDER BY session_id",
                              (user,)).fetchall()
        assert [s["session_id"] for s in summaries] == ["s1", "s2"]
        assert c.execute("SELECT count(*) AS n FROM episode WHERE user_id=%s AND kind='turn' AND processed_at IS NULL",
                         (user,)).fetchone()["n"] == 0
    # nothing left to drain
    assert worker.drain_once(limit=4) == {"processed": 0, "failed": 0, "dead": 0, "skipped": 0}


def test_worker_failed_backoff_then_dead(user, monkeypatch):
    monkeypatch.setattr(resolver, "extract", lambda *a, **k: None)
    with db.conn() as c:
        e1 = add_turn(c, user, "User: I live in El Cerrito\nAssistant: noted")
        q1 = enqueue(c, user, e1)
    out = worker.drain_once(limit=4)
    assert out == {"processed": 0, "failed": 1, "dead": 0, "skipped": 0}
    with db.conn() as c:
        r = q_row(c, q1)
        assert r["state"] == "failed" and r["attempts"] == 1 and "no valid" in r["last_error"]
        delta = r["next_attempt_at"] - datetime.now(UTC)
        assert timedelta(seconds=30) < delta <= timedelta(minutes=1, seconds=5)
        assert active(c, user, "location") == []          # never partial-write
        assert c.execute("SELECT processed_at FROM episode WHERE id=%s", (e1,)).fetchone()["processed_at"] is None
        # not due yet → not re-claimed
        assert worker.claim_jobs(c, 4) == []
        # LLM outage path + dead at max_attempts
        c.execute("UPDATE cognify_queue SET next_attempt_at=now(), attempts=4 WHERE id=%s", (q1,))

    def boom(*a, **k):
        raise LLMUnavailable("saint down")
    monkeypatch.setattr(resolver, "extract", boom)
    out = worker.drain_once(limit=4)
    assert out["dead"] == 1 and out["failed"] == 0
    with db.conn() as c:
        r = q_row(c, q1)
        assert r["state"] == "dead" and r["attempts"] == 5 and "saint down" in r["last_error"] and r["finished_at"]


def test_claim_order_and_coalesce(user):
    with db.conn() as c:
        eps = [add_turn(c, user, f"User: turn {i}\nAssistant: ok", session_id="s1") for i in range(3)]
        hi = add_turn(c, user, "User: correction\nAssistant: ok", session_id="s2")
        q_hi = enqueue(c, user, hi, session_id="s2", priority=1)
        qs = [enqueue(c, user, e) for e in eps]
    with db.conn() as c:
        jobs = worker.claim_jobs(c, limit=10)
        c.rollback()  # don't leave them running
    assert jobs[0]["id"] == q_hi                      # priority first
    assert all(j["state"] == "running" and j["attempts"] == 1 for j in jobs)
    groups = worker.coalesce(jobs)
    assert [len(g) for g in groups] == [1, 3]
    assert [j["id"] for j in groups[1]] == qs
    # size splitting
    big = [{"id": i, "user_id": user, "session_id": "x", "priority": 5, "occurred_at": datetime.now(UTC),
            "episode": {"body": "x" * 2500}} for i in range(4)]
    assert [len(g) for g in worker.coalesce(big)] == [2, 2]
    many = [{"id": i, "user_id": user, "session_id": "y", "priority": 5, "occurred_at": datetime.now(UTC),
             "episode": {"body": "x"}} for i in range(10)]
    assert [len(g) for g in worker.coalesce(many)] == [8, 2]


# ---------------------------------------------------------------------------
# curator

def test_rederive_profile(user):
    with db.conn() as c:
        assert curator.rederive_profile(c, user)["narrative"] == ""
    with db.conn() as c:   # separate txn: ingested_at (txn now()) must be later than rederived_at
        facts.upsert_fact(c, user_id=user, subject=user, predicate="name", value="Rick", source="cli", embed=False)
        facts.upsert_fact(c, user_id=user, subject=user, predicate="favorite_beer", value="IPA", source="cli", embed=False)
        facts.upsert_fact(c, user_id=user, subject=user, predicate="likes", value="IPA", source="cli", embed=False)
        facts.upsert_fact(c, user_id=user, subject=user, predicate="likes", value="stout", source="cli", embed=False)
        facts.upsert_fact(c, user_id=user, subject=user, predicate="uses_tool", value="neovim", source="cli", embed=False)  # semantic
        assert user in curator.users_with_profile_changes(c)
        r = curator.rederive_profile(c, user)
    assert r["changed"] and r["version"] == 1
    assert r["narrative"] == "Rick's favorite beer is IPA. Rick likes: IPA, stout. Rick's name is Rick."
    with db.conn() as c:
        r2 = curator.rederive_profile(c, user)
        assert not r2["changed"] and r2["version"] == 1
        assert user not in curator.users_with_profile_changes(c)
    with db.conn() as c:   # new txn → later ingested_at
        facts.upsert_fact(c, user_id=user, subject=user, predicate="location", value="El Cerrito", source="cli", embed=False)
        assert user in curator.users_with_profile_changes(c)
        r3 = curator.rederive_profile(c, user)
        assert r3["version"] == 2 and "Rick lives in El Cerrito." in r3["narrative"]
        p = c.execute("SELECT * FROM profile WHERE user_id=%s", (user,)).fetchone()
        assert p["version"] == 2 and p["narrative"] == r3["narrative"] and p["rederived_at"] is not None
        hist = c.execute("SELECT version FROM profile_history WHERE user_id=%s ORDER BY version", (user,)).fetchall()
        assert [h["version"] for h in hist] == [1, 2]


def test_archive_prune_backfill(user, monkeypatch):
    with db.conn() as c:
        old = add_turn(c, user, "User: old\nAssistant: ok", occurred_at=datetime.now(UTC) - timedelta(days=20))
        new = add_turn(c, user, "User: new\nAssistant: ok")
        c.execute("INSERT INTO snapshot(user_id, created_at) VALUES (%s, now() - interval '100 days'), (%s, now())",
                  (user, user))
        assert curator.archive_old_turns(c, days=14) >= 1
        st = {str(r["id"]): r["status"] for r in c.execute("SELECT id, status FROM episode WHERE user_id=%s", (user,))}
        assert st[old] == "archived" and st[new] == "active"
        assert curator.prune_snapshots(c, days=90) >= 1
        assert c.execute("SELECT count(*) AS n FROM snapshot WHERE user_id=%s", (user,)).fetchone()["n"] == 1
        # backfill: fake embedder fills NULL vectors for the two turns + a fact
        facts.upsert_fact(c, user_id=user, subject=user, predicate="likes", value="tea", source="cli", embed=False)
        monkeypatch.setattr(curator, "embed_texts", lambda texts, **kw: [[0.1] * 768 for _ in texts])
        r = curator.embed_backfill(c, limit=500)
        assert r["facts"] >= 1 and r["episodes"] >= 2
        assert c.execute("SELECT count(*) AS n FROM episode WHERE user_id=%s AND embedding IS NULL", (user,)).fetchone()["n"] == 0


# ---------------------------------------------------------------------------
# real LLM (opt-in):  pytest tests/test_cognify.py -m llm

@pytest.mark.llm
def test_real_llm_extract(request, user):
    if "llm" not in (getattr(request.config.option, "markexpr", "") or ""):
        pytest.skip("real LLM call; run with -m llm")
    with db.conn() as c:
        g = facts.upsert_fact(c, user_id=user, subject=user, predicate="favorite_beer", value="Guinness",
                              source="cli", source_kind="explicit")["fact"]
        text = "Actually my favorite beer is IPA, not Guinness. I live in El Cerrito."
        cands, registry = resolver.gather_context(c, user_id=user, job_text=text)
    assert any(cf["id"] == str(g["id"]) for cf in cands)
    parsed = resolver.extract(text, datetime.now(UTC), user, cands, registry)
    print("\nLLM extraction:", parsed.model_dump_json(indent=1) if parsed else None)
    assert parsed is not None and not parsed.nothing_durable
    by_pred = {f.predicate: f for f in parsed.facts if f.action == "assert"}
    assert by_pred["favorite_beer"].value.lower() == "ipa"
    assert str(g["id"]) in by_pred["favorite_beer"].contradicts
    assert "el cerrito" in by_pred["location"].value.lower()
    assert parsed.summary
    with db.conn() as c:
        ep = add_turn(c, user, text)
        res = resolver.apply(c, user_id=user, episode_ids=[ep], parsed=parsed, source="input", session_id="s1")
        assert [r["value"] for r in active(c, user, "favorite_beer")] == ["IPA"]
        print("apply:", res)
