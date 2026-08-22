"""Target resolver — the LLM decides WHICH facts a natural-language memory instruction means;
execution stays deterministic through the existing store ops.

    "forget the thing about Guinness"   → intent forget,  targets=[favorite_beer row]
    "actually I moved to Oakland"       → intent correct, targets=[location row], new_fact=location Oakland
    "I don't use Emacs anymore"         → intent retract, targets=[uses_tool Emacs row]

    resolve(c, user_id=, text=)  → plan (NOT applied; `requires_confirmation` tells the caller to ask)
    apply(c, user_id=, plan=, …) → runs facts.forget / facts.retract / facts.upsert_fact

The resolver is the smarter sibling of `core.capture.detect` (regex): it is called on demand
(REST /resolve, MCP memory(action="resolve"), CLI) — never on the capture hot path. The LLM only
RESOLVES; every write goes through `facts.*`, which own supersession, tombstones and audit.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime
from importlib import resources
from typing import Any, Literal

import psycopg
from dateutil import parser as dtparser
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from astoria.config import settings
from astoria.core import llm
from astoria.store import facts

log = logging.getLogger("astoria.cognify.targets")

INTENTS = ("forget", "retract", "correct", "remember", "none")
MAX_CANDIDATES = 30
SEARCH_TOPN = 20
MAX_REGISTRY = 60
MAX_TOKENS_LLM = 800
AUTO_CONFIDENCE = 0.85          # ≥ this with exactly one target → no confirmation needed
MAX_TOKEN_PATTERNS = 8
USER_FUNCTIONAL_TOPN = 12      # the user's own functional-key facts always ride along (correction context)

_STOP = {
    "the", "and", "that", "this", "those", "these", "thing", "things", "stuff", "about", "forget",
    "remember", "actually", "please", "delete", "remove", "erase", "retract", "correct", "memory",
    "memories", "fact", "facts", "what", "which", "know", "knew", "you", "your", "yours", "with",
    "from", "into", "anymore", "longer", "now", "not", "don't", "dont", "doesnt", "doesn't", "isn't",
    "isnt", "wrong", "right", "really", "just", "have", "has", "had", "was", "were", "are", "been",
    "like", "likes", "use", "uses", "used", "using", "moved", "live", "lives", "living", "still",
    "instead", "meant", "mean", "update", "change", "changed", "stop", "stopped", "all", "any",
    "every", "everything", "something", "anything", "nothing", "there", "here", "when", "where",
    "should", "would", "could", "can", "cant", "can't", "will", "wont", "won't", "they", "them",
    "their", "its", "it's", "than", "then", "more", "also", "too", "very", "some", "one",
    "user", "rick", "mine", "myself", "keep", "note", "noted", "store", "stored", "save", "saved",
}


# ---------------------------------------------------------------------------
# models

class Target(BaseModel):
    model_config = ConfigDict(extra="ignore")
    fact_id: str = Field(min_length=1)
    reason: str = ""

    @field_validator("fact_id", mode="before")
    @classmethod
    def _id(cls, v):
        if isinstance(v, dict):  # a full fact row was passed back (apply from a previewed plan)
            v = v.get("id") or v.get("fact_id")
        return str(v or "").strip()

    @field_validator("reason", mode="before")
    @classmethod
    def _reason(cls, v):
        return "" if v is None else str(v).strip()


class NewFact(BaseModel):
    model_config = ConfigDict(extra="ignore")
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    value: str = Field(min_length=1)
    valid_from: str | None = None

    @field_validator("subject", "predicate", "value", mode="before")
    @classmethod
    def _str(cls, v):
        return "" if v is None else str(v).strip()

    @field_validator("valid_from", mode="before")
    @classmethod
    def _vf(cls, v):
        if v is None or str(v).strip().lower() in ("", "null", "none", "unknown"):
            return None
        return str(v).strip()


class Plan(BaseModel):
    model_config = ConfigDict(extra="ignore")
    intent: Literal["forget", "retract", "correct", "remember", "none"] = "none"
    targets: list[Target] = Field(default_factory=list)
    new_fact: NewFact | None = None
    confidence: float = 0.0
    explanation: str = ""

    @field_validator("intent", mode="before")
    @classmethod
    def _intent(cls, v):
        t = str(v or "none").strip().lower()
        aliases = {"delete": "forget", "remove": "forget", "erase": "forget", "archive": "forget",
                   "update": "correct", "replace": "correct", "assert": "remember", "add": "remember",
                   "nothing": "none", "noop": "none", "no_op": "none", "recall": "none"}
        return aliases.get(t, t)

    @field_validator("targets", mode="before")
    @classmethod
    def _targets(cls, v):
        if v is None:
            return []
        if isinstance(v, (str, dict)):
            v = [v]
        out = []
        for x in v:
            if isinstance(x, str):
                out.append({"fact_id": x, "reason": ""})
            else:
                out.append(x)
        return out

    @field_validator("new_fact", mode="before")
    @classmethod
    def _nf(cls, v):
        if not v or not isinstance(v, dict) or not any(v.get(k) for k in ("subject", "predicate", "value")):
            return None
        return v

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, f))

    @field_validator("explanation", mode="before")
    @classmethod
    def _expl(cls, v):
        return "" if v is None else str(v).strip()


# ---------------------------------------------------------------------------
# candidates

def salient_tokens(text: str, max_tokens: int = MAX_TOKEN_PATTERNS) -> list[str]:
    """Lower-case content words (≥3 chars, not stopwords), order preserved, deduped."""
    seen: set[str] = set()
    out: list[str] = []
    for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]{2,}", text or ""):
        t = w.lower().strip("'-")
        if len(t) < 3 or t in _STOP or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= max_tokens:
            break
    return out


def _public(row: dict) -> dict:
    return facts.row_public(dict(row)) or {}


def gather_candidates(c: psycopg.Connection, *, user_id: str, text: str,
                      limit: int = MAX_CANDIDATES) -> list[dict]:
    """Active facts that may be what the user means: hybrid search (recall.search_facts_simple, top-20)
    ∪ facts whose hook ILIKE any salient token of the text ∪ the user's own functional-key facts;
    deduped, capped at `limit`; public rows."""
    seen: set[str] = set()
    out: list[dict] = []

    def _add(rows):
        for r in rows:
            k = str(r.get("id"))
            if not k or k in seen or len(out) >= limit:
                continue
            seen.add(k)
            row = dict(r)
            row.pop("score", None)
            out.append(_public(row))

    try:
        from astoria.retrieval import recall as _recall
        _add(_recall.search_facts_simple(c, user_id=user_id, query=text, limit=SEARCH_TOPN))
    except Exception as e:  # noqa: BLE001 — TEI/recall trouble degrades to the literal scan
        log.warning("search_facts_simple failed (%s); literal token scan only", e)

    toks = salient_tokens(text)
    if toks and len(out) < limit:
        pats = [f"%{t}%" for t in toks]
        rows = c.execute(
            "SELECT * FROM fact WHERE user_id=%s AND status='active' AND hook ILIKE ANY(%s) "
            "ORDER BY asserted_at DESC LIMIT %s", (user_id, pats, limit)).fetchall()
        _add(rows)
    # corrections usually name the NEW value, not the stored one ("actually I moved to Oakland"):
    # the user's own single-valued (functional) facts are always cheap, relevant context.
    if len(out) < limit:
        _add(c.execute(
            "SELECT * FROM fact WHERE user_id=%s AND status='active' AND subject=%s AND cardinality='functional' "
            "ORDER BY asserted_at DESC LIMIT %s", (user_id, user_id, USER_FUNCTIONAL_TOPN)).fetchall())
    return out


def _registry(c: psycopg.Connection) -> list[dict]:
    return [dict(r) for r in c.execute(
        "SELECT name, cardinality FROM predicate ORDER BY auto, name LIMIT %s", (MAX_REGISTRY,)).fetchall()]


# ---------------------------------------------------------------------------
# prompt

def system_prompt() -> str:
    return resources.files("astoria.cognify.prompts").joinpath("resolve.md").read_text(encoding="utf-8")


def _fmt_candidate(cf: dict) -> str:
    return (f"- id={cf['id']} subject={cf.get('subject')} predicate={cf.get('predicate')} "
            f"value={json.dumps(str(cf.get('value')))} layer={cf.get('layer')} "
            f"source={cf.get('source_kind')}")


def build_messages(text: str, user_id: str, candidates: list[dict], registry: list[dict]) -> list[dict]:
    reg = ", ".join(f"{r['name']}:{r['cardinality']}" for r in registry) or "(empty)"
    cands = "\n".join(_fmt_candidate(cf) for cf in candidates) or "(none)"
    user = (
        f"USER_ID: {user_id}\n"
        f"NOW: {datetime.now(UTC).date().isoformat()}\n\n"
        f"REGISTRY (predicate:cardinality):\n{reg}\n\n"
        f"CANDIDATE FACTS (only these ids may be targets):\n{cands}\n\n"
        f"INSTRUCTION:\n<<<\n{text}\n>>>\n\n"
        "Reply with the JSON object only."
    )
    return [{"role": "system", "content": system_prompt()}, {"role": "user", "content": user}]


# ---------------------------------------------------------------------------
# validation

def _match_candidate_id(fid: str, cand_ids: list[str]) -> str | None:
    """Exact id, or a unique prefix (≥ 8 chars) the model may have abbreviated."""
    if fid in cand_ids:
        return fid
    f = fid.lower().strip()
    if len(f) >= 8:
        hits = [x for x in cand_ids if x.lower().startswith(f)]
        if len(hits) == 1:
            return hits[0]
    return None


def validate_plan(raw: Any, candidates: list[dict]) -> Plan:
    """Pydantic-validate the model output and enforce: target ids ⊂ candidates; correct/remember carry
    a new_fact; remember/none carry no targets; none carries no new_fact. Raises ValueError/TypeError/ValidationError."""
    if not isinstance(raw, dict):
        raise TypeError("reply must be a JSON object")
    plan = Plan.model_validate(raw)
    if plan.intent in ("remember", "none"):   # targets are meaningless here — drop before id checks
        plan.targets = []
    cand_ids = [str(cf["id"]) for cf in candidates]
    fixed: list[Target] = []
    seen: set[str] = set()
    for t in plan.targets:
        m = _match_candidate_id(t.fact_id, cand_ids)
        if m is None:
            raise ValueError(f"target fact_id {t.fact_id!r} is not one of the candidate ids: {cand_ids}")
        if m not in seen:
            seen.add(m)
            fixed.append(Target(fact_id=m, reason=t.reason))
    plan.targets = fixed
    if plan.intent in ("correct", "remember") and plan.new_fact is None:
        raise ValueError(f"intent {plan.intent!r} requires new_fact {{subject, predicate, value}}")
    if plan.intent in ("forget", "retract", "none"):
        plan.new_fact = None
    return plan


def _llm_plan(text: str, user_id: str, candidates: list[dict], registry: list[dict]) -> tuple[Plan | None, str | None]:
    """One LLM call + one repair retry fed the validation error. → (plan, None) | (None, error)."""
    s = settings()
    messages = build_messages(text, user_id, candidates, registry)
    last_err = "no JSON object in reply"
    for attempt in (0, 1):
        raw = llm.chat_json(messages, model=s.llm_model, max_tokens=MAX_TOKENS_LLM)
        if raw is not None:
            try:
                return validate_plan(raw, candidates), None
            except (ValidationError, ValueError, TypeError) as e:
                last_err = str(e)[:1500]
                log.warning("resolve plan failed validation (attempt %d): %s", attempt + 1, last_err[:300])
        if attempt == 0:
            messages = messages + [
                {"role": "assistant", "content": json.dumps(raw)[:4000] if raw is not None else "(unparsable reply)"},
                {"role": "user", "content":
                    "Your previous reply was not valid. Error:\n" + last_err +
                    "\n\nReply again with ONLY the JSON object matching the schema (no fences, no prose). "
                    "Cite only ids from CANDIDATE FACTS."},
            ]
    return None, last_err


# ---------------------------------------------------------------------------
# resolve (plan only)

def requires_confirmation(intent: str, confidence: float, n_targets: int) -> bool:
    if intent == "remember":
        return False
    if intent == "none":
        return False          # nothing to apply, nothing to confirm
    return not (confidence >= AUTO_CONFIDENCE and n_targets == 1)


def _new_fact_public(nf: NewFact | None, user_id: str) -> dict | None:
    if nf is None:
        return None
    return {"subject": facts.canon_subject(nf.subject, user_id), "predicate": facts.canon_predicate(nf.predicate),
            "value": re.sub(r"\s+", " ", nf.value).strip(), "valid_from": nf.valid_from}


def resolve(c: psycopg.Connection, *, user_id: str, text: str, limit: int = 8) -> dict:
    """Resolve a natural-language memory instruction into a plan WITHOUT applying it.

    Returns {intent, targets:[fact rows (public) + reason], new_fact, confidence, explanation,
    requires_confirmation, text, candidates: n}. `limit` caps the number of targets kept (best first,
    as the model ordered them). On LLM/validation failure: {intent:"none", error:…, error_kind:…}."""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    base = {"intent": "none", "targets": [], "new_fact": None, "confidence": 0.0, "explanation": "",
            "requires_confirmation": False, "text": text, "candidates": 0}
    if not text:
        return {**base, "error": "empty text", "error_kind": "bad_input"}
    candidates = gather_candidates(c, user_id=user_id, text=text)
    registry = _registry(c)
    base["candidates"] = len(candidates)
    try:
        plan, err = _llm_plan(text, user_id, candidates, registry)
    except llm.LLMUnavailable as e:
        log.warning("resolve: LLM unavailable: %s", e)
        return {**base, "error": f"llm unavailable: {e}", "error_kind": "llm_unavailable"}
    if plan is None:
        return {**base, "error": f"could not resolve: {err}", "error_kind": "invalid_plan"}
    by_id = {str(cf["id"]): cf for cf in candidates}
    targets = []
    for t in plan.targets[: max(0, int(limit))]:
        row = dict(by_id[t.fact_id])
        row["reason"] = t.reason
        targets.append(row)
    return {
        "intent": plan.intent,
        "targets": targets,
        "new_fact": _new_fact_public(plan.new_fact, user_id),
        "confidence": round(float(plan.confidence), 3),
        "explanation": plan.explanation,
        "requires_confirmation": requires_confirmation(plan.intent, plan.confidence, len(targets)),
        "text": text,
        "candidates": len(candidates),
    }


# ---------------------------------------------------------------------------
# apply (deterministic)

def _parse_dt(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        d = dtparser.parse(str(v), default=datetime(1970, 1, 1, tzinfo=UTC))
    except (ValueError, OverflowError, TypeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _target_ids(plan: dict) -> list[str]:
    out: list[str] = []
    for t in plan.get("targets") or []:
        fid = t.get("id") or t.get("fact_id") if isinstance(t, dict) else t
        try:
            fid = str(uuid.UUID(str(fid)))
        except (ValueError, TypeError, AttributeError):
            continue
        if fid not in out:
            out.append(fid)
    return out


def apply(c: psycopg.Connection, *, user_id: str, plan: dict, source: str = "api",
          actor: str | None = None) -> dict:
    """Execute a resolved plan through the store ops (inside the caller's txn). Never applies `none`.

    forget   → facts.forget(mode="soft") per target
    retract  → facts.retract(fact_id=…, source_kind="explicit", reason="resolved") per target
    correct  → facts.upsert_fact(new_fact, source_kind="explicit", contradicts=[target ids])
    remember → facts.upsert_fact(new_fact, source_kind="explicit")
    Returns {intent, applied, changed:[{op, fact}], superseded:[ids], fact: new row|None, action}."""
    intent = str((plan or {}).get("intent") or "none").strip().lower()
    out: dict[str, Any] = {"intent": intent, "applied": False, "changed": [], "superseded": [], "fact": None,
                           "action": None}
    if intent not in INTENTS:
        raise ValueError(f"unknown intent {intent!r}")
    if intent == "none":
        out["reason"] = "no memory operation"
        return out
    ids = _target_ids(plan)
    text = str(plan.get("text") or "")
    expl = str(plan.get("explanation") or "")
    meta = {"resolved": {"text": text[:500], "explanation": expl[:300], "confidence": plan.get("confidence")}}

    if intent == "forget":
        for fid in ids:
            row = facts.forget(c, user_id=user_id, fact_id=fid, mode="soft", actor=actor)
            if row:
                out["changed"].append({"op": "forget", "fact": _public(row)})
        out["applied"] = bool(out["changed"])
        out["action"] = "forgotten" if out["changed"] else "noop"
        return out

    if intent == "retract":
        for fid in ids:
            for row in facts.retract(c, user_id=user_id, fact_id=fid, actor=actor,
                                     source_kind="explicit", reason="resolved"):
                out["changed"].append({"op": "retract", "fact": _public(row)})
        out["applied"] = bool(out["changed"])
        out["action"] = "retracted" if out["changed"] else "noop"
        return out

    # correct / remember
    nf = plan.get("new_fact") or {}
    if not (nf.get("subject") and nf.get("predicate") and nf.get("value")):
        raise ValueError(f"intent {intent!r} needs new_fact {{subject, predicate, value}}")
    res = facts.upsert_fact(
        c, user_id=user_id, subject=str(nf["subject"]), predicate=str(nf["predicate"]), value=str(nf["value"]),
        source=source, source_kind="explicit", valid_from=_parse_dt(nf.get("valid_from")), actor=actor,
        contradicts=ids if intent == "correct" else (), evidence=(text[:500] or None), meta=meta)
    row = res.get("fact")
    out["fact"] = _public(row) if row else None
    out["action"] = res.get("action")
    out["superseded"] = [str(x) for x in res.get("superseded") or []]
    out["changed"].append({"op": intent, "fact": out["fact"], "action": out["action"],
                           "superseded": out["superseded"]})
    out["applied"] = res.get("action") not in (None, "blocked")
    return out
