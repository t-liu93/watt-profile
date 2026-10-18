"""Projection-backed data-quality summaries and bounded locator pages."""

from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError
from sqlalchemy import text

from watt_profile.contracts.domain import UUID4, DecimalValue, DomainModel, UtcTime
from watt_profile.server.analysis import AnalysisContext
from watt_profile.server.registration import PageOptions

if TYPE_CHECKING:
    from watt_profile.server.analysis import AnalysisRead

_METRICS = (
    "energy_import_t1_kwh",
    "energy_import_t2_kwh",
    "energy_export_t1_kwh",
    "energy_export_t2_kwh",
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
)


class MetricValidity(DomainModel):
    metric: str
    valid_count: int
    missing_count: int


class Gap(DomainModel):
    received_from: UtcTime
    received_before: UtcTime
    kind: Literal["leading", "internal", "trailing", "unobserved"]


class ProblemRecord(DomainModel):
    received_at: UtcTime
    device_id: UUID4
    record_id: UUID4
    issues: tuple[dict, ...]


class IssueSummary(DomainModel):
    reason: str
    count: int


class QualityResult(DomainModel):
    context: AnalysisContext
    expected_count: int
    actual_count: int
    displayed_count: int
    coverage_percent: DecimalValue
    excess_count: int
    metrics: tuple[MetricValidity, ...]
    import_endpoint_valid_count: int
    import_endpoint_missing_count: int
    export_endpoint_valid_count: int
    export_endpoint_missing_count: int
    redacted_field_count: int
    issues: tuple[IssueSummary, ...]
    gaps: tuple[Gap, ...]
    gap_total: int
    gap_offset: int
    gap_limit: int
    problems: tuple[ProblemRecord, ...]
    problem_total: int
    problem_offset: int
    problem_limit: int


def _options(value):
    try:
        return PageOptions.model_validate(
            value.model_dump() if isinstance(value, PageOptions) else value or {}
        )
    except (ValidationError, AttributeError):
        from watt_profile.server.analysis import InvalidAnalysis

        raise InvalidAnalysis() from None


