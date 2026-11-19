"""Real source-mutation campaign for the CP4 lease/fence protocol and the trust anchor.

Not an adversarial-input test: for each entry in :data:`MUTANTS` one genuine source
edit is applied to an isolated copy of the package, the mutated source is parsed with
:func:`ast.parse` (a mutant that does not compile proves nothing), and the targeted
test file is run in a *subprocess*. A mutant counts as ``MUTANT KILLED`` only when
pytest exits with ``1`` (TESTS_FAILED) and reports a real ``FAILED test_...`` node —
never a collection, import or syntax crash.

Two layers are covered, because they are the two places where "looks correct" used
to be enough:

* ``storage/personal_cloud_vault/byos_drivers.py`` — the two-phase GC fence. The
  mutants remove, one at a time, the conditional claim, the ownership checks, the
  post-retirement lease read, the conditional (create-if-absent) rollback and the
  envelope-identity guard. Each removal is a plausible refactor and each one breaks
  a specific published guarantee.
* ``creative/storyboard_compiler/editorial_cut_solver.py`` — the mandatory anchor of
  ``verify_manifest_integrity_strict``. The mutants restore the self-attestation hole
  in its several shapes: no anchor required, the text route ignored, an agreement
  mismatch tolerated, the parser's normalization skipped.
* the CP5 hardening, each mutant undoing exactly one of its guarantees: the
  post-write chunk-generation proof that closes the ABA race (M12), the bucket-clock
  anchor that makes lease expiry immune to a lagging writer (M13), the mandatory
  ``ETag`` precondition before a fence is retired (M14), and the two non-finite guards
  in the loudness core (M15, M16).
* the CP6 closure of the lease state machine and the chunk identity: an expired-lease
  reap that trusts the read it took one round trip ago (M17), a rollback that writes
  without proving the object is still its own write (M18), a reap that degrades to an
  unconditional ``DELETE`` when the backend reports no ``ETag`` (M19), a release path
  that accepts an empty ``ETag`` as a licence to delete (M20), a chunk identity
  reduced to the content-hash ``ETag`` and therefore blind to a delete-and-reupload of
  identical bytes (M21), an unknown-identity object treated as unchanged (M22), and
  the two audio contracts that refuse a non-finite scalar parameter and a non-finite
  ``clean_reference`` (M23, M24).
* the CP6.1 hardening of the same two paths: a refresh that PUTs without an ``ETag``
  precondition (M25), a refresh that treats an unreadable or moved prior lease as
  "there was none" and then rolls it back into a delete (M26), a release that trusts a
  fresh read of the object instead of its own recorded write (M27), and a chunk
  identity that ignores the generation stamp whenever the bucket is unversioned
  (M28).

Every mutant also names the test that must be the one to kill it, and the campaign
fails if that node does not appear among the reported failures — a mutant killed by
an unrelated test would otherwise count as proof of a guard nobody exercised.

Isolation is by hardlink: the copy is instant and costs no disk, and every mutated
file is unlinked *before* it is written, so a hardlink can never write through to the
real repository. Nothing in this module touches ``sys.modules`` — no C extension
(``numpy``, ``scipy``, ``PIL``, ``sqlalchemy``) is ever reloaded in-process, because
each mutant runs in its own interpreter.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
TESTS = REPO_ROOT / "tests"

DRIVERS = "nexus_ai_agent/storage/personal_cloud_vault/byos_drivers.py"
ANCHOR = "nexus_ai_agent/creative/storyboard_compiler/editorial_cut_solver.py"
FENCE_TESTS = "tests/unit/test_personal_cloud_vault_lease_fence.py"
ANCHOR_TESTS = "tests/creative/storyboard_compiler/test_editorial_cut_solver.py"
AUDIO = "nexus_ai_agent/creative/audio_engine/loudness_bs1770.py"
WIENER = "nexus_ai_agent/creative/audio_engine/wiener_denoise.py"
AUDIO_TESTS = "tests/creative/audio_engine/test_finite_input_guards.py"

#: ``(mutant id, source path, original text, mutated text, target test file, expected kill)``.
#: Every ``original`` must occur exactly once in the real tree, which
#: ``test_every_mutation_anchor_is_unique`` enforces.
MUTANTS: tuple[tuple[str, str, str, str, str, str], ...] = (
    (
        "M01 post-retirement lease read removed",
        DRIVERS,
        """                if self._lease_state_token() != before_state or valid in (
                    self.read_active_leased_chunks(now_epoch=effective_now)
                ):
                    self._restore_chunk_conditionally(valid, staged_payload)
                    return False
                return True""",
        "                return True",
        FENCE_TESTS,
        "test_lease_registered_while_the_fence_is_being_retired_is_still_seen",
    ),
    (
        "M02 rollback loses create-if-absent",
        DRIVERS,
        """            object_key=self._object_key("chunks", f"{chunk_id}.chk"),
            body=staged_payload,
            content_type="application/octet-stream",
            extra_headers={
                "if-none-match": "*",
                CHUNK_GENERATION_META_HEADER: _new_chunk_generation(),
            },""",
        """            object_key=self._object_key("chunks", f"{chunk_id}.chk"),
            body=staged_payload,
            content_type="application/octet-stream",
            extra_headers={
                CHUNK_GENERATION_META_HEADER: _new_chunk_generation(),
            },""",
        FENCE_TESTS,
        "test_conditional_restore_never_overwrites_a_newer_chunk_object",
    ),
    (
        "M03 fence claim loses if-none-match",
        DRIVERS,
        """            object_key=fence_key,
            body=body,
            content_type="application/json",
            extra_headers={"if-none-match": "*"},""",
        """            object_key=fence_key,
            body=body,
            content_type="application/json",""",
        FENCE_TESTS,
        "test_writer_refuses_a_chunk_a_collector_is_currently_fenced_for",
    ),
    (
        "M04 envelope identity guard disabled",
        DRIVERS,
        "        if embedded is not None and embedded != chunk_id:",
        "        if False:",
        FENCE_TESTS,
        "test_restore_refuses_bytes_whose_envelope_names_another_chunk",
    ),
    (
        "M05 retire ignores the owner token",
        DRIVERS,
        "        if not fence.token or not hmac.compare_digest(fence.token, token):",
        "        if False:",
        FENCE_TESTS,
        "test_fence_retirement_refuses_a_claim_that_now_belongs_to_another_collector",
    ),
    (
        "M06 live-fence refusal disabled",
        DRIVERS,
        "        fenced = self._live_gc_fenced_chunks(chunk_ids, now_epoch=now_epoch)\n"
        "        if fenced:",
        "        fenced = self._live_gc_fenced_chunks(chunk_ids, now_epoch=now_epoch)\n"
        "        if False:",
        FENCE_TESTS,
        "test_writer_refuses_a_chunk_a_collector_is_currently_fenced_for",
    ),
    (
        "M07 trusted text route ignored",
        ANCHOR,
        """    digest: str | None = None
    if trusted_source_text is not None:""",
        """    digest: str | None = None
    if False:""",
        ANCHOR_TESTS,
        "test_wave8_trusted_source_text_closes_the_self_attestation_hole",
    ),
    (
        "M08 text/digest disagreement tolerated",
        ANCHOR,
        """        if expected_source_sha256 is not None and not hmac.compare_digest(
            digest, expected_source_sha256
        ):
            return False""",
        "        pass",
        ANCHOR_TESTS,
        "test_wave8_trusted_source_text_closes_the_self_attestation_hole",
    ),
    (
        "M09 anchor requirement dropped",
        ANCHOR,
        "        require_trusted_anchor=True,",
        "        require_trusted_anchor=False,",
        ANCHOR_TESTS,
        "test_wave8_strict_verifier_makes_the_trust_anchor_mandatory",
    ),
    (
        "M10 parser normalization skipped",
        ANCHOR,
        "    normalized = normalize_script_text(text)",
        "    normalized = text",
        ANCHOR_TESTS,
        "test_wave8_trusted_source_text_closes_the_self_attestation_hole",
    ),
    (
        "M11 byte input refused",
        ANCHOR,
        "    if isinstance(trusted_source_text, bytes):",
        "    if False:",
        ANCHOR_TESTS,
        "test_wave8_trusted_source_text_closes_the_self_attestation_hole",
    ),
    (
        "M12 ABA chunk-generation proof disabled",
        DRIVERS,
        """        lost = self._lost_materialised_chunks(preflight)
        if lost:
            self._rollback_lease_acquire(
                obj_key, our_write_etag=our_write_etag, our_lease_token=our_lease_token
            )
            raise self._lost_chunk_error(lost, "after the lease write")""",
        """        lost: tuple[str, ...] = ()
        if lost:
            raise self._lost_chunk_error(lost, "after the lease write")""",
        FENCE_TESTS,
        "test_aba_collector_finishing_before_the_lease_write_forces_a_rollback",
    ),
    (
        "M13 bucket-clock anchor dropped",
        DRIVERS,
        """    deadline = expires_at_epoch
    ttl = _lease_ttl_seconds(raw)
    observed = _server_object_epoch(headers)
    if ttl is not None and observed is not None:
        deadline = max(deadline, observed + ttl)
    return deadline""",
        "    return expires_at_epoch",
        FENCE_TESTS,
        "test_a_lagging_writer_clock_never_makes_its_fresh_lease_look_expired",
    ),
    (
        "M14 fence retirement loses the mandatory precondition",
        DRIVERS,
        """        if not fence.etag:
            # No ETag means no conditional DELETE is possible, and an unconditional
            # DELETE could destroy a *newer* owner's claim that landed one round trip
            # ago. Leaving the fence in place costs a writer a retry until the TTL
            # expires; deleting the wrong fence costs data. Fail closed.
            return False
        resp = self._send(
            "DELETE",
            object_key=self._gc_fence_key(chunk_id),
            extra_headers={"if-match": fence.etag},
        )""",
        """        resp = self._send(
            "DELETE",
            object_key=self._gc_fence_key(chunk_id),
            extra_headers={"if-match": fence.etag} if fence.etag else {},
        )""",
        FENCE_TESTS,
        "test_fence_retirement_never_degrades_to_an_unconditional_delete",
    ),
    (
        "M15 dBFS of a non-finite value is published",
        AUDIO,
        "    if not math.isfinite(value) or value <= 0.0:",
        "    if value <= 0.0:",
        AUDIO_TESTS,
        "test_dbfs_of_a_non_finite_value_is_none",
    ),
    (
        "M16 non-finite input guard disabled",
        AUDIO,
        "    if array.size and not np.all(np.isfinite(array)):",
        "    if False:",
        AUDIO_TESTS,
        "test_measure_bs1770_refuses_non_finite_samples",
    ),
    (
        "M17 expired-lease reap trusts a stale read",
        DRIVERS,
        """        if not state.is_ours(etag=expected_etag, lease_token=expected_lease_token):
            return False
        if state.deadline_epoch + self._clock_skew_margin > effective_now:
            return False
        try:
            resp = self._send(
                "DELETE",
                object_key=obj_key,
                extra_headers={"if-match": expected_etag},
            )
        except VaultTransportError:
            return False
        if resp.status_code in (200, 202, 204):
            return True""",
        """        return (
            self._send("DELETE", object_key=obj_key).status_code in (200, 202, 204, 404)
        )""",
        FENCE_TESTS,
        "test_stale_lease_cleanup_cannot_delete_a_concurrently_refreshed_lease",
    ),
    (
        "M18 rollback loses its ownership precondition",
        DRIVERS,
        """        if not state.is_ours(etag=our_write_etag, lease_token=our_lease_token):
            return
        if prior_body is not None and prior_etag and state.etag == prior_etag:
            return
        if prior_body is not None:
            resp = self._send(
                "PUT",
                object_key=obj_key,
                body=prior_body,
                content_type="application/json",
                extra_headers={"if-match": our_write_etag},
            )
        else:
            resp = self._send(
                "DELETE",
                object_key=obj_key,
                extra_headers={"if-match": our_write_etag},
            )""",
        """        if prior_body is not None:
            self._send(
                "PUT",
                object_key=obj_key,
                body=prior_body,
                content_type="application/json",
            )
            return
        self._send("DELETE", object_key=obj_key)
        return""",
        FENCE_TESTS,
        "test_failed_rollback_never_deletes_a_third_writers_newer_lease",
    ),
    (
        "M19 reap degrades to an unconditional delete without an ETag",
        DRIVERS,
        """        if not expected_etag:
            return False
        try:
            state = self._read_lease_state(obj_key)""",
        """        if not expected_etag:
            self._send("DELETE", object_key=obj_key)
            return True
        try:
            state = self._read_lease_state(obj_key)""",
        FENCE_TESTS,
        "test_absent_lease_etag_never_causes_unconditional_lease_deletion",
    ),
    (
        "M20 release accepts an empty ETag",
        DRIVERS,
        """            raw_etag: object = expected_etag
            if not isinstance(raw_etag, str) or not raw_etag.strip():
                raise VaultTransportError(
                    f"Refusing to release S3 push lease {valid_id!r} without a known ETag: "
                    "an unconditional DELETE could remove a lease another writer just took over"
                )""",
        """            raw_etag: object = expected_etag
            if False:
                raise VaultTransportError("unreachable")""",
        FENCE_TESTS,
        "test_absent_lease_etag_never_causes_unconditional_lease_deletion",
    ),
    (
        "M21 chunk identity reduced to the content ETag",
        DRIVERS,
        '''        if not version_id and not generation:
            return UNKNOWN_CHUNK_GENERATION
        return f"{version_id}|{generation}|{etag}"''',
        """        if not version_id and not generation:
            return UNKNOWN_CHUNK_GENERATION
        return etag""",
        FENCE_TESTS,
        "test_identical_bytes_reupload_with_same_etag_is_detected_as_aba_replacement",
    ),
    (
        "M22 unknown chunk identity treated as unchanged",
        DRIVERS,
        """        if not version_id and not generation:
            return UNKNOWN_CHUNK_GENERATION""",
        """        if False:
            return UNKNOWN_CHUNK_GENERATION""",
        FENCE_TESTS,
        "test_missing_version_and_generation_metadata_fails_closed",
    ),
    (
        "M23 non-finite scalar parameter accepted",
        WIENER,
        """    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite real number")""",
        """    if False:
        raise ValueError(f"{name} must be a finite real number")""",
        AUDIO_TESTS,
        "test_wiener_rejects_nan_and_inf_scalar_parameters",
    ),
    (
        "M25 lease refresh PUTs without an ETag precondition",
        DRIVERS,
        """        if not head_etag or not get_etag:
            raise VaultTransportError("S3 lease update requires a non-empty ETag precondition")""",
        """        if False:
            raise VaultTransportError("S3 lease update requires a non-empty ETag precondition")""",
        FENCE_TESTS,
        "test_update_push_lease_refuses_to_send_put_when_head_or_get_lacks_etag",
    ),
    (
        "M26 unreadable prior lease treated as absent",
        DRIVERS,
        """        if prior.status_code != 200 or not prior.body:
            raise VaultTransportError(
                f"S3 update_push_lease could not read the prior lease {valid_id!r} "
                f"(HTTP {prior.status_code}); refusing to refresh without a prior body "
                "to restore"
            )""",
        """        if False:
            raise VaultTransportError("never reached")""",
        FENCE_TESTS,
        "test_update_push_lease_aborts_before_put_when_prior_get_fails_or_disagrees_with_head",
    ),
    (
        "M27 release trusts a fresh read instead of its own write",
        DRIVERS,
        """        elif valid_id in self._owned_lease_etags:
            # Our own write: condition on the ETag it produced, so a refresh by another
            # writer turns this release into a 412 instead of a silent takeover.
            etag_to_match = self._owned_lease_etags[valid_id][0]""",
        """        elif valid_id in self._owned_lease_etags:
            observed = self._read_lease_state(obj_key)
            etag_to_match = ""
            if observed is not None:
                etag_to_match = observed.etag""",
        FENCE_TESTS,
        "test_release_push_lease_without_explicit_etag_is_still_bound_to_owner_etag",
    ),
    (
        "M28 chunk generation ignored on an unversioned bucket",
        DRIVERS,
        """        version_id = lowered.get("x-amz-version-id", "").strip()
        generation = lowered.get(CHUNK_GENERATION_META_HEADER, "").strip()""",
        """        version_id = lowered.get("x-amz-version-id", "").strip()
        generation = (
            lowered.get(CHUNK_GENERATION_META_HEADER, "").strip() if version_id else ""
        )""",
        FENCE_TESTS,
        "test_identical_bytes_aba_detected_via_metadata_generation_when_bucket_has_no_version_id",
    ),
    (
        "M24 non-finite clean_reference silently ignored",
        WIENER,
        """        reference = _require_finite("clean_reference", clean_reference).reshape(-1)""",
        """        reference = np.asarray(clean_reference, dtype=np.float64).reshape(-1)[
            : output.size
        ]""",
        AUDIO_TESTS,
        "test_wiener_rejects_non_finite_clean_reference",
    ),
)

