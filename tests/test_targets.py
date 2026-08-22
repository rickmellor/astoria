"""LLM target-resolver tests (astoria/cognify/targets.py) — local dev DB.

    ASTORIA_WORKER_ENABLED=false pytest tests/test_targets.py -q          # canned LLM (monkeypatched)
    ASTORIA_WORKER_ENABLED=false pytest tests/test_targets.py -q -m llm   # + real SAINT calls
"""
from __future__ import annotations

import os
import uuid

import pytest

os.environ.setdefault("ASTORIA_DB_DSN", "postgresql://astoria:astoria@127.0.0.1:55432/astoria")
os.environ.setdefault("ASTORIA_WORKER_ENABLED", "false")

from astoria.api import service
from astoria.cognify import targets
from astoria.core import llm
from astoria.core.llm import LLMUnavailable
from astoria.store import db, facts


@pytest.fixture(scope="session", autouse=True)
def _migrated():
    db.migrate()
    yield
    db.close_pool()


@pytest.fixture
def user():
    uid = f"t_tgt_{uuid.uuid4().hex[:8]}"
    yield uid
    with db.conn() as c:
        for t in ("cognify_queue", "fact", "episode", "tombstone", "audit", "snapshot", "profile_history", "profile"):
            c.execute(f"DELETE FROM {t} WHERE user_id=%s", (uid,))


@pytest.fixture
def seeded(user):
    """favorite_beer=IPA, location=El Cerrito, uses_tool=Emacs (explicit, no embeddings → BM25/ILIKE candidates)."""
    with db.conn() as c:
        beer = facts.upsert_fact(c, user_id=user, subject=user, predicate="favorite_beer", value="IPA",
                                 source="cli", source_kind="explicit", embed=False)["fact"]
        loc = facts.upsert_fact(c, user_id=user, subject=user, predicate="location", value="El Cerrito",
                                source="cli", source_kind="explicit", cardinality="functional", embed=False)["fact"]
        emacs = facts.upsert_fact(c, user_id=user, subject=user, predicate="uses_tool", value="Emacs",
                                  source="cli", source_kind="explicit", embed=False)["fact"]
    return {"beer": str(beer["id"]), "loc": str(loc["id"]), "emacs": str(emacs["id"])}


