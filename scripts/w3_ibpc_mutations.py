#!/usr/bin/env python3
"""Adversarial mutation harness for Wave 3 (durable IBPC live editing).

Evidence rule: a guard that has never been attacked is not evidence.  Every
mutation below rewrites one shipped Wave 3 guard with a specific weakening, then
runs the Wave 3 suites in an **isolated /tmp sandbox** and requires them to turn
RED.  The mutation is undone from the pristine bytes and the file's SHA-256 is
verified before the next mutant starts; the pristine tree must be GREEN again at
the end.

Why a sandbox: the shipped repo (and its ``__pycache__``/``.pytest_cache``) is
never touched, so a crash mid-run can never leave a mutated module behind, and a
stale bytecode cache can never execute the *un*mutated source (a silent
false-negative — the single most dangerous failure mode of a mutation harness).

Three bytecode layers are purged before **every** run: (1) the sandbox tree's
``__pycache__``/``*.pyc`` files, (2) a dedicated ``PYTHONPYCACHEPREFIX``
directory outside the tree, (3) ``PYTHONDONTWRITEBYTECODE=1`` so nothing new is
written at all; ``-p no:cacheprovider`` disables the pytest cache too.

The oracle is strict: **only** pytest ``returncode == 1`` counts as a kill.  A
usage/internal/no-tests exit (2/3/4/5) is an infrastructure error, never a kill.

Usage::

    python scripts/w3_ibpc_mutations.py            # run every mutation
    python scripts/w3_ibpc_mutations.py --list     # print the catalogue
    python scripts/w3_ibpc_mutations.py --only a,b # spot-check a subset

Exit codes: 0 = N/N killed; 1 = a guard is fake (survivor); 2 = baseline red or
sandbox unusable; 3 = restore failed; 4 = anchor drift / harness error.
"""

# ruff: noqa: E501 - the MUTATIONS table holds verbatim source snippets; they must
# match the shipped files byte-for-byte, so they cannot be re-wrapped.
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IBPC = Path("src") / "nexus_ai_agent" / "nagar" / "ibpc"
TESTS = (
    "tests/unit/creative/ibpc",
    "tests/unit/test_ibpc_approval.py",
    "tests/unit/test_ibpc_session.py",
)
#: everything the Wave 3 suites import from the repo root
STAGED = ("src", "tests", "pyproject.toml")


@dataclass(frozen=True)
class Mutation:
    name: str
    path: Path
    edits: tuple[tuple[str, str], ...]
    why: str


