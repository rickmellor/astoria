"""Shared fixtures for the Astoria acceptance suite.

Two test planes:
  * REST plane — `client` (httpx) against ASTORIA_URL (default http://127.0.0.1:8977; NAS is
    http://192.168.1.134:8933). API tests skip cleanly when /health is unreachable.
  * Store plane — `db` (psycopg, dict rows) against ASTORIA_DB_DSN (default local dev DB
    postgresql://astoria:astoria@127.0.0.1:55432/astoria). Store tests skip when the DB is down.

ASTORIA_DIRECT_DB=1|0 declares whether the DSN points at the SAME database the API serves
(unset → auto: true iff the API host is loopback and the DB connects). Tests that need to peek/poke
under the API (tombstone via store, staging inserts, bulk seeding) use `direct_db`.

Every test gets a throwaway `user_id = test-<hex8>` that is wiped at teardown through
`DELETE /users/{user_id}` (and directly in the DB when direct_db) — safe to run against any deployment.
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
import pytest

DEFAULT_URL = "http://127.0.0.1:8977"
DEFAULT_DSN = "postgresql://astoria:astoria@127.0.0.1:55432/astoria"
TEST_CLIENT = "test"


# ---------------------------------------------------------------------------
# pytest plumbing

def pytest_addoption(parser):
    parser.addoption("--run-slow", action="store_true", default=False,
                     help="run @pytest.mark.slow tests (10k seed + latency)")


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: scale / latency tests (run with --run-slow or ASTORIA_RUN_SLOW=1)")
    config.addinivalue_line("markers", "api: needs a reachable Astoria REST server")
    config.addinivalue_line("markers", "store: needs a reachable Postgres (direct store)")


def pytest_collection_modifyitems(config, items):
    run_slow = config.getoption("--run-slow") or os.environ.get("ASTORIA_RUN_SLOW", "").lower() in ("1", "true", "yes")
    if run_slow:
        return
    skip = pytest.mark.skip(reason="slow test: pass --run-slow or ASTORIA_RUN_SLOW=1")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)


# ---------------------------------------------------------------------------
# REST plane

@pytest.fixture(scope="session")
def base_url() -> str:
    return os.environ.get("ASTORIA_URL", DEFAULT_URL).rstrip("/")


@pytest.fixture(scope="session")
def health(base_url) -> dict | None:
    """/health JSON if the server answers 200, else None (cached for the session)."""
    try:
        r = httpx.get(f"{base_url}/health", timeout=5.0)
        if r.status_code == 200:
            return r.json()
    except Exception:  # noqa: BLE001
        pass
    return None


@pytest.fixture
def skip_if_no_server(health, base_url):
    if health is None:
        pytest.skip(f"Astoria API not reachable at {base_url}/health")
    return health


@pytest.fixture
def tei_ok(health) -> bool:
    """True when the server reports a healthy embedding backend (vector assertions are meaningful)."""
    tei = (health or {}).get("tei") or {}
    return bool(tei.get("ok"))


def make_client(base_url: str, client_name: str = TEST_CLIENT, timeout: float = 60.0) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=timeout,
                        headers={"X-Astoria-Client": client_name, "Accept": "application/json"})


@pytest.fixture
def client(base_url, skip_if_no_server):
    with make_client(base_url) as c:
        yield c


@pytest.fixture
def client_as(base_url, skip_if_no_server) -> Callable[[str], httpx.Client]:
    """Factory: an httpx.Client impersonating another client name (X-Astoria-Client hint)."""
    opened: list[httpx.Client] = []

    def _mk(name: str) -> httpx.Client:
        c = make_client(base_url, name)
        opened.append(c)
        return c

    yield _mk
    for c in opened:
        c.close()


# ---------------------------------------------------------------------------
# Store plane

@pytest.fixture(scope="session")
def dsn() -> str:
    return os.environ.get("ASTORIA_DB_DSN") or os.environ.get("ASTORIA_DSN") or DEFAULT_DSN


def store_connect(dsn: str):
    """A psycopg connection shaped like db.conn(): dict rows + pgvector registered, autocommit OFF."""
    import psycopg
    from psycopg.rows import dict_row
    c = psycopg.connect(dsn, row_factory=dict_row, autocommit=False, connect_timeout=5)
    try:
        from pgvector.psycopg import register_vector
        register_vector(c)
    except Exception:  # noqa: BLE001 — vector ext missing; tests that need it will fail loudly
        pass
    return c


@pytest.fixture(scope="session")
def db_available(dsn) -> bool:
    try:
        c = store_connect(dsn)
        try:
            c.execute("SELECT 1 FROM schema_migrations LIMIT 1")
        finally:
            c.close()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture
def db(dsn, db_available):
    """A direct store connection (skips when the DB is unreachable). Commits at teardown."""
    if not db_available:
        pytest.skip(f"Postgres not reachable at {dsn}")
    # make sure facts.py's settings() reads the same DSN we use (it only needs the confidence knobs,
    # but keep it coherent for anything that opens db.conn()).
    os.environ.setdefault("ASTORIA_DB_DSN", dsn)
    c = store_connect(dsn)
    try:
        yield c
        try:
            c.commit()
        except Exception:  # noqa: BLE001
            c.rollback()
    finally:
        c.close()


@pytest.fixture(scope="session")
def direct_db(base_url, db_available) -> bool:
    """Does the DSN point at the database the API serves?"""
    flag = os.environ.get("ASTORIA_DIRECT_DB", "").strip().lower()
    if flag in ("1", "true", "yes"):
        return db_available
    if flag in ("0", "false", "no"):
        return False
    host = urlparse(base_url).hostname or ""
    return db_available and host in ("127.0.0.1", "localhost", "::1")


# ---------------------------------------------------------------------------
# Throwaway user + teardown

def wipe_user(user_id: str, *, base_url: str | None = None, dsn: str | None = None) -> None:
    """Best-effort wipe: DELETE /users/{id} via the API, then direct SQL (if a DSN is given)."""
    if base_url:
        try:
            httpx.delete(f"{base_url}/users/{user_id}", timeout=30.0,
                         headers={"X-Astoria-Client": TEST_CLIENT})
        except Exception:  # noqa: BLE001
            pass
    if dsn:
        try:
            c = store_connect(dsn)
            try:
                for tbl in ("snapshot", "cognify_queue", "audit", "tombstone", "profile_history", "profile"):
                    c.execute(f"DELETE FROM {tbl} WHERE user_id=%s", (user_id,))
                # facts reference facts (supersedes); set DEFERRABLE handles it inside one txn
                c.execute("DELETE FROM fact WHERE user_id=%s", (user_id,))
                c.execute("DELETE FROM episode WHERE user_id=%s", (user_id,))
                c.commit()
            finally:
                c.close()
        except Exception:  # noqa: BLE001
            pass


def new_user_id(prefix: str = "test") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def user_id(base_url, health, dsn, db_available, direct_db):
    """Throwaway user; wiped at teardown through the API (when up) and the DB (when it is the same DB)."""
    uid = new_user_id()
    yield uid
    wipe_user(uid, base_url=base_url if health is not None else None,
              dsn=dsn if (db_available and (direct_db or health is None)) else None)


@pytest.fixture
def store_user_id(dsn, db_available):
    """Throwaway user for pure store-level tests (no API needed); wiped directly in the DB."""
    if not db_available:
        pytest.skip(f"Postgres not reachable at {dsn}")
    uid = new_user_id("store")
    yield uid
    wipe_user(uid, dsn=dsn)


# ---------------------------------------------------------------------------
# Helpers

def active_facts(client: httpx.Client, user_id: str, **params) -> list[dict]:
    r = client.get("/facts", params={"user_id": user_id, "status": "active", "limit": 200, **params})
    r.raise_for_status()
    body = r.json()
    # tolerate {"facts":[...]} envelopes as well as the canonical bare list
    if isinstance(body, dict):
        body = body.get("facts") or body.get("items") or []
    return body


def wait_for_fact(client: httpx.Client, user_id: str, *, predicate: str, value: str | None = None,
                  subject: str | None = None, status: str = "active", timeout: float = 20.0,
                  interval: float = 0.5) -> dict | None:
    """Poll GET /facts until a fact (predicate[, value][, subject]) with `status` appears; None on timeout.

    Useful when the write lands asynchronously (cognify worker) — the sync paths don't need it.
    """
    deadline = time.monotonic() + timeout
    want = (value or "").strip().lower()
    while True:
        try:
            params: dict[str, Any] = {"user_id": user_id, "predicate": predicate, "status": status, "limit": 200}
            if subject:
                params["subject"] = subject
            r = client.get("/facts", params=params)
            if r.status_code == 200:
                rows = r.json()
                if isinstance(rows, dict):
                    rows = rows.get("facts") or rows.get("items") or []
                for row in rows:
                    if not want or str(row.get("value", "")).strip().lower() == want:
                        return row
        except Exception:  # noqa: BLE001
            pass
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def recall(client: httpx.Client, user_id: str, query: str, **extra) -> dict:
    r = client.post("/recall", json={"user_id": user_id, "query": query, **extra})
    assert r.status_code == 200, f"/recall {r.status_code}: {r.text[:400]}"
    return r.json()


def fact_items(res: dict) -> list[dict]:
    return [i for i in res.get("items", []) if i.get("kind", "fact") == "fact"]


def episode_items(res: dict) -> list[dict]:
    return [i for i in res.get("items", []) if i.get("kind") == "episode"]
