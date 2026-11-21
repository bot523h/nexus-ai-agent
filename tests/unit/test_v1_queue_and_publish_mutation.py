"""AST mutation tests for the queue → publication security fences (Gate 5).

Ten load-bearing boundaries are mutated for real on the parsed AST and the
guarding tests must kill every mutant (``MUTANT KILLED``):

1. the ``attempt`` fence in ``ApprovalLedger.consume`` (a stale attempt may
   not re-read a consumed approval);
2. the ``render_profile_hash`` (``preview_digest``) identity check that binds
   the approval to the exact render profile — local reference-image content
   included;
3. the queue's ``outcome.ok`` gate (a verifier verdict that is not ``PASS``
   may never complete a job);
4. the post-materialization SHA-256 re-check of the local reference images
   (a plate swapped after the approval was bound is a refusal);
5. the ``(job_id, attempt)`` ownership fence in the staged→final promotion;
6. the shot-tree policy of the production storyboard verifier (a passport
   without a leaf per rendered shot is a forged board);
7. the ``artifact_verification == "PASS"`` condition of the studio download
   gate (an unverified artifact is never downloadable);
8. the fail-closed quarantine contract of ``_promote_staged_artifacts`` (a
   ``storyboard_render`` result without a valid staging contract may never
   promote to ``None`` — that is the old fail-open hole);
9. the ``close()`` of ``_FdLease`` (the verified download descriptor must
   actually be closed, exactly once);
10. the sealed-identity comparison of the idempotent re-entry gate (a final
    pair left by a different render is a mismatch, never a silent success).

C-Extension Safety: every mutant is ``ast.parse``-d *before* any scoring, is
exec'd as its own module under a private ``nexus_mutant_*`` name and is added
to ``sys.modules``; ``sys.modules`` is never cleared, so every C-extension
module (``numpy``, ``PIL``, ``sqlalchemy``, …) keeps its single original
instance and only the module under test is replaced by the mutant.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import os
import sys
import time
from datetime import timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from nexus_ai_agent.adapters import in_process_job_queue as queue_module
from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.api import studio as studio_module
from nexus_ai_agent.application.execution_context import ExecutionClaim
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.creative import artifact_download as download_module
from nexus_ai_agent.creative import materializer as mz
from nexus_ai_agent.creative import storyboard_jobs as storyboard_jobs_module
from nexus_ai_agent.creative import storyboard_render as storyboard_render_module
from nexus_ai_agent.creative.artifact_passport import build_passport, write_passport
from nexus_ai_agent.creative.storyboard_compiler import compile_storyboard
from nexus_ai_agent.jobs.verification import VerificationOutcome
from nexus_ai_agent.nagar.creative.handoff import build_storyboard_render_payload
from nexus_ai_agent.nagar.ibpc import approval as approval_module
from nexus_ai_agent.nagar.ibpc.approval import (
    ApprovalIdentity,
    ApprovalLedger,
    DurableApprovalLedger,
    durable_approval_ledger_path,
)
from nexus_ai_agent.nagar.ibpc.canonical import digest_of, sha256_bytes
from nexus_ai_agent.nagar.ibpc.models import ApprovalBinding

REPO_ROOT = Path(__file__).parents[2]
KEY = "k" * 40
SCRIPT = "INT. KITCHEN - DAY\nSara: Hello, are you there?\nAli: Yes, I am here.\n"
PROFILE = {"width": 320, "height": 180, "fps": 12, "sample_rate": 16000}
PROJECT_ID = "proj-mutation"
JOB_ID = "job-mutation"
PRINCIPAL_ID = "owner-mutation"
NOW = lambda: "2026-11-19T09:00:00Z"  # noqa: E731

MUTATION_TARGETS = {
    "approval": "src/nexus_ai_agent/nagar/ibpc/approval.py",
    "queue": "src/nexus_ai_agent/adapters/in_process_job_queue.py",
    "render": "src/nexus_ai_agent/creative/storyboard_render.py",
    "jobs": "src/nexus_ai_agent/creative/storyboard_jobs.py",
    "studio": "src/nexus_ai_agent/api/studio.py",
    "download": "src/nexus_ai_agent/creative/artifact_download.py",
}


# --------------------------------------------------------------------------- #
# Mutation machinery (the C-Extension-safe loader shared with the V1 suite)
# --------------------------------------------------------------------------- #


def _load_mutant(relative_path: str, mutate: Any, name: str) -> ModuleType:
    """Parse ``relative_path``, apply an AST mutation, exec it as its own module.

    ``sys.modules`` is never cleared: the mutant is *added* under a private
    name while every C-extension module (``numpy``, ``PIL``, ``sqlalchemy``, …)
    keeps its original instance. Only the module under test is replaced by the
    mutant. The source is ``ast.parse``-d (and the mutation structurally
    asserted) before any behaviour is scored.
    """
    source_path = REPO_ROOT / relative_path
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    mutate(tree)
    ast.fix_missing_locations(tree)
    code = compile(tree, f"<mutant:{name}>", "exec")
    module = ModuleType(f"nexus_mutant_{name}")
    module.__file__ = str(source_path)
    module.__dict__["__builtins__"] = __builtins__
    sys.modules[module.__name__] = module
    exec(code, module.__dict__)  # noqa: S102 - executing our own parsed AST
    return module


def _function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


def _refuses(action: Any, exc_type: type[BaseException], *, contains: str = "") -> None:
    """Assert that ``action`` is refused with ``exc_type`` (mutation primitive).

    Deliberately not :func:`pytest.raises`: a mutation check must fail with a
    plain ``AssertionError`` so :func:`_kill` can classify it, and pytest's own
    ``Failed`` outcome would escape that classification.
    """
    try:
        action()
    except exc_type as exc:
        if contains:
            assert contains in str(exc), f"{contains!r} missing from {exc!r}"
        return
    raise AssertionError(f"{exc_type.__name__} was not raised: the refusal disappeared")


def _kill(mutant_behaviour: Any) -> None:
    """A mutant is KILLED when the assertion that guards the invariant now fails."""
    try:
        mutant_behaviour()
    except AssertionError:
        return  # the guarded invariant failed: the mutant is killed
    raise AssertionError("mutant survived: the invariant was not enforced")


def _tmp_root() -> Path:
    import tempfile

    return Path(tempfile.mkdtemp(prefix="queue-mutation-"))


def _binding(**overrides: Any) -> ApprovalBinding:
    base = dict(
        session_id="s1",
        turn_id="t1",
        sequence_number=1,
        tenant_id="ten",
        project_id=PROJECT_ID,
        principal_id=PRINCIPAL_ID,
        source_revision=7,
        source_asset_digest=sha256_bytes(b"asset"),
        ast_digest=digest_of({"ast": 1}),
        plan_digest=digest_of({"plan": 1}),
        preview_digest=digest_of({"preview": 1}),
    )
    base.update(overrides)
    return ApprovalBinding(**base)


def _identity(**overrides: Any) -> ApprovalIdentity:
    base = dict(
        principal_id=PRINCIPAL_ID,
        tenant_id="ten",
        project_id=PROJECT_ID,
        session_id="s1",
        turn_id="t1",
        sequence_number=1,
        source_revision=7,
        source_asset_digest=sha256_bytes(b"asset"),
        ast_digest=digest_of({"ast": 1}),
        plan_digest=digest_of({"plan": 1}),
        preview_digest=digest_of({"preview": 1}),
    )
    base.update(overrides)
    return ApprovalIdentity(**base)


IDENT = _identity()


# --------------------------------------------------------------------------- #
# Mutant 1: the attempt fence in ApprovalLedger.consume
# --------------------------------------------------------------------------- #


def test_mutant_consume_without_the_attempt_fence_is_killed() -> None:
    """Drop ``if attempt < reservation[3]``: a superseded attempt would re-read
    the consumed approval — the stale-attempt refusal must kill it."""

    def _neuter(tree: ast.Module) -> None:
        func = _function(tree, "consume")
        found = False
        for node in ast.walk(func):
            if (
                isinstance(node, ast.If)
                and isinstance(node.test, ast.Compare)
                and "attempt < reservation[3]" in ast.unparse(node.test)
            ):
                node.test = ast.Constant(value=False)
                found = True
        assert found, "the stale-attempt guard must exist in consume"

    def _behaviour(module: ModuleType) -> None:
        ledger: ApprovalLedger = module.ApprovalLedger(now=NOW)
        binding = ledger.create(_binding())
        ledger.approve(binding.approval_id, identity=IDENT, turn_is_active=lambda: True)
        first = ledger.consume(binding.approval_id, identity=IDENT, job_id="job-fence", attempt=2)
        assert first.state is module.ApprovalState.CONSUMED
        _refuses(
            lambda: ledger.consume(
                binding.approval_id, identity=IDENT, job_id="job-fence", attempt=1
            ),
            module.ApprovalError,
            contains="stale_attempt",
        )

    _behaviour(approval_module)
    mutant = _load_mutant(MUTATION_TARGETS["approval"], _neuter, "consume_no_attempt_fence")
    _kill(lambda: _behaviour(mutant))


# --------------------------------------------------------------------------- #
# Mutant 2: the render_profile_hash (preview_digest) identity check
# --------------------------------------------------------------------------- #


def test_mutant_consume_without_the_preview_digest_check_is_killed() -> None:
    """Drop the identity comparison in ``consume`` (``_check_identity`` — the
    ``preview_digest``/render-profile hash among its fields): consuming an
    approval under a DIFFERENT render profile must still be impossible."""

    def _neuter(tree: ast.Module) -> None:
        func = _function(tree, "consume")
        found = 0
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call)
                and ast.unparse(node.value.func).endswith("_check_identity")
            ):
                node.value = ast.Constant(value=None)  # the call becomes a no-op
                found += 1
        assert found >= 1, "the identity check must exist in consume"

    def _behaviour(module: ModuleType) -> None:
        ledger: ApprovalLedger = module.ApprovalLedger(now=NOW)
        binding = ledger.create(_binding())
        ledger.approve(binding.approval_id, identity=IDENT, turn_is_active=lambda: True)
        other_profile = _identity(preview_digest=digest_of({"preview": 2}))
        _refuses(
            lambda: ledger.consume(binding.approval_id, identity=other_profile),
            module.ApprovalError,
            contains="identity_mismatch_preview_digest",
        )

    _behaviour(approval_module)
    mutant = _load_mutant(MUTATION_TARGETS["approval"], _neuter, "consume_no_profile_hash")
    _kill(lambda: _behaviour(mutant))


# --------------------------------------------------------------------------- #
# Mutant 3: the queue's non-PASS acceptance gate
# --------------------------------------------------------------------------- #


async def _drive_queue(queue: InProcessJobQueue, job_id: str, timeout: float = 60.0) -> JobStatus:
    deadline = time.monotonic() + timeout
    while True:
        status = await queue.get_status(job_id)
        if status.value in ("completed", "failed_terminal", "failed_retryable"):
            return status
        if time.monotonic() > deadline:
            raise AssertionError(f"job still {status} after {timeout}s")
        await asyncio.sleep(0.1)


def _refused_verdict_behaviour(module: ModuleType, root: Path) -> None:
    """A verifier that answers VIOLATED must never let the job complete."""

    async def scenario() -> JobStatus:
        async def probe_handler(payload: dict[str, object]) -> dict[str, object]:
            return {"success": True, "note": "probe"}

        def probe_verifier(payload: dict[str, object], result: dict[str, object]) -> Any:
            return VerificationOutcome(
                ok=False,
                reason_code="media_verdict_not_pass",
                summary={
                    "status": "failed",
                    "reason_code": "media_verdict_not_pass",
                    "media_verdict": "VIOLATED",
                },
            )

        queue = module.InProcessJobQueue(
            root / "verdict-queue.sqlite3",
            handlers={"mutation_probe": probe_handler},
            artifact_verifiers={"mutation_probe": probe_verifier},
        )
        await queue.acquire_ownership()
        job_id = await queue.enqueue(
            job_type="mutation_probe", idempotency_key="probe-verdict", payload={}
        )
        status = await _drive_queue(queue, job_id)
        await queue.shutdown()
        return status

    status = asyncio.run(scenario())
    assert status is not JobStatus.COMPLETED, "a non-PASS verdict must never complete a job"


def test_mutant_queue_accepting_a_non_pass_verdict_is_killed() -> None:
    """Neuter ``if outcome.ok:`` in the queue's process path: a refused verdict
    would complete the job — the completed-status assertion must kill it."""

    def _neuter(tree: ast.Module) -> None:
        func = _function(tree, "_process_job")
        found = False
        for node in ast.walk(func):
            if (
                isinstance(node, ast.If)
                and isinstance(node.test, ast.Attribute)
                and ast.unparse(node.test) == "outcome.ok"
            ):
                node.test = ast.Constant(value=True)
                found = True
        assert found, "the bare outcome.ok gate must exist in _process_job"

    root = _tmp_root()
    _refused_verdict_behaviour(queue_module, root)
    mutant = _load_mutant(MUTATION_TARGETS["queue"], _neuter, "queue_accepts_non_pass")
    _kill(lambda: _refused_verdict_behaviour(mutant, _tmp_root()))


# --------------------------------------------------------------------------- #
# Mutant 4: the post-materialization local-image digest re-check
# --------------------------------------------------------------------------- #


def _swapped_plate_behaviour(module: ModuleType, root: Path) -> None:
    """A local reference plate swapped between the bound digest and the
    materialization is a refusal — never a silent re-seal."""
    from nexus_ai_agent.creative.storyboard_render import RenderContext

    workspace = root / "studio"
    workspace.mkdir(parents=True, exist_ok=True)
    plate = workspace / "plates" / "plate.png"
    plate.parent.mkdir(parents=True, exist_ok=True)
    mz.write_slate_png(plate, width=320, height=180, seed=b"\x07" * 32, label="plate")
    local_images = {"s000-sh000": "plates/plate.png"}
    image_digests = module.local_image_content_digests(workspace, local_images)
    profile_hash = module.render_profile_hash(mz.RenderProfile.from_mapping(PROFILE), image_digests)

    compiled = compile_storyboard(SCRIPT)
    ledger = DurableApprovalLedger(
        durable_approval_ledger_path(workspace), now=lambda: "2026-11-19T09:00:00Z"
    )
    try:
        approval_id = module.issue_render_approval(
            ledger,
            project_id=PROJECT_ID,
            job_id=JOB_ID,
            principal_id=PRINCIPAL_ID,
            source_sha256=hashlib.sha256(SCRIPT.encode("utf-8")).hexdigest(),
            script_sha256=compiled.script_sha256,
            manifest_sha256=compiled.manifest_sha256,
            render_profile_hash=profile_hash,
        )
    finally:
        ledger.close()

    payload = {
        **build_storyboard_render_payload(
            {
                "project_id": PROJECT_ID,
                "job_id": JOB_ID,
                "screenplay_text": SCRIPT,
                "source_sha256": hashlib.sha256(SCRIPT.encode("utf-8")).hexdigest(),
                "manifest_sha256": compiled.manifest_sha256,
                "approval_id": approval_id,
                "principal_id": PRINCIPAL_ID,
                **PROFILE,
            }
        ),
        "local_images": local_images,
    }

    # Swap the plate MID-render: after the digest (bound into the approval),
    # before the materialization hashes it again.
    real_compile = module.compile_storyboard

    def swapping_compile(text: str, **kwargs: Any) -> Any:
        mz.write_slate_png(plate, width=320, height=180, seed=b"\x08" * 32, label="swapped")
        return real_compile(text, **kwargs)

    module.compile_storyboard = swapping_compile
    try:
        render_ledger = DurableApprovalLedger(
            durable_approval_ledger_path(workspace), now=lambda: "2026-11-19T09:00:00Z"
        )
        try:
            ctx = RenderContext(
                workspace_root=workspace, approval_ledger=render_ledger, signing_key=KEY
            )
            _refuses(
                lambda: module.render_storyboard_sync(payload, ctx),
                module.StoryboardRenderError,
                contains="local_image_changed_after_approval",
            )
        finally:
            render_ledger.close()
    finally:
        module.compile_storyboard = real_compile


def test_mutant_render_without_the_local_image_recheck_is_killed() -> None:
    """Neuter ``if image_digests:`` around the post-materialization plate
    re-check: the swapped plate would render — the refusal must kill it."""

    def _neuter(tree: ast.Module) -> None:
        func = _function(tree, "render_storyboard_sync")
        found = False
        for node in ast.walk(func):
            if isinstance(node, ast.If) and ast.unparse(node.test) == "image_digests":
                node.test = ast.Constant(value=False)
                found = True
        assert found, "the local-image re-check guard must exist"

    _swapped_plate_behaviour(storyboard_render_module, _tmp_root())
    mutant = _load_mutant(MUTATION_TARGETS["render"], _neuter, "render_no_plate_recheck")
    _kill(lambda: _swapped_plate_behaviour(mutant, _tmp_root()))


# --------------------------------------------------------------------------- #
# Mutant 5: the promotion ownership fence
# --------------------------------------------------------------------------- #


def _superseded_promotion_behaviour(module: ModuleType, root: Path) -> None:
    """A claim whose attempt was superseded must promote nothing."""

    async def scenario() -> None:
        release = asyncio.Event()

        async def probe_handler(payload: dict[str, object]) -> dict[str, object]:
            await release.wait()  # keep the auto-scheduled attempt 1 in flight
            return {"success": True}

        queue = module.InProcessJobQueue(
            root / "fence-queue.sqlite3",
            handlers={"mutation_probe": probe_handler},
            artifact_verifiers={},
        )
        await queue.acquire_ownership()
        job_id = await queue.enqueue(
            job_type="mutation_probe", idempotency_key="probe-fence", payload={}
        )
        deadline = time.monotonic() + 30
        while (await queue.get_status(job_id)).value != "processing":
            assert time.monotonic() < deadline, "attempt 1 never claimed the row"
            await asyncio.sleep(0.05)
        # A takeover supersedes attempt 1 (bypassing the live-task exclusion
        # exactly like a cross-process reconciler would) and attempt 2 re-claims.
        for _ in range(20):
            reset = await asyncio.to_thread(queue._reset_unfinished, timedelta(0), set())
            if any(entry[0] == job_id for entry in reset):
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("the takeover never committed")
        claim2 = await asyncio.to_thread(queue._mark_processing, job_id)
        assert claim2 is not None and claim2.attempt == 2

        staged = root / "staging"
        staged.mkdir()
        (staged / "out.mp4").write_bytes(b"staged-bytes")
        (staged / "out.passport.json").write_text("{}", encoding="utf-8")
        final = root / "final"
        final.mkdir()
        result = {
            "staged_output_path": str(staged / "out.mp4"),
            "staged_passport_path": str(staged / "out.passport.json"),
            "final_output_path": str(final / "out.mp4"),
            "final_passport_path": str(final / "out.passport.json"),
        }
        stale_claim = ExecutionClaim(job_id=job_id, attempt=1)
        renames_started: list[str] = []
        # The hook fires only once the first rename has begun: a superseded
        # attempt must be stopped BEFORE it touches the destination at all.
        queue._between_promote_replaces_hook = lambda: renames_started.append("rename")
        promoted = await asyncio.to_thread(
            queue._promote_staged_artifacts, stale_claim, result, "mutation_probe"
        )
        assert not renames_started, "a superseded attempt must never start renaming"
        assert promoted is None, "a superseded attempt must not publish"
        assert (staged / "out.mp4").is_file(), "the quarantine must keep the loser's files"
        assert not (final / "out.mp4").exists(), "nothing may reach the final path"
        release.set()
        await queue.shutdown()

    asyncio.run(scenario())


def test_mutant_promotion_without_the_ownership_fence_is_killed() -> None:
    """Neuter the ``_owns_execution`` pre-check in the staged→final promotion:
    the superseded attempt would move the files — the quarantine assertion
    must kill it."""

    def _neuter(tree: ast.Module) -> None:
        func = _function(tree, "_promote_staged_artifacts")
        found = False
        for node in ast.walk(func):
            if (
                isinstance(node, ast.If)
                and isinstance(node.test, ast.UnaryOp)
                and "_owns_execution" in ast.unparse(node.test)
            ):
                node.test = ast.Constant(value=False)
                found = True
                break  # the pre-check: the first fence in the function
        assert found, "the promotion ownership fence must exist"

    _superseded_promotion_behaviour(queue_module, _tmp_root())
    mutant = _load_mutant(MUTATION_TARGETS["queue"], _neuter, "promote_no_ownership")
    _kill(lambda: _superseded_promotion_behaviour(mutant, _tmp_root()))


# --------------------------------------------------------------------------- #
# Mutant 6: the shot-tree policy of the storyboard verifier
# --------------------------------------------------------------------------- #


def _empty_shot_tree_behaviour(module: ModuleType, root: Path, monkeypatch: Any) -> None:
    """A v2 passport whose shot tree is empty, presented for a two-shot render,
    must be refused with ``shot_tree_incomplete``."""
    workspace = root / "studio"
    render_dir = workspace / "projects" / "p6" / "renders" / "j6"
    render_dir.mkdir(parents=True, exist_ok=True)
    mp4 = render_dir / "storyboard.mp4"
    payload_bytes = b"\x00" * 2048
    mp4.write_bytes(payload_bytes)
    sha = hashlib.sha256(payload_bytes).hexdigest()
    passport = build_passport(
        job_id="j6",
        project_id="p6",
        source_sha256=hashlib.sha256(b"source").hexdigest(),
        manifest_sha256=hashlib.sha256(b"manifest").hexdigest(),
        render_profile_hash=hashlib.sha256(b"profile").hexdigest(),
        asset_merkle_root=hashlib.sha256(b"assets").hexdigest(),
        total_asset_leaves=2,
        output_rel_path="projects/p6/renders/j6/storyboard.mp4",
        output_size_bytes=len(payload_bytes),
        output_sha256=sha,
        probed_duration_s=1.0,
        verifier_status="PASS",
        created_at_unix_s=1,
        shot_hashes=[],  # the forged part: no leaves at all
        signing_key=KEY,
    )
    write_passport(render_dir / "storyboard.passport.json", passport)

    monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE", str(workspace))
    monkeypatch.setenv("NEXUS_STUDIO_SIGNING_KEY", KEY)
    result = {
        "status": "rendered",
        "verifier_status": "PASS",
        "output_rel_path": "projects/p6/renders/j6/storyboard.mp4",
        "passport_rel_path": "projects/p6/renders/j6/storyboard.passport.json",
        "output_sha256": sha,
        "shot_durations_s": [0.5, 0.5],  # two shots were rendered
        "asset_merkle_root": passport["asset_merkle_root"],
    }
    outcome = module.storyboard_render_verifier({"fps": 12}, result)
    assert outcome.ok is False, "an empty shot tree must never verify"
    assert outcome.reason_code == "shot_tree_incomplete", outcome.reason_code


def test_mutant_verifier_accepting_a_passport_without_shot_hashes_is_killed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neuter the shot-count policy in the storyboard verifier: the forged
    empty-tree passport would verify — the refusal must kill it."""

    def _neuter(tree: ast.Module) -> None:
        func = _function(tree, "storyboard_render_verifier")
        found = False
        for node in ast.walk(func):
            if isinstance(node, ast.If) and "expected_shot_count" in ast.unparse(node.test):
                node.test = ast.Constant(value=False)
                found = True
        assert found, "the shot-tree policy check must exist"

    _empty_shot_tree_behaviour(storyboard_jobs_module, _tmp_root(), monkeypatch)
    mutant = _load_mutant(MUTATION_TARGETS["jobs"], _neuter, "verifier_no_shot_tree")
    _kill(lambda: _empty_shot_tree_behaviour(mutant, _tmp_root(), monkeypatch))


