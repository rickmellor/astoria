"""Cognify resolver v1 — the LLM-at-write step (CONTRACT.md §Cognify).

    gather_context → build_messages → extract (LLM, one repair retry) → apply (deterministic)

The LLM only PROPOSES (subject, predicate, value, contradicts, dates); every write goes through
`facts.upsert_fact` / `facts.retract`, which own supersession, tombstones and trust. `apply`
runs inside the caller's transaction so a failure never leaves a half-applied job.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import uuid
from datetime import UTC, datetime
from importlib import resources
from typing import Any, Literal

import psycopg
from dateutil import parser as dtparser
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from astoria.config import settings
from astoria.core import llm
from astoria.core.embed import embed_one, embed_texts
from astoria.store import facts

log = logging.getLogger("astoria.cognify.resolver")

CONF_MIN, CONF_MAX = 0.3, 0.85
NEAR_DUP_COSINE = 0.93
SUMMARY_IMPORTANCE = 0.6
TURN_IMPORTANCE_AFTER_SUMMARY = 0.3
MAX_CANDIDATES = 30
MAX_REGISTRY = 60


# ---------------------------------------------------------------------------
# models

class ExtractedFact(BaseModel):
    model_config = ConfigDict(extra="ignore")

    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    value: str = ""
    layer: Literal["semantic", "profile", "procedural"] = "semantic"
    is_belief: bool = False
    confidence: float = 0.6
    valid_from: str | None = None
    valid_to: str | None = None
    action: Literal["assert", "retract"] = "assert"
    contradicts: list[str] = Field(default_factory=list)
    evidence: str | None = None

    @field_validator("subject", "predicate", "value", "evidence", mode="before")
    @classmethod
    def _str(cls, v):
        if v is None:
            return v
        return str(v).strip() if not isinstance(v, str) else v.strip()

    @field_validator("layer", mode="before")
    @classmethod
    def _layer(cls, v):
        v = (str(v or "semantic")).strip().lower()
        return v if v in ("semantic", "profile", "procedural") else "semantic"

    @field_validator("action", mode="before")
    @classmethod
    def _action(cls, v):
        v = (str(v or "assert")).strip().lower()
        return "retract" if v in ("retract", "remove", "delete", "negate") else "assert"

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.6

    @field_validator("contradicts", mode="before")
    @classmethod
    def _contra(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return [str(x) for x in v if x is not None]

    @field_validator("valid_from", "valid_to", mode="before")
    @classmethod
    def _dates(cls, v):
        if v is None or v == "" or str(v).lower() in ("null", "none", "unknown"):
            return None
        return str(v)


class Extraction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: str | None = None
    nothing_durable: bool = False
    facts: list[ExtractedFact] = Field(default_factory=list)

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("facts", mode="before")
    @classmethod
    def _facts(cls, v):
        return [] if v is None else v


# ---------------------------------------------------------------------------
# prompt

def system_prompt() -> str:
    return resources.files("astoria.cognify.prompts").joinpath("extract.md").read_text(encoding="utf-8")


def _fmt_candidate(cf: dict) -> str:
    vf = cf.get("valid_from")
    vf = vf.date().isoformat() if hasattr(vf, "date") else (str(vf)[:10] if vf else "—")
    return (f"- id={cf['id']} subject={cf['subject']} predicate={cf['predicate']} "
            f"value={json.dumps(str(cf['value']))} valid_from={vf}")


def build_messages(job_text: str, occurred_at: datetime | None, user_id: str,
                   candidates: list[dict], registry: list[dict]) -> list[dict]:
    occ = occurred_at or datetime.now(UTC)
    reg = ", ".join(f"{r['name']}:{r['cardinality']}" for r in registry) or "(empty)"
    cands = "\n".join(_fmt_candidate(cf) for cf in candidates) or "(none)"
    user = (
        f"USER_ID: {user_id}\n"
        f"OCCURRED_AT: {occ.isoformat()}\n\n"
        f"REGISTRY (predicate:cardinality):\n{reg}\n\n"
        f"CANDIDATE FACTS (currently believed; cite ids in \"contradicts\"):\n{cands}\n\n"
        f"TEXT:\n<<<\n{job_text}\n>>>\n\n"
        "Reply with the JSON object only."
    )
    return [{"role": "system", "content": system_prompt()}, {"role": "user", "content": user}]


# ---------------------------------------------------------------------------
# context

def gather_context(c: psycopg.Connection, *, user_id: str, job_text: str,
                   limit: int = MAX_CANDIDATES) -> tuple[list[dict], list[dict]]:
    """→ (candidates, registry). Candidates = top-20 active facts by cosine to the job text ∪ active
    facts whose (non-user) subject appears literally in the text; capped at `limit`."""
    cols = "id, subject, predicate, value, valid_from"
    seen: set[str] = set()
    out: list[dict] = []

    def _add(rows):
        for r in rows:
            k = str(r["id"])
            if k in seen or len(out) >= limit:
                continue
            seen.add(k)
            out.append({"id": k, "subject": r["subject"], "predicate": r["predicate"],
                        "value": r["value"], "valid_from": r["valid_from"]})

    vec = embed_one(job_text, query=True) if job_text else None
    if vec is not None:
        _add(c.execute(
            f"SELECT {cols} FROM fact WHERE user_id=%s AND status='active' AND embedding IS NOT NULL "
            f"ORDER BY embedding <=> %s::vector LIMIT 20", (user_id, vec)).fetchall())
    _add(c.execute(
        f"SELECT {cols} FROM fact WHERE user_id=%s AND status='active' AND subject<>%s "
        f"AND %s ILIKE '%%' || subject || '%%' ORDER BY asserted_at DESC LIMIT %s",
        (user_id, user_id, job_text or "", limit)).fetchall())
    registry = [dict(r) for r in c.execute(
        "SELECT name, cardinality FROM predicate ORDER BY auto, name LIMIT %s", (MAX_REGISTRY,)).fetchall()]
    return out, registry


# ---------------------------------------------------------------------------
# extract

def _validate(raw: Any) -> Extraction:
    if isinstance(raw, list):  # model returned a bare facts array
        raw = {"summary": None, "nothing_durable": not raw, "facts": raw}
    if not isinstance(raw, dict):
        raise ValidationError.from_exception_data("Extraction", [
            {"type": "dict_type", "loc": (), "input": raw}])
    return Extraction.model_validate(raw)


def extract(job_text: str, occurred_at: datetime | None, user_id: str, candidates: list[dict],
            registry: list[dict]) -> Extraction | None:
    """One LLM call (+ one repair retry fed the validation error). None when the model's output is
    unusable. Raises llm.LLMUnavailable when no LLM route works (caller backs off)."""
    s = settings()
    messages = build_messages(job_text, occurred_at, user_id, candidates, registry)
    last_err = "no JSON object in reply"
    for attempt in (0, 1):
        raw = llm.chat_json(messages, model=s.llm_model, max_tokens=2048)
        if raw is not None:
            try:
                return _validate(raw)
            except ValidationError as e:
                last_err = str(e)[:1500]
                log.warning("extraction failed validation (attempt %d): %s", attempt + 1, last_err[:300])
        if attempt == 0:
            messages = messages + [
                {"role": "assistant", "content": json.dumps(raw)[:4000] if raw is not None else "(unparsable reply)"},
                {"role": "user", "content":
                    "Your previous reply was not valid. Error:\n" + last_err +
                    "\n\nReply again with ONLY the JSON object matching the schema (no fences, no prose)."},
            ]
    log.warning("extraction gave up for user=%s: %s", user_id, last_err[:200])
    return None


# ---------------------------------------------------------------------------
# apply

def _parse_dt(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        d = dtparser.parse(str(v), default=datetime(1970, 1, 1, tzinfo=UTC))
    except (ValueError, OverflowError, TypeError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d


def _clamp_conf(v: float) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        v = 0.6
    if math.isnan(v):
        v = 0.6
    return max(CONF_MIN, min(CONF_MAX, v))


def _valid_uuids(ids) -> list[str]:
    out = []
    for x in ids or ():
        try:
            out.append(str(uuid.UUID(str(x))))
        except (ValueError, AttributeError, TypeError):
            continue
    return out


def _cosine(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _cardinality(c: psycopg.Connection, predicate: str) -> str:
    r = c.execute("SELECT cardinality FROM predicate WHERE name=%s", (predicate,)).fetchone()
    return r["cardinality"] if r else facts.guess_cardinality(predicate)


def _near_duplicate_value(c: psycopg.Connection, *, user_id: str, subject: str, predicate: str,
                          value: str) -> str:
    """Functional-key guard: if the active value is the same thing spelled differently
    (normalized-equal or cosine ≥ 0.93 between the VALUES), return the active value so upsert NOOPs
    and corroborates instead of flip-flopping. Otherwise return `value` unchanged."""
    active = c.execute(
        "SELECT value, value_norm FROM fact WHERE user_id=%s AND subject=%s AND predicate=%s AND status='active' "
        "ORDER BY asserted_at DESC LIMIT 1", (user_id, subject, predicate)).fetchone()
    if not active:
        return value
    if active["value_norm"] == facts.norm_value(value):
        return active["value"]
    vecs = embed_texts([value, active["value"]])
    if vecs[0] is not None and vecs[1] is not None and _cosine(vecs[0], vecs[1]) >= NEAR_DUP_COSINE:
        log.info("near-duplicate value for %s/%s/%s: %r ≈ %r", user_id, subject, predicate, value, active["value"])
        return active["value"]
    return value


def apply(c: psycopg.Connection, *, user_id: str, episode_ids: list, parsed: Extraction, source: str,
          session_id: str | None, occurred_at: datetime | None = None) -> dict:
    """Write a validated Extraction (inside the caller's txn). Returns
    {"facts": [{id, action, subject, predicate, value}], "retracted": [...], "summary_episode": id|None}."""
    occ = occurred_at or facts.now_utc()
    ep_ids = [str(e) for e in (episode_ids or [])]
    origin = ep_ids[-1] if ep_ids else None
    out_facts: list[dict] = []
    retracted: list[dict] = []

    for f in parsed.facts or []:
        subject = facts.canon_subject(f.subject, user_id)
        predicate = facts.canon_predicate(f.predicate)
        value = re.sub(r"\s+", " ", (f.value or "").strip())
        card = _cardinality(c, predicate)
        try:
            if f.action == "retract":
                if not value and card == "set":  # never blanket-retract a whole set on the LLM's word
                    log.info("skip retract without value for set predicate %s/%s", subject, predicate)
                    continue
                rows = facts.retract(c, user_id=user_id, subject=subject, predicate=predicate,
                                     value=value or None, actor=source, source_kind="extracted",
                                     reason="extracted-retract")
                for r in rows:
                    retracted.append({"id": str(r["id"]), "action": "retracted", "subject": r["subject"],
                                      "predicate": r["predicate"], "value": r["value"]})
                continue

            if not value:
                continue
            if card == "functional":
                value = _near_duplicate_value(c, user_id=user_id, subject=subject, predicate=predicate, value=value)
            valid_from = _parse_dt(f.valid_from)
            valid_to = _parse_dt(f.valid_to)
            if valid_from and valid_to and valid_to < valid_from:
                valid_to = None
            res = facts.upsert_fact(
                c, user_id=user_id, subject=subject, predicate=predicate, value=value, source=source,
                source_kind="extracted", confidence=_clamp_conf(f.confidence), valid_from=valid_from,
                valid_to=valid_to, asserted_at=occ, layer=f.layer, is_belief=bool(f.is_belief),
                origin_episode=origin, evidence=(f.evidence or None) and f.evidence[:500],
                actor=source, contradicts=_valid_uuids(f.contradicts),
                meta={"cognify": {"episodes": ep_ids, "session_id": session_id}})
            row = res.get("fact")
            out_facts.append({"id": str(row["id"]) if row else None, "action": res["action"],
                              "subject": subject, "predicate": predicate, "value": value,
                              "superseded": res.get("superseded", [])})
        except ValueError as e:  # empty value etc. — skip the one fact, keep the job
            log.warning("skip fact %s/%s/%r: %s", subject, predicate, value, e)

    summary_id = None
    if ep_ids:
        kinds = {str(r["id"]): r["kind"] for r in c.execute(
            "SELECT id, kind FROM episode WHERE id = ANY(%s::uuid[])", (ep_ids,)).fetchall()}
        turn_ids = [e for e in ep_ids if kinds.get(e) == "turn"]
        if parsed.summary and turn_ids:
            summary = re.sub(r"\s+", " ", parsed.summary).strip()
            idem = hashlib.sha256(f"{user_id}|{session_id or ''}|summary|{summary}".encode()).hexdigest()
            row = c.execute(
                "INSERT INTO episode(user_id, kind, hook, body, embedding, occurred_at, source, session_id, importance, "
                "idem_key, meta) VALUES (%s,'summary',%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (idem_key) DO UPDATE SET last_seen=now(), access_count=episode.access_count+1 "
                "RETURNING id",
                (user_id, summary[:400], summary, embed_one(summary), occ, source, session_id,
                 SUMMARY_IMPORTANCE, idem,
                 Jsonb({"cognify": {"episodes": ep_ids, "facts": len(out_facts), "retracted": len(retracted)}}))
            ).fetchone()
            summary_id = str(row["id"])
            c.execute("UPDATE episode SET importance=%s WHERE id = ANY(%s::uuid[]) AND importance > %s",
                      (TURN_IMPORTANCE_AFTER_SUMMARY, turn_ids, TURN_IMPORTANCE_AFTER_SUMMARY))
        c.execute("UPDATE episode SET processed_at=now() WHERE id = ANY(%s::uuid[])", (ep_ids,))

    return {"facts": out_facts, "retracted": retracted, "summary_episode": summary_id}