class Canned:
    """monkeypatch target for llm.chat_json: returns the queued replies in order, records the calls."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    def __call__(self, messages, *, model=None, max_tokens=1500):
        self.calls.append(messages)
        assert max_tokens == targets.MAX_TOKENS_LLM
        if not self.replies:
            raise AssertionError("chat_json called more times than expected")
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _status(c, fid):
    return c.execute("SELECT status FROM fact WHERE id=%s", (fid,)).fetchone()["status"]


def _active(c, user_id, predicate):
    return c.execute("SELECT * FROM fact WHERE user_id=%s AND predicate=%s AND status='active' ORDER BY value",
                     (user_id, predicate)).fetchall()


# ---------------------------------------------------------------------------
# candidates / tokens

def test_salient_tokens():
    assert targets.salient_tokens("forget the thing about Guinness") == ["guinness"]
    assert "oakland" in targets.salient_tokens("actually I moved to Oakland")
    assert targets.salient_tokens("ok") == []


def test_gather_candidates_literal_and_cap(user, seeded):
    with db.conn() as c:
        cands = targets.gather_candidates(c, user_id=user, text="forget the thing about emacs")
        ids = {cf["id"] for cf in cands}
        assert seeded["emacs"] in ids
        for cf in cands:
            assert "embedding" not in cf and cf.get("status") == "active"
        assert len(targets.gather_candidates(c, user_id=user, text="emacs ipa cerrito", limit=2)) <= 2


# ---------------------------------------------------------------------------
# resolve() with canned plans

def test_resolve_forget_single_target_no_confirmation(user, seeded, monkeypatch):
    canned = Canned({"intent": "forget",
                     "targets": [{"fact_id": seeded["beer"], "reason": "the beer fact"}],
                     "new_fact": None, "confidence": 0.92, "explanation": "Forget favorite_beer=IPA."})
    monkeypatch.setattr(llm, "chat_json", canned)
    with db.conn() as c:
        plan = targets.resolve(c, user_id=user, text="forget the beer stuff")
    assert plan["intent"] == "forget"
    assert [t["id"] for t in plan["targets"]] == [seeded["beer"]]
    assert plan["targets"][0]["predicate"] == "favorite_beer" and plan["targets"][0]["reason"] == "the beer fact"
    assert plan["new_fact"] is None
    assert plan["requires_confirmation"] is False
    assert plan["candidates"] >= 1 and plan["text"] == "forget the beer stuff"
    assert len(canned.calls) == 1
    # the prompt carried the candidates and the registry
    user_msg = canned.calls[0][-1]["content"]
    assert seeded["beer"] in user_msg and "favorite_beer:functional" in user_msg


def test_resolve_low_confidence_or_many_targets_needs_confirmation(user, seeded, monkeypatch):
    monkeypatch.setattr(llm, "chat_json", Canned(
        {"intent": "forget", "targets": [{"fact_id": seeded["beer"], "reason": ""}],
         "new_fact": None, "confidence": 0.6, "explanation": "vague"},
        {"intent": "retract", "targets": [{"fact_id": seeded["beer"], "reason": ""}, {"fact_id": seeded["emacs"], "reason": ""}],
         "new_fact": None, "confidence": 0.95, "explanation": "two"},
    ))
    with db.conn() as c:
        p1 = targets.resolve(c, user_id=user, text="forget the beer stuff")
        p2 = targets.resolve(c, user_id=user, text="I don't drink beer or use emacs anymore")
    assert p1["requires_confirmation"] is True
    assert p2["intent"] == "retract" and len(p2["targets"]) == 2 and p2["requires_confirmation"] is True


def test_resolve_correct_and_remember_and_none(user, seeded, monkeypatch):
    monkeypatch.setattr(llm, "chat_json", Canned(
        {"intent": "correct", "targets": [{"fact_id": seeded["loc"], "reason": "old location"}],
         "new_fact": {"subject": "I", "predicate": "location", "value": " Oakland ", "valid_from": None},
         "confidence": 0.9, "explanation": "Replace El Cerrito with Oakland."},
        {"intent": "remember", "targets": [{"fact_id": seeded["loc"], "reason": "should be dropped"}],
         "new_fact": {"subject": "rick", "predicate": "Prefers Tabs", "value": "yes"},
         "confidence": 0.8, "explanation": "new fact"},
        {"intent": "none", "targets": [], "new_fact": None, "confidence": 0.97,
         "explanation": "A question, not a memory change."},
    ))
    with db.conn() as c:
        p_correct = targets.resolve(c, user_id=user, text="actually I moved to Oakland")
        p_remember = targets.resolve(c, user_id=user, text="remember that I prefer tabs")
        p_none = targets.resolve(c, user_id=user, text="what beer do I like?")
    assert p_correct["intent"] == "correct"
    assert [t["id"] for t in p_correct["targets"]] == [seeded["loc"]]
    assert p_correct["new_fact"] == {"subject": user, "predicate": "location", "value": "Oakland", "valid_from": None}
    assert p_correct["requires_confirmation"] is False
    # remember: targets are dropped, subject/predicate canonicalised, never needs confirmation
    assert p_remember["intent"] == "remember" and p_remember["targets"] == []
    assert p_remember["new_fact"]["predicate"] == "prefers_tabs"
    assert p_remember["requires_confirmation"] is False
    assert p_none["intent"] == "none" and p_none["targets"] == [] and p_none["new_fact"] is None
    assert p_none["requires_confirmation"] is False and "error" not in p_none


def test_resolve_rejects_target_not_in_candidates(user, seeded, monkeypatch):
    bogus = str(uuid.uuid4())
    canned = Canned(
        {"intent": "forget", "targets": [{"fact_id": bogus, "reason": "made up"}], "new_fact": None,
         "confidence": 0.9, "explanation": "x"},
        {"intent": "forget", "targets": [{"fact_id": bogus, "reason": "still made up"}], "new_fact": None,
         "confidence": 0.9, "explanation": "x"},
    )
    monkeypatch.setattr(llm, "chat_json", canned)
    with db.conn() as c:
        plan = targets.resolve(c, user_id=user, text="forget the beer stuff")
    assert plan["intent"] == "none" and plan["targets"] == []
    assert plan["error_kind"] == "invalid_plan" and "not one of the candidate ids" in plan["error"]
    assert len(canned.calls) == 2
    # the repair turn fed the validation error back
    assert "not valid" in canned.calls[1][-1]["content"] and bogus in canned.calls[1][-1]["content"]


def test_resolve_repair_retry_recovers(user, seeded, monkeypatch):
    canned = Canned(
        None,  # unparsable first reply
        {"intent": "retract", "targets": [seeded["emacs"][:12]],  # bare id string, abbreviated → prefix match
         "new_fact": None, "confidence": 0.93, "explanation": "Retract uses_tool=Emacs."},
    )
    monkeypatch.setattr(llm, "chat_json", canned)
    with db.conn() as c:
        plan = targets.resolve(c, user_id=user, text="I don't use Emacs anymore")
    assert plan["intent"] == "retract"
    assert [t["id"] for t in plan["targets"]] == [seeded["emacs"]]
    assert len(canned.calls) == 2 and canned.calls[1][-2]["content"] == "(unparsable reply)"


def test_resolve_correct_without_new_fact_is_invalid_then_repaired(user, seeded, monkeypatch):
    canned = Canned(
        {"intent": "correct", "targets": [{"fact_id": seeded["loc"], "reason": ""}], "new_fact": None,
         "confidence": 0.9, "explanation": "missing new_fact"},
        {"intent": "correct", "targets": [{"fact_id": seeded["loc"], "reason": ""}],
         "new_fact": {"subject": user, "predicate": "location", "value": "Oakland"},
         "confidence": 0.9, "explanation": "ok now"},
    )
    monkeypatch.setattr(llm, "chat_json", canned)
    with db.conn() as c:
        plan = targets.resolve(c, user_id=user, text="actually I live in Oakland")
    assert plan["intent"] == "correct" and plan["new_fact"]["value"] == "Oakland"
    assert "requires new_fact" in canned.calls[1][-1]["content"]


def test_resolve_llm_unavailable_and_empty_text(user, seeded, monkeypatch):
    monkeypatch.setattr(llm, "chat_json", Canned(LLMUnavailable("saint: down; anthropic: no key")))
    with db.conn() as c:
        plan = targets.resolve(c, user_id=user, text="forget the beer stuff")
        empty = targets.resolve(c, user_id=user, text="   ")
    assert plan["intent"] == "none" and plan["error_kind"] == "llm_unavailable"
    assert empty["intent"] == "none" and empty["error_kind"] == "bad_input"


# ---------------------------------------------------------------------------
# apply() — deterministic execution through facts.*

def test_apply_forget_soft_archives_and_tombstones(user, seeded):
    plan = {"intent": "forget", "targets": [{"id": seeded["beer"]}], "new_fact": None, "text": "forget the beer stuff"}
    with db.conn() as c:
        res = targets.apply(c, user_id=user, plan=plan, source="input", actor="input")
        assert res["applied"] is True and res["action"] == "forgotten"
        assert [ch["op"] for ch in res["changed"]] == ["forget"]
        assert res["changed"][0]["fact"]["id"] == seeded["beer"]
        assert _status(c, seeded["beer"]) == "archived"
        assert _status(c, seeded["loc"]) == "active"
        ts = c.execute("SELECT * FROM tombstone WHERE user_id=%s AND predicate='favorite_beer'", (user,)).fetchone()
        assert ts and ts["reason"] == "forget_soft"
        # a later machine re-extraction of the same value is blocked; an explicit re-assert lifts it
        blocked = facts.upsert_fact(c, user_id=user, subject=user, predicate="favorite_beer", value="IPA",
                                    source="input", source_kind="extracted", embed=False)
        assert blocked["action"] == "blocked"


def test_apply_retract_by_id_explicit(user, seeded):
    plan = {"intent": "retract", "targets": [{"fact_id": seeded["emacs"], "reason": "x"}], "text": "no more emacs"}
    with db.conn() as c:
        res = targets.apply(c, user_id=user, plan=plan, source="cli", actor="cli")
        assert res["applied"] and res["action"] == "retracted"
        assert _status(c, seeded["emacs"]) == "retracted"
        ts = c.execute("SELECT * FROM tombstone WHERE user_id=%s AND predicate='uses_tool'", (user,)).fetchone()
        assert ts and ts["reason"] == "resolved" and ts["blocks"] == "non-explicit"
        aud = c.execute("SELECT op, actor FROM audit WHERE user_id=%s AND target=%s ORDER BY id DESC LIMIT 1",
                        (user, seeded["emacs"])).fetchone()
        assert aud["op"] == "retract" and aud["actor"] == "cli"
        # applying again is a no-op (already retracted)
        res2 = targets.apply(c, user_id=user, plan=plan, source="cli", actor="cli")
        assert res2["applied"] is False and res2["action"] == "noop"


def test_apply_correct_supersedes_target_explicit(user, seeded):
    plan = {"intent": "correct", "targets": [{"id": seeded["loc"]}],
            "new_fact": {"subject": user, "predicate": "location", "value": "Oakland", "valid_from": None},
            "text": "actually I moved to Oakland", "explanation": "Replace El Cerrito with Oakland.", "confidence": 0.9}
    with db.conn() as c:
        res = targets.apply(c, user_id=user, plan=plan, source="input", actor="input")
        assert res["applied"] and res["action"] == "superseded"
        assert res["superseded"] == [seeded["loc"]]
        new = res["fact"]
        assert new["value"] == "Oakland" and new["source_kind"] == "explicit" and new["source"] == "input"
        assert new["confidence"] == pytest.approx(0.90)
        assert new["meta"]["resolved"]["text"] == "actually I moved to Oakland"
        assert _status(c, seeded["loc"]) == "superseded"
        old = facts.get_fact(c, user_id=user, fact_id=seeded["loc"])
        assert str(old["superseded_by"]) == new["id"]
        assert [r["value"] for r in _active(c, user, "location")] == ["Oakland"]


def test_apply_correct_set_predicate_uses_contradicts(user, seeded):
    """Set-cardinality key: only `contradicts` (the resolved target) makes the new value supersede."""
    plan = {"intent": "correct", "targets": [{"id": seeded["emacs"]}],
            "new_fact": {"subject": user, "predicate": "uses_tool", "value": "Helix"},
            "text": "that's wrong, my editor is Helix"}
    with db.conn() as c:
        res = targets.apply(c, user_id=user, plan=plan, source="cli", actor="cli")
        assert res["applied"] and res["superseded"] == [seeded["emacs"]]
        assert [r["value"] for r in _active(c, user, "uses_tool")] == ["Helix"]


def test_apply_remember_and_none(user, seeded):
    with db.conn() as c:
        res = targets.apply(c, user_id=user, plan={"intent": "remember", "targets": [],
                                                   "new_fact": {"subject": "I", "predicate": "likes", "value": "stout"},
                                                   "text": "remember that I like stout"}, source="mcp", actor="mcp")
        assert res["applied"] and res["action"] == "inserted"
        assert res["fact"]["subject"] == user and res["fact"]["source_kind"] == "explicit"
        before = c.execute("SELECT count(*) AS n FROM audit WHERE user_id=%s", (user,)).fetchone()["n"]
        none = targets.apply(c, user_id=user, plan={"intent": "none", "targets": [{"id": seeded["beer"]}],
                                                    "new_fact": {"subject": user, "predicate": "x", "value": "y"}},
                             source="mcp", actor="mcp")
        assert none["applied"] is False and none["changed"] == []
        after = c.execute("SELECT count(*) AS n FROM audit WHERE user_id=%s", (user,)).fetchone()["n"]
        assert after == before                      # none touches nothing
        assert _status(c, seeded["beer"]) == "active"
        with pytest.raises(ValueError):
            targets.apply(c, user_id=user, plan={"intent": "explode"}, source="x")
        with pytest.raises(ValueError):
            targets.apply(c, user_id=user, plan={"intent": "correct", "targets": [], "new_fact": None}, source="x")


# ---------------------------------------------------------------------------
# service / REST / MCP wiring

def test_service_resolve_and_resolve_apply_confirm_semantics(user, seeded, monkeypatch):
    low = {"intent": "forget", "targets": [{"fact_id": seeded["beer"], "reason": "beer"}], "new_fact": None,
           "confidence": 0.6, "explanation": "vague"}
    monkeypatch.setattr(llm, "chat_json", Canned(low, low, low))
    plan = service.do_action("resolve", {"user_id": user, "text": "forget the beer stuff"}, "input")
    assert plan["intent"] == "forget" and plan["requires_confirmation"] is True
    assert "status_code" not in plan
    # text + no confirm → not applied
    r1 = service.do_action("resolve_apply", {"user_id": user, "text": "forget the beer stuff"}, "input")
    assert r1["applied"] is False and r1["reason"] == "requires_confirmation" and r1["plan"]["intent"] == "forget"
    with db.conn() as c:
        assert _status(c, seeded["beer"]) == "active"
    # text + confirm → applied
    r2 = service.do_action("resolve_apply", {"user_id": user, "text": "forget the beer stuff", "confirm": True}, "input")
    assert r2["applied"] is True and r2["intent"] == "forget" and r2["plan"]["intent"] == "forget"
    with db.conn() as c:
        assert _status(c, seeded["beer"]) == "archived"
    # a plan passed back (as the CLI/MCP do) applies without another LLM call
    plan2 = {"intent": "retract", "targets": [{"id": seeded["emacs"], "subject": user}], "new_fact": None,
             "requires_confirmation": False, "text": "no emacs"}
    r3 = service.do_action("resolve_apply", {"user_id": user, "plan": plan2}, "cli")
    assert r3["applied"] is True and r3["action"] == "retracted"
    assert service.do_action("resolve", {"user_id": user}, "cli")["status_code"] == 400
    assert service.do_action("resolve_apply", {"user_id": user}, "cli")["status_code"] == 400


def test_service_resolve_llm_unavailable_is_503(user, seeded, monkeypatch):
    monkeypatch.setattr(llm, "chat_json", Canned(LLMUnavailable("down"), LLMUnavailable("down")))
    r = service.do_action("resolve", {"user_id": user, "text": "forget the beer stuff"}, "cli")
    assert r["status_code"] == 503 and r["error_kind"] == "llm_unavailable"
    r = service.do_action("resolve_apply", {"user_id": user, "text": "forget the beer stuff", "confirm": True}, "cli")
    assert r["status_code"] == 503 and r["applied"] is False


def test_rest_resolve_routes(user, seeded, monkeypatch):
    from fastapi.testclient import TestClient

    from astoria.api.app import app
    good = {"intent": "correct", "targets": [{"fact_id": seeded["loc"], "reason": "old"}],
            "new_fact": {"subject": user, "predicate": "location", "value": "Oakland"},
            "confidence": 0.95, "explanation": "Replace El Cerrito with Oakland."}
    add = {"intent": "remember", "targets": [], "new_fact": {"subject": user, "predicate": "likes", "value": "stout"},
           "confidence": 0.9, "explanation": "new fact"}
    monkeypatch.setattr(llm, "chat_json", Canned(good, add))
    # no `with`: don't enter the app lifespan (the FastMCP session manager can start only once per process
    # and tests/test_api.py owns that); the routes only need the lazily-created DB pool.
    if True:
        tc = TestClient(app)
        r = tc.post("/resolve", json={"user_id": user, "text": "actually I moved to Oakland"},
                    headers={"X-Astoria-Client": "pytest"})
        assert r.status_code == 200, r.text
        plan = r.json()
        assert plan["intent"] == "correct" and plan["requires_confirmation"] is False
        r = tc.post("/resolve/apply", json={"user_id": user, "plan": plan}, headers={"X-Astoria-Client": "pytest"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["applied"] and body["superseded"] == [seeded["loc"]] and body["fact"]["source"] == "pytest"
        r = tc.post("/resolve", json={"user_id": user, "text": ""})
        assert r.status_code == 400
        r = tc.post("/resolve/apply", json={"user_id": user, "text": "remember that I like stout"})
        assert r.status_code == 200 and r.json()["applied"] is True  # remember → applies without confirm
        assert r.json()["fact"]["value"] == "stout"
        # MCP dispatcher exposes the same two actions
        from astoria.api.mcp_tools import build_mcp
        mcp = build_mcp()
        import asyncio
        tools = asyncio.run(mcp.get_tools()) if asyncio.iscoroutinefunction(mcp.get_tools) else mcp.get_tools()
        mem = tools["memory"] if isinstance(tools, dict) else next(t for t in tools if t.name == "memory")
        assert "resolve" in (mem.description or "") and "resolve_apply" in (mem.description or "")


# ---------------------------------------------------------------------------
# real LLM (opt-in):  ASTORIA_WORKER_ENABLED=false pytest tests/test_targets.py -m llm

@pytest.mark.llm
def test_real_llm_resolve(request, user):
    if "llm" not in (getattr(request.config.option, "markexpr", "") or ""):
        pytest.skip("real LLM call; run with -m llm")
    with db.conn() as c:  # embedded seed (TEI) so the hybrid candidate path is the real one
        seeded = {
            "beer": str(facts.upsert_fact(c, user_id=user, subject=user, predicate="favorite_beer", value="IPA",
                                          source="cli", source_kind="explicit")["fact"]["id"]),
            "loc": str(facts.upsert_fact(c, user_id=user, subject=user, predicate="location", value="El Cerrito",
                                         source="cli", source_kind="explicit", cardinality="functional")["fact"]["id"]),
            "emacs": str(facts.upsert_fact(c, user_id=user, subject=user, predicate="uses_tool", value="Emacs",
                                           source="cli", source_kind="explicit")["fact"]["id"]),
        }
    with db.conn() as c:
        p1 = targets.resolve(c, user_id=user, text="forget the beer stuff")
        print("\nforget plan:", p1)
        assert p1["intent"] == "forget", p1
        assert [t["id"] for t in p1["targets"]] == [seeded["beer"]], p1
        p2 = targets.resolve(c, user_id=user, text="actually I live in Oakland")
        print("correct plan:", p2)
        assert p2["intent"] == "correct", p2
        assert p2["new_fact"]["predicate"] == "location" and p2["new_fact"]["value"].lower() == "oakland", p2
        assert [t["id"] for t in p2["targets"]] == [seeded["loc"]], p2
        p3 = targets.resolve(c, user_id=user, text="I don't use Emacs anymore")
        print("retract plan:", p3)
        assert p3["intent"] == "retract" and [t["id"] for t in p3["targets"]] == [seeded["emacs"]], p3
        p4 = targets.resolve(c, user_id=user, text="what beer do I like?")
        print("none plan:", p4)
        assert p4["intent"] == "none", p4
        res = targets.apply(c, user_id=user, plan=p2, source="input", actor="input")
        print("apply correct:", res)
        assert [r["value"] for r in _active(c, user, "location")] == [p2["new_fact"]["value"]]
