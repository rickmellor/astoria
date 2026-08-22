"""Graph layer + aliases — store, resolver apply, recall expansion and the REST routes.

    ASTORIA_WORKER_ENABLED=false pytest tests/test_graph.py -q     (local dev DB; no LLM, no TEI needed)
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

os.environ.setdefault("ASTORIA_DB_DSN", "postgresql://astoria:astoria@127.0.0.1:55432/astoria")
os.environ.setdefault("ASTORIA_WORKER_ENABLED", "false")

from astoria.cognify import resolver
from astoria.cognify.resolver import ExtractedAlias, ExtractedEdge, ExtractedFact, Extraction
from astoria.retrieval import graph as rgraph
from astoria.store import db, facts
from astoria.store import graph as G

TABLES = ("edge", "alias", "entity", "cognify_queue", "fact", "episode", "tombstone", "audit", "snapshot",
          "profile_history", "profile")


@pytest.fixture(scope="session", autouse=True)
def _migrated():
    db.migrate()
    yield
    db.close_pool()


@pytest.fixture
def user():
    uid = f"t_graph_{uuid.uuid4().hex[:8]}"
    yield uid
    with db.conn() as c:
        for t in TABLES:
            c.execute(f"DELETE FROM {t} WHERE user_id=%s", (uid,))


def _fact(c, user, subject, predicate, value, **kw):
    return facts.upsert_fact(c, user_id=user, subject=subject, predicate=predicate, value=value,
                             source="cli", source_kind="explicit", embed=False, **kw)["fact"]


# ---------------------------------------------------------------------------
# edges CRUD + idempotency

def test_edge_crud_and_idempotency(user):
    with db.conn() as c:
        r1 = G.add_edge(c, user_id=user, src="Johnny", relation="Runs On", dst="specul8-o-matic", source="cli")
        assert r1["action"] == "inserted"
        e = r1["edge"]
        assert (e["src_kind"], e["src_id"], e["relation"], e["dst_kind"], e["dst_id"]) == \
            ("entity", "johnny", "runs_on", "entity", "specul8-o-matic")
        assert abs(e["confidence"] - 0.90) < 1e-6 and e["status"] == "active"
        # entities auto-registered
        assert G.get_entity(c, user_id=user, name="johnny") and G.get_entity(c, user_id=user, name="SPECUL8-O-MATIC")

        # same key again → noop, same id, no duplicate row; bump keeps max weight
        r2 = G.add_edge(c, user_id=user, src="johnny", relation="runs_on", dst="specul8-o-matic", weight=3, source="cli")
        assert r2["action"] == "noop" and str(r2["edge"]["id"]) == str(e["id"]) and r2["edge"]["weight"] == 3
        n = c.execute("SELECT count(*) AS n FROM edge WHERE user_id=%s", (user,)).fetchone()["n"]
        assert n == 1

        # corroboration from an independent episode+client raises confidence, still noop
        ep = c.execute("INSERT INTO episode(user_id, kind, hook, body, source) VALUES (%s,'note','h','b','input') RETURNING id",
                       (user,)).fetchone()["id"]
        r3 = G.add_edge(c, user_id=user, src="johnny", relation="runs_on", dst="specul8-o-matic", source="input",
                        source_kind="extracted", confidence=0.6, origin_episode=str(ep))
        assert r3["action"] == "noop" and r3["edge"]["confidence"] > 0.90

        # a different relation is a different edge; a fact endpoint must exist
        f = _fact(c, user, "johnny", "default_profile", "daily")
        r4 = G.add_edge(c, user_id=user, src=f"fact:{f['id']}", relation="about", dst="johnny")
        assert r4["action"] == "inserted" and r4["edge"]["src_kind"] == "fact"
        with pytest.raises(LookupError):
            G.add_edge(c, user_id=user, src=f"fact:{uuid.uuid4()}", relation="about", dst="johnny")
        with pytest.raises(ValueError):
            G.add_edge(c, user_id=user, src="johnny", relation="related_to", dst="JOHNNY")   # self-loop

        # list / filter
        assert len(G.list_edges(c, user_id=user)) == 2
        assert [x["relation"] for x in G.list_edges(c, user_id=user, relation="runs_on")] == ["runs_on"]
        assert len(G.list_edges(c, user_id=user, node="johnny")) == 2
        assert len(G.list_edges(c, user_id=user, node="specul8-o-matic")) == 1
        assert len(G.list_edges(c, user_id=user, node="specul8-o-matic", depth=1)) == 2     # via johnny

        # retract → not active, re-add inserts a NEW active row (partial unique index allows it)
        gone = G.retract_edge(c, user_id=user, edge_id=str(e["id"]))
        assert gone["status"] == "retracted"
        assert G.get_edge(c, user_id=user, edge_id=str(e["id"]))["status"] == "retracted"
        assert len(G.list_edges(c, user_id=user, relation="runs_on")) == 0
        assert len(G.list_edges(c, user_id=user, relation="runs_on", status="any")) == 1
        r5 = G.add_edge(c, user_id=user, src="johnny", relation="runs_on", dst="specul8-o-matic")
        assert r5["action"] == "inserted" and str(r5["edge"]["id"]) != str(e["id"])
        # hard delete
        assert G.retract_edge(c, user_id=user, edge_id=str(r5["edge"]["id"]), mode="hard")["id"] == r5["edge"]["id"]
        assert G.get_edge(c, user_id=user, edge_id=str(r5["edge"]["id"])) is None
        assert G.retract_edge(c, user_id=user, edge_id=str(uuid.uuid4())) is None


# ---------------------------------------------------------------------------
# neighbors: depth / fanout bounds, cycle safety

def test_neighbors_bounds_and_cycles(user):
    with db.conn() as c:
        # chain a → b → c → d → a (cycle) + hub a with 30 leaves
        for s, d in (("a", "b"), ("b", "c"), ("c", "d"), ("d", "a")):
            G.add_edge(c, user_id=user, src=s, relation="next", dst=d)
        for i in range(30):
            G.add_edge(c, user_id=user, src="a", relation="has_leaf", dst=f"leaf{i:02d}", weight=1 + i / 100)

        n1 = G.neighbors(c, user, ["a"], max_depth=1, max_fanout=100)
        names = {x["id"] for x in n1}
        assert "b" in names and "d" in names and all(x["hops"] == 1 for x in n1)     # undirected: d→a reaches d
        assert sum(1 for x in names if x.startswith("leaf")) == 30

        # fanout: only the 5 strongest edges of `a` are followed
        n_fan = G.neighbors(c, user, ["a"], max_depth=1, max_fanout=5)
        assert len(n_fan) == 5
        leaf_ids = sorted(x["id"] for x in n_fan if x["id"].startswith("leaf"))
        assert leaf_ids == ["leaf25", "leaf26", "leaf27", "leaf28", "leaf29"]          # highest weights win

        # depth: hops are shortest-path; a is never returned (seed); cycle terminates
        n3 = G.neighbors(c, user, ["entity:a"], max_depth=3, max_fanout=100)
        by = {x["id"]: x for x in n3}
        assert "a" not in by
        assert by["b"]["hops"] == 1 and by["d"]["hops"] == 1 and by["c"]["hops"] == 2
        assert by["c"]["path"][0] == "entity:a" and len(by["c"]["relations"]) == 2
        n6 = G.neighbors(c, user, ["a"], max_depth=6, max_fanout=100)
        assert {x["id"] for x in n6} == {x["id"] for x in n3}                          # nothing new past the cycle
        assert G.neighbors(c, user, ["a"], max_depth=0) == []
        assert G.neighbors(c, user, ["a"], max_depth=2, max_fanout=100, max_results=3).__len__() == 3
        # depth-2 from a 2-hop-away seed sees b only at its nearest hop
        n_c = G.neighbors(c, user, ["c"], max_depth=2, max_fanout=100)
        byc = {x["id"]: x for x in n_c}
        assert byc["b"]["hops"] == 1 and byc["d"]["hops"] == 1 and byc["a"]["hops"] == 2
        assert all(x["hops"] <= 2 for x in n_c)


# ---------------------------------------------------------------------------
# aliases

def test_alias_resolve_flatten_and_delete(user):
    with db.conn() as c:
        assert G.resolve_alias(c, user, "specul8") is None
        r = G.add_alias(c, user_id=user, alias="Specul8", canonical="Specul8-O-Matic", source="cli")
        assert r["action"] == "inserted" and r["alias"]["alias"] == "specul8" and r["alias"]["canonical"] == "specul8-o-matic"
        assert G.resolve_alias(c, user, "SPECUL8") == "specul8-o-matic"
        assert G.resolve_alias(c, user, "  specul8 ") == "specul8-o-matic"
        assert G.resolve_alias(c, user, "specul8-o-matic") is None      # canonical is not an alias
        assert G.add_alias(c, user_id=user, alias="specul8", canonical="specul8-o-matic")["action"] == "noop"
        # chain flattening: box → specul8 (an alias) lands on specul8-o-matic
        r2 = G.add_alias(c, user_id=user, alias="the box", canonical="specul8")
        assert r2["alias"]["canonical"] == "specul8-o-matic"
        # re-pointing: make specul8-o-matic an alias of 'workstation' → existing aliases follow
        r3 = G.add_alias(c, user_id=user, alias="specul8-o-matic", canonical="workstation")
        assert r3["repointed"] == 2
        assert G.resolve_alias(c, user, "specul8") == "workstation"
        assert G.resolve_alias(c, user, "the box") == "workstation"
        assert {a["alias"] for a in G.list_aliases(c, user_id=user, canonical="workstation")} == \
            {"specul8", "the box", "specul8-o-matic"}
        assert len(G.list_aliases(c, user_id=user)) == 3
        with pytest.raises(ValueError):
            G.add_alias(c, user_id=user, alias="x", canonical="X")           # self
        with pytest.raises(ValueError):
            G.add_alias(c, user_id=user, alias=user, canonical="someone")     # never alias the user away
        # edges to an alias land on the canonical entity
        e = G.add_edge(c, user_id=user, src="johnny", relation="runs_on", dst="Specul8")["edge"]
        assert e["dst_id"] == "workstation"
        # delete
        assert G.delete_alias(c, user_id=user, alias="the box")["alias"] == "the box"
        assert G.resolve_alias(c, user, "the box") is None
        assert G.delete_alias(c, user_id=user, alias="the box") is None
        # the entity side
        assert G.get_entity(c, user_id=user, name="workstation") is not None
        ent = G.ensure_entity(c, user_id=user, name="workstation", kind="System", summary="4x R9700 box")
        assert ent["kind"] == "system"
        assert G.ensure_entity(c, user_id=user, name="workstation")["summary"] == "4x R9700 box"   # never blanks


# ---------------------------------------------------------------------------
# resolver.apply with edges + aliases

def test_resolver_apply_edges_and_aliases(user):
    with db.conn() as c:
        ep = c.execute("INSERT INTO episode(user_id, kind, hook, body, source, session_id) "
                       "VALUES (%s,'turn','h','User: johnny, now called nova, runs on specul8-o-matic in the garage','input','s1') "
                       "RETURNING id", (user,)).fetchone()["id"]
    parsed = Extraction(
        summary=f"{user}'s nova (formerly johnny) runs on specul8-o-matic.", nothing_durable=False,
        facts=[
            ExtractedFact(subject="nova", predicate="runs_on", value="specul8-o-matic", confidence=0.85,
                          evidence="runs on specul8-o-matic"),
            ExtractedFact(subject="specul8-o-matic", predicate="located_in", value="garage", confidence=0.7),
            ExtractedFact(subject=user, predicate="likes", value="", action="retract"),          # skipped (no value)
            ExtractedFact(subject="I", predicate="owns_hardware", value="specul8-o-matic", confidence=0.8),
        ],
        edges=[
            ExtractedEdge(src="nova", relation="runs_on", dst="specul8-o-matic", confidence=0.95, evidence="runs on"),
            ExtractedEdge(src="fact:1", relation="about", dst="fact:2", confidence=0.5),
            ExtractedEdge(src="fact:3", relation="about", dst="nova"),            # fact:3 was not written → skipped
            ExtractedEdge(src="fact:9", relation="about", dst="nova"),            # out of range → skipped
            ExtractedEdge(src="specul8-o-matic", relation="located_in", dst="Garage", confidence=0.1),
            ExtractedEdge(src="nova", relation="self", dst="NOVA"),              # self-loop → skipped
            ExtractedEdge(src="me", relation="owns", dst="fact:4"),              # "me" → user entity
        ],
        aliases=[
            ExtractedAlias(alias="johnny", canonical="nova", evidence="now called nova"),
            ExtractedAlias(alias="I", canonical="somebody"),                     # user → refused
            ExtractedAlias(alias="x", canonical="X"),                            # self → skipped
        ],
    )
    occ = datetime.now(UTC) + timedelta(seconds=1)
    with db.conn() as c:
        res = resolver.apply(c, user_id=user, episode_ids=[str(ep)], parsed=parsed, source="input",
                             session_id="s1", occurred_at=occ)
    assert [f["predicate"] for f in res["facts"]] == ["runs_on", "located_in", "owns_hardware"]
    assert res["aliases"] == [{"alias": "johnny", "canonical": "nova", "action": "inserted"}]
    edges = {(e["src"], e["relation"], e["dst"]): e for e in res["edges"]}
    f1, f2, f4 = res["facts"][0]["id"], res["facts"][1]["id"], res["facts"][2]["id"]
    assert set(edges) == {
        ("entity:nova", "runs_on", "entity:specul8-o-matic"),
        (f"fact:{f1}", "about", f"fact:{f2}"),
        ("entity:specul8-o-matic", "located_in", "entity:garage"),
        (f"entity:{user}", "owns", f"fact:{f4}"),
    }
    with db.conn() as c:
        rows = {(r["src_id"], r["relation"], r["dst_id"]): r for r in G.list_edges(c, user_id=user)}
        e = rows[("nova", "runs_on", "specul8-o-matic")]
        assert e["source_kind"] == "extracted" and abs(e["confidence"] - 0.85) < 1e-6     # clamped [.3,.85]
        assert str(e["origin_episode"]) == str(ep) and e["asserted_at"] == occ and e["evidence"] == "runs on"
        assert abs(rows[("specul8-o-matic", "located_in", "garage")]["confidence"] - 0.3) < 1e-6
        assert G.resolve_alias(c, user, "Johnny") == "nova"
        al = G.list_aliases(c, user_id=user)
        assert len(al) == 1 and al[0]["source_kind"] == "extracted"
        # replay is idempotent: edges noop, alias noop
        res2 = resolver.apply(c, user_id=user, episode_ids=[str(ep)], parsed=parsed, source="input",
                              session_id="s1", occurred_at=occ)
        assert all(e["action"] == "noop" for e in res2["edges"])
        assert res2["aliases"][0]["action"] == "noop"
        assert c.execute("SELECT count(*) AS n FROM edge WHERE user_id=%s AND status='active'", (user,)).fetchone()["n"] == 4


def test_extraction_schema_tolerates_bad_edges():
    raw = {"summary": None, "nothing_durable": False, "facts": [],
           "edges": [{"src": "a", "relation": "rel", "dst": "b", "confidence": "x"}, "junk", None, {"src": "only"}],
           "aliases": {"alias": "p", "canonical": "q"}}
    with pytest.raises(Exception):
        Extraction.model_validate(raw)            # {"src": "only"} lacks relation/dst → invalid (repair retry path)
    raw["edges"] = raw["edges"][:3]
    ex = Extraction.model_validate(raw)
    assert len(ex.edges) == 1 and ex.edges[0].confidence == 0.6
    assert len(ex.aliases) == 1 and ex.aliases[0].alias == "p"
    ex2 = Extraction.model_validate({"summary": None, "nothing_durable": True, "facts": []})
    assert ex2.edges == [] and ex2.aliases == []


# ---------------------------------------------------------------------------
# expand_candidates

def test_expand_candidates_hops(user):
    with db.conn() as c:
        seed = _fact(c, user, "johnny", "default_profile", "daily", importance=0.9)
        sib = _fact(c, user, "johnny", "uses_tool", "vllm", importance=0.4)            # sibling: same subject
        box = _fact(c, user, "specul8-o-matic", "has_gpu", "4x R9700", importance=0.8)
        far = _fact(c, user, "garage", "temperature", "warm")
        other = _fact(c, user, "unrelated", "fact", "nothing")
        mine = _fact(c, user, user, "favorite_beer", "IPA")                            # user hub — never pulled
        G.add_edge(c, user_id=user, src="johnny", relation="runs_on", dst="specul8-o-matic")
        G.add_edge(c, user_id=user, src="specul8-o-matic", relation="located_in", dst="garage")
        G.add_edge(c, user_id=user, src=f"fact:{seed['id']}", relation="about", dst=f"fact:{box['id']}")
        G.add_edge(c, user_id=user, src=user, relation="owns", dst="specul8-o-matic")

        out = rgraph.expand_candidates(c, user, [seed["id"]], depth=2, fanout=20)
        by = {str(r["id"]): r for r in out}
        assert str(seed["id"]) not in by                                    # seeds never returned
        assert by[str(box["id"])]["graph_hops"] == 1                        # direct fact→fact edge
        assert by[str(box["id"])]["graph_via"] == "about"
        assert by[str(sib["id"])]["graph_hops"] == 1 and by[str(sib["id"])]["graph_via"] == "subject"
        assert str(far["id"]) not in by                                     # johnny→box→garage→fact = 3 hops
        assert str(other["id"]) not in by and str(mine["id"]) not in by
        assert "hook" in by[str(box["id"])] and "confidence" in by[str(box["id"])]   # FACT_COLS projection
        assert [r["graph_hops"] for r in out] == sorted(r["graph_hops"] for r in out)

        out3 = rgraph.expand_candidates(c, user, [f"fact:{seed['id']}"], depth=3, fanout=20)
        by3 = {str(r["id"]): r for r in out3}
        assert by3[str(far["id"])]["graph_hops"] == 3
        assert str(mine["id"]) not in by3                                   # user hub skipped even when reached
        assert by3[str(far["id"])]["graph_path"][0] in (f"fact:{seed['id']}", "entity:johnny")

        # caps + degenerate inputs
        assert len(rgraph.expand_candidates(c, user, [seed["id"]], depth=3, fanout=20, max_results=1)) == 1
        assert rgraph.expand_candidates(c, user, [], depth=2) == []
        assert rgraph.expand_candidates(c, user, [seed["id"]], depth=0) == []
        assert rgraph.expand_candidates(c, user, ["not a uuid", None], depth=2) == []
        # no subject seeding → only edge-reachable facts
        noseed = rgraph.expand_candidates(c, user, [seed["id"]], depth=1, fanout=20, seed_subjects=False)
        assert {str(r["id"]) for r in noseed} == {str(box["id"])}

        # archived facts drop out of expansion
        facts.forget(c, user_id=user, fact_id=str(box["id"]), mode="soft")
        out_after = rgraph.expand_candidates(c, user, [seed["id"]], depth=2, fanout=20)
        assert str(box["id"]) not in {str(r["id"]) for r in out_after}

        # render_graph: induced subgraph with labels
        g = rgraph.render_graph(c, user, "johnny", depth=1)
        assert g["root"] == "entity:johnny" and g["nodes"][0]["facts"] >= 2
        ids = {n["id"] for n in g["nodes"]}
        assert "entity:specul8-o-matic" in ids and all(e["src"] in ids and e["dst"] in ids for e in g["edges"])


# ---------------------------------------------------------------------------
# REST routes

@pytest.fixture(scope="module")
def client():
    """REST router mounted on a bare FastAPI app (the full app's MCP session manager can only be
    started once per process — tests/test_api.py owns that TestClient)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from astoria.api.rest import router
    sub = FastAPI()
    sub.include_router(router)
    with TestClient(sub) as c:
        yield c


