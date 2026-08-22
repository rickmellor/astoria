"""Shared helpers for the Astoria benchmark harness (scripts/bench/).

Environment (all optional):
  ASTORIA_URL   REST base, default http://192.168.1.134:8933
  BENCH_DSN     Postgres DSN reachable from this machine (NAS PG is loopback-only; see README in
                docs/PERFORMANCE.md "How to run" for the ssh-exec relay).
  TEI_URL       TEI embeddings base, default http://192.168.1.134:8931
  BENCH_SSH     ssh host for `docker stats` sampling (default root-dxp4800gt; "" disables)
"""
from __future__ import annotations

import json
import os
import random
import statistics
import subprocess
import threading
import time
from dataclasses import dataclass, field

ASTORIA_URL = os.environ.get("ASTORIA_URL", "http://192.168.1.134:8933").rstrip("/")
BENCH_DSN = os.environ.get("BENCH_DSN", os.environ.get("ASTORIA_DB_DSN", ""))
TEI_URL = os.environ.get("TEI_URL", "http://192.168.1.134:8931").rstrip("/")
BENCH_SSH = os.environ.get("BENCH_SSH", "root-dxp4800gt")
BENCH_PREFIX = "bench-"          # every user_id this harness touches starts with this
EMBED_DIM = 768

# ---------------------------------------------------------------------------
# statistics

def pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def summarize(lat_ms: list[float], errors: int = 0, elapsed_s: float | None = None) -> dict:
    n = len(lat_ms)
    out = {"n": n, "errors": errors,
           "p50_ms": round(pct(lat_ms, .50), 1) if n else None,
           "p95_ms": round(pct(lat_ms, .95), 1) if n else None,
           "p99_ms": round(pct(lat_ms, .99), 1) if n else None,
           "max_ms": round(max(lat_ms), 1) if n else None,
           "mean_ms": round(statistics.fmean(lat_ms), 1) if n else None}
    if elapsed_s:
        out["elapsed_s"] = round(elapsed_s, 1)
        out["rps"] = round(n / elapsed_s, 2)
    return out


def fmt_row(name: str, s: dict) -> str:
    return (f"| {name} | {s.get('n')} | {s.get('p50_ms')} | {s.get('p95_ms')} | {s.get('p99_ms')} | "
            f"{s.get('max_ms')} | {s.get('rps', '')} | {s.get('errors')} |")


TABLE_HEADER = "| case | n | p50 ms | p95 ms | p99 ms | max ms | req/s | errors |\n|---|---|---|---|---|---|---|---|"

# ---------------------------------------------------------------------------
# queries: 30 varied natural-language recalls (mix of profile, semantic, procedural, episodic shapes)

QUERIES = [
    "what beer do I like", "which editor do I use", "postgres pgvector setup on the nas", "rdna4 vulkan rocm",
    "johnny default profile", "what tools does the user use", "home assistant zwave", "favorite language",
    "nfs jumbo frames ugreen", "megaplan memoryos input clients", "saint router cascade", "qwen gemma kimi seats",
    "skills and goals", "people I know", "decisions about the fleet", "current focus", "preferred shell",
    "timezone and location", "stout porter lager", "embedding recall capture cognify",
    "snapshot audit tombstone", "bitemporal supersede", "hardware owned", "services running",
    "projects worked on", "how to run vllm", "neovim fish zsh", "astoria portland oregon",
    "claude opus sonnet haiku", "router embed recall",
]

