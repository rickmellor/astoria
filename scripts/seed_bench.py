#!/usr/bin/env python3
"""Seed a throwaway Astoria user with N facts for the T7 scale test, then (optionally) time recalls.

Two modes:
  --direct-db   INSERT straight into Postgres with random unit vectors (fast; no TEI) — use when the DSN
                points at the database the API serves (local dev, or on the NAS host).
  (default)     POST /facts over HTTP in a thread pool (slower: every insert embeds via TEI).

Idempotent: the fact key is (user_id, subject_i, predicate_i, value_i) with deterministic values, so a
re-run upserts/noops instead of duplicating (direct mode uses ON CONFLICT DO NOTHING on the partial
unique indexes' key columns via a pre-check; HTTP mode relies on the store's idempotent NOOP).

Usage:
  scripts/seed_bench.py --user bench-rick --n 10000 --direct-db --dsn postgresql://astoria:astoria@127.0.0.1:55432/astoria
  scripts/seed_bench.py --user bench-rick --n 10000 --url http://192.168.1.134:8933 --recalls 30
  scripts/seed_bench.py --user bench-rick --wipe --url ...
Also importable: seed_direct(dsn, user_id, n), seed_http(base_url, user_id, n, workers), bench_queries().
"""
from __future__ import annotations

import argparse
import hashlib
import math
import os
import random
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

DEFAULT_URL = os.environ.get("ASTORIA_URL", "http://127.0.0.1:8977")
DEFAULT_DSN = os.environ.get("ASTORIA_DB_DSN", "postgresql://astoria:astoria@127.0.0.1:55432/astoria")
EMBED_DIM = 768

# vocabulary — many predicates (set + functional), varied subjects/values so BM25 + vectors both have work
_SET_PREDS = ["uses_tool", "likes", "dislikes", "owns_hardware", "runs_service", "works_on_project", "goal",
              "decided", "learned_howto", "has_skill", "knows_person", "interested_in", "related_to", "fact"]
_FUNC_PREDS = ["favorite_beer", "favorite_editor", "favorite_language", "preferred_shell", "default_model",
               "default_johnny_profile", "current_focus", "primary_workstation", "timezone", "location"]
_EXTRA_SET = [f"bench_attr_{i:03d}" for i in range(120)]          # auto-registered set predicates
_EXTRA_FUNC = [f"favorite_bench_{i:03d}" for i in range(40)]      # auto-registered functional (prefix rule)
_WORDS = ("postgres pgvector nomic vulkan rocm rdna4 r9700 qwen gemma kimi llama vllm saint johnny nas "
          "ugreen nfs jumbo zwave homeassistant neovim fish zsh python rust typer fastapi httpx pytest "
          "ipa stout guinness porter lager tripel saison pilsner astoria portland seattle oregon "
          "megaplan memoryos input claude opus sonnet haiku fable router cascade embed recall capture "
          "cognify curator snapshot audit tombstone bitemporal supersede functional cardinality").split()


def _rng(user_id: str) -> random.Random:
    return random.Random(int(hashlib.sha256(user_id.encode()).hexdigest()[:8], 16))


def gen_facts(user_id: str, n: int) -> list[dict]:
    """Deterministic list of n distinct (subject, predicate, value) triples for user_id."""
    rng = _rng(user_id)
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    i = 0
    while len(out) < n:
        i += 1
        roll = rng.random()
        if roll < 0.55:
            pred = rng.choice(_SET_PREDS)
        elif roll < 0.85:
            pred = rng.choice(_EXTRA_SET)
        elif roll < 0.95:
            pred = rng.choice(_EXTRA_FUNC)
        else:
            pred = rng.choice(_FUNC_PREDS)
        functional = pred.startswith(("favorite_", "default_", "primary_", "preferred_", "current_")) or pred in ("timezone", "location")
        # functional keys need distinct subjects to stay 1-active each; set keys can share subjects
        subject = user_id if (not functional and rng.random() < 0.3) else f"bench-subj-{rng.randrange(4000):04d}"
        value = " ".join(rng.choice(_WORDS) for _ in range(rng.randint(1, 4))) + f" #{i}"
        key = (subject, pred, "" if functional else value.lower())   # functional: one value per key
        if key in seen:
            continue
        seen.add(key)
        out.append({"subject": subject, "predicate": pred, "value": value,
                    "cardinality": "functional" if functional else "set"})
    return out


def bench_queries() -> list[str]:
    base = ["what beer do I like", "which editor do I use", "postgres pgvector setup on the nas", "rdna4 vulkan rocm",
            "johnny default profile", "what tools does the user use", "home assistant zwave", "favorite language",
            "nfs jumbo frames ugreen", "megaplan memoryos input clients", "saint router cascade", "qwen gemma kimi seats",
            "skills and goals", "people I know", "decisions about the fleet", "current focus", "preferred shell",
            "timezone and location", "stout porter lager", "embedding recall capture cognify",
            "snapshot audit tombstone", "bitemporal supersede", "hardware owned", "services running",
            "projects worked on", "how to run vllm", "neovim fish zsh", "astoria portland oregon",
            "claude opus sonnet haiku", "router embed recall"]
    return base[:30]


