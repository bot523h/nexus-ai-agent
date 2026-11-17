"""WAVE 33 — the Pack Coverage Seal.

The pack-coverage measurement decides whether the shipped capability packs meet
their per-pack coverage contract. That measurement is only evidence if it is
*bound*: a number in a log can be edited after the fact, so the seal binds the
whole measurement — the shipped pack set, the per-pack percentages, the
threshold and the provenance — into one canonical, tamper-evident digest.

The seal is the same construction used across PROMETHEUS-ATLAS:

* a canonical (JCS) document of the measurement;
* a Merkle root over one leaf per pack, so a single altered pack percentage
  changes the root;
* a seal digest over the whole document, and a :class:`PackCoverageSeal`
  that can be appended to the Wave 31 self-healing ledger or sealed into the
  Wave 30 holographic vault.

:func:`evaluate_pack_coverage` is the pure decision function: given the shipped
packs, the per-pack coverage and the contract, it returns a
:class:`PackCoverageVerdict` that is ``accepted`` only when every shipped pack
has a manifest, a canonical target and coverage at or above the threshold — and
it reports each reason it refused, never a silent pass.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from nexus_ai_agent.creative.atlas.atlas_spine_governance import jcs_canonical_bytes
from nexus_ai_agent.creative.atlas.holographic_equation_vault import HolographicEquationVault
from nexus_ai_agent.creative.atlas.self_healing_linear_ledger import LinearEvidenceLedger

__all__ = [
    "SEAL_SCHEMA",
    "PACK_MANIFEST",
    "MIN_GOVERNANCE_COVERAGE_THRESHOLD",
    "DEFAULT_COVERAGE_THRESHOLD",
    "PackCoverageError",
    "PackCoverageEntry",
    "PackCoverageVerdict",
    "PackCoverageSeal",
    "evaluate_pack_coverage",
    "seal_pack_coverage",
    "build_entries",
    "entries_from_report",
    "seal_pack_coverage_from_report",
    "measure_pack_coverage_seal",
]

SEAL_SCHEMA: Final[str] = "atlas.pack-coverage-seal/1"
PACK_MANIFEST: Final[str] = "pack.manifest.json"
MIN_GOVERNANCE_COVERAGE_THRESHOLD: Final[float] = 95.0
DEFAULT_COVERAGE_THRESHOLD: Final[float] = 95.0
_MISSING: Final[object] = object()


class PackCoverageError(ValueError):
    """Raised for a malformed coverage measurement."""


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _merkle_root(leaves: Sequence[bytes]) -> str:
    """RFC 6962-style Merkle root over the (already canonical) leaves."""
    if not leaves:
        return _sha256(b"")
    level = [_sha256(b"\x00" + leaf) for leaf in leaves]
    while len(level) > 1:
        nxt: list[str] = []
        for i in range(0, len(level), 2):
            if i + 1 < len(level):
                nxt.append(_sha256(b"\x01" + bytes.fromhex(level[i]) + bytes.fromhex(level[i + 1])))
            else:
                nxt.append(level[i])
        level = nxt
    return level[0]


@dataclass(frozen=True)
class PackCoverageEntry:
    """One shipped pack's measured coverage."""

    pack: str
    coverage_percent: float
    has_manifest: bool
    has_canonical_target: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "pack": self.pack,
            "coverage_percent": round(self.coverage_percent, 4),
            "has_manifest": self.has_manifest,
            "has_canonical_target": self.has_canonical_target,
        }

    def leaf(self) -> bytes:
        return jcs_canonical_bytes(self.to_dict())


@dataclass(frozen=True)
class PackCoverageVerdict:
    """The coverage decision, with every refusal reason preserved."""

    accepted: bool
    threshold: float
    reasons: tuple[str, ...]
    entries: tuple[PackCoverageEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "threshold": self.threshold,
            "reasons": list(self.reasons),
            "entries": [e.to_dict() for e in self.entries],
        }


def _validate_threshold(threshold: object) -> float:
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(float(threshold))
        or float(threshold) < MIN_GOVERNANCE_COVERAGE_THRESHOLD
        or float(threshold) > 100.0
    ):
        raise PackCoverageError("Pack coverage governance threshold must be in [95.0, 100.0]")
    return float(threshold)


