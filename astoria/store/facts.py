"""Fact store — the deterministic, LLM-free control plane over bitemporal facts.

The make-or-break piece (DESIGN §7, round-2 review W1-W3). Supersession is:
  * serialized   — per-key advisory xact lock (+ partial unique indexes): no double-active rows;
  * ordered by ASSERTION time — the newer STATEMENT wins (`asserted_at`), independent of the
                   validity window it claims (`valid_from`), so a back-dated "since June" correction
                   still supersedes, and a delayed/out-of-order queue item cannot resurrect;
  * idempotent   — same value already active → NOOP + bump/corroborate (no re-supersede);
  * cardinality-aware — functional predicates supersede by key; set predicates add members and use
                   `retract` to remove one;
  * tombstone-aware — a human retract/forget/delete of a triple blocks non-explicit re-activation.
Retraction closes the BELIEF axis (expired_at) without touching valid_to.
"""
from __future__ import annotations

import hashlib
import uuid
import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable

import psycopg
from psycopg.types.json import Jsonb

from astoria.config import settings
from astoria.core.embed import embed_one

log = logging.getLogger("astoria.facts")

USER_ALIASES = {"user", "i", "me", "my", "myself", "the user", "owner", "self", "you", "rick"}
FUNCTIONAL_PREFIXES = ("favorite_", "default_", "primary_", "preferred_", "current_")
FUNCTIONAL_SUFFIXES = ("_is", "_name")

# trust caps by client (source) and by source_kind — trust = min(client_cap, kind_cap, content_conf)
CLIENT_TRUST = {"cli": 1.0, "human": 1.0, "input": 0.85, "claude-code": 0.85, "megaplan": 0.6,
                "curator": 0.5, "import": 0.4, "api": 0.7, "anonymous": 0.5, "mcp": 0.7}
KIND_TRUST = {"explicit": 1.0, "detector": 0.9, "extracted": 0.75, "imported": 0.45, "curator": 0.5}
KIND_CONF = {"explicit": 0.90, "detector": 0.80, "extracted": 0.60, "imported": 0.45, "curator": 0.50}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def canon_subject(s: str | None, user_id: str) -> str:
    t = re.sub(r"\s+", " ", (s or "").strip())
    if not t or t.lower() in USER_ALIASES or t.lower() == user_id.lower():
        return user_id
    return t.lower() if len(t) <= 64 else t[:64].lower()


def canon_predicate(p: str | None) -> str:
    t = (p or "fact").strip().lower()
    t = re.sub(r"[^a-z0-9_]+", "_", t).strip("_")
    return t[:64] or "fact"


def guess_cardinality(predicate: str) -> str:
    if predicate.startswith(FUNCTIONAL_PREFIXES) or predicate.endswith(FUNCTIONAL_SUFFIXES):
        return "functional"
    return "set"


def render_hook(subject: str, predicate: str, value: str) -> str:
    return f"{subject} {predicate.replace('_', ' ')}: {value}"


def norm_value(v: str) -> str:
    return re.sub(r"\s+", " ", (v or "").strip()).lower()


def trust_for(source: str | None, source_kind: str, content_conf: float | None = None) -> float:
    cap = min(CLIENT_TRUST.get((source or "anonymous").lower(), 0.6), KIND_TRUST.get(source_kind, 0.6))
    return min(cap, content_conf) if content_conf is not None else cap


def _lock_key(user_id: str, subject: str, predicate: str) -> int:
    h = hashlib.blake2b(f"{user_id}|{subject}|{predicate}".encode(), digest_size=8).digest()
    return int.from_bytes(h, "big", signed=True)


def ensure_predicate(cur: psycopg.Cursor, name: str, cardinality: str | None = None,
                     layer_hint: str | None = None) -> dict:
    row = cur.execute("SELECT name, cardinality, layer_hint FROM predicate WHERE name=%s", (name,)).fetchone()
    if row:
        if cardinality and cardinality != row["cardinality"]:
            cur.execute("UPDATE predicate SET cardinality=%s, auto=false WHERE name=%s", (cardinality, name))
            row = dict(row, cardinality=cardinality)
        return row
    card = cardinality or guess_cardinality(name)
    hint = layer_hint or "semantic"
    cur.execute("INSERT INTO predicate(name, cardinality, layer_hint, auto) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (name) DO NOTHING", (name, card, hint, cardinality is None))
    return {"name": name, "cardinality": card, "layer_hint": hint}


