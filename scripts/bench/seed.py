#!/usr/bin/env python3
"""Deep-load seeder for the Astoria performance validation (direct SQL, no TEI).

Writes N facts + M episodes per bench user straight into Postgres with COPY, using random unit
vectors (768-d) so the HNSW/GIN indexes carry realistic weight. Generated columns (value_norm, tsv)
are computed by Postgres. Only user_ids with prefix `bench-` are ever touched; `--wipe` removes them
all and prints before/after row counts.

Shape of the synthetic data (per user, defaults 5000 facts / 2500 episodes):
  * ~60 predicates (30 seeded + 15 functional `preferred_bench_NN` + 15 set `bench_attr_NN`)
  * subjects = the user_id (40%) + 200 entities `bench-ent-NNN`
  * asserted_at / occurred_at spread over the last 3 years
  * ~8% of facts sit in supersede chains (2-6 long, status=superseded → active head)
  * ~2% retracted; ~10% beliefs; mixed source_kind / confidence / importance
  * bench-u00 also gets ONE 50-long supersede chain on (user, current_focus) for /history & /as_of
  * episodes: 70% turns (sessions of 8, meta.user_input/agent_response), 20% summaries, 10% notes

Usage:
  BENCH_DSN=postgresql://... scripts/bench/seed.py --users 20 --facts 5000 --episodes 2500
  scripts/bench/seed.py --sizes              # table / index sizes + row counts
  scripts/bench/seed.py --vacuum             # VACUUM ANALYZE fact, episode
  scripts/bench/seed.py --wipe               # delete every bench-* user (prints before/after)
  --index-mode inplace|rebuild : inplace (default) keeps the HNSW indexes and measures the real
      per-row insert cost; rebuild drops fact_vec/episode_vec, COPYs, then CREATE INDEX CONCURRENTLY
      and reports build time (faster for very large loads).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import random
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import BENCH_DSN, BENCH_PREFIX, unit_vec_text  # noqa: E402

NOW = datetime.now(timezone.utc)
THREE_YEARS_S = 3 * 365 * 86400

SEED_FUNC = ["name", "location", "timezone", "employer", "role", "favorite_beer", "favorite_editor",
             "favorite_language", "preferred_shell", "preferred_response_style", "communication_preference",
             "primary_workstation", "primary_nas", "default_model", "default_johnny_profile"]
SEED_SET = ["likes", "dislikes", "interested_in", "has_skill", "knows_person", "uses_tool", "owns_hardware",
            "runs_service", "works_on_project", "goal", "decided", "fact", "learned_howto", "related_to"]
BENCH_FUNC = [f"preferred_bench_{i:02d}" for i in range(15)]
BENCH_SET = [f"bench_attr_{i:02d}" for i in range(15)]
PROFILE_PREDS = {"name", "location", "timezone", "employer", "role", "favorite_beer", "favorite_editor",
                 "favorite_language", "preferred_shell", "preferred_response_style", "communication_preference",
                 "primary_workstation", "primary_nas", "likes", "dislikes", "interested_in", "has_skill", "knows_person"}
CHAIN_PRED = "current_focus"   # reserved on bench-u00 / subject=user for the 50-long chain

WORDS = ("postgres pgvector nomic vulkan rocm rdna4 r9700 qwen gemma kimi llama vllm saint johnny nas "
         "ugreen nfs jumbo zwave homeassistant neovim fish zsh python rust typer fastapi httpx pytest "
         "ipa stout guinness porter lager tripel saison pilsner astoria portland seattle oregon "
         "megaplan memoryos input claude opus sonnet haiku fable router cascade embed recall capture "
         "cognify curator snapshot audit tombstone bitemporal supersede functional cardinality "
         "threadripper chassis gpu vram kernel container compose systemd backup restore latency "
         "throughput index hnsw gin bm25 fusion budget token profile narrative belief evidence").split()

TURN_Q = ["how do I {a} the {b} on the {c}", "what is the best way to {a} {b}", "remind me how we {a} {b} last time",
          "can you {a} the {b} for {c}", "why does {b} fail when I {a} it", "should I {a} {b} or {c}",
          "set my favorite {b} to {c}", "what did we decide about {b}", "show me the {b} {c} numbers",
          "I moved the {b} to the {c} yesterday"]
TURN_A = ["You {a} the {b} by editing the {c} config and restarting the unit.", "Last time we {a} {b} via {c}; it worked.",
          "The {b} lives on the {c}; use the {a} script.", "Noted: favorite {b} is now {c}.",
          "We decided to keep {b} on {c} because of {a} cost.", "Measured {b}: {c} is faster than {a}."]
VERBS = "restart tune bench migrate deploy wipe seed index recall capture embed route scale pin patch".split()


def _rng(tag: str) -> random.Random:
    return random.Random(int(hashlib.sha256(tag.encode()).hexdigest()[:12], 16))


def _ts(rng: random.Random) -> datetime:
    return NOW - timedelta(seconds=rng.random() * THREE_YEARS_S)


def _sentence(rng: random.Random, tpl: list[str]) -> str:
    return rng.choice(tpl).format(a=rng.choice(VERBS), b=rng.choice(WORDS), c=rng.choice(WORDS))


def bench_users(n: int) -> list[str]:
    return [f"{BENCH_PREFIX}u{i:02d}" for i in range(n)]


# ---------------------------------------------------------------------------
# generators (yield COPY rows)

FACT_COLS = ("id,user_id,subject,predicate,cardinality,value,hook,embedding,layer,valid_from,valid_to,asserted_at,"
             "ingested_at,expired_at,status,supersedes,superseded_by,confidence,source,source_kind,source_trust,"
             "is_belief,importance,last_seen,access_count,corroborations,tags,origin_episode,evidence,ref,meta")
EPI_COLS = ("id,user_id,kind,hook,body,embedding,occurred_at,ingested_at,source,session_id,importance,access_count,"
            "last_seen,status,processed_at,idem_key,tags,meta")


def _fact_row(rng, uid, subj, pred, card, value, status, asserted, valid_to=None, expired=None, sup=None, sup_by=None,
              is_belief=False):
    layer = "profile" if (subj == uid and pred in PROFILE_PREDS) else ("procedural" if pred == "learned_howto" else "semantic")
    conf = round(rng.uniform(0.5, 0.95), 3)
    kind = rng.choice(["explicit", "explicit", "extracted", "extracted", "detector", "imported"])
    trust = round(min(conf, rng.uniform(0.6, 0.9)), 3)
    return (uuid.uuid4(), uid, subj, pred, card, value, f"{subj} {pred.replace('_', ' ')}: {value}", unit_vec_text(rng),
            layer, asserted, valid_to, asserted, asserted + timedelta(seconds=rng.randint(0, 3600)), expired, status,
            sup, sup_by, conf, "bench", kind, trust, is_belief, round(rng.random(), 3),
            asserted + timedelta(days=rng.randint(0, 30)), rng.randint(0, 5), rng.randint(0, 2), [], None, None, None, "{}")


GROUP_END = object()   # sentinel: a supersede chain ended here — safe to flush a COPY batch


def gen_facts(uid: str, n: int, chain_len_special: int | None = None):
    """Yield n fact rows for uid (as tuples matching FACT_COLS), with GROUP_END markers after every
    complete supersede chain (superseded_by is a DEFERRED FK: a chain must land in one txn)."""
    rng = _rng(uid + "|facts")
    entities = [f"bench-ent-{k:03d}" for k in range(200)]
    func_preds = SEED_FUNC + BENCH_FUNC
    set_preds = SEED_SET + BENCH_SET
    func_used: set[tuple[str, str]] = set()
    set_used: set[tuple[str, str, str]] = set()
    emitted = 0

    # the 50-long chain first (bench-u00 only): (user, current_focus)
    if chain_len_special:
        func_used.add((uid, CHAIN_PRED))
        t0 = NOW - timedelta(days=chain_len_special * 7)
        ids = [uuid.uuid4() for _ in range(chain_len_special)]
        for i in range(chain_len_special):
            at = t0 + timedelta(days=7 * i)
            last = i == chain_len_special - 1
            row = list(_fact_row(rng, uid, uid, CHAIN_PRED, "functional", f"focus step {i:02d} {rng.choice(WORDS)}",
                                 "active" if last else "superseded", at,
                                 valid_to=None if last else t0 + timedelta(days=7 * (i + 1)),
                                 expired=None if last else t0 + timedelta(days=7 * (i + 1)),
                                 sup=ids[i - 1] if i else None, sup_by=None if last else ids[i + 1]))
            row[0] = ids[i]
            yield tuple(row)
            emitted += 1
        yield GROUP_END

    while emitted < n:
        functional = rng.random() < 0.35
        subj = uid if rng.random() < 0.40 else rng.choice(entities)
        if functional:
            pred = rng.choice(func_preds)
            if (subj, pred) in func_used or (subj == uid and pred == CHAIN_PRED):
                continue
            func_used.add((subj, pred))
            roll = rng.random()
            if roll < 0.08 * 2.2:   # ~8% of ROWS in chains ⇒ ~18% of functional keys get a chain
                L = rng.randint(2, 6)
                t0 = _ts(rng)
                ids = [uuid.uuid4() for _ in range(L)]
                for i in range(L):
                    at = t0 + timedelta(days=rng.randint(1, 120) * (i + 1))
                    last = i == L - 1
                    nxt = at + timedelta(days=rng.randint(1, 120))
                    row = list(_fact_row(rng, uid, subj, pred, "functional",
                                         " ".join(rng.choice(WORDS) for _ in range(rng.randint(1, 3))) + f" v{i}",
                                         "active" if last else "superseded", at,
                                         valid_to=None if last else nxt, expired=None if last else nxt,
                                         sup=ids[i - 1] if i else None, sup_by=None if last else ids[i + 1]))
                    row[0] = ids[i]
                    yield tuple(row)
                    emitted += 1
                yield GROUP_END
                continue
            status = "retracted" if rng.random() < 0.02 else "active"
            at = _ts(rng)
            yield _fact_row(rng, uid, subj, pred, "functional",
                            " ".join(rng.choice(WORDS) for _ in range(rng.randint(1, 3))), status, at,
                            expired=(at + timedelta(days=3)) if status == "retracted" else None,
                            is_belief=rng.random() < 0.1)
            emitted += 1
        else:
            pred = rng.choice(set_preds)
            value = " ".join(rng.choice(WORDS) for _ in range(rng.randint(1, 4))) + f" #{rng.randint(0, 99999)}"
            key = (subj, pred, value.lower())
            if key in set_used:
                continue
            set_used.add(key)
            status = "retracted" if rng.random() < 0.02 else "active"
            at = _ts(rng)
            yield _fact_row(rng, uid, subj, pred, "set", value, status, at,
                            expired=(at + timedelta(days=3)) if status == "retracted" else None,
                            is_belief=rng.random() < 0.1)
            emitted += 1


def gen_episodes(uid: str, n: int):
    rng = _rng(uid + "|episodes")
    emitted = 0
    while emitted < n:
        sess = f"{uid}-s{rng.randint(0, 10**6):06d}"
        base = _ts(rng)
        for i in range(8):
            if emitted >= n:
                return
            roll = rng.random()
            occ = base + timedelta(minutes=3 * i)
            if roll < 0.70:
                kind = "turn"
                q, a = _sentence(rng, TURN_Q), _sentence(rng, TURN_A)
                body = f"User: {q}\n\nAssistant: {a}"
                hook = q[:400]
                meta = '{"user_input": %s, "agent_response": %s}' % (_json_str(q), _json_str(a))
            elif roll < 0.90:
                kind = "summary"
                body = f"Session summary: {_sentence(rng, TURN_A)} {_sentence(rng, TURN_A)}"
                hook, meta = body[:400], "{}"
            else:
                kind = "note"
                body = f"Note: {_sentence(rng, TURN_A)}"
                hook, meta = body[:400], "{}"
            key = hashlib.sha256(f"{uid}|{sess}|{kind}|{body}".encode()).hexdigest()
            yield (uuid.uuid4(), uid, kind, hook, body, unit_vec_text(rng), occ, occ + timedelta(seconds=2), "bench",
                   sess if kind == "turn" else None, round(rng.random(), 3), rng.randint(0, 3), None, "active",
                   occ + timedelta(minutes=1), key, [], meta)
            emitted += 1


def _json_str(s: str) -> str:
    import json
    return json.dumps(s)


# ---------------------------------------------------------------------------
# loading

def ensure_predicates(c) -> None:
    rows = [(p, "functional", "semantic") for p in BENCH_FUNC] + [(p, "set", "semantic") for p in BENCH_SET]
    c.cursor().executemany("INSERT INTO predicate(name, cardinality, layer_hint, auto) VALUES (%s,%s,%s,true) "
                           "ON CONFLICT (name) DO NOTHING", rows)
    c.commit()


def copy_rows(c, table: str, cols: str, rows, batch: int = 2000) -> tuple[int, float]:
    """COPY rows in batches (one txn per batch). Returns (rows, seconds)."""
    n, t0 = 0, time.perf_counter()
    buf: list[tuple] = []

    def flush():
        nonlocal n
        if not buf:
            return
        with c.cursor() as cur, cur.copy(f"COPY {table} ({cols}) FROM STDIN") as cp:
            for r in buf:
                cp.write_row(r)
        c.commit()
        n += len(buf)
        buf.clear()

    pending_group = False   # inside a supersede chain → don't flush until its GROUP_END
    for r in rows:
        if r is GROUP_END:
            pending_group = False
        else:
            buf.append(r)
            # a row with superseded_by set starts/continues a chain (FACT_COLS index 16)
            if table == "fact" and r[16] is not None:
                pending_group = True
        if len(buf) >= batch and not pending_group:
            flush()
            el = time.perf_counter() - t0
            print(f"    {table}: {n} rows, {n / el:.0f} rows/s", end="\r", flush=True)
    flush()
    el = time.perf_counter() - t0
    print(f"    {table}: {n} rows in {el:.1f}s ({n / el:.0f} rows/s)          ")
    return n, el


def sizes(c) -> dict:
    out: dict = {"rows": {}, "tables": {}, "indexes": {}}
    for t in ("fact", "episode", "snapshot", "audit", "cognify_queue"):
        out["rows"][t] = c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        out["tables"][t] = {"total": c.execute("SELECT pg_size_pretty(pg_total_relation_size(%s))", (t,)).fetchone()[0],
                            "heap": c.execute("SELECT pg_size_pretty(pg_relation_size(%s))", (t,)).fetchone()[0],
                            "toast": c.execute("SELECT pg_size_pretty(coalesce(pg_total_relation_size(reltoastrelid),0)) FROM pg_class WHERE relname=%s", (t,)).fetchone()[0]}
    out["bytes"] = {t: c.execute("SELECT pg_total_relation_size(%s)", (t,)).fetchone()[0] for t in ("fact", "episode")}
    out["bytes"]["fact_heap"] = c.execute("SELECT pg_relation_size('fact')").fetchone()[0]
    out["bytes"]["episode_heap"] = c.execute("SELECT pg_relation_size('episode')").fetchone()[0]
    for r in c.execute("SELECT indexrelname, pg_size_pretty(pg_relation_size(indexrelid)), pg_relation_size(indexrelid) "
                       "FROM pg_stat_user_indexes WHERE relname IN ('fact','episode') ORDER BY 3 DESC").fetchall():
        out["indexes"][r[0]] = r[1]
        out["bytes"][r[0]] = r[2]
    out["db_total"] = c.execute("SELECT pg_size_pretty(pg_database_size(current_database()))").fetchone()[0]
    out["bytes"]["db_total"] = c.execute("SELECT pg_database_size(current_database())").fetchone()[0]
    out["bench_rows"] = {"fact": c.execute("SELECT count(*) FROM fact WHERE user_id LIKE 'bench-%'").fetchone()[0],
                         "episode": c.execute("SELECT count(*) FROM episode WHERE user_id LIKE 'bench-%'").fetchone()[0]}
    return out


def print_sizes(s: dict) -> None:
    print(f"db total: {s['db_total']}   bench rows: {s['bench_rows']}")
    print("| table | rows | total | heap | toast |\n|---|---|---|---|---|")
    for t, v in s["tables"].items():
        print(f"| {t} | {s['rows'][t]} | {v['total']} | {v['heap']} | {v['toast']} |")
    print("| index | size |\n|---|---|")
    for k, v in s["indexes"].items():
        print(f"| {k} | {v} |")


WIPE_TABLES = ("snapshot", "cognify_queue", "tombstone", "profile_history", "profile", "audit", "edge", "entity", "alias",
               "fact", "episode")


def seed_edges(c, user_id: str, n: int) -> dict:
    """n random ACTIVE edges for one bench user so recall's graph expansion has something to walk:
    fact→entity (the fact's subject links to another entity, relation related_to) and entity→entity
    (part_of). Subjects of that user's active facts are the entity nodes. Returns counts + seconds."""
    if not user_id.startswith(BENCH_PREFIX):
        raise SystemExit(f"refusing to touch non-bench user {user_id!r}")
    rng = _rng(user_id + "|edges")
    rows = c.execute("SELECT id::text, subject FROM fact WHERE user_id=%s AND status='active'", (user_id,)).fetchall()
    ents = sorted({r[1] for r in rows if r[1] != user_id})
    if len(rows) < 2 or len(ents) < 2:
        return {"edges": 0, "reason": "not enough facts/entities"}
    t0 = time.perf_counter()
    vals = []
    for i in range(n):
        if rng.random() < 0.7:
            f = rng.choice(rows); e = rng.choice(ents)
            vals.append((user_id, "fact", f[0], "entity", e, "related_to", round(rng.uniform(0.3, 1.0), 2)))
        else:
            a, b = rng.sample(ents, 2)
            vals.append((user_id, "entity", a, "entity", b, "part_of", round(rng.uniform(0.3, 1.0), 2)))
    with c.cursor() as cur:
        cur.executemany("INSERT INTO edge(user_id, src_kind, src_id, dst_kind, dst_id, relation, weight, source, source_kind, confidence) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,'bench','imported',0.6) ON CONFLICT DO NOTHING", vals)
    c.commit()
    n_db = c.execute("SELECT count(*) FROM edge WHERE user_id=%s AND status='active'", (user_id,)).fetchone()[0]
    return {"edges": n_db, "entities": len(ents), "facts": len(rows), "seconds": round(time.perf_counter() - t0, 1)}


def counts(c) -> dict:
    return {t: c.execute(f"SELECT count(*) FROM {t} WHERE user_id LIKE %s", (BENCH_PREFIX + "%",)).fetchone()[0]
            for t in WIPE_TABLES}


def wipe(c) -> dict:
    before = counts(c)
    t0 = time.perf_counter()
    with c.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = 0")
        for t in WIPE_TABLES:
            cur.execute(f"DELETE FROM {t} WHERE user_id LIKE %s", (BENCH_PREFIX + "%",))
        cur.execute("DELETE FROM predicate WHERE name LIKE 'preferred_bench_%' OR name LIKE 'bench_attr_%'")
    c.commit()
    after = counts(c)
    return {"before": before, "after": after, "seconds": round(time.perf_counter() - t0, 1)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", default=BENCH_DSN)
    ap.add_argument("--users", type=int, default=0, help="number of bench-uNN users to LOAD (0 = no load; "
                    "use with --sizes/--vacuum/--wipe for maintenance only)")
    ap.add_argument("--first-user", type=int, default=0, help="start at bench-uNN (resume)")
    ap.add_argument("--facts", type=int, default=5000)
    ap.add_argument("--episodes", type=int, default=2500)
    ap.add_argument("--batch", type=int, default=2000)
    ap.add_argument("--index-mode", choices=["inplace", "rebuild"], default="inplace")
    ap.add_argument("--sizes", action="store_true")
    ap.add_argument("--vacuum", action="store_true")
    ap.add_argument("--wipe", action="store_true")
    ap.add_argument("--edges", type=int, default=0, help="insert N random active edges for --edges-user (graph-expansion cost probe)")
    ap.add_argument("--edges-user", default=f"{BENCH_PREFIX}real")
    ap.add_argument("--out", default=os.environ.get("BENCH_OUT"), help="append JSON records (load/sizes/wipe) to this file")
    a = ap.parse_args(argv)

    def emit(rec: dict) -> None:
        import json
        rec["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if a.out:
            with open(a.out, "a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
    if not a.dsn:
        print("BENCH_DSN (or --dsn) required", file=sys.stderr)
        return 2
    import psycopg
    c = psycopg.connect(a.dsn, autocommit=False)

    if a.wipe:
        r = wipe(c)
        print("wipe:", r)
        c.rollback(); c.autocommit = True
        c.execute("VACUUM ANALYZE fact"); c.execute("VACUUM ANALYZE episode")
        sz = sizes(c); print_sizes(sz)
        emit({"phase": "wipe", **r, "sizes": sz})
        return 0
    if a.edges:
        r = seed_edges(c, a.edges_user, a.edges)
        print("edges:", r); emit({"phase": "edges", "user": a.edges_user, **r})
        if not a.users:
            return 0
    if not a.users:
        if a.vacuum:
            c.rollback(); c.autocommit = True
            t0 = time.perf_counter(); c.execute("VACUUM ANALYZE fact"); c.execute("VACUUM ANALYZE episode")
            print(f"VACUUM ANALYZE fact, episode: {time.perf_counter() - t0:.1f}s")
        if a.sizes:
            sz = sizes(c); print_sizes(sz); emit({"phase": "sizes", "sizes": sz})
        if not (a.vacuum or a.sizes):
            ap.print_usage()
        return 0

    ensure_predicates(c)
    if a.index_mode == "rebuild":
        c.rollback(); c.autocommit = True
        c.execute("DROP INDEX IF EXISTS fact_vec"); c.execute("DROP INDEX IF EXISTS episode_vec")
        c.autocommit = False
        print("dropped fact_vec / episode_vec (rebuild mode)")
    tot_f = tot_e = 0
    tf = te = 0.0
    for i, uid in enumerate(bench_users(a.users)):
        if i < a.first_user:
            continue
        print(f"[{uid}]")
        n, s = copy_rows(c, "fact", FACT_COLS, gen_facts(uid, a.facts, 50 if i == 0 else None), a.batch); tot_f += n; tf += s
        n, s = copy_rows(c, "episode", EPI_COLS, gen_episodes(uid, a.episodes), a.batch); tot_e += n; te += s
    print(f"TOTAL facts {tot_f} in {tf:.0f}s ({tot_f / max(tf, 1e-9):.0f}/s)  episodes {tot_e} in {te:.0f}s ({tot_e / max(te, 1e-9):.0f}/s)")
    rec = {"phase": "load", "users": a.users, "first_user": a.first_user, "facts": tot_f, "facts_s": round(tf, 1),
           "facts_rps": round(tot_f / max(tf, 1e-9)), "episodes": tot_e, "episodes_s": round(te, 1),
           "episodes_rps": round(tot_e / max(te, 1e-9)), "index_mode": a.index_mode}
    if a.index_mode == "rebuild":
        c.rollback(); c.autocommit = True
        for name, tbl in (("fact_vec", "fact"), ("episode_vec", "episode")):
            t0 = time.perf_counter()
            c.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {tbl} USING hnsw(embedding vector_cosine_ops)")
            print(f"CREATE INDEX CONCURRENTLY {name}: {time.perf_counter() - t0:.1f}s  "
                  f"(maintenance_work_mem={c.execute('SHOW maintenance_work_mem').fetchone()[0]})")
        c.autocommit = False
    if a.vacuum:
        c.rollback(); c.autocommit = True
        t0 = time.perf_counter(); c.execute("VACUUM ANALYZE fact"); c.execute("VACUUM ANALYZE episode")
        rec["vacuum_s"] = round(time.perf_counter() - t0, 1)
        print(f"VACUUM ANALYZE: {rec['vacuum_s']}s")
    if a.sizes:
        rec["sizes"] = sizes(c); print_sizes(rec["sizes"])
    emit(rec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
