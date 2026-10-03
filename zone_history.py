"""Append-only timezone history for recurring series.

A timezone move keeps the local wall time and the recurrence phase; only the
zone that interprets them changes.  Because a zone change applies *from a
local date onward*, the series' current zone alone is not enough to rebuild
past (or already persisted) occurrences: every date must resolve through the
zone that was in effect for that local date.  This module stores an
append-only, revisioned history of those changes.

Revision 0 is the series' creation zone and is seeded with the first
version's ``starts_on`` date; every accepted change appends the next
revision.

Rules:

* A change applies to local dates ``>= effective_date``; earlier dates keep
  resolving through the earlier zone.
* ``expected_revision`` is an optimistic-concurrency baseline.  The baseline
  is the ``zone_revision`` currently in force (0 before any change, ``n``
  after the nth change).  A stale baseline rejects the whole change.
* Re-materialization runs over the *full* affected window (every persisted
  instance from the effective date through the last persisted one), not just
  the effective day, so stale UTC instants cannot survive the change.
  Locked instances (confirmed or reminder already sent) are immutable
  evidence and are skipped by the materializer.  Dates beyond the persisted
  window are resolved on demand by later ticks/queries through the history,
  so restarting the process cannot change any result.
* Everything happens in the caller's transaction: any failure rolls the
  schedule back to its previous state.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any


class StaleRevision(Exception):
    """Raised when a timezone change is based on an outdated revision."""


ZONE_HISTORY_SQL = """
CREATE TABLE IF NOT EXISTS series_timezone_history (
    id INTEGER PRIMARY KEY,
    series_id INTEGER NOT NULL REFERENCES series(id),
    zone_revision INTEGER NOT NULL,
    effective_date TEXT NOT NULL,
    timezone TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(series_id, zone_revision)
);

CREATE INDEX IF NOT EXISTS series_timezone_history_idx
    ON series_timezone_history(series_id, effective_date);
"""


def initialize(conn: sqlite3.Connection) -> None:
    """Create the history table and migrate older databases."""
    conn.executescript(ZONE_HISTORY_SQL)
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(occurrences)").fetchall()
    }
    if "zone_revision" not in columns:
        conn.execute(
            "ALTER TABLE occurrences ADD COLUMN zone_revision INTEGER NOT NULL DEFAULT 0"
        )
    # Backfill series created before this feature existed: their current zone
    # is the creation zone (revision 0), effective from their first version.
    conn.execute(
        """
        INSERT INTO series_timezone_history(
            series_id, zone_revision, effective_date, timezone, created_at
        )
        SELECT s.id, 0, v.starts_on, s.timezone, s.created_at
        FROM series s
        JOIN series_versions v
          ON v.series_id = s.id AND v.version_no = 1
        WHERE NOT EXISTS (
            SELECT 1 FROM series_timezone_history h
            WHERE h.series_id = s.id AND h.zone_revision = 0
        )
        """
    )


def seed_series(
    conn: sqlite3.Connection,
    series_id: int,
    timezone: str,
    effective_date: date,
    created_at: str,
) -> None:
    """Record the creation zone as revision 0 (called inside create_series)."""
    conn.execute(
        """
        INSERT INTO series_timezone_history(
            series_id, zone_revision, effective_date, timezone, created_at
        ) VALUES (?, 0, ?, ?, ?)
        """,
        (series_id, effective_date.isoformat(), timezone, created_at),
    )


def current_revision(conn: sqlite3.Connection, series_id: int) -> int:
    row = conn.execute(
        """
        SELECT zone_revision FROM series_timezone_history
        WHERE series_id = ?
        ORDER BY zone_revision DESC
        LIMIT 1
        """,
        (series_id,),
    ).fetchone()
    return 0 if row is None else row["zone_revision"]


def set_timezone(
    service: Any,
    series_id: int,
    timezone: str,
    effective_date: date,
    expected_revision: int = 0,
) -> dict[str, str | int]:
    # Fail early for an unknown timezone before touching any row.
    service.timezones.get(timezone)

    with service._connect() as conn:
        now = service._now(conn)
        with service._transaction(conn):
            series = conn.execute(
                "SELECT * FROM series WHERE id = ?", (series_id,)
            ).fetchone()
            if series is None:
                raise KeyError(f"unknown series {series_id}")

            revision = current_revision(conn, series_id)
            if expected_revision != revision:
                raise StaleRevision(
                    f"expected zone revision {revision}, got {expected_revision}"
                )

            next_revision = revision + 1
            effective_text = effective_date.isoformat()

            # Insert the history row first so zone lookups see it while the
            # materializer rebuilds the affected window below.
            conn.execute(
                """
                INSERT INTO series_timezone_history(
                    series_id, zone_revision, effective_date, timezone, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (series_id, next_revision, effective_text, timezone, now.isoformat()),
            )
            conn.execute(
                "UPDATE series SET timezone = ? WHERE id = ?",
                (timezone, series_id),
            )

            # Rebuild every unlocked persisted instance on/after the effective
            # date, including the effective day itself.  Anything farther in
            # the future has never been persisted; later ticks and queries
            # materialize it through the history, so results are identical
            # after a restart.
            last_row = conn.execute(
                """
                SELECT MAX(scheduled_local_date) AS last_date
                FROM occurrences WHERE series_id = ?
                """,
                (series_id,),
            ).fetchone()
            end = effective_date
            if last_row is not None and last_row["last_date"] is not None:
                end = max(end, date.fromisoformat(last_row["last_date"]))
            service._materialize_locked(conn, effective_date, end, now)

    return {"timezone": timezone, "zone_revision": next_revision}


def series_zone_entries(conn: sqlite3.Connection, series_id: int):
    """History rows for a series ordered by revision (then effective date)."""
    return conn.execute(
        """
        SELECT * FROM series_timezone_history
        WHERE series_id = ?
        ORDER BY zone_revision, effective_date
        """,
        (series_id,),
    ).fetchall()


def zone_at(conn: sqlite3.Connection, series: sqlite3.Row, day: date):
    """Resolve the timezone and its revision for a series local date.

    Kept for direct callers; the materializer batches the lookup via
    :func:`series_zone_entries`.
    """
    day_text = day.isoformat() if hasattr(day, "isoformat") else str(day)
    row = conn.execute(
        """
        SELECT timezone, zone_revision FROM series_timezone_history
        WHERE series_id = ? AND effective_date <= ?
        ORDER BY zone_revision DESC
        LIMIT 1
        """,
        (series["id"], day_text),
    ).fetchone()
    if row is None:
        return series["timezone"], 0
    return row["timezone"], row["zone_revision"]
