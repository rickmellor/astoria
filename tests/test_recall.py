"""Recall tests against the local dev DB (postgresql://astoria:astoria@127.0.0.1:55432/astoria, migrated)
and the NAS TEI (real embeddings). Uses a throwaway user_id and cleans up after itself."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from astoria.core import embed
from astoria.retrieval import recall as R
from astoria.store import db, facts

NOW = datetime.now(timezone.utc)


def _episode(c, user_id, hook, body, *, kind="summary", occurred_at=None, session_id=None, meta=None):
    from psycopg.types.json import Jsonb
    vec = embed.embed_one(hook)
    return c.execute(
        "INSERT INTO episode(user_id, kind, hook, body, embedding, occurred_at, source, session_id, meta) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (user_id, kind, hook, body, vec, occurred_at or NOW, "test", session_id, Jsonb(meta or {}))).fetchone()["id"]


@pytest.fixture(scope="module")
def uid():
    user_id = f"t_recall_{uuid.uuid4().hex[:8]}"
    with db.conn() as c:
        # Guinness first (a month ago), then IPA now → Guinness superseded
        facts.upsert_fact(c, user_id=user_id, subject="I", predicate="favorite_beer", value="Guinness",
                          source="cli", asserted_at=NOW - timedelta(days=30))
        r = facts.upsert_fact(c, user_id=user_id, subject="I", predicate="favorite_beer", value="IPA", source="cli")
        assert r["action"] == "superseded"
        facts.upsert_fact(c, user_id=user_id, subject="I", predicate="uses_tool", value="Neovim", source="cli")
        facts.upsert_fact(c, user_id=user_id, subject="I", predicate="location", value="El Cerrito", source="cli")
        _episode(c, user_id, "Talked about trying a new IPA at the brewery and how it compares to Guinness",
                 "User: I had a great IPA at the brewery yesterday, way better than the Guinness I used to drink.\n"
                 "Assistant: Nice — sounds like IPA is your favorite now.", occurred_at=NOW - timedelta(days=5))
        _episode(c, user_id, "Set up NFS mounts on the NAS for model storage",
                 "User: mounted /mnt/ug-models over 10GbE.\nAssistant: Great, jumbo frames help.",
                 kind="note", occurred_at=NOW - timedelta(days=2))
        # a working-memory turn in a session
        _episode(c, user_id, "turn: hello", "User: hello there\nAssistant: hi rick", kind="turn",
                 session_id="sess-1", occurred_at=NOW - timedelta(minutes=1),
                 meta={"user_input": "hello there", "agent_response": "hi rick"})
    yield user_id
    with db.conn() as c:
        c.execute("DELETE FROM snapshot WHERE user_id=%s", (user_id,))
        c.execute("DELETE FROM audit WHERE user_id=%s", (user_id,))
        c.execute("DELETE FROM tombstone WHERE user_id=%s", (user_id,))
        c.execute("DELETE FROM fact WHERE user_id=%s", (user_id,))
        c.execute("DELETE FROM episode WHERE user_id=%s", (user_id,))
        c.execute("DELETE FROM profile WHERE user_id=%s", (user_id,))


def _recall(uid, query, **kw):
    with db.conn() as c:
        return R.recall(c, user_id=uid, query=query, **kw)


def test_beer_returns_ipa_first_not_guinness(uid):
    res = _recall(uid, "what beer do I like")
    assert res["health"] == {"tei": "ok", "degraded": False}
    assert res["items"], res
    top = res["items"][0]
    assert top["kind"] == "fact" and top["predicate"] == "favorite_beer" and top["value"] == "IPA"
    assert not any(it["kind"] == "fact" and it["value"] == "Guinness" for it in res["items"])
    assert res["context"].startswith(R.CONTEXT_HEADER)
    assert f"- {uid} favorite beer: IPA  [profile · 0.90]" in res["context"]
    # items are JSON-safe
    import json
    json.dumps(res)


def test_editor_finds_neovim(uid):
    res = _recall(uid, "what editor do I use")  # bare "editor" vs "<uid> uses tool: Neovim" is ~0.42-0.46 cosine
    vals = [it.get("value") for it in res["items"] if it["kind"] == "fact"]
    assert "Neovim" in vals
    assert res["items"][0]["value"] == "Neovim"


def test_unrelated_query_is_empty(uid):
    # NB: nomic cosines between unrelated short texts sit ~0.40-0.47, close to the contract's 0.45 floor;
    # this query measured max 0.442 over 10 random throwaway subjects.
    res = _recall(uid, "medieval French monastery architecture")
    assert res["items"] == []
    assert res["context"] == ""
    assert res["snapshot_id"]


def test_episodes_included_then_facts_only_excludes(uid):
    res = _recall(uid, "IPA at the brewery")
    kinds = {it["kind"] for it in res["items"]}
    assert "episode" in kinds
    assert "from a past session (" in res["context"] and "[episodic]" in res["context"]
    res2 = _recall(uid, "IPA at the brewery", facts_only=True)
    assert all(it["kind"] == "fact" for it in res2["items"])
    res3 = _recall(uid, "IPA at the brewery", layers=("profile", "semantic"))
    assert all(it["kind"] == "fact" for it in res3["items"])


def test_as_of_returns_guinness(uid):
    at = NOW - timedelta(days=10)
    res = _recall(uid, "favorite beer", as_of=at)
    vals = [it["value"] for it in res["items"] if it["predicate"] == "favorite_beer"]
    assert vals == ["Guinness"]
    assert "IPA" not in [it["value"] for it in res["items"]]
    # string timestamps accepted too
    res2 = _recall(uid, "favorite beer", as_of=at.isoformat())
    assert [it["value"] for it in res2["items"] if it["predicate"] == "favorite_beer"] == ["Guinness"]


def test_working_memory_and_profile(uid):
    res = _recall(uid, "beer", session_id="sess-1", include_profile=True)
    assert res["working"] == [{"user_input": "hello there", "agent_response": "hi rick",
                               "occurred_at": res["working"][0]["occurred_at"]}]
    assert res["profile"] is not None
    assert res["profile"]["narrative"] == ""
    preds = {f["predicate"] for f in res["profile"]["facts"]}
    assert {"favorite_beer", "location"} <= preds
    # this session's turns are never recalled as episodes
    assert not any(it.get("episode_kind") == "turn" for it in res["items"])


def test_snapshot_and_touch(uid):
    res = _recall(uid, "favorite beer", client="pytest", session_id="sess-1")
    with db.conn() as c:
        snap = c.execute("SELECT * FROM snapshot WHERE id=%s", (res["snapshot_id"],)).fetchone()
        assert snap["user_id"] == uid and snap["client"] == "pytest" and snap["query"] == "favorite beer"
        assert snap["session_id"] == "sess-1"
        fid = [it["id"] for it in res["items"] if it["kind"] == "fact"][0]
        assert uuid.UUID(fid) in snap["fact_ids"]
        row = c.execute("SELECT access_count FROM fact WHERE id=%s", (fid,)).fetchone()
        assert row["access_count"] >= 1


def test_degraded_bm25_only(uid, monkeypatch):
    monkeypatch.setattr(R, "embed_one", lambda *a, **k: None)
    res = _recall(uid, "favorite beer")
    assert res["health"] == {"tei": "down", "degraded": True}
    assert res["items"] and res["items"][0]["value"] == "IPA"


def test_stale_hint_flips_on_newer_episode(uid):
    res = _recall(uid, "what beer do I like")
    ipa = [it for it in res["items"] if it.get("value") == "IPA"][0]
    assert ipa["stale_hint"] is False
    with db.conn() as c:
        _episode(c, uid, "my favorite beer is stout these days",
                 "User: honestly my favorite beer is stout these days.\nAssistant: Noted.",
                 kind="note", occurred_at=NOW + timedelta(minutes=10))
    res = _recall(uid, "what beer do I like")
    ipa = [it for it in res["items"] if it.get("value") == "IPA"][0]
    assert ipa["stale_hint"] is True
    assert f"- {uid} favorite beer: IPA  [profile · 0.90 · stale?]" in res["context"]


def test_briefing_and_search_facts_simple(uid):
    with db.conn() as c:
        b = R.briefing(c, user_id=uid)
        assert b["context"].startswith(f"Known about {uid} (authoritative, as of ")
        assert f"- {uid} favorite beer: IPA  [profile · 0.90" in b["context"]
        assert f"- {uid} uses tool: Neovim  [semantic · 0.90]" in b["context"]
        assert b["narrative"] == ""
        assert {f["predicate"] for f in b["facts"]} >= {"favorite_beer", "location", "uses_tool"}
        hits = R.search_facts_simple(c, user_id=uid, query="favorite beer")
        assert hits and hits[0]["value"] == "IPA" and "score" in hits[0]
        assert "embedding" not in hits[0]