# --------------------------------------------------------------------------- #
# Mutant 7: the studio download gate
# --------------------------------------------------------------------------- #


def _unverified_download_behaviour(module: ModuleType) -> None:
    row = {
        "id": "job-77",
        "status": "completed",
        "canonical_status": "completed",
        "result": {
            "output_rel_path": "projects/p77/renders/job-77/storyboard.mp4",
            "artifact_verification": {
                "status": "verified",
                "media_verdict": "VIOLATED",  # NOT a PASS
            },
        },
    }
    _refuses(
        lambda: module._require_completed_and_verified(row),
        module.DownloadRefused,
        contains="artifact_not_verified",
    )


def test_mutant_download_gate_without_the_pass_condition_is_killed() -> None:
    """Neuter the ``artifact_verification`` PASS condition in the download
    gate: the unverified artifact would be served — the refusal must kill it."""

    def _neuter(tree: ast.Module) -> None:
        func = _function(tree, "_require_completed_and_verified")
        found = False
        for node in ast.walk(func):
            if (
                isinstance(node, ast.If)
                and "media_verdict" in ast.unparse(node.test)
                and "status.lower()" not in ast.unparse(node.test)
            ):
                node.test = ast.Constant(value=False)
                found = True
        assert found, "the verification-block gate must exist"

    _unverified_download_behaviour(studio_module)
    mutant = _load_mutant(MUTATION_TARGETS["studio"], _neuter, "download_no_pass_gate")
    _kill(lambda: _unverified_download_behaviour(mutant))


