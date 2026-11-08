#!/usr/bin/env python3
"""Adversarial mutation harness for the durable project/revision lifecycle (task-282).

Evidence rule: an invariant that has never been attacked is not evidence.  Each
mutation below rewrites one shipped correctness/safety guard, re-runs the
lifecycle suites, and requires them to turn RED.  Every source file is restored
afterwards -- including on failure -- and the final run must be GREEN again.

Usage::

    python scripts/durable_project_state_mutations.py          # run every mutation
    python scripts/durable_project_state_mutations.py --list

Exit code 0 means "N/N mutants killed".
"""

# ruff: noqa: E501 - the MUTATIONS table holds verbatim source snippets; they must
# match the shipped files byte-for-byte, so they cannot be re-wrapped.
from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "src" / "nexus_ai_agent" / "creative" / "project_state" / "store.py"
PERSISTENCE = ROOT / "src" / "nexus_ai_agent" / "creative" / "studio" / "persistence.py"
RENDER_JOBS = ROOT / "src" / "nexus_ai_agent" / "creative" / "render_jobs.py"
QUEUE = ROOT / "src" / "nexus_ai_agent" / "adapters" / "in_process_job_queue.py"
HANDOFF = ROOT / "src" / "nexus_ai_agent" / "nagar" / "creative" / "handoff.py"
TESTS = (
    "tests/unit/test_durable_project_state.py",
    "tests/integration/test_durable_project_concurrency.py",
    "tests/integration/test_durable_project_restart.py",
    "tests/integration/test_durable_project_revision_pinning.py",
    "tests/unit/test_nagar_cognition_queue_handoff.py",
)