def calculate_quality(read: "AnalysisRead", *, gaps=None, problems=None) -> QualityResult:
    """Calculate coverage and return independently paged gaps and persisted issue locators."""
    gap_page, problem_page = _options(gaps), _options(problems)
    c = read.context
    params = dict(
        site_id=c.site_id,
        session_id=c.session_id,
        received_from=c.range.received_from,
        received_before=c.range.received_before,
    )
    # Aggregate each fixed projection column before constructing the bounded
    # metric result; do not expand every reading into eighteen metric rows.
    metric_counts = ", ".join(f"count(s.{m}) AS {m}" for m in _METRICS)
    summary = (
        read.connection.execute(
            text(f"""
        SELECT count(*) AS actual_count, {metric_counts},
          count(*) FILTER (WHERE energy_import_t1_kwh IS NOT NULL
            AND energy_import_t2_kwh IS NOT NULL) AS import_valid,
          count(*) FILTER (WHERE energy_import_t1_kwh IS NULL
            OR energy_import_t2_kwh IS NULL) AS import_missing,
          count(*) FILTER (WHERE energy_export_t1_kwh IS NOT NULL
            AND energy_export_t2_kwh IS NOT NULL) AS export_valid,
          count(*) FILTER (WHERE energy_export_t1_kwh IS NULL
            OR energy_export_t2_kwh IS NULL) AS export_missing,
          coalesce(sum((SELECT count(*) FROM jsonb_array_elements(s.fields) f
            WHERE (f->>'redacted')::boolean)), 0) AS redacted
        FROM snapshots s WHERE site_id=:site_id AND session_id=:session_id
          AND received_at >= :received_from AND received_at < :received_before
    """),
            params,
        )
        .mappings()
        .one()
    )
    validity = [
        {
            "metric": name,
            "valid_count": summary[name],
            "missing_count": summary["actual_count"] - summary[name],
        }
        for name in sorted(_METRICS)
    ]
    duration = c.range.received_before - c.range.received_from
    duration_us = (duration.days * 86400 + duration.seconds) * 1_000_000 + duration.microseconds
    expected = (duration_us + 10_000_000 - 1) // 10_000_000
    actual = summary["actual_count"]
    displayed = min(actual, expected)
    percent = Decimal(0) if not expected else Decimal(displayed * 100) / Decimal(expected)
    gaps_sql = text("""
      WITH observed AS MATERIALIZED (
        SELECT received_at, lag(received_at) OVER (ORDER BY received_at,device_id,record_id) prev
        FROM snapshots WHERE site_id=:site_id AND session_id=:session_id
          AND received_at >= :received_from AND received_at < :received_before
      ), g AS (
        SELECT CAST(:received_from AS timestamptz) AS received_from,
          CAST(:received_before AS timestamptz) AS received_before, 'unobserved' kind
          FROM observed HAVING count(*)=0
        UNION ALL SELECT CAST(:received_from AS timestamptz), min(received_at), 'leading'
          FROM observed
          HAVING min(received_at)-CAST(:received_from AS timestamptz) > interval '30 seconds'
        UNION ALL SELECT prev+interval '10 seconds', received_at, 'internal' FROM observed
          WHERE received_at-prev > interval '30 seconds'
        UNION ALL SELECT max(received_at)+interval '10 seconds',
          CAST(:received_before AS timestamptz), 'trailing' FROM observed
          HAVING CAST(:received_before AS timestamptz)-max(received_at) > interval '30 seconds'
      ) SELECT * FROM g ORDER BY received_from,received_before,kind
    """)
    gap_total = read.connection.scalar(text(f"SELECT count(*) FROM ({gaps_sql.text}) g"), params)
    page_params = params | {"offset": gap_page.offset, "limit": gap_page.limit}
    gap_rows = (
        read.connection.execute(text(gaps_sql.text + " LIMIT :limit OFFSET :offset"), page_params)
        .mappings()
        .all()
    )
    problem_total = read.connection.scalar(
        text("""
      SELECT count(*) FROM (SELECT device_id,record_id FROM snapshots s
       CROSS JOIN LATERAL jsonb_array_elements(s.quality->'issues') issue
       WHERE site_id=:site_id AND session_id=:session_id AND received_at >= :received_from
       AND received_at < :received_before
       AND issue->>'reason' <> 'redacted' GROUP BY device_id,record_id) q
    """),
        params,
    )
    issue_rows = (
        read.connection.execute(
            text("""
      SELECT received_at,device_id,record_id,
        jsonb_agg(issue ORDER BY issue->>'code',issue->>'reason') AS issues
      FROM snapshots s CROSS JOIN LATERAL jsonb_array_elements(s.quality->'issues') issue
      WHERE site_id=:site_id AND session_id=:session_id AND received_at >= :received_from
        AND received_at < :received_before AND issue->>'reason' <> 'redacted'
      GROUP BY received_at,device_id,record_id ORDER BY received_at,device_id,record_id
      LIMIT :limit OFFSET :offset
    """),
            params | {"limit": problem_page.limit, "offset": problem_page.offset},
        )
        .mappings()
        .all()
    )
    issue_summaries = (
        read.connection.execute(
            text("""
      SELECT issue->>'reason' AS reason, count(*) AS count
      FROM snapshots s CROSS JOIN LATERAL jsonb_array_elements(s.quality->'issues') issue
      WHERE site_id=:site_id AND session_id=:session_id AND received_at >= :received_from
        AND received_at < :received_before AND issue->>'reason' <> 'redacted'
      GROUP BY issue->>'reason'
      ORDER BY issue->>'reason'
    """),
            params,
        )
        .mappings()
        .all()
    )
    return QualityResult(
        context=c,
        expected_count=expected,
        actual_count=actual,
        displayed_count=displayed,
        coverage_percent=percent,
        excess_count=max(0, actual - expected),
        metrics=tuple(MetricValidity(**row) for row in validity),
        import_endpoint_valid_count=summary["import_valid"],
        import_endpoint_missing_count=summary["import_missing"],
        export_endpoint_valid_count=summary["export_valid"],
        export_endpoint_missing_count=summary["export_missing"],
        redacted_field_count=summary["redacted"],
        issues=tuple(IssueSummary(**row) for row in issue_summaries),
        gaps=tuple(Gap(**row) for row in gap_rows),
        gap_total=gap_total,
        gap_offset=gap_page.offset,
        gap_limit=gap_page.limit,
        problems=tuple(ProblemRecord(**row) for row in issue_rows),
        problem_total=problem_total,
        problem_offset=problem_page.offset,
        problem_limit=problem_page.limit,
    )
