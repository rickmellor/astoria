"""Capture path tests against the local dev DB (ASTORIA_DB_DSN or the config default).

Each test uses a throwaway user_id and wipes its rows on exit. Embeddings are stubbed to None by
default (the code must tolerate no vector); one test hits the real TEI if it is reachable.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from astoria.core import capture as cap
from astoria.store import db, episodes, facts


def _wipe(user_id: str) -> None:
    with db.conn() as c:
        c.execute("DELETE FROM fact WHERE user_id=%s", (user_id,))
        c.execute("DELETE FROM episode WHERE user_id=%s", (user_id,))  # cognify_queue cascades
        c.execute("DELETE FROM tombstone WHERE user_id=%s", (user_id,))
        c.execute("DELETE FROM audit WHERE user_id=%s", (user_id,))
        c.execute("DELETE FROM snapshot WHERE user_id=%s", (user_id,))


@pytest.fixture
def uid():
    u = f"t_{uuid.uuid4().hex[:10]}"
    yield u
    _wipe(u)


@pytest.fixture(autouse=True)
def no_embed(monkeypatch):
    """Default: pretend TEI is down. Tests that want real vectors undo this explicitly."""
    monkeypatch.setattr(episodes, "embed_one", lambda *a, **k: None)
    monkeypatch.setattr(facts, "embed_one", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# gate / detector (pure)

@pytest.mark.parametrize("text,reason", [
    ("/help me", "slash_command"),
    ("ok", "ack"), ("Thanks!", "ack"), ("thank you", "ack"), ("y", "ack"), ("hi", "too_short"),
    ("", "empty"), ("   ", "empty"), ("short", "too_short"),
    ("continue", "ack"),
])
def test_gate_drops(text, reason):
    assert cap.gate(text) == reason


def test_gate_keeps_real_text():
    assert cap.gate("I live in Portland, Oregon these days.") is None


def test_detect_patterns(uid):
    d = cap.detect("Actually, my favorite beer is IPA.", uid)
    assert d == {"op": "correct", "subject": uid, "predicate": "favorite_beer", "value": "IPA", "cardinality": "functional"}
    d = cap.detect("my favourite programming language is now Rust!", uid)
    assert d["predicate"] == "favorite_programming_language" and d["value"] == "Rust" and d["op"] == "remember"
    d = cap.detect("/remember me preferred_shell fish", uid)
    assert d == {"op": "remember", "subject": uid, "predicate": "preferred_shell", "value": "fish"}
    d = cap.detect("/correct rick Favorite-Editor 'Neovim'", uid)
    assert d["op"] == "correct" and d["predicate"] == "favorite_editor" and d["value"] == "Neovim"
    d = cap.detect("/forget user likes", uid)
    assert d == {"op": "retract", "subject": uid, "predicate": "likes", "value": None}
    d = cap.detect("I now live in Portland.", uid)
    assert d["predicate"] == "location" and d["value"] == "Portland"
    d = cap.detect("My name is Rick Mellor", uid)
    assert d["predicate"] == "name" and d["value"] == "Rick Mellor"
    d = cap.detect("I don't like cilantro anymore.", uid)
    assert d["op"] == "retract" and d["predicate"] == "likes" and d["value"] == "cilantro anymore"
    d = cap.detect("I no longer use Emacs", uid)
    assert d["op"] == "retract" and d["predicate"] == "uses_tool" and d["also_try"] == ["likes"]
    d = cap.detect("I really love stouts.", uid)
    assert d == {"op": "remember", "subject": uid, "predicate": "likes", "value": "stouts", "cardinality": "set"}
    assert cap.detect("how do I configure nginx?", uid) is None
    assert cap.detect("", uid) is None


def test_correction_hint():
    assert cap.is_correction_hint("Actually, I meant the other one")
    assert cap.is_correction_hint("I don't use vim anymore")
    assert cap.is_correction_hint("we no longer ship that")
    assert not cap.is_correction_hint("what is the weather like")


# ---------------------------------------------------------------------------
# capture (DB)

def test_capture_gate_drops_without_episode(uid):
    with db.conn() as c:
        r = cap.capture(c, user_id=uid, kind="turn", user_input="ok", agent_response="great", session_id="s1")
        assert r == {"episode_id": None, "deduped": False, "dropped": "ack", "detector": None, "queued": False}
        r = cap.capture(c, user_id=uid, kind="note", text="/jobs", session_id="s1")
        assert r["dropped"] == "slash_command"
        assert episodes.list_episodes(c, user_id=uid, status="any") == []


def test_capture_dedupe_on_replay(uid):
    with db.conn() as c:
        r1 = cap.capture(c, user_id=uid, kind="turn", user_input="Tell me about pgvector HNSW tuning",
                         agent_response="Set ef_search...", session_id="s1", source="input")
        assert r1["dropped"] is None and r1["deduped"] is False and r1["queued"] is True
        r2 = cap.capture(c, user_id=uid, kind="turn", user_input="Tell me about pgvector HNSW tuning",
                         agent_response="Set ef_search...", session_id="s1", source="input")
        assert r2["episode_id"] == r1["episode_id"]
        assert r2["deduped"] is True and r2["queued"] is False
        eps = episodes.list_episodes(c, user_id=uid)
        assert len(eps) == 1 and eps[0]["access_count"] == 1 and eps[0]["last_seen"] is not None
        # same text in another session is a different episode
        r3 = cap.capture(c, user_id=uid, kind="turn", user_input="Tell me about pgvector HNSW tuning",
                         agent_response="Set ef_search...", session_id="s2", source="input")
        assert r3["episode_id"] != r1["episode_id"] and r3["deduped"] is False
        # enqueue rows: exactly one per non-deduped episode
        q = c.execute("SELECT episode_id, priority, state FROM cognify_queue WHERE user_id=%s", (uid,)).fetchall()
        assert {str(x["episode_id"]) for x in q} == {r1["episode_id"], r3["episode_id"]}
        assert all(x["state"] == "pending" and x["priority"] == 5 for x in q)


def test_detector_favorite_then_supersede(uid):
    with db.conn() as c:
        r = cap.capture(c, user_id=uid, kind="turn", user_input="Actually, my favorite beer is IPA",
                        agent_response="Noted.", session_id="s1", source="input", actor="input")
        assert r["dropped"] is None
        det = r["detector"]
        assert det["op"] == "correct" and det["predicate"] == "favorite_beer" and det["value"] == "IPA"
        assert det["action"] == "inserted" and det["fact_id"]
        active = facts.list_facts(c, user_id=uid, predicate="favorite_beer")
        assert [f["value"] for f in active] == ["IPA"]
        assert active[0]["source_kind"] == "detector" and abs(active[0]["confidence"] - 0.8) < 1e-6
        assert str(active[0]["origin_episode"]) == r["episode_id"]
        assert active[0]["layer"] == "profile"
        # correction hint → priority 1
        q = c.execute("SELECT priority FROM cognify_queue WHERE episode_id=%s", (r["episode_id"],)).fetchone()
        assert q["priority"] == 1

        r2 = cap.capture(c, user_id=uid, kind="turn", user_input="my favorite beer is Guinness",
                         agent_response="Updated.", session_id="s1", source="input")
        assert r2["detector"]["action"] == "superseded" and det["fact_id"] in r2["detector"]["superseded"]
        active = facts.list_facts(c, user_id=uid, predicate="favorite_beer")
        assert [f["value"] for f in active] == ["Guinness"]
        hist = facts.history(c, user_id=uid, subject=uid, predicate="favorite_beer")
        assert [(h["value"], h["status"]) for h in hist] == [("Guinness", "active"), ("IPA", "superseded")]
        q = c.execute("SELECT priority FROM cognify_queue WHERE episode_id=%s", (r2["episode_id"],)).fetchone()
        assert q["priority"] == 5


def test_detector_retract_pattern(uid):
    with db.conn() as c:
        r = cap.capture(c, user_id=uid, kind="note", text="I really like cilantro", source="cli")
        assert r["detector"]["predicate"] == "likes" and r["detector"]["action"] == "inserted"
        fid = r["detector"]["fact_id"]
        assert [f["value"] for f in facts.list_facts(c, user_id=uid, predicate="likes")] == ["cilantro"]

        r2 = cap.capture(c, user_id=uid, kind="note", text="I don't like cilantro", source="cli", priority="high")
        det = r2["detector"]
        assert det["op"] == "retract" and det["action"] == "retracted" and det["fact_id"] == fid
        assert facts.list_facts(c, user_id=uid, predicate="likes") == []
        assert facts.get_fact(c, user_id=uid, fact_id=fid)["status"] == "retracted"
        # tombstone blocks non-explicit resurrection
        ts = c.execute("SELECT * FROM tombstone WHERE user_id=%s AND predicate='likes'", (uid,)).fetchone()
        assert ts and ts["blocks"] == "non-explicit"
        q = c.execute("SELECT priority FROM cognify_queue WHERE episode_id=%s", (r2["episode_id"],)).fetchone()
        assert q["priority"] == 1

        # also_try: "I no longer use X" falls through uses_tool → likes
        cap.capture(c, user_id=uid, kind="note", text="/remember me likes Emacs", source="cli")
        r3 = cap.capture(c, user_id=uid, kind="note", text="I no longer use Emacs", source="cli")
        assert r3["detector"]["action"] == "retracted" and r3["detector"]["predicate"] == "likes"
        # retract with nothing to retract is a noop, not an error
        r4 = cap.capture(c, user_id=uid, kind="note", text="/forget me favorite_beer", source="cli")
        assert r4["detector"]["action"] == "noop" and r4["dropped"] is None


def test_slash_command_detector_bypasses_gate_and_stores_episode(uid):
    with db.conn() as c:
        r = cap.capture(c, user_id=uid, kind="note", text="/remember me preferred_shell fish", source="cli")
        assert r["dropped"] is None and r["episode_id"]
        assert r["detector"]["action"] == "inserted"
        f = facts.list_facts(c, user_id=uid, predicate="preferred_shell")[0]
        assert f["value"] == "fish" and f["cardinality"] == "functional"
        assert r["queued"] is True


def test_cognify_disabled_not_queued(uid):
    with db.conn() as c:
        r = cap.capture(c, user_id=uid, kind="summary", text="Session about pgvector tuning and HNSW.",
                        session_id="s9", cognify=False)
        assert r["queued"] is False and r["dropped"] is None
        assert c.execute("SELECT count(*) AS n FROM cognify_queue WHERE user_id=%s", (uid,)).fetchone()["n"] == 0


def test_enqueue_row_fields(uid):
    with db.conn() as c:
        ep = episodes.add_episode(c, user_id=uid, kind="note", text="a durable note about nothing much", source="cli")
        row = episodes.enqueue_cognify(c, user_id=uid, episode_id=str(ep["episode"]["id"]), session_id="sx",
                                       priority=1, payload={"x": 1})
        assert row["state"] == "pending" and row["priority"] == 1 and row["kind"] == "extract"
        assert row["payload"] == {"x": 1} and row["session_id"] == "sx" and row["attempts"] == 0
        # delete_episode cascades queue rows
        assert episodes.delete_episode(c, user_id=uid, episode_id=str(ep["episode"]["id"]))
        assert c.execute("SELECT count(*) AS n FROM cognify_queue WHERE user_id=%s", (uid,)).fetchone()["n"] == 0


def test_recent_turns_ordering(uid):
    t0 = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    with db.conn() as c:
        for i in range(6):
            cap.capture(c, user_id=uid, kind="turn", user_input=f"question number {i} about things",
                        agent_response=f"answer {i}", session_id="s1", occurred_at=t0 + timedelta(minutes=i),
                        cognify=False)
        # other session + a note must not leak in
        cap.capture(c, user_id=uid, kind="turn", user_input="unrelated session question", agent_response="x",
                    session_id="s2", cognify=False)
        cap.capture(c, user_id=uid, kind="note", text="a note in session one, not a turn", session_id="s1", cognify=False)
        turns = episodes.recent_turns(c, user_id=uid, session_id="s1", n=4)
        assert [t["user_input"] for t in turns] == [f"question number {i} about things" for i in (2, 3, 4, 5)]
        assert [t["agent_response"] for t in turns] == ["answer 2", "answer 3", "answer 4", "answer 5"]
        assert all(t["kind"] == "turn" for t in turns)
        assert episodes.recent_turns(c, user_id=uid, session_id=None) == []
        # archived turns drop out of working memory
        episodes.archive_episode(c, user_id=uid, episode_id=str(turns[-1]["id"]))
        turns2 = episodes.recent_turns(c, user_id=uid, session_id="s1", n=4)
        assert [t["user_input"] for t in turns2][-1] == "question number 4 about things"
        # body parse fallback when meta lacks user_input/agent_response
        c.execute("UPDATE episode SET meta='{}'::jsonb WHERE user_id=%s", (uid,))
        turns3 = episodes.recent_turns(c, user_id=uid, session_id="s1", n=1)
        assert turns3[0]["user_input"] == "question number 4 about things" and turns3[0]["agent_response"] == "answer 4"


def test_episode_helpers(uid):
    with db.conn() as c:
        ep = episodes.add_episode(c, user_id=uid, kind="turn", user_input="  hello   there  general kenobi ",
                                  agent_response="hi", session_id="s1", tags=["a"], meta={"k": 1})
        row = ep["episode"]
        assert row["hook"] == "hello there general kenobi"
        assert row["body"] == "User: hello   there  general kenobi\nAssistant: hi"
        assert row["meta"] == {"k": 1, "user_input": "  hello   there  general kenobi ", "agent_response": "hi"}
        pub = episodes.row_public(row)
        assert "embedding" not in pub and "tsv" not in pub and isinstance(pub["id"], str)
        assert isinstance(pub["occurred_at"], str)
        got = episodes.get_episode(c, user_id=uid, episode_id=str(row["id"]))
        assert got["id"] == row["id"]
        assert episodes.touch(c, [row["id"]]) == 1
        assert episodes.get_episode(c, user_id=uid, episode_id=str(row["id"]))["access_count"] == 1
        assert episodes.list_episodes(c, user_id=uid, kind="turn", session_id="s1")[0]["id"] == row["id"]
        assert episodes.list_episodes(c, user_id=uid, kind="note") == []
        with pytest.raises(ValueError):
            episodes.add_episode(c, user_id=uid, kind="bogus", text="x")
        with pytest.raises(ValueError):
            episodes.add_episode(c, user_id=uid, kind="note")


def test_real_embedding_if_tei_reachable(uid, monkeypatch):
    from astoria.core import embed as real_embed
    monkeypatch.setattr(episodes, "embed_one", real_embed.embed_one)
    if not real_embed.embed_health().get("ok"):
        pytest.skip("TEI not reachable")
    with db.conn() as c:
        ep = episodes.add_episode(c, user_id=uid, kind="note", text="Rick prefers IPAs over lagers.")
        vec = ep["episode"]["embedding"]
        assert vec is not None
        vec = vec.to_list() if hasattr(vec, "to_list") else list(vec)
        assert len(vec) == 768


# ---------------------------------------------------------------------------
# async write-path embedding (settings.embed_sync=False default) + backfill

def test_capture_async_embedding_null_then_backfill(uid, monkeypatch):
    from astoria.config import settings
    from astoria.curator import maintenance as curator

    def must_not_embed(*a, **k):
        raise AssertionError("embed_one must not be called on the async write path")
    monkeypatch.setattr(episodes, "embed_one", must_not_embed)
    monkeypatch.setattr(facts, "embed_one", must_not_embed)
    assert settings().embed_sync is False
    with db.conn() as c:
        r = cap.capture(c, user_id=uid, kind="note", text="Rick prefers IPAs over lagers, async path.", cognify=False)
        assert r["dropped"] is None
        ep = episodes.get_episode(c, user_id=uid, episode_id=r["episode_id"])
        assert ep["embedding"] is None
        # detector fact on the same path: also NULL
        r2 = cap.capture(c, user_id=uid, kind="note", text="/remember me preferred_shell fish", cognify=False)
        f = facts.get_fact(c, user_id=uid, fact_id=r2["detector"]["fact_id"])
        assert f["embedding"] is None
        # recall's BM25 leg still finds the un-embedded episode
        hit = c.execute("SELECT id FROM episode WHERE user_id=%s AND tsv @@ plainto_tsquery('english', 'IPAs lagers')",
                        (uid,)).fetchall()
        assert [str(h["id"]) for h in hit] == [r["episode_id"]]
        # worker tick: embed_backfill fills both
        monkeypatch.setattr(curator, "embed_texts", lambda texts, **kw: [[0.2] * 768 for _ in texts])
        rep = curator.embed_backfill(c, limit=500)
        assert rep["facts"] >= 1 and rep["episodes"] >= 2
        assert episodes.get_episode(c, user_id=uid, episode_id=r["episode_id"])["embedding"] is not None
        assert facts.get_fact(c, user_id=uid, fact_id=r2["detector"]["fact_id"])["embedding"] is not None
        assert c.execute("SELECT count(*) AS n FROM episode WHERE user_id=%s AND embedding IS NULL", (uid,)).fetchone()["n"] == 0


def test_capture_sync_embeds_inline(uid, monkeypatch):
    from astoria.config import settings
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return [0.3] * 768
    monkeypatch.setattr(episodes, "embed_one", fake)
    monkeypatch.setattr(facts, "embed_one", fake)
    with db.conn() as c:
        r = cap.capture(c, user_id=uid, kind="note", text="sync capture should embed inline", cognify=False, sync=True)
        assert episodes.get_episode(c, user_id=uid, episode_id=r["episode_id"])["embedding"] is not None
        assert calls["n"] == 1
        # settings.embed_sync=True flips the default
        monkeypatch.setattr(settings(), "embed_sync", True)
        r2 = cap.capture(c, user_id=uid, kind="note", text="/remember me preferred_shell zsh", cognify=False)
        assert episodes.get_episode(c, user_id=uid, episode_id=r2["episode_id"])["embedding"] is not None
        assert facts.get_fact(c, user_id=uid, fact_id=r2["detector"]["fact_id"])["embedding"] is not None
        assert calls["n"] == 3
        # explicit sync=False wins over the setting
        r3 = cap.capture(c, user_id=uid, kind="note", text="explicit async even when the setting says sync", cognify=False, sync=False)
        assert episodes.get_episode(c, user_id=uid, episode_id=r3["episode_id"])["embedding"] is None
        assert calls["n"] == 3
