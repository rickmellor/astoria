"""Cognify worker — drains `cognify_queue` in-process and runs the curator timers.

Each tick (settings.cognify_poll_s, 30 s): curator.embed_backfill FIRST (fills the NULL embeddings the async
write path leaves behind; batched 8 → TEI), then the cognify drain. Curator groups: hourly
(settings.curator_interval_min: profile re-derive for changed users + working-window turn archive), reflect
(settings.reflect_interval_h), daily (settings.curator_daily_h: dedup_facts + decay + prune_snapshots).

Single-leader: the loop only drains while it holds `pg_try_advisory_lock(43)` on a dedicated
connection (CONTRACT §Service). Jobs are claimed with FOR UPDATE SKIP LOCKED, coalesced per
(user_id, session_id), and each group is extracted+applied+marked-done in ONE transaction — a
failure backs the rows off (1,5,15,60,240 min; dead after max_attempts) and writes no facts.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime

import psycopg
from psycopg.types.json import Jsonb

from astoria.cognify import resolver
from astoria.config import settings
from astoria.core.llm import LLMUnavailable
from astoria.curator import maintenance as curator
from astoria.store import db

log = logging.getLogger("astoria.cognify.worker")

ADVISORY_LOCK_ID = 43
TICK_S = 30.0                 # default only; run_forever reads settings().cognify_poll_s
BACKOFF_MIN = [1, 5, 15, 60, 240]
GROUP_MAX_EPISODES = 8
GROUP_MAX_CHARS = 6000
BODY_MAX_CHARS = 6000
STALE_RUNNING_MIN = 30
EMBED_BACKFILL_LIMIT = 1000    # rows per tick (facts + episodes), batched 8 → TEI


def backoff_minutes(attempts: int) -> int:
    return BACKOFF_MIN[max(0, min(attempts - 1, len(BACKOFF_MIN) - 1))]


# ---------------------------------------------------------------------------
# claim / coalesce

def claim_jobs(c: psycopg.Connection, limit: int = 4) -> list[dict]:
    """Claim up to `limit` ready jobs (pending/failed, due) → state='running', attempts+1.
    Each returned job carries its episode row under "episode" (kind, body, occurred_at, source, status)."""
    # crash recovery: rows stuck in 'running' (next_attempt_at is stamped at claim time)
    c.execute(
        "UPDATE cognify_queue SET state='failed', last_error=left(coalesce(last_error,'')||' [stale running reclaimed]', 2000) "
        "WHERE state='running' AND next_attempt_at < now() - make_interval(mins => %s)", (STALE_RUNNING_MIN,))
    rows = c.execute(
        "WITH j AS (SELECT id FROM cognify_queue WHERE state IN ('pending','failed') AND next_attempt_at <= now() "
        "           ORDER BY priority, occurred_at, id LIMIT %s FOR UPDATE SKIP LOCKED) "
        "UPDATE cognify_queue q SET state='running', attempts=q.attempts+1, next_attempt_at=now() "
        "FROM j WHERE q.id=j.id RETURNING q.*", (limit,)).fetchall()
    if not rows:
        return []
    eps = {str(e["id"]): e for e in c.execute(
        "SELECT id, kind, body, occurred_at, source, status, session_id FROM episode WHERE id = ANY(%s::uuid[])",
        ([str(r["episode_id"]) for r in rows],)).fetchall()}
    jobs = []
    for r in rows:
        j = dict(r)
        j["episode"] = eps.get(str(r["episode_id"]))
        jobs.append(j)
    # keep the claim order (priority, occurred_at)
    jobs.sort(key=lambda j: (j["priority"], j["occurred_at"], j["id"]))
    return jobs


def coalesce(jobs: list[dict]) -> list[list[dict]]:
    """Group by (user_id, session_id) keeping first-seen order; split a group at 8 episodes / 6000 chars."""
    groups: dict[tuple, list[list[dict]]] = {}
    order: list[tuple] = []
    for j in jobs:
        key = (j["user_id"], j.get("session_id"))
        if key not in groups:
            groups[key] = [[]]
            order.append(key)
        buckets = groups[key]
        cur = buckets[-1]
        body_len = len((j.get("episode") or {}).get("body") or "")
        cur_len = sum(len((x.get("episode") or {}).get("body") or "") for x in cur)
        if cur and (len(cur) >= GROUP_MAX_EPISODES or cur_len + body_len > GROUP_MAX_CHARS):
            cur = []
            buckets.append(cur)
        cur.append(j)
    return [b for key in order for b in groups[key] if b]


# ---------------------------------------------------------------------------
# process

def _fmt_ts(d: datetime | None) -> str:
    if d is None:
        return "????-??-?? ??:??"
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC).strftime("%Y-%m-%d %H:%M")


def build_job_text(group: list[dict]) -> str:
    parts = []
    for j in group:
        ep = j.get("episode") or {}
        body = (ep.get("body") or "").strip()
        if len(body) > BODY_MAX_CHARS:
            body = body[:BODY_MAX_CHARS] + " …[truncated]"
        parts.append(f"[{_fmt_ts(ep.get('occurred_at') or j.get('occurred_at'))}] {body}")
    return "\n\n".join(parts)


def _mark_failed(c: psycopg.Connection, ids: list[int], error: str) -> dict:
    rows = c.execute(
        "UPDATE cognify_queue SET "
        "  state = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'failed' END, "
        "  next_attempt_at = now() + make_interval(mins => (ARRAY[1,5,15,60,240])[least(greatest(attempts,1),5)]), "
        "  last_error = left(%s, 2000), "
        "  finished_at = CASE WHEN attempts >= max_attempts THEN now() ELSE NULL END "
        "WHERE id = ANY(%s) RETURNING id, state", (error, ids)).fetchall()
    out = {"failed": 0, "dead": 0}
    for r in rows:
        out[r["state"]] = out.get(r["state"], 0) + 1
    return out


def _mark_done(c: psycopg.Connection, ids: list[int], result: dict) -> None:
    c.execute(
        "UPDATE cognify_queue SET state='done', finished_at=now(), last_error=NULL, payload = payload || %s "
        "WHERE id = ANY(%s)", (Jsonb({"result": result}), ids))


def _mark_skipped(c: psycopg.Connection, ids: list[int], why: str) -> None:
    c.execute("UPDATE cognify_queue SET state='skipped', finished_at=now(), last_error=%s WHERE id = ANY(%s)",
              (why, ids))


def process_group(group: list[dict]) -> dict:
    """Extract + apply one coalesced group in a single transaction; queue rows marked done in that txn.
    Returns {"ids": [...], "state": done|failed|dead|skipped, "result"|"error"}."""
    ids = [j["id"] for j in group]
    user_id = group[0]["user_id"]
    session_id = group[0].get("session_id")
    kind = group[0].get("kind") or "extract"
    try:
        with db.conn() as c:
            # non-extract queue kinds are routed to the curator
            if kind == "rederive_profile":
                res = curator.rederive_profile(c, user_id, llm=bool(settings().profile_llm))
                _mark_done(c, ids, {"rederive_profile": {"version": res["version"], "changed": res["changed"]}})
                return {"ids": ids, "state": "done", "result": res}
            if kind == "embed_backfill":
                res = curator.embed_backfill(c)
                _mark_done(c, ids, {"embed_backfill": res})
                return {"ids": ids, "state": "done", "result": res}

            live = [j for j in group if j.get("episode") and j["episode"].get("status") != "deleted"]
            if not live:
                _mark_skipped(c, ids, "episode missing or deleted")
                return {"ids": ids, "state": "skipped"}
            live.sort(key=lambda j: (j["episode"].get("occurred_at") or j["occurred_at"], j["id"]))
            episode_ids = [str(j["episode_id"]) for j in live]
            occurred_at = max(j["episode"].get("occurred_at") or j["occurred_at"] for j in live)
            source = live[-1]["episode"].get("source") or "api"
            job_text = build_job_text(live)

            candidates, registry = resolver.gather_context(c, user_id=user_id, job_text=job_text)
            try:
                parsed = resolver.extract(job_text, occurred_at, user_id, candidates, registry)
            except LLMUnavailable as e:
                st = _mark_failed(c, ids, f"llm unavailable: {e}")
                return {"ids": ids, "state": "dead" if st.get("dead") else "failed", "error": str(e), "counts": st}
            if parsed is None:
                st = _mark_failed(c, ids, "extraction returned no valid JSON")
                return {"ids": ids, "state": "dead" if st.get("dead") else "failed",
                        "error": "no valid extraction", "counts": st}

            result = resolver.apply(c, user_id=user_id, episode_ids=episode_ids, parsed=parsed, source=source,
                                    session_id=session_id, occurred_at=occurred_at)
            summary = {"facts": len(result["facts"]), "retracted": len(result["retracted"]),
                       "summary_episode": result["summary_episode"], "nothing_durable": parsed.nothing_durable}
            _mark_done(c, ids, summary)
            dead = [j["id"] for j in group if j.get("episode") is None or j["episode"].get("status") == "deleted"]
            if dead:
                _mark_skipped(c, dead, "episode missing or deleted")
            log.info("cognify done user=%s session=%s jobs=%d facts=%d retracted=%d summary=%s",
                     user_id, session_id, len(ids), summary["facts"], summary["retracted"], summary["summary_episode"])
            return {"ids": ids, "state": "done", "result": result}
    except Exception as e:
        log.exception("cognify group failed user=%s session=%s", user_id, session_id)
        try:
            with db.conn() as c:
                st = _mark_failed(c, ids, f"{type(e).__name__}: {e}")
        except Exception:
            log.exception("could not mark jobs failed: %s", ids)
            st = {"failed": len(ids), "dead": 0}
        return {"ids": ids, "state": "dead" if st.get("dead") else "failed", "error": str(e), "counts": st}


def drain_once(limit: int | None = None) -> dict:
    """Claim → coalesce → process. Returns job counts {"processed","failed","dead"} (+"skipped")."""
    lim = limit or settings().cognify_batch or 4
    out = {"processed": 0, "failed": 0, "dead": 0, "skipped": 0}
    with db.conn() as c:
        jobs = claim_jobs(c, lim)
    if not jobs:
        return out
    for group in coalesce(jobs):
        r = process_group(group)
        n = len(r["ids"])
        if r["state"] == "done":
            out["processed"] += n
        elif r["state"] == "skipped":
            out["skipped"] += n
        else:
            counts = r.get("counts") or {}
            out["failed"] += counts.get("failed", n if r["state"] == "failed" else 0)
            out["dead"] += counts.get("dead", n if r["state"] == "dead" else 0)
    return out


# ---------------------------------------------------------------------------
# leader lock + loop

def _acquire_leader_lock() -> psycopg.Connection | None:
    conn = psycopg.connect(settings().db_dsn, autocommit=True, application_name="astoria-worker")
    try:
        got = conn.execute("SELECT pg_try_advisory_lock(%s) AS got", (ADVISORY_LOCK_ID,)).fetchone()[0]
    except Exception:
        conn.close()
        raise
    if got:
        return conn
    conn.close()
    return None


def _lock_alive(conn: psycopg.Connection | None) -> bool:
    if conn is None or conn.closed:
        return False
    try:
        conn.execute("SELECT 1").fetchone()
        return True
    except Exception:  # noqa: BLE001
        with contextlib.suppress(Exception):
            conn.close()
        return False


def _release_leader_lock(conn: psycopg.Connection | None) -> None:
    if conn is None:
        return
    with contextlib.suppress(Exception):
        if not conn.closed:
            conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_ID,))
            conn.close()


def _schedule_intervals() -> dict[str, float]:
    """Seconds between runs of each curator group (from Settings)."""
    st = settings()
    return {
        "hourly": max(60.0, float(st.curator_interval_min) * 60.0),   # profile re-derive check + working-window archive
        "reflect": max(300.0, float(st.reflect_interval_h) * 3600.0),
        "daily": max(3600.0, float(st.curator_daily_h) * 3600.0),       # dedup + decay + snapshot prune
    }


def embed_backfill_tick() -> dict:
    """Every tick, BEFORE the cognify drain: fill NULL embeddings left by the async write path."""
    with db.conn() as c:
        r = curator.embed_backfill(c, limit=EMBED_BACKFILL_LIMIT)
    if r.get("facts") or r.get("episodes"):
        log.info("embed_backfill: facts=%d episodes=%d pending=%d/%d", r["facts"], r["episodes"],
                 r["pending_facts"], r["pending_episodes"])
    return r


def _curator_tick(due: dict) -> None:
    """Run whichever curator groups are due (sync; called via to_thread). One log line per pass."""
    st = settings()
    if due.get("hourly"):
        with db.conn() as c:
            users = curator.users_with_profile_changes(c)
        for u in users:
            try:
                with db.conn() as c:
                    r = curator.rederive_profile(c, u, llm=bool(st.profile_llm))
                log.info("rederive_profile %s: v%s changed=%s source=%s facts=%d",
                         u, r["version"], r["changed"], r.get("source"), r.get("facts", 0))
            except Exception:
                log.exception("rederive_profile failed for %s", u)
        try:
            with db.conn() as c:
                n = curator.archive_old_turns(c)
            if n:
                log.info("archive_old_turns: archived %d turns (window %dh / %d per session)",
                         n, st.working_window_hours, st.working_window_turns)
        except Exception:
            log.exception("archive_old_turns failed")
    if due.get("reflect"):
        try:
            with db.conn() as c:
                users = curator.users_with_unreflected(c)
        except Exception:
            log.exception("reflect: user scan failed")
            users = []
        for u in users:
            try:
                with db.conn() as c:
                    r = curator.reflect(c, u)
                log.info("reflect %s: episodes=%d insights=%d written=%d%s", u, r["episodes"], r["insights"],
                         sum(1 for w in r["written"] if w.get("action") not in (None, "error")),
                         f" error={r['error']}" if r.get("error") else "")
            except Exception:
                log.exception("reflect failed for %s", u)
    if due.get("daily"):
        try:
            with db.conn() as c:
                users = curator.users_with_facts(c)
        except Exception:
            log.exception("daily curator: user scan failed")
            users = []
        for u in users:
            try:
                with db.conn() as c:
                    r = curator.dedup_facts(c, u)
                log.info("dedup_facts %s: candidates=%d merged=%d skipped=%d", u, r["candidates"], r["merged"], r["skipped"])
            except Exception:
                log.exception("dedup_facts failed for %s", u)
            try:
                with db.conn() as c:
                    r = curator.decay(c, u)
                log.info("decay %s: candidates=%d archived=%d kept=%d threshold=%.3f",
                         u, r["candidates"], r["archived"], r["kept"], r["threshold"])
            except Exception:
                log.exception("decay failed for %s", u)
        try:
            with db.conn() as c:
                n1 = curator.prune_snapshots(c)
            log.info("prune_snapshots: pruned %d", n1)
        except Exception:
            log.exception("prune_snapshots failed")


async def run_forever(stop: asyncio.Event, tick_s: float | None = None) -> None:
    """Leader-elected loop. Every tick (settings.cognify_poll_s): embed_backfill, then drain cognify.
    Curator groups: hourly (profile re-derive + working-window archive; settings.curator_interval_min),
    reflect (settings.reflect_interval_h), daily (dedup + decay + snapshot prune; settings.curator_daily_h).
    Immediate run on start. Never raises."""
    tick_s = float(tick_s if tick_s is not None else (settings().cognify_poll_s or TICK_S))
    lock_conn: psycopg.Connection | None = None
    interval = _schedule_intervals()
    last = {k: 0.0 for k in interval}
    log.info("cognify worker loop starting (tick %.0fs; curator hourly=%.0fs reflect=%.0fs daily=%.0fs)",
             tick_s, interval["hourly"], interval["reflect"], interval["daily"])
    try:
        while not stop.is_set():
            try:
                if not await asyncio.to_thread(_lock_alive, lock_conn):
                    lock_conn = await asyncio.to_thread(_acquire_leader_lock)
                    if lock_conn is not None:
                        log.info("worker: acquired leader lock %d", ADVISORY_LOCK_ID)
                if lock_conn is not None:
                    try:
                        await asyncio.to_thread(embed_backfill_tick)
                    except Exception:
                        log.exception("embed_backfill tick failed")
                    r = await asyncio.to_thread(drain_once)
                    if r.get("processed") or r.get("failed") or r.get("dead"):
                        log.info("drain: %s", r)
                    now = time.monotonic()
                    due = {k: (now - last[k] >= interval[k]) for k in interval}
                    if any(due.values()):
                        for k, d in due.items():
                            if d:
                                last[k] = now
                        await asyncio.to_thread(_curator_tick, due)
            except Exception:
                log.exception("worker tick failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=tick_s)
            except TimeoutError:
                pass
    finally:
        await asyncio.to_thread(_release_leader_lock, lock_conn)
        log.info("cognify worker loop stopped")
