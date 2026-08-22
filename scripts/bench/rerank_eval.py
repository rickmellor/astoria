#!/usr/bin/env python3
"""Rerank quality eval — recall for user `rick` on the LIVE service, cross-encoder stage ON vs OFF.

~15 query → expected-predicate cases (what a good top-5 looks like for Rick's real store). For each case
and each side we call POST /recall (`rerank: true|false`) and score the returned items:

  precision@5  = |relevant items in top-5| / 5          (relevant = predicate ∈ want, or hook contains want_text)
  MRR          = 1 / rank of the first relevant item   (0 when none in the returned list)
  hit@k        = a relevant item appears in the top-k  (k per case, default 5)
  avoid        = count of `avoid` predicates inside the top-5 (lower is better; 0 = clean)
  latency      = wall-clock per /recall call, p50/p95 over every call of that side

Modes:
  rest (default) — through the REST API. Needs the service to pass the `rerank` flag through
                   (health.rerank == "off" on a rerank:false call). If it does not, the eval says so and
                   (unless --no-exec-fallback) switches to exec mode for BOTH sides.
  exec           — runs recall() in-process INSIDE the astoria container (`docker exec -i astoria python -`
                   over ssh, the db_probe.py pattern) for both sides: identical code path, the container's
                   own env (reranker URL, DB); latency then excludes HTTP/ssh (the script times recall()).

Usage:
  .venv/bin/python scripts/bench/rerank_eval.py [--url http://192.168.1.134:8933] [--user rick]
        [--repeat 2] [--mode rest|exec] [--json out.json] [--show]
Read-only apart from recall's own side effects (snapshot rows + access_count bumps).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time

import httpx

ASTORIA_URL = os.environ.get("ASTORIA_URL", "http://192.168.1.134:8933").rstrip("/")
EXEC_CMD = os.environ.get("BENCH_DB_EXEC", "ssh -o BatchMode=yes root-dxp4800gt docker exec -i astoria python -")

# query, want predicates, optional want_text (hook substring, case-insensitive, counts as relevant too),
# avoid predicates (should not appear in the top-5), k for hit@k
CASES: list[dict] = [
    {"q": "tell me about my family", "want": ["family", "spouse", "has_pet", "wedding_anniversary", "dating_anniversary"],
     "avoid": ["owns_equipment", "owns_hardware", "uses_tool", "runs_service", "learned_howto"], "k": 8},
    {"q": "who is my wife", "want": ["spouse"], "avoid": ["owns_equipment", "owns_hardware"], "k": 3},
    {"q": "do I have any pets", "want": ["has_pet"], "avoid": ["owns_equipment", "owns_hardware"], "k": 3},
    {"q": "what are my kids' names", "want": ["family"], "avoid": ["owns_equipment", "owns_hardware"], "k": 3},
    {"q": "what multimeters do I have", "want": ["owns_equipment"], "want_text": "multimeter", "avoid": ["family", "spouse"], "k": 5},
    {"q": "what oscilloscopes do I own", "want": ["owns_equipment"], "want_text": "oscilloscope", "avoid": ["family", "spouse"], "k": 5},
    {"q": "what is my current job", "want": ["job", "role"], "avoid": ["owns_equipment", "has_pet", "family"], "k": 3},
    {"q": "where did I work in 2010", "want": ["career_history"], "want_text": "logitech", "avoid": ["owns_equipment", "has_pet"], "k": 5},
    {"q": "what cars do I own", "want": ["owns_hardware"], "want_text": "ferrari", "avoid": ["owns_equipment", "family"], "k": 5},
    {"q": "where do I live", "want": ["location"], "avoid": ["owns_equipment", "uses_tool"], "k": 3},
    {"q": "what 3d printers do I use", "want": ["uses_tool"], "want_text": "printer", "avoid": ["family", "spouse", "has_pet"], "k": 5},
    {"q": "what are my hobbies", "want": ["hobby", "interested_in"], "avoid": ["owns_equipment", "learned_howto", "decided"], "k": 5},
    {"q": "which aircraft do I like to fly in DCS", "want": ["favorite_aircraft", "has_skill", "hobby"], "avoid": ["owns_equipment", "family"], "k": 5},
    {"q": "what editor and programming language do I prefer", "want": ["favorite_editor", "favorite_language"], "avoid": ["owns_equipment", "family"], "k": 5},
    {"q": "what GPUs are in my workstation", "want": ["owns_hardware", "primary_workstation"], "want_text": "r9700", "avoid": ["family", "has_pet"], "k": 5},
    {"q": "when is my wedding anniversary", "want": ["wedding_anniversary", "spouse"], "avoid": ["owns_equipment", "owns_hardware"], "k": 3},
    {"q": "what models do I run locally", "want": ["runs_service", "default_model"], "avoid": ["family", "has_pet", "spouse"], "k": 5},
]


# ---------------------------------------------------------------------------
# scoring

def _relevant(item: dict, case: dict) -> bool:
    """fact with a wanted predicate; when the case names want_text the hook must contain it too."""
    if item.get("kind", "fact") != "fact" or item.get("predicate") not in case["want"]:
        return False
    wt = case.get("want_text")
    return (not wt) or (wt.lower() in (item.get("text") or "").lower())


def score_case(items: list[dict], case: dict) -> dict:
    rel = [_relevant(it, case) for it in items]
    top5 = rel[:5]
    p5 = sum(top5) / 5.0
    rr = 0.0
    for i, r in enumerate(rel, start=1):
        if r:
            rr = 1.0 / i
            break
    k = int(case.get("k", 5))
    hit = any(rel[:k])
    avoid = sum(1 for it in items[:5] if it.get("predicate") in case.get("avoid", ()))
    return {"p5": p5, "rr": rr, "hit": hit, "avoid": avoid, "n": len(items)}


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


# ---------------------------------------------------------------------------
# rest mode

def rest_recall(client: httpx.Client, user: str, q: str, rerank: bool, limit: int) -> tuple[dict, float]:
    t0 = time.perf_counter()
    r = client.post("/recall", json={"user_id": user, "query": q, "limit": limit, "max_tokens": 4000, "rerank": rerank})
    ms = (time.perf_counter() - t0) * 1000
    r.raise_for_status()
    return r.json(), ms


def run_rest(url: str, user: str, repeat: int, limit: int) -> dict | None:
    out = {"on": [], "off": []}
    with httpx.Client(base_url=url, timeout=60, headers={"X-Astoria-Client": "bench"}) as client:
        probe, _ = rest_recall(client, user, "probe", False, 3)
        h = (probe.get("health") or {}).get("rerank")
        if h != "off":
            print(f"!! REST does not honor rerank:false (health.rerank={h!r}) — the API must pass the `rerank` "
                  f"flag to recall.recall(); falling back to exec mode", file=sys.stderr)
            return None
        probe_on, _ = rest_recall(client, user, "probe", True, 3)
        print(f"rest: health.rerank on={probe_on.get('health', {}).get('rerank')!r} off={h!r}")
        for rep in range(repeat):
            for case in CASES:
                for side, flag in (("off", False), ("on", True)):
                    res, ms = rest_recall(client, user, case["q"], flag, limit)
                    out[side].append({"case": case["q"], "rep": rep, "ms": ms, "items": res["items"],
                                      "health": res.get("health", {}).get("rerank")})
    return out


# ---------------------------------------------------------------------------
# exec mode (in-container recall(), both sides)

_EXEC_SCRIPT = r'''
import json, sys, time
from astoria.store import db
from astoria.retrieval import recall as R
cfg = json.loads(sys.stdin.readline())
out = {"on": [], "off": []}
for rep in range(cfg["repeat"]):
    for case in cfg["cases"]:
        for side, flag in (("off", False), ("on", True)):
            t0 = time.perf_counter()
            with db.conn() as c:
                res = R.recall(c, user_id=cfg["user"], query=case["q"], limit=cfg["limit"], max_tokens=4000,
                               client="bench", rerank=flag)
            ms = (time.perf_counter() - t0) * 1000
            out[side].append({"case": case["q"], "rep": rep, "ms": ms, "items": res["items"],
                              "health": res["health"].get("rerank")})
print("@@RESULT@@" + json.dumps(out))
'''


def run_exec(user: str, repeat: int, limit: int) -> dict:
    payload = json.dumps({"user": user, "repeat": repeat, "limit": limit, "cases": CASES})
    # the script itself travels on stdin (`python -`), so the config is inlined as a Python string literal
    script = _EXEC_SCRIPT.replace("cfg = json.loads(sys.stdin.readline())", "cfg = json.loads(" + repr(payload) + ")")
    p = subprocess.run(EXEC_CMD.split(), input=script, capture_output=True, text=True, timeout=1800)
    if p.returncode != 0:
        print(p.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"exec mode failed (rc={p.returncode})")
    line = [l for l in p.stdout.splitlines() if l.startswith("@@RESULT@@")]
    if not line:
        print(p.stdout[-2000:], p.stderr[-2000:], file=sys.stderr)
        raise SystemExit("exec mode: no result line")
    return json.loads(line[-1][len("@@RESULT@@"):])


# ---------------------------------------------------------------------------
# report

def summarize(runs: dict, show: bool) -> dict:
    by_case: dict[str, dict] = {}
    agg: dict[str, dict] = {}
    for side in ("off", "on"):
        rows = runs[side]
        scores = []
        for r in rows:
            case = next(c for c in CASES if c["q"] == r["case"])
            sc = score_case(r["items"], case)
            scores.append(sc)
            by_case.setdefault(r["case"], {}).setdefault(side, []).append(sc)
        lat = [r["ms"] for r in rows]
        cold = [r["ms"] for r in rows if r["rep"] == 0]
        agg[side] = {
            "n_calls": len(rows),
            "cold_p50_ms": pct(cold, .5), "cold_p95_ms": pct(cold, .95),
            "p5": statistics.fmean(s["p5"] for s in scores) if scores else 0,
            "mrr": statistics.fmean(s["rr"] for s in scores) if scores else 0,
            "hit": statistics.fmean(1.0 if s["hit"] else 0.0 for s in scores) if scores else 0,
            "avoid": sum(s["avoid"] for s in scores) / max(1, len(scores)),
            "p50_ms": pct(lat, .5), "p95_ms": pct(lat, .95), "mean_ms": statistics.fmean(lat) if lat else 0,
            "health": sorted({str(r.get("health")) for r in rows}),
        }
    print("\n| side | calls | precision@5 | MRR | hit@k | avoid/case | p50 ms | p95 ms | mean ms | rep0 p50 | rep0 p95 | health.rerank |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for side in ("off", "on"):
        a = agg[side]
        print(f"| rerank {side} | {a['n_calls']} | {a['p5']:.3f} | {a['mrr']:.3f} | {a['hit']:.3f} | {a['avoid']:.2f} | "
              f"{a['p50_ms']:.0f} | {a['p95_ms']:.0f} | {a['mean_ms']:.0f} | {a['cold_p50_ms']:.0f} | {a['cold_p95_ms']:.0f} | {','.join(a['health'])} |")
    print("(rep0 = first pass, cold rerank cache; later reps hit the (query, hook) LRU in rerank.py)")
    print("\n| case | p@5 off→on | MRR off→on | hit@k off→on | avoid off→on |")
    print("|---|---|---|---|---|")
    for q, sides in by_case.items():
        def m(side, key):
            xs = sides.get(side, [])
            if not xs:
                return float("nan")
            return statistics.fmean((1.0 if x[key] else 0.0) if key == "hit" else x[key] for x in xs)
        print(f"| {q} | {m('off','p5'):.2f}→{m('on','p5'):.2f} | {m('off','rr'):.2f}→{m('on','rr'):.2f} | "
              f"{m('off','hit'):.0f}→{m('on','hit'):.0f} | {m('off','avoid'):.1f}→{m('on','avoid'):.1f} |")
    if show:
        seen = set()
        for side in ("off", "on"):
            for r in runs[side]:
                if (r["case"], side) in seen:
                    continue
                seen.add((r["case"], side))
                print(f"\n--- [{side}] {r['case']}")
                for i, it in enumerate(r["items"][:8], start=1):
                    rs = it.get("rerank_score")
                    print(f"  {i:2d} {it['score']:.4f} {'' if rs is None else f'(ce {rs:+.1f})':>10s} "
                          f"{it.get('predicate') or it.get('kind')!s:22.22s} | {(it.get('text') or '')[:80]}")
    return {"aggregate": agg, "by_case": {q: {s: v for s, v in sides.items()} for q, sides in by_case.items()}}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=ASTORIA_URL)
    ap.add_argument("--user", default="rick")
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--mode", choices=("rest", "exec"), default="rest")
    ap.add_argument("--no-exec-fallback", action="store_true")
    ap.add_argument("--json", default="")
    ap.add_argument("--show", action="store_true", help="print top-8 per case, both sides")
    a = ap.parse_args()

    runs = None
    mode = a.mode
    if mode == "rest":
        runs = run_rest(a.url, a.user, a.repeat, a.limit)
        if runs is None:
            if a.no_exec_fallback:
                raise SystemExit(2)
            mode = "exec"
    if mode == "exec":
        print(f"exec mode via: {EXEC_CMD}")
        runs = run_exec(a.user, a.repeat, a.limit)
    print(f"\nmode={mode} user={a.user} cases={len(CASES)} repeat={a.repeat} limit={a.limit} url={a.url}")
    summary = summarize(runs, a.show)
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"mode": mode, "summary": summary, "runs": runs}, f, indent=1, default=str)
        print(f"\nwrote {a.json}")


if __name__ == "__main__":
    main()