def _audit(cur, user_id: str, actor: str | None, op: str, target: Any, detail: dict) -> None:
    cur.execute("INSERT INTO audit(user_id, actor, op, target, detail) VALUES (%s,%s,%s,%s,%s)",
                (user_id, actor or "anonymous", op, target, Jsonb(detail)))


def _tombstoned(cur, user_id: str, subject: str, predicate: str, vnorm: str) -> dict | None:
    return cur.execute("SELECT * FROM tombstone WHERE user_id=%s AND subject=%s AND predicate=%s AND value_norm=%s "
                       "AND blocks='non-explicit'", (user_id, subject, predicate, vnorm)).fetchone()


def _tombstone(cur, user_id: str, subject: str, predicate: str, vnorm: str, reason: str, by: str | None,
               blocks: str = "non-explicit") -> None:
    cur.execute("INSERT INTO tombstone(user_id,subject,predicate,value_norm,reason,by_source,blocks) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (user_id,subject,predicate,value_norm) "
                "DO UPDATE SET reason=EXCLUDED.reason, by_source=EXCLUDED.by_source, blocks=EXCLUDED.blocks, created_at=now()",
                (user_id, subject, predicate, vnorm, reason, by, blocks))


def _lift_tombstone(cur, user_id: str, subject: str, predicate: str, vnorm: str) -> None:
    cur.execute("DELETE FROM tombstone WHERE user_id=%s AND subject=%s AND predicate=%s AND value_norm=%s",
                (user_id, subject, predicate, vnorm))



def _close_versioned(cur, old: dict, new_id, vf: datetime) -> uuid.UUID:
    """Bitemporal close of an active row `old` superseded by `new_id` (valid from `vf`).

    1. Insert a closed COPY of `old` (same identity/valid_from/asserted_at/embedding, valid_to=max(valid_from,vf),
       ingested_at=now, status='superseded', superseded_by=new_id, meta.version_of=old.id) — the CURRENT belief.
    2. Close the ORIGINAL on the belief axis only (expired_at=now, status='superseded', superseded_by=new_id,
       meta.belief_closed_by=<copy id>) — its valid_to stays as it was believed, so `as_believed_at` before
       the correction still returns it. Returns the copy's id (what the new fact's `supersedes` points at).
    """
    copy_id = uuid.uuid4()
    meta = dict(old.get("meta") or {})
    meta["version_of"] = str(old["id"])
    cur.execute(
        "INSERT INTO fact(id,user_id,subject,predicate,cardinality,value,hook,detail,embedding,layer,valid_from,valid_to,"
        "asserted_at,ingested_at,expired_at,status,supersedes,superseded_by,confidence,source,source_kind,source_trust,"
        "is_belief,importance,last_seen,access_count,corroborations,tags,origin_episode,evidence,ref,meta) "
        "SELECT %s,user_id,subject,predicate,cardinality,value,hook,detail,embedding,layer,valid_from,"
        "GREATEST(valid_from,%s),asserted_at,now(),NULL,'superseded',supersedes,%s,confidence,source,source_kind,"
        "source_trust,is_belief,importance,last_seen,access_count,corroborations,tags,origin_episode,evidence,ref,%s "
        "FROM fact WHERE id=%s", (copy_id, vf, new_id, Jsonb(meta), old["id"]))
    cur.execute(
        "UPDATE fact SET status='superseded', expired_at=now(), superseded_by=%s, "
        "meta=meta || %s WHERE id=%s", (new_id, Jsonb({"belief_closed_by": str(copy_id)}), old["id"]))
    return copy_id

# ---------------------------------------------------------------------------
# write path

