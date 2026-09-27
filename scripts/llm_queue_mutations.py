#!/usr/bin/env python3
"""Mutation probes for the in-process LLM request queue.

The harness copies the package to a temporary directory, weakens one invariant
at a time, and runs the matching behavioral regression test against that copy.
It never edits the working tree. A green baseline, every mutant killed, and a
green restored copy are required for exit status 0.

Usage::

    python scripts/llm_queue_mutations.py
    python scripts/llm_queue_mutations.py --list
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "nexus_ai_agent"
QUEUE_RELATIVE = Path("features") / "request_queue.py"
TEST_FILE = "tests/unit/test_request_queue.py"


@dataclass(frozen=True)
class Mutation:
    name: str
    old: str
    new: str
    test: str
    invariant: str


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "timed_out_queue_entry_is_removed",
        "            self._remove_queued_locked(req)\n"
        "            req.state = _RequestState.TIMED_OUT if timed_out else _RequestState.CANCELLED",
        "            req.state = _RequestState.TIMED_OUT if timed_out else _RequestState.CANCELLED",
        "test_same_user_timeout_releases_exactly_one_pending_slot",
        "a timed-out request must be removed from its user lane",
    ),
    Mutation(
        "cancelled_queued_entry_is_removed",
        "            self._remove_queued_locked(req)\n"
        "            req.state = _RequestState.TIMED_OUT if timed_out else _RequestState.CANCELLED",
        "            if timed_out:\n"
        "                self._remove_queued_locked(req)\n"
        "            req.state = _RequestState.TIMED_OUT if timed_out else _RequestState.CANCELLED",
        "test_withdrawn_queued_requests_never_run_after_backlog_clears",
        "a caller-cancelled queued request must be removed immediately",
    ),
    Mutation(
        "running_provider_receives_cancellation",
        "            if req.execution_task is not None and not req.execution_task.done():\n"
        "                req.execution_task.cancel()\n"
        "            self._ready_event.set()\n"
        "            return True",
        "            self._ready_event.set()\n            return True",
        "test_caller_cancellation_propagates_to_running_provider_task",
        "caller cancellation must reach the locally running provider task",
    ),
    Mutation(
        "per_user_pending_count_is_decremented_once",
        "        user_count = self._pending_per_user.get(req.user_id, 0) - 1",
        "        user_count = self._pending_per_user.get(req.user_id, 0) - 2",
        "test_same_user_timeout_releases_exactly_one_pending_slot",
        "one terminal transition must release one per-user pending slot",
    ),
    Mutation(
        "provider_attempt_is_accounted",
        "        self._provider_attempts += 1\n",
        "        pass  # mutation: lose physical provider-attempt accounting\n",
        "test_each_typed_retry_consumes_a_separate_provider_attempt",
        "each physical provider invocation must be visible and quota-accounted",
    ),
    Mutation(
        "user_round_robin_is_retained",
        "                if lane:\n"
        "                    rotation.append(user_id)\n"
        "                else:",
        "                if lane:\n"
        "                    pass  # mutation: no next turn for this user's lane\n"
        "                else:",
        "test_round_robin_rotates_users_within_a_priority_tier",
        "one busy user must not drain its entire same-priority backlog first",
    ),
    Mutation(
        "strict_priority_is_preserved_across_tiers",
        "        for priority in Priority:\n            rotation = self._user_rotation[priority]\n",
        "        for priority in reversed(Priority):\n"
        "            rotation = self._user_rotation[priority]\n",
        "test_priority_order_is_preserved_across_user_lanes",
        "higher-priority requests must be selected before lower-priority lanes",
    ),
    Mutation(
        "close_settles_waiters_with_closed_error",
        "            req.future.set_exception(RequestQueueClosedError(_CLOSED_MESSAGE))\n",
        "            req.future.set_result(_CLOSED_MESSAGE)\n",
        "test_close_settles_active_and_queued_waiters_and_rejects_new_work",
        "close must settle every accepted waiter with an explicit closed outcome",
    ),
    Mutation(
        "typed_retry_limit_remains_bounded",
        "                if is_retryable and retry_number < self._max_retries:\n"
        "                    delay = self._retry_delay_seconds(exc, retry_number)\n",
        "                if is_retryable and retry_number <= self._max_retries:\n"
        "                    delay = self._retry_delay_seconds(exc, retry_number)\n",
        "test_typed_retries_stop_at_configured_bound",
        "provider retries must stop after the configured retry count",
    ),
    Mutation(
        "retry_after_minimum_is_not_shortened",
        "                    base = max(base, provider_delay)\n",
        "                    base = base\n",
        "test_retry_after_is_honored_and_excessive_wait_is_not_shortened",
        "a provider Retry-After minimum must not be shortened by local backoff",
    ),
    Mutation(
        "retry_classification_uses_structured_status",
        "        return None\n\n    @staticmethod\n    def _is_retryable_status",
        "        message_code = str(exc)[:3]\n"
        "        return int(message_code) if message_code.isdigit() else None\n\n"
        "    @staticmethod\n"
        "    def _is_retryable_status",
        "test_retry_requires_structured_transient_status_and_honors_disable",
        "provider error text must never be interpreted as an HTTP status",
    ),
    Mutation(
        "daily_quota_rollover_is_rechecked_while_waiting",
        "            self._check_request(req)\n"
        "            self._reset_day_if_needed()\n"
        "            self._clean_minute_timestamps()",
        "            self._check_request(req)\n            self._clean_minute_timestamps()",
        "test_daily_quota_rollover_rechecks_capacity_while_waiting",
        "daily quota must roll over without requiring another provider attempt",
    ),
    Mutation(
        "external_worker_cancellation_is_propagated",
        "                    task.cancel()\n"
        "                    raise\n"
        "                if req.cancel_event.is_set() or self._closed:",
        "                    task.cancel()\n"
        "                    pass  # mutation: worker swallows an external shutdown\n"
        "                if req.cancel_event.is_set() or self._closed:",
        "test_worker_cancellation_settles_active_work_and_close_is_idempotent",
        "an externally cancelled worker must propagate instead of re-running work",
    ),
    Mutation(
        "provider_self_cancel_settles_as_typed_failure",
        "                req.failed = True\n"
        "                log.error(\n"
        '                    "queue_provider_cancelled",',
        "                req.failed = True\n"
        "                raise  # mutation: a provider self-cancel climbs into the worker\n"
        "                log.error(\n"
        '                    "queue_provider_cancelled",',
        "test_provider_self_cancellation_is_typed_failure_and_worker_survives",
        "a provider cancelling itself must settle as a typed failure, never kill the worker",
    ),
    Mutation(
        "caller_timeout_catch_is_version_independent",
        "        except _timeout_errors():\n"
        "            cancelled = await self._cancel_request(req, timed_out=True)",
        "        except TimeoutError:  # mutation: 3.10 timeout-class split returns\n"
        "            cancelled = await self._cancel_request(req, timed_out=True)",
        "test_submit_timeout_survives_py310_timeout_class_split",
        "a caller timeout must be recognized on Python 3.10, not escape raw",
    ),
    Mutation(
        "capacity_wait_timeout_catch_is_version_independent",
        "        except _timeout_errors():\n"
        "            return req.cancel_event.is_set() or self._closed\n"
        "        return True",
        "        except TimeoutError:  # mutation: 3.10 timeout-class split returns\n"
        "            return req.cancel_event.is_set() or self._closed\n"
        "        return True",
        "test_capacity_quota_window_wait_survives_py310_timeout_class_split",
        "a worker-side capacity-wait timeout must not become a fake provider error on 3.10",
    ),
)


def _run_pytest(package_root: Path, test: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    python_paths = [str(package_root), str(ROOT / "src")]
    if env.get("PYTHONPATH"):
        python_paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_paths)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "asyncio_mode=auto",
            test,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _tail(output: str, lines: int = 8) -> str:
    return "\n".join(output.strip().splitlines()[-lines:])


def _is_test_failure(result: subprocess.CompletedProcess[str]) -> bool:
    output = result.stdout + result.stderr
    return result.returncode == 1 and re.search(r"(?m)^\d+ failed(?:,|\s|$)", output) is not None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        for mutation in MUTATIONS:
            print(f"{mutation.name}: {mutation.invariant}")
        return 0

    with tempfile.TemporaryDirectory(prefix="nexus-llm-queue-mutations-") as temporary:
        package_root = Path(temporary) / "src"
        shutil.copytree(PACKAGE, package_root / "nexus_ai_agent")
        queue_path = package_root / "nexus_ai_agent" / QUEUE_RELATIVE

        print("baseline ... ", end="", flush=True)
        baseline = _run_pytest(package_root, TEST_FILE)
        if baseline.returncode != 0:
            print("RED — refusing to mutate a failing baseline")
            print(_tail(baseline.stdout + baseline.stderr, lines=30))
            return 2
        print("GREEN")

        killed = 0
        survivors: list[str] = []
        for mutation in MUTATIONS:
            current = queue_path.read_text(encoding="utf-8")
            if mutation.old not in current:
                print(f"{mutation.name}: DOES NOT APPLY — source drifted")
                survivors.append(mutation.name)
                continue

            queue_path.write_text(current.replace(mutation.old, mutation.new, 1), encoding="utf-8")
            try:
                result = _run_pytest(
                    package_root,
                    f"{TEST_FILE}::{mutation.test}",
                )
            finally:
                queue_path.write_text(current, encoding="utf-8")

            if result.returncode == 0:
                print(f"{mutation.name}: SURVIVED — {mutation.invariant}")
                survivors.append(mutation.name)
            elif _is_test_failure(result):
                killed += 1
                print(f"{mutation.name}: killed")
            else:
                print(f"{mutation.name}: ERROR — mutant did not produce a pytest failure")
                print(_tail(result.stdout + result.stderr, lines=30))
                return 4

        print("restored baseline ... ", end="", flush=True)
        restored = _run_pytest(package_root, TEST_FILE)
        if restored.returncode != 0:
            print("RED — restored copy failed")
            print(_tail(restored.stdout + restored.stderr, lines=30))
            return 3
        print("GREEN")

    if survivors:
        print(f"{killed}/{len(MUTATIONS)} mutants killed; survivors: {', '.join(survivors)}")
        return 1
    print(f"{killed}/{len(MUTATIONS)} mutants killed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
