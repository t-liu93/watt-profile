"""Projection-only average-day profiles, composed in an existing read snapshot."""

from datetime import time
from typing import TYPE_CHECKING, Literal

from sqlalchemy import text

from watt_profile.contracts.domain import DecimalValue, DomainModel
from watt_profile.server.analysis import AnalysisContext

if TYPE_CHECKING:
    from watt_profile.server.analysis import AnalysisRead


Metric = Literal["power_import_kw", "power_export_kw"]
DateGroup = Literal["all", "weekday", "weekend"]


class TypicalDaySlot(DomainModel):
    local_time: time
    metric: Metric
    mean: DecimalValue | None
    contributing_date_count: int


class TypicalDayGroup(DomainModel):
    group: DateGroup
    slots: tuple[TypicalDaySlot, ...]


class TypicalDayResult(DomainModel):
    context: AnalysisContext
    groups: tuple[TypicalDayGroup, ...]


# Scaffold complete local dates and quarter-hour slots, but calculate their
# actual duration by stepping through UTC instants. This naturally gives the
# autumn repeated slot 1,800 seconds, and the spring missing slot no duration.
# Reading metrics stay columnwise through date/slot aggregation; only the small
# grouped projection is expanded into metric rows for the final result.
_TYPICAL_DAY_SQL = text("""
WITH bounds AS (
    SELECT (:received_from AT TIME ZONE 'Europe/Amsterdam')::date AS first_day,
        ((:received_before - interval '1 microsecond')
            AT TIME ZONE 'Europe/Amsterdam')::date AS last_day
), dates AS (
    SELECT d::date AS day
    FROM bounds CROSS JOIN LATERAL generate_series(first_day, last_day, interval '1 day') AS d
), slots AS (
    SELECT dates.day, n AS slot_index,
        (dates.day::timestamp + n * interval '15 minutes') AS local_start
    FROM dates CROSS JOIN generate_series(0, 95) AS n
), actual_duration AS (
    SELECT ((instant AT TIME ZONE 'Europe/Amsterdam')::date) AS day,
        (extract(hour FROM instant AT TIME ZONE 'Europe/Amsterdam')::int * 4
            + extract(minute FROM instant AT TIME ZONE 'Europe/Amsterdam')::int
            / 15)::int AS slot_index,
        count(*) * 900 AS seconds
    FROM dates
    CROSS JOIN LATERAL generate_series(
        dates.day::timestamp AT TIME ZONE 'Europe/Amsterdam',
        (dates.day + 1)::timestamp AT TIME ZONE 'Europe/Amsterdam' - interval '15 minutes',
        interval '15 minutes'
    ) AS instant
    GROUP BY 1, 2
), daily AS (
    SELECT (s.received_at AT TIME ZONE 'Europe/Amsterdam')::date AS day,
        (extract(hour FROM s.received_at AT TIME ZONE 'Europe/Amsterdam')::int * 4
            + extract(minute FROM s.received_at AT TIME ZONE 'Europe/Amsterdam')::int
            / 15)::int AS slot_index,
        avg(s.power_import_kw) AS import_mean, count(s.power_import_kw) AS import_count,
        avg(s.power_export_kw) AS export_mean, count(s.power_export_kw) AS export_count
    FROM snapshots s
    WHERE s.site_id = :site_id AND s.session_id = :session_id
        AND s.received_at >= :received_from AND s.received_at < :received_before
    GROUP BY 1, 2
), eligible AS (
    SELECT slots.day, slots.slot_index,
        (extract(isodow FROM slots.day) < 6) AS is_weekday,
        'power_import_kw'::text AS metric,
        daily.import_mean AS mean,
        coalesce(daily.import_count, 0) AS valid_count,
        coalesce(duration.seconds, 0) AS full_seconds
    FROM slots LEFT JOIN actual_duration duration USING (day, slot_index)
    LEFT JOIN daily USING (day, slot_index)
    UNION ALL
    SELECT slots.day, slots.slot_index,
        (extract(isodow FROM slots.day) < 6), 'power_export_kw',
        daily.export_mean, coalesce(daily.export_count, 0), coalesce(duration.seconds, 0)
    FROM slots LEFT JOIN actual_duration duration USING (day, slot_index)
    LEFT JOIN daily USING (day, slot_index)
), grouped AS (
    SELECT g.group_name, e.slot_index, e.metric,
        avg(e.mean) FILTER (WHERE e.valid_count * 10 >= ceil(e.full_seconds * 0.8)) AS mean,
        count(*) FILTER (WHERE e.valid_count * 10 >= ceil(e.full_seconds * 0.8)) AS date_count
    FROM eligible e
    CROSS JOIN LATERAL (
        SELECT 'all'::text AS group_name
        UNION ALL SELECT 'weekday' WHERE e.is_weekday
        UNION ALL SELECT 'weekend' WHERE NOT e.is_weekday
    ) AS g
    WHERE e.full_seconds > 0
    GROUP BY g.group_name, e.slot_index, e.metric
)
SELECT group_name, slot_index, metric, mean, date_count
FROM grouped ORDER BY group_name, slot_index, metric
""")


def calculate_typical_day(read: "AnalysisRead") -> TypicalDayResult:
    """Average within each eligible date/slot, then weight each date equally."""
    context = read.context
    rows = read.connection.execute(
        _TYPICAL_DAY_SQL,
        {
            "site_id": context.site_id,
            "session_id": context.session_id,
            "received_from": context.range.received_from,
            "received_before": context.range.received_before,
        },
    ).mappings()
    by_group: dict[str, list[TypicalDaySlot]] = {"all": [], "weekday": [], "weekend": []}
    for row in rows:
        by_group[row["group_name"]].append(
            TypicalDaySlot(
                local_time=time(row["slot_index"] // 4, (row["slot_index"] % 4) * 15),
                metric=row["metric"],
                mean=row["mean"],
                contributing_date_count=row["date_count"],
            )
        )
    # Emit a stable 96-slot grid. A group/slot with no eligible dates has a
    # null mean and zero contributors; missing spring slots remain absent from
    # the SQL result and are represented this way for chart alignment.
    groups = []
    for group_name, slots in by_group.items():
        indexed = {(slot.local_time, slot.metric): slot for slot in slots}
        complete = tuple(
            indexed.get(
                (time(hour, minute), metric),
                TypicalDaySlot(
                    local_time=time(hour, minute),
                    metric=metric,
                    mean=None,
                    contributing_date_count=0,
                ),
            )
            for hour in range(24)
            for minute in range(0, 60, 15)
            for metric in ("power_import_kw", "power_export_kw")
        )
        groups.append(TypicalDayGroup(group=group_name, slots=complete))
    return TypicalDayResult(context=context, groups=tuple(groups))
