"""Real-disk artifact export and pipeline handoff (Milestone E).

The lab writes its findings/plan report as a *real* file through the
repository's existing authorized filesystem boundary
(:class:`nexus_ai_agent.tools.filesystem_policy.WorkspaceFilesystem`) and,
where an attempt scope is supplied, through the existing
:class:`nexus_ai_agent.execution.staging.AttemptStaging` publish path. It never
mints a passport, never claims ``AssetVersion``/``Revision`` ownership, and
never dispatches a command.

After writing, :func:`export_report` **re-opens the persisted bytes** and
re-hashes them independently, so a returned digest is a claim about bytes that
were actually read back from disk — not about an in-memory buffer.

The integration handoff is an inert, typed dict: a later, authorized
integration PR maps it onto the CommandBus/rendering contracts. The lab does not
call them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from nexus_ai_agent.execution.staging import AttemptStaging
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

from .analysis import AnalysisReport, report_to_dict

__all__ = ["ExportedArtifact", "export_report", "build_handoff"]

#: A hard cap so a hostile or accidental report cannot fill the disk.
MAX_REPORT_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class ExportedArtifact:
    """Metadata about a report that was written and independently re-read."""

    relative_path: str
    sha256: str
    size_bytes: int
    reopened_sha256: str
    verified: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "reopened_sha256": self.reopened_sha256,
            "verified": self.verified,
        }


def _serialise(report: AnalysisReport) -> bytes:
    payload = report_to_dict(report)
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    raw = data.encode("utf-8")
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError(f"report is {len(raw)} bytes > {MAX_REPORT_BYTES}")
    return raw


def export_report(
    report: AnalysisReport,
    filesystem: WorkspaceFilesystem,
    relative_path: str,
    *,
    staging: AttemptStaging | None = None,
) -> ExportedArtifact:
    """Write the report, re-open it, and verify the digest independently.

    ``relative_path`` must be a safe relative path inside ``filesystem``'s root;
    the filesystem boundary rejects traversal, absolute paths and symlinks.
    When ``staging`` is supplied the bytes are written there first and
    ``publish``-ed into ``relative_path`` (the existing two-phase path).
    """
    raw = _serialise(report)
    expected = hashlib.sha256(raw).hexdigest()

    if staging is not None:
        staged_name = "atlas-report.json"
        staging.prepare()
        staging.write(staged_name, raw)
        published = staging.publish(staged_name, relative_path)
        relative_path = str(published.relative_to(filesystem.root))
    else:
        filesystem.write_bytes(relative_path, raw, create_parents=True)

    # Independent re-read through the boundary.
    reopened = filesystem.read_bytes(relative_path, max_bytes=MAX_REPORT_BYTES)
    reopened_digest = hashlib.sha256(reopened).hexdigest()
    return ExportedArtifact(
        relative_path=relative_path,
        sha256=expected,
        size_bytes=len(raw),
        reopened_sha256=reopened_digest,
        verified=(reopened_digest == expected and reopened == raw),
    )


def build_handoff(report: AnalysisReport, artifact: ExportedArtifact) -> dict[str, Any]:
    """Build an inert, typed handoff for a later authorized integration PR.

    This dict names what an integration layer would consume. The lab itself
    dispatches nothing and holds no authority.
    """
    handoff: dict[str, Any] = {
        "kind": "atlas.edit_plan_handoff",
        "schema_version": 1,
        "report_fingerprint": report.fingerprint,
        "report_status": report.status.value,
        "artifact": artifact.as_dict(),
        "authority": "advisory",  # never authoritative; a consumer must re-check
        "requires_authorization": True,
    }
    if report.plan is not None and report.plan.steps:
        handoff["proposed_steps"] = [
            {
                "candidate_id": step.candidate_id,
                "asset_ref": step.asset_ref,
                "covers": list(step.covers),
                "frames": step.frames,
                "evidence_verified": step.evidence_verified,
            }
            for step in report.plan.steps
        ]
        handoff["plan_status"] = report.plan.status.value
        handoff["plan_optimal"] = report.plan.optimal
    return handoff
