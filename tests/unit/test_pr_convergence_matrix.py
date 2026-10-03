"""Unit tests for the Phase 2 PR & Branch Convergence Matrix (§9)."""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MATRIX_JSON = REPO_ROOT / "docs" / "audits" / "PR_CONVERGENCE_MATRIX_2026-10-03.json"
MATRIX_MD = REPO_ROOT / "docs" / "audits" / "PR_CONVERGENCE_2026-10-03.md"

ALLOWED_DISPOSITIONS = {
    "ABSORBED_INTO_CANONICAL_FOUNDATION",
    "SUPERSEDED_BY_CANONICAL_FOUNDATION",
    "KEEP_OPEN_PENDING_HUMAN_MERGE",
    "CLOSE_RECOMMENDED_DUPLICATE",
    "CLOSE_RECOMMENDED_OBSOLETE",
    "DO_NOT_MERGE_UNSAFE_OR_DRIFTED",
}

MANDATORY_PRIORITY_PRS = {
    # Cluster 1
    148,
    147,
    140,
    130,
    129,
    # Cluster 2
    135,
    128,
    126,
    # Cluster 3
    143,
    136,
    131,
    # Cluster 4
    149,
    146,
    145,
    144,
    142,
    137,
    134,
    127,
    124,
}


def test_convergence_matrix_files_exist_and_cover_all_67_open_prs() -> None:
    assert MATRIX_JSON.is_file()
    assert MATRIX_MD.is_file()
    payload = json.loads(MATRIX_JSON.read_text(encoding="utf-8"))
    assert payload["total_open_prs"] == 67
    prs = payload["prs"]
    assert len(prs) == 67

    pr_numbers = {entry["pr_number"] for entry in prs}
    assert MANDATORY_PRIORITY_PRS.issubset(pr_numbers)
    assert min(pr_numbers) == 33
    assert max(pr_numbers) == 149


def test_every_pr_entry_has_required_fields_and_allowed_disposition() -> None:
    payload = json.loads(MATRIX_JSON.read_text(encoding="utf-8"))
    required_keys = {
        "pr_number",
        "title",
        "branch",
        "head_sha",
        "cluster",
        "unique_value",
        "overlap_conflict_analysis",
        "disposition",
        "exact_files_capabilities_absorbed",
        "exact_reason_if_not_absorbed",
        "required_human_action",
    }
    for entry in payload["prs"]:
        assert required_keys.issubset(entry.keys()), f"PR #{entry.get('pr_number')} missing keys"
        assert entry["disposition"] in ALLOWED_DISPOSITIONS
        assert len(entry["head_sha"]) == 40
        assert entry["unique_value"].strip()
        assert entry["overlap_conflict_analysis"].strip()
        assert entry["required_human_action"].strip()


def test_overwritten_branches_135_and_140_are_explicitly_recovered() -> None:
    payload = json.loads(MATRIX_JSON.read_text(encoding="utf-8"))
    recovered = {item["pr_number"]: item for item in payload["overwritten_branches_recovered"]}
    assert 135 in recovered
    assert 140 in recovered
    assert recovered[135]["recovered_commit_sha"].startswith("788a1db")
    assert recovered[140]["recovered_commit_sha"].startswith("5c9a818")
