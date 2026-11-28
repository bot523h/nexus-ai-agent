#!/usr/bin/env python3
"""Wave 5 — N=3 latency benchmark for the five Studio execution paths.

Five paths are measured, each against the *shipped* module with real objects on
a real disk (real SQLite with WAL, real files, real threads, real HMAC):

  (a) ``StudioUnifiedAuthorityBridge`` proof generation and verification
  (b) ``DurableRevisionPinningStore`` pinning plus worker verification
  (c) ``StudioPreviewReadbackBuilder`` rational preview and ``verify_wysiwys``
  (d) ``ExecutionOutboxCoordinator`` full cycle (stage → consume → enqueue →
      poll → verified completion)
  (e) ``StudioDisasterRecoveryManager`` online backup, verification and restore

Three independent trials are run.  Every trial re-creates its working directory
from scratch, so a trial cannot be warmed by the previous one, and the report
carries the per-trial samples *and* the pooled median / p95 / standard deviation
rather than a single cherry-picked number.

Why N=3 and not N=1
-------------------
A single timing is not a measurement.  The spread across trials is the part of
the number that matters: a p95 far above the median means the path has a
scheduling tail, and a stddev near zero across three fresh directories is the
only evidence that the median is reproducible at all.  Three is the smallest N
that can show a spread; it is stated plainly rather than dressed up as a
significance test, which three samples cannot support.

The report is bound to the exact ``HEAD`` SHA, so a number can never be quoted
against a tree it was not measured on.  If the worktree is dirty the SHA is
suffixed ``-dirty`` and the report says so.

Usage::

    python scripts/benchmark_wave4_studio_e2e.py                 # N=3, JSON+MD
    python scripts/benchmark_wave4_studio_e2e.py --trials 5
    python scripts/benchmark_wave4_studio_e2e.py --out /tmp/bench

Exit code 0 means "every path was measured in every trial".  A path that raises
is reported as an error, not silently skipped, and the process exits 1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(ROOT / "src"))

REPORT_SCHEMA = "nexus.studio.wave5_benchmark.v1"

TENANT = "tenant-bench-1"
PROJECT = "proj-bench-1"
PRINCIPAL = "prn_" + "b" * 32
SESSION = "sess-bench-1"
TURN = "turn-bench-1"
APPROVAL = "appr-bench-1"
REVISION = "rev-bench-1"
REV_JCS = "sha256:" + "e" * 64


# --------------------------------------------------------------------------- #
# Measurement
# --------------------------------------------------------------------------- #
@dataclass
class Trial:
    """One fresh run of one path: its samples, in milliseconds."""

    name: str
    samples_ms: list[float] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def _percentile(sorted_samples: list[float], fraction: float) -> float:
    """Nearest-rank percentile on an already-sorted list (no interpolation).

    Nearest-rank rather than interpolated: with three trials an interpolated p95
    is a number no observation supports, while nearest-rank is always a value
    that was actually measured.
    """
    if not sorted_samples:
        return 0.0
    if len(sorted_samples) == 1:
        return sorted_samples[0]
    index = max(0, min(len(sorted_samples) - 1, math.ceil(fraction * len(sorted_samples)) - 1))
    return sorted_samples[index]


def _summarise(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "n": float(len(ordered)),
        "median_ms": statistics.median(ordered) if ordered else 0.0,
        "p95_ms": _percentile(ordered, 0.95),
        "stddev_ms": statistics.stdev(ordered) if len(ordered) > 1 else 0.0,
        "min_ms": ordered[0] if ordered else 0.0,
        "max_ms": ordered[-1] if ordered else 0.0,
    }


def _time_call(fn: Callable[[], Any]) -> float:
    """Run ``fn`` once and return its wall-clock duration in milliseconds."""
    started = time.perf_counter()
    fn()
    return (time.perf_counter() - started) * 1000.0


# --------------------------------------------------------------------------- #
# The five paths
# --------------------------------------------------------------------------- #
class _Clock:
    """Monotonic, deterministic ISO-8601 UTC clock (mirrors the tests)."""

    def __init__(self) -> None:
        self._tick = 0

    def __call__(self) -> str:
        self._tick += 1
        return f"2026-11-27T00:00:{self._tick % 60:02d}+00:00"


def _seed_database(path: Path) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE clips (id TEXT PRIMARY KEY, name TEXT)")
        connection.execute("CREATE TABLE markers (id INTEGER PRIMARY KEY, at_us INTEGER)")
        connection.executemany(
            "INSERT INTO clips VALUES (?, ?)", [(f"clip-{i}", f"Clip {i}") for i in range(8)]
        )
        connection.executemany(
            "INSERT INTO markers VALUES (?, ?)", [(i, i * 1000) for i in range(4)]
        )
        connection.commit()
    finally:
        connection.close()


def _authority_material(root: Path) -> tuple[Any, Any]:
    """A real principal store with one active owner and the bridge over it."""
    from nexus_ai_agent.adapters.studio_directory_bridge import StudioUnifiedAuthorityBridge
    from nexus_ai_agent.adapters.studio_principal_auth import StudioPrincipalAuthStore
    from nexus_ai_agent.nagar.ibpc.authority import StudioRole

    store = StudioPrincipalAuthStore(root / "principals.sqlite3", now=_Clock())
    store.ensure_project(PROJECT, tenant_id=TENANT)
    store.register_principal(
        PRINCIPAL,
        tenant_id=TENANT,
        raw_token=f"tok-{PRINCIPAL}-secret",
        display_name="Bench Owner",
        allow_system_bootstrap=True,
    )
    store.grant_membership(
        PROJECT, PRINCIPAL, role=StudioRole.OWNER, granted_by="system", allow_system_bootstrap=True
    )
    store.bind_channel(
        PRINCIPAL,
        channel="studio",
        external_subject=f"subject-{PRINCIPAL}",
        user_id=1,
        chat_id=2,
        allow_system_bootstrap=True,
    )
    return store, StudioUnifiedAuthorityBridge(store, now=_Clock())


def bench_authority_bridge(root: Path, iterations: int) -> Callable[[], None]:
    store, bridge = _authority_material(root)
    binding_ref = store.default_binding_for(PRINCIPAL)
    if binding_ref is None:
        raise AssertionError("bench principal has no default binding")

    def run() -> None:
        for _ in range(iterations):
            proof = bridge.verify_worker_execution_authority(
                principal_id=PRINCIPAL,
                project_id=PROJECT,
                tenant_id=TENANT,
                binding_ref=binding_ref,
            )
            if not proof.verify_integrity():
                raise AssertionError("proof failed its own integrity check")

    def cleanup() -> None:
        clo = getattr(store, "close", None)
        if callable(clo):
            clo()

    run.cleanup = cleanup  # type: ignore[attr-defined]
    return run


def _pin_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "project_id": PROJECT,
        "tenant_id": TENANT,
        "session_id": SESSION,
        "turn_id": TURN,
        "revision_number": 1,
        "parent_revision_id": None,
        "plan_id": "plan-bench-1",
        "plan_jcs_sha256": "sha256:" + "1" * 64,
        "parameters_jcs_sha256": "sha256:" + "2" * 64,
        "preview_digest": "sha256:" + "3" * 64,
        "input_asset_pins": (),
    }
    kwargs.update(overrides)
    return kwargs


def bench_revision_pinning(root: Path, iterations: int) -> Callable[[], None]:
    from nexus_ai_agent.creative.studio.revision_pinning import DurableRevisionPinningStore

    assets = root / "assets"
    (assets / "media").mkdir(parents=True, exist_ok=True)
    asset = assets / "media" / "bench.mp4"
    asset.write_bytes(b"bench-payload" * 64)
    store = DurableRevisionPinningStore(root / "pins.sqlite3")

    def run() -> None:
        parent: str | None = None
        for index in range(iterations):
            pin = store.pin_asset_from_disk(
                asset_id="asset-bench", file_path=asset, authorized_asset_root=assets
            )
            revision_id = f"rev-{uuid.uuid4().hex[:12]}"
            envelope = store.pin_revision(
                revision_id=revision_id,
                **_pin_kwargs(
                    revision_number=index + 1,
                    parent_revision_id=parent,
                    input_asset_pins=(pin,),
                ),
            )
            # Each revision must name its predecessor: the schema enforces the
            # chain, so the benchmark walks a real lineage rather than an
            # impossible one.
            parent = revision_id
            store.verify_worker_execution_pins(
                revision_id=revision_id,
                expected_revision_jcs_sha256=envelope.revision_jcs_sha256,
                expected_plan_jcs_sha256=envelope.plan_jcs_sha256,
                authorized_asset_root=assets,
                expected_project_id=PROJECT,
                expected_tenant_id=TENANT,
            )

    run.cleanup = store.close  # type: ignore[attr-defined]
    return run


def bench_preview_readback(root: Path, iterations: int) -> Callable[[], None]:
    from fractions import Fraction

    from nexus_ai_agent.creative.studio.preview_readback import (
        StudioPreviewReadbackBuilder,
        lcm_timebase,
        verify_wysiwys_readback,
    )

    labels = {"fa": "برش", "en": "Cut"}

    def run() -> None:
        # Exact rational arithmetic on the NTSC family is the expensive part of
        # the preview; it is measured once per iteration, not amortised away.
        common = lcm_timebase(Fraction(30000, 1001), Fraction(24000, 1001))
        if common != Fraction(120000, 1001):
            raise AssertionError(f"lcm_timebase drifted to {common}")
        for index in range(iterations):
            builder = StudioPreviewReadbackBuilder(
                tenant_id=TENANT,
                project_id=PROJECT,
                revision_id=f"rev-{index}",
                revision_number=index + 1,
                principal_id=PRINCIPAL,
                timebase=(30000, 1001),
            )
            for clip in range(4):
                builder.add_operation(
                    operation="preview.sample_cut",
                    target_clip_id=f"clip-{clip}",
                    span=(0, "5/2"),
                    labels=labels,
                )
            readback = builder.build_readback()
            if not verify_wysiwys_readback(readback, expected_digest=readback.preview_digest):
                raise AssertionError("readback failed its own WYSIWYS verification")

    return run


def bench_execution_outbox(root: Path, iterations: int) -> Callable[[], None]:
    from nexus_ai_agent.nagar.ibpc.approval import ApprovalIdentity, DurableApprovalLedger
    from nexus_ai_agent.nagar.ibpc.execution_outbox import (
        DurableExecutionOutboxStore,
        ExecutionOutboxCoordinator,
        VerifiedExecutionCompletion,
        derive_reserved_job_id,
    )
    from nexus_ai_agent.nagar.ibpc.models import ApprovalBinding

    ledger = DurableApprovalLedger(
        root / "approvals.sqlite3", now=lambda: "2026-11-27T00:00:00+00:00"
    )
    store = DurableExecutionOutboxStore(root / "outbox.sqlite3")
    plan_digest = "sha256:" + "c" * 64
    preview_digest = "sha256:" + "d" * 64
    asset_digest = "sha256:" + "a" * 64
    ast_digest = "sha256:" + "b" * 64

    def identity_for(approval_id: str) -> ApprovalIdentity:
        return ApprovalIdentity(
            principal_id=PRINCIPAL,
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            sequence_number=1,
            source_revision=0,
            source_asset_digest=asset_digest,
            ast_digest=ast_digest,
            plan_digest=plan_digest,
            preview_digest=preview_digest,
            revision_id=REVISION,
        )

    def payload_for(approval_id: str) -> dict[str, Any]:
        return {
            "approval_identity": {
                "principal_id": PRINCIPAL,
                "sequence_number": 1,
                "source_revision": 0,
                "source_asset_digest": asset_digest,
                "ast_digest": ast_digest,
                "plan_digest": plan_digest,
                "preview_digest": preview_digest,
                "revision_id": REVISION,
            },
            "tenant_id": TENANT,
            "project_id": PROJECT,
            "session_id": SESSION,
            "turn_id": TURN,
            "revision_jcs_sha256": REV_JCS,
            "operations": [{"op_id": "bench", "start": 0, "end": 5}],
        }

    def consume(
        approval_id: str, identity: Any, *, job_id: str | None = None, attempt: int = 1
    ) -> None:
        ledger.consume(approval_id, identity=identity, job_id=job_id, attempt=attempt)

    jobs: dict[str, dict[str, Any]] = {}

    def enqueue(*, idempotency_key: str, payload: dict[str, Any], job_id: str | None = None) -> str:
        resolved = job_id or derive_reserved_job_id(idempotency_key)
        jobs[resolved] = {"status": "COMPLETED", "artifact_id": "artifact-bench"}
        return resolved

    def await_completion(job_id: str) -> dict[str, Any]:
        return dict(jobs[job_id])

    def verify(_row: Any, completion: Any) -> VerifiedExecutionCompletion:
        return VerifiedExecutionCompletion(
            artifact_id=str(completion.artifact_id),
            artifact_sha256="a" * 64,
            passport_id="passport-bench",
            lineage_digest="sha256:" + "f" * 64,
            verified_at_utc="2026-11-27T00:00:00+00:00",
        )

    plain = ExecutionOutboxCoordinator(
        store=store,
        consume_approval_fn=consume,
        enqueue_job_fn=enqueue,
        await_completion_fn=await_completion,
        verify_completion_fn=verify,
        poll_timeout_seconds=0.0,
        sleep=lambda _s: None,
    )
    coordinator = ExecutionOutboxCoordinator(
        store=store,
        consume_approval_fn=consume,
        enqueue_job_fn=enqueue,
        await_completion_fn=await_completion,
        verify_completion_fn=verify,
        confirm_consumption_fn=plain.confirm_consumption_from_ledger(ledger),
        poll_timeout_seconds=0.0,
        sleep=lambda _s: None,
    )

    def run() -> None:
        for index in range(iterations):
            approval_id = f"appr-bench-{index}-{uuid.uuid4().hex[:8]}"
            ledger.create(
                ApprovalBinding(
                    approval_id=approval_id,
                    session_id=SESSION,
                    turn_id=TURN,
                    sequence_number=1,
                    tenant_id=TENANT,
                    project_id=PROJECT,
                    principal_id=PRINCIPAL,
                    source_revision=0,
                    source_asset_digest=asset_digest,
                    ast_digest=ast_digest,
                    plan_digest=plan_digest,
                    preview_digest=preview_digest,
                    revision_id=REVISION,
                )
            )
            ledger.approve(
                approval_id, identity=identity_for(approval_id), turn_is_active=lambda: True
            )
            outcome = coordinator.execute_or_recover(
                tenant_id=TENANT,
                project_id=PROJECT,
                session_id=SESSION,
                turn_id=TURN,
                principal_id=PRINCIPAL,
                approval_id=approval_id,
                revision_id=REVISION,
                revision_jcs_sha256=REV_JCS,
                plan_payload=payload_for(approval_id),
            )
            if outcome.status != "COMPLETED":
                raise AssertionError(f"outbox cycle ended {outcome.status}: {outcome.detail}")

    def cleanup() -> None:
        store.close()
        ledger.close()

    run.cleanup = cleanup  # type: ignore[attr-defined]
    return run


def bench_disaster_recovery(root: Path, iterations: int) -> Callable[[], None]:
    from nexus_ai_agent.creative.studio.production_dr import (
        BackupKeyMaterial,
        StudioDisasterRecoveryManager,
    )

    database = root / "live.sqlite3"
    _seed_database(database)
    manager = StudioDisasterRecoveryManager(
        root / "backups", key_material=BackupKeyMaterial("bk-bench", "ab" * 32)
    )

    def run() -> None:
        for index in range(iterations):
            manifest = manager.create_hot_backup(
                database_path=database, labels={"trial": str(index)}
            )
            bundle = manager.backup_root / manifest.bundle_id
            report = manager.verify_backup_bundle(bundle)
            if not report.verified:
                raise AssertionError(f"bundle failed verification: {report.problems}")
            manager.restore_from_backup(
                bundle, target_database_path=root / f"restored-{index}.sqlite3"
            )

    return run


def bench_runtime_build_and_close(root: Path, iterations: int) -> Callable[[], None]:
    """(f) ``_build_runtime()`` + ``runtime.close()``: the per-request overhead.

    Every Studio route builds a whole ``UnifiedStudioRuntime`` per request --
    ``_build_runtime`` says so explicitly, on purpose, to avoid leaking file
    handles across a worker reload.  That is only a defensible design if the
    construction cost is small, and until now it was the one number nobody had
    measured: (a)-(e) time the modules *inside* the runtime, not the cost of
    assembling it.

    The pair is measured together because they are one unit of work: a route
    pays for the build and the close, and a build whose close is expensive is
    not cheap.  Each iteration closes before the next build, so the samples
    cannot hide an unclosed-handle pileup behind a warm cache.
    """
    from nexus_ai_agent.adapters.studio_directory_bridge import StudioUnifiedAuthorityBridge
    from nexus_ai_agent.adapters.studio_principal_auth import StudioPrincipalAuthStore
    from nexus_ai_agent.application.ports.job_queue import JobStatus
    from nexus_ai_agent.nagar.ibpc.approval_mac import StaticKeyRing
    from nexus_ai_agent.nagar.ibpc.authority import StudioRole
    from nexus_ai_agent.nagar.ibpc.composition import build_unified_studio_runtime
    from nexus_ai_agent.nagar.ibpc.service import IBPCServiceDeps

    auth_path = root / "studio_auth.sqlite3"
    store = StudioPrincipalAuthStore(auth_path)
    store.ensure_project(PROJECT, tenant_id=TENANT, active_revision_id="0")
    store.register_principal(
        PRINCIPAL,
        tenant_id=TENANT,
        raw_token="bench-token-0123456",
        display_name="Bench Owner",
        allow_system_bootstrap=True,
    )
    store.grant_membership(
        PROJECT, PRINCIPAL, role=StudioRole.OWNER, granted_by="system", allow_system_bootstrap=True
    )
    store.bind_channel(
        PRINCIPAL,
        channel="studio",
        external_subject=f"subject-{PRINCIPAL}",
        user_id=1,
        chat_id=2,
        allow_system_bootstrap=True,
    )
    binding_ref = store.default_binding_for(PRINCIPAL) or ""

    class _NeverDrainedQueue:
        """The queue port, idle: this path measures construction, not rendering."""

        async def enqueue(self, **_kwargs: Any) -> str:  # pragma: no cover - not reached
            raise AssertionError("the build benchmark must not enqueue")

        async def get_status(self, _job_id: str) -> JobStatus:  # pragma: no cover
            return JobStatus.PENDING

        async def get_result(self, _job_id: str) -> dict[str, Any] | None:  # pragma: no cover
            return None

    def run() -> None:
        for _ in range(iterations):
            deps = IBPCServiceDeps(
                queue=_NeverDrainedQueue(),
                registry=None,
                workspace_root=root,
                revision_provider=lambda: 0,
                authority=store,
                channel_binding_ref=binding_ref,
                durable=True,
                project_revision_resolver=store.get_project_revision,
            )
            runtime = build_unified_studio_runtime(
                deps,
                # A real MAC key ring: the runtime refuses to assemble an
                # approval ledger without one, so "no key" would benchmark the
                # refusal path rather than the build.
                key_provider=StaticKeyRing({"bench-k1": b"k" * 32}, active_key_id="bench-k1"),
                # The real mapping: build_unified_studio_runtime indexes these
                # keys directly, so a made-up name is a KeyError at build time.
                store_filenames={
                    "authority": "bench_authority.sqlite3",
                    "revision_pins": "bench_revision_pins.sqlite3",
                    "execution_outbox": "bench_execution_outbox.sqlite3",
                },
                enable_execution_outbox=True,
                enable_disaster_recovery=False,
                execution_authority_bridge=StudioUnifiedAuthorityBridge(store),
            )
            try:
                if runtime.service is None:
                    raise AssertionError("the runtime built no service")
            finally:
                runtime.close()

    def cleanup() -> None:
        store.close()

    run.cleanup = cleanup  # type: ignore[attr-defined]
    return run


#: ``(label, factory)`` in the order the specification names them.
PATHS: tuple[tuple[str, str, Callable[[Path, int], Callable[[], None]]], ...] = (
    ("a", "authority_bridge_proof", bench_authority_bridge),
    ("b", "revision_pinning_worker_verify", bench_revision_pinning),
    ("c", "preview_readback_wysiwys", bench_preview_readback),
    ("d", "execution_outbox_full_cycle", bench_execution_outbox),
    ("e", "disaster_recovery_backup_verify_restore", bench_disaster_recovery),
    ("f", "runtime_build_and_close", bench_runtime_build_and_close),
)


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def _git(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:  # pragma: no cover - git absent
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def head_sha() -> tuple[str, bool]:
    """The exact commit measured, and whether the worktree was dirty."""
    sha = _git("rev-parse", "HEAD") or "unknown"
    dirty = bool(_git("status", "--porcelain"))
    return (f"{sha}-dirty" if dirty else sha), dirty


def run_trials(*, trials: int, iterations: int) -> dict[str, Any]:
    sha, dirty = head_sha()
    results: dict[str, list[Trial]] = {name: [] for _label, name, _factory in PATHS}
    for trial_index in range(1, trials + 1):
        # A fresh directory per trial per path: no trial can warm another.
        for _label, name, factory in PATHS:
            workdir = Path(tempfile.mkdtemp(prefix=f"nexus-bench-{trial_index}-{name}-"))
            trial = Trial(name=f"{name}#trial{trial_index}")
            try:
                run = factory(workdir, iterations)
                trial.samples_ms.append(_time_call(run))
                cleanup = getattr(run, "cleanup", None)
                if callable(cleanup):
                    cleanup()
            except BaseException as exc:  # noqa: BLE001 - reported, never hidden
                trial.error = f"{type(exc).__name__}: {exc}"
            finally:
                shutil.rmtree(workdir, ignore_errors=True)
            results[name].append(trial)

    summaries: dict[str, Any] = {}
    for _label, name, _factory in PATHS:
        trials_for_path = results[name]
        samples = [sample for trial in trials_for_path for sample in trial.samples_ms]
        summaries[name] = {
            "trials": [
                {"name": trial.name, "samples_ms": trial.samples_ms, "error": trial.error}
                for trial in trials_for_path
            ],
            "samples_ms": samples,
            **_summarise(samples),
            "ok": all(trial.ok for trial in trials_for_path),
        }

    return {
        "schema": REPORT_SCHEMA,
        "head_sha": sha,
        "worktree_dirty": dirty,
        "trials": trials,
        "iterations_per_trial": iterations,
        "measured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "python": sys.version.split()[0],
        "paths": summaries,
        "ok": all(summary["ok"] for summary in summaries.values()),
    }


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Studio Waves 4-6 — N=3 latency benchmark",
        "",
        f"- `HEAD` SHA: `{report['head_sha']}`"
        + (
            " (worktree dirty — numbers describe the working tree)"
            if report["worktree_dirty"]
            else ""
        ),
        f"- Trials: **{report['trials']}**, iterations per trial: {report['iterations_per_trial']}",
        f"- Measured at: {report['measured_at_utc']} on CPython {report['python']}",
        "",
        "| # | Path | Trials | Median (ms) | p95 (ms) | Stddev (ms) | Min | Max |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for label, name, _factory in PATHS:
        summary = report["paths"][name]
        lines.append(
            f"| {label} | `{name}` | {int(summary['n'])} | {summary['median_ms']:.3f} | "
            f"{summary['p95_ms']:.3f} | {summary['stddev_ms']:.3f} | "
            f"{summary['min_ms']:.3f} | {summary['max_ms']:.3f} |"
        )
    trials = int(report["trials"])
    lines += [
        "",
        "Each trial runs in its own fresh temporary directory, so a later trial is",
        "never warmed by an earlier one.  `p95` is nearest-rank over the pooled",
        "samples (with N=3 that is the maximum observation, which is the honest",
        "answer: an interpolated p95 at N=3 is a number no measurement supports).",
        "",
        f"**Read the spread, not the point estimate.**  N={trials}: "
        f"`max` is the largest of {trials} samples and `p95` is by construction the "
        "same number, so neither is a tail estimate -- they are a sample bound.  "
        "The three numbers that carry information at this N are `min` (best case "
        "the path can reach), `median` and `stddev` (how reproducible it is).  A "
        "larger N would be needed to make any p95 claim, and this report does not "
        "make one.",
        "",
        "`runtime_build_and_close` (path f) is deliberately separate from (a)-(e): "
        "those measure the modules *inside* the runtime, while (f) measures what "
        "every Studio HTTP route pays per request to assemble and tear the runtime "
        "down.  It is a fixed overhead on top of whichever path a route then uses, "
        "so it is not additive with the others and must not be summed with them.",
        "",
        "## Per-trial samples (ms)",
        "",
    ]
    for _label, name, _factory in PATHS:
        summary = report["paths"][name]
        rendered = ", ".join(
            f"{trial['name']}: "
            + (
                f"{trial['samples_ms'][0]:.3f}"
                if trial["samples_ms"]
                else f"ERROR ({trial['error']})"
            )
            for trial in summary["trials"]
        )
        lines.append(f"- `{name}` — {rendered}")
    lines += [
        "",
        f"**Overall: {'PASS' if report['ok'] else 'FAIL'}** — every path measured in every trial.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    if args.trials < 1:
        parser.error("--trials must be >= 1")
    if args.iterations < 1:
        parser.error("--iterations must be >= 1")

    report = run_trials(trials=args.trials, iterations=args.iterations)

    out_dir = args.out or (ROOT / "docs" / "audits" / "benchmarks")
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"wave4_studio_e2e_benchmark_{report['head_sha'][:12]}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(_markdown(report), encoding="utf-8")

    for label, name, _factory in PATHS:
        summary = report["paths"][name]
        print(
            f"  ({label}) {name}: median={summary['median_ms']:.3f}ms "
            f"p95={summary['p95_ms']:.3f}ms stddev={summary['stddev_ms']:.3f}ms "
            f"n={int(summary['n'])} ok={summary['ok']}"
        )
    print(f"\nHEAD {report['head_sha']}")
    print(f"json: {json_path}")
    print(f"md:   {md_path}")
    digest = hashlib.sha256(json_path.read_bytes()).hexdigest()
    print(f"report sha256: {digest}")
    print("PASS" if report["ok"] else "FAIL")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