# --------------------------------------------------------------------------- #
# Mutant 8: the fail-closed quarantine contract of the promotion
# --------------------------------------------------------------------------- #


def _missing_staging_contract_behaviour(module: ModuleType, root: Path) -> None:
    """A ``storyboard_render`` result with NO quarantine contract at all must
    be refused with ``staged_artifacts_missing_or_unpromoted`` — never a
    silent ``None`` (the old fail-open hole CP4 closes)."""

    async def scenario() -> None:
        queue = module.InProcessJobQueue(
            root / "m8-queue.sqlite3",
            handlers={},
            artifact_verifiers={},
        )
        await queue.acquire_ownership()
        try:
            claim = ExecutionClaim(job_id="job-m8", attempt=1)
            result: dict[str, object] = {
                "status": "rendered",
                "verifier_status": "PASS",
                # NO staged_*/final_* paths: the quarantine contract is absent.
                "output_rel_path": "projects/p8/renders/job-m8/storyboard.mp4",
            }
            await asyncio.to_thread(
                queue._promote_staged_artifacts, claim, result, "storyboard_render"
            )
        except module._PromotionRefused as exc:
            assert "staged_artifacts_missing_or_unpromoted" in str(exc), str(exc)
        else:
            raise AssertionError("a contract-less storyboard result must fail closed")
        finally:
            await queue.shutdown()

    asyncio.run(scenario())


