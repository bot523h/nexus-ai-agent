"""One revision truth: the durable pin, and nothing that can disagree with it.

A revision can be named in two places -- the project row's ``active_revision_id``
pointer and the immutable ``PinnedRevisionEnvelope`` in the pinning store -- and
before Wave 3 nothing forced them to agree.  ``_authoritative_revision`` read the
pointer and never consulted the pin, so a turn could execute against a revision
with no pin behind it, and the worker's asset verification would then be
verifying a revision the admission path never meant to authorise.

These tests pin the rule that replaced it: the durable head wins, a disagreement
between the two is refused rather than resolved, and pinning advances both in
lockstep under a compare-and-swap on the parent.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.creative.studio.revision_pinning import (
    AssetVersionPin,
    DurableRevisionPinningStore,
    RevisionConcurrencyConflictError,
)
from nexus_ai_agent.nagar.ibpc.service import IBPCService, IBPCServiceDeps
from nexus_ai_agent.nagar.ibpc.session import StaleRevisionError

TENANT = "ten-rev"
PROJECT = "proj-rev"


class _Auth:
    """Just enough of the auth store: the service reads one revision pointer."""

    def __init__(self, revision: str) -> None:
        self.revision = revision
        self.updates: list[str] = []

    def get_project_revision(self, project_id: str) -> str:
        if project_id != PROJECT:
            raise AssertionError(f"unexpected project {project_id!r}")
        return self.revision

    def set_project_revision(self, project_id: str, revision_id: str) -> None:
        self.updates.append(revision_id)
        self.revision = revision_id


def _pin(
    store: DurableRevisionPinningStore,
    *,
    revision_id: str,
    number: int,
    parent: str | None,
    plan_hex: str = "d" * 64,
) -> None:
    store.pin_revision(
        revision_id=revision_id,
        project_id=PROJECT,
        tenant_id=TENANT,
        session_id="sess-1",
        turn_id=f"turn-{number}",
        revision_number=number,
        parent_revision_id=parent,
        plan_id="plan-1",
        plan_jcs_sha256=plan_hex,
        parameters_jcs_sha256="e" * 64,
        preview_digest="sha256:" + "f" * 64,
    )


def _service(root: Path, *, auth: Any, pins: DurableRevisionPinningStore | None) -> IBPCService:
    return IBPCService(
        IBPCServiceDeps(
            queue=None,
            registry=None,
            workspace_root=root,
            revision_provider=lambda: 1,
            authority=auth,
            project_revision_resolver=auth.get_project_revision,
            revision_pinning_store=pins,
        )
    )


def test_the_pinned_head_is_authoritative(tmp_path: Path) -> None:
    """With a pin present, the pin answers -- not the pointer."""
    auth = _Auth("rev-1")
    pins = DurableRevisionPinningStore(tmp_path / "pins.sqlite3")
    _pin(pins, revision_id="rev-1", number=1, parent=None)
    service = _service(tmp_path, auth=auth, pins=pins)
    _revision, authoritative = service._authoritative_revision(PROJECT)
    assert authoritative == "rev-1"


def test_a_disagreement_is_refused_not_resolved(tmp_path: Path) -> None:
    """Pointer and pin disagreeing is a split brain, and it fails closed.

    Picking either side would be a decision with real consequences: prefer the
    pointer and a turn renders bytes no pin authorises; prefer the pin and a
    client told "rev-2 is active" silently executes rev-1.
    """
    auth = _Auth("rev-2")  # pointer advanced...
    pins = DurableRevisionPinningStore(tmp_path / "pins.sqlite3")
    _pin(pins, revision_id="rev-1", number=1, parent=None)  # ...but nothing pinned.
    service = _service(tmp_path, auth=auth, pins=pins)
    with pytest.raises(StaleRevisionError) as excinfo:
        service._authoritative_revision(PROJECT)
    assert excinfo.value.reason == "revision_split_brain"
    assert "rev-2" in excinfo.value.detail and "rev-1" in excinfo.value.detail


def test_without_a_pin_the_pointer_still_answers(tmp_path: Path) -> None:
    """A project that has pinned nothing keeps working exactly as before."""
    auth = _Auth("rev-1")
    pins = DurableRevisionPinningStore(tmp_path / "pins.sqlite3")
    service = _service(tmp_path, auth=auth, pins=pins)
    _revision, authoritative = service._authoritative_revision(PROJECT)
    assert authoritative == "rev-1"


def test_without_a_store_the_pointer_answers(tmp_path: Path) -> None:
    """An unwired deployment is untouched by this rule."""
    auth = _Auth("rev-1")
    service = _service(tmp_path, auth=auth, pins=None)
    _revision, authoritative = service._authoritative_revision(PROJECT)
    assert authoritative == "rev-1"


def test_pinning_advances_the_head_only_through_the_parent_cas(tmp_path: Path) -> None:
    """A second revision must name the current head as its parent.

    This is the compare-and-swap that makes two concurrent turns unable to both
    advance the project: the loser's parent no longer matches the head.
    """
    pins = DurableRevisionPinningStore(tmp_path / "pins.sqlite3")
    _pin(pins, revision_id="rev-1", number=1, parent=None)
    assert pins.head_of(PROJECT) == "rev-1"

    _pin(pins, revision_id="rev-2", number=2, parent="rev-1")
    assert pins.head_of(PROJECT) == "rev-2"

    with pytest.raises(RevisionConcurrencyConflictError) as excinfo:
        _pin(pins, revision_id="rev-3", number=3, parent="rev-1")  # stale parent
    assert excinfo.value.reason == "stale_parent"
    # The refusal left the head exactly where it was.
    assert pins.head_of(PROJECT) == "rev-2"


def test_repinning_the_same_revision_is_idempotent(tmp_path: Path) -> None:
    """A retried pin returns the stored envelope rather than conflicting."""
    pins = DurableRevisionPinningStore(tmp_path / "pins.sqlite3")
    _pin(pins, revision_id="rev-1", number=1, parent=None)
    first = pins.get_envelope("rev-1")
    _pin(pins, revision_id="rev-1", number=1, parent=None)
    second = pins.get_envelope("rev-1")
    assert first is not None and second is not None
    assert first.revision_jcs_sha256 == second.revision_jcs_sha256


def test_worker_verification_checks_the_pinned_asset_bytes(tmp_path: Path) -> None:
    """A pin is only worth anything if the worker re-reads the bytes."""
    pins = DurableRevisionPinningStore(tmp_path / "pins.sqlite3")
    assets = tmp_path / "assets"
    assets.mkdir()
    payload = b"the exact bytes this revision was pinned against"
    (assets / "clip.mp4").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()

    pins.pin_revision(
        revision_id="rev-1",
        project_id=PROJECT,
        tenant_id=TENANT,
        session_id="sess-1",
        turn_id="turn-1",
        revision_number=1,
        parent_revision_id=None,
        plan_id="plan-1",
        plan_jcs_sha256="d" * 64,
        parameters_jcs_sha256="e" * 64,
        preview_digest="sha256:" + "f" * 64,
        input_asset_pins=(
            AssetVersionPin(
                asset_id="clip",
                relative_path="clip.mp4",
                content_sha256=digest,
                byte_size=len(payload),
            ),
        ),
    )
    envelope = pins.verify_worker_execution_pins(
        revision_id="rev-1",
        expected_revision_jcs_sha256=pins.get_envelope("rev-1").revision_jcs_sha256,
        expected_plan_jcs_sha256="d" * 64,
        authorized_asset_root=assets,
        expected_project_id=PROJECT,
        expected_tenant_id=TENANT,
    )
    assert envelope.revision_id == "rev-1"

    # Swap the bytes for a DIFFERENT payload of the SAME length, so the size
    # check cannot be what catches it: this must fail on the content digest.
    (assets / "clip.mp4").write_bytes(b"X" * len(payload))
    with pytest.raises(Exception) as excinfo:
        pins.verify_worker_execution_pins(
            revision_id="rev-1",
            expected_revision_jcs_sha256=envelope.revision_jcs_sha256,
            expected_plan_jcs_sha256="d" * 64,
            authorized_asset_root=assets,
            expected_project_id=PROJECT,
            expected_tenant_id=TENANT,
        )
    violation = str(excinfo.value).lower()
    assert "content_drift" in violation, violation
    assert "size_mismatch" not in violation, "the size check must not mask a content swap"
