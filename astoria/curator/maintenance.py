"""Curator — housekeeping passes run by the worker loop (astoria/cognify/worker.py).

Deterministic (no LLM):
    embed_backfill      fill NULL embeddings (facts + episodes) — the async write path's other half
    archive_old_turns   working-memory turns older than `working_window_hours` OR beyond
                        `working_window_turns` per session → status='archived'
    prune_snapshots     drop recall snapshots older than N days
    dedup_facts         merge near-duplicate ACTIVE set-valued facts of one (subject, predicate)
    decay               soft-archive stale, never-recalled machine-sourced semantic facts

LLM (via llm.chat_json; every pass degrades to "no change" when the LLM is unreachable):
    reflect             higher-order insights (beliefs, source_kind='curator') over recent episodes
    rederive_profile    profile narrative — LLM (source='llm', prompt profile.md) when asked and sane,
                        else the deterministic TEMPLATE (source='template'); versioned + history

Every pass is idempotent, verifies its targets inside the transaction right before writing
(verify-before-commit), takes `dry_run` and returns a small report dict the worker logs as one line.
"""
from __future__ import annotations

import logging
import math
import re
from datetime import UTC, datetime
from importlib import resources

import psycopg
from psycopg.types.json import Jsonb

from astoria.config import settings
from astoria.core.embed import embed_texts
from astoria.core.llm import LLMUnavailable, chat_json
from astoria.store import facts

log = logging.getLogger("astoria.curator")

EMBED_BATCH = 8
CURATOR_ACTOR = "curator"


# ---------------------------------------------------------------------------
# embeddings

def embed_backfill(c: psycopg.Connection, limit: int = 200) -> dict:
    """Embed up to `limit` facts + `limit` episodes whose embedding is NULL. Returns counts."""
    done = {"facts": 0, "episodes": 0, "pending_facts": 0, "pending_episodes": 0}
    facts_rows = c.execute(
        "SELECT id, hook FROM fact WHERE embedding IS NULL AND status <> 'deleted' ORDER BY ingested_at LIMIT %s",
        (limit,)).fetchall()
    for i in range(0, len(facts_rows), EMBED_BATCH):
        chunk = facts_rows[i:i + EMBED_BATCH]
        vecs = embed_texts([r["hook"] for r in chunk])
        for r, v in zip(chunk, vecs):
            if v is not None:
                c.execute("UPDATE fact SET embedding=%s WHERE id=%s AND embedding IS NULL", (v, r["id"]))
                done["facts"] += 1
    ep_rows = c.execute(
        "SELECT id, hook FROM episode WHERE embedding IS NULL AND status <> 'deleted' ORDER BY ingested_at LIMIT %s",
        (limit,)).fetchall()
    for i in range(0, len(ep_rows), EMBED_BATCH):
        chunk = ep_rows[i:i + EMBED_BATCH]
        vecs = embed_texts([r["hook"] for r in chunk])
        for r, v in zip(chunk, vecs):
            if v is not None:
                c.execute("UPDATE episode SET embedding=%s WHERE id=%s AND embedding IS NULL", (v, r["id"]))
                done["episodes"] += 1
    done["pending_facts"] = c.execute(
        "SELECT count(*) AS n FROM fact WHERE embedding IS NULL AND status <> 'deleted'").fetchone()["n"]
    done["pending_episodes"] = c.execute(
        "SELECT count(*) AS n FROM episode WHERE embedding IS NULL AND status <> 'deleted'").fetchone()["n"]
    return done


# ---------------------------------------------------------------------------
# retention

def prune_snapshots(c: psycopg.Connection, days: int = 90) -> int:
    return c.execute("DELETE FROM snapshot WHERE created_at < now() - make_interval(days => %s)", (days,)).rowcount


