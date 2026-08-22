"""Belief-axis versioning: as_of(at, as_believed_at=B) must answer what we believed at B."""
import uuid
from datetime import datetime, timedelta, timezone

from astoria.store import db, facts

UTC = timezone.utc


def _u():
    return "t-belief-" + uuid.uuid4().hex[:6]


def test_supersede_versions_old_row_and_belief_axis():
    u = _u()
    jul15 = datetime(2026, 7, 15, tzinfo=UTC)
    with db.conn() as c:
        g = facts.upsert_fact(c, user_id=u, subject=u, predicate="favorite_beer", value="Guinness",
                              source="cli", valid_from=jul15, asserted_at=jul15, embed=False)["fact"]
    t_between = datetime.now(UTC)
    with db.conn() as c:
        ipa = facts.upsert_fact(c, user_id=u, subject=u, predicate="favorite_beer", value="IPA",
                                source="cli", embed=False)
        assert ipa["action"] == "superseded"
        new = ipa["fact"]
        rows = c.execute("SELECT * FROM fact WHERE user_id=%s ORDER BY ingested_at", (u,)).fetchall()
        assert len(rows) == 3                                   # original, versioned copy, new
        orig = next(r for r in rows if r["id"] == g["id"])
        copy = next(r for r in rows if (r["meta"] or {}).get("version_of") == str(g["id"]))
        assert orig["status"] == "superseded" and orig["expired_at"] is not None and orig["valid_to"] is None
        assert copy["status"] == "superseded" and copy["expired_at"] is None and copy["valid_to"] is not None
        assert str(new["supersedes"]) == str(copy["id"])
        # current belief: IPA now, Guinness for the past
        now = datetime.now(UTC)
        assert [r["value"] for r in facts.as_of(c, user_id=u, at=now, predicate="favorite_beer")] == ["IPA"]
        assert [r["value"] for r in facts.as_of(c, user_id=u, at=datetime(2026, 8, 1, tzinfo=UTC),
                                                predicate="favorite_beer")] == ["Guinness"]
        # belief axis: what did we believe at t_between (before the correction)? Guinness, as current.
        got = facts.as_of(c, user_id=u, at=now, as_believed_at=t_between, predicate="favorite_beer")
        assert [r["value"] for r in got] == ["Guinness"]
        # and before the first assertion was ingested → nothing
        assert facts.as_of(c, user_id=u, at=now, as_believed_at=jul15 - timedelta(days=1), predicate="favorite_beer") == []
        # history hides the belief-closed original: exactly 2 entries (IPA, Guinness copy)
        h = facts.history(c, user_id=u, subject=u, predicate="favorite_beer")
        assert [r["value"] for r in h] == ["IPA", "Guinness"]
        assert len(facts.history(c, user_id=u, subject=u, predicate="favorite_beer", include_expired=True)) == 3
        c.execute("DELETE FROM fact WHERE user_id=%s", (u,)); c.execute("DELETE FROM audit WHERE user_id=%s", (u,))


def test_retract_is_belief_close_only():
    u = _u()
    with db.conn() as c:
        r = facts.upsert_fact(c, user_id=u, subject=u, predicate="likes", value="kayaking", source="cli", embed=False)["fact"]
    t_between = datetime.now(UTC)
    with db.conn() as c:
        facts.retract(c, user_id=u, subject=u, predicate="likes", value="kayaking", actor="cli")
        now = datetime.now(UTC)
        assert facts.as_of(c, user_id=u, at=now, predicate="likes") == []                       # not believed now
        assert [x["value"] for x in facts.as_of(c, user_id=u, at=now, as_believed_at=t_between, predicate="likes")] == ["kayaking"]
        row = facts.get_fact(c, user_id=u, fact_id=str(r["id"]))
        assert row["status"] == "retracted" and row["valid_to"] is None and row["expired_at"] is not None
        c.execute("DELETE FROM fact WHERE user_id=%s", (u,)); c.execute("DELETE FROM tombstone WHERE user_id=%s", (u,)); c.execute("DELETE FROM audit WHERE user_id=%s", (u,))