_PYTEST_TESTS_FAILED = 1
_COLLECTION_CRASH_MARKERS = (
    "ImportError",
    "ModuleNotFoundError",
    "SyntaxError",
    "ERROR collecting",
)


@dataclass(frozen=True)
class KillReceipt:
    """Structured proof that one mutant died from a failing assertion."""

    mutant_id: str
    exit_code: int
    killed_by: str


def _clone_tree(destination: Path) -> Path:
    """Hardlink-copy ``src`` and ``tests`` into ``destination`` (instant, no disk)."""
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("src", "tests"):
        shutil.copytree(REPO_ROOT / name, destination / name, copy_function=os.link)
    shutil.copy(REPO_ROOT / "pyproject.toml", destination / "pyproject.toml")
    return destination


def _run_pytest(tree: Path, target: str) -> subprocess.CompletedProcess[str]:
    env: dict[str, Any] = dict(os.environ)
    env["PYTHONPATH"] = str(tree / "src")
    env["COVERAGE_FILE"] = "/tmp/.coverage_cp4_mutation"
    return subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q", "-p", "no:cacheprovider", "-x"],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


def _failed_node_ids(stdout: str) -> list[str]:
    ids: list[str] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("FAILED "):
            node = stripped[len("FAILED ") :].split(" ")[0].strip()
            if node:
                ids.append(node)
    return ids


