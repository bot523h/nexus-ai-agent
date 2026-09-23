"""Small allow-listed in-process O1 metrics registry — M0 extended.

M0 adds canonical job metrics with bounded labels and cardinality guards.
"""

from __future__ import annotations

from collections import defaultdict
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


class MetricsRegistry:
    def __init__(self) -> None:
        self._values: defaultdict[tuple[str, tuple[tuple[str, str], ...]], int] = defaultdict(int)

    def increment(self, name: str, *, labels: dict[str, str] | None = None, value: int = 1) -> None:
        labels = labels or {}
        unknown = set(labels) - ALLOWED_LABELS
        if unknown:
            raise ValueError(f"unsupported metric labels: {sorted(unknown)}")
        # High-cardinality guard first: reject values that look like URLs, mentions, or overly long
        for k, v in labels.items():
            if len(v) > 64:
                raise ValueError(f"label value too long for {k}: {len(v)} > 64")
            if "://" in v or v.startswith("@"):
                raise ValueError(f"high-cardinality label value rejected for {k}: {v!r}")
        # Cardinality guard: job_type must be bounded for canonical metrics
        if "job_type" in labels:
            jt = labels["job_type"]
            if name in CANONICAL_JOB_METRICS and jt not in BOUNDED_JOB_TYPES:
                labels = dict(labels)
                labels["job_type"] = "unknown"
        key = (name, tuple(sorted(labels.items())))
        self._values[key] += value

    def snapshot(self) -> dict[str, int]:
        return {
            f"{name}{_format_labels(labels)}": value
            for (name, labels), value in sorted(self._values.items())
        }

    def reset(self) -> None:
        """Reset all counters — test-only, not for production use."""
        self._values.clear()

    def get(self, name: str, labels: dict[str, str] | None = None) -> int:
        labels = labels or {}
        key = (name, tuple(sorted(labels.items())))
        return self._values.get(key, 0)


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


def _format_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{key}="{value}"' for key, value in labels) + "}"
