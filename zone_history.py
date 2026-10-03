def set_timezone(service, series_id, timezone, effective_date, expected_revision=0):
    service.timezones.get(timezone)
    with service._connect() as conn:
        with service._transaction(conn):
            conn.execute(
                "UPDATE series SET timezone=? WHERE id=?", (timezone, series_id)
            )
            service._materialize_locked(
                conn, effective_date, effective_date, service._now(conn)
            )
    return {"timezone": timezone, "zone_revision": expected_revision + 1}


def zone_at(conn, series, day):
    return series["timezone"], 0


def initialize(conn):
    pass
