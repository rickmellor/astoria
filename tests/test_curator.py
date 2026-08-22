"""Curator v2 tests — local dev DB (postgresql://astoria:astoria@127.0.0.1:55432/astoria), no LLM/TEI:
`chat_json` and `embed_texts` are monkeypatched on the curator module.

    ASTORIA_WORKER_ENABLED=false pytest tests/test_curator.py -q
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

os.environ.setdefault("ASTORIA_DB_DSN", "postgresql://astoria:astoria@127.0.0.1:55432/astoria")

from astoria.config import settings
from astoria.core.llm import LLMUnavailable
from astoria.curator import maintenance as curator
from astoria.store import db, episodes, facts


@pytest.fixture(scope="session", autouse=True)
def _migrated():
    db.migrate()
    yield
    db.close_pool()


@pytest.fixture
def user():
    uid = f"t_cur_{uuid.uuid4().hex[:8]}"
    yield uid
    with db.conn() as c:
        for t in ("cognify_queue", "fact", "episode", "tombstone", "audit", "snapshot", "profile_history", "profile"):
            c.execute(f"DELETE FROM {t} WHERE user_id=%s", (uid,))


def seed(c, user, predicate, value, *, subject=None, source="input", source_kind="extracted", confidence=None,
         is_belief=False, importance=0.5, layer=None, cardinality=None):
    return facts.upsert_fact(c, user_id=user, subject=subject or user, predicate=predicate, value=value,
                             source=source, source_kind=source_kind, confidence=confidence, is_belief=is_belief,
                             importance=importance, layer=layer, cardinality=cardinality, embed=False)["fact"]


def vec(seed_val: float, dim: int = 768) -> list[float]:
    v = [seed_val] * dim
    v[0] = 1.0
    return v


def status_of(c, fid) -> str:
    return c.execute("SELECT status FROM fact WHERE id=%s", (fid,)).fetchone()["status"]


# ---------------------------------------------------------------------------
# dedup

def test_dedup_merges_containment_and_cosine_pairs(user):
    with db.conn() as c:
        a = seed(c, user, "uses_tool", "neovim")                       # ⊂ "neovim editor" → containment
        b = seed(c, user, "uses_tool", "neovim editor")
        x = seed(c, user, "likes", "hoppy IPAs", source_kind="extracted")   # cosine pair (fake vectors)
        y = seed(c, user, "likes", "hoppy india pale ales", source_kind="extracted")
        z = seed(c, user, "likes", "stout")                             # unrelated: stays
        # cosine: x ≈ y (identical vectors), z orthogonal-ish
        c.execute("UPDATE fact SET embedding=%s WHERE id=%s", (vec(0.01), x["id"]))
        c.execute("UPDATE fact SET embedding=%s WHERE id=%s", (vec(0.01), y["id"]))
        zv = [0.0] * 768; zv[5] = 1.0
        c.execute("UPDATE fact SET embedding=%s WHERE id=%s", (zv, z["id"]))
        # functional keys are never dedup candidates
        seed(c, user, "favorite_beer", "IPA", source_kind="explicit")

        dry = curator.dedup_facts(c, user, dry_run=True)
        assert dry["dry_run"] and dry["candidates"] == 2 and dry["merged"] == 0
        assert all(status_of(c, f["id"]) == "active" for f in (a, b, x, y, z))

        r = curator.dedup_facts(c, user)
        assert r["merged"] == 2, r
        whys = {p["why"] for p in r["pairs"]}
        assert whys == {"containment", "cosine"}
        # containment: richer value kept
        assert status_of(c, b["id"]) == "active" and status_of(c, a["id"]) == "retracted"
        # cosine: richer (longer) value kept
        assert status_of(c, y["id"]) == "active" and status_of(c, x["id"]) == "retracted"
        assert status_of(c, z["id"]) == "active"
        keep = facts.get_fact(c, user_id=user, fact_id=str(b["id"]))
        assert keep["meta"]["merged_from"][0]["id"] == str(a["id"])
        # curator retract → tombstone blocks='none' (not a human tombstone)
        ts = c.execute("SELECT blocks, reason FROM tombstone WHERE user_id=%s AND predicate='uses_tool'", (user,)).fetchall()
        assert ts and all(t["blocks"] == "none" and t["reason"] == "curator-dedup" for t in ts)
        # non-explicit re-assert of the dropped value is therefore NOT blocked
        again = facts.upsert_fact(c, user_id=user, subject=user, predicate="uses_tool", value="neovim",
                                  source="input", source_kind="extracted", embed=False)
        assert again["action"] == "inserted"
        assert c.execute("SELECT count(*) AS n FROM audit WHERE user_id=%s AND op='curator-dedup'", (user,)).fetchone()["n"] == 2
        # idempotent: the re-asserted "neovim" is a fresh containment pair; dedup it, then nothing is left
        r2 = curator.dedup_facts(c, user)
        assert r2["merged"] == 1
        r3 = curator.dedup_facts(c, user)
        assert r3["candidates"] == 0 and r3["merged"] == 0
        assert [f["value"] for f in facts.list_facts(c, user_id=user, predicate="uses_tool")] == ["neovim editor"]


def test_dedup_keeps_human_stated_over_machine(user):
    with db.conn() as c:
        human = seed(c, user, "uses_tool", "vim", source="cli", source_kind="explicit")
        machine = seed(c, user, "uses_tool", "vim editor from the terminal", source_kind="extracted")
        r = curator.dedup_facts(c, user)
        assert r["merged"] == 1
        assert status_of(c, human["id"]) == "active" and status_of(c, machine["id"]) == "retracted"
        # short/noise containment is ignored ("go" in "google")
        seed(c, user, "likes", "go")
        seed(c, user, "likes", "google")
        assert curator.dedup_facts(c, user)["candidates"] == 0


# ---------------------------------------------------------------------------
# decay

def backdate(c, fid, days, *, access=0):
    c.execute("UPDATE fact SET ingested_at=now() - make_interval(days => %s), last_seen=now() - make_interval(days => %s), "
              "asserted_at=now() - make_interval(days => %s), access_count=%s WHERE id=%s", (days, days, days, access, fid))


def test_decay_archives_only_eligible(user):
    with db.conn() as c:
        old_extracted = seed(c, user, "uses_tool", "a very old tool")              # semantic, extracted → archive
        mid_extracted = seed(c, user, "uses_tool", "a mid-age tool")               # 120 d, non-belief → kept (score > thr)
        old_belief = seed(c, user, "uses_tool", "a mid-age belief", is_belief=True)   # 120 d belief → archive
        old_explicit = seed(c, user, "uses_tool", "a human-stated tool", source="cli", source_kind="explicit")
        old_profile = seed(c, user, "likes", "old profile like")                   # profile layer → never
        old_accessed = seed(c, user, "uses_tool", "a recalled tool")               # access_count 1 → never
        fresh = seed(c, user, "uses_tool", "a fresh tool")                         # 10 d → not eligible
        old_proc = seed(c, user, "uses_tool", "an old runbook", layer="procedural")
        important = seed(c, user, "uses_tool", "an important old tool", importance=0.95)
        backdate(c, old_extracted["id"], 200)
        backdate(c, mid_extracted["id"], 120)
        backdate(c, old_belief["id"], 120)
        backdate(c, old_explicit["id"], 200)
        backdate(c, old_profile["id"], 200)
        backdate(c, old_accessed["id"], 200, access=1)
        backdate(c, fresh["id"], 10)
        backdate(c, old_proc["id"], 200)
        backdate(c, important["id"], 200)

        dry = curator.decay(c, user, dry_run=True)
        assert dry["dry_run"] and dry["archived"] == 2 and set(dry["archived_ids"]) == {str(old_extracted["id"]), str(old_belief["id"])}
        assert all(status_of(c, f["id"]) == "active" for f in (old_extracted, old_belief))

        r = curator.decay(c, user)
        assert r["archived"] == 2 and r["candidates"] == 4, r    # old_extracted, mid, belief, important
        assert status_of(c, old_extracted["id"]) == "archived"
        assert status_of(c, old_belief["id"]) == "archived"
        for f in (mid_extracted, old_explicit, old_profile, old_accessed, fresh, old_proc, important):
            assert status_of(c, f["id"]) == "active", f["value"]
        row = facts.get_fact(c, user_id=user, fact_id=str(old_extracted["id"]))
        assert row["meta"]["archived_reason"] == "decay" and row["expired_at"] is not None
        assert c.execute("SELECT count(*) AS n FROM audit WHERE user_id=%s AND op='curator-decay'", (user,)).fetchone()["n"] == 2
        # no tombstone: decay is soft and revivable
        assert c.execute("SELECT count(*) AS n FROM tombstone WHERE user_id=%s", (user,)).fetchone()["n"] == 0
        # idempotent
        assert curator.decay(c, user)["archived"] == 0


def test_decay_score_shape():
    now = datetime.now(UTC)
    base = {"importance": 0.5, "access_count": 0, "source_trust": 0.6, "is_belief": False, "last_seen": now}
    assert abs(curator.decay_score(base, now=now) - 0.3) < 1e-9
    half = dict(base, last_seen=now - timedelta(days=settings().decay_half_life_days))
    assert abs(curator.decay_score(half, now=now) - 0.15) < 1e-6
    belief = dict(base, is_belief=True, last_seen=now - timedelta(days=settings().decay_belief_half_life_days))
    assert abs(curator.decay_score(belief, now=now) - 0.15) < 1e-6
    assert curator.decay_score(dict(base, access_count=5), now=now) > curator.decay_score(base, now=now)


# ---------------------------------------------------------------------------
# reflect

def test_reflect_with_fake_llm(user, monkeypatch):
    calls = []

    def fake_chat_json(messages, **kw):
        calls.append(messages)
        return {"insights": [
            {"subject": "me", "predicate": "working_style", "value": "iterates in small verified steps",
             "confidence": 0.9, "layer": "procedural", "evidence": "verified each step"},
            {"subject": user, "predicate": "interested_in", "value": "local inference on AMD GPUs",
             "confidence": 0.2, "evidence": "RDNA4"},
            {"subject": user, "predicate": "tends_to", "value": "", "confidence": 0.5},       # dropped: empty value
            *[{"subject": user, "predicate": f"p{i}", "value": f"v{i}", "confidence": 0.5} for i in range(5)],  # cap at 5
        ]}

    monkeypatch.setattr(curator, "chat_json", fake_chat_json)
    with db.conn() as c:
        seed(c, user, "uses_tool", "neovim", source="cli", source_kind="explicit")
        e1 = episodes.add_episode(c, user_id=user, kind="summary", text="Rick verified each step of the RDNA4 vLLM bring-up.",
                                  source="input", embed=False)["episode"]
        e2 = episodes.add_episode(c, user_id=user, kind="note", text="Rick prefers to iterate in small verified steps.",
                                  source="cli", embed=False)["episode"]
        old = episodes.add_episode(c, user_id=user, kind="summary", text="An old summary outside the window.",
                                   occurred_at=datetime.now(UTC) - timedelta(days=10), embed=False)["episode"]
        turn = episodes.add_episode(c, user_id=user, kind="turn", user_input="a raw turn is not reflected over",
                                    agent_response="ok", session_id="s1", embed=False)["episode"]
        dry = curator.reflect(c, user, dry_run=True)
        assert dry["episodes"] == 2 and dry["insights"] == 5 and dry["dry_run"]
        assert facts.list_facts(c, user_id=user, status="any", predicate="working_style") == []

        r = curator.reflect(c, user)
        assert r["episodes"] == 2 and r["insights"] == 5 and len(r["written"]) == 5
        assert len(calls) == 2
        sys_prompt = calls[-1][0]["content"]
        assert "curator" in sys_prompt.lower() and "neovim" in calls[-1][1]["content"]   # known facts passed
        ws = facts.list_facts(c, user_id=user, predicate="working_style")[0]
        assert ws["source_kind"] == "curator" and ws["source"] == "curator" and ws["is_belief"] is True
        assert abs(ws["confidence"] - 0.6) < 1e-6 and ws["layer"] == "procedural" and ws["embedding"] is None
        assert str(ws["origin_episode"]) in (str(e1["id"]), str(e2["id"]))
        assert ws["meta"]["reflect"] is True
        # low confidence → staging by the existing gate
        ii = facts.list_facts(c, user_id=user, status="staging", predicate="interested_in")
        assert len(ii) == 1 and abs(ii[0]["confidence"] - 0.2) < 1e-6
        # the 5-cap: p0..p2 written (3 + 2 = 5), p3/p4 not
        assert facts.list_facts(c, user_id=user, predicate="p2") and not facts.list_facts(c, user_id=user, predicate="p3")
        # episodes marked reflected (only the two in-window summary/note rows)
        flags = {str(r_["id"]): (r_["meta"] or {}).get("reflected") for r_ in
                 c.execute("SELECT id, meta FROM episode WHERE user_id=%s", (user,))}
        assert flags[str(e1["id"])] is True and flags[str(e2["id"])] is True
        assert flags.get(str(old["id"])) is None and flags.get(str(turn["id"])) is None
        # idempotent: nothing left to reflect, LLM not called again
        r2 = curator.reflect(c, user)
        assert r2["episodes"] == 0 and len(calls) == 2


def test_reflect_llm_unavailable_writes_nothing(user, monkeypatch):
    def boom(messages, **kw):
        raise LLMUnavailable("saint down")
    monkeypatch.setattr(curator, "chat_json", boom)
    with db.conn() as c:
        e = episodes.add_episode(c, user_id=user, kind="note", text="A fresh note that should stay unreflected.",
                                 embed=False)["episode"]
        r = curator.reflect(c, user)
        assert r["episodes"] == 1 and r["insights"] == 0 and "llm unavailable" in r["error"]
        assert (c.execute("SELECT meta FROM episode WHERE id=%s", (e["id"],)).fetchone()["meta"] or {}).get("reflected") is None
        assert facts.list_facts(c, user_id=user, status="any") == []
        # garbage JSON → same
        monkeypatch.setattr(curator, "chat_json", lambda m, **kw: {"nope": 1})
        r = curator.reflect(c, user)
        assert r["error"] == "no valid JSON" and r["episodes"] == 1
        assert user in curator.users_with_unreflected(c)


# ---------------------------------------------------------------------------
# profile narrative — LLM path + template fallback

def test_mention_ratio():
    assert curator._mention_ratio("Rick lives in El Cerrito and likes IPA.", ["Rick", "El Cerrito", "IPA"]) == 1.0
    assert curator._mention_ratio("Rick likes beer.", ["Rick", "El Cerrito", "IPA"]) < 0.8
    assert curator._mention_ratio("", []) == 1.0


def test_profile_llm_path_and_fallback(user, monkeypatch):
    good = "Rick lives in El Cerrito and his favorite beer is IPA. Rick likes IPA and stout."
    monkeypatch.setattr(curator, "chat_json", lambda m, **kw: {"narrative": good})
    with db.conn() as c:
        seed(c, user, "name", "Rick", source="cli", source_kind="explicit")
        seed(c, user, "favorite_beer", "IPA", source="cli", source_kind="explicit")
        seed(c, user, "likes", "IPA", source="cli", source_kind="explicit")
        seed(c, user, "likes", "stout", source="cli", source_kind="explicit")
        seed(c, user, "location", "El Cerrito", source="cli", source_kind="explicit")
        dry = curator.rederive_profile(c, user, llm=True, dry_run=True)
        assert dry["source"] == "llm" and dry["changed"] and dry["narrative"] == good
        assert c.execute("SELECT count(*) AS n FROM profile WHERE user_id=%s", (user,)).fetchone()["n"] == 0
        r = curator.rederive_profile(c, user, llm=True)
        assert r["source"] == "llm" and r["version"] == 1 and r["narrative"] == good
        p = c.execute("SELECT * FROM profile WHERE user_id=%s", (user,)).fetchone()
        assert p["source"] == "llm" and p["narrative"] == good and p["version"] == 1
        assert [h["version"] for h in c.execute("SELECT version FROM profile_history WHERE user_id=%s ORDER BY version", (user,))] == [1]
        # template path untouched when llm=False
        r_t = curator.rederive_profile(c, user, llm=False)
        assert r_t["source"] == "template" and r_t["version"] == 2 and r_t["narrative"] == curator.render_profile_narrative(
            user, c.execute("SELECT predicate, cardinality, value FROM fact WHERE user_id=%s AND layer='profile' AND status='active' "
                            "ORDER BY predicate, asserted_at, ingested_at", (user,)).fetchall())
    with db.conn() as c:
        # sanity-check failure (mentions < 80 % of values) → template fallback, source='template'
        monkeypatch.setattr(curator, "chat_json", lambda m, **kw: {"narrative": "A thoroughly pleasant person who enjoys beverages."})
        r = curator.rederive_profile(c, user, llm=True)
        assert r["source"] == "template" and "El Cerrito" in r["narrative"]
        assert c.execute("SELECT source FROM profile WHERE user_id=%s", (user,)).fetchone()["source"] == "template"
        # LLM unavailable → template; unchanged text → no version bump
        v = r["version"]

        def boom(m, **kw):
            raise LLMUnavailable("down")
        monkeypatch.setattr(curator, "chat_json", boom)
        r = curator.rederive_profile(c, user, llm=True)
        assert r["source"] == "template" and not r["changed"] and r["version"] == v
        # non-JSON / missing key → template
        monkeypatch.setattr(curator, "chat_json", lambda m, **kw: None)
        assert curator.rederive_profile(c, user, llm=True)["source"] == "template"


# ---------------------------------------------------------------------------
# working-memory window

def test_archive_old_turns_window(user):
    now = datetime.now(UTC)
    with db.conn() as c:
        ids = []
        for i in range(25):   # 25 recent turns in one session, oldest first
            ids.append(str(episodes.add_episode(c, user_id=user, kind="turn", user_input=f"turn {i} of the window test",
                                                agent_response="ok", session_id="win", embed=False,
                                                occurred_at=now - timedelta(minutes=25 - i))["episode"]["id"]))
        stale = str(episodes.add_episode(c, user_id=user, kind="turn", user_input="a stale turn from yesterday",
                                         agent_response="ok", session_id="other", embed=False,
                                         occurred_at=now - timedelta(hours=13))["episode"]["id"])
        n = curator.archive_old_turns(c, hours=12, per_session=20)
        assert n >= 6
        st = {str(r["id"]): r["status"] for r in c.execute("SELECT id, status FROM episode WHERE user_id=%s", (user,))}
        assert st[stale] == "archived"
        assert [st[i] for i in ids[:5]] == ["archived"] * 5 and all(st[i] == "active" for i in ids[5:])
        assert len(episodes.recent_turns(c, user_id=user, session_id="win", n=50)) == 20
        # defaults come from Settings
        assert settings().working_window_turns == 20 and settings().working_window_hours == 72
        assert curator.archive_old_turns(c) == 0
