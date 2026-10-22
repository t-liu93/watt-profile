"""Bounded projection-only curves and independent sampled peaks.

Bucket origins are the explicit curve start. Gap flags describe observed reading
coverage, independently of metric availability; consumers break lines at empty
buckets and at ``break_before``, and mark ``has_gap`` within populated buckets.
"""

from typing import TYPE_CHECKING, Literal

from sqlalchemy import text

from watt_profile.contracts.domain import UUID4, DecimalValue, DomainModel, UtcTime
from watt_profile.server.analysis import (
    AnalysisContext,
    AnalysisRange,
    InvalidAnalysis,
    parse_range,
)

if TYPE_CHECKING:
    from watt_profile.server.analysis import AnalysisRead

Metric = Literal[
    "power_import_kw",
    "power_export_kw",
    "voltage_l1_v",
    "voltage_l2_v",
    "voltage_l3_v",
    "current_l1_a",
    "current_l2_a",
    "current_l3_a",
    "power_import_l1_kw",
    "power_import_l2_kw",
    "power_import_l3_kw",
    "power_export_l1_kw",
    "power_export_l2_kw",
    "power_export_l3_kw",
]
_METRICS = frozenset(Metric.__args__)
MAX_BUCKETS = 2000


class CurveBucket(DomainModel):
    received_from: UtcTime
    received_before: UtcTime
    mean: DecimalValue | None
    minimum: DecimalValue | None
    maximum: DecimalValue | None
    valid_count: int
    has_gap: bool
    break_before: bool


class MetricCurve(DomainModel):
    metric: Metric
    buckets: tuple[CurveBucket, ...]


class SampledPeak(DomainModel):
    value_kw: DecimalValue
    received_at: UtcTime
    device_id: UUID4
    record_id: UUID4


class PowerResult(DomainModel):
    context: AnalysisContext
    curve_range: AnalysisRange
    resolution_seconds: int
    import_power: MetricCurve
    export_power: MetricCurve
    import_sampled_peak: SampledPeak | None
    export_sampled_peak: SampledPeak | None


def curve_range(global_range: AnalysisRange, zoom: AnalysisRange | dict | None) -> AnalysisRange:
    selected = global_range if zoom is None else parse_range(zoom)
    if (
        selected.received_from < global_range.received_from
        or selected.received_before > global_range.received_before
    ):
        raise InvalidAnalysis()
    return selected


def select_resolution(selected: AnalysisRange, *, max_buckets: int = MAX_BUCKETS) -> int:
    """Choose a readable scale, using exact integer microseconds for the cap."""
    if type(max_buckets) is not int or not 1 <= max_buckets <= MAX_BUCKETS:
        raise InvalidAnalysis()
    duration = selected.received_before - selected.received_from
    micros = (duration.days * 86400 + duration.seconds) * 1_000_000 + duration.microseconds
    required = (micros + max_buckets * 1_000_000 - 1) // (max_buckets * 1_000_000)
    for seconds in (10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 21600, 43200, 86400):
        if seconds >= required:
            return seconds
    return required


# Identifiers below come exclusively from the fixed Metric allowlist, never inputs.
# All raw readings, metric arithmetic, gap detection, and grouping stay in SQL.
# Aggregate columns before expanding metrics: only bounded bucket rows become
# metric rows, rather than multiplying every reading by the number of metrics.
_CURVE_SQL = """
WITH global_observed AS MATERIALIZED (
    SELECT received_at, device_id, record_id, {columns},
        lag(received_at) OVER (ORDER BY received_at, device_id, record_id) AS previous_at
    FROM snapshots
    WHERE site_id = :site_id AND session_id = :session_id
        AND received_at >= :global_from AND received_at < :global_before
), observed AS (
    SELECT * FROM global_observed
    WHERE received_at >= :received_from AND received_at < :received_before
), bounds AS (
    SELECT CAST(:received_from AS timestamptz) AS start_at,
        CAST(:received_before AS timestamptz) AS end_at,
        make_interval(secs => :seconds) AS step
), buckets AS (
    SELECT i, start_at + i * step AS start_at,
        least(start_at + (i + 1) * step, end_at) AS end_at
    FROM bounds CROSS JOIN generate_series(0, :bucket_count - 1) AS i
), gaps AS (
    SELECT previous_at + interval '10 seconds' AS start_at, received_at AS end_at
    FROM global_observed WHERE received_at - previous_at > interval '30 seconds'
    UNION ALL
    SELECT b.start_at, coalesce(min(o.received_at), b.end_at)
    FROM bounds b LEFT JOIN global_observed o ON true GROUP BY b.start_at, b.end_at
    HAVING min(o.received_at) IS NULL
        OR min(o.received_at) - CAST(:global_from AS timestamptz) > interval '30 seconds'
    UNION ALL
    SELECT max(o.received_at) + interval '10 seconds', b.end_at
    FROM bounds b JOIN global_observed o ON true GROUP BY b.end_at
    HAVING CAST(:global_before AS timestamptz) - max(o.received_at) > interval '30 seconds'
), gap_buckets AS (
    SELECT i, bool_or(g.end_at > b.start_at + i * b.step) AS has_gap
    FROM gaps g CROSS JOIN bounds b
    CROSS JOIN LATERAL generate_series(
        greatest(0, floor(extract(epoch FROM (g.start_at-b.start_at)) / :seconds)::int),
        least(:bucket_count - 1,
            floor(extract(epoch FROM (g.end_at-b.start_at)) / :seconds)::int)
    ) AS i WHERE g.start_at < b.end_at AND g.end_at >= b.start_at GROUP BY i
), projected AS (
    SELECT floor(extract(epoch FROM (o.received_at-b.start_at)) / :seconds)::int AS i,
        {columns}
    FROM observed o CROSS JOIN bounds b
), grouped AS (
    SELECT i, {aggregates} FROM projected GROUP BY i
)
SELECT b.*, m.metric, m.mean, m.minimum, m.maximum,
    coalesce(m.valid_count, 0) AS valid_count, coalesce(gap.has_gap, false) AS has_gap,
    gap.i IS NOT NULL AS gap_break
FROM buckets b LEFT JOIN grouped g ON g.i = b.i
CROSS JOIN LATERAL (VALUES {values}) AS m(metric, mean, minimum, maximum, valid_count)
LEFT JOIN gap_buckets gap ON gap.i = b.i
ORDER BY m.metric, b.i
"""


