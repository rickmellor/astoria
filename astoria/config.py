"""Astoria settings — env-driven (ASTORIA_* / POSTGRES_* / ANTHROPIC_API_KEY).

One place for every knob; defaults target the NAS deployment in deploy/nas/.env.example.
"""
from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ASTORIA_", extra="ignore", env_file=".env", env_file_encoding="utf-8")

    # --- storage -----------------------------------------------------------
    db_dsn: str = Field(default="postgresql://astoria:astoria@127.0.0.1:55432/astoria")
    db_pool_min: int = 1
    db_pool_max: int = 8

    # --- identity ----------------------------------------------------------
    user_default: str = "default"                    # user_id applied when a request omits it
    # "name:token,name:token" — clients that may assert trust (human-stated facts).
    client_tokens: str = ""
    # when true, WRITE actions require a valid bearer token (reads stay open on the LAN); default off
    require_token: bool = False

    # --- embeddings (NAS TEI nomic, 768-d, pinned) --------------------------
    embed_url: str = "http://localhost:8931"          # single OpenAI-compatible nomic endpoint (TEI, vLLM, …)
    # optional priority list "url|model,url|model" — e.g. a fast GPU/CPU seat first, an always-on TEI as fallback.
    # Every endpoint is verified (served model mentions nomic + canary vector-space check). Empty → embed_url.
    embed_urls: str = ""
    embed_dim: int = 768
    embed_require_substring: str = "nomic-embed"     # served-model assertion
    embed_timeout_s: float = 20.0
    embed_max_chars: int = 6000                       # nomic cap is 2048 tokens; TEI auto-truncates
    # False → capture / POST /facts write rows with embedding NULL (no TEI call in the request) and the
    # worker's embed_backfill fills them on its next tick; True (or per-request sync=true) embeds inline.
    embed_sync: bool = False

    # --- rerank (optional cross-encoder stage over the top-N recall candidates) ---
    # priority list "url|model,url|model" of TEI rerankers (POST /rerank); NAS astoria-rerank container
    # (cross-encoder/ms-marco-MiniLM-L-6-v2, 22M params, CPU) is the default. Empty → stage off.
    rerank_urls: str = ""                             # "url|model,…" TEI /rerank endpoints in priority order; empty → stage off
    rerank_enabled: bool = True                       # kill switch; per-request `rerank=False` also bypasses
    # top-N fact candidates (by score) sent to the reranker, plus the top-6 episode candidates (recall.py
    # RERANK_EPISODES). CPU-bound on the NAS (~0.3 ms/token, hooks capped at 240 chars): 30 facts + 6
    # episodes ≈ 300-350 ms cold; repeated (query, hook) pairs are cached — measured 2026-08-22.
    rerank_top_n: int = 30
    rerank_weight: float = 0.6                        # final = (1-w)·norm(score) + w·norm(sigmoid(rerank))
    rerank_timeout_s: float = 3.0                     # read path: fail fast, degrade to the base ranking

    # --- LLM (cognify/curator only — never at read) --------------------------
    llm_url: str = "http://localhost:4000/v1"        # OpenAI-compatible gateway for the write path (extraction)
    llm_model: str = "auto"
    llm_timeout_s: float = 120.0
    llm_fallback_model: str = "claude-sonnet-4-6"    # direct Anthropic when SAINT unreachable
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    profile_llm: bool = True                          # LLM profile narrative (source='llm'); template fallback

    # --- retrieval defaults (request params override) --------------------------
    recall_limit: int = 12                            # default `limit`
    recall_token_budget: int = 1000                   # default `max_tokens`
    recall_min_cosine: float = 0.45                   # nomic: short personal queries vs long hooks sit ~0.46-0.50; BM25 + query synonyms carry the rest
    graph_max_depth: int = 2                          # graph expansion hops (0 = off)
    graph_max_fanout: int = 20
    # recency half-lives (days) used by recall scoring AND curator decay
    recency_half_life_days: float = 180.0             # semantic facts
    belief_half_life_days: float = 60.0               # is_belief facts
    episodic_half_life_days: float = 30.0             # episodes
    # curator DECAY (forgetting) half-lives — deliberately shorter than ranking recency: an unrecalled,
    # machine-sourced fact ages out of the active set faster than it drops in rank
    decay_half_life_days: float = 90.0
    decay_belief_half_life_days: float = 45.0

    # --- trust / confidence --------------------------------------------------
    confidence_floor: float = 0.05
    confidence_cap: float = 0.98
    confidence_staging_threshold: float = 0.35       # below → staging, not active

    # --- workers -------------------------------------------------------------
    worker_enabled: bool = True
    cognify_poll_s: float = 30.0                      # worker tick: embed_backfill + cognify drain (CONTRACT: 30 s)
    cognify_batch: int = 4
    curator_interval_min: int = 60                    # profile re-derive check + working-window archive cadence
    reflect_interval_h: float = 6.0                   # curator.reflect cadence (LLM)
    curator_daily_h: float = 24.0                     # dedup / decay / snapshot prune cadence
    working_window_turns: int = 20                    # archive_old_turns: keep at most N active turns per session
    working_window_hours: int = 72                    # archive_old_turns: turns older than this leave working memory
    decay_archive_threshold: float = 0.08             # curator.decay: score below → status='archived'
    decay_min_age_days: int = 90                      # curator.decay: only facts ingested longer ago than this
    dedup_cosine: float = 0.93                        # curator.dedup_facts: near-duplicate threshold

    # --- service -------------------------------------------------------------
    log_level: str = "INFO"
    version: str = "0.1.0"

    def client_token_map(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for pair in (self.client_tokens or "").split(","):
            if ":" in pair:
                name, tok = pair.split(":", 1)
                if name.strip() and tok.strip():
                    out[tok.strip()] = name.strip()
        return out


@lru_cache
def settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:  # tests
    settings.cache_clear()
    os.environ.pop("_ASTORIA_SETTINGS_RESET", None)
