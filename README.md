# Recurring schedule service

`ScheduleService` is a standard-library Python service that persists recurring
events in SQLite. It supports:

- daily and weekly recurrence;
- IANA timezones via `zoneinfo`, plus deterministic scripted timezones for tests;
- series-level policies for spring-forward gaps: `skip` or move forward to the
  first valid wall time;
- series-level policies for fall-back repeats: first or second occurrence;
- single-day overrides and cancellations with revisions;
- append-only series versions, applied from a specified local date;
- immutable occurrence evidence once an occurrence is confirmed or its reminder
  has fired;
- a controlled clock with unique, idempotent reminder records 15 minutes before
  each scheduled UTC instant.

## Run the tests

```bash
python3 -m unittest test_scheduler.py -v
```

The tests use fixed, scripted timezone transition data rather than depending on
the host timezone database. They cover skipped and shifted gaps, first/second
fall-back occurrences, cross-day UTC reminders, version/exception interleaving,
and idempotency after repeated ticks or process restarts.

## Example

```python
from datetime import date, datetime
from scheduler import (
    AmbiguousPolicy,
    Frequency,
    GapPolicy,
    ScheduleService,
)

service = ScheduleService("schedules.sqlite")
service.initialize_clock(datetime(2026, 1, 1))

series = service.create_series(
    name="daily stand-up",
    timezone="America/New_York",
    local_time="09:00",
    starts_on=date(2026, 1, 5),
    frequency=Frequency.DAILY,
    ambiguous=AmbiguousPolicy.FIRST,
    gap=GapPolicy.FORWARD,
)

# Versions are immutable evidence. A modification applies to dates on and after
# its effective local date; already reminded/confirmed occurrences keep their
# old rule version.
service.modify_series(
    series["series_id"],
    effective_date=date(2026, 2, 1),
    local_time="10:30",
)

service.set_exception(series["series_id"], date(2026, 1, 7), "13:00")
service.cancel_date(series["series_id"], date(2026, 1, 8))

instances = service.list_instances(date(2026, 1, 5), date(2026, 2, 2))
reminders_during_tick = service.advance_clock(datetime(2026, 1, 5, 14, 45))
```

All times supplied to `initialize_clock` and `advance_clock` are naive UTC
`datetime` values. Occurrence APIs return local wall time, a `Z`-suffixed UTC
instant, UTC offset, rule/exception version details, and skip reason where
applicable.

set_timezone 支持按系列当地生效日期切换时区，钟点和循环相位不变。已确认或已发提醒的实例保留原证据；单日例外仍绑定原系列当地日期。expected_revision 表示时区变更基准，过期基准不能生效；重启后保留时区变化。
