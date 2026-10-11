"""Bounded ten-second window selection, independent of runtime and storage."""

from __future__ import annotations

import math
from datetime import datetime

from watt_profile.contracts.domain import (
    SessionDescriptor,
    Snapshot,
    SourceDescriptor,
    snapshot_from_reading,
)
from watt_profile.p1.profile import MappedReading


class WindowSampler:
    """Select the latest received reading in each half-open window.

    The caller owns this instance for one collection run and provides a fresh
    monotonic anchor on restart. ``offer`` advances boundaries first, so a
    reading received exactly at a boundary belongs to the new window.
    """

    def __init__(
        self,
        session: SessionDescriptor,
        source: SourceDescriptor,
        monotonic_anchor: float,
        *,
        interval_seconds: float = 10.0,
    ) -> None:
        if not math.isfinite(monotonic_anchor):
            raise ValueError("invalid_monotonic_time")
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("invalid_window_interval")
        if source.session_id != session.session_id:
            raise ValueError("descriptor_mismatch")
        self._session = session
        self._source = source
        self._anchor = monotonic_anchor
        self._interval = interval_seconds
        self._last_monotonic = monotonic_anchor
        self._candidate: MappedReading | None = None

    def offer(self, reading: MappedReading, monotonic_received: float) -> tuple[Snapshot, ...]:
        """Advance elapsed windows, then retain this reading for its window."""
        if not math.isfinite(monotonic_received):
            raise ValueError("invalid_monotonic_time")
        if monotonic_received < self._last_monotonic:
            raise ValueError("monotonic_time_moved_backwards")
        completed = self._advance(monotonic_received, reading.received_at)
        self._candidate = reading
        self._last_monotonic = monotonic_received
        return completed

    def advance(self, monotonic_now: float, utc_now: datetime) -> tuple[Snapshot, ...]:
        """Finalize an elapsed window once; empty and skipped windows emit none."""
        if not math.isfinite(monotonic_now):
            raise ValueError("invalid_monotonic_time")
        if monotonic_now < self._last_monotonic:
            raise ValueError("monotonic_time_moved_backwards")
        completed = self._advance(monotonic_now, utc_now)
        self._last_monotonic = monotonic_now
        return completed

    def _advance(self, monotonic_now: float, utc_now: datetime) -> tuple[Snapshot, ...]:
        elapsed = monotonic_now - self._anchor
        windows_elapsed = math.floor(elapsed / self._interval)
        if windows_elapsed <= 0:
            return ()

        candidate = self._candidate
        self._candidate = None
        self._anchor += windows_elapsed * self._interval
        if candidate is None:
            return ()
        return (snapshot_from_reading(candidate, self._session, self._source, utc_now),)
