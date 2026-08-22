"""Astoria settings — env-driven (ASTORIA_* / POSTGRES_* / ANTHROPIC_API_KEY).

One place for every knob; defaults target the NAS deployment in deploy/nas/.env.example.
"""
from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ASTORIA_", extra="ignore")

    # --- storage -----------------------------------------------------------
    db_dsn: str = Field(default="postgresql://astoria:astoria@127.0.0.1:55432/astoria")
    db_pool_min: int = 1
    db_pool_max: int = 8

    # --- identity ----------------------------------------------------------
    user_default: str = "rick"
    # "name:token,name:token" — clients that may assert trust (human-stated facts).
    client_tokens: str = ""

    # --- embeddings (NAS TEI nomic, 768-d, pinned) --------------------------
    embed_url: str = "http://192.168.1.134:8931"
    # priority list "url|model,url|model": workstation nomic seat via SAINT first (fast, nightly-off),
    # then the always-on NAS TEI. Empty → embed_url only.
    embed_urls: str = "http://192.168.1.221:4000|saint-local-embed,http://192.168.1.134:8931|nomic"
    embed_dim: int = 768
    embed_require_substring: str = "nomic-embed"     # served-model assertion
    embed_timeout_s: float = 20.0
    embed_max_chars: int = 6000                       # nomic cap is 2048 tokens; TEI auto-truncates

    # --- LLM (cognify/curator only — never at read) --------------------------
    llm_url: str = "http://192.168.1.221:4000/v1"    # SAINT (workstation; nightly power-off)
    llm_model: str = "saint-cloud-medium"
    llm_timeout_s: float = 120.0
    llm_fallback_model: str = "claude-sonnet-4-6"    # direct Anthropic when SAINT unreachable
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")

    # --- retrieval defaults -------------------------------------------------
    recall_limit: int = 12
    recall_token_budget: int = 1200
    recall_min_score: float = 0.15
    recall_min_cosine: float = 0.48                   # nomic: unrelated text sits ~0.40-0.47; BM25 covers keyword hits
    vector_candidates: int = 60
    fts_candidates: int = 40
    graph_max_depth: int = 2
    graph_max_fanout: int = 20
    # score = relevance * recency * importance * trust (weights are exponents)
    w_recency: float = 0.6
    w_importance: float = 0.4
    w_trust: float = 0.7
    recency_half_life_days: float = 90.0              # facts; episodes use half of this
    contiguity_boost: float = 0.15

    # --- trust / confidence --------------------------------------------------
    trust_prior_human: float = 1.0
    trust_prior_doc: float = 0.85
    trust_prior_tool: float = 0.7
    trust_prior_inferred: float = 0.5
    confidence_floor: float = 0.05
    confidence_cap: float = 0.98
    confidence_staging_threshold: float = 0.35       # below → staging, not active
    belief_half_life_days: float = 45.0

    # --- workers -------------------------------------------------------------
    worker_enabled: bool = True
    cognify_poll_s: float = 5.0
    cognify_batch: int = 4
    curator_interval_min: int = 60
    backup_enabled: bool = True
    backup_hour_local: int = 3
    backup_keep: int = 14
    backup_dir: str = "/backups"
    working_window_turns: int = 20
    working_window_hours: int = 12
    decay_archive_threshold: float = 0.08

    # --- service -------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8933
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