@dataclass(frozen=True)
class Mutation:
    name: str
    path: Path
    old: str
    new: str
    why: str


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "cas_predicate_removed",
        STORE,
        "                orm.ProjectRevisionHead.revision_id == expected_parent_revision_id,  # type: ignore[arg-type]",
        "                orm.ProjectRevisionHead.project_id == project_id,  # mutated: CAS predicate dropped",
        "the head advance must be a compare-and-swap on the expected parent; "
        "dropping it lets a stale writer silently overwrite a newer revision",
    ),
    Mutation(
        "cross_project_read_check_removed",
        STORE,
        "        if row.project_id != project_id:",
        "        if False:",
        "a revision must only be readable through the project that owns it; "
        "removing the check leaks another project's state",
    ),
    Mutation(
        "genesis_arbiter_defanged",
        STORE,
        """            try:
                await session.flush()
            except IntegrityError as exc:
                raise RevisionConflict("project already has a head; genesis refused") from exc
            return""",
        """            await session.flush()
            return""",
        "a second genesis must be refused as a typed RevisionConflict; defanging "
        "the primary-key arbiter lets a duplicate head overwrite the project",
    ),
    Mutation(
        "unset_cas_token_accepted",
        STORE,
        "        if expected_parent_revision_id is _UNSET:",
        "        if False:",
        "an unguarded commit (no CAS token) must be refused; accepting it turns "
        "every write into last-writer-wins",
    ),
    Mutation(
        "content_address_not_digested",
        PERSISTENCE,
        '    return "sha256:" + hashlib.sha256(state_bytes).hexdigest()',
        '    return "sha256:" + state_bytes.decode("utf-8")',
        "the revision id must be the content hash of the state bytes; returning "
        "the raw bytes breaks content addressing and immutability",
    ),
    Mutation(
        "read_integrity_revalidation_removed",
        STORE,
        "        _verify_revision_integrity(row)",
        "        pass  # mutated: read-time integrity re-validation removed",
        "F1: a read must re-derive the revision's identity from its stored bytes; "
        "dropping the check lets a directly-tampered row pass the read path",
    ),
    Mutation(
        "content_address_revalidation_removed",
        STORE,
        "    if content_address(canonical) != row.revision_id:",
        "    if False:  # mutated: content-address re-validation removed",
        "F1: the stored bytes must hash back to the stored revision id; a forged "
        "revision id would otherwise be accepted",
    ),
    Mutation(
        "state_hash_revalidation_removed",
        STORE,
        "    if compute_state_hash(project) != row.state_hash:",
        "    if False:  # mutated: state-hash re-validation removed",
        "F1: the stored state_hash must match the stored content; a tampered hash "
        "column would otherwise be accepted",
    ),
    Mutation(
        "project_identity_guard_removed",
        STORE,
        "    if project.project_id != row.project_id:",
        "    if False:  # mutated: embedded project identity no longer checked",
        "Finding 1: a re-homed row (embedded state belongs to another project) "
        "must be refused; dropping the guard leaks one project's state to another",
    ),
    Mutation(
        "commit_preflight_state_hash_removed",
        STORE,
        "        if compute_state_hash(project) != project.state_hash:",
        "        if False:  # mutated: stale state_hash no longer detected pre-write",
        "Finding 2: a stale state_hash must be refused BEFORE the insert/head "
        "advance; dropping the preflight advances the head to an unreadable revision",
    ),
    Mutation(
        "revision_pin_ignored_by_worker",
        RENDER_JOBS,
        "    if data.revision_id is None:\n        return None",
        "    if True:  # mutated: pinned job falls back to the current head\n        return None",
        "Slice 1: a pinned job must load its exact revision, never the live head; "
        "ignoring the pin lets a moved head silently change what a job executes",
    ),
    Mutation(
        "revision_pin_hash_check_removed",
        RENDER_JOBS,
        "    if record.state_hash != data.revision_hash:",
        "    if False:  # mutated: pinned hash check removed",
        "Slice 1: the loaded revision's canonical hash must equal the job's pin; "
        "dropping the check accepts a revision the job was never admitted against",
    ),
    Mutation(
        "handoff_pin_not_recorded",
        HANDOFF,
        "    if revision_pin is not None:\n        revision_id, revision_hash = revision_pin",
        "    if False:  # mutated: handoff stops pinning the revision\n        revision_id, revision_hash = revision_pin",
        "Slice 1: the handoff must record the pinned revision in the durable payload; "
        "omitting it makes the job fall back to the current head",
    ),
    Mutation(
        "canonical_admission_without_store_allowed",
        HANDOFF,
        "    if canonical is not None and revision_store is None:",
        "    if False:  # mutated: canonical admission no longer requires the store",
        "Finding 3: a canonical admission must be pinned; dropping the guard lets a "
        "durable intent enqueue an unpinned job the worker runs from a rebuilt baseline",
    ),
    Mutation(
        "handoff_rereads_head_instead_of_admitted_revision",
        HANDOFF,
        """            admitted = await revision_store.load_revision(
                canonical.project_id, admitted_revision.revision_id
            )""",
        "            admitted = await revision_store.load_head(canonical.project_id)",
        "task-282: the handoff must pin the ADMITTED revision and never re-read the "
        "live head; re-reading re-targets the job at a concurrent head move (TOCTOU)",
    ),
    Mutation(
        "artifact_binding_conflict_ignored",
        STORE,
        """        if row.revision_id != revision_id:
            raise ArtifactBindingConflict("artifact already bound to a different revision")""",
        """        if False:  # mutated: repointing an artifact to another revision is allowed
            raise ArtifactBindingConflict("artifact already bound to a different revision")""",
        "Slice 2: an artifact's provenance is fixed at first bind; dropping the "
        "conflict check silently re-points it at another revision",
    ),
    Mutation(
        "artifact_binding_skips_revision_validation",
        STORE,
        "        await self._load_validated_revision(project_id, revision_id)",
        "        pass  # mutated: binding no longer validates the revision exists",
        "Slice 2: an artifact must only bind to a revision that exists and validates; "
        "skipping the load lets an artifact bind to a vanished/tampered revision",
    ),
    Mutation(
        "artifact_verify_remeasure_removed",
        STORE,
        """        if measured != binding.artifact_id:
            raise ArtifactRevisionMismatch(
                "artifact bytes no longer match the bound content address"
            )""",
        """        if False:  # mutated: re-verification no longer re-measures the artifact
            raise ArtifactRevisionMismatch(
                "artifact bytes no longer match the bound content address"
            )""",
        "Slice 2: re-verification must re-measure the artifact bytes; removing the "
        "re-measure trusts a replaced/tampered artifact",
    ),
    Mutation(
        "artifact_binding_failopen",
        RENDER_JOBS,
        """    except Exception as exc:  # noqa: BLE001 - every binding failure must be typed
        raise CreativeRenderError(
            "artifact_binding_failed", f"{type(exc).__name__}: {exc}"
        ) from exc""",
        """    except Exception:  # mutated: binding failure is swallowed
        return result""",
        "Slice 2: a refused binding must fail the job closed, never succeed without "
        "provenance; swallowing the error lets a pinned job publish unbound",
    ),
    Mutation(
        "artifact_binding_skips_digest_remeasure",
        RENDER_JOBS,
        """    if not is_content_address(raw_sha) or raw_sha != measured:
        raise CreativeRenderError(
            "artifact_binding_failed",
            "reported artifact digest does not match the artifact's measured bytes",
        )""",
        """    if False:  # mutated: bind trusts the reported digest instead of re-measuring
        raise CreativeRenderError(
            "artifact_binding_failed",
            "reported artifact digest does not match the artifact's measured bytes",
        )""",
        "Slice 2 review: the binding must re-measure the published bytes; trusting the "
        "reported digest binds an artifact that a later write already replaced",
    ),
    Mutation(
        "artifact_content_address_validation_removed",
        STORE,
        "        if not is_content_address(artifact_id):",
        "        if False:  # mutated: a non-hex content address is accepted",
        "Slice 2 review: a same-length non-hex id can never match a real file; "
        "accepting it persists a permanently unverifiable binding",
    ),
    Mutation(
        "artifact_verify_oserror_failopen",
        STORE,
        """        except OSError as exc:
            raise ArtifactRevisionMismatch(
                "artifact bytes could not be read for re-verification"
            ) from exc""",
        """        except OSError:  # mutated: a missing artifact is trusted as verified
            return binding""",
        "Slice 2 review: a missing/unreadable artifact must be a typed refusal, never "
        "a silent 'verified' — fail-closed on the read path",
    ),
    Mutation(
        "artifact_binding_confirmed_before_verification",
        QUEUE,
        "                if publication is not None and publication.confirm is not None:",
        "                if False:  # mutated: the binding is never confirmed by the queue",
        "D-0038 residual 1: the durable binding must be committed by the queue AFTER "
        "verification+publication and BEFORE COMPLETED; disabling the confirm hook "
        "means a job completes with no artifact→revision provenance",
    ),
    Mutation(
        "artifact_confirmation_refusal_ignored",
        QUEUE,
        "                    if confirm_error is not None:",
        "                    if False:  # mutated: a refused confirmation is ignored",
        "D-0038 residual 1: a refused confirmation must fail the job closed; ignoring "
        "it lets an unconfirmed job reach COMPLETED and publish without provenance",
    ),
    Mutation(
        "confirmation_fence_recheck_removed",
        QUEUE,
        """                    if not await asyncio.to_thread(
                        self._owns_execution, claim, JobStatus.VERIFYING
                    ):
                        self._log_rejected(job_id, claim, JobStatus.COMPLETED)
                        return
                    confirm_error = await self._confirm_safely(publication, payload, result, claim)""",
        "                    confirm_error = await self._confirm_safely(publication, payload, result, claim)",
        "the durable binding is a side effect: ownership must be re-read at the "
        "commit boundary, else an attempt superseded during the re-probe window "
        "commits provenance for a row it no longer owns",
    ),
    Mutation(
        "lost_cas_binding_retraction_removed",
        QUEUE,
        """                    if publication is not None and publication.confirm is not None:
                        await self._retract_confirmation_safely(publication, payload, result, claim)""",
        """                    if False:  # mutated: the committed binding is never retracted
                        await self._retract_confirmation_safely(publication, payload, result, claim)""",
        "F1: a takeover landing *inside* the confirm commit window makes the terminal "
        "CAS fail; the committed binding must be retracted, else a superseded attempt "
        "leaves orphan durable provenance for a job that never reached COMPLETED",
    ),
    Mutation(
        "retraction_not_token_scoped",
        STORE,
        "                    _BINDING.c.attempt == attempt,",
        "                    _BINDING.c.artifact_id == artifact_id,  # mutated: fencing token dropped from the delete predicate",
        "F1: retraction must be scoped to the exact (job_id, attempt) that wrote the "
        "row; dropping the fencing token lets a superseded attempt delete a "
        "successor's live binding",
    ),
    Mutation(
        "rebind_fencing_token_not_monotonic",
        STORE,
        """        result = await session.execute(
            update(orm.ProjectArtifactBinding)
            .where(
                _BINDING.c.artifact_id == row.artifact_id,
                _BINDING.c.attempt < attempt,
            )
            .values(attempt=attempt)
            .execution_options(synchronize_session="fetch")
        )
        await session.commit()
        # Reflect the committed post-state (the row kept its token when the
        # conditional UPDATE matched nothing).
        row.attempt = attempt if getattr(result, "rowcount", 0) > 0 else max(row.attempt, attempt)""",
        """        await session.commit()  # mutated: the fencing token never advances on re-bind""",
        "F1: a re-bind by a newer attempt must advance the fencing token monotonically; "
        "otherwise a successor's row keeps the old token and a stale retraction can "
        "delete it",
    ),
)

