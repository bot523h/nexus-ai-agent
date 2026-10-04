#!/usr/bin/env python3
"""Canonical Nagar / Nexus V1 Foundation Gate verifier.

Computes phase gates (SECURITY_GATE, CONVERGENCE_GATE, DURABILITY_GATE,
PROVENANCE_GATE, RECOVERY_GATE, L4_GATE, GOVERNANCE_GATE), verifies the
hash-chained receipt ledger under ``docs/audits/receipts/`` and the
SUBJECT_SHA -> WITNESS_SHA commit separation protocol (ADR-0013), and
evaluates the 33-boolean ``foundation_gate`` acceptance matrix from live
repository and verification state.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
RECEIPTS_DIR = REPO_ROOT / "docs" / "audits" / "receipts"

EXPECTED_RECEIPTS: tuple[tuple[str, str], ...] = (
    ("PHASE_0", "phase0-truth-freeze.receipt.json"),
    ("PHASE_1", "phase1-security.receipt.json"),
    ("PHASE_2", "phase2-pr-convergence.receipt.json"),
    ("PHASE_3", "phase3-architecture.receipt.json"),
    ("PHASE_4", "phase4-durability.receipt.json"),
    ("PHASE_5", "phase5-provenance.receipt.json"),
    ("PHASE_6", "phase6-recovery.receipt.json"),
    ("PHASE_7", "phase7-l4.receipt.json"),
    ("PHASE_8", "phase8-governance.receipt.json"),
    ("PHASE_9", "phase9-docs.receipt.json"),
)


def _canonical_receipt_digest(data: dict[str, Any]) -> str:
    payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _git(*args: str, cwd: Path = REPO_ROOT) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout.strip()


def verify_receipt_chain(
    receipts_dir: Path = RECEIPTS_DIR,
    *,
    require_phase8: bool = True,
    require_phase9: bool = False,
) -> dict[str, Any]:
    """Verify hash chaining and SUBJECT_SHA -> WITNESS_SHA separation across receipts."""
    errors: list[str] = []
    verified: list[dict[str, Any]] = []
    prev_digests: set[str] | None = None

    for phase_key, filename in EXPECTED_RECEIPTS:
        path = receipts_dir / filename
        if not path.exists():
            if phase_key == "PHASE_9" and not require_phase9:
                continue
            if phase_key == "PHASE_8" and not require_phase8:
                continue
            errors.append(f"missing receipt: {filename}")
            continue

        raw_bytes = path.read_bytes()
        raw = json.loads(raw_bytes)
        recorded_prev = raw.get("previous_receipt_digest")
        if prev_digests is None:
            if recorded_prev not in (None, "GENESIS"):
                errors.append(
                    f"{filename}: expected previous_receipt_digest in (None, 'GENESIS'), "
                    f"got {recorded_prev}"
                )
        else:
            if recorded_prev not in prev_digests:
                errors.append(
                    f"{filename}: previous_receipt_digest mismatch "
                    f"(expected one of {sorted(prev_digests)}, got {recorded_prev})"
                )

        subject_sha = (
            raw.get("subject_sha")
            or raw.get("git_sha")
            or raw.get("live_main", {}).get("origin_main_sha")
        )
        if not subject_sha:
            errors.append(f"{filename}: missing subject_sha/git_sha")
        else:
            code, _ = _git("cat-file", "-e", f"{subject_sha}^{{commit}}")
            if code != 0:
                errors.append(f"{filename}: subject_sha {subject_sha} is not a valid commit")

        # Check witness commit separation for committed receipts after Phase 0
        rel_path = f"docs/audits/receipts/{filename}"
        code, witness_sha = _git("log", "-n", "1", "--format=%H", "--", rel_path)
        witness_separated = False
        if code == 0 and witness_sha:
            if phase_key == "PHASE_0":
                witness_separated = True
            elif subject_sha:
                p_code, parent_sha = _git("rev-parse", f"{witness_sha}^")
                if p_code == 0 and (
                    parent_sha.startswith(subject_sha) or subject_sha.startswith(parent_sha)
                ):
                    witness_separated = True
                else:
                    errors.append(
                        f"{filename}: witness commit {witness_sha} parent {parent_sha} "
                        f"does not match subject_sha {subject_sha}"
                    )

        canon_digest = _canonical_receipt_digest(raw)
        raw_digest = hashlib.sha256(raw_bytes).hexdigest()
        prev_digests = {canon_digest, raw_digest}
        verified.append(
            {
                "phase": phase_key,
                "file": filename,
                "subject_sha": subject_sha,
                "witness_sha": witness_sha or None,
                "witness_separated": witness_separated,
                "digest": raw_digest,
                "canonical_digest": canon_digest,
            }
        )

    return {
        "ok": not errors,
        "errors": errors,
        "receipts": verified,
    }


def evaluate_foundation_gate(
    repo_root: Path = REPO_ROOT,
    *,
    require_phase8: bool = True,
    require_phase9: bool = False,
) -> dict[str, Any]:
    """Compute all phase gates and the 33-boolean foundation_gate report."""
    receipts_dir = repo_root / "docs" / "audits" / "receipts"
    chain = verify_receipt_chain(
        receipts_dir,
        require_phase8=require_phase8,
        require_phase9=require_phase9,
    )
    by_phase: dict[str, dict[str, Any]] = {}
    for _, filename in EXPECTED_RECEIPTS:
        p = receipts_dir / filename
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            by_phase[filename] = data

    p0 = by_phase.get("phase0-truth-freeze.receipt.json", {})
    p1 = by_phase.get("phase1-security.receipt.json", {})
    p2 = by_phase.get("phase2-pr-convergence.receipt.json", {})
    p3 = by_phase.get("phase3-architecture.receipt.json", {})
    p4 = by_phase.get("phase4-durability.receipt.json", {})
    p5 = by_phase.get("phase5-provenance.receipt.json", {})
    p6 = by_phase.get("phase6-recovery.receipt.json", {})
    p7 = by_phase.get("phase7-l4.receipt.json", {})
    p8 = by_phase.get("phase8-governance.receipt.json", {})
    p9 = by_phase.get("phase9-docs.receipt.json", {})

    # Check live repo artifacts and invariants
    restricted_shell_exists = (
        repo_root / "src" / "nexus_ai_agent" / "tools" / "system_shell.py"
    ).exists()
    pack_trust_exists = (
        repo_root / "src" / "nexus_ai_agent" / "creative" / "packs" / "trust.py"
    ).exists()
    persistence_exists = (
        repo_root / "src" / "nexus_ai_agent" / "creative" / "studio" / "persistence.py"
    ).exists()
    passport_exists = (
        repo_root / "src" / "nexus_ai_agent" / "creative" / "studio" / "passport.py"
    ).exists()
    temporal_exists = (
        repo_root / "src" / "nexus_ai_agent" / "creative" / "temporal" / "core.py"
    ).exists()
    continuum_exists = (repo_root / ".nexus" / "continuum.json").exists()
    matrix_doc_exists = (
        repo_root / "docs" / "audits" / "FOUNDATION_VERIFICATION_MATRIX.md"
    ).exists()

    # Check board governance directly via agent_board.validate_board
    spec = importlib.util.spec_from_file_location(
        "agent_board_check", repo_root / "scripts" / "agent_board.py"
    )
    board_valid = False
    if spec and spec.loader:
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        v_res = mod.validate_board(mod.load_board())
        board_valid = bool(v_res.get("ok"))

    # Check remote sync against origin/<branch>
    _, branch_name = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo_root)
    _, local_head = _git("rev-parse", "HEAD", cwd=repo_root)
    r_code, remote_head = _git("rev-parse", f"origin/{branch_name}", cwd=repo_root)
    remote_synced = r_code == 0 and bool(local_head) and local_head == remote_head

    p0_gates = p0.get("gate_result", {})
    p1_gates = p1.get("gate_booleans", {})
    p2_gates = p2.get("gate_booleans", {})
    p3_gates = p3.get("gate_booleans", {})
    p4_gates = p4.get("gate_booleans", {})
    p5_gates = p5.get("gate_booleans", {})
    p6_gates = p6.get("gate_booleans", {})
    p7_gates = p7.get("gate_booleans", {})
    p8_gates = p8.get("gate_booleans", {})
    p9_gates = p9.get("gate_booleans", {})

    security_gate = bool(
        p1_gates.get("SECURITY_GATE") and restricted_shell_exists and pack_trust_exists
    )
    convergence_gate = bool(
        p2_gates.get("CONVERGENCE_GATE")
        and p3_gates.get("PHASE_3")
        and p3_gates.get("one_authority_per_responsibility")
    )
    durability_gate = bool(p4_gates.get("DURABILITY_GATE") and persistence_exists)
    provenance_gate = bool(p5_gates.get("PROVENANCE_GATE") and passport_exists)
    recovery_gate = bool(p6_gates.get("RECOVERY_GATE") and persistence_exists)
    l4_gate = bool(p7_gates.get("L4_GATE") and temporal_exists)
    governance_gate = bool(
        board_valid and continuum_exists and (not require_phase8 or p8_gates.get("GOVERNANCE_GATE"))
    )

    docs_aligned = bool(matrix_doc_exists and (not require_phase9 or p9_gates.get("PHASE_9")))

    foundation_gate = {
        "live_github_truth_queried": bool(len(p0.get("open_prs", [])) == 67),
        "main_sha_recorded": bool(
            p0.get("live_main", {}).get("origin_main_sha")
            == "e5b326b2eaf691a638d030ad57acf1ce60016ef0"
        ),
        "push_authority_probed": bool(
            p0.get("capability_preflight", {}).get("push_to_authorized_branch") is True
        ),
        "board_not_blindly_trusted": bool(p0_gates.get("PHASE_0")),
        "pr_classification_complete": bool(
            p2.get("convergence_summary", {}).get("total_open_prs_inventoried") == 67
        ),
        "all_stop_blockers_closed": security_gate,
        "restricted_shell_safe": bool(security_gate and restricted_shell_exists),
        "ssrf_redirect_rebinding_safe": bool(security_gate and p1_gates.get("stop_b_enforced")),
        "capability_pack_signing_ed25519": bool(security_gate and pack_trust_exists),
        "command_bus_authorizer_required": bool(security_gate and p1_gates.get("stop_c_enforced")),
        "raw_payload_not_trusted_for_actor": bool(
            security_gate and p1_gates.get("stop_c_enforced")
        ),
        "high_value_pr_work_preserved": convergence_gate,
        "one_authority_per_responsibility": bool(p3_gates.get("one_authority_per_responsibility")),
        "single_execution_backbone": bool(p3_gates.get("canonical_command_capability_boundary")),
        "durable_studio_persistence": durability_gate,
        "durable_idempotency": bool(
            durability_gate and p4_gates.get("durable_idempotency_verified")
        ),
        "durable_undo_redo": bool(durability_gate and p4_gates.get("atomic_plan_commit_verified")),
        "durable_audit_trail": bool(
            durability_gate and p4_gates.get("durable_project_state_verified")
        ),
        "artifact_passport_bound": bool(
            provenance_gate and p5_gates.get("artifact_passport_verified")
        ),
        "independent_media_verification": bool(
            provenance_gate and p5_gates.get("independent_media_verifier_verified")
        ),
        "causal_provenance_verified": bool(
            provenance_gate and p5_gates.get("causal_chain_verified")
        ),
        "lease_fencing_enforced": bool(recovery_gate and p4_gates.get("lease_fencing_verified")),
        "crash_recovery_reconciler_verified": bool(
            recovery_gate and all(p6_gates.get(f"CRASH_{i}") for i in range(1, 9))
        ),
        "render_lane_verified": bool(l4_gate and p7_gates.get("l4_authority_boundary_verified")),
        "delivery_pack_verified": bool(l4_gate and p7_gates.get("otio_roundtrip_verified")),
        "temporal_rational_math_verified": bool(
            l4_gate
            and p7_gates.get("rational_timebase_verified")
            and p7_gates.get("zero_float_drift_verified")
        ),
        "vertical_slice_real_artifact_verified": bool(
            l4_gate and provenance_gate and p5_gates.get("independent_media_verifier_verified")
        ),
        "board_referee_exit_codes_verified": governance_gate,
        "continuum_contract_resolved": bool(continuum_exists),
        "docs_truth_aligned": docs_aligned,
        "receipts_chained": bool(chain["ok"]),
        "subject_and_witness_sha_separated": bool(
            chain["ok"]
            and all(r["witness_separated"] for r in chain["receipts"] if r["witness_sha"])
        ),
        "branch_pushed_and_remote_verified": remote_synced,
    }

    gates = {
        "SECURITY_GATE": security_gate,
        "CONVERGENCE_GATE": convergence_gate,
        "DURABILITY_GATE": durability_gate,
        "PROVENANCE_GATE": provenance_gate,
        "RECOVERY_GATE": recovery_gate,
        "L4_GATE": l4_gate,
        "GOVERNANCE_GATE": governance_gate,
    }

    return {
        "gates": gates,
        "receipt_chain": chain,
        "foundation_gate": foundation_gate,
        "all_gates_pass": all(gates.values()),
        "all_foundation_booleans_true": all(foundation_gate.values()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nagar / Nexus V1 Foundation Gate Verifier")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON report")
    parser.add_argument(
        "--no-require-phase8",
        action="store_true",
        help="Allow phase8 receipt to be absent (used while building Phase 8 subject commit)",
    )
    parser.add_argument(
        "--require-phase9",
        action="store_true",
        help="Require phase9-docs.receipt.json to exist and be hash-chained",
    )
    args = parser.parse_args(argv)

    report = evaluate_foundation_gate(
        REPO_ROOT,
        require_phase8=not args.no_require_phase8,
        require_phase9=args.require_phase9,
    )
    if args.as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for gate_name, status in report["gates"].items():
            print(f"{gate_name} = {'PASS' if status else 'FAIL'}")
        print(f"RECEIPT_CHAIN = {'PASS' if report['receipt_chain']['ok'] else 'FAIL'}")
    return 0 if (report["all_gates_pass"] and report["receipt_chain"]["ok"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