def evaluate_pack_coverage(
    entries: Sequence[PackCoverageEntry],
    *,
    threshold: float = DEFAULT_COVERAGE_THRESHOLD,
) -> PackCoverageVerdict:
    """Decide whether the shipped packs meet the coverage contract."""
    validated_threshold = _validate_threshold(threshold)
    reasons: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not entry.pack:
            raise PackCoverageError("every entry needs a non-empty pack name")
        if entry.pack in seen:
            raise PackCoverageError(f"duplicate pack {entry.pack!r}")
        seen.add(entry.pack)
        if not 0.0 <= entry.coverage_percent <= 100.0:
            raise PackCoverageError(
                f"pack {entry.pack!r} coverage {entry.coverage_percent} is not a percentage"
            )
        if not entry.has_manifest:
            reasons.append(f"shipped pack {entry.pack!r} has no {PACK_MANIFEST}")
        if not entry.has_canonical_target:
            reasons.append(f"shipped pack {entry.pack!r} has no canonical test target")
        if entry.coverage_percent < validated_threshold:
            reasons.append(
                f"pack {entry.pack!r}: {entry.coverage_percent:.2f}% < {validated_threshold:.2f}%"
            )
    return PackCoverageVerdict(
        accepted=not reasons,
        threshold=validated_threshold,
        reasons=tuple(reasons),
        entries=tuple(entries),
    )


@dataclass(frozen=True)
class PackCoverageSeal:
    """A canonical, tamper-evident seal over a pack-coverage measurement."""

    verdict: PackCoverageVerdict
    git_commit: str
    schema: str = SEAL_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "git_commit": self.git_commit,
            "accepted": self.verdict.accepted,
            "threshold": self.verdict.threshold,
            "reasons": list(self.verdict.reasons),
            "entries": [e.to_dict() for e in self.verdict.entries],
            "merkle_root": self.merkle_root,
        }

    def canonical_bytes(self) -> bytes:
        return jcs_canonical_bytes(self.to_dict())

    @property
    def merkle_root(self) -> str:
        return _merkle_root([e.leaf() for e in self.verdict.entries])

    @property
    def digest(self) -> str:
        return _sha256(self.canonical_bytes())

    def append_to_ledger(self, ledger: LinearEvidenceLedger) -> None:
        ledger.append(self.canonical_bytes())

    def seal_into_vault(self, vault: HolographicEquationVault) -> None:
        for entry in self.verdict.entries:
            vault.seal(
                f"coverage:{entry.pack}",
                33,
                f"{entry.pack}={entry.coverage_percent:.2f}%",
            )


def seal_pack_coverage(
    entries: Sequence[PackCoverageEntry],
    *,
    git_commit: str,
    threshold: float = DEFAULT_COVERAGE_THRESHOLD,
) -> PackCoverageSeal:
    """Evaluate and seal a pack-coverage measurement in one call."""
    if not git_commit:
        raise PackCoverageError("git_commit must be a non-empty revision")
    verdict = evaluate_pack_coverage(entries, threshold=threshold)
    return PackCoverageSeal(verdict=verdict, git_commit=git_commit)


def build_entries(
    shipped_packs: Sequence[str],
    coverage: Mapping[str, float],
    *,
    manifests: Sequence[str],
    canonical_targets: Sequence[str],
) -> tuple[PackCoverageEntry, ...]:
    """Assemble entries from the four facts the contract pins together.

    A shipped pack with no measurement is recorded at ``0.0`` so the verdict
    refuses it rather than dropping it from the evidence.
    """
    manifest_set = set(manifests)
    target_set = set(canonical_targets)
    entries = [
        PackCoverageEntry(
            pack=pack,
            coverage_percent=float(coverage.get(pack, 0.0)),
            has_manifest=pack in manifest_set,
            has_canonical_target=pack in target_set,
        )
        for pack in sorted(set(shipped_packs))
    ]
    return tuple(entries)


def _shipped_pack_directories(pack_root: Path) -> tuple[str, ...]:
    """Directories directly under ``pack_root`` that ship a ``pack.manifest.json``."""
    return tuple(
        sorted(
            entry.name
            for entry in pack_root.iterdir()
            if entry.is_dir() and (entry / PACK_MANIFEST).is_file()
        )
    )


def _report_field(report: Any, key: str) -> Any:
    if isinstance(report, Mapping):
        return report.get(key, _MISSING)
    return getattr(report, key, _MISSING)


def _report_health_reasons(report: Any, *, expected_commit: str) -> tuple[str, ...]:
    for sha_key in ("commit_sha", "head_sha"):
        sha_val = _report_field(report, sha_key)
        if isinstance(sha_val, str) and sha_val and sha_val != expected_commit:
            raise PackCoverageError(
                f"report {sha_key} {sha_val!r} does not match expected commit {expected_commit!r}"
            )

    reasons: list[str] = []
    verified = _report_field(report, "verified")
    if verified is not _MISSING and bool(verified) is not True:
        reasons.append("report verified flag is False")

    exit_code = _report_field(report, "pytest_exit_code")
    if exit_code is not _MISSING and (isinstance(exit_code, bool) or exit_code != 0):
        reasons.append(f"report pytest_exit_code is {exit_code!r} (expected 0)")

    for count_key in ("failed", "errors"):
        count_val = _report_field(report, count_key)
        if count_val is not _MISSING and (isinstance(count_val, bool) or count_val != 0):
            reasons.append(f"report {count_key} is {count_val!r} (expected 0)")

    issues = _report_field(report, "measurement_issues")
    if issues is not _MISSING and issues is not None and len(issues) > 0:
        reasons.append(f"report measurement_issues is non-empty ({len(issues)})")

    return tuple(reasons)