def _ok(r, code=200):
    assert r.status_code == code, f"{r.status_code}: {r.text[:400]}"
    return r.json()


def test_rest_graph_routes(client, user):
    H = {"X-Astoria-Client": "pytest"}
    f = _ok(client.post("/facts", json={"user_id": user, "subject": "johnny", "predicate": "default_profile",
                                        "value": "daily"}, headers=H))["fact"]
    e1 = _ok(client.post("/edges", json={"user_id": user, "src": "johnny", "relation": "runs_on",
                                         "dst": "specul8-o-matic", "evidence": "runs on"}, headers=H))
    assert e1["action"] == "inserted" and e1["edge"]["src"] == "entity:johnny" and e1["edge"]["source"] == "pytest"
    e2 = _ok(client.post("/edges", json={"user_id": user, "src": f"fact:{f['id']}", "relation": "about",
                                         "dst": "johnny"}, headers=H))
    assert e2["edge"]["src_kind"] == "fact"
    assert _ok(client.post("/edges", json={"user_id": user, "src": "johnny", "relation": "runs_on",
                                           "dst": "specul8-o-matic"}, headers=H))["action"] == "noop"
    r = client.post("/edges", json={"user_id": user, "src": f"fact:{uuid.uuid4()}", "relation": "x", "dst": "y"}, headers=H)
    assert r.status_code == 404
    r = client.post("/edges", json={"user_id": user, "src": "a", "relation": "x", "dst": "a"}, headers=H)
    assert r.status_code == 400

    rows = _ok(client.get("/edges", params={"user_id": user}))
    assert len(rows) == 2 and {x["relation"] for x in rows} == {"runs_on", "about"}
    rows = _ok(client.get("/edges", params={"user_id": user, "node": "specul8-o-matic", "depth": 1}))
    assert len(rows) == 2
    rows = _ok(client.get("/edges", params={"user_id": user, "relation": "about"}))
    assert len(rows) == 1

    g = _ok(client.get("/graph", params={"user_id": user, "node": "johnny", "depth": 2}))
    assert g["root"] == "entity:johnny" and g["counts"]["nodes"] == 3 and g["counts"]["edges"] == 2
    assert any(n["kind"] == "fact" and n["label"] for n in g["nodes"])
    assert client.get("/graph", params={"user_id": user}).status_code == 422            # node required

    # aliases
    a = _ok(client.post("/aliases", json={"user_id": user, "alias": "Specul8", "canonical": "specul8-o-matic"}, headers=H))
    assert a["action"] == "inserted" and a["alias"]["alias"] == "specul8"
    assert [x["alias"] for x in _ok(client.get("/aliases", params={"user_id": user}))] == ["specul8"]
    g2 = _ok(client.get("/graph", params={"user_id": user, "node": "specul8", "depth": 1}))
    assert g2["root"] == "entity:specul8-o-matic" and g2["nodes"][0]["aliases"] == ["specul8"]
    assert client.post("/aliases", json={"user_id": user, "alias": user, "canonical": "z"}, headers=H).status_code == 400
    d = _ok(client.delete(f"/aliases/specul8", params={"user_id": user}, headers=H))
    assert d["deleted"] is True
    assert client.delete("/aliases/specul8", params={"user_id": user}, headers=H).status_code == 404

    # edge delete (retract, then hard) + /op mirror + MCP-ish action names
    d = _ok(client.delete(f"/edges/{e1['edge']['id']}", params={"user_id": user}, headers=H))
    assert d["mode"] == "retract" and d["edge"]["status"] == "retracted"
    assert len(_ok(client.get("/edges", params={"user_id": user}))) == 1
    d = _ok(client.delete(f"/edges/{e1['edge']['id']}", params={"user_id": user, "mode": "hard"}, headers=H))
    assert d["deleted"] is True
    assert client.delete(f"/edges/{e1['edge']['id']}", params={"user_id": user}, headers=H).status_code == 404
    op = _ok(client.post("/op", json={"action": "edges_list", "user_id": user}, headers=H))
    assert len(op) == 1
    op = _ok(client.post("/op", json={"action": "alias_add", "user_id": user, "alias": "j", "canonical": "johnny"}, headers=H))
    assert op["alias"]["canonical"] == "johnny"
    assert any(r["op"] in ("edge_add", "alias_add") for r in _ok(client.get("/audit", params={"user_id": user})))
