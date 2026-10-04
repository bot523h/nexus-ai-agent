"""Unit tests for the canonical Foundation Gate verifier (scripts/foundation_gate.py)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "foundation_gate.py"

EXPECTED_FOUNDATION_KEYS = {
    "live_github_truth_queried",
    "main_sha_recorded",
    "push_authority_probed",
    "board_not_blindly_trusted",
    "pr_classification_complete",
    "all_stop_blockers_closed",
    "restricted_shell_safe",
    "ssrf_redirect_rebinding_safe",
    "capability_pack_signing_ed25519",
    "command_bus_authorizer_required",
    "raw_payload_not_trusted_for_actor",
    "high_value_pr_work_preserved",
    "one_authority_per_responsibility",
    "single_execution_backbone",
    "durable_studio_persistence",
    "durable_idempotency",
    "durable_undo_redo",
    "durable_audit_trail",
    "artifact_passport_bound",
    "independent_media_verification",
    "causal_provenance_verified",
    "lease_fencing_enforced",
    "crash_recovery_reconciler_verified",
    "render_lane_verified",
    "delivery_pack_verified",
    "temporal_rational_math_verified",
    "vertical_slice_real_artifact_verified",
    "board_referee_exit_codes_verified",
    "continuum_contract_resolved",
    "docs_truth_aligned",
    "receipts_chained",
    "subject_and_witness_sha_separated",
    "branch_pushed_and_remote_verified",
}


def _load_gate_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("foundation_gate_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_foundation_gate_evaluates_live_receipt_chain_and_33_booleans() -> None:
    mod = _load_gate_module()
    report = mod.evaluate_foundation_gate(
        REPO_ROOT,
        require_phase8=False,
        require_phase9=False,
    )
    assert report["receipt_chain"]["ok"] is True, report["receipt_chain"]["errors"]
    assert report["all_gates_pass"] is True, report["gates"]
    fg = report["foundation_gate"]
    assert set(fg.keys()) == EXPECTED_FOUNDATION_KEYS
    for key, val in fg.items():
        if key in {"docs_truth_aligned", "branch_pushed_and_remote_verified"}:
            assert isinstance(val, bool)
        else:
            assert val is True, f"expected foundation_gate[{key!r}] to be True, got {val!r}"


def test_receipt_chain_detects_tampered_previous_digest(tmp_path: Path) -> None:
    mod = _load_gate_module()
    src_dir = REPO_ROOT / "docs" / "audits" / "receipts"
    for p in src_dir.glob("*.json"):
        (tmp_path / p.name).write_text(p.read_text(encoding="utf-8"), encoding="utf-8")

    p1_path = tmp_path / "phase1-security.receipt.json"
    p1 = json.loads(p1_path.read_text(encoding="utf-8"))
    p1["previous_receipt_digest"] = "0" * 64
    p1_path.write_text(json.dumps(p1), encoding="utf-8")

    chain = mod.verify_receipt_chain(tmp_path, require_phase8=False, require_phase9=False)
    assert chain["ok"] is False
    assert any("previous_receipt_digest mismatch" in err for err in chain["errors"])