def test_campaign_has_the_expected_mutants() -> None:
    assert len(MUTANTS) == 28
    assert len({m[0] for m in MUTANTS}) == 28


def test_every_mutation_anchor_is_unique() -> None:
    """Each ``original`` must appear exactly once, or the campaign is meaningless."""
    for mutant_id, rel, old, _new, _target, _expected in MUTANTS:
        text = (SRC / rel).read_text(encoding="utf-8")
        assert text.count(old) == 1, f"{mutant_id}: anchor count {text.count(old)} in {rel}"


def test_every_target_test_file_exists() -> None:
    for _mid, _rel, _old, _new, target, _expected in MUTANTS:
        assert (REPO_ROOT / target).is_file(), f"missing target test {target}"


@pytest.mark.parametrize(("mutant_id", "rel", "old", "new", "target", "expected"), MUTANTS)
def test_mutant_is_killed(
    mutant_id: str,
    rel: str,
    old: str,
    new: str,
    target: str,
    expected: str,
    tmp_path: Path,
) -> None:
    """Inject one real defect and require a real assertion kill for it.

    Step A — the unmutated tree is green for the target file (otherwise a "kill" is
    just a pre-existing failure); Step B — the mutated module still parses; Step C —
    pytest exits exactly ``1`` and names a ``FAILED test_...`` node with no
    collection crash markers.
    """
    baseline_tree = _clone_tree(tmp_path / "baseline")
    baseline = _run_pytest(baseline_tree, target)
    assert baseline.returncode == 0, (
        f"{mutant_id}: baseline for {target} is not green (exit {baseline.returncode})\n"
        f"{baseline.stdout[-2000:]}"
    )

    tree = _clone_tree(tmp_path / "mutant")
    module = tree / "src" / rel
    original = (SRC / rel).read_text(encoding="utf-8")
    assert original.count(old) == 1
    mutated = original.replace(old, new, 1)
    ast.parse(mutated, filename=str(module))
    module.unlink()  # break the hardlink before writing: never touch the real repo
    module.write_text(mutated, encoding="utf-8")

    result = _run_pytest(tree, target)
    combined = result.stdout + result.stderr
    node_ids = _failed_node_ids(result.stdout)
    if result.returncode == 0:
        raise AssertionError(
            f"{mutant_id}: MUTANT SURVIVED ({old!r} -> {new!r})\n{result.stdout[-2000:]}"
        )
    assert result.returncode == _PYTEST_TESTS_FAILED, (
        f"{mutant_id}: not an assertion kill (exit {result.returncode})\n{combined[-2000:]}"
    )
    for marker in _COLLECTION_CRASH_MARKERS:
        assert marker not in combined, f"{mutant_id}: infra crash {marker!r}\n{combined[-2000:]}"
    assert node_ids, f"{mutant_id}: exit 1 without a FAILED node id\n{result.stdout[-2000:]}"
    # The kill must come from the test that actually exercises the guard being
    # removed; a mutant killed by an unrelated test would prove nothing about it.
    assert any(expected in node for node in node_ids), (
        f"{mutant_id}: killed by {node_ids}, but the guard is only exercised by "
        f"{expected!r} — the mutant was not detected by the test that owns it"
    )
    print(f"MUTANT KILLED {mutant_id} -> {expected}")
    assert KillReceipt(mutant_id, result.returncode, expected).exit_code == _PYTEST_TESTS_FAILED