def upsert_fact(c: psycopg.Connection, *, user_id: str, subject: str, predicate: str, value: str,
                source: str = "api", source_kind: str = "explicit", confidence: float | None = None,
                valid_from: datetime | None = None, valid_to: datetime | None = None,
                asserted_at: datetime | None = None, layer: str | None = None, is_belief: bool = False,
                importance: float = 0.5, tags: Iterable[str] = (), origin_episode: str | None = None,
                evidence: str | None = None, ref: dict | None = None, cardinality: str | None = None,
                actor: str | None = None, embed: bool = True, meta: dict | None = None,
                contradicts: Iterable[str] = (), historical: bool = False, status_override: str | None = None) -> dict:
    """The supersede-or-insert transaction (call inside `db.conn()`).

    Returns {"fact": row, "action": inserted|superseded|noop|historical|staging|blocked, "superseded": [ids]}.
    """
    s = settings()
    subject = canon_subject(subject, user_id)
    predicate = canon_predicate(predicate)
    value = re.sub(r"\s+", " ", (value or "").strip())
    if not value:
        raise ValueError("empty value")
    vnorm = norm_value(value)
    aa = asserted_at or now_utc()
    vf = valid_from or aa
    conf = confidence if confidence is not None else KIND_CONF.get(source_kind, 0.6)
    conf = max(s.confidence_floor, min(s.confidence_cap, float(conf)))
    trust = trust_for(source, source_kind, conf)
    hook = render_hook(subject, predicate, value)
    meta = dict(meta or {})

    with c.cursor() as cur:
        pred = ensure_predicate(cur, predicate, cardinality)
        card = pred["cardinality"]
        lay = layer or ("profile" if (subject == user_id and pred.get("layer_hint") == "profile")
                        else pred.get("layer_hint") or "semantic")
        if lay not in ("semantic", "profile", "procedural"):
            lay = "semantic"
        # Embed BEFORE taking the per-key lock (TEI ≈ 200 ms) so concurrent writers on one key
        # don't serialise on the network call. Cheap unlocked pre-check: a same-value re-assert
        # is a NOOP that needs no vector. (The locked path below re-reads authoritatively.)
        vec = None
        if embed and not historical:
            pre = cur.execute(
                "SELECT 1 FROM fact WHERE user_id=%s AND subject=%s AND predicate=%s AND status='active' "
                "AND value_norm=%s LIMIT 1", (user_id, subject, predicate, vnorm)).fetchone()
            if not pre:
                vec = embed_one(hook)
        elif embed:
            vec = embed_one(hook)
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key(user_id, subject, predicate),))

        # tombstone guard (cross-tier resurrection): only explicit human re-asserts lift it
        ts = _tombstoned(cur, user_id, subject, predicate, vnorm)
        if ts:
            if source_kind == "explicit":
                _lift_tombstone(cur, user_id, subject, predicate, vnorm)
            else:
                _audit(cur, user_id, actor, "blocked_tombstone", None,
                       {"subject": subject, "predicate": predicate, "value": value, "source_kind": source_kind})
                return {"fact": None, "action": "blocked", "superseded": []}

        # current active row(s) for the key
        if card == "functional":
            active = cur.execute(
                "SELECT * FROM fact WHERE user_id=%s AND subject=%s AND predicate=%s AND status='active' "
                "ORDER BY asserted_at DESC LIMIT 1", (user_id, subject, predicate)).fetchone()
        else:
            active = cur.execute(
                "SELECT * FROM fact WHERE user_id=%s AND subject=%s AND predicate=%s AND status='active' "
                "AND value_norm=%s LIMIT 1", (user_id, subject, predicate, vnorm)).fetchone()

        # idempotency: same value already active → NOOP + corroborate/bump
        if active and active["value_norm"] == vnorm and not historical:
            independent = (origin_episode is not None and str(active.get("origin_episode")) != str(origin_episode)
                           and active.get("source") != source)
            new_conf, corr = active["confidence"], active["corroborations"]
            if independent:
                corr += 1
                new_conf = min(s.confidence_cap, 1 - (1 - new_conf) * 0.6)
            # an explicit re-assert upgrades trust/kind
            new_trust = max(active["source_trust"], trust)
            new_kind = "explicit" if source_kind == "explicit" else active["source_kind"]
            cur.execute(
                "UPDATE fact SET last_seen=now(), access_count=access_count+1, corroborations=%s, confidence=%s, "
                "source_trust=%s, source_kind=%s, asserted_at=GREATEST(asserted_at,%s) WHERE id=%s RETURNING *",
                (corr, new_conf, new_trust, new_kind, aa, active["id"]))
            row = cur.fetchone()
            _audit(cur, user_id, actor, "noop", row["id"], {"subject": subject, "predicate": predicate, "value": value})
            return {"fact": row, "action": "noop", "superseded": []}

        # staging gate: low-confidence non-explicit extractions don't go active
        status = status_override or "active"
        if status == "active" and source_kind in ("extracted", "imported", "curator") and conf < s.confidence_staging_threshold:
            status = "staging"
        # trust guard: a machine-extracted/imported/curated value must not SILENTLY override a
        # human-stated (explicit/detector) active value on a FUNCTIONAL key. It may supersede only
        # when the extractor explicitly declared `contradicts` against that row (it saw the candidate
        # and judged a real contradiction — "actually my favorite beer is IPA"); an incidental same-key
        # assertion ("grew up near Skiatook" → location) lands in staging as a flagged conflict.
        declared = {str(x) for x in (contradicts or ())}
        if (status == "active" and active is not None and card == "functional"
                and aa >= active["asserted_at"]                      # older statements take the history path below
                and source_kind in ("extracted", "imported", "curator")
                and active.get("source_kind") in ("explicit", "detector")
                and str(active["id"]) not in declared):
            status = "staging"
            meta["conflict_with"] = str(active["id"])
            _audit(cur, user_id, actor, "conflict_staged", None,
                   {"subject": subject, "predicate": predicate, "value": value, "active": str(active["id"])})
        if embed and vec is None:  # pre-check guessed NOOP but the locked read disagreed (rare race)
            vec = embed_one(hook)

        new_id = uuid.uuid4()

        def _insert(st: str, sup_by=None, vt=None, supersedes=None):
            cur.execute(
                "INSERT INTO fact(id,user_id,subject,predicate,cardinality,value,hook,embedding,layer,valid_from,valid_to,"
                "asserted_at,expired_at,status,supersedes,superseded_by,confidence,source,source_kind,source_trust,"
                "is_belief,importance,tags,origin_episode,evidence,ref,meta) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (new_id, user_id, subject, predicate, card, value, hook, vec, lay, vf, vt, aa,
                 None, st, supersedes, sup_by, conf, source, source_kind,   # historical rows are CURRENT belief (expired_at NULL)
                 trust, is_belief, importance, list(tags), origin_episode, evidence, Jsonb(ref) if ref else None,
                 Jsonb(meta)))
            return cur.fetchone()

        # explicit history entry (valid_to given / historical=True): closed row, no supersede
        if historical or (valid_to is not None and valid_to <= now_utc()):
            row = _insert("superseded", vt=valid_to or vf)
            _audit(cur, user_id, actor, "historical", row["id"], {"subject": subject, "predicate": predicate, "value": value})
            return {"fact": row, "action": "historical", "superseded": []}

        # assertion-order guard (functional): an OLDER statement than the active one cannot clobber it
        if active and card == "functional" and status == "active" and aa < active["asserted_at"]:
            row = _insert("superseded", sup_by=active["id"], vt=min(active["valid_from"], active["asserted_at"]))
            _audit(cur, user_id, actor, "historical", row["id"],
                   {"reason": "older assertion than active", "active": str(active["id"])})
            return {"fact": row, "action": "historical", "superseded": []}

        superseded: list[str] = []
        supersedes_id = None
        if status == "active":
            # close the old active row(s) FIRST (the partial unique index forbids two actives), then insert.
            # Closing is BITEMPORAL: the original row keeps its valid window as we believed it (belief
            # axis closed: expired_at=now) and a closed COPY carries the corrected valid_to as the current
            # belief — so as_of(at, as_believed_at=B) still answers what we believed at B.
            if active and card == "functional":
                ver = _close_versioned(cur, active, new_id, vf)
                supersedes_id = ver
                superseded.append(str(active["id"]))
            # explicit contradictions named by the resolver (any cardinality), same user only
            for cid in contradicts or ():
                try:
                    r2 = cur.execute(
                        "SELECT * FROM fact WHERE id=%s AND user_id=%s AND status='active' AND id<>%s",
                        (cid, user_id, new_id)).fetchone()
                    if r2:
                        _close_versioned(cur, r2, new_id, vf)
                        superseded.append(str(r2["id"]))
                except Exception:  # bad uuid etc. — ignore
                    pass
        row = _insert(status, vt=valid_to, supersedes=supersedes_id)
        action = "superseded" if superseded else ("staging" if status == "staging" else "inserted")
        _audit(cur, user_id, actor, action, row["id"],
               {"subject": subject, "predicate": predicate, "value": value, "superseded": superseded})
        return {"fact": row, "action": action, "superseded": superseded}