def test_mutant_promotion_returning_none_without_staging_is_killed() -> None:
    """Revert the fail-closed contract check to ``return None``: the
    contract-less storyboard result would promote silently — the refusal
    must kill it."""

    def _swap_raise_to_return(stmts: list[ast.stmt]) -> bool:
        found = False
        for index, stmt in enumerate(stmts):
            if isinstance(
                stmt, ast.Raise
            ) and "staged_artifacts_missing_or_unpromoted" in ast.unparse(stmt):
                stmts[index] = ast.Return(value=ast.Constant(value=None))
                found = True
            for field in ("body", "orelse", "finalbody"):
                child = getattr(stmt, field, None)
                if isinstance(child, list) and _swap_raise_to_return(child):
                    found = True
        return found

    def _mutate(tree: ast.Module) -> None:
        func = _function(tree, "_promote_staged_artifacts")
        assert _swap_raise_to_return(func.body), "the fail-closed contract raise must exist"

    _missing_staging_contract_behaviour(queue_module, _tmp_root())
    mutant = _load_mutant(MUTATION_TARGETS["queue"], _mutate, "promote_fail_open_none")
    _kill(lambda: _missing_staging_contract_behaviour(mutant, _tmp_root()))


# --------------------------------------------------------------------------- #
# Mutant 9: the fd lease of the authorized download
# --------------------------------------------------------------------------- #


