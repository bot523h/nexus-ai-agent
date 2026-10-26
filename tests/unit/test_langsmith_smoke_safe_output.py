"""The LangSmith connectivity smoke must never leak secrets (task-274).

``scripts/langsmith_smoke.py`` prints a ``RESULT=`` line that CI records as
evidence, so that line is a public surface.  A failing SDK/HTTP error can embed
the API key, the request URL (with credentials or query parameters) and the
request payload (prompt/trace content); these tests prove the smoke reports an
*allow-listed* diagnostic only — the exception class and an optional status
code — and never echoes any of those values to stdout or stderr.

The script is loaded by path (``scripts/`` is unpackaged — see
``tests/architecture/test_scripts_import_boundary.py``), matching the existing
``test_ci_extras_parity`` / ``test_agent_board`` pattern.  ``langsmith.Client``
is replaced with a synthetic raiser so the failure path runs without a network.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import types
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "langsmith_smoke.py"

SENTINEL_KEY = "sk-live-SENTINELKEY0123456789"
SENTINEL_URL = "https://user:SENTINELPASS@collector.example.test/runs?token=SENTINELTOK"
SENTINEL_PAYLOAD = "SENTINELPROMPT-payload-content"
SENTINELS = (SENTINEL_KEY, SENTINEL_URL, SENTINEL_PAYLOAD, "SENTINELPASS", "SENTINELTOK")


def _load_smoke() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("langsmith_smoke_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(smoke: types.ModuleType) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = smoke.main()
    return code, out.getvalue() + err.getvalue()


def test_missing_secret_is_a_visible_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    smoke = _load_smoke()
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    code, combined = _run(smoke)
    assert code == 0
    assert "RESULT=SKIPPED_NO_SECRET" in combined


def test_failure_output_never_leaks_key_url_or_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    smoke = _load_smoke()

    class _RaisingClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError(
                f"api_key={SENTINEL_KEY} url={SENTINEL_URL} payload={SENTINEL_PAYLOAD}"
            )

    fake = types.ModuleType("langsmith")
    fake.Client = _RaisingClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langsmith", fake)
    monkeypatch.setenv("LANGSMITH_API_KEY", SENTINEL_KEY)

    code, combined = _run(smoke)
    assert code == 1
    assert "RESULT=FAILED" in combined
    assert "RuntimeError" in combined  # the allow-listed class name survives
    for sentinel in SENTINELS:
        assert sentinel not in combined, f"smoke leaked {sentinel!r} into its output"


def test_status_code_is_reported_without_the_message(monkeypatch: pytest.MonkeyPatch) -> None:
    smoke = _load_smoke()

    class _StatusError(Exception):
        status_code = 403

    class _RaisingClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise _StatusError(f"key={SENTINEL_KEY} url={SENTINEL_URL}")

    fake = types.ModuleType("langsmith")
    fake.Client = _RaisingClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langsmith", fake)
    monkeypatch.setenv("LANGSMITH_API_KEY", SENTINEL_KEY)

    code, combined = _run(smoke)
    assert code == 1
    assert "_StatusError status=403" in combined
    for sentinel in SENTINELS:
        assert sentinel not in combined


def test_success_path_emits_ok_with_synthetic_data(monkeypatch: pytest.MonkeyPatch) -> None:
    smoke = _load_smoke()
    calls: list[dict[str, object]] = []

    class _RecordingClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def create_run(self, **kwargs: object) -> None:
            calls.append(kwargs)

        def flush(self) -> None:
            pass

    fake = types.ModuleType("langsmith")
    fake.Client = _RecordingClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langsmith", fake)
    monkeypatch.setenv("LANGSMITH_API_KEY", SENTINEL_KEY)
    monkeypatch.setenv("LANGSMITH_PROJECT", "nexus-ai-agent-smoke")

    code, combined = _run(smoke)
    assert code == 0
    assert "RESULT=OK" in combined
    assert len(calls) == 2  # a parent/child pair proves trace ingest