def archive_old_turns(c: psycopg.Connection, days: int | None = None, *, hours: float | None = None,
                      per_session: int | None = None) -> int:
    """Working-memory window: archive active turns older than `hours` (default settings.working_window_hours;
    `days` is the legacy spelling) OR beyond the newest `per_session` turns of a session (default
    settings.working_window_turns; 0 disables). Returns rows archived."""
    st = settings()
    if hours is None:
        hours = float(days) * 24.0 if days is not None else float(st.working_window_hours)
    if per_session is None:
        per_session = int(st.working_window_turns)
    n = c.execute(
        "UPDATE episode SET status='archived' WHERE kind='turn' AND status='active' "
        "AND occurred_at < now() - make_interval(secs => %s)", (float(hours) * 3600.0,)).rowcount
    if per_session and per_session > 0:
        n += c.execute(
            "WITH ranked AS (SELECT id, row_number() OVER (PARTITION BY user_id, session_id "
            "  ORDER BY occurred_at DESC, ingested_at DESC) AS rn FROM episode "
            "  WHERE kind='turn' AND status='active' AND session_id IS NOT NULL) "
            "UPDATE episode e SET status='archived' FROM ranked r WHERE e.id=r.id AND r.rn > %s",
            (int(per_session),)).rowcount
    return n


# ---------------------------------------------------------------------------
# profile narrative (template, no LLM)

# set predicates with a natural verb phrase; anything else falls back to "<Name> <pred words>: a, b."
_SET_PHRASES = {
    "likes": "{n} likes: {v}.",
    "dislikes": "{n} dislikes: {v}.",
    "interested_in": "{n} is interested in: {v}.",
    "has_skill": "{n} has skills in: {v}.",
    "knows_person": "{n} knows: {v}.",
    "uses_tool": "{n} uses: {v}.",
    "owns_hardware": "{n} owns: {v}.",
    "runs_service": "{n} runs: {v}.",
    "works_on_project": "{n} works on: {v}.",
    "goal": "{n}'s goals: {v}.",
}
_FUNC_PHRASES = {
    "name": "{n}'s name is {v}.",
    "location": "{n} lives in {v}.",
    "timezone": "{n}'s timezone is {v}.",
    "employer": "{n} works at {v}.",
    "role": "{n}'s role is {v}.",
}


def _display_name(user_id: str, by_pred: dict[str, list[str]]) -> str:
    if by_pred.get("name"):
        return by_pred["name"][-1]
    return user_id[:1].upper() + user_id[1:] if user_id else "The user"


def render_profile_narrative(user_id: str, rows: list[dict]) -> str:
    """Deterministic narrative from active profile-layer facts (rows need predicate, cardinality, value)."""
    by_pred: dict[str, list[str]] = {}
    card: dict[str, str] = {}
    for r in rows:
        by_pred.setdefault(r["predicate"], []).append(r["value"])
        card[r["predicate"]] = r["cardinality"]
    if not by_pred:
        return ""
    n = _display_name(user_id, by_pred)
    poss = n + ("'" if n.endswith("s") else "'s")
    sentences: list[str] = []
    for pred in sorted(by_pred):
        vals = by_pred[pred]
        words = pred.replace("_", " ")
        if card.get(pred) == "functional":
            v = vals[-1]
            tpl = _FUNC_PHRASES.get(pred, "{p} {w} is {v}.")
            sentences.append(tpl.format(n=n, v=v, p=poss, w=words))
        else:
            seen, uniq = set(), []
            for v in vals:
                k = re.sub(r"\s+", " ", v.strip().lower())
                if k not in seen:
                    seen.add(k); uniq.append(v.strip())
            v = ", ".join(uniq)
            tpl = _SET_PHRASES.get(pred, "{p} {w}: {v}.")
            sentences.append(tpl.format(n=n, v=v, p=poss, w=words))
    return " ".join(sentences)


def _prompt(name: str) -> str:
    return resources.files("astoria.cognify.prompts").joinpath(name).read_text(encoding="utf-8")


def _mention_ratio(narrative: str, values: list[str]) -> float:
    """Share of fact values the narrative mentions (normalized containment, or every ≥3-char token present)."""
    if not values:
        return 1.0
    text = re.sub(r"[^a-z0-9 ]+", " ", narrative.lower())
    text = " " + re.sub(r"\s+", " ", text).strip() + " "
    hit = 0
    for v in values:
        vn = re.sub(r"[^a-z0-9 ]+", " ", v.lower())
        vn = re.sub(r"\s+", " ", vn).strip()
        if not vn:
            hit += 1
            continue
        if f" {vn} " in text:
            hit += 1
            continue
        toks = [t for t in vn.split() if len(t) >= 3] or vn.split()
        if toks and all(f" {t} " in text for t in toks):
            hit += 1
    return hit / len(values)


