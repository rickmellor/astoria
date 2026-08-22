"""Postgres access: one psycopg3 pool, pgvector registration, idempotent migrations.

Schema files live in astoria/sql/NNN_name.sql and are applied in order at startup;
each file records itself in schema_migrations, so re-running is a no-op.
"""
from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from importlib import resources
from typing import Iterator

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from astoria.config import settings

log = logging.getLogger("astoria.db")
_pool: ConnectionPool | None = None


def _configure(conn: psycopg.Connection) -> None:
    register_vector(conn)
    conn.row_factory = dict_row


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        s = settings()
        _pool = ConnectionPool(
            s.db_dsn, min_size=s.db_pool_min, max_size=s.db_pool_max,
            configure=_configure, kwargs={"autocommit": False}, open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def conn() -> Iterator[psycopg.Connection]:
    """A pooled connection inside a transaction: commit on success, rollback on error."""
    with pool().connection() as c:
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise


def migrate() -> list[str]:
    """Apply any unapplied astoria/sql/*.sql in lexical order. Returns versions applied."""
    applied: list[str] = []
    files = sorted(
        (p for p in resources.files("astoria.sql").iterdir() if p.name.endswith(".sql")),
        key=lambda p: p.name,
    )
    with pool().connection() as c:
        c.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        c.commit()
        done = {r["version"] for r in c.execute("SELECT version FROM schema_migrations").fetchall()}
        for f in files:
            version = re.sub(r"\.sql$", "", f.name)
            if version in done:
                continue
            sql = f.read_text(encoding="utf-8")
            log.info("applying migration %s", version)
            c.execute(sql)
            c.execute("INSERT INTO schema_migrations(version) VALUES (%s) ON CONFLICT DO NOTHING", (version,))
            c.commit()
            applied.append(version)
    return applied


def healthcheck() -> dict:
    with pool().connection() as c:
        r = c.execute("SELECT count(*) AS n FROM fact WHERE status='active'").fetchone()
        e = c.execute("SELECT count(*) AS n FROM episode WHERE status='active'").fetchone()
        q = c.execute("SELECT count(*) AS n FROM cognify_queue WHERE state IN ('pending','failed')").fetchone()
        v = c.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()
    return {"facts_active": r["n"], "episodes_active": e["n"], "cognify_pending": q["n"],
            "pgvector": v["extversion"] if v else None}
