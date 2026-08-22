#!/usr/bin/env python3
"""Render the benchmark JSONL (loadgen/seed --out) as markdown tables + threshold verdicts.

    scripts/bench/report.py results.jsonl            # markdown to stdout
    scripts/bench/report.py results.jsonl --json     # the threshold verdict as JSON (CI-friendly)

Records are keyed by `phase` and the optional `label` given to loadgen (`--label pre-load|150k|...`).
When several records share (phase,label) the LAST one wins. The thresholds below are the acceptance
contract from the performance-validation task (docs/PERFORMANCE.md "Verdict").
"""
from __future__ import annotations

import argparse
import json
import sys

THRESHOLDS = {
    "recall_e2e_p95_8clients_ms": 500,     # end-to-end /recall p95 at scale, 8 concurrent clients (TEI-inclusive)
    "recall_db_only_p95_ms": 80,           # DB-only recall p95 at scale (service code path, pre-embedded query)
    "capture_p95_ms": 400,                 # /capture cognify=false p95 under mixed load
    "errors": 0,                           # across every HTTP phase
    "oom": 0,                              # no container OOM kill
    "correct_exactly_one_active": True,    # 20 concurrent /correct on one functional key → 1 active
}

H = "| case | n | p50 ms | p95 ms | p99 ms | max ms | req/s | errors |\n|---|---|---|---|---|---|---|---|"


def row(name: str, s: dict | None) -> str:
    if not s:
        return f"| {name} | – | – | – | – | – | – | – |"
    return (f"| {name} | {s.get('n')} | {s.get('p50_ms')} | {s.get('p95_ms')} | {s.get('p99_ms')} | "
            f"{s.get('max_ms')} | {s.get('rps', '')} | {s.get('errors', 0)} |")


def load(path: str) -> dict[tuple[str, str], dict]:
    recs: dict[tuple[str, str], dict] = {}
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        recs[(r["phase"], r.get("label", ""))] = r
    return recs


def by_phase(recs, phase):
    return {lab: r for (ph, lab), r in recs.items() if ph == phase}


def peak_str(stats: dict | None) -> str:
    if not stats:
        return ""
    return "; ".join(f"{k}: cpu {v['cpu_peak']:.0f}% mem {v['mem_peak_mib']:.0f} MiB/{v['mem_limit']}" for k, v in stats.items())


