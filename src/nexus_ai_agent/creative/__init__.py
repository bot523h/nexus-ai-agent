"""Creative subsystem.

This package re-exports its legacy helpers lazily (PEP 562). The legacy
modules (``ffmpeg_executor``, ``video_director``, ``job_registry``) pull heavy
optional dependencies, and eagerly importing them here meant that importing the
pure ``creative.studio`` / ``creative.spine`` cores required those dependencies
too. Lazy re-export keeps the public names (``from nexus_ai_agent.creative
import render_jobs`` and the historical ``__all__`` surface) working while
letting the dependency-free cores import in isolation.
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "Caption",
    "Cut",
    "FFmpegResult",
    "JobRegistry",
    "VideoEditPlan",
    "Zoom",
    "analyze_video_with_gemini",
    "execute_ffmpeg_commands",
]

#: public name -> (module, attribute).
_LAZY: dict[str, tuple[str, str]] = {
    "FFmpegResult": ("nexus_ai_agent.creative.ffmpeg_executor", "FFmpegResult"),
    "execute_ffmpeg_commands": (
        "nexus_ai_agent.creative.ffmpeg_executor",
        "execute_ffmpeg_commands",
    ),
    "JobRegistry": ("nexus_ai_agent.creative.job_registry", "JobRegistry"),
    "Caption": ("nexus_ai_agent.creative.video_director", "Caption"),
    "Cut": ("nexus_ai_agent.creative.video_director", "Cut"),
    "VideoEditPlan": ("nexus_ai_agent.creative.video_director", "VideoEditPlan"),
    "Zoom": ("nexus_ai_agent.creative.video_director", "Zoom"),
    "analyze_video_with_gemini": (
        "nexus_ai_agent.creative.video_director",
        "analyze_video_with_gemini",
    ),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        module_name, attribute = _LAZY[name]
        value = getattr(importlib.import_module(module_name), attribute)
        globals()[name] = value
        return value
    try:
        return importlib.import_module(f"{__name__}.{name}")
    except ModuleNotFoundError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc


def __dir__() -> list[str]:
    return sorted(__all__)
