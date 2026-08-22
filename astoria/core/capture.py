"""Capture path — episode first (durable, replay-safe), then the LLM-free detector, then queue cognify.

    gate()    — cheap drop rules for noise (slash commands, one-word acks, tiny texts)
    detect()  — regex v1 detector for explicit memory statements (/remember, "my favorite X is Y", ...)
    capture() — the write transaction every client goes through (REST /capture, MCP capture/add_memory)

Nothing here calls an LLM; the detector only applies what a human literally said, at detector trust
(confidence .80). Everything else waits for cognify.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import datetime

import psycopg

from astoria.store import episodes, facts

log = logging.getLogger("astoria.capture")

ACK_WORDS = {"ok", "okay", "done", "y", "n", "yes", "no", "thanks", "thank you", "continue", "k", "sure"}
MIN_CHARS = 8

_SLASH_CMD = re.compile(r"^/\w+")
_CORRECTION_HINT = re.compile(
    r"\b(?:actually|correction|i meant|instead|no longer)\b|(?:\bnot\b|n'?t\b).*\banymore\b", re.IGNORECASE)

# --- detector patterns (case-insensitive, whole message) ------------------------------------
_FLAGS = re.IGNORECASE | re.DOTALL
_P_REMEMBER = re.compile(r"^/remember\s+(\S+)\s+(\S+)\s+(.+)$", _FLAGS)
_P_CORRECT = re.compile(r"^/correct\s+(\S+)\s+(\S+)\s+(.+)$", _FLAGS)
_P_FORGET = re.compile(r"^/forget\s+(\S+)\s+(\S+)(?:\s+(.+))?$", _FLAGS)
_P_FAVORITE = re.compile(
    r"^(?:actually,?\s*)?my (favorite|favourite|default|preferred|primary|current) ([\w ]{1,30}?) is (?:now )?(.+?)[.!]?$",
    _FLAGS)
_P_LOCATION = re.compile(r"^(?:actually,?\s*)?i (?:now )?(?:live|am based) in (.+?)[.!]?$", _FLAGS)
_P_NAME = re.compile(r"^(?:actually,?\s*)?my name is (.+?)[.!]?$", _FLAGS)
_P_DISLIKE = re.compile(r"^(?:actually,?\s*)?i (?:don'?t|do not|no longer) (like|use|prefer) (.+?)[.!]?$", _FLAGS)
_P_LIKE = re.compile(r"^(?:actually,?\s*)?i (?:really )?(?:like|love|enjoy) (.+?)[.!]?$", _FLAGS)


def _clean_value(v: str | None) -> str:
    v = re.sub(r"\s+", " ", (v or "")).strip()
    v = v.strip("\"'`“”‘’")
    v = re.sub(r"[.!?,;:]+$", "", v).strip()
    return v.strip("\"'`“”‘’").strip()


def gate(text: str | None) -> str | None:
    """Return a reason to drop the text (not worth an episode), or None to keep it."""
    t = (text or "").strip()
    if not t:
        return "empty"
    if _SLASH_CMD.match(t):
        return "slash_command"
    if re.sub(r"[.!?]+$", "", t.lower()).strip() in ACK_WORDS:
        return "ack"
    if len(t) < MIN_CHARS:
        return "too_short"
    return None


def is_correction_hint(text: str | None) -> bool:
    return bool(text) and bool(_CORRECTION_HINT.search(text))


def detect(text: str | None, user_id: str) -> dict | None:
    """Regex v1 detector. Returns {"op": correct|retract|remember, "subject", "predicate", "value", ...} or None."""
    t = re.sub(r"\s+", " ", (text or "")).strip()
    if not t:
        return None
    corrected = bool(re.match(r"^actually\b", t, re.IGNORECASE))

    m = _P_REMEMBER.match(t)
    if m:
        return {"op": "remember", "subject": facts.canon_subject(m.group(1), user_id),
                "predicate": facts.canon_predicate(m.group(2)), "value": _clean_value(m.group(3))}
    m = _P_CORRECT.match(t)
    if m:
        return {"op": "correct", "subject": facts.canon_subject(m.group(1), user_id),
                "predicate": facts.canon_predicate(m.group(2)), "value": _clean_value(m.group(3))}
    m = _P_FORGET.match(t)
    if m:
        return {"op": "retract", "subject": facts.canon_subject(m.group(1), user_id),
                "predicate": facts.canon_predicate(m.group(2)),
                "value": _clean_value(m.group(3)) or None}
    m = _P_FAVORITE.match(t)
    if m:
        kind = m.group(1).lower().replace("favourite", "favorite")
        pred = facts.canon_predicate(f"{kind}_{m.group(2)}")
        return {"op": "correct" if corrected else "remember", "subject": user_id, "predicate": pred,
                "value": _clean_value(m.group(3)), "cardinality": "functional"}
    m = _P_LOCATION.match(t)
    if m:
        return {"op": "correct" if corrected else "remember", "subject": user_id, "predicate": "location",
                "value": _clean_value(m.group(1)), "cardinality": "functional"}
    m = _P_NAME.match(t)
    if m:
        return {"op": "correct" if corrected else "remember", "subject": user_id, "predicate": "name",
                "value": _clean_value(m.group(1)), "cardinality": "functional"}
    m = _P_DISLIKE.match(t)
    if m:
        verb = m.group(1).lower()
        first, other = ("uses_tool", "likes") if verb == "use" else ("likes", "uses_tool")
        return {"op": "retract", "subject": user_id, "predicate": first, "value": _clean_value(m.group(2)),
                "also_try": [other]}
    m = _P_LIKE.match(t)
    if m:
        return {"op": "remember", "subject": user_id, "predicate": "likes", "value": _clean_value(m.group(1)),
                "cardinality": "set"}
    return None


def _apply_detector(c: psycopg.Connection, det: dict, *, user_id: str, source: str, actor: str | None,
                    episode_id: str | None, evidence: str | None) -> dict:
    out = {k: det.get(k) for k in ("op", "subject", "predicate", "value")}
    out.update(fact_id=None, action=None)
    try:
        with c.transaction():  # savepoint: a detector failure must not poison the episode write
            if det["op"] == "retract":
                rows = []
                for pred in [det["predicate"], *det.get("also_try", [])]:
                    rows = facts.retract(c, user_id=user_id, subject=det["subject"], predicate=pred,
                                         value=det.get("value"), actor=actor, source_kind="detector")
                    if rows:
                        out["predicate"] = pred
                        break
                out["fact_id"] = str(rows[0]["id"]) if rows else None
                out["action"] = "retracted" if rows else "noop"
                out["retracted"] = [str(r["id"]) for r in rows]
            else:
                if not det.get("value"):
                    raise ValueError("empty value")
                res = facts.upsert_fact(
                    c, user_id=user_id, subject=det["subject"], predicate=det["predicate"], value=det["value"],
                    source=source, source_kind="detector", confidence=facts.KIND_CONF["detector"],
                    origin_episode=episode_id, evidence=(evidence or "")[:500] or None,
                    cardinality=det.get("cardinality"), actor=actor)
                out["fact_id"] = str(res["fact"]["id"]) if res.get("fact") else None
                out["action"] = res["action"]
                out["superseded"] = res.get("superseded", [])
    except Exception as e:  # noqa: BLE001
        log.warning("detector apply failed user=%s det=%s: %s", user_id, det, e)
        out["action"] = "error"
        out["error"] = str(e)
    return out


def capture(c: psycopg.Connection, *, user_id: str, kind: str = "turn", text: str | None = None,
            user_input: str | None = None, agent_response: str | None = None, source: str = "api",
            session_id: str | None = None, occurred_at: datetime | None = None, importance: float = 0.5,
            tags: Iterable[str] = (), meta: dict | None = None, cognify: bool = True,
            priority: str = "normal", actor: str | None = None) -> dict:
    """Gate → episode (idempotent) → detector → cognify queue. Call inside `db.conn()`.

    Returns {"episode_id", "deduped", "dropped", "detector", "queued"}.
    """
    user_text = user_input if (kind == "turn" and user_input is not None) else (text if text is not None else user_input)
    det = detect(user_text, user_id)
    # detector-matched slash commands (/remember, /correct, /forget) are memory ops, not noise — keep them
    reason = None if det else gate(user_text)
    if reason:
        return {"episode_id": None, "deduped": False, "dropped": reason, "detector": None, "queued": False}

    ep = episodes.add_episode(
        c, user_id=user_id, kind=kind, text=text, user_input=user_input, agent_response=agent_response,
        source=source, session_id=session_id, occurred_at=occurred_at, importance=importance, tags=tags,
        meta=meta, embed=True)
    row, deduped = ep["episode"], ep["deduped"]
    episode_id = str(row["id"])

    detector = None
    if det:
        detector = _apply_detector(c, det, user_id=user_id, source=source, actor=actor,
                                   episode_id=episode_id, evidence=user_text)

    queued = False
    if cognify and not deduped:
        prio = 1 if (priority == "high" or is_correction_hint(user_text)) else 5
        episodes.enqueue_cognify(c, user_id=user_id, episode_id=episode_id, session_id=session_id, priority=prio,
                                 occurred_at=row.get("occurred_at"),
                                 payload={"kind": kind, "source": source, "detector": bool(det)})
        queued = True

    return {"episode_id": episode_id, "deduped": deduped, "dropped": None, "detector": detector, "queued": queued}
