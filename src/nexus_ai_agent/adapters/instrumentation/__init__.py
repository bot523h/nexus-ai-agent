"""Runtime instrumentation for the durable job queue (M0 integration).

Wraps :class:`InProcessJobQueue` at the composition roots so the five queue
events become observable without editing the queue adapter itself (that file
is owned by the Research-V2 claim-lease work).
"""

from nexus_ai_agent.adapters.instrumentation.job_instrumentation import (
    InstrumentedJobQueue,
    SaturationReport,
    classify_failure_error,
)

__all__ = ["InstrumentedJobQueue", "SaturationReport", "classify_failure_error"]