def profile_narrative_llm(user_id: str, rows: list[dict]) -> str | None:
    """LLM narrative over profile-layer facts (prompt profile.md). None when the LLM is unavailable, returns
    no narrative, or fails the sanity check (must mention ≥ 80 % of the fact values, ≤ 2500 chars)."""
    if not rows:
        return None
    lines = []
    for r in rows:
        tag = "one current value" if r.get("cardinality") == "functional" else "one of several"
        lines.append(f"- {r['predicate']} = {r['value']}  ({tag})")
    name = _display_name(user_id, {"name": [r["value"] for r in rows if r["predicate"] == "name"]})
    user = (f"USER_ID: {user_id}\nDISPLAY_NAME: {name}\n\nFACTS ({len(rows)}):\n" + "\n".join(lines)
            + "\n\nReturn {\"narrative\": \"...\"}")
    try:
        out = chat_json([{"role": "system", "content": _prompt("profile.md")}, {"role": "user", "content": user}],
                        max_tokens=900)
    except LLMUnavailable as e:
        log.info("profile narrative: LLM unavailable (%s); template fallback", e)
        return None
    text = (out or {}).get("narrative") if isinstance(out, dict) else None
    if not isinstance(text, str):
        return None
    text = re.sub(r"\s+", " ", text).strip()
    if not text or len(text) > 2500:
        return None
    ratio = _mention_ratio(text, [r["value"] for r in rows])
    if ratio < 0.8:
        log.warning("profile narrative for %s rejected: mentions %.0f%% of fact values", user_id, ratio * 100)
        return None
    return text


def rederive_profile(c: psycopg.Connection, user_id: str, *, llm: bool = False, dry_run: bool = False) -> dict:
    """Rebuild the profile narrative from active profile-layer facts; bump version + history only when
    the text changed. Always stamps rederived_at (so users_with_profile_changes settles).

    `llm=True` (the worker passes settings().profile_llm) renders with the LLM (source='llm') and falls
    back to the template when the LLM is unreachable or its output fails the sanity check."""
    rows = c.execute(
        "SELECT predicate, cardinality, value FROM fact WHERE user_id=%s AND subject=%s AND layer='profile' "
        "AND status='active' ORDER BY predicate, asserted_at, ingested_at", (user_id, user_id)).fetchall()
    source = "template"
    narrative = None
    if llm and rows:
        narrative = profile_narrative_llm(user_id, rows)
        if narrative is not None:
            source = "llm"
    if narrative is None:
        narrative = render_profile_narrative(user_id, rows)
    cur = c.execute("SELECT narrative, version FROM profile WHERE user_id=%s FOR UPDATE", (user_id,)).fetchone()
    if cur is None:
        if not dry_run:
            c.execute("INSERT INTO profile(user_id, narrative, version, rederived_at, source) "
                      "VALUES (%s, '', 0, NULL, 'template') ON CONFLICT (user_id) DO NOTHING", (user_id,))
        cur = {"narrative": "", "version": 0}
    changed = narrative != cur["narrative"]
    version = cur["version"]
    if dry_run:
        return {"user_id": user_id, "version": version + (1 if changed else 0), "changed": changed,
                "narrative": narrative, "facts": len(rows), "source": source, "dry_run": True}
    if changed:
        version += 1
        c.execute("UPDATE profile SET narrative=%s, version=%s, rederived_at=now(), source=%s WHERE user_id=%s",
                  (narrative, version, source, user_id))
        c.execute("INSERT INTO profile_history(user_id, version, narrative) VALUES (%s,%s,%s) "
                  "ON CONFLICT (user_id, version) DO UPDATE SET narrative=EXCLUDED.narrative, created_at=now()",
                  (user_id, version, narrative))
    else:
        c.execute("UPDATE profile SET rederived_at=now() WHERE user_id=%s", (user_id,))
    return {"user_id": user_id, "version": version, "changed": changed, "narrative": narrative, "facts": len(rows),
            "source": source}


