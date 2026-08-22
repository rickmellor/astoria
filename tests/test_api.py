"""HTTP/MCP layer tests against the local dev DB (postgresql://astoria:astoria@127.0.0.1:55432/astoria).

Run:  ASTORIA_WORKER_ENABLED=false pytest tests/test_api.py -q
Each run uses a fresh user_id (wiped at the end via DELETE /users/{id}); the recall/capture
routes are skipped until their modules exist.
"""
from __future__ import annotations

import importlib.util
import json
import os
import uuid
from datetime import UTC, datetime

import pytest

os.environ.setdefault("ASTORIA_WORKER_ENABLED", "false")
os.environ.setdefault("ASTORIA_CLIENT_TOKENS", "claude-code:test-token-cc")

from fastapi.testclient import TestClient

from astoria.api.app import app
from astoria.store import db, facts

HAS_RECALL = importlib.util.find_spec("astoria.retrieval.recall") is not None
HAS_CAPTURE = importlib.util.find_spec("astoria.core.capture") is not None
needs_recall = pytest.mark.skipif(not HAS_RECALL, reason="astoria.retrieval.recall not built yet")
needs_capture = pytest.mark.skipif(not HAS_CAPTURE, reason="astoria.core.capture not built yet")

UID = f"test_api_{uuid.uuid4().hex[:8]}"
HDR = {"X-Astoria-Client": "pytest"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c
        c.delete(f"/users/{UID}")


def _ok(r, code=200):
    assert r.status_code == code, f"{r.status_code}: {r.text[:400]}"
    return r.json()


# ---- basics -----------------------------------------------------------------

def test_root_and_health(client):
    r = _ok(client.get("/"))
    assert r["service"] == "astoria" and r["mcp"] == "/mcp/"
    h = _ok(client.get("/health"))
    assert h["status"] == "ok"
    for k in ("db", "tei", "llm", "queue", "version"):
        assert k in h
    assert "pending" in h["queue"] and "dead" in h["queue"]
    assert "facts_active" in h["db"]


def test_op_unknown_action_is_400(client):
    r = client.post("/op", json={"action": "nope"})
    assert r.status_code == 400 and "error" in r.json()


# ---- facts: add / list / correct / history / as_of / retract ------------------

def test_facts_add_and_list(client):
    r = _ok(client.post("/facts", json={"user_id": UID, "subject": "I", "predicate": "favorite_beer",
                                          "value": "Stout", "valid_from": "2020-01-01"}, headers=HDR))
    assert r["action"] == "inserted" and r["superseded"] == []
    f = r["fact"]
    assert f["subject"] == UID and f["predicate"] == "favorite_beer" and f["value"] == "Stout"
    assert f["status"] == "active" and f["source_kind"] == "explicit" and f["source"] == "pytest"
    assert "embedding" not in f
    lst = _ok(client.get("/facts", params={"user_id": UID}))
    assert any(x["id"] == f["id"] for x in lst)
    one = _ok(client.get(f"/facts/{f['id']}", params={"user_id": UID}))
    assert one["id"] == f["id"]
    assert client.get(f"/facts/{uuid.uuid4()}", params={"user_id": UID}).status_code == 404


def test_correct_supersedes(client):
    r = _ok(client.post("/correct", json={"user_id": UID, "subject": "me", "predicate": "favorite_beer",
                                            "value": "IPA", "valid_from": "2024-01-01"}, headers=HDR))
    assert r["action"] == "superseded" and len(r["superseded"]) == 1
    active = _ok(client.get("/facts", params={"user_id": UID, "predicate": "favorite_beer"}))
    assert len(active) == 1 and active[0]["value"] == "IPA"
    old = _ok(client.get(f"/facts/{r['superseded'][0]}", params={"user_id": UID}))
    assert old["status"] == "superseded" and old["superseded_by"] == r["fact"]["id"]


def test_history_chain(client):
    h = _ok(client.get("/history", params={"user_id": UID, "subject": UID, "predicate": "favorite_beer"}))
    assert [x["value"] for x in h] == ["IPA", "Stout"]
    assert h[0]["status"] == "active" and h[1]["status"] == "superseded"


def test_as_of(client):
    then = _ok(client.post("/as_of", json={"user_id": UID, "at": "2022-06-01", "predicate": "favorite_beer"}))
    assert [x["value"] for x in then] == ["Stout"]
    now = _ok(client.post("/as_of", json={"user_id": UID, "at": datetime.now(UTC).isoformat(),
                                           "predicate": "favorite_beer"}))
    assert [x["value"] for x in now] == ["IPA"]
    before = _ok(client.post("/as_of", json={"user_id": UID, "at": "2010-01-01", "predicate": "favorite_beer"}))
    assert before == []
    assert client.post("/as_of", json={"user_id": UID, "at": "not a date"}).status_code == 400


def test_patch_fact(client):
    f = _ok(client.get("/facts", params={"user_id": UID, "predicate": "favorite_beer"}))[0]
    r = _ok(client.patch(f"/facts/{f['id']}", json={"user_id": UID, "importance": 0.9, "tags": ["t1"]}))
    assert r["importance"] == pytest.approx(0.9) and r["tags"] == ["t1"]
    bad = client.patch(f"/facts/{f['id']}", json={"user_id": UID, "status": "retracted"})
    assert bad.status_code == 400 and "error" in bad.json()


def test_retract(client):
    _ok(client.post("/facts", json={"user_id": UID, "subject": "I", "predicate": "uses_tool", "value": "Emacs"}))
    r = _ok(client.post("/retract", json={"user_id": UID, "subject": "I", "predicate": "uses_tool", "value": "Emacs"}))
    assert len(r["retracted"]) == 1
    assert _ok(client.get("/facts", params={"user_id": UID, "predicate": "uses_tool"})) == []
    hist = _ok(client.get("/history", params={"user_id": UID, "subject": UID, "predicate": "uses_tool"}))
    assert hist[0]["status"] == "retracted"
    # fact_id variant, nothing left to retract → empty list, not an error
    r2 = _ok(client.post("/retract", json={"user_id": UID, "fact_id": hist[0]["id"]}))
    assert r2["retracted"] == []


def test_forget_soft_and_hard(client):
    a = _ok(client.post("/facts", json={"user_id": UID, "subject": "I", "predicate": "likes", "value": "sailing"}))
    b = _ok(client.post("/facts", json={"user_id": UID, "subject": "I", "predicate": "likes", "value": "skiing"}))
    r = _ok(client.post("/forget", json={"user_id": UID, "fact_id": a["fact"]["id"], "mode": "soft"}))
    assert [x["id"] for x in r["forgotten"]] == [a["fact"]["id"]]
    assert _ok(client.get(f"/facts/{a['fact']['id']}", params={"user_id": UID}))["status"] == "archived"
    r = _ok(client.delete(f"/facts/{b['fact']['id']}", params={"user_id": UID, "mode": "hard"}))
    assert r["deleted"] is True
    assert client.get(f"/facts/{b['fact']['id']}", params={"user_id": UID}).status_code == 404
    assert client.post("/forget", json={"user_id": UID, "fact_id": str(uuid.uuid4())}).status_code == 404
    # forget by query (hook ILIKE fallback when recall isn't built)
    c = _ok(client.post("/facts", json={"user_id": UID, "subject": "I", "predicate": "likes", "value": "kayaking"}))
    r = _ok(client.post("/forget", json={"user_id": UID, "query": "kayaking"}))
    assert c["fact"]["id"] in [x["id"] for x in r["forgotten"]]


def test_approve_flow(client):
    with db.conn() as c:
        res = facts.upsert_fact(c, user_id=UID, subject=UID, predicate="favorite_editor", value="Neovim",
                                source="pytest", source_kind="extracted", confidence=0.3, embed=False)
    assert res["action"] == "staging"
    sid = str(res["fact"]["id"])
    assert _ok(client.get("/facts", params={"user_id": UID, "predicate": "favorite_editor"})) == []
    staged = _ok(client.get("/facts", params={"user_id": UID, "predicate": "favorite_editor", "status": "staging"}))
    assert [x["id"] for x in staged] == [sid]
    r = _ok(client.post("/approve", json={"user_id": UID, "fact_id": sid}))
    f = r["fact"]
    assert f["status"] == "active" and f["value"] == "Neovim" and f["confidence"] >= 0.8
    assert _ok(client.get(f"/facts/{sid}", params={"user_id": UID}))["status"] == "archived"
    assert client.post("/approve", json={"user_id": UID, "fact_id": str(uuid.uuid4())}).status_code == 404


# ---- dispatcher, profile, predicates, audit, episodes -------------------------

def test_op_dispatcher(client):
    lst = _ok(client.post("/op", json={"action": "facts_list", "user_id": UID}))
    assert isinstance(lst, list) and lst
    r = _ok(client.post("/op", json={"action": "fact_add", "user_id": UID, "subject": "astoria",
                                     "predicate": "decided", "value": "REST+MCP on one uvicorn"}))
    assert r["action"] == "inserted" and r["fact"]["subject"] == "astoria"
    prof = _ok(client.post("/op", json={"action": "profile", "user_id": UID}))
    assert prof["user_id"] == UID and isinstance(prof["facts"], list)
    assert any(f["predicate"] == "favorite_beer" for f in prof["facts"])  # seed predicate → profile layer
    assert client.post("/op", json={"action": "fact_get", "user_id": UID}).status_code == 400


def test_profile_routes(client):
    p = _ok(client.get("/profile", params={"user_id": UID}))
    assert p["narrative"] == "" and p["version"] == 0
    compat = _ok(client.get(f"/users/{UID}/profile"))
    assert compat == {"user_id": UID, "user_profile": "None"}
    empty = _ok(client.get(f"/users/never_seen_{uuid.uuid4().hex[:6]}/profile"))
    assert empty["user_profile"] == "None"


def test_predicates(client):
    preds = _ok(client.get("/predicates"))
    names = {p["name"] for p in preds}
    assert {"favorite_beer", "uses_tool", "likes"} <= names
    r = _ok(client.patch("/predicates/decided", json={"layer_hint": "semantic"}))
    assert r["name"] == "decided" and r["auto"] is False
    assert client.patch("/predicates/decided", json={"cardinality": "nope"}).status_code == 400
    assert client.patch(f"/predicates/no_such_{uuid.uuid4().hex[:6]}", json={"cardinality": "set"}).status_code == 404


def test_audit(client):
    rows = _ok(client.get("/audit", params={"user_id": UID, "limit": 100}))
    ops = {r["op"] for r in rows}
    assert {"inserted", "superseded", "retract", "forget_soft", "forget_hard"} <= ops
    assert all(r["actor"] for r in rows)


def test_episodes_list_and_delete(client):
    from astoria.store import episodes
    with db.conn() as c:
        row = episodes.add_episode(c, user_id=UID, kind="note", text="astoria api test note", source="pytest",
                                   embed=False)["episode"]
    eid = str(row["id"])
    lst = _ok(client.get("/episodes", params={"user_id": UID, "kind": "note"}))
    assert any(e["id"] == eid for e in lst) and all("embedding" not in e for e in lst)
    one = _ok(client.get(f"/episodes/{eid}", params={"user_id": UID}))
    assert one["body"] == "astoria api test note"
    assert _ok(client.delete(f"/episodes/{eid}", params={"user_id": UID}))["deleted"] is True
    assert client.delete(f"/episodes/{eid}", params={"user_id": UID}).status_code == 404


def test_auth_token_maps_client(client):
    r = _ok(client.post("/facts", json={"user_id": UID, "subject": "I", "predicate": "preferred_shell",
                                          "value": "zsh"}, headers={"Authorization": "Bearer test-token-cc"}))
    assert r["fact"]["source"] == "claude-code"
    r = _ok(client.post("/facts", json={"user_id": UID, "subject": "I", "predicate": "timezone", "value": "UTC"}))
    assert r["fact"]["source"] == "anonymous"


# ---- recall / capture / compat (skipped until the modules exist) --------------

@needs_capture
def test_capture_note_and_turn(client):
    r = _ok(client.post("/capture", json={"user_id": UID, "kind": "note", "cognify": False,
                                            "text": "Astoria API test: the user keeps models on the NAS."}))
    assert r.get("episode_id") and r.get("deduped") is False
    again = _ok(client.post("/capture", json={"user_id": UID, "kind": "note", "cognify": False,
                                                "text": "Astoria API test: the user keeps models on the NAS."}))
    assert again.get("deduped") is True and again["episode_id"] == r["episode_id"]
    t = _ok(client.post("/capture", json={"user_id": UID, "kind": "turn", "session_id": "s1", "cognify": False,
                                            "user_input": "what's my favorite beer?",
                                            "agent_response": "IPA, per your memory."}))
    assert t.get("episode_id")
    dropped = _ok(client.post("/capture", json={"user_id": UID, "kind": "note", "text": "ok"}))
    assert dropped.get("dropped")
    eps = _ok(client.get("/episodes", params={"user_id": UID, "session_id": "s1"}))
    assert len(eps) == 1 and eps[0]["kind"] == "turn"


@needs_capture
def test_compat_memories_add(client):
    r = _ok(client.post("/memories", json={"user_id": UID, "user_input": "I moved to Brisbane last month",
                                             "agent_response": "Noted: Brisbane.",
                                             "timestamp": "2026-08-01T10:00:00"}))
    assert r["status"] == "ok" and r["user_id"] == UID
    eps = _ok(client.get("/episodes", params={"user_id": UID, "kind": "turn"}))
    assert any(e["occurred_at"].startswith("2026-08-01T10:00:00") for e in eps)


@needs_recall
def test_recall_route(client):
    r = _ok(client.post("/recall", json={"user_id": UID, "query": "favorite beer", "session_id": "s1",
                                           "include_profile": True}))
    for k in ("user_id", "query", "items", "working", "context", "health", "snapshot_id"):
        assert k in r, k
    assert isinstance(r["items"], list) and isinstance(r["context"], str)
    assert any(it.get("predicate") == "favorite_beer" and it.get("value") == "IPA" for it in r["items"])
    assert "IPA" in r["context"]
    if HAS_CAPTURE:
        assert r["working"] and r["working"][0]["user_input"].startswith("what's my favorite beer")
    empty = _ok(client.post("/recall", json={"user_id": f"nobody_{uuid.uuid4().hex[:6]}", "query": "anything"}))
    assert empty["items"] == [] and empty["context"] == ""


@needs_recall
def test_briefing_route(client):
    b = _ok(client.get("/briefing", params={"user_id": UID, "max_tokens": 800}))
    for k in ("narrative", "facts", "context"):
        assert k in b


@needs_recall
def test_compat_retrieve(client):
    r = _ok(client.post("/retrieve", json={"user_id": UID, "query": "beer"}))
    for k in ("user_id", "query", "short_term_history", "retrieved_pages", "retrieved_user_knowledge",
              "retrieved_assistant_knowledge", "user_profile"):
        assert k in r, k
    assert r["retrieved_assistant_knowledge"] == []
    assert any("IPA" in k["knowledge"] for k in r["retrieved_user_knowledge"])
    assert r["user_profile"] == "None" or isinstance(r["user_profile"], str)
    for t in r["short_term_history"]:
        assert {"user_input", "agent_response", "timestamp"} <= set(t)


def test_recall_route_503_when_module_missing(client):
    if HAS_RECALL:
        pytest.skip("recall module present")
    r = client.post("/recall", json={"user_id": UID, "query": "x"})
    assert r.status_code == 503 and r.json() == {"error": "module not ready"}


# ---- MCP mount ----------------------------------------------------------------

def _mcp_post(client, payload, session=None):
    h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if session:
        h["mcp-session-id"] = session
    r = client.post("/mcp/", json=payload, headers=h)
    body = r.text
    msgs = []
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        for line in body.splitlines():
            if line.startswith("data:"):
                msgs.append(json.loads(line[5:].strip()))
    elif body.strip():
        msgs.append(r.json())
    return r, msgs


def test_mcp_initialize_and_tools_list(client):
    r, msgs = _mcp_post(client, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                            "clientInfo": {"name": "pytest", "version": "0"}}})
    assert r.status_code == 200, r.text[:300]
    assert msgs and msgs[0]["result"]["serverInfo"]["name"] == "astoria"
    sid = r.headers.get("mcp-session-id")
    r2, _ = _mcp_post(client, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    assert r2.status_code in (200, 202)
    r3, msgs3 = _mcp_post(client, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sid)
    assert r3.status_code == 200
    names = {t["name"] for t in msgs3[-1]["result"]["tools"]}
    assert {"recall", "capture", "remember", "forget", "memory", "retrieve_memory", "add_memory",
            "get_user_profile"} <= names
    # one tool call through the MCP path
    r4, msgs4 = _mcp_post(client, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                   "params": {"name": "memory", "arguments": {"action": "health"}}}, sid)
    assert r4.status_code == 200 and msgs4[-1]["result"].get("isError") is not True


# ---- wipe -----------------------------------------------------------------------

def test_user_wipe(client):
    r = _ok(client.delete(f"/users/{UID}"))
    assert r["deleted"] is True and r["counts"]["fact"] >= 1
    assert _ok(client.get("/facts", params={"user_id": UID, "status": "any"})) == []
    assert _ok(client.get("/episodes", params={"user_id": UID, "status": "any"})) == []
