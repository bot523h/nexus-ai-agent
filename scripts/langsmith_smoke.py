#!/usr/bin/env python3
"""Prove the configured LangSmith key can actually accept a trace (task-274).

This is a *connectivity* smoke, not a unit test: it speaks to the real
collector, so it can only run where a tracing key is present.  It therefore
fails *visible*, never *green*:

* no ``LANGSMITH_API_KEY`` in the environment → ``RESULT=SKIPPED_NO_SECRET``
  and exit 0.  A missing secret is a visible skip; a fabricated pass would be
  the exact lie this check exists to prevent.
* the key is present but the endpoint refuses it → ``RESULT=FAILED`` and a
  non-zero exit, with the error *type* only (never the key, never the payload).

The emitted ``RESULT=`` line is the evidence CI records.
"""

from __future__ import annotations

import os
import sys
import uuid


def _emit(result: str, detail: str = "") -> None:
    line = f"RESULT={result}"
    if detail:
        line += f" DETAIL={detail}"
    print(line, flush=True)


def main() -> int:
    api_key = os.environ.get("LANGSMITH_API_KEY")
    if not api_key:
        _emit("SKIPPED_NO_SECRET", "LANGSMITH_API_KEY is not set")
        return 0

    try:
        from langsmith import Client
    except ImportError:
        _emit("FAILED", "langsmith SDK not installed (it ships with the core install)")
        return 2

    project = os.environ.get("LANGSMITH_PROJECT", "nexus-ai-agent-smoke")
    endpoint = os.environ.get("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")
    trace_id = uuid.uuid4()
    try:
        client = Client(api_key=api_key, api_url=endpoint)
        # A parent/child pair exercises the real run-ingest path the runtime
        # would use — a single ping would not prove trace ingest works.
        client.create_run(
            name="nexus-smoke-parent",
            run_type="chain",
            inputs={"probe": "connectivity"},
            id=trace_id,
            project_name=project,
        )
        client.create_run(
            name="nexus-smoke-child",
            run_type="tool",
            inputs={"probe": "connectivity"},
            parent_run_id=trace_id,
            project_name=project,
        )
        client.flush()
    except Exception as exc:  # noqa: BLE001 - the whole point is to report any failure
        _emit("FAILED", f"{type(exc).__name__}: {exc}")
        return 1

    _emit("OK", f"project={project}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