def users_with_profile_changes(c: psycopg.Connection) -> list[str]:
    """Users whose profile-layer facts were inserted/closed since their last re-derive (or never derived)."""
    rows = c.execute(
        "SELECT DISTINCT f.user_id FROM fact f LEFT JOIN profile p ON p.user_id=f.user_id "
        "WHERE f.layer='profile' AND (p.rederived_at IS NULL OR f.ingested_at > p.rederived_at "
        "OR (f.expired_at IS NOT NULL AND f.expired_at > p.rederived_at)) ORDER BY f.user_id").fetchall()
    return [r["user_id"] for r in rows]


def users_with_facts(c: psycopg.Connection) -> list[str]:
    return [r["user_id"] for r in c.execute("SELECT DISTINCT user_id FROM fact WHERE status='active' ORDER BY 1")]


# ---------------------------------------------------------------------------
# dedup — near-duplicate ACTIVE set-valued facts of one (subject, predicate)

_KIND_RANK = {"explicit": 3, "detector": 3, "extracted": 2, "imported": 1, "curator": 1}


def _vec(v) -> list[float] | None:
    if v is None:
        return None
    if hasattr(v, "tolist"):
        v = v.tolist()
    elif hasattr(v, "to_list"):
        v = v.to_list()
    return list(v)


def _cosine(a, b) -> float:
    a, b = _vec(a), _vec(b)
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _contains(short: str, long: str) -> bool:
    """Word-bounded containment of normalized values (\"neovim\" ⊂ \"neovim editor\"); ≥ 3 chars, no one-token noise."""
    short, long = short.strip(), long.strip()
    if len(short) < 3 or short == long:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(short) + r"(?![a-z0-9])", long) is not None


def _keeper(a: dict, b: dict) -> tuple[dict, dict]:
    """(keep, drop): human-stated beats machine, then richer (longer value), then higher confidence, then newer."""
    def key(r):
        return (_KIND_RANK.get(r["source_kind"], 0), len(r["value"]), float(r["confidence"] or 0),
                r["asserted_at"] or datetime.min.replace(tzinfo=UTC), str(r["id"]))
    return (a, b) if key(a) >= key(b) else (b, a)


def find_duplicate_pairs(c: psycopg.Connection, user_id: str, *, cosine: float | None = None) -> list[dict]:
    """Candidate merges among ACTIVE set facts: same (subject, predicate), different value, cosine ≥ threshold
    on the stored embeddings OR normalized-string containment. Returns [{keep, drop, why, cos}] newest-first."""
    thr = cosine if cosine is not None else float(settings().dedup_cosine)
    rows = c.execute(
        "SELECT id, subject, predicate, value, value_norm, embedding, asserted_at, confidence, source_kind, "
        "importance, access_count, corroborations, origin_episode, source FROM fact "
        "WHERE user_id=%s AND status='active' AND cardinality='set' ORDER BY subject, predicate, asserted_at",
        (user_id,)).fetchall()
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["subject"], r["predicate"]), []).append(dict(r))
    pairs: list[dict] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                if a["value_norm"] == b["value_norm"]:
                    continue
                why, cos = None, 0.0
                if _contains(a["value_norm"], b["value_norm"]) or _contains(b["value_norm"], a["value_norm"]):
                    why = "containment"
                if a.get("embedding") is not None and b.get("embedding") is not None:
                    cos = _cosine(a["embedding"], b["embedding"])
                    if cos >= thr:
                        why = why or "cosine"
                if why:
                    keep, drop = _keeper(a, b)
                    pairs.append({"keep": keep, "drop": drop, "why": why, "cos": round(cos, 4)})
    return pairs