def render(recs: dict) -> tuple[str, dict]:
    out: list[str] = []
    verdict: dict = {}
    P = lambda s="": out.append(s)  # noqa: E731

    # --- TEI floor -----------------------------------------------------------
    tei = by_phase(recs, "tei")
    if tei:
        r = list(tei.values())[-1]
        P("### TEI query-embed floor (fixed per-request cost, independent of store size)"); P(); P(H)
        P(row("TEI embed, 1 client (sequential)", r["sequential"]))
        P(row("TEI embed, 4 concurrent clients", r["concurrent_4"]))
        P(row("TEI embed, 8 concurrent clients", r["concurrent_8"]))
        P()

    # --- per-endpoint floors (embedding seats, rerankers) ----------------------
    for lab, r in by_phase(recs, "embed-floor").items():
        P(f"### Query-embed floor per embedding endpoint (fresh texts, no cache) [{lab}]"); P(); P(H)
        for name, e in r["endpoints"].items():
            P(row(f"embed [{name}] {e['model']} 1 client", e["sequential"]))
            for k in ("concurrent_4", "concurrent_8"):
                if k in e:
                    P(row(f"embed [{name}] {k.split('_')[1]} concurrent clients", e[k]))
        P()
    for lab, r in by_phase(recs, "rerank-floor").items():
        P(f"### Cross-encoder floor per reranker endpoint (1 query × {r['texts_per_call']} hooks per call, fresh query) [{lab}]"); P(); P(H)
        for name, e in r["endpoints"].items():
            P(row(f"rerank [{name}] 1 client", e["sequential"]))
            for k in ("concurrent_4", "concurrent_8"):
                if k in e:
                    P(row(f"rerank [{name}] {k.split('_')[1]} concurrent clients", e[k]))
        P()
    for lab, r in by_phase(recs, "embed-gap").items():
        P(f"### Async write path — time until the worker has embedded new rows [{lab}]"); P(); P(H)
        P(row(f"/capture (async) × {r['n']}", r["capture"])); P(row(f"POST /facts (async) × {r['n']}", r["facts_post"])); P()
        P(f"- embedded at write time: {r['embedded_at_write']}; first row embedded after {r['first_embedded_s']} s; "
          f"all {r['n']}+{r['n']} embedded after {r['all_embedded_s']} s (writes took {r['writes_s']} s; errors {r.get('errors', 0)})"); P()
    for lab, r in by_phase(recs, "correct-seq").items():
        P(f"### {r['n']} sequential POST /correct on ONE functional key (API-built chain, belief-axis versioning) [{lab}]"); P(); P(H)
        P(row(f"POST /correct × {r['n']} sequential", r["correct"]))
        P(row(f"GET /history (chain len {r['history_len']}, active {r['history_active']})", r["history"]))
        P(row(f"POST /as_of scoped (rows/query {r['as_of_rows_per_query']})", r["as_of"]))
        P(row("POST /as_of scoped + as_believed_at", r["as_of_believed"])); P()
        P(f"- HTTP {r['codes']}, actions {r['actions']}; rows in `fact` for the key: **{r.get('db_rows_total')}** "
          f"({r.get('db_rows_by_status')}) → {r.get('db_rows_total', 0) / max(1, r['n']):.2f} rows per /correct"); P()

    # --- baseline vs scale ---------------------------------------------------
    base = by_phase(recs, "baseline"); rec = by_phase(recs, "recall")
    if base or rec:
        P("### /recall, /capture, POST /facts — before vs after the deep load (single client)"); P(); P(H)
        for lab, r in base.items():
            P(row(f"[{lab}] /recall e2e (30 varied queries)", r["recall_e2e"]))
            P(row(f"[{lab}] /recall DB-only (same 30, pre-embedded, in-container)", r["recall_db_only"]))
            P(row(f"[{lab}] TEI query embed (same 30)", r["tei_query_embed"]))
            P(row(f"[{lab}] /capture cognify=false (turn){' — async embed' if 'capture_sync' in r else ''}", r["capture"]))
            if r.get("capture_sync"):
                P(row(f"[{lab}] /capture cognify=false (turn) sync=true (inline embed)", r["capture_sync"]))
            P(row(f"[{lab}] POST /facts novel set-fact{' — async embed' if 'facts_post_sync' in r else ' (TEI embed + insert)'} {r.get('facts_post_actions', '')}", r["facts_post"]))
            if r.get("facts_post_sync"):
                P(row(f"[{lab}] POST /facts novel set-fact sync=true (inline embed + insert) {r.get('facts_post_sync_actions', '')}", r["facts_post_sync"]))
        for lab, r in rec.items():
            P(row(f"[{lab}] /recall e2e (real-embedding user, {r['n']} queries{', rerank ' + ('on' if r.get('rerank') else 'off') if r.get('rerank') is not None else ''})", r["e2e"]))
            P(row(f"[{lab}] /recall DB-only iterative_scan=off", r["db_only"]))
            P(row(f"[{lab}] /recall DB-only iterative_scan=relaxed_order", r["db_only_iterative"]))
            P(row(f"[{lab}] TEI query embed", r["tei_query_embed"]))
        P()
        for lab, r in rec.items():
            P(f"[{lab}] semantic hit-rate (expected predicate in items, {len(r.get('e2e_misses', [])) + int(round(r['e2e_hit_rate'] * 20))} probes): "
              f"e2e **{r['e2e_hit_rate']}**, DB-only {r['db_only_hit_rate']}, iterative {r['db_only_iterative_hit_rate']}"
              + (f"; misses: {r['e2e_misses']}" if r.get("e2e_misses") else ""))
            vc = r["db_only_vec_candidates_of_40"]; vci = r["db_only_iterative_vec_candidates_of_40"]
            P(f"[{lab}] HNSW candidates returned of LIMIT 40 after the user/status/layer filter: "
              f"off min {vc['min']} mean {vc['mean']} (queries <40: {vc['lt40']}); relaxed_order min {vci['min']} mean {vci['mean']} (<40: {vci['lt40']})")
            P()
        if any("breakdown" in r for r in rec.values()):
            P("DB-only per-step cost (in-container, ms p50 / p95):"); P()
            steps = [k for k in next(iter(rec.values()))["breakdown"] if not k.startswith("_") and k != "probe_s"]
            P("| step | " + " | ".join(f"{lab}" for lab in rec) + " |"); P("|---|" + "---|" * len(rec))
            for st in steps:
                P(f"| {st} | " + " | ".join(f"{r['breakdown'][st]['p50']} / {r['breakdown'][st]['p95']}" if st in r.get("breakdown", {}) else "–" for r in rec.values()) + " |")
            P()

    # --- load ----------------------------------------------------------------
    loads = by_phase(recs, "load")
    if loads:
        P("### Deep load (direct COPY through the tunnel, in-place HNSW inserts)"); P()
        P("| users | facts | facts/s | episodes | episodes/s | VACUUM ANALYZE s |"); P("|---|---|---|---|---|---|")
        for r in loads.values():
            P(f"| {r['users']} | {r['facts']} | {r['facts_rps']} | {r['episodes']} | {r['episodes_rps']} | {r.get('vacuum_s', '')} |")
        P()
    sz_recs = [r for r in loads.values() if "sizes" in r] + list(by_phase(recs, "sizes").values())
    if sz_recs:
        s = sz_recs[-1]["sizes"]
        P(f"Sizes after load (db total {s['db_total']}; bench rows {s['bench_rows']}):"); P()
        P("| table | rows | total | heap | toast |"); P("|---|---|---|---|---|")
        for t, v in s["tables"].items():
            P(f"| {t} | {s['rows'][t]} | {v['total']} | {v['heap']} | {v['toast']} |")
        P(); P("| index | size |"); P("|---|---|")
        for k, v in s["indexes"].items():
            P(f"| {k} | {v} |")
        P()

    # --- concurrency ---------------------------------------------------------
    conc = by_phase(recs, "concurrency")
    errors_total = 0
    for lab, r in conc.items():
        flags = (f" — rerank {'on' if r.get('rerank') else ('off' if r.get('rerank') is False else 'service default')}"
                 f"{', fresh queries (caches cold)' if r.get('unique') else ', 30 fixed queries (embed/rerank caches warm)'}") if "rerank" in r else ""
        P(f"### Concurrency sweep — /recall only, {r['seconds']:.0f} s each [{lab}]{flags}"); P(); P(H)
        for run in r["runs"]:
            P(row(f"recall × {run['clients']} clients", run)); errors_total += run.get("errors", 0)
            if run["clients"] == 8:
                verdict[f"recall_e2e_p95_8clients_ms[{lab}]"] = run["p95_ms"]
                # contract row = the service default (rerank on, or an unlabelled sweep), warm caches; other labels listed below it
                if "recall_e2e_p95_8clients_ms" not in verdict or (r.get("rerank") is not False and not r.get("unique")):
                    verdict["recall_e2e_p95_8clients_ms"] = run["p95_ms"]
        P()
        for run in r["runs"]:
            P(f"- {run['clients']} clients peak: {peak_str(run.get('stats'))}")
        P(f"- OOM check: `{r.get('oom')}`"); P()

    # --- DB-only concurrency -------------------------------------------------
    for lab, r in by_phase(recs, "db-concurrency").items():
        P(f"### DB-only concurrency — the store alone (in-container, pre-embedded queries, TEI excluded), {r['seconds']:.0f} s each [{lab}] — user `{r['user']}`, iterative_scan={r.get('iterative_scan', 'off')}, random_vectors={r.get('random_vectors', False)}"); P(); P(H)
        for run in r["runs"]:
            P(row(f"DB-only recall × {run['clients']} connections", run)); errors_total += run.get("errors", 0)
            if run["clients"] == 8:
                verdict["recall_db_only_p95_8clients_ms"] = run["p95_ms"]
        P(); P(f"- peak: {peak_str(r.get('stats'))}"); P()

    # --- mixed ---------------------------------------------------------------
    mixed = by_phase(recs, "mixed")
    for lab, r in mixed.items():
        flags = (f" — rerank {'on' if r.get('rerank') else ('off' if r.get('rerank') is False else 'service default')}") if "rerank" in r else ""
        P(f"### Mixed load — 8 recall + 4 capture + 2 POST /facts clients, {r['seconds']:.0f} s [{lab}]{flags}"); P(); P(H)
        for k, s in r["runs"].items():
            P(row(k, s)); errors_total += s.get("errors", 0)
            if k.startswith("capture"):
                verdict["capture_p95_ms"] = s["p95_ms"]
            if k.startswith("recall"):
                verdict["recall_e2e_p95_mixed_ms"] = s["p95_ms"]
        P(); P(f"- peak: {peak_str(r.get('stats'))}"); P(f"- OOM check: `{r.get('oom')}`"); P()

    # --- correct -------------------------------------------------------------
    for lab, r in by_phase(recs, "correct").items():
        P(f"### 20 concurrent POST /correct on ONE functional key [{lab}]"); P(); P(H)
        P(row("POST /correct × 20 (parallel)", r["latency"])); P()
        P(f"- HTTP codes {r['codes']}; active rows after: API {r['active_after_api']}, DB {r['active_after_db']}; "
          f"history length {r['history_len']}; **{'PASS' if r['pass'] else 'FAIL'}**")
        verdict["correct_exactly_one_active"] = bool(r["pass"]); P()

    # --- chain ---------------------------------------------------------------
    for lab, r in by_phase(recs, "chain").items():
        P(f"### /history and /as_of on a 50-long supersede chain [{lab}]"); P(); P(H)
        P(row(f"GET /history (chain len {r['history_len']}, active {r['history_active']})", r["history"]))
        P(row(f"POST /as_of scoped (rows/query {r['as_of_rows_per_query']})", r["as_of"]))
        P(row("POST /as_of scoped + as_believed_at", r["as_of_believed"]))
        P(row("POST /as_of unscoped (whole user, limit 50)", r["as_of_unscoped"])); P()

    # --- worker --------------------------------------------------------------
    for lab, r in by_phase(recs, "worker").items():
        P(f"### Worker interference — recall × 4 while cognify drains {r['turns']} turns [{lab}]"); P(); P(H)
        P(row("recall × 4, worker idle (control)", r["recall_idle_worker"]))
        P(row(f"capture cognify=true × {r['turns']} (enqueue)", r["enqueue"]))
        P(row("recall × 4, worker draining", r["recall_draining"])); P()
        P(f"- queue before {r['queue_before']} → after {r['queue_after']} (queued {r['queued']})")
        P(f"- peak idle: {peak_str(r['recall_idle_worker'].get('stats'))}")
        P(f"- peak draining: {peak_str(r['recall_draining'].get('stats'))}"); P()
        errors_total += r["recall_idle_worker"].get("errors", 0) + r["recall_draining"].get("errors", 0) + r["enqueue"].get("errors", 0)

    # --- explain -------------------------------------------------------------
    for lab, r in by_phase(recs, "explain").items():
        P(f"### EXPLAIN (ANALYZE, BUFFERS) summaries [{lab}] — GUCs {r['gucs']}"); P()
        P("| query | scan | exec ms | buffers (shared hit/read) |"); P("|---|---|---|---|")
        for name, txt in r["plans"].items():
            scan = "HNSW" if "Index Scan using fact_vec" in txt or "Index Scan using episode_vec" in txt else \
                   ("GIN" if "Bitmap Index Scan on fact_tsv" in txt or "Bitmap Index Scan on episode_tsv" in txt else
                    ("fact_key" if "fact_key" in txt else ("seq scan" if "Seq Scan" in txt else "other")))
            ex = next((l.split("Execution Time:")[1].strip() for l in txt.splitlines() if "Execution Time:" in l), "?")
            buf = next((l.strip().replace("Buffers: ", "") for l in txt.splitlines() if l.strip().startswith("Buffers:")), "?")
            P(f"| {name} | {scan} | {ex} | {buf} |")
        P(); P("<details><summary>full plans</summary>"); P()
        for name, txt in r["plans"].items():
            P(f"**{name}**"); P("```"); P(txt); P("```"); P()
        P("</details>"); P()

    # --- filtered HNSW -------------------------------------------------------
    for lab, r in by_phase(recs, "filter").items():
        P(f"### Filtered HNSW on a bulk user (`{r['user']}`: {r['user_rows']} active of {r['total_rows']} facts), {r['n']} random query vectors [{lab}]"); P()
        P("| hnsw.iterative_scan | fact candidates of LIMIT 40 (min / mean / max; queries <40) | fact_vec ms p50 / p95 | episode candidates of LIMIT 20 (min / mean / max; <20) | episode_vec ms p50 / p95 |")
        P("|---|---|---|---|---|")
        for mode in ("off", "relaxed_order"):
            m = r[mode]; f = m["fact_candidates_of_40"]; e = m["episode_candidates_of_20"]
            P(f"| {mode} | {f['min']} / {f['mean']} / {f['max']}; {f['lt_k']} | {m['fact_vec_ms']['p50']} / {m['fact_vec_ms']['p95']} | "
              f"{e['min']} / {e['mean']} / {e['max']}; {e['lt_k']} | {m['episode_vec_ms']['p50']} / {m['episode_vec_ms']['p95']} |")
        P()

    # --- wipe ----------------------------------------------------------------
    for lab, r in by_phase(recs, "wipe").items():
        P(f"### Cleanup — bench-* rows before → after wipe [{lab}]"); P()
        P("| table | before | after |"); P("|---|---|---|")
        for t in r["before"]:
            P(f"| {t} | {r['before'][t]} | {r['after'][t]} |")
        P(); P(f"- wipe took {r['seconds']} s; db total after: {r['sizes']['db_total']}"); P()

    # --- verdict -------------------------------------------------------------
    verdict["errors"] = errors_total
    for r in list(conc.values()) + list(mixed.values()):
        if "OOMKilled=true" in str(r.get("oom", "")):
            verdict["oom"] = verdict.get("oom", 0) + 1
    verdict.setdefault("oom", 0)
    # the contract rows use the PRIMARY scale label ("150k" — the real-embedding user); other labels are informational
    primary = rec.get("150k") or (list(rec.values())[-1] if rec else None)
    if primary:
        verdict["recall_db_only_p95_ms"] = primary["db_only"]["p95_ms"]
        verdict["recall_db_only_iterative_p95_ms"] = primary["db_only_iterative"]["p95_ms"]
        verdict["recall_e2e_1client_p95_ms"] = primary["e2e"]["p95_ms"]
    b150 = base.get("150k")
    if b150:
        verdict["capture_1client_p95_ms"] = b150["capture"]["p95_ms"]
    checks = [
        ("recall p95 < 500 ms e2e, 8 concurrent clients, at scale (TEI-inclusive)", "recall_e2e_p95_8clients_ms", lambda v: v < 500, True),
        ("  (info) recall p95 e2e, 1 client, at scale", "recall_e2e_1client_p95_ms", lambda v: v < 500, False),
        ("DB-only recall p95 < 80 ms at scale (real-embedding user, deployed code: scan=relaxed_order)", "recall_db_only_iterative_p95_ms", lambda v: v < 80, True),
        ("  (info) same probe with the session GUC scan=off (the deployed recall() still SET LOCALs relaxed_order; only the candidate-count query differs)", "recall_db_only_p95_ms", lambda v: v < 80, False),
        ("capture p95 < 400 ms (1 client, at scale)", "capture_1client_p95_ms", lambda v: v < 400, True),
        ("  (info) capture p95 under mixed load (8 recall + 4 capture + 2 facts clients)", "capture_p95_ms", lambda v: v < 400, False),
        ("zero HTTP errors across all phases", "errors", lambda v: v == 0, True),
        ("no container OOM kill", "oom", lambda v: v == 0, True),
        ("exactly 1 active under 20 concurrent /correct", "correct_exactly_one_active", lambda v: v is True, True),
    ]
    for i, lab in enumerate(conc):
        key = f"recall_e2e_p95_8clients_ms[{lab}]"
        if key in verdict:
            hard = not conc[lab].get("unique") and conc[lab].get("rerank") is False   # the rerank-off sweep is a hard row too (report both)
            checks.insert(1 + i, (f"  {'' if hard else '(info) '}recall p95 < 500 ms e2e, 8 concurrent clients [{lab}]", key, lambda v: v < 500, hard))
    for lab, r in by_phase(recs, "correct-seq").items():
        key = f"correct_seq_errors[{lab}]"; verdict[key] = r["correct"].get("errors", 0)
        errors_total += r["correct"].get("errors", 0)
    for lab, r in by_phase(recs, "embed-gap").items():
        key = f"embed_gap_all_s[{lab}]"
        vals = list((r["all_embedded_s"] or {}).values())
        # None = not embedded before the phase's polling window closed → report the window as a lower bound
        verdict[key] = max(vals) if vals and all(v is not None for v in vals) else f"> {r['samples'][-1][0] if r.get('samples') else '?'} s ({', '.join(k for k, v in (r['all_embedded_s'] or {}).items() if v is None)} not embedded in the window)"
        checks.append((f"  (info) async embed: every new row embedded within 60 s [{lab}]", key, lambda v: isinstance(v, (int, float)) and v <= 60, False))
        errors_total += r.get("errors", 0)
    verdict["errors"] = errors_total
    for lab, r in by_phase(recs, "db-concurrency").items():
        for run in r["runs"]:
            if run["clients"] == 8:
                key = f"db_only_8conn_p95[{lab}]"; verdict[key] = run["p95_ms"]
                checks.append((f"  (info) DB-only recall p95, 8 parallel connections [{lab}]", key, lambda v: v < 500, False))
    for lab, r in by_phase(recs, "filter").items():
        key = f"filter_mean_candidates_off[{lab}]"; verdict[key] = r["off"]["fact_candidates_of_40"]["mean"]
        checks.append((f"  (info) HNSW candidates of 40 for a 4%-share user, scan=off [{lab}] (want 40)", key, lambda v: v >= 40, False))
        key = f"filter_mean_candidates_relaxed[{lab}]"; verdict[key] = r["relaxed_order"]["fact_candidates_of_40"]["mean"]
        checks.append((f"  (info) same with scan=relaxed_order (the fix)", key, lambda v: v >= 40, False))
    P("### Verdict vs thresholds"); P(); P("| threshold | measured | result |"); P("|---|---|---|")
    verdict["pass"] = True
    for name, key, fn, hard in checks:
        v = verdict.get(key)
        ok = (v is not None) and fn(v)
        if hard:
            verdict["pass"] = verdict["pass"] and ok
        P(f"| {name} | {v if v is not None else 'not measured'} | {('PASS' if ok else 'FAIL') if hard else ('ok' if ok else 'short')} |")
    P()
    return "\n".join(out), verdict


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("jsonl")
    ap.add_argument("--json", action="store_true", help="print the verdict dict as JSON instead of markdown")
    a = ap.parse_args(argv)
    md, verdict = render(load(a.jsonl))
    if a.json:
        print(json.dumps(verdict, indent=2))
    else:
        print(md)
    return 0 if verdict.get("pass") else 1


if __name__ == "__main__":
    sys.exit(main())
