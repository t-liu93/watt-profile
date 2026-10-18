"""Aligned, bounded phase curves composed inside an analysis read snapshot."""

from typing import TYPE_CHECKING, Literal

from watt_profile.contracts.domain import DomainModel
from watt_profile.server.analysis import AnalysisContext, AnalysisRange, InvalidAnalysis
from watt_profile.server.analysis_power import MetricCurve, aggregate_curves

if TYPE_CHECKING:
    from watt_profile.server.analysis import AnalysisRead

Direction = Literal["import", "export"]
PHASE_MAX_BUCKETS = 500


class PhaseResult(DomainModel):
    context: AnalysisContext
    curve_range: AnalysisRange
    resolution_seconds: int
    direction: Direction
    voltage: tuple[MetricCurve, MetricCurve, MetricCurve]
    current: tuple[MetricCurve, MetricCurve, MetricCurve]
    power: tuple[MetricCurve, MetricCurve, MetricCurve]


def calculate_phases(read: "AnalysisRead", *, direction: str = "import", zoom=None) -> PhaseResult:
    if direction not in ("import", "export"):
        raise InvalidAnalysis()
    power_metrics = tuple(f"power_{direction}_l{phase}_kw" for phase in (1, 2, 3))
    metrics = (
        "voltage_l1_v",
        "voltage_l2_v",
        "voltage_l3_v",
        "current_l1_a",
        "current_l2_a",
        "current_l3_a",
        *power_metrics,
    )
    selected, seconds, curves = aggregate_curves(
        read, metrics, zoom=zoom, max_buckets=PHASE_MAX_BUCKETS
    )
    return PhaseResult(
        context=read.context,
        curve_range=selected,
        resolution_seconds=seconds,
        direction=direction,
        voltage=curves[:3],
        current=curves[3:6],
        power=curves[6:],
    )