def aggregate_curves(
    read: "AnalysisRead",
    metrics: tuple[Metric, ...],
    *,
    zoom: AnalysisRange | dict | None = None,
    max_buckets: int = MAX_BUCKETS,
) -> tuple[AnalysisRange, int, tuple[MetricCurve, ...]]:
    """Reusable bounded metric envelopes; lower caps support multi-series views."""
    if not metrics or len(set(metrics)) != len(metrics) or any(m not in _METRICS for m in metrics):
        raise InvalidAnalysis()
    selected = curve_range(read.context.range, zoom)
    seconds = select_resolution(selected, max_buckets=max_buckets)
    duration = selected.received_before - selected.received_from
    step_micros = seconds * 1_000_000
    micros = (duration.days * 86400 + duration.seconds) * 1_000_000 + duration.microseconds
    count = (micros + step_micros - 1) // step_micros
    rows = (
        read.connection.execute(
            text(
                _CURVE_SQL.format(
                    columns=", ".join(metrics),
                    aggregates=", ".join(
                        f"avg({m}) AS {m}_mean, min({m}) AS {m}_min, "
                        f"max({m}) AS {m}_max, count({m}) AS {m}_count"
                        for m in metrics
                    ),
                    values=", ".join(
                        f"('{m}', g.{m}_mean, g.{m}_min, g.{m}_max, g.{m}_count)" for m in metrics
                    ),
                )
            ),
            {
                "site_id": read.context.site_id,
                "session_id": read.context.session_id,
                "received_from": selected.received_from,
                "received_before": selected.received_before,
                "seconds": seconds,
                "bucket_count": count,
                "global_from": read.context.range.received_from,
                "global_before": read.context.range.received_before,
            },
        )
        .mappings()
        .all()
    )
    curves = []
    for metric in metrics:
        buckets = []
        previous = None
        for row in rows:
            if row["metric"] != metric:
                continue
            buckets.append(
                CurveBucket(
                    received_from=row["start_at"],
                    received_before=row["end_at"],
                    mean=row["mean"],
                    minimum=row["minimum"],
                    maximum=row["maximum"],
                    valid_count=row["valid_count"],
                    has_gap=row["has_gap"],
                    break_before=(
                        previous is None
                        or not previous["valid_count"]
                        or not row["valid_count"]
                        or previous["has_gap"]
                        or row["gap_break"]
                    ),
                )
            )
            previous = row
        curves.append(MetricCurve(metric=metric, buckets=tuple(buckets)))
    return selected, seconds, tuple(curves)


def calculate_power(
    read: "AnalysisRead", *, zoom: AnalysisRange | dict | None = None
) -> PowerResult:
    """Zoom changes curves only; sampled peak statistics retain the global range."""
    selected, seconds, curves = aggregate_curves(
        read,
        ("power_import_kw", "power_export_kw"),
        zoom=zoom,
    )
    peaks = {}
    for direction in ("import", "export"):
        metric = f"power_{direction}_kw"
        row = (
            read.connection.execute(
                text(f"""
            SELECT {metric} AS value_kw, received_at, device_id, record_id FROM snapshots
            WHERE site_id = :site_id AND session_id = :session_id
                AND received_at >= :received_from AND received_at < :received_before
                AND {metric} IS NOT NULL
            ORDER BY {metric} DESC, received_at, device_id, record_id LIMIT 1
        """),
                {
                    "site_id": read.context.site_id,
                    "session_id": read.context.session_id,
                    "received_from": read.context.range.received_from,
                    "received_before": read.context.range.received_before,
                },
            )
            .mappings()
            .one_or_none()
        )
        peaks[direction] = SampledPeak(**row) if row is not None else None
    return PowerResult(
        context=read.context,
        curve_range=selected,
        resolution_seconds=seconds,
        import_power=curves[0],
        export_power=curves[1],
        import_sampled_peak=peaks["import"],
        export_sampled_peak=peaks["export"],
    )
