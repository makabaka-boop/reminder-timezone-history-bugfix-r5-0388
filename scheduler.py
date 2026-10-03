"""SQLite-backed recurring calendar service.

The public API is intentionally small and framework-independent.  A
:class:`ScheduleService` owns the SQLite schema and the scheduling rules; callers
can put any HTTP layer in front of it.

Scheduled wall-clock times are resolved through a timezone object.  IANA
timezones are supported through :mod:`zoneinfo`, while tests can register fully
deterministic transition timezones with :class:`TimezoneRegistry`.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional
from zoneinfo import ZoneInfo


UTC = timezone.utc
REMINDER_LEAD = timedelta(minutes=15)


class Frequency(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"


class GapPolicy(str, Enum):
    SKIP = "skip"
    FORWARD = "forward"


class AmbiguousPolicy(str, Enum):
    FIRST = "first"
    SECOND = "second"


class ConflictingOccurrence(Exception):
    """Raised when an immutable occurrence would be changed."""


@dataclass(frozen=True)
class Transition:
    """A timezone transition, described in UTC and offsets around it."""

    at_utc: datetime
    offset_before: timedelta
    offset_after: timedelta


@dataclass(frozen=True)
class LocalResolution:
    scheduled_local: datetime
    local: datetime
    utc: Optional[datetime]
    utc_offset: Optional[timedelta]
    kind: str  # normal, ambiguous, gap_forward, gap_skipped
    ambiguous_occurrence: Optional[int] = None
    reason: Optional[str] = None

    @property
    def skipped(self) -> bool:
        return self.kind == "gap_skipped"


class Timezone:
    name = "UTC"

    def resolve(
        self,
        local: datetime,
        *,
        ambiguous: AmbiguousPolicy = AmbiguousPolicy.FIRST,
        gap: GapPolicy = GapPolicy.SKIP,
    ) -> LocalResolution:
        raise NotImplementedError


class ScriptedTimezone(Timezone):
    """A timezone with explicit transitions, useful for deterministic tests."""

    def __init__(
        self,
        name: str,
        transitions: Iterable[Transition],
        initial_offset: timedelta = timedelta(0),
    ) -> None:
        self.name = name
        self.transitions = tuple(sorted(transitions, key=lambda item: item.at_utc))
        self.initial_offset = initial_offset

    def offset_at_utc(self, value: datetime) -> timedelta:
        offset = self.initial_offset
        for transition in self.transitions:
            if value >= transition.at_utc:
                offset = transition.offset_after
            else:
                break
        return offset

    def resolve(
        self,
        local: datetime,
        *,
        ambiguous: AmbiguousPolicy = AmbiguousPolicy.FIRST,
        gap: GapPolicy = GapPolicy.SKIP,
    ) -> LocalResolution:
        offsets = {self.initial_offset}
        offsets.update(item.offset_before for item in self.transitions)
        offsets.update(item.offset_after for item in self.transitions)

        candidates: list[datetime] = []
        for offset in offsets:
            candidate_utc = local - offset
            if (
                self.offset_at_utc(candidate_utc) == offset
                and candidate_utc + offset == local
            ):
                if candidate_utc not in candidates:
                    candidates.append(candidate_utc)
        candidates.sort()

        if len(candidates) == 2:
            ordinal = 1 if ambiguous == AmbiguousPolicy.FIRST else 2
            selected = candidates[ordinal - 1]
            return LocalResolution(
                scheduled_local=local,
                local=local,
                utc=selected,
                utc_offset=self.offset_at_utc(selected),
                kind="ambiguous",
                ambiguous_occurrence=ordinal,
            )

        if len(candidates) == 1:
            selected = candidates[0]
            return LocalResolution(
                scheduled_local=local,
                local=local,
                utc=selected,
                utc_offset=self.offset_at_utc(selected),
                kind="normal",
            )

        return self._resolve_gap(local, gap)

    def _resolve_gap(self, local: datetime, gap: GapPolicy) -> LocalResolution:
        for transition in self.transitions:
            if transition.offset_after <= transition.offset_before:
                continue
            first_local = transition.at_utc + transition.offset_before
            second_local = transition.at_utc + transition.offset_after
            if first_local <= local < second_local:
                if gap == GapPolicy.SKIP:
                    return LocalResolution(
                        scheduled_local=local,
                        local=local,
                        utc=None,
                        utc_offset=None,
                        kind="gap_skipped",
                        reason="nonexistent_local_time",
                    )
                return LocalResolution(
                    scheduled_local=local,
                    local=second_local,
                    utc=transition.at_utc,
                    utc_offset=transition.offset_after,
                    kind="gap_forward",
                    reason="moved_forward_over_nonexistent_local_time",
                )

        # Defensive fallback.  A scripted timezone without a matching interval
        # has an invalid transition description.
        raise ValueError(f"No UTC mapping exists for {local.isoformat()}")


class ZoneInfoTimezone(Timezone):
    """Resolver backed by a real IANA zone.

    ``zoneinfo`` reports both possible folds, but does not expose a transition
    table directly.  Candidate UTC instants therefore identify normal and
    repeated times; a narrow binary search locates the transition for spring
    forward gaps.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._zone = ZoneInfo(name)

    @staticmethod
    def _epoch(value: datetime) -> float:
        return value.replace(tzinfo=UTC).timestamp()

    @staticmethod
    def _from_epoch(value: float) -> datetime:
        return datetime.fromtimestamp(value, UTC).replace(tzinfo=None)

    def _offset_at_utc(self, value: datetime) -> timedelta:
        aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value
        return aware.astimezone(self._zone).utcoffset()

    def resolve(
        self,
        local: datetime,
        *,
        ambiguous: AmbiguousPolicy = AmbiguousPolicy.FIRST,
        gap: GapPolicy = GapPolicy.SKIP,
    ) -> LocalResolution:
        candidates: list[datetime] = []
        seen_offsets: set[timedelta] = set()

        for fold in (0, 1):
            guessed = local.replace(tzinfo=self._zone, fold=fold)
            offset = guessed.utcoffset()
            if offset in seen_offsets:
                continue
            seen_offsets.add(offset)

            candidate_utc = local - offset
            back = candidate_utc.replace(tzinfo=UTC).astimezone(self._zone)
            back_local = back.replace(tzinfo=None)
            if back_local == local and back.utcoffset() == offset:
                if candidate_utc not in candidates:
                    candidates.append(candidate_utc)

        candidates.sort()

        if len(candidates) == 2:
            ordinal = 1 if ambiguous == AmbiguousPolicy.FIRST else 2
            selected = candidates[ordinal - 1]
            return LocalResolution(
                scheduled_local=local,
                local=local,
                utc=selected,
                utc_offset=self._offset_at_utc(selected),
                kind="ambiguous",
                ambiguous_occurrence=ordinal,
            )

        if len(candidates) == 1:
            selected = candidates[0]
            return LocalResolution(
                scheduled_local=local,
                local=local,
                utc=selected,
                utc_offset=self._offset_at_utc(selected),
                kind="normal",
            )

        if gap == GapPolicy.SKIP:
            return LocalResolution(
                scheduled_local=local,
                local=local,
                utc=None,
                utc_offset=None,
                kind="gap_skipped",
                reason="nonexistent_local_time",
            )

        transition_utc, new_offset = self._previous_transition(local)
        actual_local = transition_utc + new_offset
        return LocalResolution(
            scheduled_local=local,
            local=actual_local,
            utc=transition_utc,
            utc_offset=new_offset,
            kind="gap_forward",
            reason="moved_forward_over_nonexistent_local_time",
        )

    def _previous_transition(self, local: datetime) -> tuple[datetime, timedelta]:
        # For a gap, fold=0 maps the requested wall time to an instant after the
        # transition using the earlier (shorter UTC wall) interpretation.
        guessed = local.replace(tzinfo=self._zone, fold=0)
        upper = local - guessed.utcoffset()
        post_offset = self._offset_at_utc(upper)

        lower = upper - timedelta(hours=48)
        for _ in range(400):
            if self._offset_at_utc(lower) != post_offset:
                break
            lower -= timedelta(hours=48)
        else:  # pragma: no cover - all known zones transition within this window
            raise RuntimeError("Could not locate timezone transition")

        lo = self._epoch(lower)
        hi = self._epoch(upper)
        for _ in range(80):
            middle = (lo + hi) / 2
            if self._offset_at_utc(self._from_epoch(middle)) == post_offset:
                hi = middle
            else:
                lo = middle

        transition = self._from_epoch(round(hi, 6))
        # Rounding is exact for IANA transitions, which are second-granularity.
        if self._offset_at_utc(transition) != post_offset:
            transition += timedelta(microseconds=1)
        return transition, post_offset


