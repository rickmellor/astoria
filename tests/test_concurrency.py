"""Store-level concurrency: the supersession control plane must stay single-active under contention.

Runs straight against Postgres (no API, no TEI — `embed=False`), one psycopg connection per worker,
20 workers hammering the same functional key. The per-key `pg_advisory_xact_lock` + the partial unique
indexes in 001_schema.sql are what make these pass; if either regresses you get duplicate actives or
unique-violation errors here.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from astoria.store import facts

from conftest import store_connect

pytestmark = pytest.mark.store

WORKERS = 20


def _upsert_in_own_conn(dsn: str, **kw) -> dict:
    c = store_connect(dsn)
    try:
        res = facts.upsert_fact(c, embed=False, **kw)
        c.commit()
        return {"action": res["action"], "id": str(res["fact"]["id"]) if res["fact"] else None}
    except Exception as e:  # noqa: BLE001
        c.rollback()
        return {"action": "error", "error": repr(e)}
    finally:
        c.close()


def _run(dsn: str, jobs: list[dict]) -> list[dict]:
    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(_upsert_in_own_conn, dsn, **j) for j in jobs]
        for f in as_completed(futs):
            out.append(f.result())
    return out


def _rows(db, user_id: str, subject: str, predicate: str) -> list[dict]:
    return db.execute("SELECT * FROM fact WHERE user_id=%s AND subject=%s AND predicate=%s",
                      (user_id, subject, predicate)).fetchall()


def test_concurrent_distinct_values_one_active(dsn, db, store_user_id):
    """20 concurrent upserts on ONE functional key with 20 distinct values → exactly 1 active row,
    20 rows total, every non-active row is superseded (has superseded_by) or historical; no errors."""
    uid = store_user_id
    jobs = [dict(user_id=uid, subject=uid, predicate="favorite_beer", value=f"beer-{i:02d}",
                 source="test", source_kind="explicit") for i in range(WORKERS)]
    results = _run(dsn, jobs)
    errors = [r for r in results if r["action"] == "error"]
    assert not errors, f"upsert errors under contention: {errors[:3]}"

    rows = [r for r in _rows(db, uid, uid, "favorite_beer") if not (r["meta"] or {}).get("version_of")]  # logical rows (belief-axis copies excluded)
    assert len(rows) == WORKERS, f"expected {WORKERS} rows, got {len(rows)}"
    active = [r for r in rows if r["status"] == "active"]
    assert len(active) == 1, f"expected exactly one active, got {[r['value'] for r in active]}"
    for r in rows:
        if r["status"] == "active":
            continue
        assert r["status"] == "superseded", f"non-active row has status {r['status']}"
        # a superseded row points at its successor (chain) or was a historical insert with a closed window
        assert r["superseded_by"] is not None or r["valid_to"] is not None, r
        assert r["expired_at"] is not None or r["valid_to"] is not None, r
    # the chain is consistent: every superseded_by points at a row that exists for this key
    ids = {str(r["id"]) for r in rows}
    for r in rows:
        if r["superseded_by"] is not None:
            assert str(r["superseded_by"]) in ids
    # the winner is the newest assertion
    newest = max(rows, key=lambda r: (r["asserted_at"], r["ingested_at"]))
    assert newest["status"] == "active" or any(
        str(a["id"]) == str(newest["superseded_by"]) for a in active), "newest assertion should be (or point at) the active row"


def test_concurrent_identical_upserts_one_row(dsn, db, store_user_id):
    """20 concurrent IDENTICAL upserts → a single row (idempotent NOOP path), access_count bumped."""
    uid = store_user_id
    jobs = [dict(user_id=uid, subject=uid, predicate="favorite_editor", value="Neovim",
                 source="test", source_kind="explicit") for _ in range(WORKERS)]
    results = _run(dsn, jobs)
    errors = [r for r in results if r["action"] == "error"]
    assert not errors, f"upsert errors under contention: {errors[:3]}"
    actions = sorted(r["action"] for r in results)
    assert actions.count("inserted") == 1, actions
    assert actions.count("noop") == WORKERS - 1, actions

    rows = [r for r in _rows(db, uid, uid, "favorite_editor") if not (r["meta"] or {}).get("version_of")]  # logical rows (belief-axis copies excluded)
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
    assert rows[0]["status"] == "active"
    assert rows[0]["access_count"] == WORKERS - 1, rows[0]["access_count"]


def test_concurrent_set_valued_distinct_all_active(dsn, db, store_user_id):
    """10 concurrent distinct members of a SET predicate → 10 active rows (no supersession)."""
    uid = store_user_id
    n = 10
    jobs = [dict(user_id=uid, subject=uid, predicate="likes", value=f"thing-{i}",
                 source="test", source_kind="explicit") for i in range(n)]
    results = _run(dsn, jobs)
    errors = [r for r in results if r["action"] == "error"]
    assert not errors, f"upsert errors under contention: {errors[:3]}"
    assert all(r["action"] == "inserted" for r in results), sorted(r["action"] for r in results)
    rows = [r for r in _rows(db, uid, uid, "likes") if not (r["meta"] or {}).get("version_of")]  # logical rows (belief-axis copies excluded)
    assert len(rows) == n
    assert all(r["status"] == "active" for r in rows)
    assert len({r["value_norm"] for r in rows}) == n


def test_concurrent_mixed_set_dupes(dsn, db, store_user_id):
    """Set predicate: 20 concurrent upserts over 5 distinct values → 5 active rows, 15 noops."""
    uid = store_user_id
    jobs = [dict(user_id=uid, subject=uid, predicate="uses_tool", value=f"tool-{i % 5}",
                 source="test", source_kind="explicit") for i in range(WORKERS)]
    results = _run(dsn, jobs)
    errors = [r for r in results if r["action"] == "error"]
    assert not errors, errors[:3]
    rows = [r for r in _rows(db, uid, uid, "uses_tool") if not (r["meta"] or {}).get("version_of")]  # logical rows (belief-axis copies excluded)
    assert len(rows) == 5
    assert all(r["status"] == "active" for r in rows)
    assert sum(1 for r in results if r["action"] == "noop") == WORKERS - 5


def test_concurrent_correct_then_retract_race(dsn, db, store_user_id):
    """Interleaved upserts + retracts on a functional key never leave two actives and never error."""
    uid = store_user_id
    facts.upsert_fact(db, user_id=uid, subject=uid, predicate="current_focus", value="seed",
                      source="test", embed=False)
    db.commit()

    def worker(i: int) -> dict:
        c = store_connect(dsn)
        try:
            if i % 4 == 3:
                rows = facts.retract(c, user_id=uid, subject=uid, predicate="current_focus")
                c.commit()
                return {"action": "retract", "n": len(rows)}
            res = facts.upsert_fact(c, user_id=uid, subject=uid, predicate="current_focus", value=f"v{i}",
                                    source="test", embed=False)
            c.commit()
            return {"action": res["action"]}
        except Exception as e:  # noqa: BLE001
            c.rollback()
            return {"action": "error", "error": repr(e)}
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(worker, range(WORKERS)))
    errors = [r for r in results if r["action"] == "error"]
    assert not errors, errors[:3]
    rows = [r for r in _rows(db, uid, uid, "current_focus") if not (r["meta"] or {}).get("version_of")]  # logical rows (belief-axis copies excluded)
    assert sum(1 for r in rows if r["status"] == "active") <= 1
    assert len(rows) == 1 + sum(1 for i in range(WORKERS) if i % 4 != 3)
