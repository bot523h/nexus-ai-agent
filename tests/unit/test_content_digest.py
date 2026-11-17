"""Content-level restore proof — the digests the DR drill compares.

A row count is satisfied by a restore that kept every header and lost every
body, so the drill hashes the ordered query output on both sides. These tests
pin the two properties that make that comparison meaningful: order comes from
the query (not from physical row order) and the digest is over content.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from nexus_ai_agent.maintenance.content_digest import (
    ContentDigestProof,
    content_digest_proof,
    content_evidence,
    ordered_content_sha256,
)

QUERY = "SELECT id, name FROM rows ORDER BY id"


def _runner(text: str):
    seen: list[str] = []

    def run(sql: str) -> str:
        seen.append(sql)
        return text

    run.seen = seen  # type: ignore[attr-defined]
    return run


def test_identical_content_matches_and_reports_the_digest() -> None:
    text = "1|row-1\n2|row-2\n"
    proof = content_digest_proof(QUERY, source=_runner(text), restored=_runner(text))
    assert proof.matches
    assert proof.source_sha256 == hashlib.sha256(text.strip().encode()).hexdigest()
    payload = proof.to_dict()
    assert payload["content_digest_matches"] is True
    assert payload["source_content_sha256"] == payload["restored_content_sha256"]
    assert payload["sql"] == QUERY


def test_trailing_formatting_whitespace_is_not_content() -> None:
    """`psql --tuples-only` appends a newline; it must not count as a difference."""
    assert ordered_content_sha256(_runner("1|a\n2|b\n\n"), QUERY) == ordered_content_sha256(
        _runner("1|a\n2|b"), QUERY
    )


def test_reordered_or_altered_rows_do_not_match() -> None:
    proof = content_digest_proof(
        QUERY,
        source=_runner("1|row-1\n2|row-2"),
        restored=_runner("2|row-2\n1|row-1"),
    )
    assert not proof.matches
    assert proof.to_dict()["content_digest_matches"] is False


def test_a_single_changed_byte_changes_the_digest() -> None:
    proof = content_digest_proof(QUERY, source=_runner("1|row-1"), restored=_runner("1|row-l"))
    assert not proof.matches


def test_the_same_query_is_run_on_both_sides() -> None:
    """A drift between the two queries would compare different relations."""
    source = _runner("1|a")
    restored = _runner("1|a")
    content_digest_proof(QUERY, source=source, restored=restored)
    assert source.seen == [QUERY]  # type: ignore[attr-defined]
    assert restored.seen == [QUERY]  # type: ignore[attr-defined]


def test_proof_is_immutable_and_self_consistent() -> None:
    proof = ContentDigestProof(sql=QUERY, source_sha256="a", restored_sha256="a")
    assert proof.matches is True
    assert proof.matches == (proof.source_sha256 == proof.restored_sha256)


# --------------------------------------------------------------------------- #
# the evidence bundle reads those proofs, and refuses a stale one
# --------------------------------------------------------------------------- #


def _benchmark(out: Path, **overrides: Any) -> None:
    payload = {
        "schema": "nexus-p0-benchmark/v1",
        "generated_at": "2026-01-01T00:00:10+00:00",
        "rows": 20000,
        "restored_rows": 20000,
        "source_content_sha256": "aa",
        "restored_content_sha256": "aa",
        "content_digest_matches": True,
    }
    payload.update(overrides)
    (out / "benchmark.json").write_text(json.dumps(payload))


def test_evidence_accepts_a_fresh_content_matching_benchmark(tmp_path: Path) -> None:
    _benchmark(tmp_path)
    result = content_evidence(
        tmp_path,
        dr_check={"stdout_tail": ["DR-PROOF-OK: … content_digest_matches=true"]},
        not_before="2026-01-01T00:00:00+00:00",
    )
    assert result["all_content_matches"] is True
    assert result["reasons"] == []
    assert result["benchmark"]["source_content_sha256"] == "aa"


def test_evidence_refuses_a_stale_benchmark(tmp_path: Path) -> None:
    """An old file under a zero exit code would be published as this run's proof."""
    _benchmark(tmp_path, generated_at="2025-12-31T23:59:59+00:00")
    result = content_evidence(
        tmp_path,
        dr_check={"stdout_tail": ["DR-PROOF-OK: … content_digest_matches=true"]},
        not_before="2026-01-01T00:00:00+00:00",
    )
    assert result["all_content_matches"] is False
    assert any("older than this run" in reason for reason in result["reasons"])


def test_evidence_refuses_a_mismatch_or_a_missing_proof(tmp_path: Path) -> None:
    _benchmark(tmp_path, content_digest_matches=False, restored_content_sha256="bb")
    result = content_evidence(tmp_path, dr_check={"stdout_tail": []}, not_before="2026-01-01")
    assert result["all_content_matches"] is False
    assert any("does not report content_digest_matches" in r for r in result["reasons"])
    assert any("dr_proof did not report" in r for r in result["reasons"])


def test_evidence_refuses_a_missing_or_broken_benchmark(tmp_path: Path) -> None:
    result = content_evidence(
        tmp_path,
        dr_check={"stdout_tail": ["content_digest_matches=true"]},
        not_before="2026-01-01",
    )
    assert "benchmark.json is missing" in result["reasons"]

    (tmp_path / "benchmark.json").write_text("{not json")
    result = content_evidence(
        tmp_path,
        dr_check={"stdout_tail": ["content_digest_matches=true"]},
        not_before="2026-01-01",
    )
    assert any("not JSON" in reason for reason in result["reasons"])
    assert result["benchmark"] is None


def test_the_scripts_publish_the_flag_the_bundle_reads() -> None:
    """The two drills and the bundle must not drift into different literals."""
    root = Path(__file__).resolve().parents[2]
    bench = (root / "scripts" / "p0_benchmark.py").read_text()
    dr = (root / "scripts" / "dr_proof.py").read_text()
    evidence = (root / "scripts" / "p0_evidence.py").read_text()
    digests = (root / "src" / "nexus_ai_agent" / "maintenance" / "content_digest.py").read_text()
    assert "content_digest_proof" in bench and "content_digest_matches" in bench
    assert "content_digest_matches=true" in dr
    assert "content_digest_matches=true" in digests
    # The bundle delegates the parse to the package (tests/ may not import scripts/).
    assert "content_evidence" in evidence
    assert "from nexus_ai_agent.maintenance.content_digest import content_evidence" in evidence