class TimezoneRegistry:
    def __init__(self) -> None:
        self._zones: dict[str, Timezone] = {}

    def register(self, zone: Timezone) -> None:
        self._zones[zone.name] = zone

    def get(self, name: str) -> Timezone:
        if name in self._zones:
            return self._zones[name]
        return ZoneInfoTimezone(name)


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS series (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    timezone TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS series_versions (
    id INTEGER PRIMARY KEY,
    series_id INTEGER NOT NULL REFERENCES series(id),
    version_no INTEGER NOT NULL,
    effective_date TEXT NOT NULL,
    starts_on TEXT NOT NULL,
    until_date TEXT,
    frequency TEXT NOT NULL,
    interval INTEGER NOT NULL,
    local_time TEXT NOT NULL,
    ambiguous_policy TEXT NOT NULL,
    gap_policy TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(series_id, version_no),
    UNIQUE(series_id, effective_date)
);

CREATE TABLE IF NOT EXISTS exceptions (
    id INTEGER PRIMARY KEY,
    series_id INTEGER NOT NULL REFERENCES series(id),
    local_date TEXT NOT NULL,
    action TEXT NOT NULL,
    local_time TEXT,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(series_id, local_date)
);

CREATE TABLE IF NOT EXISTS occurrences (
    id INTEGER PRIMARY KEY,
    series_id INTEGER NOT NULL REFERENCES series(id),
    scheduled_local_date TEXT NOT NULL,
    scheduled_local_time TEXT NOT NULL,
    actual_local TEXT,
    utc_at TEXT,
    utc_offset TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    adjustment TEXT,
    ambiguous_occurrence INTEGER,
    version_id INTEGER NOT NULL REFERENCES series_versions(id),
    version_no INTEGER NOT NULL,
    exception_id INTEGER REFERENCES exceptions(id),
    exception_revision INTEGER,
    reminder_due_at TEXT,
    reminder_sent INTEGER NOT NULL DEFAULT 0,
    confirmed_at TEXT,
    materialized_at TEXT NOT NULL,
    UNIQUE(series_id, scheduled_local_date)
);

CREATE INDEX IF NOT EXISTS occurrences_reminder_idx
    ON occurrences(reminder_due_at, reminder_sent, status);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY,
    occurrence_id INTEGER NOT NULL UNIQUE REFERENCES occurrences(id),
    series_id INTEGER NOT NULL REFERENCES series(id),
    due_at TEXT NOT NULL,
    fired_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clock_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    now_utc TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class OccurrenceFingerprint:
    version_id: int
    exception_id: Optional[int]
    exception_revision: Optional[int]
    status: str
    scheduled_local_time: str
    actual_local: Optional[str]
    utc_at: Optional[str]
    utc_offset: Optional[str]
    reason: Optional[str]
    adjustment: Optional[str]
    ambiguous_occurrence: Optional[int]
    reminder_due_at: Optional[str]


class ScheduleService:
    def set_timezone(self, series_id, *, timezone, effective_date, expected_revision=0):
        from zone_history import set_timezone

        return set_timezone(
            self, series_id, timezone, effective_date, expected_revision
        )

    def __init__(
        self,
        database: str | Path,
        *,
        timezones: Optional[TimezoneRegistry] = None,
    ) -> None:
        self.database = str(database)
        self.timezones = timezones or TimezoneRegistry()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.database)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)
            from zone_history import initialize

            initialize(conn)
            conn.commit()

    @contextmanager
    def _transaction(self, conn: sqlite3.Connection) -> Iterator[None]:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def _iso(value: datetime) -> str:
        if value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value.isoformat()

    @staticmethod
    def _parse_utc(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed
        return parsed.astimezone(UTC).replace(tzinfo=None)

    def _now(self, conn: sqlite3.Connection) -> datetime:
        row = conn.execute(
            "SELECT now_utc FROM clock_state WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("controlled clock has not been initialized")
        return self._parse_utc(row["now_utc"])

    def initialize_clock(self, now_utc: datetime) -> datetime:
        """Set the first clock value without backfilling missed reminders."""
        now = self._as_naive_utc(now_utc)
        with self._connect() as conn:
            with self._transaction(conn):
                exists = conn.execute(
                    "SELECT 1 FROM clock_state WHERE singleton = 1"
                ).fetchone()
                if exists is not None:
                    raise RuntimeError(
                        "clock is already initialized; use advance_clock"
                    )
                conn.execute(
                    "INSERT INTO clock_state(singleton, now_utc) VALUES (1, ?)",
                    (now.isoformat(),),
                )
        return now

    def current_clock(self) -> Optional[datetime]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT now_utc FROM clock_state WHERE singleton = 1"
            ).fetchone()
        return None if row is None else self._parse_utc(row["now_utc"])

    @staticmethod
    def _as_naive_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value
        return value.astimezone(UTC).replace(tzinfo=None)

    @staticmethod
    def _validate_time(value: str) -> str:
        parsed = time.fromisoformat(value)
        return parsed.isoformat()

    def create_series(
        self,
        *,
        name: str,
        timezone: str,
        local_time: str,
        starts_on: date,
        frequency: Frequency | str = Frequency.DAILY,
        interval: int = 1,
        until_date: Optional[date] = None,
        ambiguous: AmbiguousPolicy | str = AmbiguousPolicy.FIRST,
        gap: GapPolicy | str = GapPolicy.SKIP,
    ) -> dict[str, Any]:
        local_time = self._validate_time(local_time)
        frequency = Frequency(frequency)
        ambiguous = AmbiguousPolicy(ambiguous)
        gap = GapPolicy(gap)
        if interval < 1:
            raise ValueError("interval must be positive")
        if until_date is not None and until_date < starts_on:
            raise ValueError("until_date cannot precede starts_on")
        # Fail early for an unknown timezone.
        self.timezones.get(timezone)

        with self._connect() as conn:
            now = self._now(conn)
            with self._transaction(conn):
                cur = conn.execute(
                    "INSERT INTO series(name, timezone, created_at) VALUES (?, ?, ?)",
                    (name, timezone, now.isoformat()),
                )
                series_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO series_versions(
                        series_id, version_no, effective_date, starts_on, until_date,
                        frequency, interval, local_time, ambiguous_policy, gap_policy,
                        created_at
                    ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        series_id,
                        starts_on.isoformat(),
                        starts_on.isoformat(),
                        None if until_date is None else until_date.isoformat(),
                        frequency.value,
                        interval,
                        local_time,
                        ambiguous.value,
                        gap.value,
                        now.isoformat(),
                    ),
                )
                from zone_history import record_baseline

                record_baseline(conn, series_id, timezone, now)
                version = conn.execute(
                    "SELECT * FROM series_versions WHERE series_id = ?",
                    (series_id,),
                ).fetchone()
        return self._series_version_payload(version)

    def modify_series(
        self,
        series_id: int,
        *,
        effective_date: date,
        local_time: Optional[str] = None,
        starts_on: Optional[date] = None,
        until_date: Optional[date] = None,
        frequency: Optional[Frequency | str] = None,
        interval: Optional[int] = None,
        ambiguous: Optional[AmbiguousPolicy | str] = None,
        gap: Optional[GapPolicy | str] = None,
    ) -> dict[str, Any]:
        """Create an immutable version effective on and after a local date."""
        with self._connect() as conn:
            now = self._now(conn)
            series = conn.execute(
                "SELECT * FROM series WHERE id = ?", (series_id,)
            ).fetchone()
            if series is None:
                raise KeyError(f"unknown series {series_id}")

            latest = conn.execute(
                """
                SELECT * FROM series_versions
                WHERE series_id = ?
                ORDER BY version_no DESC
                LIMIT 1
                """,
                (series_id,),
            ).fetchone()
            if date.fromisoformat(latest["effective_date"]) >= effective_date:
                raise ValueError("effective_date must be later than the latest version")

            values = {
                "starts_on": (
                    starts_on.isoformat() if starts_on else latest["starts_on"]
                ),
                "until_date": (
                    until_date.isoformat()
                    if until_date is not None
                    else latest["until_date"]
                ),
                "frequency": (
                    Frequency(frequency).value
                    if frequency is not None
                    else latest["frequency"]
                ),
                "interval": interval if interval is not None else latest["interval"],
                "local_time": (
                    self._validate_time(local_time)
                    if local_time is not None
                    else latest["local_time"]
                ),
                "ambiguous_policy": (
                    AmbiguousPolicy(ambiguous).value
                    if ambiguous is not None
                    else latest["ambiguous_policy"]
                ),
                "gap_policy": (
                    GapPolicy(gap).value if gap is not None else latest["gap_policy"]
                ),
            }
            if values["interval"] < 1:
                raise ValueError("interval must be positive")
            new_start = date.fromisoformat(values["starts_on"])
            new_until = (
                date.fromisoformat(values["until_date"])
                if values["until_date"]
                else None
            )
            if new_until is not None and new_until < new_start:
                raise ValueError("until_date cannot precede starts_on")

            with self._transaction(conn):
                version_no = latest["version_no"] + 1
                conn.execute(
                    """
                    INSERT INTO series_versions(
                        series_id, version_no, effective_date, starts_on, until_date,
                        frequency, interval, local_time, ambiguous_policy, gap_policy,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        series_id,
                        version_no,
                        effective_date.isoformat(),
                        values["starts_on"],
                        values["until_date"],
                        values["frequency"],
                        values["interval"],
                        values["local_time"],
                        values["ambiguous_policy"],
                        values["gap_policy"],
                        now.isoformat(),
                    ),
                )
                self._materialize_locked(conn, effective_date, effective_date, now)
                version = conn.execute(
                    """
                    SELECT * FROM series_versions
                    WHERE series_id = ? AND version_no = ?
                    """,
                    (series_id, version_no),
                ).fetchone()
        return self._series_version_payload(version)

    def set_exception(
        self,
        series_id: int,
        local_date: date,
        local_time: str,
    ) -> dict[str, Any]:
        return self._write_exception(
            series_id, local_date, action="override", local_time=local_time
        )

    def cancel_date(self, series_id: int, local_date: date) -> dict[str, Any]:
        return self._write_exception(
            series_id, local_date, action="cancel", local_time=None
        )

    def _write_exception(
        self,
        series_id: int,
        local_date: date,
        *,
        action: str,
        local_time: Optional[str],
    ) -> dict[str, Any]:
        if local_time is not None:
            local_time = self._validate_time(local_time)
        with self._connect() as conn:
            now = self._now(conn)
            with self._transaction(conn):
                series = conn.execute(
                    "SELECT timezone FROM series WHERE id = ?", (series_id,)
                ).fetchone()
                if series is None:
                    raise KeyError(f"unknown series {series_id}")
                latest = conn.execute(
                    """
                    SELECT * FROM series_versions
                    WHERE series_id = ? AND effective_date <= ?
                    ORDER BY effective_date DESC
                    LIMIT 1
                    """,
                    (series_id, local_date.isoformat()),
                ).fetchone()
                if latest is None or not self._belongs_to_version(latest, local_date):
                    raise ValueError("exception date is not part of this recurrence")

                locked = conn.execute(
                    """
                    SELECT id FROM occurrences
                    WHERE series_id = ? AND scheduled_local_date = ?
                      AND (reminder_sent = 1 OR confirmed_at IS NOT NULL)
                    """,
                    (series_id, local_date.isoformat()),
                ).fetchone()
                if locked is not None:
                    raise ConflictingOccurrence(
                        f"occurrence for {local_date.isoformat()} is already confirmed"
                    )

                existing = conn.execute(
                    "SELECT * FROM exceptions WHERE series_id = ? AND local_date = ?",
                    (series_id, local_date.isoformat()),
                ).fetchone()
                revision = 1 if existing is None else existing["revision"] + 1
                conn.execute(
                    """
                    INSERT INTO exceptions(
                        series_id, local_date, action, local_time, revision, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(series_id, local_date) DO UPDATE SET
                        action = excluded.action,
                        local_time = excluded.local_time,
                        revision = excluded.revision,
                        created_at = excluded.created_at
                    """,
                    (
                        series_id,
                        local_date.isoformat(),
                        action,
                        local_time,
                        revision,
                        now.isoformat(),
                    ),
                )
                self._materialize_locked(conn, local_date, local_date, now)
                row = conn.execute(
                    """
                    SELECT * FROM exceptions
                    WHERE series_id = ? AND local_date = ?
                    """,
                    (series_id, local_date.isoformat()),
                ).fetchone()
        return self._exception_payload(row)

    def confirm_occurrence(self, occurrence_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            now = self._now(conn)
            with self._transaction(conn):
                row = conn.execute(
                    "SELECT * FROM occurrences WHERE id = ?", (occurrence_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(f"unknown occurrence {occurrence_id}")
                if row["confirmed_at"] is None:
                    conn.execute(
                        "UPDATE occurrences SET confirmed_at = ? WHERE id = ?",
                        (now.isoformat(), occurrence_id),
                    )
                row = conn.execute(
                    "SELECT * FROM occurrences WHERE id = ?", (occurrence_id,)
                ).fetchone()
            return self._occurrence_payload(row, conn.execute)

    def list_instances(self, start: date, end: date) -> list[dict[str, Any]]:
        if end < start:
            raise ValueError("end cannot precede start")
        with self._connect() as conn:
            now = self._now(conn)
            with self._transaction(conn):
                self._materialize_locked(conn, start, end, now)
                rows = conn.execute(
                    """
                    SELECT * FROM occurrences
                    WHERE scheduled_local_date BETWEEN ? AND ?
                    ORDER BY scheduled_local_date, series_id, id
                    """,
                    (start.isoformat(), end.isoformat()),
                ).fetchall()
                payloads = [self._occurrence_payload(row, conn.execute) for row in rows]
        return payloads

    def advance_clock(self, new_now_utc: datetime) -> list[dict[str, Any]]:
        new_now = self._as_naive_utc(new_now_utc)
        with self._connect() as conn:
            with self._transaction(conn):
                old_row = conn.execute(
                    "SELECT now_utc FROM clock_state WHERE singleton = 1"
                ).fetchone()
                if old_row is None:
                    raise RuntimeError("initialize_clock must be called first")
                old_now = self._parse_utc(old_row["now_utc"])
                if new_now < old_now:
                    raise ValueError("controlled clock cannot move backwards")
                if new_now == old_now:
                    # Re-running the same tick must return the same unique records
                    # without inserting another reminder for any occurrence.
                    rows = conn.execute(
                        """
                        SELECT
                            r.id AS reminder_id,
                            r.occurrence_id AS occurrence_id,
                            r.series_id AS reminder_series_id,
                            r.due_at AS due_at,
                            r.fired_at AS fired_at,
                            o.series_id AS series_id,
                            o.scheduled_local_date AS scheduled_local_date,
                            o.scheduled_local_time AS scheduled_local_time,
                            o.actual_local AS actual_local,
                            o.utc_at AS utc_at,
                            o.status AS status,
                            o.reason AS reason,
                            o.version_id AS version_id,
                            o.version_no AS version_no,
                            o.exception_id AS exception_id,
                            o.exception_revision AS exception_revision
                        FROM reminders r
                        JOIN occurrences o ON o.id = r.occurrence_id
                        WHERE r.fired_at = ?
                        ORDER BY r.due_at, r.id
                        """,
                        (new_now.isoformat(),),
                    ).fetchall()
                    return [self._reminder_payload(row) for row in rows]

                # Events whose 15-minute reminder lies in this interval can be
                # roughly two calendar days away at the date level after DST and
                # international offsets.  Resolving the actual wall times below
                # supplies the exact UTC boundary.
                local_start = (old_now + REMINDER_LEAD).date() - timedelta(days=2)
                local_end = (new_now + REMINDER_LEAD).date() + timedelta(days=2)
                self._materialize_locked(conn, local_start, local_end, new_now)

                conn.execute(
                    "UPDATE clock_state SET now_utc = ? WHERE singleton = 1",
                    (new_now.isoformat(),),
                )

                due = conn.execute(
                    """
                    SELECT id, series_id, reminder_due_at
                    FROM occurrences
                    WHERE status = 'scheduled'
                      AND reminder_sent = 0
                      AND reminder_due_at IS NOT NULL
                      AND reminder_due_at > ?
                      AND reminder_due_at <= ?
                    """,
                    (old_now.isoformat(), new_now.isoformat()),
                ).fetchall()
                for row in due:
                    conn.execute(
                        """
                        INSERT INTO reminders(
                            occurrence_id, series_id, due_at, fired_at, created_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            row["id"],
                            row["series_id"],
                            row["reminder_due_at"],
                            new_now.isoformat(),
                            new_now.isoformat(),
                        ),
                    )
                    conn.execute(
                        "UPDATE occurrences SET reminder_sent = 1 WHERE id = ?",
                        (row["id"],),
                    )

                reminders = conn.execute(
                    """
                    SELECT
                        r.id AS reminder_id,
                        r.occurrence_id AS occurrence_id,
                        r.series_id AS reminder_series_id,
                        r.due_at AS due_at,
                        r.fired_at AS fired_at,
                        o.series_id AS series_id,
                        o.scheduled_local_date AS scheduled_local_date,
                        o.scheduled_local_time AS scheduled_local_time,
                        o.actual_local AS actual_local,
                        o.utc_at AS utc_at,
                        o.status AS status,
                        o.reason AS reason,
                        o.version_id AS version_id,
                        o.version_no AS version_no,
                        o.exception_id AS exception_id,
                        o.exception_revision AS exception_revision
                    FROM reminders r
                    JOIN occurrences o ON o.id = r.occurrence_id
                    WHERE r.fired_at = ?
                    ORDER BY r.due_at, r.id
                    """,
                    (new_now.isoformat(),),
                ).fetchall()
                result = [self._reminder_payload(row) for row in reminders]
        return result

    def list_reminders(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    r.id AS reminder_id,
                    r.occurrence_id AS occurrence_id,
                    r.series_id AS reminder_series_id,
                    r.due_at AS due_at,
                    r.fired_at AS fired_at,
                    o.series_id AS series_id,
                    o.scheduled_local_date AS scheduled_local_date,
                    o.scheduled_local_time AS scheduled_local_time,
                    o.actual_local AS actual_local,
                    o.utc_at AS utc_at,
                    o.status AS status,
                    o.reason AS reason,
                    o.version_id AS version_id,
                    o.version_no AS version_no,
                    o.exception_id AS exception_id,
                    o.exception_revision AS exception_revision
                FROM reminders r
                JOIN occurrences o ON o.id = r.occurrence_id
                ORDER BY r.due_at, r.id
                """
            ).fetchall()
        return [self._reminder_payload(row) for row in rows]

    def _materialize_locked(
        self,
        conn: sqlite3.Connection,
        start: date,
        end: date,
        now: datetime,
    ) -> None:
        series_rows = conn.execute("SELECT * FROM series").fetchall()
        existing_rows = conn.execute(
            """
            SELECT * FROM occurrences
            WHERE scheduled_local_date BETWEEN ? AND ?
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        exceptions = conn.execute(
            """
            SELECT * FROM exceptions
            WHERE local_date BETWEEN ? AND ?
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchall()

        existing: dict[tuple[int, str], sqlite3.Row] = {
            (row["series_id"], row["scheduled_local_date"]): row
            for row in existing_rows
        }
        exception_map: dict[tuple[int, str], sqlite3.Row] = {
            (row["series_id"], row["local_date"]): row for row in exceptions
        }

        desired: dict[tuple[int, str], OccurrenceFingerprint] = {}
        details: dict[tuple[int, str], dict[str, Any]] = {}
        dates = self._date_range(start, end)

        for series in series_rows:
            versions = conn.execute(
                """
                SELECT * FROM series_versions
                WHERE series_id = ?
                ORDER BY effective_date
                """,
                (series["id"],),
            ).fetchall()

            for day in dates:
                effective_versions = [
                    row
                    for row in versions
                    if date.fromisoformat(row["effective_date"]) <= day
                ]
                if not effective_versions:
                    continue
                version = max(
                    effective_versions,
                    key=lambda row: date.fromisoformat(row["effective_date"]),
                )
                if not self._belongs_to_version(version, day):
                    # A day exception overrides an occurrence; it cannot create
                    # one on a date the selected series version never schedules.
                    continue

                key = (series["id"], day.isoformat())
                exception = exception_map.get(key)
                from zone_history import zone_at

                zone_name, zone_revision = zone_at(conn, series, day)
                zone = self.timezones.get(zone_name)
                detail = self._resolve_desired(
                    zone=zone,
                    version=version,
                    day=day,
                    exception=exception,
                    now=now,
                )
                desired[key] = self._fingerprint(detail)
                details[key] = detail

        for key, row in list(existing.items()):
            locked = row["reminder_sent"] == 1 or row["confirmed_at"] is not None
            if locked:
                # Immutable evidence: neither later versions nor day exceptions
                # rewrite a confirmed/reminded instance.
                continue
            if key not in desired:
                conn.execute("DELETE FROM occurrences WHERE id = ?", (row["id"],))
                continue
            if self._fingerprint_from_row(row) != desired[key]:
                conn.execute("DELETE FROM occurrences WHERE id = ?", (row["id"],))
                existing.pop(key, None)

        for key, detail in details.items():
            if key in existing:
                continue
            conn.execute(
                """
                INSERT INTO occurrences(
                    series_id, scheduled_local_date, scheduled_local_time,
                    actual_local, utc_at, utc_offset, status, reason, adjustment,
                    ambiguous_occurrence, version_id, version_no, exception_id,
                    exception_revision, reminder_due_at, materialized_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key[0],
                    key[1],
                    detail["scheduled_local_time"],
                    detail["actual_local"],
                    detail["utc_at"],
                    detail["utc_offset"],
                    detail["status"],
                    detail["reason"],
                    detail["adjustment"],
                    detail["ambiguous_occurrence"],
                    detail["version_id"],
                    detail["version_no"],
                    detail["exception_id"],
                    detail["exception_revision"],
                    detail["reminder_due_at"],
                    now.isoformat(),
                ),
            )

    @staticmethod
    def _date_range(start: date, end: date) -> list[date]:
        count = (end - start).days
        return [start + timedelta(days=index) for index in range(count + 1)]

    @staticmethod
    def _belongs_to_version(version: sqlite3.Row, day: date) -> bool:
        starts_on = date.fromisoformat(version["starts_on"])
        until_date = (
            date.fromisoformat(version["until_date"]) if version["until_date"] else None
        )
        if day < starts_on or (until_date is not None and day > until_date):
            return False
        if version["frequency"] == Frequency.DAILY.value:
            period = version["interval"]
            return (day - starts_on).days % period == 0
        if version["frequency"] == Frequency.WEEKLY.value:
            period = version["interval"] * 7
            return (day - starts_on).days % period == 0
        raise ValueError(f"unknown frequency {version['frequency']}")

    def _resolve_desired(
        self,
        *,
        zone: Timezone,
        version: sqlite3.Row,
        day: date,
        exception: Optional[sqlite3.Row],
        now: datetime,
    ) -> dict[str, Any]:
        scheduled_time = version["local_time"]
        status = "scheduled"
        reason: Optional[str] = None
        exception_id: Optional[int] = None
        exception_revision: Optional[int] = None

        if exception is not None:
            exception_id = exception["id"]
            exception_revision = exception["revision"]
            if exception["action"] == "cancel":
                return {
                    "scheduled_local_time": scheduled_time,
                    "actual_local": None,
                    "utc_at": None,
                    "utc_offset": None,
                    "status": "cancelled",
                    "reason": "exception_cancelled",
                    "adjustment": None,
                    "ambiguous_occurrence": None,
                    "version_id": version["id"],
                    "version_no": version["version_no"],
                    "exception_id": exception_id,
                    "exception_revision": exception_revision,
                    "reminder_due_at": None,
                }
            scheduled_time = exception["local_time"]

        wall_time = time.fromisoformat(scheduled_time)
        local = datetime.combine(day, wall_time)
        resolution = zone.resolve(
            local,
            ambiguous=AmbiguousPolicy(version["ambiguous_policy"]),
            gap=GapPolicy(version["gap_policy"]),
        )

        adjustment: Optional[str] = None
        ambiguous_occurrence: Optional[int] = None
        actual_local: Optional[str] = None
        utc_text: Optional[str] = None
        offset_text: Optional[str] = None
        due_text: Optional[str] = None

        if resolution.skipped:
            status = "skipped"
            reason = resolution.reason
        else:
            actual_local = resolution.local.isoformat()
            utc_text = resolution.utc.isoformat()
            offset_text = self._format_offset(resolution.utc_offset)
            due_text = (resolution.utc - REMINDER_LEAD).isoformat()
            ambiguous_occurrence = resolution.ambiguous_occurrence
            if resolution.kind == "gap_forward":
                adjustment = "gap_forward"
                reason = resolution.reason
            elif resolution.kind == "ambiguous":
                adjustment = "ambiguous_local_time"

        return {
            "scheduled_local_time": scheduled_time,
            "actual_local": actual_local,
            "utc_at": utc_text,
            "utc_offset": offset_text,
            "status": status,
            "reason": reason,
            "adjustment": adjustment,
            "ambiguous_occurrence": ambiguous_occurrence,
            "version_id": version["id"],
            "version_no": version["version_no"],
            "exception_id": exception_id,
            "exception_revision": exception_revision,
            "reminder_due_at": due_text,
        }

    @staticmethod
    def _format_offset(offset: timedelta) -> str:
        total_minutes = round(offset.total_seconds() / 60)
        sign = "-" if total_minutes < 0 else "+"
        total_minutes = abs(total_minutes)
        return f"{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"

    @staticmethod
    def _fingerprint(detail: dict[str, Any]) -> OccurrenceFingerprint:
        return OccurrenceFingerprint(
            version_id=detail["version_id"],
            exception_id=detail["exception_id"],
            exception_revision=detail["exception_revision"],
            status=detail["status"],
            scheduled_local_time=detail["scheduled_local_time"],
            actual_local=detail["actual_local"],
            utc_at=detail["utc_at"],
            utc_offset=detail["utc_offset"],
            reason=detail["reason"],
            adjustment=detail["adjustment"],
            ambiguous_occurrence=detail["ambiguous_occurrence"],
            reminder_due_at=detail["reminder_due_at"],
        )

    @staticmethod
    def _fingerprint_from_row(row: sqlite3.Row) -> OccurrenceFingerprint:
        return OccurrenceFingerprint(
            version_id=row["version_id"],
            exception_id=row["exception_id"],
            exception_revision=row["exception_revision"],
            status=row["status"],
            scheduled_local_time=row["scheduled_local_time"],
            actual_local=row["actual_local"],
            utc_at=row["utc_at"],
            utc_offset=row["utc_offset"],
            reason=row["reason"],
            adjustment=row["adjustment"],
            ambiguous_occurrence=row["ambiguous_occurrence"],
            reminder_due_at=row["reminder_due_at"],
        )

    @staticmethod
    def _series_version_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "series_id": row["series_id"],
            "version_id": row["id"],
            "version_no": row["version_no"],
            "effective_date": row["effective_date"],
            "starts_on": row["starts_on"],
            "until_date": row["until_date"],
            "frequency": row["frequency"],
            "interval": row["interval"],
            "local_time": row["local_time"],
            "ambiguous_policy": row["ambiguous_policy"],
            "gap_policy": row["gap_policy"],
        }

    @staticmethod
    def _exception_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "exception_id": row["id"],
            "series_id": row["series_id"],
            "local_date": row["local_date"],
            "action": row["action"],
            "local_time": row["local_time"],
            "revision": row["revision"],
        }

    @staticmethod
    def _utc_payload(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return f"{value}Z"

    def _occurrence_payload(
        self,
        row: sqlite3.Row,
        execute: Any = None,
    ) -> dict[str, Any]:
        scheduled_local_date = row["scheduled_local_date"]
        scheduled_time = row["scheduled_local_time"]
        return {
            "occurrence_id": row["id"],
            "series_id": row["series_id"],
            "scheduled_local": f"{scheduled_local_date}T{scheduled_time}",
            "actual_local": row["actual_local"],
            "utc_at": self._utc_payload(row["utc_at"]),
            "utc_offset": row["utc_offset"],
            "status": row["status"],
            "skip_reason": row["reason"] if row["status"] == "skipped" else None,
            "reason": row["reason"],
            "adjustment": row["adjustment"],
            "ambiguous_occurrence": row["ambiguous_occurrence"],
            "rule_version": {
                "version_id": row["version_id"],
                "version_no": row["version_no"],
                "exception_id": row["exception_id"],
                "exception_revision": row["exception_revision"],
            },
            "reminder_due_at": self._utc_payload(row["reminder_due_at"]),
            "reminder_sent": bool(row["reminder_sent"]),
            "confirmed_at": self._utc_payload(row["confirmed_at"]),
        }

    def _reminder_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        # sqlite3.Row contains duplicate column names from the join; access the
        # reminder columns explicitly and construct occurrence data from the
        # occurrence columns selected below.
        return {
            "reminder_id": row["reminder_id"],
            "occurrence_id": row["occurrence_id"],
            "series_id": row["series_id"],
            "due_at": f"{row['due_at']}Z",
            "fired_at": f"{row['fired_at']}Z",
            "scheduled_local": (
                f"{row['scheduled_local_date']}T{row['scheduled_local_time']}"
            ),
            "actual_local": row["actual_local"],
            "utc_at": self._utc_payload(row["utc_at"]),
            "rule_version": {
                "version_id": row["version_id"],
                "version_no": row["version_no"],
                "exception_id": row["exception_id"],
                "exception_revision": row["exception_revision"],
            },
            "skip_reason": row["reason"] if row["status"] == "skipped" else None,
        }
