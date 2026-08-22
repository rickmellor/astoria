"""The ONE action dispatcher behind REST (`/op` + typed routes) and the MCP tools.

`do_action(action, params, client)` opens a transaction, calls the store/retrieval
functions, converts rows to JSON-safe dicts and maps domain errors to `{"error": ...}`
(with an advisory `status_code` the REST layer turns into a 4xx/503). Modules that are
still being built (capture / recall / worker) are imported lazily INSIDE the actions, so
the service boots — and everything else works — even when one of them is missing
(`{"error": "module not ready"}`, 503).

Nothing here calls the LLM — except the on-demand `resolve` / `resolve_apply` actions (the LLM
target resolver, cognify/targets.py; the LLM only picks targets, writes stay deterministic). TEI
outages degrade (embed_one → None) and never error.
"""
from __future__ import annotations

import importlib
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import psycopg
from dateutil import parser as dtparser
from psycopg.types.json import Jsonb

from astoria.config import settings
from astoria.store import db, facts

log = logging.getLogger("astoria.service")

VALID_ACTIONS = ("queue_stats", 
    "recall", "capture", "briefing", "profile",
    "facts_list", "fact_get", "fact_add", "fact_update", "fact_delete",
    "correct", "retract", "forget", "approve", "history", "as_of",
    "episodes_list", "episode_get", "episode_delete",
    "predicates_list", "predicate_update", "audit", "health", "user_wipe",
    # compat (MemoryOS shapes)
    "retrieve", "memories_add", "user_profile",
    # graph layer + aliases (store/graph.py, retrieval/graph.py)
    "graph", "edges_list", "edge_add", "edge_delete", "aliases_list", "alias_add", "alias_delete",
    # LLM target resolver (cognify/targets.py): the ONE read-path-adjacent LLM call — on demand only
    "resolve", "resolve_apply",
)
LAYERS = ("profile", "semantic", "procedural", "episodic")


# ---------------------------------------------------------------------------
# helpers

class NotReady(RuntimeError):
    """A sibling module (capture / recall / worker) is not importable yet."""


def _err(msg: str, status: int = 400) -> dict:
    return {"error": msg, "status_code": status}


def _mod(name: str):
    """Lazy import of a module that another agent may still be writing."""
    try:
        return importlib.import_module(name)
    except ImportError as e:
        raise NotReady(f"module not ready: {name} ({e})") from e


def _ts(v: Any) -> datetime | None:
    """ISO-8601 (or epoch) → aware UTC datetime; naive → UTC. None/"" → None. Raises ValueError."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        dt = v
    elif isinstance(v, (int, float)):
        dt = datetime.fromtimestamp(float(v), tz=UTC)
    else:
        try:
            dt = dtparser.isoparse(str(v))
        except (ValueError, OverflowError):
            try:
                dt = dtparser.parse(str(v))
            except (ValueError, OverflowError) as e:
                raise ValueError(f"bad timestamp {v!r}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _uid(p: dict) -> str:
    return str(p.get("user_id") or settings().user_default)


def _int(v: Any, default: int, lo: int = 0, hi: int = 1000) -> int:
    try:
        n = int(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def _float(v: Any, default: float | None) -> float | None:
    if v in (None, ""):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _bool(v: Any, default: bool = False) -> bool:
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def _list(v: Any) -> list:
    if v is None or v == "":
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    if isinstance(v, (list, tuple, set)):
        return [x for x in v if x not in (None, "")]
    return [v]


def _layers(v: Any) -> tuple | None:
    ls = [str(x).lower() for x in _list(v)]
    ls = [x for x in ls if x in LAYERS]
    return tuple(ls) or None


def _jsonable(v: Any) -> Any:
    """Deep JSON-safe conversion for arbitrary row/dict payloads (uuid/datetime → str)."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items() if k not in ("embedding", "tsv")}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    if hasattr(v, "tolist"):  # numpy vectors
        return None
    return str(v)


def _fact(row) -> dict | None:
    return facts.row_public(row)


def _facts(rows: Iterable) -> list[dict]:
    return [facts.row_public(r) for r in rows]


def _episode(row) -> dict | None:
    try:
        from astoria.store import episodes
        return episodes.row_public(row)
    except Exception:  # noqa: BLE001
        return _jsonable(row)