def retract(c: psycopg.Connection, *, user_id: str, subject: str | None = None, predicate: str | None = None,
            value: str | None = None, fact_id: str | None = None, actor: str | None = None,
            source_kind: str = "explicit", reason: str = "retract") -> list[dict]:
    """Close the belief window ("we no longer believe this"); valid_to untouched. Tombstones the triple
    (explicit/detector retracts block re-extraction; extracted retracts don't block explicit re-asserts)."""
    with c.cursor() as cur:
        if fact_id:
            key = cur.execute("SELECT subject, predicate FROM fact WHERE id=%s AND user_id=%s", (fact_id, user_id)).fetchone()
            if not key:
                return []
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key(user_id, key["subject"], key["predicate"]),))
            rows = cur.execute("UPDATE fact SET status='retracted', expired_at=now() WHERE id=%s AND user_id=%s "
                               "AND status IN ('active','staging') RETURNING *", (fact_id, user_id)).fetchall()
        else:
            subject = canon_subject(subject, user_id)
            predicate = canon_predicate(predicate)
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key(user_id, subject, predicate),))
            q = ("UPDATE fact SET status='retracted', expired_at=now() WHERE user_id=%s AND subject=%s AND predicate=%s "
                 "AND status IN ('active','staging')")
            args: list = [user_id, subject, predicate]
            if value:
                q += " AND value_norm=%s"
                args.append(norm_value(value))
            rows = cur.execute(q + " RETURNING *", args).fetchall()
        for r in rows:
            _tombstone(cur, user_id, r["subject"], r["predicate"], r["value_norm"], reason, actor,
                       "non-explicit" if source_kind in ("explicit", "detector") else "none")
            _audit(cur, user_id, actor, "retract", r["id"], {"subject": r["subject"], "predicate": r["predicate"], "value": r["value"]})
        return rows