def dedup_facts(c: psycopg.Connection, user_id: str, *, dry_run: bool = False, cosine: float | None = None) -> dict:
    """Merge near-duplicate ACTIVE set facts: keep the human-stated / richer / newer row (folding the other's
    access_count, corroborations, confidence, importance into it), retract the other with reason
    'curator-dedup' (source_kind curator → tombstone blocks='none': not a human tombstone). Idempotent."""
    pairs = find_duplicate_pairs(c, user_id, cosine=cosine)
    report = {"user_id": user_id, "candidates": len(pairs), "merged": 0, "skipped": 0, "pairs": [], "dry_run": dry_run}
    dropped: set[str] = set()
    for pr in pairs:
        keep, drop = pr["keep"], pr["drop"]
        kid, did = str(keep["id"]), str(drop["id"])
        entry = {"keep": kid, "drop": did, "keep_value": keep["value"], "drop_value": drop["value"],
                 "why": pr["why"], "cos": pr["cos"]}
        if kid in dropped or did in dropped:
            report["skipped"] += 1
            continue
        if dry_run:
            report["pairs"].append(entry)
            dropped.add(did)
            continue
        with c.transaction():   # savepoint per merge: one bad pair must not poison the pass
            # verify-before-commit: both rows still active right now (another writer may have moved them)
            live = {str(r["id"]): r for r in c.execute(
                "SELECT id, status, access_count, corroborations, confidence, importance, meta FROM fact "
                "WHERE id = ANY(%s::uuid[]) FOR UPDATE", ([kid, did],)).fetchall()}
            if live.get(kid, {}).get("status") != "active" or live.get(did, {}).get("status") != "active":
                report["skipped"] += 1
                continue
            rows = facts.retract(c, user_id=user_id, fact_id=did, actor=CURATOR_ACTOR, source_kind="curator",
                                 reason="curator-dedup")
            if not rows:
                report["skipped"] += 1
                continue
            k, d = live[kid], live[did]
            meta = dict(k.get("meta") or {})
            merged = list(meta.get("merged_from") or [])
            merged.append({"id": did, "value": drop["value"], "why": pr["why"], "at": datetime.now(UTC).isoformat()})
            meta["merged_from"] = merged[-20:]
            c.execute(
                "UPDATE fact SET access_count=access_count+%s, corroborations=GREATEST(corroborations, %s), "
                "confidence=GREATEST(confidence, %s), importance=GREATEST(importance, %s), last_seen=now(), meta=%s "
                "WHERE id=%s AND status='active'",
                (int(d["access_count"] or 0), int(d["corroborations"] or 0), float(d["confidence"] or 0),
                 float(d["importance"] or 0), Jsonb(meta), kid))
            c.execute("UPDATE fact SET meta = meta || %s WHERE id=%s",
                      (Jsonb({"dedup_merged_into": kid, "retract_reason": "curator-dedup"}), did))
            c.execute("INSERT INTO audit(user_id, actor, op, target, detail) VALUES (%s,%s,%s,%s,%s)",
                      (user_id, CURATOR_ACTOR, "curator-dedup", kid, Jsonb(entry)))
            report["merged"] += 1
            report["pairs"].append(entry)
            dropped.add(did)
    return report


# ---------------------------------------------------------------------------
# decay — soft-archive stale, never-recalled machine-sourced semantic facts

DECAY_KINDS = ("extracted", "curator", "imported")


def _access_factor(n: int) -> float:
    """f(access_count): 1.0 when never recalled, grows with use (log)."""
    return 1.0 + math.log1p(max(0, int(n or 0)))


def decay_score(row: dict, *, now: datetime | None = None) -> float:
    """score = importance × f(access_count) × source_trust × recency; recency = 2^(−age/half_life) with age from
    last_seen (insert or last touch) and half_life = belief_half_life_days for beliefs else recency_half_life_days."""
    st = settings()
    now = now or datetime.now(UTC)
    seen = row.get("last_seen") or row.get("ingested_at") or now
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)
    age_days = max(0.0, (now - seen).total_seconds() / 86400.0)
    half = float(st.belief_half_life_days if row.get("is_belief") else st.recency_half_life_days) or 1.0
    recency = math.pow(2.0, -age_days / half)
    return float(row.get("importance") or 0) * _access_factor(row.get("access_count") or 0) \
        * float(row.get("source_trust") or 0) * recency