# the "real" user: 500 realistic triples written through POST /facts (true TEI vectors) so semantic
# quality at scale can be checked. Keep (predicate, value) plausible and queryable.
REAL_TOPICS = {
    "favorite_beer": ["IPA", "stout", "porter", "saison", "pilsner", "tripel", "lager", "hazy IPA"],
    "favorite_editor": ["Neovim", "VS Code", "Emacs", "Helix"],
    "favorite_language": ["Python", "Rust", "Go", "TypeScript"],
    "preferred_shell": ["fish", "zsh", "bash"],
    "uses_tool": ["Neovim", "tmux", "fish shell", "ripgrep", "fzf", "httpx", "pytest", "ruff", "docker compose",
                  "llama.cpp", "vLLM", "pgvector", "Postgres 18", "systemd user units", "Home Assistant",
                  "zwave-js-ui", "WirePlumber", "PipeWire", "Typer", "FastAPI", "FastMCP", "Claude Code"],
    "owns_hardware": ["4x AMD R9700 GPUs", "Threadripper 3945WX workstation", "UGREEN DXP4800GT NAS",
                      "Anker PowerConf mic", "10GbE switch", "dual 9070XT 9900K box"],
    "runs_service": ["SAINT router", "johnny inference manager", "MemoryOS", "MegaPlan", "Astoria memory",
                     "TEI nomic embeddings", "Home Assistant", "jellyfin", "qbittorrent"],
    "works_on_project": ["NOVA stack", "input agent platform", "johnny v2 vllm-tui", "Astoria", "MegaPlan",
                         "SAINT cloud ladder", "infrastructure docs repo"],
    "goal": ["expand to 8 R9700 GPUs", "make recall under 500 ms", "keep the NAS as permanent model store",
             "replace bash johnny with python", "finish SAINT johnny live integration"],
    "decided": ["DDR4 platform stays", "GLM-5.2 abandoned as too slow", "Qwen-122B is the default delegate",
                "fp8 KV is a context tool not a speed tool", "vLLM ROCm pinned at 0.20.2"],
    "learned_howto": ["restart saint with systemctl --user restart saint.service",
                      "open the NAS postgres only on loopback", "bind SAINT to 0.0.0.0 for MemoryOS extraction",
                      "re-apply the pi subagents grammar patch", "use the Vulkan image for Kimi K2.7"],
    "likes": ["Portland", "hoppy beer", "terse answers", "open frame chassis", "jumbo frames"],
    "dislikes": ["platform swap suggestions", "sluggish memory layers", "verbose summaries", "emoji in reports"],
    "interested_in": ["RDNA4 kernels", "bitemporal databases", "reciprocal rank fusion", "HNSW tuning",
                      "agent platforms", "local inference"],
    "has_skill": ["Python", "Rust", "Postgres tuning", "docker compose", "home automation", "GPU fleet ops"],
    "knows_person": ["the Nova Robotics crew", "Short Circuit team", "an Anthropic contact"],
    "location": ["Portland, Oregon"], "timezone": ["America/Los_Angeles"],
    "default_model": ["qwen-122b", "gemma-26B chat", "kimi k2.7 code"],
    "current_focus": ["Astoria performance validation", "SAINT johnny data plane", "input agent platform"],
    "primary_workstation": ["specul8-o-matic"], "primary_nas": ["UGREEN DXP4800GT"],
    "related_to": ["SAINT routes to johnny seats", "MegaPlan stores plans on the NAS",
                   "MemoryOS is replaced by Astoria", "input talks to SAINT"],
    "fact": ["the NAS has 7 GB of RAM", "astoria postgres has a 1 GB memory limit", "TEI nomic is CPU only",
             "recall uses RRF with k 60", "facts are bitemporal", "the R9700 has 32 GB VRAM",
             "Qwen 27B runs at 16.85 t/s on Vulkan", "Ornith runs at 25 t/s on four R9700"],
}
REAL_SUBJECTS = ["user", "specul8-o-matic", "the NAS", "SAINT", "johnny", "Astoria", "MegaPlan", "the fleet",
                 "input", "MemoryOS", "TEI", "Qwen-122B", "Kimi K2.7", "Ornith", "Home Assistant"]

# queries whose answer is known to exist in the real user's facts (semantic quality probes)
REAL_QUERIES = [
    ("what beer do I like", "favorite_beer"), ("which editor do I use", "favorite_editor"),
    ("what shell do I prefer", "preferred_shell"), ("programming language preference", "favorite_language"),
    ("what GPUs do I own", "owns_hardware"), ("which services run on the NAS", "runs_service"),
    ("what am I working on", "works_on_project"), ("goals for the GPU fleet", "goal"),
    ("decisions about the platform", "decided"), ("how do I restart saint", "learned_howto"),
    ("where do I live", "location"), ("what timezone", "timezone"), ("default llm model", "default_model"),
    ("what is my current focus", "current_focus"), ("things I dislike", "dislikes"),
    ("my main workstation", "primary_workstation"), ("how much ram does the nas have", "fact"),
    ("tools I use daily", "uses_tool"), ("what am I interested in", "interested_in"), ("skills", "has_skill"),
]


def real_facts(user_id: str, n: int = 500, seed: int = 7) -> list[dict]:
    """Deterministic realistic triples for the real-embedding user (subject/predicate/value)."""
    rng = random.Random(seed)
    out, seen = [], set()
    preds = list(REAL_TOPICS)
    while len(out) < n:
        pred = rng.choice(preds)
        val = rng.choice(REAL_TOPICS[pred])
        functional = pred.startswith(("favorite_", "default_", "primary_", "preferred_", "current_")) or pred in ("location", "timezone")
        subj = "user" if rng.random() < 0.55 else rng.choice(REAL_SUBJECTS)
        if functional:
            # keep one value per (subject, predicate) so the store doesn't just supersede
            key = (subj, pred)
        else:
            if rng.random() < 0.5:
                val = f"{val} ({rng.choice(['2025', '2026', 'planned', 'verified', 'since July', 'on the workstation', 'on the nas'])})"
            key = (subj, pred, val.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"subject": subj, "predicate": pred, "value": val})
    return out