def forget(c: psycopg.Connection, *, user_id: str, fact_id: str, mode: str = "soft", actor: str | None = None) -> dict | None:
    with c.cursor() as cur:
        if mode == "hard":
            row = cur.execute("DELETE FROM fact WHERE id=%s AND user_id=%s RETURNING id, subject, predicate, value, value_norm",
                              (fact_id, user_id)).fetchone()
        else:
            row = cur.execute("UPDATE fact SET status='archived', expired_at=COALESCE(expired_at, now()) WHERE id=%s "
                              "AND user_id=%s RETURNING id, subject, predicate, value, value_norm", (fact_id, user_id)).fetchone()
        if row:
            _tombstone(cur, user_id, row["subject"], row["predicate"], row["value_norm"], f"forget_{mode}", actor)
            _audit(cur, user_id, actor, f"forget_{mode}", row["id"], {"subject": row["subject"], "predicate": row["predicate"]})
        return row


def update_fact(c: psycopg.Connection, *, user_id: str, fact_id: str, actor: str | None = None, **fields) -> dict | None:
    """Direct edit by id (no LLM). Editable: value, confidence, importance, tags, layer, valid_from, valid_to,
    asserted_at, is_belief, ref, status (active|archived|staging), evidence, detail."""
    allowed = {"value", "confidence", "importance", "tags", "layer", "valid_from", "valid_to", "asserted_at",
               "is_belief", "ref", "status", "evidence", "detail"}
    sets, args = [], []
    for k, v in fields.items():
        if k not in allowed or v is None:
            continue
        if k == "status" and v not in ("active", "archived", "staging"):
            raise ValueError("status must be active|archived|staging (use retract/forget otherwise)")
        if k == "ref":
            v = Jsonb(v)
        sets.append(f"{k}=%s")
        args.append(v)
    if not sets:
        return get_fact(c, user_id=user_id, fact_id=fact_id)
    with c.cursor() as cur:
        row = cur.execute(f"UPDATE fact SET {', '.join(sets)} WHERE id=%s AND user_id=%s RETURNING *",
                          (*args, fact_id, user_id)).fetchone()
        if row and fields.get("value"):
            hook = render_hook(row["subject"], row["predicate"], row["value"])
            cur.execute("UPDATE fact SET hook=%s, embedding=%s WHERE id=%s", (hook, embed_one(hook), fact_id))
            row = cur.execute("SELECT * FROM fact WHERE id=%s", (fact_id,)).fetchone()
        if row:
            _audit(cur, user_id, actor, "update", row["id"], {k: (str(v) if k != "ref" else "ref") for k, v in fields.items()})
        return row


def approve_staging(c: psycopg.Connection, *, user_id: str, fact_id: str, actor: str | None = None) -> dict | None:
    """Promote a staging row to active via the normal supersede path (so functional keys stay unique)."""
    row = get_fact(c, user_id=user_id, fact_id=fact_id)
    if not row or row["status"] != "staging":
        return row
    res = upsert_fact(c, user_id=user_id, subject=row["subject"], predicate=row["predicate"], value=row["value"],
                      source=row["source"], source_kind="explicit", confidence=max(row["confidence"], 0.8),
                      valid_from=row["valid_from"], asserted_at=now_utc(), layer=row["layer"],
                      importance=row["importance"], tags=row["tags"], origin_episode=row["origin_episode"],
                      evidence=row["evidence"], actor=actor, embed=False)
    with c.cursor() as cur:
        cur.execute("UPDATE fact SET status='archived', expired_at=now() WHERE id=%s AND status='staging'", (fact_id,))
        if res["fact"] is not None and row.get("embedding") is not None:
            cur.execute("UPDATE fact SET embedding=%s WHERE id=%s AND embedding IS NULL", (row["embedding"], res["fact"]["id"]))
    return res["fact"]