def decay(c: psycopg.Connection, user_id: str, *, dry_run: bool = False, threshold: float | None = None,
          min_age_days: int | None = None) -> dict:
    """ACTIVE semantic-layer facts with source_kind in (extracted, curator, imported), ingested more than
    `min_age_days` ago and never recalled (access_count==0) whose decay_score < threshold → status='archived'
    (meta.archived_reason='decay'). Never explicit/detector rows, never profile/procedural layers. Idempotent."""
    st = settings()
    thr = float(threshold if threshold is not None else st.decay_archive_threshold)
    age = int(min_age_days if min_age_days is not None else st.decay_min_age_days)
    rows = c.execute(
        "SELECT id, subject, predicate, value, importance, access_count, source_trust, last_seen, ingested_at, "
        "is_belief, source_kind, layer FROM fact WHERE user_id=%s AND status='active' AND layer='semantic' "
        "AND source_kind = ANY(%s) AND access_count=0 AND ingested_at < now() - make_interval(days => %s) "
        "ORDER BY ingested_at", (user_id, list(DECAY_KINDS), age)).fetchall()
    report = {"user_id": user_id, "candidates": len(rows), "archived": 0, "kept": 0, "threshold": thr,
              "archived_ids": [], "dry_run": dry_run}
    now = datetime.now(UTC)
    for r in rows:
        score = decay_score(r, now=now)
        if score >= thr:
            report["kept"] += 1
            continue
        if dry_run:
            report["archived"] += 1
            report["archived_ids"].append(str(r["id"]))
            continue
        # verify-before-commit: still active, still never recalled, still machine-sourced
        upd = c.execute(
            "UPDATE fact SET status='archived', expired_at=COALESCE(expired_at, now()), meta = meta || %s "
            "WHERE id=%s AND status='active' AND access_count=0 AND layer='semantic' AND source_kind = ANY(%s) "
            "RETURNING id",
            (Jsonb({"archived_reason": "decay", "decay_score": round(score, 4), "archived_at": now.isoformat()}),
             r["id"], list(DECAY_KINDS))).fetchone()
        if not upd:
            report["kept"] += 1
            continue
        c.execute("INSERT INTO audit(user_id, actor, op, target, detail) VALUES (%s,%s,%s,%s,%s)",
                  (user_id, CURATOR_ACTOR, "curator-decay", r["id"],
                   Jsonb({"subject": r["subject"], "predicate": r["predicate"], "value": r["value"],
                          "score": round(score, 4), "threshold": thr})))
        report["archived"] += 1
        report["archived_ids"].append(str(r["id"]))
    return report


# ---------------------------------------------------------------------------
# reflect — higher-order insights over recent summary/note episodes (LLM)

REFLECT_MAX_EPISODES = 40
REFLECT_MAX_CHARS = 12000
REFLECT_MAX_INSIGHTS = 5
REFLECT_MAX_CONF = 0.6
REFLECT_DAYS = 7


def unreflected_episodes(c: psycopg.Connection, user_id: str, *, days: int = REFLECT_DAYS,
                         limit: int = REFLECT_MAX_EPISODES) -> list[dict]:
    return c.execute(
        "SELECT id, kind, body, occurred_at, source FROM episode WHERE user_id=%s AND status='active' "
        "AND kind IN ('summary','note') AND occurred_at > now() - make_interval(days => %s) "
        "AND COALESCE(meta->>'reflected','false') <> 'true' ORDER BY occurred_at DESC LIMIT %s",
        (user_id, int(days), int(limit))).fetchall()


def users_with_unreflected(c: psycopg.Connection, *, days: int = REFLECT_DAYS) -> list[str]:
    return [r["user_id"] for r in c.execute(
        "SELECT DISTINCT user_id FROM episode WHERE status='active' AND kind IN ('summary','note') "
        "AND occurred_at > now() - make_interval(days => %s) AND COALESCE(meta->>'reflected','false') <> 'true' "
        "ORDER BY 1", (int(days),))]


def _fmt_ts(d) -> str:
    if d is None:
        return "????-??-??"
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC).strftime("%Y-%m-%d %H:%M")


