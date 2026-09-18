"""Small allow-listed in-process O1 metrics registry."""

from __future__ import annotations

from collections import defaultdict
from typing import Final

ALLOWED_LABELS: Final[frozenset[str]] = frozenset(
    {"backend", "scope", "error_code", "reason", "outcome"}
)


class MetricsRegistry:
    def __init__(self) -> None:
        self._values: defaultdict[tuple[str, tuple[tuple[str, str], ...]], int] = defaultdict(int)

    def increment(self, name: str, *, labels: dict[str, str] | None = None, value: int = 1) -> None:
        labels = labels or {}
        unknown = set(labels) - ALLOWED_LABELS
        if unknown:
            raise ValueError(f"unsupported metric labels: {sorted(unknown)}")
        key = (name, tuple(sorted(labels.items())))
        self._values[key] += value

    def snapshot(self) -> dict[str, int]:
        return {
            f"{name}{_format_labels(labels)}": value
            for (name, labels), value in sorted(self._values.items())
        }


def _format_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{key}="{value}"' for key, value in labels) + "}"
