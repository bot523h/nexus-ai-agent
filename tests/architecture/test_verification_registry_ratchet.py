"""Architecture ratchet: no job handler may exist without an artifact verifier.

The ratchet makes the task-180 closure permanent: ``worker.
default_job_handlers`` and ``adapters.in_process_job_queue.
default_artifact_verifiers`` must cover the SAME job types. Adding a new
handler without a verifier — or silently deleting a verifier — turns this
test red, so "execution success = job success" can never come back through
the default composition roots (bot process and CLI drain both install the
default registry).
"""

from __future__ import annotations

from nexus_ai_agent.adapters.in_process_job_queue import default_artifact_verifiers
from nexus_ai_agent.jobs.verification import VerificationOutcome
from nexus_ai_agent.worker import default_job_handlers

#: The verified job types this repository promises (task-178 + task-180).
EXPECTED_VERIFIED_JOB_TYPES = frozenset(
    {"creative_render", "slideshow_render", "story", "pdf_extract"}
)


def test_every_default_handler_has_a_default_verifier() -> None:
    handlers = default_job_handlers()
    verifiers = default_artifact_verifiers()
    unverified = set(handlers) - set(verifiers)
    assert not unverified, (
        f"job types without an artifact verifier would keep the historical "
        f"unverified semantics (execution success = job success): {sorted(unverified)}"
    )


def test_verified_job_types_match_the_promised_set() -> None:
    """Detect silent removals as well as additions (the list is a contract)."""
    verifiers = default_artifact_verifiers()
    assert set(verifiers) == EXPECTED_VERIFIED_JOB_TYPES


def test_verifiers_are_side_effect_free_by_signature() -> None:
    """A verifier is (payload, result) → VerificationOutcome, synchronously.

    The queue runs verifiers in a worker thread and treats a crash as a
    fail-closed job failure; anything that is not callable with exactly the
    documented two positional arguments is not a legal verifier.
    """
    import inspect

    for job_type, verifier in default_artifact_verifiers().items():
        assert callable(verifier), job_type
        signature = inspect.signature(verifier)
        positional = [
            name
            for name, parameter in signature.parameters.items()
            if parameter.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        assert positional == ["payload", "result"], (
            f"{job_type}: verifier must accept (payload, result); got {positional}"
        )
        # the return type annotation must be the contract's outcome type
        assert signature.return_annotation in (VerificationOutcome, "VerificationOutcome"), (
            f"{job_type}: verifier must be annotated -> VerificationOutcome"
        )