def _reflect_messages(user_id: str, eps: list[dict], known: list[dict]) -> list[dict]:
    parts, used = [], 0
    for e in reversed(eps):   # oldest first
        body = re.sub(r"\s+", " ", (e["body"] or "")).strip()
        room = REFLECT_MAX_CHARS - used
        if room <= 0:
            break
        if len(body) > room:
            body = body[:room] + " …[truncated]"
        used += len(body)
        parts.append(f"[{_fmt_ts(e['occurred_at'])} · {e['kind']}] {body}")
    known_lines = [f"- {k['subject']} {k['predicate']}: {k['value']}" for k in known]
    user = (f"USER_ID: {user_id}\n\nKNOWN FACTS (do not restate these):\n" + ("\n".join(known_lines) or "- (none)")
            + "\n\nEPISODES:\n" + "\n\n".join(parts)
            + "\n\nReturn {\"insights\": [...]} with at most 5 items (or an empty list).")
    return [{"role": "system", "content": _prompt("reflect.md")}, {"role": "user", "content": user}]


def reflect(c: psycopg.Connection, user_id: str, *, dry_run: bool = False, days: int = REFLECT_DAYS) -> dict:
    """Ask the LLM for ≤ 5 higher-order insights over the user's recent (last `days`) unreflected summary/note
    episodes; write them as beliefs (source='curator', source_kind='curator', is_belief=True, confidence ≤ 0.6 —
    the staging gate applies) with embedding NULL (embed_backfill fills it) and mark the episodes
    meta.reflected=true. LLM unavailable / unparsable → nothing written, episodes stay unreflected."""
    eps = unreflected_episodes(c, user_id, days=days)
    report = {"user_id": user_id, "episodes": len(eps), "insights": 0, "written": [], "dry_run": dry_run}
    if not eps:
        return report
    known = c.execute(
        "SELECT subject, predicate, value FROM fact WHERE user_id=%s AND status='active' "
        "ORDER BY importance DESC, last_seen DESC LIMIT 60", (user_id,)).fetchall()
    try:
        out = chat_json(_reflect_messages(user_id, eps, known), max_tokens=1200)
    except LLMUnavailable as e:
        report["error"] = f"llm unavailable: {e}"
        return report
    if not isinstance(out, dict) or not isinstance(out.get("insights"), list):
        report["error"] = "no valid JSON"
        return report
    ep_ids = [str(e["id"]) for e in eps]
    origin = ep_ids[0]   # newest
    proposed = []
    for raw in out["insights"]:
        if len(proposed) >= REFLECT_MAX_INSIGHTS:
            break
        if not isinstance(raw, dict):
            continue
        subj = facts.canon_subject(str(raw.get("subject") or user_id), user_id)
        pred = facts.canon_predicate(str(raw.get("predicate") or ""))
        val = re.sub(r"\s+", " ", str(raw.get("value") or "")).strip()
        if not pred or pred == "fact" or not val or len(val) > 300:
            continue
        try:
            conf = float(raw.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        conf = max(0.05, min(REFLECT_MAX_CONF, conf))
        layer = raw.get("layer") if raw.get("layer") in ("semantic", "procedural") else None
        proposed.append({"subject": subj, "predicate": pred, "value": val, "confidence": conf, "layer": layer,
                         "evidence": (str(raw.get("evidence") or "")[:500] or None)})
    report["insights"] = len(proposed)
    if dry_run:
        report["written"] = proposed
        return report
    for ins in proposed:
        try:
            with c.transaction():
                res = facts.upsert_fact(
                    c, user_id=user_id, subject=ins["subject"], predicate=ins["predicate"], value=ins["value"],
                    source=CURATOR_ACTOR, source_kind="curator", confidence=ins["confidence"], is_belief=True,
                    layer=ins["layer"], origin_episode=origin, evidence=ins["evidence"], actor=CURATOR_ACTOR,
                    embed=False, meta={"reflect": True, "episodes": ep_ids[:20]})
            report["written"].append({**ins, "action": res["action"],
                                      "id": str(res["fact"]["id"]) if res.get("fact") else None})
        except Exception as e:  # noqa: BLE001
            log.warning("reflect: insight write failed for %s (%s): %s", user_id, ins, e)
            report["written"].append({**ins, "action": "error", "error": str(e)})
    c.execute("UPDATE episode SET meta = meta || %s WHERE id = ANY(%s::uuid[])",
              (Jsonb({"reflected": True, "reflected_at": datetime.now(UTC).isoformat()}), ep_ids))
    return report