def _fd_lease_close_behaviour(module: ModuleType) -> None:
    """``_FdLease.close()`` must retire the descriptor — really close it."""
    fd = os.open(str(REPO_ROOT / "pyproject.toml"), os.O_RDONLY)
    try:
        lease = module._FdLease(fd)
        assert lease.fd == fd, "the lease must expose the owned descriptor"
        lease.close()
        assert lease.fd is None, "close() must retire the descriptor"
        try:
            os.fstat(fd)
        except OSError:
            return  # closed exactly once: the descriptor is really gone
        raise AssertionError("close() must actually close the descriptor")
    finally:
        try:  # best-effort tidy-up on the mutant path (fd may be closed)
            os.close(fd)
        except OSError:
            pass


def test_mutant_fd_lease_without_a_real_close_is_killed() -> None:
    """Neuter ``_FdLease.close`` to a ``pass``: the verified download
    descriptor would stay open forever — the real-close assertion must kill it."""

    def _neuter(tree: ast.Module) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "_FdLease":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "close":
                        item.body = [ast.Pass()]
                        return
        raise AssertionError("_FdLease.close must exist")

    _fd_lease_close_behaviour(download_module)
    mutant = _load_mutant(MUTATION_TARGETS["download"], _neuter, "fd_lease_no_close")
    _kill(lambda: _fd_lease_close_behaviour(mutant))


