"""Append-only timezone history for a recurring series.

A timezone move keeps the local wall clock time and the recurrence phase; only
the rule that resolves a local wall time to a UTC instant changes.  Every switch
is therefore stored as an immutable *zone revision* that applies from a
series-local date.  Occurrences (and single-day exceptions, which stay bound to
their original series-local date) are resolved against the zone in effect on
their own date::

    revision 0  -> zone at creation, effective since the calendar minimum
    revision 1  -> first move,  effective on a later local date
    revision 2  -> second move, effective on a still later local date

Revisions are optimistic-concurrency tokens: a caller submits the revision its
change is based on (``expected_revision``) and a stale base is rejected.

Confirmed occurrences and occurrences whose reminder has already fired are
never rebuilt, so a zone change only affects unlocked instances.
"""

from __future__ import annotations

from datetime import date
from typing import Any


def initialize(conn: Any) -> None:
    """Create the zone history table (idempotent across restarts)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS zone_versions (
            id INTEGER PRIMARY KEY,
            series_id INTEGER NOT NULL REFERENCES series(id),
            revision INTEGER NOT NULL,
            effective_date TEXT NOT NULL,
            timezone TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(series_id, revision),
            UNIQUE(series_id, effective_date)
        )
        """
    )


def record_baseline(conn: Any, series_id: int, timezone: str, now: Any) -> None:
    """Record the revision-0 zone a series is created with.

    The baseline is in effect from the dawn of the calendar: it predates every
    occurrence, so the first real move can take effect on any local date.
    """
    conn.execute(
        """
        INSERT INTO zone_versions(
            series_id, revision, effective_date, timezone, created_at
        ) VALUES (?, 0, ?, ?, ?)
        """,
        (series_id, date.min.isoformat(), timezone, now.isoformat()),
    )


def latest_revision(conn: Any, series_id: int) -> Any:
    return conn.execute(
        """
        SELECT * FROM zone_versions
        WHERE series_id = ?
        ORDER BY revision DESC
        LIMIT 1
        """,
        (series_id,),
    ).fetchone()


def zone_at(conn: Any, series: Any, day: date) -> tuple[str, int]:
    """Return ``(timezone name, revision)`` in effect for ``day``.

    The selected revision is the one with the greatest effective date not later
    than ``day``; the wall time and recurrence phase are untouched.
    """
    row = conn.execute(
        """
        SELECT timezone, revision
        FROM zone_versions
        WHERE series_id = ? AND effective_date <= ?
        ORDER BY effective_date DESC, revision DESC
        LIMIT 1
        """,
        (series["id"], day.isoformat()),
    ).fetchone()
    if row is None:
        # Databases created before zone history existed: the series column
        # holds the original revision-0 zone.
        return series["timezone"], 0
    return row["timezone"], row["revision"]


def set_timezone(
    service: Any,
    series_id: int,
    timezone: str,
    effective_date: date,
    expected_revision: int = 0,
) -> dict[str, Any]:
    """Switch ``series_id`` to ``timezone`` from local date ``effective_date``.

    The wall-clock time, recurrence interval/phase, and date-bound exceptions
    are unchanged; only UTC resolution changes.  Already confirmed or
    reminder-sent occurrences keep their original evidence.  Every other
    already-persisted occurrence on or after the effective date is rebuilt in
    the same transaction, so a failed change never leaves a half-updated
    schedule.
    """
    if not isinstance(effective_date, date):
        raise TypeError("effective_date must be a datetime.date")
    # Resolve an unknown zone before opening the transaction so a bad zone name
    # fails without touching the schedule.
    service.timezones.get(timezone)

    with service._connect() as conn:
        now = service._now(conn)
        with service._transaction(conn):
            series = conn.execute(
                "SELECT * FROM series WHERE id = ?", (series_id,)
            ).fetchone()
            if series is None:
                raise KeyError(f"unknown series {series_id}")

            latest = latest_revision(conn, series_id)
            if latest is None:
                # Defensive seeding for a series written before history existed.
                record_baseline(conn, series_id, series["timezone"], now)
                current_revision = 0
                current_effective = date.min
            else:
                current_revision = latest["revision"]
                current_effective = date.fromisoformat(latest["effective_date"])

            if int(expected_revision) != current_revision:
                raise ValueError(
                    f"stale timezone revision: change based on "
                    f"{expected_revision!r}, current revision is "
                    f"{current_revision}"
                )
            if effective_date <= current_effective:
                raise ValueError(
                    "effective_date must be later than the current timezone "
                    "revision's effective date"
                )

            new_revision = current_revision + 1
            conn.execute(
                """
                INSERT INTO zone_versions(
                    series_id, revision, effective_date, timezone, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    series_id,
                    new_revision,
                    effective_date.isoformat(),
                    timezone,
                    now.isoformat(),
                ),
            )
            conn.execute(
                "UPDATE series SET timezone = ? WHERE id = ?",
                (timezone, series_id),
            )

            # Rebuild every *persisted* occurrence on and after the effective
            # date: the previous implementation only refreshed the effective
            # day, leaving later instances on the old zone until a query happened
            # to touch them.  Dates not materialized yet need nothing here -
            # zone_at resolves them lazily under the new zone.  Materialization
            # skips locked rows, preserving confirmed/sent evidence, and runs in
            # this same transaction so any failure rolls the whole change back.
            last_row = conn.execute(
                """
                SELECT MAX(scheduled_local_date) AS last_date
                FROM occurrences
                WHERE series_id = ?
                """,
                (series_id,),
            ).fetchone()
            end_date = (
                date.fromisoformat(last_row["last_date"])
                if last_row is not None and last_row["last_date"]
                else effective_date
            )
            service._materialize_locked(conn, effective_date, end_date, now)

    return {
        "timezone": timezone,
        "zone_revision": new_revision,
        "effective_date": effective_date.isoformat(),
    }