def entries_from_report(
    report: Any, *, pack_root: Path | None = None
) -> tuple[PackCoverageEntry, ...]:
    """Turn a live :class:`~nexus_ai_agent.continuum.pack_coverage.CoverageReport`
    into seal entries, so the seal reflects a *measured* number, not a claimed one.

    The shipped pack set, the manifest presence and the canonical-target presence
    are read from the real pack tree; the per-pack percentage comes from the
    report. A shipped pack the report did not measure is recorded at ``0.0`` and
    refused by :func:`evaluate_pack_coverage`.
    """
    from nexus_ai_agent.continuum import pack_coverage as continuum

    root = (pack_root or continuum.DEFAULT_PACK_ROOT).resolve()
    shipped = _shipped_pack_directories(root)
    raw_packs = _report_field(report, "packs")
    measured: dict[str, float] = {}
    for pack in raw_packs:
        if isinstance(pack, Mapping):
            measured[str(pack["pack"])] = float(pack["percent"])
        else:
            measured[str(pack.pack)] = float(pack.percent)
    manifests = shipped
    canonical_targets = tuple(continuum.PACK_TEST_TARGETS)
    return build_entries(
        shipped,
        measured,
        manifests=manifests,
        canonical_targets=canonical_targets,
    )


def seal_pack_coverage_from_report(
    report: Any,
    *,
    git_commit: str | None = None,
    commit_sha: str | None = None,
    threshold: float = DEFAULT_COVERAGE_THRESHOLD,
    pack_root: Path | None = None,
) -> PackCoverageSeal:
    """Seal a *measured* coverage report (see :func:`entries_from_report`).

    The report's own acceptance is not trusted: the seal is built from the
    measured per-pack numbers and re-evaluated against the contract, so a report
    that was tampered with (or produced against the wrong tree) cannot yield an
    accepted seal.
    """
    _validate_threshold(threshold)
    if git_commit is not None and commit_sha is not None and git_commit != commit_sha:
        raise PackCoverageError("git_commit and commit_sha disagree")
    commit = commit_sha if commit_sha is not None else git_commit
    if not commit:
        raise PackCoverageError("git_commit must be a non-empty revision")
    health_reasons = _report_health_reasons(report, expected_commit=commit)
    entries = entries_from_report(report, pack_root=pack_root)
    verdict = evaluate_pack_coverage(entries, threshold=threshold)
    if health_reasons:
        verdict = PackCoverageVerdict(
            accepted=False,
            threshold=verdict.threshold,
            reasons=verdict.reasons + health_reasons,
            entries=verdict.entries,
        )
    return PackCoverageSeal(verdict=verdict, git_commit=commit)


def measure_pack_coverage_seal(
    *,
    git_commit: str | None = None,
    threshold: float = DEFAULT_COVERAGE_THRESHOLD,
    pack_root: Path | None = None,
    tests: Sequence[str] | None = None,
    packs: Sequence[str] | None = None,
    runner: Callable[..., Any] | None = None,
) -> PackCoverageSeal:
    """Run a real pack-coverage measurement and seal the result.

    ``runner`` defaults to :func:`nexus_ai_agent.continuum.pack_coverage.measure`
    (which runs pytest in an isolated child process); tests inject a runner so the
    sealing path is exercised without spawning a subprocess. The commit defaults
    to the live ``HEAD`` of the repository.
    """
    from nexus_ai_agent.continuum import pack_coverage as continuum

    root = (pack_root or continuum.DEFAULT_PACK_ROOT).resolve()
    measure = runner or continuum.measure
    kwargs: dict[str, Any] = {"pack_root": root, "threshold": threshold}
    if tests is not None:
        kwargs["tests"] = list(tests)
    if packs is not None:
        kwargs["packs"] = list(packs)
    report = measure(**kwargs)
    commit = git_commit
    if commit is None:
        provenance = getattr(report, "provenance", None)
        commit = getattr(provenance, "git_commit", None)
    if not commit:
        raise PackCoverageError("the measurement is not bound to a commit")
    return seal_pack_coverage_from_report(
        report, git_commit=str(commit), threshold=threshold, pack_root=root
    )