def _unit_vec(rng: random.Random) -> str:
    v = [rng.gauss(0, 1) for _ in range(EMBED_DIM)]
    s = math.sqrt(sum(x * x for x in v)) or 1.0
    return "[" + ",".join(f"{x / s:.6f}" for x in v) + "]"


def seed_direct(dsn: str, user_id: str, n: int = 10000, batch: int = 1000) -> int:
    """Bulk INSERT n facts with random unit vectors. Returns rows inserted (0 for keys already present)."""
    import psycopg
    rows = gen_facts(user_id, n)
    rng = _rng(user_id + "|vec")
    inserted = 0
    with psycopg.connect(dsn, autocommit=False) as c:
        preds = {(r["predicate"], r["cardinality"]) for r in rows}
        c.cursor().executemany(
            "INSERT INTO predicate(name, cardinality, layer_hint, auto) VALUES (%s,%s,'semantic',true) "
            "ON CONFLICT (name) DO NOTHING", sorted(preds))
        existing = {(r[0], r[1], r[2]) for r in c.execute(
            "SELECT subject, predicate, value_norm FROM fact WHERE user_id=%s", (user_id,)).fetchall()}
        todo = [r for r in rows if (r["subject"], r["predicate"], r["value"].lower()) not in existing]
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            args = [(user_id, r["subject"], r["predicate"], r["cardinality"], r["value"],
                     f"{r['subject']} {r['predicate'].replace('_', ' ')}: {r['value']}", _unit_vec(rng),
                     "semantic", 0.9, "bench", "explicit", 0.7, rng.random()) for r in chunk]
            c.cursor().executemany(
                "INSERT INTO fact(user_id, subject, predicate, cardinality, value, hook, embedding, layer, "
                "confidence, source, source_kind, source_trust, importance) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s::vector,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", args)
            inserted += len(chunk)
            c.commit()
    return inserted


def seed_http(base_url: str, user_id: str, n: int = 10000, workers: int = 16, client_name: str = "bench") -> int:
    """POST /facts for n triples in a thread pool. Returns the number of non-error responses."""
    import httpx
    rows = gen_facts(user_id, n)
    ok = 0

    def one(r: dict) -> bool:
        with httpx.Client(base_url=base_url, timeout=60.0, headers={"X-Astoria-Client": client_name}) as cl:
            resp = cl.post("/facts", json={"user_id": user_id, "subject": r["subject"], "predicate": r["predicate"],
                                           "value": r["value"], "cardinality": r["cardinality"], "layer": "semantic"})
            return resp.status_code == 200

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for f in as_completed([ex.submit(one, r) for r in rows]):
            ok += 1 if f.result() else 0
    return ok


def bench_recalls(base_url: str, user_id: str, n_recalls: int = 30, client_name: str = "bench") -> dict:
    import httpx
    qs = bench_queries()
    lat: list[float] = []
    with httpx.Client(base_url=base_url, timeout=60.0, headers={"X-Astoria-Client": client_name}) as cl:
        for i in range(n_recalls):
            q = qs[i % len(qs)]
            t = time.monotonic()
            r = cl.post("/recall", json={"user_id": user_id, "query": q, "limit": 12})
            lat.append((time.monotonic() - t) * 1000)
            r.raise_for_status()
    s = sorted(lat)
    return {"recalls": len(lat), "p50_ms": round(statistics.median(lat), 1),
            "p95_ms": round(s[int(round(0.95 * (len(s) - 1)))], 1), "max_ms": round(max(lat), 1)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", default=os.environ.get("ASTORIA_BENCH_USER", "bench-rick"))
    ap.add_argument("--n", type=int, default=10000)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--direct-db", action="store_true", help="insert straight into Postgres (random unit vectors)")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--recalls", type=int, default=0, help="after seeding, time this many /recall calls")
    ap.add_argument("--wipe", action="store_true", help="DELETE /users/{user} and exit")
    a = ap.parse_args(argv)

    if a.wipe:
        import httpx
        r = httpx.delete(f"{a.url.rstrip('/')}/users/{a.user}", timeout=120)
        print(f"wipe {a.user}: {r.status_code} {r.text[:200]}")
        return 0 if r.status_code == 200 else 1

    t0 = time.monotonic()
    if a.direct_db:
        n = seed_direct(a.dsn, a.user, a.n)
        print(f"seeded {n} new facts for {a.user} via direct DB in {time.monotonic() - t0:.1f}s")
    else:
        n = seed_http(a.url, a.user, a.n, a.workers)
        print(f"seeded {n}/{a.n} facts for {a.user} via {a.url} in {time.monotonic() - t0:.1f}s")
    if a.recalls:
        rep = bench_recalls(a.url, a.user, a.recalls)
        print(f"recall latency: {rep}")
        return 0 if rep["p95_ms"] < 800 else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