MUTATIONS: tuple[Mutation, ...] = (
    # -- slice 3.1: durable live session -----------------------------------
    Mutation(
        "idempotency_lookup_disabled",
        IBPC / "live_session.py",
        (
            (
                "            prior = self._fetch_turn_by_key(idempotency_key)",
                "            prior = None  # mutation: idempotency replay disabled",
            ),
        ),
        "f(f(x)) == f(x): a duplicate submission must return the stored turn",
    ),
    Mutation(
        "stale_revision_check_removed",
        IBPC / "live_session.py",
        (
            (
                "            # 3) stale-revision protection: the turn must be compiled against the\n"
                "            #    revision the session is actually on.\n"
                "            if base_revision_id != session.active_revision_id:",
                "            # 3) stale-revision protection: the turn must be compiled against the\n"
                "            #    revision the session is actually on.\n"
                "            if False:",
            ),
        ),
        "a turn compiled against a stale revision must fail closed (StaleSessionRevisionError)",
    ),
    Mutation(
        "preview_stale_revision_check_removed",
        IBPC / "live_session.py",
        (
            (
                "            if base_revision_id != session.active_revision_id:\n"
                "                raise StaleSessionRevisionError(\n"
                '                    "stale_revision",\n'
                '                    f"edit base {base_revision_id!r} != active {session.active_revision_id!r}",',
                "            if False:  # mutation: a stale base revision is accepted\n"
                "                raise StaleSessionRevisionError(\n"
                '                    "stale_revision",\n'
                '                    f"edit base {base_revision_id!r} != active {session.active_revision_id!r}",',
            ),
        ),
        "a preview attached to a stale base revision must fail closed",
    ),
    Mutation(
        "monotonic_turn_index_broken",
        IBPC / "live_session.py",
        (
            (
                "            index = session.last_turn_index + 1",
                "            index = 1  # mutation: monotonic index disabled",
            ),
        ),
        "turn_index must increase strictly per session (never reused)",
    ),
    Mutation(
        "epoch_cas_conflict_ignored",
        IBPC / "live_session.py",
        (
            (
                '        if cursor.rowcount != 1:\n            raise ConcurrentSessionMutationError(\n                conflict_reason, f"{session.session_id!r} was mutated concurrently"\n            )',
                '        if False:  # mutation: version_epoch CAS never reports a conflict\n            raise ConcurrentSessionMutationError(\n                conflict_reason, f"{session.session_id!r} was mutated concurrently"\n            )',
            ),
        ),
        "a lost version_epoch compare-and-swap must raise ConcurrentSessionMutationError",
    ),
    Mutation(
        "termination_does_not_revoke_approvals",
        IBPC / "live_session.py",
        (
            (
                "            if revoke_approvals is not None:\n                revoke_approvals(session_id)",
                "            if False:  # mutation: unconsumed preview approvals survive close()\n                revoke_approvals(session_id)",
            ),
        ),
        "close_session/abort_session must invalidate unconsumed preview approvals",
    ),
    Mutation(
        "wal_journal_mode_disabled",
        IBPC / "live_session.py",
        (
            (
                '        self._conn.execute("PRAGMA journal_mode=WAL")',
                '        self._conn.execute("PRAGMA journal_mode=MEMORY")  # mutation',
            ),
        ),
        "the store must run in WAL mode (crash-safe multi-process writes)",
    ),
    Mutation(
        "synchronous_full_disabled",
        IBPC / "live_session.py",
        (
            (
                '        self._conn.execute("PRAGMA synchronous=FULL")',
                '        self._conn.execute("PRAGMA synchronous=OFF")  # mutation',
            ),
        ),
        "the store must fsync on commit (synchronous=FULL)",
    ),
    Mutation(
        "foreign_keys_disabled",
        IBPC / "live_session.py",
        (
            (
                '        self._conn.execute("PRAGMA foreign_keys=ON")',
                '        self._conn.execute("PRAGMA foreign_keys=OFF")  # mutation',
            ),
        ),
        "turn rows must be referentially bound to their session",
    ),
    # -- slice 3.2: canonical single-use approvals -------------------------
    Mutation(
        "verify_mac_bypass",
        IBPC / "approval.py",
        (
            (
                '        if not hmac.compare_digest(actual, expected):\n            raise ApprovalBindingMismatchError("the stored binding MAC does not verify")',
                '        if False:  # mutation: verify_canonical trusts any MAC\n            raise ApprovalBindingMismatchError("the stored binding MAC does not verify")',
            ),
        ),
        "verify_canonical must reject a tampered binding MAC",
    ),
    Mutation(
        "consume_mac_bypass",
        IBPC / "approval.py",
        (
            (
                "        if not hmac.compare_digest(mac, expected):",
                "        if False:  # mutation: consume_approval trusts any MAC",
            ),
        ),
        "consume_approval must reject a tampered binding MAC",
    ),
    Mutation(
        "consume_payload_compare_bypass",
        IBPC / "approval.py",
        (
            (
                "        if payload is not None and payload.as_dict() != stored.as_dict():",
                "        if False:  # mutation: a presented payload is never compared",
            ),
        ),
        "a substituted binding payload must be refused before the consume",
    ),
    Mutation(
        "consume_expiry_bypass",
        IBPC / "approval.py",
        (
            (
                "        if _utc_instant(self._now()) >= expires:",
                "        if False:  # mutation: expired tokens are consumable",
            ),
        ),
        "an expired approval can never be consumed (ApprovalExpiredError)",
    ),
    Mutation(
        "single_use_bypass",
        IBPC / "approval.py",
        (
            (
                "            if binding.state is ApprovalState.CONSUMED:\n                reservation = self._read_reservation(approval_id)",
                "            if False:  # mutation: an approval may be consumed twice\n                reservation = self._read_reservation(approval_id)",
            ),
        ),
        "an approval is single-use: replay raises ApprovalAlreadyConsumedError",
    ),
    Mutation(
        "bind_once_bypass",
        IBPC / "approval.py",
        (
            (
                "            if existing is not None and not hmac.compare_digest(existing, mac):",
                "            if False:  # mutation: an approval can be re-bound",
            ),
        ),
        "an approval is bound once; re-binding to another payload is refused",
    ),
    Mutation(
        "naive_timestamp_accepted",
        IBPC / "approval.py",
        (
            (
                '    if parsed.tzinfo is None:\n        raise ApprovalExpiredError("timestamp must be timezone-aware (UTC)")',
                '    if False:  # mutation: naive timestamps are treated as UTC\n        raise ApprovalExpiredError("timestamp must be timezone-aware (UTC)")',
            ),
        ),
        "an unparsable/naive instant must fail closed, never be assumed UTC",
    ),
    Mutation(
        "empty_binding_field_accepted",
        IBPC / "approval.py",
        (
            (
                '        if not isinstance(value, str) or not value:\n            raise ApprovalBindingMismatchError(f"binding field {name!r} must be a non-empty string")',
                '        if False:  # mutation: empty binding fields enter the signed payload\n            raise ApprovalBindingMismatchError(f"binding field {name!r} must be a non-empty string")',
            ),
        ),
        "every signed binding field must be a non-empty string (no empty-scope approval)",
    ),
    # -- slice 3.3: durable revocable consent ------------------------------
    Mutation(
        "revocation_does_not_flip_state",
        IBPC / "privacy.py",
        (
            (
                "                (ConsentState.REVOKED.value, stamp, existing.consent_id),",
                "                (ConsentState.GRANTED.value, stamp, existing.consent_id),  # mutation",
            ),
        ),
        "revoke_consent must take effect immediately (the row flips to REVOKED)",
    ),
    Mutation(
        "gate_revoked_state_ignored",
        IBPC / "privacy.py",
        (
            (
                "            if record.state is ConsentState.REVOKED:",
                "            if False:  # mutation: a revoked grant still authorizes processing",
            ),
        ),
        "the fail-closed gate must refuse a REVOKED grant",
    ),
    Mutation(
        "gate_expiry_ignored",
        IBPC / "privacy.py",
        (
            (
                "            if _parse_instant(self._now()) >= _parse_instant(record.expires_at_utc):",
                "            if False:  # mutation: consent never expires",
            ),
        ),
        "consent is time-bounded: an expired grant must refuse processing",
    ),
    Mutation(
        "gate_provider_binding_ignored",
        IBPC / "privacy.py",
        (
            (
                '            " FROM privacy_consents WHERE principal_id = ? AND project_id = ? AND provider_id = ?"\n            " AND data_category = ? AND action = ?",\n            (principal_id, project_id, provider_id, data_category.value, action.value),',
                '            " FROM privacy_consents WHERE principal_id = ? AND project_id = ? AND ? IS NOT NULL"\n            " AND data_category = ? AND action = ?",  # mutation: provider not bound\n            (principal_id, project_id, provider_id, data_category.value, action.value),',
            ),
        ),
        "consent is bound to the exact provider (never usable cross-provider)",
    ),
    Mutation(
        "audit_chain_verification_bypassed",
        IBPC / "privacy.py",
        (
            (
                '            if expected != event["event_sha256"]:\n                return False',
                "            if False:  # mutation: a tampered audit row still verifies\n                return False",
            ),
        ),
        "the append-only audit log is hash-chained: an edited/deleted row never verifies",
    ),
    # -- slice 3.4: studio principal authority -----------------------------
    Mutation(
        "role_check_bypassed",
        IBPC / "principal_authority.py",
        (
            (
                "        if facts.role not in allowed:",
                "        if False:  # mutation: any role may perform any studio action",
            ),
        ),
        "VIEWER/AUDITOR must never gain EDITOR/OWNER studio actions",
    ),
    Mutation(
        "binding_owner_check_bypassed",
        IBPC / "principal_authority.py",
        (
            (
                "            if binding[0] != principal_id:",
                "            if False:  # mutation: cross-user channel binding accepted",
            ),
        ),
        "a principal presenting another principal's binding is refused (cross-user IDOR)",
    ),
    Mutation(
        "membership_check_bypassed",
        IBPC / "principal_authority.py",
        (
            (
                "            membership = self._fetch_membership(project_id, principal_id)\n            if membership is None:",
                "            membership = self._fetch_membership(project_id, principal_id)\n            if False:  # mutation: non-members are authorized",
            ),
        ),
        "a non-member of the target project is refused (cross-project IDOR)",
    ),
    Mutation(
        "membership_project_scope_ignored",
        IBPC / "principal_authority.py",
        (
            (
                '            "SELECT project_id, principal_id, role, created_at_utc FROM studio_memberships"\n            " WHERE project_id = ? AND principal_id = ?",\n            (project_id, principal_id),',
                '            "SELECT project_id, principal_id, role, created_at_utc FROM studio_memberships"\n            " WHERE ? IS NOT NULL AND principal_id = ?",  # mutation: project ignored\n            (project_id, principal_id),',
            ),
        ),
        "membership is scoped to the target project (project A never authorizes B)",
    ),
    # -- service wiring ----------------------------------------------------
    Mutation(
        "principal_authorization_bypassed",
        IBPC / "service.py",
        (
            (
                "        authority = self._deps.principal_authority\n        binding_ref = self._deps.channel_binding_ref\n        if not binding_ref:",
                "        return AuthorizedPrincipalFacts(  # mutation: everything is authorized\n"
                "            principal_id=principal_id,\n"
                "            project_id=project_id,\n"
                '            channel_binding_id="cbn_" + "0" * 64,\n'
                "            role=MembershipRole.OWNER,\n"
                "        )\n"
                "        authority = self._deps.principal_authority\n        binding_ref = self._deps.channel_binding_ref\n        if not binding_ref:",
            ),
        ),
        "the service must refuse session start / turn / approve / consume for an unauthorized principal",
    ),
    Mutation(
        "durable_consent_gate_disabled",
        IBPC / "service.py",
        (
            (
                "        store = self._deps.privacy_consent_store\n        if store is None:\n            return",
                "        store = self._deps.privacy_consent_store\n        if True:  # mutation: the durable consent ledger is never consulted\n            return",
            ),
        ),
        "no media evidence work happens without a live durable consent grant",
    ),
    Mutation(
        "canonical_payload_not_presented",
        IBPC / "service.py",
        (
            (
                "        payload = self._issued_binding_payload(ctx)\n        consume_canonical(",
                "        payload = None  # mutation: the canonical binding is not presented\n        consume_canonical(",
            ),
        ),
        "execution must present the exact canonical payload it is bound to",
    ),
    Mutation(
        "scope_signature_check_removed",
        IBPC / "service.py",
        (
            (
                "            if ctx.scope_signature is None or not hmac.compare_digest(\n                ctx.scope_signature, self._scope_signature(ctx, identity)\n            ):",
                "            if False and (  # mutation: the approved scope signature is not re-checked\n                ctx.scope_signature is None or not hmac.compare_digest(\n                    ctx.scope_signature, self._scope_signature(ctx, identity)\n                )\n            ):",
            ),
        ),
        "a changed execution scope must be refused before any consume",
    ),
    Mutation(
        "approval_ttl_effectively_infinite",
        IBPC / "service.py",
        (
            (
                "            expires_at = (issued_at_dt + timedelta(minutes=APPROVAL_TTL_MINUTES)).strftime(",
                "            expires_at = (issued_at_dt + timedelta(minutes=APPROVAL_TTL_MINUTES * 100000)).strftime(  # mutation",
            ),
        ),
        "an approval token is short-lived: an expired binding refuses execution",
    ),
    # -- R1a: the issuance instant is minted once and never recomputed ------
    Mutation(
        "issuance_instant_not_persisted",
        IBPC / "service.py",
        (
            (
                "            ctx.issued_expires_at_utc = expires_at",
                "            ctx.issued_expires_at_utc = None  # mutation: TTL instant dropped",
            ),
        ),
        "the expiry minted at issuance is persisted with the context and compared at consume",
    ),
    Mutation(
        "ttl_compared_against_a_fresh_clock",
        IBPC / "service.py",
        (
            (
                "        expires = _parse_instant(ctx.issued_expires_at_utc)\n        if current >= expires:",
                "        expires = _parse_instant(self._deps.now()) + timedelta(days=3650)  # mutation: the window slides\n        if current >= expires:",
            ),
        ),
        "an approval is judged against the instant it was ISSUED at, not against consume-time",
    ),
    # -- R1b: verification uses the stored key id --------------------------
    Mutation(
        "key_id_not_looked_up",
        IBPC / "approval.py",
        (
            (
                "            return self._key_provider.key(key_id).material",
                "            return self._key_provider.key(self._key_provider.active_key_id).material  # mutation: rotation unsafe",
            ),
        ),
        "the MAC is verified with the key the row was issued under (rotation-safe)",
    ),
    # -- R2a: the voice lane goes through the same boundary ----------------
    Mutation(
        "voice_intent_skips_the_consent_gate",
        IBPC / "service.py",
        (
            (
                "        refusal = self._require_media_consent(session, ConsentScope.AUDIO)\n        if refusal is not None:\n            return refusal",
                "        refusal = None  # mutation: audio processed without a consent grant\n        if refusal is not None:\n            return refusal",
            ),
        ),
        "no audio is transcribed without a durable AUDIO consent grant",
    ),
    Mutation(
        "voice_intent_skips_turn_authorization",
        IBPC / "service.py",
        (
            (
                "        refusal = self._authorize_turn_creation(session)\n        if refusal is not None:\n            return refusal\n        refusal = self._require_media_consent(session, ConsentScope.AUDIO)",
                "        refusal = None  # mutation: any member may open a voice turn\n        if refusal is not None:\n            return refusal\n        refusal = self._require_media_consent(session, ConsentScope.AUDIO)",
            ),
        ),
        "creating a turn is an EDITOR/OWNER action, re-verified on every turn",
    ),
    # -- R2b: identity never becomes a role -------------------------------
    Mutation(
        "synthetic_owner_reinstated",
        IBPC / "service.py",
        (
            (
                "            if self._deps.require_role_authority or action in _OWNER_ONLY_ACTIONS:",
                "            return AuthorizedPrincipalFacts(  # mutation: identity is treated as OWNER\n                principal_id=resolved.principal_id,\n                project_id=resolved.project_id,\n                channel_binding_id=resolved.channel_binding_id,\n                role=MembershipRole.OWNER,\n            )\n            if self._deps.require_role_authority or action in _OWNER_ONLY_ACTIONS:",
            ),
        ),
        "a verified identity is not a role: the OWNER-only action is refused without a role proof",
    ),
    # -- R3: a second process can rebuild and execute the approval ---------
    Mutation(
        "recovery_disabled",
        IBPC / "service.py",
        (
            (
                "    def recover_from_durable_state(self, *, force: bool = False) -> dict[str, Any]:",
                "    def recover_from_durable_state(self, *, force: bool = False) -> dict[str, Any]:\n        if not force:  # mutation: restart recovery disabled\n            return {'sessions': 0, 'turns': 0, 'contexts': 0, 'revoked': 0}",
            ),
        ),
        "Service B reconstructs the session, turn and executable context of Service A",
    ),
    Mutation(
        "recovered_contexts_overwrite_the_live_one",
        IBPC / "service.py",
        (
            (
                "                        if turn.approval_id in self._contexts:",
                "                        if False:  # mutation: a rebuilt context replaces the live one",
            ),
        ),
        "recovery never replaces the context the approval was signed over",
    ),
    Mutation(
        "terminated_session_approvals_never_reconciled",
        IBPC / "service.py",
        (
            (
                "                revoked = self._revoke_session_approvals(terminal.session_id)",
                "                revoked = ()  # mutation: a terminated session keeps live approvals",
            ),
        ),
        "a crash between the two stores cannot leave an approval executable after recovery",
    ),
    Mutation(
        "mirror_swallows_lifecycle_divergence",
        IBPC / "service.py",
        (
            (
                '        except TurnStoreError as exc:\n            if exc.reason not in _BENIGN_TURN_MIRROR_MISSES:\n                raise ApprovalError(f"durable_turn_not_mirrored:{exc.reason}", exc.detail) from exc',
                '        except TurnStoreError as exc:\n            if False:  # mutation: every lifecycle divergence is swallowed\n                raise ApprovalError(f"durable_turn_not_mirrored:{exc.reason}", exc.detail) from exc',
            ),
        ),
        "a durable lifecycle divergence between the stores is surfaced, not swallowed",
    ),
    Mutation(
        "context_write_failure_swallowed",
        IBPC / "service.py",
        (
            (
                '        except TurnStoreError as exc:\n            if exc.reason not in _BENIGN_TURN_MIRROR_MISSES:\n                raise ApprovalError(\n                    f"durable_context_not_persisted:{exc.reason}", exc.detail\n                ) from exc',
                '        except TurnStoreError as exc:\n            return  # mutation: an unwritable context is ignored\n            raise ApprovalError(\n                f"durable_context_not_persisted:{exc.reason}", exc.detail\n            ) from exc',
            ),
        ),
        "a context that cannot be persisted is a typed failure, never a silent skip",
    ),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stage_sandbox() -> Path:
    """Copy the shipped tree into a private /tmp sandbox and return its root."""
    sandbox = Path(tempfile.mkdtemp(prefix="nexus-w3-mutations-"))
    tree = sandbox / "tree"
    tree.mkdir()
    for name in STAGED:
        source = ROOT / name
        target = tree / name
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target)
    return tree


