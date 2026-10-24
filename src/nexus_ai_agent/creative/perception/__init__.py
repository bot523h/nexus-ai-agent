"""Host-side perception adapter: free, offline FFmpeg measurement -> typed evidence.

The pure pack lives in :mod:`nexus_ai_agent.creative.packs.perception`; this
package owns every subprocess and file read (the studio and the packs never do
I/O).  See :mod:`nexus_ai_agent.creative.perception.analyzer`.
"""

from __future__ import annotations

from nexus_ai_agent.creative.perception.analyzer import (
    AnalysisError,
    AnalysisFailedError,
    AnalysisInputError,
    AnalysisTimeoutError,
    AnalysisUnavailableError,
    FfmpegAnalyzer,
)

__all__ = [
    "AnalysisError",
    "AnalysisFailedError",
    "AnalysisInputError",
    "AnalysisTimeoutError",
    "AnalysisUnavailableError",
    "FfmpegAnalyzer",
]