# ---------------------------------------------------------------------------
# read path

def get_fact(c: psycopg.Connection, *, user_id: str, fact_id: str) -> dict | None:
    return c.execute("SELECT * FROM fact WHERE id=%s AND user_id=%s", (fact_id, user_id)).fetchone()


def list_facts(c: psycopg.Connection, *, user_id: str, subject: str | None = None, predicate: str | None = None,
               status: str | None = "active", layer: str | None = None, q: str | None = None,
               limit: int = 50, offset: int = 0) -> list[dict]:
    where, args = ["user_id=%s"], [user_id]
    if subject:
        where.append("subject=%s"); args.append(canon_subject(subject, user_id))
    if predicate:
        where.append("predicate=%s"); args.append(canon_predicate(predicate))
    if status and status != "any":
        where.append("status=%s"); args.append(status)
    if layer:
        where.append("layer=%s"); args.append(layer)
    if q:
        where.append("(hook ILIKE %s)"); args.append(f"%{q}%")
    sql = (f"SELECT * FROM fact WHERE {' AND '.join(where)} ORDER BY layer, subject, predicate, asserted_at DESC "
           f"LIMIT %s OFFSET %s")
    return c.execute(sql, (*args, limit, offset)).fetchall()


def history(c: psycopg.Connection, *, user_id: str, subject: str, predicate: str,
            include_expired: bool = False) -> list[dict]:
    """Full chain for a key, newest assertion first (active, superseded, retracted, archived, staging).
    Belief-closed originals (rows superseded by a versioned copy) are hidden unless include_expired."""
    extra = "" if include_expired else " AND NOT (meta ? 'belief_closed_by')"
    return c.execute(
        f"SELECT * FROM fact WHERE user_id=%s AND subject=%s AND predicate=%s{extra} "
        "ORDER BY asserted_at DESC, ingested_at DESC",
        (user_id, canon_subject(subject, user_id), canon_predicate(predicate))).fetchall()


def as_of(c: psycopg.Connection, *, user_id: str, at: datetime, as_believed_at: datetime | None = None,
          subject: str | None = None, predicate: str | None = None, limit: int = 50) -> list[dict]:
    """Point-in-time on the VALID axis (status-aware); optional BELIEF axis via as_believed_at."""
    where = ["user_id=%s", "valid_from <= %s", "(valid_to IS NULL OR valid_to > %s)"]
    args: list = [user_id, at, at]
    if as_believed_at is not None:
        # BELIEF axis: rows we had ingested by B and had not expired by B — whatever their status is NOW
        where += ["status NOT IN ('staging','deleted')", "ingested_at <= %s", "(expired_at IS NULL OR expired_at > %s)"]
        args += [as_believed_at, as_believed_at]
    else:
        # CURRENT belief: only rows still believed (not expired/retracted/archived)
        where += ["status IN ('active','superseded')", "expired_at IS NULL"]
    if subject:
        where.append("subject=%s"); args.append(canon_subject(subject, user_id))
    if predicate:
        where.append("predicate=%s"); args.append(canon_predicate(predicate))
    # newest assertion per key wins when several rows overlap the instant
    sql = (f"SELECT DISTINCT ON (subject, predicate, CASE WHEN cardinality='set' THEN value_norm ELSE '' END) * "
           f"FROM fact WHERE {' AND '.join(where)} "
           f"ORDER BY subject, predicate, CASE WHEN cardinality='set' THEN value_norm ELSE '' END, asserted_at DESC LIMIT %s")
    return c.execute(sql, (*args, limit)).fetchall()


def row_public(r: dict | None) -> dict | None:
    """JSON-safe projection (no embeddings / tsvectors)."""
    if r is None:
        return None
    out = {}
    for k, v in r.items():
        if k in ("embedding", "tsv", "value_norm", "body_embedding", "value_embedding"):
            continue
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        elif k in ("id", "supersedes", "superseded_by", "origin_episode", "episode_id") and v is not None:
            v = str(v)
        out[k] = v
    return out