def _purge_bytecode(tree: Path, pycache_prefix: Path) -> None:
    """Purge all three bytecode layers so only the mutated source can run."""
    # layer 1: in-tree caches
    for cache in list(tree.rglob("__pycache__")):
        shutil.rmtree(cache, ignore_errors=True)
    for stale in list(tree.rglob("*.pyc")):
        stale.unlink(missing_ok=True)
    pytest_cache = tree / ".pytest_cache"
    if pytest_cache.exists():
        shutil.rmtree(pytest_cache, ignore_errors=True)
    # layer 2: the dedicated out-of-tree PYCACHEPREFIX
    if pycache_prefix.exists():
        shutil.rmtree(pycache_prefix, ignore_errors=True)
    pycache_prefix.mkdir(parents=True, exist_ok=True)
    # layer 3 is enforced by the environment in ``run_tests``
    # (PYTHONDONTWRITEBYTECODE=1), so nothing is regenerated at all.


def run_tests(tree: Path) -> int:
    """Run the Wave 3 suites inside the sandbox; return pytest's raw exit code.

    The raw code is returned, never collapsed to a bool: only ``1`` proves a
    mutation was caught by an assertion, while 2/3/4/5 is a harness error.
    """
    pycache_prefix = tree.parent / "pycache"
    _purge_bytecode(tree, pycache_prefix)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(tree / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONPYCACHEPREFIX"] = str(pycache_prefix)  # layer 2
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # layer 3
    env.pop("PYTHONSTARTUP", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-x",
            "-p",
            "no:cacheprovider",
            *(str(tree / test) for test in TESTS),
        ],
        cwd=tree,
        capture_output=True,
        text=True,
        env=env,
        shell=False,
    )
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    parser.add_argument(
        "--only",
        default="",
        help="comma-separated mutation names to run (default: all); the baseline is always run",
    )
    args = parser.parse_args()
    if args.list:
        for mutation in MUTATIONS:
            print(f"{mutation.name}: {mutation.why}")
        return 0

    selected = [name.strip() for name in args.only.split(",") if name.strip()]
    names = {mutation.name for mutation in MUTATIONS}
    if selected and not set(selected) <= names:
        print(f"unknown mutation name(s): {sorted(set(selected) - names)}")
        return 2
    catalogue = tuple(m for m in MUTATIONS if not selected or m.name in selected)

    tree = _stage_sandbox()
    print(f"sandbox: {tree}")

    print("baseline ... ", end="", flush=True)
    if run_tests(tree) != 0:
        print("RED — refusing to mutate a suite that is already failing")
        return 2
    print("GREEN")

    results: list[tuple[str, str, str, str, str]] = []
    killed = 0
    blocked = 0
    for mutation in catalogue:
        target = tree / mutation.path
        rel = mutation.path
        original = target.read_text(encoding="utf-8")
        before_sha = _sha256(target)
        mutated = original
        applicable = True
        for old, new in mutation.edits:
            if old not in mutated:
                print(f"{mutation.name}: ANCHOR DRIFT (source changed under the harness)")
                applicable = False
                break
            mutated = mutated.replace(old, new, 1)
        if not applicable:
            results.append((mutation.name, str(rel), mutation.why, "ANCHOR_DRIFT", "n/a"))
            blocked += 1
            continue
        target.write_text(mutated, encoding="utf-8")
        try:
            code = run_tests(tree)
        finally:
            target.write_text(original, encoding="utf-8")
        if _sha256(target) != before_sha:
            results.append(
                (mutation.name, str(rel), mutation.why, "RESTORE_FAILED", "sha256 mismatch")
            )
            blocked += 1
            continue
        if code == 1:
            killed += 1
            print(f"{mutation.name}: killed")
            results.append((mutation.name, str(rel), mutation.why, "KILLED", "pytest exit 1"))
        elif code == 0:
            print(f"{mutation.name}: SURVIVED — {mutation.why}")
            results.append((mutation.name, str(rel), mutation.why, "SURVIVED", "pytest exit 0"))
        else:
            print(f"{mutation.name}: HARNESS ERROR (pytest exit {code}) — not counted as a kill")
            results.append(
                (mutation.name, str(rel), mutation.why, "HARNESS_ERROR", f"pytest exit {code}")
            )
            blocked += 1

    print("restored baseline ... ", end="", flush=True)
    if run_tests(tree) != 0:
        print("RED — restore failed")
        return 3
    print("GREEN")

    print()
    print("mutation evidence (name | file | invariant | result | evidence)")
    for name, rel, why, result, evidence in results:
        print(f"- {name} | {rel} | {why} | {result} | {evidence}")
    print()
    print(f"{killed}/{len(catalogue)} killed, {blocked} blocked (anchor drift / harness error)")
    shutil.rmtree(tree.parent, ignore_errors=True)
    if blocked:
        return 4
    return 0 if killed == len(catalogue) else 1


if __name__ == "__main__":
    raise SystemExit(main())
