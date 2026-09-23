"""Small allow-listed in-process O1 metrics registry — M0 extended.

M0 adds canonical job metrics with bounded labels and cardinality guards.

M0 runtime integration adds two instrument types, chosen per the queue-metrics
research (Prometheus best practices / Google SRE / OTel semconv):

* **counters** (`increment`) — monotonic events, read with `rate()`;
* **gauges** (`set_gauge`) — current state set() from a queryable source of
  truth (SQLite counts), self-correcting, never inc/dec-tracked;
* **histograms** (`observe`) — latency with pre-fixed buckets frozen at M0
  (changing buckets later invalidates `histogram_quantile()` history).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Final

ALLOWED_LABELS: Final[frozenset[str]] = frozenset(
    {"backend", "scope", "error_code", "reason", "outcome", "job_type"}
)

# M0 canonical job metrics — low-cardinality, bounded labels, never secret
CANONICAL_JOB_METRICS: Final[frozenset[str]] = frozenset(
    {
        "jobs_created_total",
        "jobs_claimed_total",
        "jobs_completed_total",
        "jobs_failed_total",
        "jobs_recovered_total",
        # Backward-compatible aliases with nexus_ prefix
        "nexus_jobs_created_total",
        "nexus_jobs_claimed_total",
        "nexus_jobs_completed_total",
        "nexus_jobs_failed_total",
        "nexus_jobs_recovered_total",
    }
)

# Gauge names (state, not events — no _total suffix).
GAUGE_METRICS: Final[frozenset[str]] = frozenset(
    {
        "queue_depth",
        "jobs_inflight",
    }
)

# Histogram names. ``job_type`` labels are bounded for these too.
HISTOGRAM_METRICS: Final[frozenset[str]] = frozenset(
    {
        "job_duration_seconds",
    }
)

# Frozen at M0: bucket redesign invalidates histogram_quantile() history.
# Seconds; +Inf is implied as the final overflow bucket.
JOB_DURATION_BUCKETS: Final[tuple[float, ...]] = (
    0.1,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    300.0,
)

# Bounded job_type values — prevents cardinality explosion
BOUNDED_JOB_TYPES: Final[frozenset[str]] = frozenset(
    {
        "pdf_extract",
        "story",
        "slideshow_render",
        "image_gen",
        "video_render",
        "audio_process",
        "caption",
        "ad_delivery",
        "channel_schedule",
        "reminder",
        "unknown",
    }
)


def _validate_labels(name: str, labels: dict[str, str]) -> dict[str, str]:
    """Shared label guard: allow-list, cardinality, bounded job_type."""
    unknown = set(labels) - ALLOWED_LABELS
    if unknown:
        raise ValueError(f"unsupported metric labels: {sorted(unknown)}")
    # High-cardinality guard first: reject values that look like URLs, mentions, or overly long
    for k, v in labels.items():
        if len(v) > 64:
            raise ValueError(f"label value too long for {k}: {len(v)} > 64")
        if "://" in v or v.startswith("@"):
            raise ValueError(f"high-cardinality label value rejected for {k}: {v!r}")
    # Cardinality guard: job_type must be bounded for canonical + histogram metrics
    if "job_type" in labels and name in (CANONICAL_JOB_METRICS | HISTOGRAM_METRICS):
        jt = labels["job_type"]
        if jt not in BOUNDED_JOB_TYPES:
            labels = dict(labels)
            labels["job_type"] = "unknown"
    return labels


def _le_label(bound: float | None) -> str:
    """Prometheus-style le label: ``+Inf`` for the overflow bucket."""
    if bound is None:
        return "+Inf"
    return f"{bound:g}"


@dataclass
class _HistogramState:
    """Per-series histogram: per-bucket counts (+Inf last), sum and count."""

    buckets: list[int] = field(
        default_factory=lambda: [0] * (len(JOB_DURATION_BUCKETS) + 1)
    )
    total: float = 0.0
    count: int = 0


class MetricsRegistry:
    def __init__(self) -> None:
        self._values: defaultdict[tuple[str, tuple[tuple[str, str], ...]], int] = defaultdict(int)
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], _HistogramState] = {}

    def increment(self, name: str, *, labels: dict[str, str] | None = None, value: int = 1) -> None:
        labels = _validate_labels(name, labels or {})
        key = (name, tuple(sorted(labels.items())))
        self._values[key] += value

    def set_gauge(
        self,
        name: str,
        value: float,
        *,
        labels: dict[str, str] | None = None,
    ) -> None:
        """Set a gauge to a value sampled from the source of truth (not tracked)."""
        if not math.isfinite(value):
            raise ValueError(f"gauge value must be finite, got {value!r}")
        labels = _validate_labels(name, labels or {})
        key = (name, tuple(sorted(labels.items())))
        self._gauges[key] = float(value)

    def observe(
        self,
        name: str,
        value: float,
        *,
        labels: dict[str, str] | None = None,
    ) -> None:
        """Record one latency observation into the frozen bucket layout."""
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"observation must be finite and non-negative, got {value!r}")
        labels = _validate_labels(name, labels or {})
        key = (name, tuple(sorted(labels.items())))
        hist = self._histograms.get(key)
        if hist is None:
            hist = _HistogramState()
            self._histograms[key] = hist
        # First bucket whose bound >= value; overflow lands in the +Inf slot.
        idx = len(JOB_DURATION_BUCKETS)
        for i, bound in enumerate(JOB_DURATION_BUCKETS):
            if value <= bound:
                idx = i
                break
        hist.buckets[idx] += 1
        hist.total += value
        hist.count += 1

    def snapshot(self) -> dict[str, float]:
        out: dict[str, float] = {
            f"{name}{_format_labels(labels)}": value
            for (name, labels), value in sorted(self._values.items())
        }
        for (name, labels), value in sorted(self._gauges.items()):
            out[f"{name}{_format_labels(labels)}"] = value
        for (name, labels), hist in sorted(self._histograms.items()):
            cumulative = 0
            for i, bound in enumerate(JOB_DURATION_BUCKETS):
                cumulative += hist.buckets[i]
                le = _format_labels(labels, extra=(("le", _le_label(bound)),))
                out[f"{name}_bucket{le}"] = float(cumulative)
            le_inf = _format_labels(labels, extra=(("le", "+Inf"),))
            out[f"{name}_bucket{le_inf}"] = float(sum(hist.buckets))
            out[f"{name}_sum{_format_labels(labels)}"] = hist.total
            out[f"{name}_count{_format_labels(labels)}"] = float(hist.count)
        return out

    def reset(self) -> None:
        """Reset all counters, gauges and histograms — test-only, not for production use."""
        self._values.clear()
        self._gauges.clear()
        self._histograms.clear()

    def get(self, name: str, labels: dict[str, str] | None = None) -> int:
        labels = labels or {}
        key = (name, tuple(sorted(labels.items())))
        return self._values.get(key, 0)

    def get_gauge(self, name: str, labels: dict[str, str] | None = None) -> float | None:
        labels = labels or {}
        key = (name, tuple(sorted(labels.items())))
        return self._gauges.get(key)


_registry: MetricsRegistry | None = None


def get_metrics_registry() -> MetricsRegistry:
    """Process-wide registry (low cardinality, in-memory, no dependencies)."""
    global _registry
    if _registry is None:
        _registry = MetricsRegistry()
    return _registry


def reset_metrics_registry() -> None:
    """Reset the global registry — test helper."""
    global _registry
    if _registry is not None:
        _registry.reset()
    else:
        _registry = MetricsRegistry()


def _format_labels(
    labels: tuple[tuple[str, str], ...],
    *,
    extra: tuple[tuple[str, str], ...] = (),
) -> str:
    items = tuple(sorted(labels + extra))
    if not items:
        return ""
    return "{" + ",".join(f'{key}="{value}"' for key, value in items) + "}"
