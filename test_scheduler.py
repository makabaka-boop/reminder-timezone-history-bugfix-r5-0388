import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from scheduler import (
    AmbiguousPolicy,
    ConflictingOccurrence,
    Frequency,
    GapPolicy,
    ScheduleService,
    ScriptedTimezone,
    StaleRevision,
    TimezoneRegistry,
    Transition,
)


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def make_registry() -> TimezoneRegistry:
    registry = TimezoneRegistry()
    # Spring forward on 2024-03-10: local 02:00-02:59:59 does not exist.
    registry.register(
        ScriptedTimezone(
            "Test/Spring",
            [
                Transition(
                    at_utc=datetime(2024, 3, 10, 7, 0),
                    offset_before=timedelta(hours=-5),
                    offset_after=timedelta(hours=-4),
                )
            ],
            initial_offset=timedelta(hours=-5),
        )
    )
    # Fall back on 2024-11-03: local 01:00-01:59:59 is repeated.
    registry.register(
        ScriptedTimezone(
            "Test/Fall",
            [
                Transition(
                    at_utc=datetime(2024, 11, 3, 6, 0),
                    offset_before=timedelta(hours=-4),
                    offset_after=timedelta(hours=-5),
                )
            ],
            initial_offset=timedelta(hours=-4),
        )
    )
    registry.register(
        ScriptedTimezone("Test/Tokyo", [], initial_offset=timedelta(hours=9))
    )
    registry.register(
        ScriptedTimezone("Test/UTC", [], initial_offset=timedelta(0))
    )
    return registry


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "schedule.sqlite"
        self.registry = make_registry()
        self.service = ScheduleService(self.db_path, timezones=self.registry)
        self.service.initialize_clock(datetime(2024, 1, 1))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def restart(self) -> ScheduleService:
        return ScheduleService(self.db_path, timezones=self.registry)

    def instance_for(self, day: date):
        rows = self.service.list_instances(day, day)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_nonexistent_spring_forward_time_can_be_skipped(self) -> None:
        series = self.service.create_series(
            name="skip gap",
            timezone="Test/Spring",
            local_time="02:30",
            starts_on=date(2024, 3, 10),
            gap=GapPolicy.SKIP,
        )
        row = self.instance_for(date(2024, 3, 10))

        self.assertEqual(row["status"], "skipped")
        self.assertEqual(row["skip_reason"], "nonexistent_local_time")
        self.assertIsNone(row["utc_at"])
        self.assertIsNone(row["reminder_due_at"])
        self.assertEqual(row["rule_version"]["version_id"], series["version_id"])

    def test_nonexistent_spring_forward_time_moves_forward(self) -> None:
        self.service.create_series(
            name="forward gap",
            timezone="Test/Spring",
            local_time="02:30",
            starts_on=date(2024, 3, 10),
            gap=GapPolicy.FORWARD,
        )
        row = self.instance_for(date(2024, 3, 10))

        self.assertEqual(row["status"], "scheduled")
        self.assertEqual(row["scheduled_local"], "2024-03-10T02:30:00")
        self.assertEqual(row["actual_local"], "2024-03-10T03:00:00")
        self.assertEqual(row["utc_at"], "2024-03-10T07:00:00Z")
        self.assertEqual(row["utc_offset"], "-04:00")
        self.assertEqual(row["reason"], "moved_forward_over_nonexistent_local_time")

    def test_ambiguous_fall_back_time_uses_first_or_second_occurrence(self) -> None:
        first = self.service.create_series(
            name="first overlap",
            timezone="Test/Fall",
            local_time="01:30",
            starts_on=date(2024, 11, 2),
            ambiguous=AmbiguousPolicy.FIRST,
        )
        first_row = self.instance_for(date(2024, 11, 3))
        self.assertEqual(first_row["utc_at"], "2024-11-03T05:30:00Z")
        self.assertEqual(first_row["utc_offset"], "-04:00")
        self.assertEqual(first_row["ambiguous_occurrence"], 1)

        second_service = ScheduleService(
            Path(self.temp_dir.name) / "second.sqlite",
            timezones=self.registry,
        )
        second_service.initialize_clock(datetime(2024, 1, 1))
        second_service.create_series(
            name="second overlap",
            timezone="Test/Fall",
            local_time="01:30",
            starts_on=date(2024, 11, 2),
            ambiguous=AmbiguousPolicy.SECOND,
        )
        rows = second_service.list_instances(date(2024, 11, 3), date(2024, 11, 3))
        second_row = rows[0]
        self.assertEqual(second_row["utc_at"], "2024-11-03T06:30:00Z")
        self.assertEqual(second_row["utc_offset"], "-05:00")
        self.assertEqual(second_row["ambiguous_occurrence"], 2)
        self.assertNotEqual(
            first_row["utc_at"],
            second_row["utc_at"],
            "an ambiguous local time must not be reduced to one fixed offset",
        )
        self.assertEqual(first["version_no"], 1)

    def test_weekly_recurrence_and_cross_day_utc_reminder(self) -> None:
        # Tuesday weekly event at 23:50 local (-05) is Wednesday 04:50 UTC.
        self.service.create_series(
            name="weekly night",
            timezone="Test/Spring",
            local_time="23:50",
            starts_on=date(2024, 3, 19),  # Tuesday
            frequency=Frequency.WEEKLY,
            interval=2,
        )
        rows = self.service.list_instances(date(2024, 3, 5), date(2024, 3, 26))
        scheduled_days = [row["scheduled_local"][:10] for row in rows]
        self.assertEqual(scheduled_days, ["2024-03-19"])

        march19 = next(
            row for row in rows if row["scheduled_local"] == "2024-03-19T23:50:00"
        )
        self.assertEqual(march19["utc_at"], "2024-03-20T03:50:00Z")
        self.assertEqual(march19["reminder_due_at"], "2024-03-20T03:35:00Z")

        first_batch = self.service.advance_clock(datetime(2024, 3, 20, 3, 35))
        self.assertEqual(len(first_batch), 1)
        self.assertEqual(first_batch[0]["due_at"], "2024-03-20T03:35:00Z")

    def test_repeated_advance_and_restart_are_idempotent(self) -> None:
        self.service.create_series(
            name="daily",
            timezone="Test/Spring",
            local_time="10:00",
            starts_on=date(2024, 3, 10),
        )
        due = datetime(2024, 3, 10, 13, 45)

        self.assertEqual(
            self.service.advance_clock(due), self.service.advance_clock(due)
        )
        self.service = self.restart()
        self.assertEqual(self.service.advance_clock(datetime(2024, 3, 10, 14)), [])
        reminders = self.service.list_reminders()
        self.assertEqual(len(reminders), 1)

        with sqlite3.connect(self.db_path) as raw:
            count = raw.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
        self.assertEqual(count, 1)

    def test_modification_applies_after_effective_date_and_preserves_evidence(
        self,
    ) -> None:
        series = self.service.create_series(
            name="changing daily",
            timezone="Test/Spring",
            local_time="09:00",
            starts_on=date(2024, 3, 9),
        )

        due_before = datetime(2024, 3, 9, 13, 45)
        self.service.advance_clock(due_before)
        before = self.instance_for(date(2024, 3, 9))
        self.assertEqual(before["rule_version"]["version_no"], 1)
        self.assertEqual(before["utc_at"], "2024-03-09T14:00:00Z")
        self.assertTrue(before["reminder_sent"])

        version2 = self.service.modify_series(
            series["series_id"],
            effective_date=date(2024, 3, 10),
            local_time="11:00",
        )
        self.service = self.restart()

        unchanged = self.instance_for(date(2024, 3, 9))
        changed = self.instance_for(date(2024, 3, 10))
        self.assertEqual(unchanged["rule_version"]["version_id"], series["version_id"])
        self.assertEqual(unchanged["utc_at"], "2024-03-09T14:00:00Z")
        self.assertEqual(changed["rule_version"]["version_id"], version2["version_id"])
        self.assertEqual(changed["scheduled_local"], "2024-03-10T11:00:00")
        self.assertEqual(changed["utc_at"], "2024-03-10T15:00:00Z")

        with self.assertRaises(ConflictingOccurrence):
            self.service.set_exception(series["series_id"], date(2024, 3, 9), "12:00")

    def test_explicitly_confirmed_instance_keeps_original_rule_evidence(self) -> None:
        series = self.service.create_series(
            name="confirmed daily",
            timezone="Test/Spring",
            local_time="09:00",
            starts_on=date(2024, 3, 10),
        )
        day = date(2024, 3, 10)
        occurrence = self.instance_for(day)
        confirmed = self.service.confirm_occurrence(occurrence["occurrence_id"])
        self.assertIsNotNone(confirmed["confirmed_at"])

        with self.assertRaises(ConflictingOccurrence):
            self.service.set_exception(series["series_id"], day, "12:00")
        self.service.modify_series(
            series["series_id"],
            effective_date=date(2024, 3, 11),
            local_time="11:00",
        )

        rows = {
            date.fromisoformat(row["scheduled_local"][:10]): row
            for row in self.service.list_instances(day, date(2024, 3, 11))
        }
        row = rows[day]
        later = rows[date(2024, 3, 11)]
        self.assertEqual(row["rule_version"]["version_no"], 1)
        self.assertEqual(row["scheduled_local"], "2024-03-10T09:00:00")
        self.assertEqual(row["utc_at"], "2024-03-10T13:00:00Z")
        self.assertEqual(later["rule_version"]["version_no"], 2)
        self.assertEqual(later["scheduled_local"], "2024-03-11T11:00:00")
        self.assertFalse(row["reminder_sent"])
        self.assertIsNotNone(row["confirmed_at"])

    def test_single_day_exception_interleaves_without_duplicate_or_lost_reminder(
        self,
    ) -> None:
        series = self.service.create_series(
            name="exception daily",
            timezone="Test/Spring",
            local_time="09:00",
            starts_on=date(2024, 3, 10),
        )

        exception = self.service.set_exception(
            series["series_id"], date(2024, 3, 10), "12:30"
        )
        first = self.instance_for(date(2024, 3, 10))
        self.assertEqual(first["scheduled_local"], "2024-03-10T12:30:00")
        self.assertEqual(first["utc_at"], "2024-03-10T16:30:00Z")
        self.assertEqual(
            first["rule_version"]["exception_id"], exception["exception_id"]
        )

        # Materialize the reminder.
        due = datetime(2024, 3, 10, 16, 15)
        reminders = self.service.advance_clock(due)
        self.assertEqual(len(reminders), 1)

        # The confirmed instance is evidence of revision 1 and cannot be changed.
        with self.assertRaises(ConflictingOccurrence):
            self.service.set_exception(series["series_id"], date(2024, 3, 10), "18:00")
        with self.assertRaises(ConflictingOccurrence):
            self.service.cancel_date(series["series_id"], date(2024, 3, 10))

        self.service = self.restart()
        self.assertEqual(self.service.advance_clock(datetime(2024, 3, 10, 17)), [])
        final = self.instance_for(date(2024, 3, 10))
        self.assertEqual(final["rule_version"]["exception_revision"], 1)
        self.assertEqual(len(self.service.list_reminders()), 1)

    def test_exception_then_series_modification_still_applies_together(self) -> None:
        series = self.service.create_series(
            name="interleaved daily",
            timezone="Test/Spring",
            local_time="09:00",
            starts_on=date(2024, 3, 10),
        )
        exception = self.service.set_exception(
            series["series_id"], date(2024, 3, 11), "12:30"
        )
        version2 = self.service.modify_series(
            series["series_id"],
            effective_date=date(2024, 3, 11),
            local_time="11:00",
        )
        row = self.instance_for(date(2024, 3, 11))

        self.assertEqual(row["scheduled_local"], "2024-03-11T12:30:00")
        self.assertEqual(row["utc_at"], "2024-03-11T16:30:00Z")
        self.assertEqual(row["rule_version"]["version_id"], version2["version_id"])
        self.assertEqual(
            row["rule_version"]["exception_revision"], exception["revision"]
        )

    def test_cancelled_exception_has_no_reminder(self) -> None:
        series = self.service.create_series(
            name="cancel daily",
            timezone="Test/Spring",
            local_time="09:00",
            starts_on=date(2024, 3, 10),
        )
        self.service.cancel_date(series["series_id"], date(2024, 3, 10))
        cancelled = self.instance_for(date(2024, 3, 10))
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNone(cancelled["reminder_due_at"])
        self.service.advance_clock(datetime(2024, 3, 10, 14, 0))
        cancelled_reminders = [
            reminder
            for reminder in self.service.list_reminders()
            if reminder["scheduled_local"] == "2024-03-10T09:00:00"
        ]
        self.assertEqual(cancelled_reminders, [])


    def test_timezone_change_applies_from_local_date_without_touching_past(
        self,
    ) -> None:
        series = self.service.create_series(
            name="mover",
            timezone="Test/Spring",
            local_time="09:00",
            starts_on=date(2024, 3, 1),
        )
        # Persist a wide window under the original zone first.
        self.service.list_instances(date(2024, 3, 1), date(2024, 3, 20))

        result = self.service.set_timezone(
            series["series_id"],
            timezone="Test/Tokyo",
            effective_date=date(2024, 3, 15),
            expected_revision=0,
        )
        self.assertEqual(result, {"timezone": "Test/Tokyo", "zone_revision": 1})

        rows = {
            date.fromisoformat(row["scheduled_local"][:10]): row
            for row in self.service.list_instances(
                date(2024, 3, 13), date(2024, 3, 16)
            )
        }
        # Earlier local dates keep the old zone's evidence and revision 0.
        self.assertEqual(rows[date(2024, 3, 13)]["utc_offset"], "-04:00")
        self.assertEqual(rows[date(2024, 3, 13)]["utc_at"], "2024-03-13T13:00:00Z")
        self.assertEqual(
            rows[date(2024, 3, 13)]["rule_version"]["zone_revision"], 0
        )
        # On and after the effective date the wall time and phase are kept but
        # the instant is resolved in the new zone.
        self.assertEqual(
            rows[date(2024, 3, 15)]["scheduled_local"], "2024-03-15T09:00:00"
        )
        self.assertEqual(rows[date(2024, 3, 15)]["utc_offset"], "+09:00")
        self.assertEqual(rows[date(2024, 3, 15)]["utc_at"], "2024-03-15T00:00:00Z")
        self.assertEqual(
            rows[date(2024, 3, 15)]["reminder_due_at"], "2024-03-14T23:45:00Z"
        )
        self.assertEqual(
            rows[date(2024, 3, 15)]["rule_version"]["zone_revision"], 1
        )

        # A restart must not reinterpret any date.
        service = self.restart()
        rows = {
            date.fromisoformat(row["scheduled_local"][:10]): row
            for row in service.list_instances(date(2024, 3, 13), date(2024, 3, 16))
        }
        self.assertEqual(rows[date(2024, 3, 13)]["utc_at"], "2024-03-13T13:00:00Z")
        self.assertEqual(rows[date(2024, 3, 15)]["utc_at"], "2024-03-15T00:00:00Z")

    def test_timezone_change_preserves_confirmed_and_sent_evidence(self) -> None:
        series = self.service.create_series(
            name="locked",
            timezone="Test/Spring",
            local_time="09:00",
            starts_on=date(2024, 3, 10),
        )
        self.service.advance_clock(datetime(2024, 3, 10, 12, 45))
        self.service.set_timezone(
            series["series_id"],
            timezone="Test/Tokyo",
            effective_date=date(2024, 3, 10),
            expected_revision=0,
        )

        rows = {
            date.fromisoformat(row["scheduled_local"][:10]): row
            for row in self.service.list_instances(date(2024, 3, 10), date(2024, 3, 11))
        }
        sent = rows[date(2024, 3, 10)]
        # The already-sent instance is immutable evidence of the old zone.
        self.assertTrue(sent["reminder_sent"])
        self.assertEqual(sent["utc_at"], "2024-03-10T13:00:00Z")
        self.assertEqual(sent["utc_offset"], "-04:00")
        self.assertEqual(sent["rule_version"]["zone_revision"], 0)
        # Unlocked later instances follow the new zone.
        self.assertEqual(rows[date(2024, 3, 11)]["utc_offset"], "+09:00")
        self.assertEqual(rows[date(2024, 3, 11)]["rule_version"]["zone_revision"], 1)

        reminders = self.service.list_reminders()
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0]["utc_at"], "2024-03-10T13:00:00Z")
        self.assertEqual(reminders[0]["rule_version"]["zone_revision"], 0)

        # Restarting keeps the original reminder and its frozen evidence; the
        # unlocked later days fire their own (new-zone) reminders.
        service = self.restart()
        service.advance_clock(datetime(2024, 3, 12))
        locked_reminders = [
            reminder
            for reminder in service.list_reminders()
            if reminder["scheduled_local"] == "2024-03-10T09:00:00"
        ]
        self.assertEqual(len(locked_reminders), 1)
        self.assertEqual(locked_reminders[0]["utc_at"], "2024-03-10T13:00:00Z")
        self.assertEqual(
            locked_reminders[0]["rule_version"]["zone_revision"], 0
        )

    def test_timezone_change_rematerializes_all_persisted_future_instances(
        self,
    ) -> None:
        series = self.service.create_series(
            name="partial",
            timezone="Test/Spring",
            local_time="10:00",
            starts_on=date(2024, 5, 1),
        )
        # Only one day is persisted before the change.
        self.service.list_instances(date(2024, 5, 10), date(2024, 5, 10))
        self.service.set_timezone(
            series["series_id"],
            timezone="Test/Tokyo",
            effective_date=date(2024, 5, 10),
            expected_revision=0,
        )
        rows = {
            date.fromisoformat(row["scheduled_local"][:10]): row
            for row in self.service.list_instances(date(2024, 5, 9), date(2024, 5, 12))
        }
        self.assertEqual(rows[date(2024, 5, 9)]["utc_offset"], "-04:00")
        self.assertEqual(rows[date(2024, 5, 10)]["utc_offset"], "+09:00")
        self.assertEqual(rows[date(2024, 5, 10)]["utc_at"], "2024-05-10T01:00:00Z")
        self.assertEqual(rows[date(2024, 5, 11)]["utc_offset"], "+09:00")

    def test_timezone_change_keeps_exceptions_bound_to_their_local_dates(
        self,
    ) -> None:
        series = self.service.create_series(
            name="exceptions",
            timezone="Test/Spring",
            local_time="09:00",
            starts_on=date(2024, 6, 1),
        )
        self.service.set_exception(series["series_id"], date(2024, 6, 3), "12:30")
        self.service.cancel_date(series["series_id"], date(2024, 6, 4))
        self.service.set_exception(series["series_id"], date(2024, 6, 7), "20:00")
        self.service.set_timezone(
            series["series_id"],
            timezone="Test/Tokyo",
            effective_date=date(2024, 6, 5),
            expected_revision=0,
        )
        rows = {
            date.fromisoformat(row["scheduled_local"][:10]): row
            for row in self.service.list_instances(date(2024, 6, 2), date(2024, 6, 8))
        }
        # Pre-effective override keeps its wall time but resolves in the old
        # zone; the cancellation remains a cancellation.
        self.assertEqual(
            rows[date(2024, 6, 3)]["scheduled_local"], "2024-06-03T12:30:00"
        )
        self.assertEqual(rows[date(2024, 6, 3)]["utc_at"], "2024-06-03T16:30:00Z")
        self.assertEqual(rows[date(2024, 6, 4)]["status"], "cancelled")
        # Post-effective normal and overridden days use the new zone.
        self.assertEqual(rows[date(2024, 6, 5)]["utc_at"], "2024-06-05T00:00:00Z")
        self.assertEqual(
            rows[date(2024, 6, 7)]["scheduled_local"], "2024-06-07T20:00:00"
        )
        self.assertEqual(rows[date(2024, 6, 7)]["utc_at"], "2024-06-07T11:00:00Z")

    def test_timezone_change_preserves_interval_phase_and_resolves_dst_rules(
        self,
    ) -> None:
        # Biweekly phase anchored to the series start survives the zone cut.
        weekly = self.service.create_series(
            name="biweekly",
            timezone="Test/Spring",
            local_time="09:00",
            starts_on=date(2024, 3, 5),
            frequency=Frequency.WEEKLY,
            interval=2,
        )
        self.service.set_timezone(
            weekly["series_id"],
            timezone="Test/Tokyo",
            effective_date=date(2024, 3, 20),
            expected_revision=0,
        )
        days = [
            row["scheduled_local"][:10]
            for row in self.service.list_instances(date(2024, 3, 1), date(2024, 4, 5))
        ]
        self.assertEqual(days, ["2024-03-05", "2024-03-19", "2024-04-02"])

        # Moving INTO the spring zone at the gap date honors the gap policy.
        gap_series = self.service.create_series(
            name="into gap",
            timezone="Test/Tokyo",
            local_time="02:30",
            starts_on=date(2024, 3, 8),
            gap=GapPolicy.SKIP,
        )
        self.service.set_timezone(
            gap_series["series_id"],
            timezone="Test/Spring",
            effective_date=date(2024, 3, 10),
            expected_revision=0,
        )
        gap_rows = {
            date.fromisoformat(row["scheduled_local"][:10]): row
            for row in self.service.list_instances(
                date(2024, 3, 9), date(2024, 3, 11)
            )
        }
        self.assertEqual(gap_rows[date(2024, 3, 10)]["status"], "skipped")
        self.assertEqual(
            gap_rows[date(2024, 3, 10)]["skip_reason"], "nonexistent_local_time"
        )
        self.assertEqual(gap_rows[date(2024, 3, 11)]["utc_offset"], "-04:00")

        # Moving INTO the fall zone at the overlap honors the ambiguity policy.
        amb = self.service.create_series(
            name="into overlap",
            timezone="Test/Tokyo",
            local_time="01:30",
            starts_on=date(2024, 11, 2),
            ambiguous=AmbiguousPolicy.SECOND,
        )
        self.service.set_timezone(
            amb["series_id"],
            timezone="Test/Fall",
            effective_date=date(2024, 11, 3),
            expected_revision=0,
        )
        overlap_rows = [
            row
            for row in self.service.list_instances(
                date(2024, 11, 3), date(2024, 11, 3)
            )
            if row["series_id"] == amb["series_id"]
        ]
        self.assertEqual(len(overlap_rows), 1)
        row = overlap_rows[0]
        self.assertEqual(row["ambiguous_occurrence"], 2)
        self.assertEqual(row["utc_at"], "2024-11-03T06:30:00Z")

    def test_stale_or_invalid_timezone_change_is_atomic(self) -> None:
        series = self.service.create_series(
            name="revisioned",
            timezone="Test/Spring",
            local_time="09:00",
            starts_on=date(2024, 7, 1),
        )
        self.service.set_timezone(
            series["series_id"],
            timezone="Test/Tokyo",
            effective_date=date(2024, 7, 5),
            expected_revision=0,
        )

        with self.assertRaises(StaleRevision):
            self.service.set_timezone(
                series["series_id"],
                timezone="Test/UTC",
                effective_date=date(2024, 7, 8),
                expected_revision=0,
            )

        with sqlite3.connect(self.db_path) as raw:
            timezone = raw.execute(
                "SELECT timezone FROM series WHERE id = ?", (series["series_id"],)
            ).fetchone()[0]
            revisions = [
                row[0]
                for row in raw.execute(
                    "SELECT zone_revision FROM series_timezone_history "
                    "WHERE series_id = ? ORDER BY zone_revision",
                    (series["series_id"],),
                )
            ]
        self.assertEqual(timezone, "Test/Tokyo")
        self.assertEqual(revisions, [0, 1])

        # The rejected change must not have rebuilt the instances either.
        row = self.service.list_instances(date(2024, 7, 5), date(2024, 7, 5))[0]
        self.assertEqual(row["utc_offset"], "+09:00")
        self.assertEqual(row["rule_version"]["zone_revision"], 1)

    def test_consecutive_timezone_chains_compose(self) -> None:
        series = self.service.create_series(
            name="chain",
            timezone="Test/Spring",
            local_time="09:00",
            starts_on=date(2024, 8, 1),
        )
        self.service.set_timezone(
            series["series_id"],
            timezone="Test/Tokyo",
            effective_date=date(2024, 8, 5),
            expected_revision=0,
        )
        self.service.set_timezone(
            series["series_id"],
            timezone="Test/UTC",
            effective_date=date(2024, 8, 10),
            expected_revision=1,
        )
        # A same-day correction based on revision 2 supersedes the 8/10 rule.
        self.service.set_timezone(
            series["series_id"],
            timezone="Test/Spring",
            effective_date=date(2024, 8, 10),
            expected_revision=2,
        )
        rows = {
            date.fromisoformat(row["scheduled_local"][:10]): row
            for row in self.service.list_instances(date(2024, 8, 4), date(2024, 8, 11))
        }
        self.assertEqual(rows[date(2024, 8, 4)]["utc_offset"], "-04:00")
        self.assertEqual(rows[date(2024, 8, 5)]["utc_offset"], "+09:00")
        self.assertEqual(rows[date(2024, 8, 9)]["utc_offset"], "+09:00")
        self.assertEqual(rows[date(2024, 8, 10)]["utc_offset"], "-04:00")
        self.assertEqual(
            rows[date(2024, 8, 10)]["rule_version"]["zone_revision"], 3
        )


if __name__ == "__main__":
    unittest.main()