# ---------------------------------------------------------------------------
# vectors

def unit_vec_text(rng: random.Random, dim: int = EMBED_DIM, decimals: int = 4) -> str:
    v = [rng.gauss(0.0, 1.0) for _ in range(dim)]
    s = (sum(x * x for x in v) ** 0.5) or 1.0
    return "[" + ",".join(f"{x / s:.{decimals}f}" for x in v) + "]"


def tei_embed(text: str, *, query: bool = True, timeout: float = 30.0) -> tuple[list[float] | None, float]:
    """Embed one text via TEI; returns (vector|None, latency_ms)."""
    import httpx
    prefix = "search_query: " if query else "search_document: "
    t = time.perf_counter()
    try:
        r = httpx.post(f"{TEI_URL}/v1/embeddings", json={"input": [prefix + text], "model": "nomic"}, timeout=timeout)
        r.raise_for_status()
        return r.json()["data"][0]["embedding"], (time.perf_counter() - t) * 1000
    except Exception:  # noqa: BLE001
        return None, (time.perf_counter() - t) * 1000


# ---------------------------------------------------------------------------
# docker stats sampler (ssh)

@dataclass
class StatsSampler:
    containers: tuple[str, ...] = ("astoria", "astoria-postgres", "memoryos-tei")
    interval_s: float = 5.0
    samples: list[dict] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thr: threading.Thread | None = None

    def _sample(self) -> dict | None:
        if not BENCH_SSH:
            return None
        try:
            out = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", BENCH_SSH, "docker stats --no-stream --format '{{json .}}' " + " ".join(self.containers)],
                capture_output=True, text=True, timeout=20).stdout
        except Exception:  # noqa: BLE001
            return None
        row = {"t": time.time()}
        for line in out.splitlines():
            try:
                j = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            row[j["Name"]] = {"cpu": j["CPUPerc"], "mem": j["MemUsage"], "mem_pct": j["MemPerc"]}
        return row

    def _loop(self) -> None:
        while not self._stop.is_set():
            s = self._sample()
            if s:
                self.samples.append(s)
            self._stop.wait(self.interval_s)

    def start(self) -> "StatsSampler":
        self._thr = threading.Thread(target=self._loop, daemon=True)
        self._thr.start()
        return self

    def stop(self) -> dict:
        self._stop.set()
        if self._thr:
            self._thr.join(timeout=30)
        return self.peak()

    def peak(self) -> dict:
        """Peak CPU% and mem (MiB) per container over the sampled window."""
        out: dict[str, dict] = {}
        for s in self.samples:
            for name, v in s.items():
                if name == "t":
                    continue
                cpu = float(v["cpu"].rstrip("%") or 0)
                mem = _mib(v["mem"].split("/")[0])
                o = out.setdefault(name, {"cpu_peak": 0.0, "mem_peak_mib": 0.0, "mem_limit": v["mem"].split("/")[-1].strip()})
                o["cpu_peak"] = max(o["cpu_peak"], cpu)
                o["mem_peak_mib"] = max(o["mem_peak_mib"], mem)
        return out


def _mib(s: str) -> float:
    s = s.strip()
    for suf, mul in (("GiB", 1024), ("MiB", 1), ("KiB", 1 / 1024), ("kB", 1 / 1024), ("MB", 1), ("GB", 1024), ("B", 1 / 1048576)):
        if s.endswith(suf):
            return float(s[: -len(suf)]) * mul
    return 0.0


def docker_stats_once() -> dict | None:
    return StatsSampler()._sample()


def oom_events(since_minutes: int = 60) -> str:
    """Any OOM kills on the NAS containers? (docker events + dmesg tail)."""
    if not BENCH_SSH:
        return "n/a"
    try:
        out = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", BENCH_SSH,
             f"docker events --since {since_minutes}m --until 0s --filter event=oom 2>/dev/null | tail -5; "
             "docker inspect astoria astoria-postgres --format '{{.Name}} OOMKilled={{.State.OOMKilled}} restarts={{.RestartCount}}'"],
            capture_output=True, text=True, timeout=30).stdout
        return out.strip() or "no oom events"
    except Exception as e:  # noqa: BLE001
        return f"unavailable: {e}"