# --------------------------------------------------------------------------- #
# Mutant 10: the sealed-identity gate of the idempotent re-entry
# --------------------------------------------------------------------------- #


def _foreign_final_pair_behaviour(module: ModuleType, root: Path, monkeypatch: Any) -> None:
    """An idempotent re-entry whose surviving final pair verifies but was
    sealed for DIFFERENT bytes (another render's ``output_sha256``) must be
    refused with ``idempotent_pair_identity_mismatch``."""

    async def scenario() -> None:
        release = asyncio.Event()

        async def probe_handler(payload: dict[str, object]) -> dict[str, object]:
            await release.wait()  # keep the auto-scheduled attempt 1 in flight
            return {"success": True}

        workspace = root / "studio-m10"
        render_dir = workspace / "projects" / "p10" / "renders" / "j10"
        render_dir.mkdir(parents=True, exist_ok=True)
        mp4 = render_dir / "storyboard.mp4"
        payload_bytes = b"\x11" * 2048
        mp4.write_bytes(payload_bytes)
        real_sha = hashlib.sha256(payload_bytes).hexdigest()
        passport = build_passport(
            job_id="j10",
            project_id="p10",
            source_sha256=hashlib.sha256(b"source").hexdigest(),
            manifest_sha256=hashlib.sha256(b"manifest").hexdigest(),
            render_profile_hash=hashlib.sha256(b"profile").hexdigest(),
            asset_merkle_root=hashlib.sha256(b"assets").hexdigest(),
            total_asset_leaves=1,
            output_rel_path="projects/p10/renders/j10/storyboard.mp4",
            output_size_bytes=len(payload_bytes),
            output_sha256=real_sha,
            probed_duration_s=1.0,
            verifier_status="PASS",
            created_at_unix_s=1,
            shot_hashes=[hashlib.sha256(b"shot-1").hexdigest()],
            signing_key=KEY,
        )
        write_passport(render_dir / "storyboard.passport.json", passport)

        monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE", str(workspace))
        monkeypatch.setenv("NEXUS_STUDIO_SIGNING_KEY", KEY)

        queue = module.InProcessJobQueue(
            root / "m10-queue.sqlite3",
            handlers={"mutation_probe": probe_handler},
            artifact_verifiers={},
        )
        await queue.acquire_ownership()
        try:
            job_id = await queue.enqueue(
                job_type="mutation_probe", idempotency_key="probe-m10", payload={}
            )
            deadline = time.monotonic() + 30
            while (await queue.get_status(job_id)).value != "processing":
                assert time.monotonic() < deadline, "attempt 1 never claimed the row"
                await asyncio.sleep(0.05)
            for _ in range(20):
                reset = await asyncio.to_thread(queue._reset_unfinished, timedelta(0), set())
                if any(entry[0] == job_id for entry in reset):
                    break
                await asyncio.sleep(0.1)
            else:
                raise AssertionError("the takeover never committed")
            claim = await asyncio.to_thread(queue._mark_processing, job_id)
            assert claim is not None and claim.attempt == 2
            assert await asyncio.to_thread(queue._mark_verifying, claim)

            # The staged files of THIS attempt are gone; a complete final pair
            # sits at the destination — but it was sealed for OTHER bytes.
            result = {
                "status": "rendered",
                "verifier_status": "PASS",
                "job_id": "j10",  # matches the passport...
                "output_sha256": hashlib.sha256(b"other-bytes-entirely").hexdigest(),
                "staged_output_path": str(
                    workspace / "artifacts" / ".staging" / job_id / "attempt-2" / "storyboard.mp4"
                ),
                "staged_passport_path": str(
                    workspace
                    / "artifacts"
                    / ".staging"
                    / job_id
                    / "attempt-2"
                    / "storyboard.passport.json"
                ),
                "final_output_path": str(mp4),
                "final_passport_path": str(render_dir / "storyboard.passport.json"),
            }
            await asyncio.to_thread(
                queue._promote_staged_artifacts, claim, result, "storyboard_render"
            )
        except module._PromotionRefused as exc:
            assert "idempotent_pair_identity_mismatch" in str(exc), str(exc)
        else:
            raise AssertionError("a foreign final pair must never be adopted")
        finally:
            release.set()
            await queue.shutdown()

    asyncio.run(scenario())


