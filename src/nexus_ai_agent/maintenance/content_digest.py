"""Content-level proof for a backup/restore drill.

A row *count* is the weakest possible claim about a restore. It is satisfied by
a dump that kept every row header and lost every byte of text, and by a restore
that shuffled the rows. The drill in ``scripts/dr_proof.py`` and
``scripts/p0_benchmark.py`` therefore also hashes the ordered result of a fixed
query on both sides of the dump and compares the two digests — the same idea the
rest of this repository applies to artifacts (a content address, not a size).

The digest is defined here, in one place, so the two scripts cannot drift into
two different notions of "the same data":

* the query must carry its own ``ORDER BY`` — an unordered ``SELECT`` has no
  canonical text form, and a restore that reinserted the same rows in another
  physical order must still compare equal;
* the hash covers the *text* psql returns for that query, which is exactly what
  an operator sees when they check by hand.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Runs one ``sql`` statement against a database and returns its text output.
#: The scripts bind this to their own ``psql`` helper.
SqlRunner = Callable[[str], str]


@dataclass(frozen=True)
class ContentDigestProof:
    """The two digests and whether they agree."""

    sql: str
    source_sha256: str
    restored_sha256: str

    @property
    def matches(self) -> bool:
        return self.source_sha256 == self.restored_sha256

    def to_dict(self) -> dict[str, Any]:
        return {
            "sql": " ".join(self.sql.split()),
            "source_content_sha256": self.source_sha256,
            "restored_content_sha256": self.restored_sha256,
            "content_digest_matches": self.matches,
        }


def ordered_content_sha256(runner: SqlRunner, sql: str) -> str:
    """SHA-256 of the ordered row text ``sql`` returns.

    Trailing whitespace is stripped on both sides: ``psql --tuples-only`` ends
    its output with a newline, and that newline is formatting, not content.
    """
    return hashlib.sha256(runner(sql).strip().encode("utf-8")).hexdigest()


def content_digest_proof(sql: str, *, source: SqlRunner, restored: SqlRunner) -> ContentDigestProof:
    """Hash ``sql`` on both sides of the restore and report whether they agree.

    ``sql`` is executed verbatim on both databases; it must be a read-only query
    with an explicit ``ORDER BY`` (see the module docstring).
    """
    return ContentDigestProof(
        sql=sql,
        source_sha256=ordered_content_sha256(source, sql),
        restored_sha256=ordered_content_sha256(restored, sql),
    )


def content_evidence(out_dir: Path, *, dr_check: dict[str, Any], not_before: str) -> dict[str, Any]:
    """Read the content-level proofs the two drill scripts just produced.

    An exit code of 0 already implies both digests matched, but a bundle is read
    by humans: the actual hashes belong in it. It also closes a real hole — a
    ``benchmark.json`` left behind by an earlier run would otherwise be published
    as this revision's evidence under a zero exit code, so the recorded
    ``generated_at`` has to be at or after this bundle started.

    This lives in the package rather than next to the script because
    ``tests/`` may not import the unpackaged ``scripts/`` namespace
    (``tests/architecture/test_scripts_import_boundary.py``), and the behaviour
    here is exactly the kind that deserves a test.
    """
    reasons: list[str] = []
    bench_path = out_dir / "benchmark.json"
    benchmark: dict[str, Any] | None = None
    if not bench_path.is_file():
        reasons.append("benchmark.json is missing")
    else:
        try:
            loaded = json.loads(bench_path.read_text())
        except json.JSONDecodeError as exc:
            loaded = None
            reasons.append(f"benchmark.json is not JSON: {exc}")
        if isinstance(loaded, dict):
            benchmark = {
                "rows": loaded.get("rows"),
                "restored_rows": loaded.get("restored_rows"),
                "source_content_sha256": loaded.get("source_content_sha256"),
                "restored_content_sha256": loaded.get("restored_content_sha256"),
                "content_digest_matches": loaded.get("content_digest_matches"),
                "generated_at": loaded.get("generated_at"),
            }
            if loaded.get("content_digest_matches") is not True:
                reasons.append("benchmark.json does not report content_digest_matches")
            if str(loaded.get("generated_at") or "") < not_before:
                reasons.append(
                    "benchmark.json is older than this run "
                    f"({loaded.get('generated_at')} < {not_before})"
                )
        elif loaded is not None:
            reasons.append("benchmark.json is not an object")

    dr_stdout = " ".join(str(line) for line in dr_check.get("stdout_tail") or [])
    dr_matches = "content_digest_matches=true" in dr_stdout
    if not dr_matches:
        reasons.append("dr_proof did not report content_digest_matches=true")

    return {
        "benchmark": benchmark,
        "dr_proof_content_digest_matches": dr_matches,
        "all_content_matches": not reasons,
        "reasons": reasons,
    }


__all__ = [
    "ContentDigestProof",
    "SqlRunner",
    "content_digest_proof",
    "content_evidence",
    "ordered_content_sha256",
]