def _as_dict(r: Any) -> dict:
    """RecallResult may be a dict or a dataclass/pydantic model — normalise."""
    if r is None:
        return {}
    if isinstance(r, dict):
        return r
    for attr in ("model_dump", "to_dict", "asdict", "dict"):
        fn = getattr(r, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001
                pass
    if hasattr(r, "__dict__"):
        return dict(vars(r))
    return dict(r)


def _turn_fields(row: dict) -> tuple[str | None, str | None]:
    m = row.get("meta") or {}
    ui, ar = m.get("user_input"), m.get("agent_response")
    if ui is None and ar is None:
        try:
            from astoria.store.episodes import _parse_turn
            ui, ar = _parse_turn(row.get("body") or "")
        except Exception:  # noqa: BLE001
            pass
    return ui, ar


# ---------------------------------------------------------------------------
# individual actions (each receives an open connection `c`)

def _recall(c, p: dict, client: str) -> dict:
    recall = _mod("astoria.retrieval.recall")
    query = str(p.get("query") or "")
    kw = dict(
        user_id=_uid(p), query=query, session_id=p.get("session_id") or None,
        max_tokens=_int(p.get("max_tokens"), 1000, 50, 20000), limit=_int(p.get("limit"), 12, 1, 200),
        facts_only=_bool(p.get("facts_only")), include_profile=_bool(p.get("include_profile")),
        as_of=_ts(p.get("as_of")), as_believed_at=_ts(p.get("as_believed_at")), client=client,
        rerank=(None if p.get("rerank") in (None, "") else _bool(p.get("rerank"))),
    )
    layers = _layers(p.get("layers"))
    if layers:
        kw["layers"] = layers
    return _jsonable(_as_dict(recall.recall(c, **kw)))


def _capture(c, p: dict, client: str, *, kind_default: str = "note") -> dict:
    capture = _mod("astoria.core.capture")
    kw = dict(
        user_id=_uid(p), kind=str(p.get("kind") or kind_default), source=str(p.get("source") or client),
        session_id=p.get("session_id") or None, occurred_at=_ts(p.get("occurred_at")),
        importance=_float(p.get("importance"), 0.5), tags=_list(p.get("tags")), meta=p.get("meta") or None,
        cognify=_bool(p.get("cognify"), True), priority=str(p.get("priority") or "normal"),
    )
    if p.get("sync") is not None:   # per-request inline embedding; default follows settings().embed_sync
        kw["sync"] = _bool(p.get("sync"))
    if p.get("text"):
        kw["text"] = str(p["text"])
    if p.get("user_input") is not None or p.get("agent_response") is not None:
        kw["user_input"] = p.get("user_input")
        kw["agent_response"] = p.get("agent_response")
    if "text" not in kw and "user_input" not in kw:
        return _err("capture needs text or user_input/agent_response")
    return _jsonable(_as_dict(capture.capture(c, **kw)))


def _briefing(c, p: dict) -> dict:
    recall = _mod("astoria.retrieval.recall")
    return _jsonable(_as_dict(recall.briefing(c, user_id=_uid(p), max_tokens=_int(p.get("max_tokens"), 1200, 50, 20000))))


def _profile_row(c, user_id: str) -> dict:
    row = c.execute("SELECT user_id, narrative, version, rederived_at, source FROM profile WHERE user_id=%s",
                    (user_id,)).fetchone()
    return dict(row) if row else {"user_id": user_id, "narrative": "", "version": 0, "rederived_at": None,
                                  "source": None}


def _profile(c, p: dict) -> dict:
    uid = _uid(p)
    prof = _profile_row(c, uid)
    rows = facts.list_facts(c, user_id=uid, layer="profile", status="active", limit=_int(p.get("limit"), 200, 1, 1000))
    return {"user_id": uid, "narrative": prof.get("narrative") or "", "version": prof.get("version") or 0,
            "rederived_at": _jsonable(prof.get("rederived_at")), "facts": _facts(rows)}


def _facts_list(c, p: dict) -> list:
    return _facts(facts.list_facts(
        c, user_id=_uid(p), subject=p.get("subject") or None, predicate=p.get("predicate") or None,
        status=p.get("status") or "active", layer=p.get("layer") or None, q=p.get("q") or p.get("query") or None,
        limit=_int(p.get("limit"), 50, 1, 1000), offset=_int(p.get("offset"), 0, 0, 10**9)))


def _fact_get(c, p: dict) -> dict:
    fid = p.get("fact_id") or p.get("id")
    if not fid:
        return _err("fact_id required")
    row = facts.get_fact(c, user_id=_uid(p), fact_id=str(fid))
    return _fact(row) if row else _err("fact not found", 404)


def _fact_add(c, p: dict, client: str) -> dict:
    for k in ("subject", "predicate", "value"):
        if p.get(k) in (None, ""):
            return _err(f"{k} required")
    kw: dict[str, Any] = dict(
        user_id=_uid(p), subject=str(p["subject"]), predicate=str(p["predicate"]), value=str(p["value"]),
        source=str(p.get("source") or client), source_kind="explicit", actor=client,
        confidence=_float(p.get("confidence"), None), valid_from=_ts(p.get("valid_from")),
        valid_to=_ts(p.get("valid_to")), asserted_at=_ts(p.get("asserted_at")),
        layer=p.get("layer") or None, tags=_list(p.get("tags")), cardinality=p.get("cardinality") or None,
        historical=_bool(p.get("historical")), importance=_float(p.get("importance"), 0.5),
        is_belief=_bool(p.get("is_belief")), evidence=p.get("evidence") or None, meta=p.get("meta") or None,
        ref=p.get("ref") or None,
        # async write path: embedding NULL unless embed_sync or the request says sync=true (embed_backfill fills it)
        embed=bool(settings().embed_sync) or _bool(p.get("sync")),
    )
    if p.get("cardinality") not in (None, "", "functional", "set"):
        return _err("cardinality must be functional|set")
    res = facts.upsert_fact(c, **kw)
    return {"fact": _fact(res.get("fact")), "action": res.get("action"), "superseded": list(res.get("superseded") or [])}


_UPDATABLE = ("value", "confidence", "importance", "tags", "layer", "valid_from", "valid_to", "asserted_at",
              "is_belief", "ref", "status", "evidence", "detail")


def _fact_update(c, p: dict, client: str) -> dict:
    fid = p.get("fact_id") or p.get("id")
    if not fid:
        return _err("fact_id required")
    fields: dict[str, Any] = {}
    for k in _UPDATABLE:
        if k in p and p[k] is not None:
            v = p[k]
            if k in ("valid_from", "valid_to", "asserted_at"):
                v = _ts(v)
            elif k == "tags":
                v = _list(v)
            elif k in ("confidence", "importance"):
                v = _float(v, None)
            elif k == "is_belief":
                v = _bool(v)
            fields[k] = v
    row = facts.update_fact(c, user_id=_uid(p), fact_id=str(fid), actor=client, **fields)
    return _fact(row) if row else _err("fact not found", 404)


def _fact_delete(c, p: dict, client: str) -> dict:
    fid = p.get("fact_id") or p.get("id")
    if not fid:
        return _err("fact_id required")
    mode = str(p.get("mode") or "soft")
    if mode not in ("soft", "hard"):
        return _err("mode must be soft|hard")
    row = facts.forget(c, user_id=_uid(p), fact_id=str(fid), mode=mode, actor=client)
    if not row:
        return _err("fact not found", 404)
    return {"deleted": True, "mode": mode, "fact_id": str(row["id"])}


def _retract(c, p: dict, client: str) -> dict:
    uid = _uid(p)
    if not p.get("fact_id") and not (p.get("subject") or p.get("predicate")):
        return _err("retract needs fact_id or subject+predicate")
    if not p.get("fact_id") and not p.get("predicate"):
        return _err("retract by triple needs predicate")
    rows = facts.retract(c, user_id=uid, subject=p.get("subject") or None, predicate=p.get("predicate") or None,
                         value=p.get("value") or None, fact_id=str(p["fact_id"]) if p.get("fact_id") else None,
                         actor=client, source_kind=str(p.get("source_kind") or "explicit"),
                         reason=str(p.get("reason") or "retract"))
    return {"retracted": [str(r["id"]) for r in rows], "facts": _facts(rows)}


def _search_facts_simple(c, user_id: str, query: str, limit: int) -> list[dict]:
    """recall.search_facts_simple when available, else a hook ILIKE scan."""
    fn = None
    try:
        fn = getattr(_mod("astoria.retrieval.recall"), "search_facts_simple", None)
    except NotReady:
        pass
    if fn is not None:
        try:
            rows = fn(c, user_id=user_id, query=query, limit=limit)
        except TypeError:
            rows = fn(c, user_id, query, limit)  # positional variant
        return [dict(r) for r in rows]
    return [dict(r) for r in facts.list_facts(c, user_id=user_id, q=query, limit=limit)]


def _forget(c, p: dict, client: str) -> dict:
    uid = _uid(p)
    mode = str(p.get("mode") or "soft")
    if mode not in ("soft", "hard"):
        return _err("mode must be soft|hard")
    targets: list[str] = []
    if p.get("fact_id"):
        targets = [str(p["fact_id"])]
    elif p.get("subject") or p.get("predicate"):
        rows = facts.list_facts(c, user_id=uid, subject=p.get("subject") or None, predicate=p.get("predicate") or None,
                                status="active", limit=200)
        if p.get("value"):
            vn = facts.norm_value(str(p["value"]))
            rows = [r for r in rows if r.get("value_norm") == vn]
        targets = [str(r["id"]) for r in rows]
    elif p.get("query"):
        # conservative: only the best match unless the caller widens `limit` (semantic search is fuzzy)
        rows = _search_facts_simple(c, uid, str(p["query"]), _int(p.get("limit"), 1, 1, 50))
        targets = [str(r["id"]) for r in rows if r.get("id")]
    else:
        return _err("forget needs fact_id, subject/predicate, or query")
    forgotten = []
    for fid in targets:
        row = facts.forget(c, user_id=uid, fact_id=fid, mode=mode, actor=client)
        if row:
            forgotten.append({"id": str(row["id"]), "subject": row["subject"], "predicate": row["predicate"],
                              "value": row["value"], "mode": mode})
    if not forgotten and p.get("fact_id"):
        return _err("fact not found", 404)
    return {"forgotten": forgotten}


def _approve(c, p: dict, client: str) -> dict:
    fid = p.get("fact_id") or p.get("id")
    if not fid:
        return _err("fact_id required")
    row = facts.approve_staging(c, user_id=_uid(p), fact_id=str(fid), actor=client)
    return {"fact": _fact(row)} if row else _err("fact not found", 404)


def _history(c, p: dict) -> list | dict:
    if not p.get("predicate"):
        return _err("subject and predicate required")
    return _facts(facts.history(c, user_id=_uid(p), subject=p.get("subject") or _uid(p), predicate=str(p["predicate"])))


def _as_of(c, p: dict) -> list | dict:
    at = _ts(p.get("at"))
    if at is None:
        return _err("at (ISO-8601) required")
    rows = facts.as_of(c, user_id=_uid(p), at=at, as_believed_at=_ts(p.get("as_believed_at")),
                       subject=p.get("subject") or None, predicate=p.get("predicate") or None,
                       limit=_int(p.get("limit"), 50, 1, 1000))
    q = (p.get("query") or p.get("q") or "").strip().lower()
    if q:
        rows = [r for r in rows if q in (r.get("hook") or "").lower()]
    return _facts(rows)


def _episodes_list(c, p: dict) -> list:
    from astoria.store import episodes
    rows = episodes.list_episodes(c, user_id=_uid(p), session_id=p.get("session_id") or None,
                                  kind=p.get("kind") or None, status=p.get("status") or "active",
                                  limit=_int(p.get("limit"), 50, 1, 1000), offset=_int(p.get("offset"), 0, 0, 10**9))
    return [_episode(r) for r in rows]


def _episode_get(c, p: dict) -> dict:
    from astoria.store import episodes
    eid = p.get("episode_id") or p.get("id")
    if not eid:
        return _err("episode_id required")
    row = episodes.get_episode(c, user_id=_uid(p), episode_id=str(eid))
    return _episode(row) if row else _err("episode not found", 404)


def _episode_delete(c, p: dict, client: str) -> dict:
    from astoria.store import episodes
    eid = p.get("episode_id") or p.get("id")
    if not eid:
        return _err("episode_id required")
    uid = _uid(p)
    ok = episodes.delete_episode(c, user_id=uid, episode_id=str(eid))
    if not ok:
        return _err("episode not found", 404)
    c.execute("INSERT INTO audit(user_id, actor, op, target, detail) VALUES (%s,%s,%s,%s,%s)",
              (uid, client, "episode_delete", str(eid), Jsonb({})))
    return {"deleted": True, "episode_id": str(eid)}


def _predicates_list(c, p: dict) -> list:
    rows = c.execute("SELECT name, cardinality, layer_hint, auto, description, created_at FROM predicate "
                     "ORDER BY name").fetchall()
    return [_jsonable(dict(r)) for r in rows]


def _predicate_update(c, p: dict, client: str) -> dict:
    name = facts.canon_predicate(p.get("name") or p.get("predicate"))
    sets, args = [], []
    if p.get("cardinality"):
        if p["cardinality"] not in ("functional", "set"):
            return _err("cardinality must be functional|set")
        sets.append("cardinality=%s"); args.append(p["cardinality"])
    if p.get("layer_hint"):
        if p["layer_hint"] not in ("semantic", "profile", "procedural"):
            return _err("layer_hint must be semantic|profile|procedural")
        sets.append("layer_hint=%s"); args.append(p["layer_hint"])
    if p.get("description") is not None:
        sets.append("description=%s"); args.append(str(p["description"]))
    if not sets:
        row = c.execute("SELECT name, cardinality, layer_hint, auto, description, created_at FROM predicate WHERE name=%s",
                        (name,)).fetchone()
        return _jsonable(dict(row)) if row else _err("predicate not found", 404)
    sets.append("auto=false")
    row = c.execute(f"UPDATE predicate SET {', '.join(sets)} WHERE name=%s "
                    "RETURNING name, cardinality, layer_hint, auto, description, created_at", (*args, name)).fetchone()
    if not row:
        return _err("predicate not found", 404)
    c.execute("INSERT INTO audit(user_id, actor, op, target, detail) VALUES (%s,%s,%s,NULL,%s)",
              (_uid(p), client, "predicate_update", Jsonb({"name": name, **{k: p[k] for k in ("cardinality", "layer_hint") if p.get(k)}})))
    return _jsonable(dict(row))


def _audit(c, p: dict) -> list:
    rows = c.execute("SELECT id, user_id, actor, op, target, detail, created_at FROM audit WHERE user_id=%s "
                     "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
                     (_uid(p), _int(p.get("limit"), 50, 1, 1000), _int(p.get("offset"), 0, 0, 10**9))).fetchall()
    return [_jsonable(dict(r)) for r in rows]


def _queue_counts(c) -> dict:
    rows = c.execute("SELECT state, count(*) AS n FROM cognify_queue GROUP BY state").fetchall()
    by = {r["state"]: int(r["n"]) for r in rows}
    return {"pending": by.get("pending", 0) + by.get("failed", 0) + by.get("running", 0),
            "dead": by.get("dead", 0), "by_state": by}


def _queue_stats(c, p: dict, client: str) -> dict:
    rows = c.execute("SELECT state, count(*) AS n, min(created_at) AS oldest FROM cognify_queue GROUP BY state").fetchall()
    by = {r["state"]: int(r["n"]) for r in rows}
    oldest = {r["state"]: (r["oldest"].isoformat() if r["oldest"] else None) for r in rows}
    dead = c.execute("SELECT id, user_id, episode_id, attempts, last_error, next_attempt_at FROM cognify_queue "
                     "WHERE state='dead' ORDER BY created_at DESC LIMIT 20").fetchall()
    pend = c.execute("SELECT count(*) AS n FROM fact WHERE embedding IS NULL AND status IN ('active','staging')").fetchone()
    pende = c.execute("SELECT count(*) AS n FROM episode WHERE embedding IS NULL AND status='active'").fetchone()
    return {"by_state": by, "oldest": oldest, "pending": by.get("pending", 0) + by.get("failed", 0) + by.get("running", 0),
            "dead": by.get("dead", 0), "dead_jobs": [{k: (str(v) if k in ("id", "episode_id") else (v.isoformat() if hasattr(v, "isoformat") else v)) for k, v in d.items()} for d in dead],
            "embed_backlog": {"facts": pend["n"], "episodes": pende["n"]}}


def _health() -> dict:
    from astoria.core import embed, llm
    s = settings()
    out: dict[str, Any] = {"status": "ok", "version": s.version}
    try:
        out["db"] = db.healthcheck()
        with db.conn() as c:
            out["queue"] = _queue_counts(c)
    except Exception as e:  # noqa: BLE001
        log.warning("health: db failed: %s", e)
        out.update(status="error", db={"ok": False, "error": str(e)}, queue=None, status_code=503)
    try:
        out["tei"] = embed.embed_health()
    except Exception as e:  # noqa: BLE001
        out["tei"] = {"ok": False, "error": str(e)}
    try:
        out["llm"] = llm.llm_health()
    except Exception as e:  # noqa: BLE001
        out["llm"] = {"saint": f"unreachable ({e})", "fallback": bool(s.anthropic_api_key)}
    try:
        from astoria.core import rerank as _rr
        out["rerank"] = _rr.rerank_health()
    except Exception as e:  # noqa: BLE001
        out["rerank"] = {"ok": False, "error": str(e)}
    return out


_WIPE_TABLES = ("cognify_queue", "snapshot", "edge", "alias", "entity", "fact", "episode", "tombstone", "profile_history", "profile", "audit")


def _user_wipe(c, p: dict, client: str) -> dict:
    uid = p.get("user_id")
    if not uid:
        return _err("user_id required")
    deleted: dict[str, int] = {}
    with c.cursor() as cur:
        for t in _WIPE_TABLES:
            cur.execute(f"DELETE FROM {t} WHERE user_id=%s", (uid,))
            deleted[t] = cur.rowcount
        cur.execute("INSERT INTO audit(user_id, actor, op, target, detail) VALUES (%s,%s,%s,NULL,%s)",
                    (uid, client, "user_wipe", Jsonb(deleted)))
    return {"deleted": True, "user_id": uid, "counts": deleted}


# ---- LLM target resolver --------------------------------------------------

def _resolve(c, p: dict, client: str) -> dict:
    """Natural-language memory instruction → plan (NOT applied). One LLM call (cognify/targets)."""
    targets = _mod("astoria.cognify.targets")
    text = str(p.get("text") or p.get("query") or "").strip()
    if not text:
        return _err("text required")
    plan = targets.resolve(c, user_id=_uid(p), text=text, limit=_int(p.get("limit"), 8, 1, 50))
    if plan.get("error_kind") == "llm_unavailable":
        return {**plan, "status_code": 503}
    return plan


def _resolve_apply(c, p: dict, client: str) -> dict:
    """Apply a plan (from `resolve`) or resolve `text` and apply it when no confirmation is needed
    (or confirm=true). Returns {applied, plan, changed, superseded, fact, action, reason?}."""
    targets = _mod("astoria.cognify.targets")
    uid = _uid(p)
    plan = p.get("plan")
    if plan is not None and not isinstance(plan, dict):
        return _err("plan must be an object (the result of resolve)")
    if plan is None:
        text = str(p.get("text") or p.get("query") or "").strip()
        if not text:
            return _err("resolve_apply needs plan or text")
        plan = targets.resolve(c, user_id=uid, text=text, limit=_int(p.get("limit"), 8, 1, 50))
        if plan.get("error_kind") == "llm_unavailable":
            return {**plan, "applied": False, "status_code": 503}
        if plan.get("error"):
            return {"applied": False, "plan": plan, "reason": plan["error"]}
    confirm = _bool(p.get("confirm"))
    intent = str(plan.get("intent") or "none")
    if intent == "none":
        return {"applied": False, "plan": plan, "reason": plan.get("error") or "no memory operation"}
    if plan.get("requires_confirmation", True) and not confirm:
        return {"applied": False, "plan": plan, "reason": "requires_confirmation"}
    res = targets.apply(c, user_id=uid, plan=plan, source=str(p.get("source") or client), actor=client)
    return {**res, "plan": plan}


# ---- MemoryOS compat ------------------------------------------------------

def _narrative_or_none(c, uid: str, recall_res: dict | None = None) -> str:
    nar = ""
    prof = (recall_res or {}).get("profile") if recall_res else None
    if isinstance(prof, dict):
        nar = prof.get("narrative") or ""
    if not nar:
        nar = _profile_row(c, uid).get("narrative") or ""
    return nar or "None"


def _compat_retrieve(c, p: dict, client: str) -> dict:
    from astoria.store import episodes
    uid = _uid(p)
    query = str(p.get("query") or "")
    recall = _mod("astoria.retrieval.recall")
    res = _as_dict(recall.recall(c, user_id=uid, query=query, limit=_int(p.get("limit"), 12, 1, 100),
                                 include_profile=True, client=client))
    # last 4 turns across sessions, oldest first
    turns = episodes.list_episodes(c, user_id=uid, kind="turn", limit=4)
    short_term = []
    for r in reversed(turns):
        ui, ar = _turn_fields(dict(r))
        short_term.append({"user_input": ui or "", "agent_response": ar or "",
                           "timestamp": _jsonable(r.get("occurred_at"))})
    pages, knowledge = [], []
    for it in res.get("items") or []:
        it = _as_dict(it)
        if it.get("kind") == "episode":
            ui, ar = None, None
            row = episodes.get_episode(c, user_id=uid, episode_id=str(it.get("id"))) if it.get("id") else None
            if row:
                ui, ar = _turn_fields(dict(row))
            pages.append({"user_input": ui if ui is not None else (it.get("text") or ""),
                          "agent_response": ar or "",
                          "timestamp": _jsonable(it.get("occurred_at")),
                          "meta_info": (row or {}).get("hook") or it.get("text") or ""})
        else:
            knowledge.append({"knowledge": it.get("text") or "",
                              "timestamp": _jsonable(it.get("asserted_at") or it.get("valid_from"))})
    return {"user_id": uid, "query": query, "short_term_history": short_term, "retrieved_pages": pages,
            "retrieved_user_knowledge": knowledge, "retrieved_assistant_knowledge": [],
            "user_profile": _narrative_or_none(c, uid, res)}


def _compat_memories_add(c, p: dict, client: str) -> dict:
    uid = _uid(p)
    if not (p.get("user_input") or p.get("agent_response")):
        return _err("user_input and agent_response required")
    r = _capture(c, {"user_id": uid, "kind": "turn", "user_input": p.get("user_input") or "",
                     "agent_response": p.get("agent_response") or "", "source": client,
                     "session_id": p.get("session_id") or None, "occurred_at": p.get("timestamp") or p.get("occurred_at"),
                     **({"sync": p["sync"]} if p.get("sync") is not None else {})},
                 client, kind_default="turn")
    if isinstance(r, dict) and r.get("error"):
        return r
    return {"status": "ok", "user_id": uid, **{k: r.get(k) for k in ("episode_id", "deduped", "queued") if k in r}}


def _compat_user_profile(c, p: dict) -> dict:
    uid = _uid(p)
    return {"user_id": uid, "user_profile": _narrative_or_none(c, uid)}


# ---- graph layer + aliases ---------------------------------------------------

def _graph(c, p: dict) -> dict:
    rg = _mod("astoria.retrieval.graph")
    node = p.get("node")
    if not node:
        return _err("node required (entity name, 'entity:<name>' or 'fact:<uuid>')")
    depth = _int(p.get("depth"), settings().graph_max_depth, 0, 6)
    fanout = _int(p.get("fanout"), settings().graph_max_fanout, 1, 200)
    return _jsonable(rg.render_graph(c, _uid(p), node, depth=depth, fanout=fanout))


def _edges_list(c, p: dict) -> list:
    g = _mod("astoria.store.graph")
    rows = g.list_edges(c, user_id=_uid(p), node=p.get("node") or None, relation=p.get("relation") or None,
                        depth=_int(p.get("depth"), 0, 0, 6), status=p.get("status") or "active",
                        limit=_int(p.get("limit"), 200, 1, 2000), offset=_int(p.get("offset"), 0, 0, 10**9))
    return [_jsonable(g.row_public(r)) for r in rows]


def _edge_add(c, p: dict, client: str) -> dict:
    g = _mod("astoria.store.graph")
    for k in ("src", "relation", "dst"):
        if not p.get(k):
            return _err(f"{k} required")
    try:
        res = g.add_edge(c, user_id=_uid(p), src=p["src"], relation=p["relation"], dst=p["dst"],
                         src_kind=p.get("src_kind") or None, dst_kind=p.get("dst_kind") or None,
                         weight=_float(p.get("weight"), 1.0), valid_from=_ts(p.get("valid_from")),
                         valid_to=_ts(p.get("valid_to")), source=str(p.get("source") or client),
                         source_kind=str(p.get("source_kind") or "explicit"), confidence=_float(p.get("confidence"), None),
                         evidence=p.get("evidence") or None, meta=p.get("meta") or None, actor=client)
    except LookupError as e:
        return _err(f"not found: {e}", 404)
    return {"edge": _jsonable(g.row_public(res["edge"])), "action": res["action"]}


def _edge_delete(c, p: dict, client: str) -> dict:
    g = _mod("astoria.store.graph")
    eid = p.get("edge_id") or p.get("id")
    if not eid:
        return _err("edge_id required")
    mode = str(p.get("mode") or "retract")
    if mode not in ("retract", "archive", "hard"):
        return _err("mode must be retract|archive|hard")
    row = g.retract_edge(c, user_id=_uid(p), edge_id=str(eid), actor=client, mode=mode)
    if not row:
        return _err("edge not found", 404)
    return {"deleted": mode == "hard", "mode": mode, "edge": _jsonable(g.row_public(row))}


def _aliases_list(c, p: dict) -> list:
    g = _mod("astoria.store.graph")
    rows = g.list_aliases(c, user_id=_uid(p), canonical=p.get("canonical") or None,
                          limit=_int(p.get("limit"), 500, 1, 5000), offset=_int(p.get("offset"), 0, 0, 10**9))
    return [_jsonable(g.row_public(dict(r))) for r in rows]


def _alias_add(c, p: dict, client: str) -> dict:
    g = _mod("astoria.store.graph")
    if not p.get("alias") or not p.get("canonical"):
        return _err("alias and canonical required")
    res = g.add_alias(c, user_id=_uid(p), alias=str(p["alias"]), canonical=str(p["canonical"]),
                      source=str(p.get("source") or client), source_kind=str(p.get("source_kind") or "explicit"),
                      actor=client)
    return {"alias": _jsonable(g.row_public(res["alias"])), "action": res["action"], "repointed": res["repointed"]}


def _alias_delete(c, p: dict, client: str) -> dict:
    g = _mod("astoria.store.graph")
    if not p.get("alias"):
        return _err("alias required")
    row = g.delete_alias(c, user_id=_uid(p), alias=str(p["alias"]), actor=client)
    if not row:
        return _err("alias not found", 404)
    return {"deleted": True, "alias": _jsonable(g.row_public(row))}


# ---------------------------------------------------------------------------
# dispatcher

def do_action(action: str, p: dict | None, client: str = "anonymous") -> dict | list:
    """Run one action. `p` holds only the params relevant to `action`; `client` is the caller's
    identity (fact `source` / audit actor). Returns JSON-safe dict or list; errors as
    `{"error": msg, "status_code": 4xx|503}`."""
    a = (action or "").strip().lower()
    p = dict(p or {})
    client = client or "anonymous"
    if a not in VALID_ACTIONS:
        return _err(f"unknown action: {action!r}. valid: {', '.join(VALID_ACTIONS)}")
    if a == "health":
        return _health()
    try:
        with db.conn() as c:
            if a == "queue_stats":
                return _queue_stats(c, p, client)
            if a == "recall":
                return _recall(c, p, client)
            if a == "capture":
                return _capture(c, p, client)
            if a == "briefing":
                return _briefing(c, p)
            if a == "profile":
                return _profile(c, p)
            if a == "facts_list":
                return _facts_list(c, p)
            if a == "fact_get":
                return _fact_get(c, p)
            if a in ("fact_add", "correct"):
                return _fact_add(c, p, client)
            if a == "fact_update":
                return _fact_update(c, p, client)
            if a == "fact_delete":
                return _fact_delete(c, p, client)
            if a == "retract":
                return _retract(c, p, client)
            if a == "forget":
                return _forget(c, p, client)
            if a == "approve":
                return _approve(c, p, client)
            if a == "history":
                return _history(c, p)
            if a == "as_of":
                return _as_of(c, p)
            if a == "episodes_list":
                return _episodes_list(c, p)
            if a == "episode_get":
                return _episode_get(c, p)
            if a == "episode_delete":
                return _episode_delete(c, p, client)
            if a == "predicates_list":
                return _predicates_list(c, p)
            if a == "predicate_update":
                return _predicate_update(c, p, client)
            if a == "audit":
                return _audit(c, p)
            if a == "user_wipe":
                return _user_wipe(c, p, client)
            if a == "retrieve":
                return _compat_retrieve(c, p, client)
            if a == "memories_add":
                return _compat_memories_add(c, p, client)
            if a == "user_profile":
                return _compat_user_profile(c, p)
            if a == "resolve":
                return _resolve(c, p, client)
            if a == "resolve_apply":
                return _resolve_apply(c, p, client)
            if a == "graph":
                return _graph(c, p)
            if a == "edges_list":
                return _edges_list(c, p)
            if a == "edge_add":
                return _edge_add(c, p, client)
            if a == "edge_delete":
                return _edge_delete(c, p, client)
            if a == "aliases_list":
                return _aliases_list(c, p)
            if a == "alias_add":
                return _alias_add(c, p, client)
            if a == "alias_delete":
                return _alias_delete(c, p, client)
    except NotReady as e:
        log.info("action %s: %s", a, e)
        return _err("module not ready", 503)
    except ValueError as e:
        return _err(str(e), 400)
    except LookupError as e:
        return _err(f"not found: {e}", 404)
    except psycopg.DataError as e:  # bad uuid / out-of-range literal etc.
        return _err(f"bad input: {str(e).splitlines()[0]}", 400)
    return _err(f"unhandled action: {a}", 500)