def test_mutant_idempotent_pair_without_the_identity_check_is_killed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neuter the sealed-identity comparison in ``_verify_idempotent_pair``:
    the foreign final pair would be adopted as this attempt's publication —
    the mismatch refusal must kill it."""

    def _neuter(tree: ast.Module) -> None:
        func = _function(tree, "_verify_idempotent_pair")
        found = False
        for node in ast.walk(func):
            if isinstance(node, ast.If) and "passport_value != attempt_value" in ast.unparse(
                node.test
            ):
                node.test = ast.Constant(value=False)
                found = True
        assert found, "the sealed-identity comparison must exist"

    _foreign_final_pair_behaviour(queue_module, _tmp_root(), monkeypatch)
    mutant = _load_mutant(MUTATION_TARGETS["queue"], _neuter, "idempotent_no_identity")
    _kill(lambda: _foreign_final_pair_behaviour(mutant, _tmp_root(), monkeypatch))


# --------------------------------------------------------------------------- #
# Mutant 11 (CP4.1): the call-site guard that refuses a ``None`` promotion
# --------------------------------------------------------------------------- #


def _promotion_returns_none_behaviour(module: ModuleType, root: Path) -> None:
    """A storyboard job whose promotion publishes nothing must never complete."""

    async def scenario() -> JobStatus:
        async def probe_handler(payload: dict[str, object]) -> dict[str, object]:
            return {"status": "rendered", "note": "probe"}

        def probe_verifier(payload: dict[str, object], result: dict[str, object]) -> Any:
            return VerificationOutcome(
                ok=True,
                reason_code="pass",
                summary={"status": "passed", "media_verdict": "PASS"},
            )

        queue = module.InProcessJobQueue(
            root / "callsite-queue.sqlite3",
            handlers={"storyboard_render": probe_handler},
            artifact_verifiers={"storyboard_render": probe_verifier},
        )
        # Instance attribute shadows the method: the promotion silently publishes nothing.
        queue._promote_staged_artifacts = lambda claim, result, job_type: None  # type: ignore[method-assign]
        await queue.acquire_ownership()
        job_id = await queue.enqueue(
            job_type="storyboard_render", idempotency_key="probe-callsite", payload={}
        )
        status = await _drive_queue(queue, job_id)
        await queue.shutdown()
        return status

    status = asyncio.run(scenario())
    assert status is JobStatus.FAILED_TERMINAL, (
        f"a promotion that published nothing must fail the job closed, got {status}"
    )


def test_mutant_call_site_completing_after_a_none_promotion_is_killed() -> None:
    """Neuter the ``promoted_result is None or not published`` guard in ``_process_job``:
    a storyboard job would reach COMPLETED without any published pair."""

    def _neuter(tree: ast.Module) -> None:
        func = _function(tree, "_process_job")
        found = False
        for node in ast.walk(func):
            if isinstance(node, ast.If) and "promoted_result is None" in ast.unparse(node.test):
                node.test = ast.Constant(value=False)
                found = True
                break
        assert found, "the call-site promotion guard must exist"

    _promotion_returns_none_behaviour(queue_module, _tmp_root())
    mutant = _load_mutant(MUTATION_TARGETS["queue"], _neuter, "callsite_no_promotion_guard")
    _kill(lambda: _promotion_returns_none_behaviour(mutant, _tmp_root()))


# --------------------------------------------------------------------------- #
# Mutant 12 (CP4.1): the rollback after an ownership loss following the replace
# --------------------------------------------------------------------------- #


def _ownership_lost_after_replace_behaviour(module: ModuleType, root: Path) -> None:
    """A superseded attempt that loses ownership after the rename must roll back."""
    renders = root / "final"
    staging = root / "staging"
    renders.mkdir(parents=True, exist_ok=True)
    staging.mkdir(parents=True, exist_ok=True)
    final_out, final_pp = renders / "out.mp4", renders / "out.passport.json"
    staged_out, staged_pp = staging / "new.mp4", staging / "new.passport.json"
    final_out.write_bytes(b"previous-mp4")
    final_pp.write_bytes(b"previous-passport")
    staged_out.write_bytes(b"superseded-mp4")
    staged_pp.write_bytes(b"superseded-passport")
    result = {
        "status": "rendered",
        "staged_output_path": str(staged_out),
        "staged_passport_path": str(staged_pp),
        "final_output_path": str(final_out),
        "final_passport_path": str(final_pp),
    }
    calls = {"n": 0}

    def _owns(self: Any, claim: Any, expected: Any) -> bool:
        calls["n"] += 1
        return calls["n"] == 1  # held for the pre-rename check, lost after the replace

    async def _unused() -> None:
        return None

    queue = module.InProcessJobQueue(
        root / "ownership-queue.sqlite3", handlers={}, artifact_verifiers={}
    )
    original = module.InProcessJobQueue._owns_execution
    module.InProcessJobQueue._owns_execution = _owns  # type: ignore[method-assign]
    try:
        promoted = queue._promote_staged_artifacts(
            ExecutionClaim(job_id="qjob-m12", attempt=2), result, "mutation_probe"
        )
    finally:
        module.InProcessJobQueue._owns_execution = original  # type: ignore[method-assign]
    assert promoted is None, "a superseded promotion must publish nothing"
    assert final_out.read_bytes() == b"previous-mp4", "the superseded bytes must not stay published"
    assert final_pp.read_bytes() == b"previous-passport"
    assert staged_out.is_file(), "the superseded bytes return to the quarantine"
    assert not list(renders.glob("*.promote-backup"))


def test_mutant_ownership_loss_without_rollback_is_killed() -> None:
    """Remove the ``_rollback_promotion`` call of the post-replace ownership branch:
    the superseded generation would stay published — the rollback check must kill it."""

    def _neuter(tree: ast.Module) -> None:
        func = _function(tree, "_promote_staged_artifacts")
        found = False
        for node in ast.walk(func):
            if not isinstance(node, ast.If) or "_owns_execution" not in ast.unparse(node.test):
                continue
            for index, stmt in enumerate(node.body):
                if isinstance(stmt, ast.Expr) and "_rollback_promotion" in ast.unparse(stmt):
                    node.body[index] = ast.Pass()
                    found = True
        assert found, "the post-replace ownership rollback must exist"

    _ownership_lost_after_replace_behaviour(queue_module, _tmp_root())
    mutant = _load_mutant(MUTATION_TARGETS["queue"], _neuter, "ownership_no_rollback")
    _kill(lambda: _ownership_lost_after_replace_behaviour(mutant, _tmp_root()))