TEST_TIMEOUT_SECONDS = 600


def run_tests() -> bool:
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, shell=False, TESTS are repo-local literals
            [sys.executable, "-m", "pytest", "-q", "-x", *TESTS],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=TEST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(f"(test run exceeded {TEST_TIMEOUT_SECONDS}s) ", end="", flush=True)
        return False
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        for mutation in MUTATIONS:
            print(f"{mutation.name}: {mutation.why}")
        return 0

    print("baseline ... ", end="", flush=True)
    if not run_tests():
        print("RED — refusing to mutate a suite that is already failing")
        return 2
    print("GREEN")

    killed = 0
    survivors: list[str] = []
    for mutation in MUTATIONS:
        original = mutation.path.read_text(encoding="utf-8")
        if mutation.old not in original:
            print(f"{mutation.name}: MUTATION DOES NOT APPLY (source drifted)")
            survivors.append(mutation.name)
            continue
        try:
            mutation.path.write_text(
                original.replace(mutation.old, mutation.new, 1), encoding="utf-8"
            )
            green = run_tests()
        finally:
            mutation.path.write_text(original, encoding="utf-8")
        if green:
            print(f"{mutation.name}: SURVIVED — {mutation.why}")
            survivors.append(mutation.name)
        else:
            killed += 1
            print(f"{mutation.name}: killed")

    print("restored baseline ... ", end="", flush=True)
    if not run_tests():
        print("RED — restore failed")
        return 3
    print("GREEN")
    print(f"{killed}/{len(MUTATIONS)} killed")
    if survivors:
        print("survivors: " + ", ".join(survivors))
    return 0 if killed == len(MUTATIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
