#!/usr/bin/env python3
"""PR convergence: discover, classify, correlate, rank, recommend — never mutate.

Fifty open pull requests and sixty ``arena/*`` branches are not a plan; they are
a queue nobody can read.  This tool turns that queue into a **deterministic
convergence graph** so an owner can decide, instead of guessing.

Hard rule — this tool is read-only.  It never merges, closes, deletes, retargets
or comments.  Every mutation stays with the repository owner.  The verbs here
are: DISCOVER, CLASSIFY, CORRELATE, DETECT, RANK, RECOMMEND.

It is also deliberately **not** a second coordinator.  The board, its leases and
the open-PR transport stay in ``scripts/agent_board.py``; this module imports
them.  What is new is the analysis: duplicate detection with proof and
confidence, explicit supersession edges, a deterministic dependency graph,
per-PR evidence freshness, a composite staleness signal, and a recommended
(never enforced) merge order.

Determinism is the contract: the same inputs always produce the same report,
byte for byte.  No LLM, no wall-clock reading inside the analysis (``now`` is an
argument), no set iteration in output order.  A merge order decided at runtime
by a model would not be auditable; this one is a pure function of the evidence.

Exit codes: ``0`` report produced; ``1`` a ``--fail-on`` threshold tripped;
``2`` a source was unreadable — which is never a report.

Persian note: این ابزار فقط مشاهده و توصیه می‌کند؛ هیچ PR را merge/close/delete
نمی‌کند. ترتیب پیشنهادی deterministic است، نه تصمیم یک مدل در runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The board and its GitHub transport are the existing authorities.  Importing
# them (rather than re-implementing a fetcher) is what keeps this from becoming
# a second coordinator.
from agent_board import (  # noqa: E402
    _claim_live,
    _gh_get,
    _parse,
    gc_expired,
    load_board,
)

#: naive/aware mixing is a TypeError, so every "unknown time" uses one aware sentinel
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

EXIT_OK = 0
EXIT_FAIL_ON = 1
EXIT_BLOCKED = 2

CLASSES = (
    "CANONICAL",
    "DUPLICATE",
    "SUPERSEDING",
    "DEPENDENT",
    "BLOCKED",
    "STALE",
    "ORPHANED",
    "ACTIVE",
)

RELATIONS = ("conflicts_with", "duplicates", "supersedes", "depends_on", "blocks", "unlocks")


# --------------------------------------------------------------------------- #
# inputs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Thresholds:
    """Every staleness/duplication knob, in one place and overridable.

    Thresholds are parameters, not magic numbers buried in comparisons: the
    repository's semantics decide them, and a report always carries the values
    it was produced with.
    """

    stale_age_days: int = 14
    inactive_days: int = 7
    duplicate_jaccard: float = 0.75
    duplicate_min_shared_files: int = 3
    duplicate_confidence: float = 0.60
    supersede_containment: float = 1.0
    title_token_overlap: float = 0.60
    objective_min_files: int = 5
    objective_min_share: float = 0.70

    def to_dict(self) -> dict[str, float]:
        return {
            "stale_age_days": self.stale_age_days,
            "inactive_days": self.inactive_days,
            "duplicate_jaccard": self.duplicate_jaccard,
            "duplicate_min_shared_files": self.duplicate_min_shared_files,
            "duplicate_confidence": self.duplicate_confidence,
            "supersede_containment": self.supersede_containment,
            "title_token_overlap": self.title_token_overlap,
            "objective_min_files": self.objective_min_files,
            "objective_min_share": self.objective_min_share,
        }


@dataclass(frozen=True)
class PR:
    """The evidence known about one open pull request."""

    number: int
    title: str
    head_branch: str
    head_sha: str
    base_ref: str
    base_sha: str
    created_at: datetime | None
    updated_at: datetime | None
    files: tuple[str, ...]
    files_complete: bool
    draft: bool = False
    labels: tuple[str, ...] = ()

    @property
    def file_set(self) -> frozenset[str]:
        return frozenset(self.files)

    def missing_evidence(self) -> list[str]:
        gaps: list[str] = []
        if not self.head_sha:
            gaps.append("head_sha")
        if not self.base_sha:
            gaps.append("base_sha")
        if not self.head_branch:
            gaps.append("head_branch")
        if not self.files_complete:
            gaps.append("changed-file list is truncated")
        if not self.files:
            gaps.append("changed-file list is empty")
        return gaps


def _dt(value: str | None) -> datetime | None:
    """Parse a UTC timestamp.  Unparsable stays ``None`` — never guessed."""
    if not value:
        return None
    parsed = _parse(value)
    if parsed is not None:
        return parsed.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def pr_from_record(record: dict) -> PR:
    """Build a PR from a GitHub REST record or an offline fixture.

    Unknown fields stay unknown (empty string / None) — never invented, because
    an invented SHA would make stale evidence look fresh.
    """
    files: list[str] = []
    for entry in record.get("files") or []:
        if isinstance(entry, str):
            files.append(entry)
        elif isinstance(entry, dict) and entry.get("filename"):
            files.append(str(entry["filename"]))
    complete = record.get("files_complete")
    if complete is None:
        complete = not record.get("files_truncated", False)
    return PR(
        number=int(record["number"]),
        title=str(record.get("title") or ""),
        head_branch=str(record.get("head_branch") or ""),
        head_sha=str(record.get("head_sha") or ""),
        base_ref=str(record.get("base_ref") or ""),
        base_sha=str(record.get("base_sha") or ""),
        created_at=_dt(record.get("created_at")),
        updated_at=_dt(record.get("updated_at")),
        files=tuple(sorted(files)),
        files_complete=bool(complete),
        draft=bool(record.get("draft", False)),
        labels=tuple(sorted(record.get("labels") or [])),
    )


# --------------------------------------------------------------------------- #
# board correlation
# --------------------------------------------------------------------------- #


@dataclass
class BoardIndex:
    """Live board claims indexed by branch and by task."""

    by_branch: dict[str, list[dict]] = field(default_factory=dict)
    by_task: dict[str, dict] = field(default_factory=dict)
    claims: list[dict] = field(default_factory=list)

    @classmethod
    def build(cls, board: dict) -> BoardIndex:
        index = cls(claims=list(board.get("claims", [])))
        for claim in index.claims:
            task = claim.get("task")
            if task:
                index.by_task[str(task)] = claim
            branch = claim.get("agent_branch")
            if branch and _claim_live(claim):
                index.by_branch.setdefault(str(branch), []).append(claim)
        return index

    def live_claims_for(self, branch: str) -> list[dict]:
        return self.by_branch.get(branch, [])

    def any_claim_mentioning(self, needle: str) -> list[dict]:
        if not needle:
            return []
        hits = []
        for claim in self.claims:
            blob = json.dumps(claim, ensure_ascii=False)
            if needle in blob:
                hits.append(claim)
        return hits


# --------------------------------------------------------------------------- #
# evidence freshness
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Freshness:
    verdict: str  # FRESH | PARTIAL | UNVERIFIABLE
    base_drift: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict,
            "base_drift": self.base_drift,
            "reasons": list(self.reasons),
        }


def assess_freshness(pr: PR, live_main_sha: str | None, now: datetime, th: Thresholds) -> Freshness:
    """Is this PR's evidence about the commit it is actually at?

    Evidence recorded against an old SHA must never be generalised to a new
    head, so every input the analysis needs is checked for presence *and*
    agreement with the live base before any conclusion is drawn from it.
    """
    reasons: list[str] = []
    gaps = pr.missing_evidence()
    if gaps:
        reasons.extend(f"missing {gap}" for gap in gaps)

    base_drift = False
    if pr.base_sha and live_main_sha:
        base_drift = pr.base_sha != live_main_sha
        if base_drift:
            reasons.append(
                f"base_sha {pr.base_sha[:10]} != live main {live_main_sha[:10]} (base drift)"
            )
    elif not live_main_sha:
        reasons.append("live main head was not supplied, so drift cannot be measured")

    if pr.updated_at and (now - pr.updated_at) > timedelta(days=th.inactive_days):
        reasons.append(f"no activity for {(now - pr.updated_at).days}d")

    if gaps and any("truncated" in g or "empty" in g for g in gaps):
        return Freshness("UNVERIFIABLE", base_drift, tuple(reasons))
    if reasons:
        return Freshness("PARTIAL", base_drift, tuple(reasons))
    return Freshness("FRESH", base_drift, ())


# --------------------------------------------------------------------------- #
# duplicate detection — proof, confidence, witnesses
# --------------------------------------------------------------------------- #

_TITLE_TOKENS = re.compile(r"[a-z0-9_]{4,}")


def _title_tokens(title: str) -> frozenset[str]:
    return frozenset(_TITLE_TOKENS.findall(title.lower()))


@dataclass(frozen=True)
class DuplicateEvidence:
    a: int
    b: int
    confidence: float
    witnesses: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "a": self.a,
            "b": self.b,
            "confidence": round(self.confidence, 3),
            "witnesses": list(self.witnesses),
        }


def _shared_objective(paths: frozenset[str], th: Thresholds) -> str | None:
    """The subsystem both PRs are editing, when they clearly share one.

    ``.agents/board.json`` + two docs files is coordination noise, not a shared
    architectural objective, so this needs a real subtree concentration.
    """
    if len(paths) < th.objective_min_files:
        return None
    counts: dict[str, int] = {}
    for path in paths:
        parts = path.split("/")
        if len(parts) < 4:
            continue
        prefix = "/".join(parts[:3])
        counts[prefix] = counts.get(prefix, 0) + 1
    if not counts:
        return None
    prefix, hits = max(sorted(counts.items()), key=lambda kv: (kv[1], kv[0]))
    if hits / len(paths) >= th.objective_min_share:
        return f"{prefix} ({hits}/{len(paths)} shared paths)"
    return None


def duplicate_evidence(a: PR, b: PR, index: BoardIndex, th: Thresholds) -> DuplicateEvidence | None:
    """Score two open PRs as duplicates, or return None below the floor.

    A title is not proof.  Every signal contributes a witness string carrying
    the measured value, so a reviewer can re-derive the confidence by hand.
    The weights are deliberately additive and capped at 1.0; the file-set
    signals are tiered so that a near-identical change set can carry the
    verdict on its own, while incidental coordination-file overlap cannot.
    """
    shared = a.file_set & b.file_set
    union = a.file_set | b.file_set
    jaccard = (len(shared) / len(union)) if union else 0.0

    witnesses: list[str] = []
    confidence = 0.0

    # S1 — the same commits, pushed to two branches.
    if a.head_sha and a.head_sha == b.head_sha:
        confidence += 1.0
        witnesses.append(f"S1 identical head_sha {a.head_sha[:10]} (same commits)")

    # S2 — change-set overlap, tiered.
    if jaccard >= 0.90 and len(shared) >= 5:
        confidence += 0.65
        witnesses.append(
            f"S2 near-identical change set jaccard={jaccard:.2f} shared={len(shared)} "
            f"union={len(union)}"
        )
    elif jaccard >= th.duplicate_jaccard and len(shared) >= th.duplicate_min_shared_files:
        confidence += 0.50
        witnesses.append(
            f"S2 strong file overlap jaccard={jaccard:.2f} shared={len(shared)} union={len(union)}"
        )
    elif shared:
        witnesses.append(
            f"S2 file overlap below floor jaccard={jaccard:.2f} shared={len(shared)} "
            f"(needs jaccard >= {th.duplicate_jaccard} and >= {th.duplicate_min_shared_files} "
            "files for any weight)"
        )

    # S3/S4 — the board says these two branches are the same work.
    a_claims = index.live_claims_for(a.head_branch)
    b_claims = index.live_claims_for(b.head_branch)
    a_tasks = {c.get("task") for c in a_claims}
    b_tasks = {c.get("task") for c in b_claims}
    if a_tasks and a_tasks & b_tasks:
        confidence += 0.30
        witnesses.append(f"S3 same live board task {sorted(a_tasks & b_tasks)}")

    a_zones = {c.get("zone") for c in a_claims}
    b_zones = {c.get("zone") for c in b_claims}
    if a_zones and a_zones & b_zones:
        confidence += 0.15
        witnesses.append(f"S4 same live board zone {sorted(a_zones & b_zones)}")

    # S5 — the same architectural objective, measured as subtree concentration.
    objective = _shared_objective(shared, th)
    if objective:
        confidence += 0.20
        witnesses.append(f"S5 same architectural objective: {objective}")

    # S6 — corroborating only; never decisive on its own.
    ta, tb = _title_tokens(a.title), _title_tokens(b.title)
    token_overlap = (len(ta & tb) / len(ta | tb)) if (ta | tb) else 0.0
    if token_overlap >= th.title_token_overlap:
        confidence += 0.10
        witnesses.append(f"S6 title token overlap={token_overlap:.2f}")

    if confidence < th.duplicate_confidence:
        return None
    return DuplicateEvidence(a.number, b.number, min(confidence, 1.0), tuple(witnesses))


def _union_groups(pairs: list[tuple[int, int]]) -> list[list[int]]:
    """Deterministic union-find over duplicate pairs."""
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            # determinism: the smaller root always wins, independent of pair order
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb
    groups: dict[int, list[int]] = {}
    for node in sorted(parent):
        groups.setdefault(find(node), []).append(node)
    return [sorted(members) for _, members in sorted(groups.items())]


# --------------------------------------------------------------------------- #
# supersession
# --------------------------------------------------------------------------- #

_SUPERSEDED_BY = re.compile(r"superseded_by_pr(\d+)", re.IGNORECASE)


def supersession_edges(
    prs: dict[int, PR], index: BoardIndex, th: Thresholds
) -> list[dict[str, object]]:
    """Explicit and derived supersession relationships.

    Recorded as a *relationship*, never as an action: the superseded PR stays
    open.  Only the owner closes it.
    """
    edges: list[dict[str, object]] = []

    # 1. explicit, from the board: a claim whose status names the PR that
    #    replaced it, or a `supersedes` field.
    for claim in index.claims:
        status = str(claim.get("status") or "")
        match = _SUPERSEDED_BY.search(status)
        if match:
            edges.append(
                {
                    "relation": "supersedes",
                    "from": int(match.group(1)),
                    "to": None,
                    "superseded_task": claim.get("task"),
                    "confidence": 1.0,
                    "witnesses": [
                        f"board claim {claim.get('task')!r} status={status!r} names the winner"
                    ],
                    "source": "board",
                }
            )
        for target in claim.get("supersedes") or []:
            edges.append(
                {
                    "relation": "supersedes",
                    "from": None,
                    "to": target,
                    "superseded_task": claim.get("task"),
                    "confidence": 1.0,
                    "witnesses": [
                        f"board claim {claim.get('task')!r} declares supersedes={target}"
                    ],
                    "source": "board",
                }
            )

    # 2. derived: same zone, the newer PR's change set contains the older one's.
    numbers = sorted(prs)
    for i, na in enumerate(numbers):
        for nb in numbers[i + 1 :]:
            a, b = prs[na], prs[nb]
            older, newer = (
                (a, b) if (a.updated_at or _EPOCH) <= (b.updated_at or _EPOCH) else (b, a)
            )
            if not older.file_set:
                continue
            contained = older.file_set <= newer.file_set
            containment = len(older.file_set & newer.file_set) / len(older.file_set)
            if not (contained and containment >= th.supersede_containment):
                continue
            if older.head_sha == newer.head_sha:
                continue  # identical commits are a duplicate, not a supersession
            za = {c.get("zone") for c in index.live_claims_for(older.head_branch)}
            zb = {c.get("zone") for c in index.live_claims_for(newer.head_branch)}
            witnesses = [
                f"PR#{older.number} files ({len(older.file_set)}) are a subset of "
                f"PR#{newer.number} files ({len(newer.file_set)}); containment={containment:.2f}"
            ]
            if za and za & zb:
                witnesses.append(f"same live board zone {sorted(za & zb)}")
            edges.append(
                {
                    "relation": "supersedes",
                    "from": newer.number,
                    "to": older.number,
                    "superseded_task": None,
                    "confidence": 0.8 if (za and za & zb) else 0.6,
                    "witnesses": witnesses,
                    "source": "derived",
                }
            )
    return edges


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #


@dataclass
class Assessment:
    pr: PR
    freshness: Freshness
    primary: str
    signals: list[str] = field(default_factory=list)
    board_tasks: list[str] = field(default_factory=list)
    board_zones: list[str] = field(default_factory=list)
    age_days: int | None = None
    inactive_days: int | None = None
    conflicts: list[int] = field(default_factory=list)
    duplicate_of: int | None = None
    supersedes: list[int] = field(default_factory=list)
    superseded_by: int | None = None
    canonical_rank: tuple[object, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "number": self.pr.number,
            "title": self.pr.title,
            "head_branch": self.pr.head_branch,
            "head_sha": self.pr.head_sha,
            "base_ref": self.pr.base_ref,
            "base_sha": self.pr.base_sha,
            "class": self.primary,
            "signals": self.signals,
            "evidence": self.freshness.to_dict(),
            "board_tasks": self.board_tasks,
            "board_zones": self.board_zones,
            "age_days": self.age_days,
            "inactive_days": self.inactive_days,
            "conflicts_with": self.conflicts,
            "duplicate_of": self.duplicate_of,
            "supersedes": self.supersedes,
            "superseded_by": self.superseded_by,
            "file_count": len(self.pr.files),
        }


def _canonical_rank_key(a: Assessment) -> tuple[object, ...]:
    """Deterministic survivor ranking inside a duplicate cluster.

    Sorted ascending, so the *smallest* tuple wins.  Order of preference:
    board-claimed > evidence fresh > no base drift > newer > larger > lower
    number.  Every component is a measured value, so the choice is reproducible
    and explainable.
    """
    return (
        0 if a.board_tasks else 1,
        0 if a.freshness.verdict == "FRESH" else (1 if a.freshness.verdict == "PARTIAL" else 2),
        1 if a.freshness.base_drift else 0,
        -(a.pr.updated_at.timestamp() if a.pr.updated_at else 0.0),
        -len(a.pr.files),
        a.pr.number,
    )


def _is_stale(a: Assessment, th: Thresholds, has_newer_conflict: bool) -> bool:
    """Stale is a *composite* signal, and inactivity is a necessary part of it.

    Semantics, chosen from this repository's measured shape rather than
    hard-coded blindly: with 49 open PRs across 13 distinct base SHAs, "base
    drifted" and "a newer PR touches the same file" are each true for most of
    the queue, so either alone would retire the whole board.  Age alone never
    retires a PR either — an old PR that is still being updated is alive.

    A PR is stale when it has been inactive for ``inactive_days`` **and** at
    least one corroborating signal holds (old, base drifted, a newer PR
    conflicts).  An explicit supersession is decisive on its own, because that
    is a recorded fact rather than an inference.
    """
    if a.superseded_by is not None:
        return True
    if a.inactive_days is None or a.inactive_days < th.inactive_days:
        return False
    corroboration = sum(
        (
            a.age_days is not None and a.age_days >= th.stale_age_days,
            a.freshness.base_drift,
            has_newer_conflict,
        )
    )
    return corroboration >= 1


# --------------------------------------------------------------------------- #
# the analysis
# --------------------------------------------------------------------------- #


def analyze(
    prs: list[PR],
    board: dict,
    *,
    live_main_sha: str | None = None,
    now: datetime | None = None,
    thresholds: Thresholds | None = None,
) -> dict[str, object]:
    """The whole convergence analysis, as a pure function of its inputs."""
    th = thresholds or Thresholds()
    moment = now or datetime.now(timezone.utc)
    index = BoardIndex.build(board)
    by_number = {pr.number: pr for pr in prs}
    numbers = sorted(by_number)

    if len(by_number) != len(prs):
        raise ValueError("duplicate PR numbers in the input")

    assessments: dict[int, Assessment] = {}
    for number in numbers:
        pr = by_number[number]
        freshness = assess_freshness(pr, live_main_sha, moment, th)
        claims = index.live_claims_for(pr.head_branch)
        a = Assessment(pr=pr, freshness=freshness, primary="ACTIVE")
        a.board_tasks = sorted({str(c.get("task")) for c in claims if c.get("task")})
        a.board_zones = sorted({str(c.get("zone")) for c in claims if c.get("zone")})
        if pr.created_at:
            a.age_days = (moment - pr.created_at).days
        if pr.updated_at:
            a.inactive_days = (moment - pr.updated_at).days
        assessments[number] = a

    # ---- conflicts: shared files between two open PRs --------------------- #
    for i, na in enumerate(numbers):
        for nb in numbers[i + 1 :]:
            shared = by_number[na].file_set & by_number[nb].file_set
            if shared:
                assessments[na].conflicts.append(nb)
                assessments[nb].conflicts.append(na)

    # ---- duplicates ------------------------------------------------------- #
    dup_edges: list[DuplicateEvidence] = []
    for i, na in enumerate(numbers):
        for nb in numbers[i + 1 :]:
            evidence = duplicate_evidence(by_number[na], by_number[nb], index, th)
            if evidence:
                dup_edges.append(evidence)
    clusters = _union_groups([(e.a, e.b) for e in dup_edges])
    cluster_of: dict[int, int] = {}
    canonical_of: dict[int, int] = {}
    for members in clusters:
        if len(members) < 2:
            continue
        ranked = sorted((assessments[m] for m in members), key=_canonical_rank_key)
        winner = ranked[0]
        for member in members:
            cluster_of[member] = winner.pr.number
        canonical_of[winner.pr.number] = winner.pr.number
        for loser in ranked[1:]:
            loser.duplicate_of = winner.pr.number

    # ---- supersession ----------------------------------------------------- #
    sup_edges = supersession_edges(by_number, index, th)
    for edge in sup_edges:
        src, dst = edge.get("from"), edge.get("to")
        if isinstance(src, int) and src in assessments:
            if isinstance(dst, int) and dst not in assessments[src].supersedes:
                assessments[src].supersedes.append(dst)
        if isinstance(dst, int) and dst in assessments and isinstance(src, int):
            assessments[dst].superseded_by = src

    # ---- dependency edges from the board ---------------------------------- #
    dep_edges: list[dict[str, object]] = []
    branch_of_task: dict[str, int] = {}
    for number in numbers:
        for task in assessments[number].board_tasks:
            branch_of_task.setdefault(task, number)
    for task, number in sorted(branch_of_task.items()):
        claim = index.by_task.get(task, {})
        for prereq in claim.get("prerequisites") or []:
            if prereq in branch_of_task:
                dep_edges.append(
                    {
                        "relation": "depends_on",
                        "from": number,
                        "to": branch_of_task[prereq],
                        "witnesses": [f"board task {task!r} prerequisites include {prereq!r}"],
                        "source": "board",
                    }
                )

    # ---- classification --------------------------------------------------- #
    # Every matched condition is recorded as a signal; only the *primary* class
    # uses precedence, so a PR that is both orphaned and superseded reports both
    # facts instead of hiding one behind the other.
    for number in numbers:
        a = assessments[number]
        gaps = a.pr.missing_evidence()
        has_newer_conflict = any(
            (by_number[c].updated_at or _EPOCH) > (a.pr.updated_at or _EPOCH) for c in a.conflicts
        )
        unowned = not a.board_tasks and not index.any_claim_mentioning(f"#{number}")
        blocked = a.freshness.verdict == "UNVERIFIABLE" or (bool(gaps) and not a.board_tasks)
        is_cluster_winner = number in canonical_of
        stale = _is_stale(a, th, has_newer_conflict)

        if blocked:
            a.signals.append("evidence incomplete: " + ", ".join(gaps or ["unverifiable"]))
        if unowned:
            a.signals.append(f"no live board claim owns head branch {a.pr.head_branch!r}")
        if a.duplicate_of is not None:
            a.signals.append(
                f"duplicate of PR#{a.duplicate_of} (confidence and witnesses in duplicate_evidence)"
            )
        if is_cluster_winner:
            a.signals.append(
                "chosen survivor of a duplicate cluster by deterministic rank "
                "(claimed > fresh > no drift > newer > larger > lower number)"
            )
        if a.supersedes:
            a.signals.append(f"supersedes PR(s) {sorted(a.supersedes)}")
        if a.superseded_by is not None:
            a.signals.append(f"superseded by PR#{a.superseded_by}")
        if stale:
            a.signals.append(
                f"composite staleness: age={a.age_days}d inactive={a.inactive_days}d "
                f"base_drift={a.freshness.base_drift} newer_conflict={has_newer_conflict} "
                f"superseded_by={a.superseded_by}"
            )
        if a.conflicts:
            a.signals.append(f"shares changed files with {len(a.conflicts)} other open PR(s)")

        # Precedence, most actionable first: a PR whose evidence cannot be read
        # is BLOCKED; redundancy outranks an ownership gap; an unowned PR
        # outranks one that is merely old.
        if blocked:
            a.primary = "BLOCKED"
        elif a.duplicate_of is not None:
            a.primary = "DUPLICATE"
        elif is_cluster_winner:
            a.primary = "CANONICAL"
        elif a.supersedes:
            a.primary = "SUPERSEDING"
        elif stale:
            a.primary = "STALE"
        elif unowned:
            a.primary = "ORPHANED"
        elif a.conflicts:
            a.primary = "DEPENDENT"
        else:
            a.primary = "ACTIVE"
            a.signals.append("recent activity, evidence fresh, no correlation pressure")

    # ---- relation list ---------------------------------------------------- #
    relations: list[dict[str, object]] = []
    for i, na in enumerate(numbers):
        for nb in numbers[i + 1 :]:
            shared = sorted(by_number[na].file_set & by_number[nb].file_set)
            if shared:
                relations.append(
                    {
                        "relation": "conflicts_with",
                        "from": na,
                        "to": nb,
                        "witnesses": [f"{len(shared)} shared path(s): " + ", ".join(shared[:5])],
                        "source": "derived",
                    }
                )
    for evidence in dup_edges:
        relations.append(
            {
                "relation": "duplicates",
                "from": evidence.a,
                "to": evidence.b,
                "confidence": round(evidence.confidence, 3),
                "witnesses": list(evidence.witnesses),
                "source": "derived",
            }
        )
    relations.extend(sup_edges)
    relations.extend(dep_edges)
    depends = {(e["from"], e["to"]) for e in dep_edges}  # type: ignore[index]
    for src, dst in sorted(depends):
        relations.append(
            {
                "relation": "blocks",
                "from": dst,
                "to": src,
                "witnesses": [f"inverse of PR#{src} depends_on PR#{dst}"],
                "source": "derived",
            }
        )
        relations.append(
            {
                "relation": "unlocks",
                "from": dst,
                "to": src,
                "witnesses": [f"merging PR#{dst} unblocks PR#{src}"],
                "source": "derived",
            }
        )
    relations.sort(
        key=lambda e: (str(e["relation"]), int(e.get("from") or 0), int(e.get("to") or 0))
    )

    order, cycles = recommend_order(assessments, dep_edges)

    summary: dict[str, int] = {name: 0 for name in CLASSES}
    for a in assessments.values():
        summary[a.primary] += 1

    return {
        "generated_at_utc": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "thresholds": th.to_dict(),
        "live_main_sha": live_main_sha,
        "pr_count": len(numbers),
        "class_summary": summary,
        "prs": [assessments[n].to_dict() for n in numbers],
        "duplicate_clusters": [c for c in clusters if len(c) > 1],
        "duplicate_evidence": [e.to_dict() for e in dup_edges],
        "relations": relations,
        "recommended_merge_order": order,
        "dependency_cycles": cycles,
        "evidence_freshness_summary": {
            verdict: sum(1 for a in assessments.values() if a.freshness.verdict == verdict)
            for verdict in ("FRESH", "PARTIAL", "UNVERIFIABLE")
        },
        "mutation_policy": "read-only: this report never merges, closes, deletes or retargets",
    }


def recommend_order(
    assessments: dict[int, Assessment], dep_edges: list[dict[str, object]]
) -> tuple[list[int], list[list[int]]]:
    """A deterministic recommended merge order — a recommendation, never a plan.

    Kahn's algorithm over the board's declared dependencies, with the tie broken
    by a measured score: canonicality, then how many PRs the merge unlocks, then
    how little conflict pressure it carries, then smaller scope, then fresher
    evidence, then PR number.  Merge authority stays with GitHub and the owner.
    """
    nodes = sorted(assessments)
    dependents: dict[int, list[int]] = {n: [] for n in nodes}
    indegree: dict[int, int] = {n: 0 for n in nodes}
    # `depends_on` reads "from depends on to", so the *prerequisite* (`to`) must
    # be placed first: it holds no incoming edge, and the dependent waits on it.
    for edge in dep_edges:
        dependent, prerequisite = edge.get("from"), edge.get("to")
        if (
            isinstance(dependent, int)
            and isinstance(prerequisite, int)
            and dependent in indegree
            and prerequisite in indegree
        ):
            if dependent not in dependents[prerequisite]:
                dependents[prerequisite].append(dependent)
                indegree[dependent] += 1

    class_rank = {
        "CANONICAL": 0,
        "ACTIVE": 1,
        "SUPERSEDING": 2,
        "DEPENDENT": 3,
        "STALE": 4,
        "ORPHANED": 5,
        "BLOCKED": 6,
        "DUPLICATE": 7,
    }

    def score(n: int) -> tuple[object, ...]:
        a = assessments[n]
        return (
            class_rank.get(a.primary, 9),
            -len(dependents[n]),
            len(a.conflicts),
            len(a.pr.files),
            0 if a.freshness.verdict == "FRESH" else 1,
            n,
        )

    ready = sorted([n for n in nodes if indegree[n] == 0], key=score)
    order: list[int] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for nxt in sorted(dependents[current]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
        ready.sort(key=score)

    cycles: list[list[int]] = []
    if len(order) != len(nodes):
        cycles = [sorted(n for n in nodes if n not in set(order))]
        order.extend(cycles[0])
    return order, cycles


# --------------------------------------------------------------------------- #
# collection (thin adapter over the existing board transport)
# --------------------------------------------------------------------------- #


def collect_prs(repo: str, token: str | None) -> list[dict]:
    """Live open-PR snapshot, paginated, using ``agent_board._gh_get``."""
    records: list[dict] = []
    page = 1
    while True:
        rows = _gh_get(
            f"https://api.github.com/repos/{repo}/pulls?state=open&per_page=100&page={page}",
            token,
        )
        if not isinstance(rows, list):
            raise ValueError(f"GitHub open-PR response is not a list: {rows!r}")
        records.extend(rows)
        if len(rows) < 100:
            break
        page += 1

    out: list[dict] = []
    for record in records:
        number = record["number"]
        files: list[str] = []
        complete = True
        fpage = 1
        while True:
            rows = _gh_get(
                f"https://api.github.com/repos/{repo}/pulls/{number}/files?per_page=100&page={fpage}",
                token,
            )
            if not isinstance(rows, list):
                raise ValueError(f"GitHub files response for PR #{number} is not a list")
            files.extend(str(row["filename"]) for row in rows)
            if len(rows) < 100:
                break
            fpage += 1
            if fpage > 30:
                complete = False
                break
        out.append(
            {
                "number": number,
                "title": record.get("title", ""),
                "head_branch": (record.get("head") or {}).get("ref", ""),
                "head_sha": (record.get("head") or {}).get("sha", ""),
                "base_ref": (record.get("base") or {}).get("ref", ""),
                "base_sha": (record.get("base") or {}).get("sha", ""),
                "created_at": record.get("created_at"),
                "updated_at": record.get("updated_at"),
                "draft": record.get("draft", False),
                "labels": [label.get("name", "") for label in record.get("labels", [])],
                "files": files,
                "files_complete": complete,
            }
        )
    return out


def live_main_sha(repo: str, token: str | None, branch: str = "main") -> str | None:
    ref, _err = _gh_get(f"https://api.github.com/repos/{repo}/git/ref/heads/{branch}", token)
    if isinstance(ref, dict):
        sha = (ref.get("object") or {}).get("sha")
        return str(sha) if sha else None
    return None


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _load_records(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        for key in ("prs", "pulls", "records"):
            if key in data:
                data = data[key]
                break
    if not isinstance(data, list):
        raise ValueError(f"--prs-json must contain a list of PR records: {path}")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--prs-json", dest="prs_json", default="", help="offline PR records")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY") or "")
    parser.add_argument("--token", default=None)
    parser.add_argument("--board", default=None, help="board.json path (default: the repo board)")
    parser.add_argument("--main-sha", default=None, help="live main head (else read from GitHub)")
    parser.add_argument("--as-of", default=None, help="freeze 'now' (YYYY-MM-DDTHH:MM:SSZ)")
    parser.add_argument("--json", dest="as_json", action="store_true")
    parser.add_argument("--stale-age-days", type=int, default=None)
    parser.add_argument("--inactive-days", type=int, default=None)
    parser.add_argument("--duplicate-confidence", type=float, default=None)
    parser.add_argument(
        "--fail-on",
        default="",
        help="exit 1 when any open PR has this class (e.g. ORPHANED or BLOCKED)",
    )
    args = parser.parse_args(argv)

    board_path = Path(args.board) if args.board else None
    if board_path is not None:
        board = json.loads(board_path.read_text(encoding="utf-8"))
    else:
        board = load_board()
    gc_expired(board)  # in-memory only: this tool never writes the board

    if args.prs_json:
        try:
            records = _load_records(args.prs_json)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"pr-convergence: cannot read PR records: {exc}", file=sys.stderr)
            return EXIT_BLOCKED
        main_sha = args.main_sha
    else:
        if "/" not in args.repo:
            print(
                "pr-convergence: provide --repo owner/name (or GITHUB_REPOSITORY) or --prs-json",
                file=sys.stderr,
            )
            return EXIT_BLOCKED
        token = args.token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None
        try:
            records = collect_prs(args.repo, token)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"pr-convergence: GitHub API error: {exc}", file=sys.stderr)
            return EXIT_BLOCKED
        main_sha = args.main_sha or live_main_sha(args.repo, token)
        if not main_sha:
            print(
                "pr-convergence: cannot read the live main head — base drift is unmeasurable",
                file=sys.stderr,
            )
            return EXIT_BLOCKED

    now = _dt(args.as_of) if args.as_of else datetime.now(timezone.utc)
    if now is None:
        print(f"pr-convergence: unparsable --as-of {args.as_of!r}", file=sys.stderr)
        return EXIT_BLOCKED
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    overrides: dict[str, float] = {}
    if args.stale_age_days is not None:
        overrides["stale_age_days"] = args.stale_age_days
    if args.inactive_days is not None:
        overrides["inactive_days"] = args.inactive_days
    if args.duplicate_confidence is not None:
        overrides["duplicate_confidence"] = args.duplicate_confidence
    thresholds = Thresholds(**{**Thresholds().to_dict(), **overrides})  # type: ignore[arg-type]

    try:
        prs = [pr_from_record(record) for record in records]
        report = analyze(prs, board, live_main_sha=main_sha, now=now, thresholds=thresholds)
    except (ValueError, KeyError, TypeError) as exc:
        print(f"pr-convergence: cannot analyse the input: {exc}", file=sys.stderr)
        return EXIT_BLOCKED

    if args.as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"pr-convergence: {report['pr_count']} open PR(s), read-only report")
        print("  classes: " + " ".join(f"{k}={v}" for k, v in report["class_summary"].items()))  # type: ignore[union-attr]
        print(f"  evidence: {report['evidence_freshness_summary']}")
        print(f"  duplicate clusters: {report['duplicate_clusters']}")
        print(f"  recommended order: {report['recommended_merge_order']}")
        if report["dependency_cycles"]:
            print(f"  DEPENDENCY CYCLES: {report['dependency_cycles']}")
        for row in report["prs"]:  # type: ignore[union-attr]
            print(
                f"  PR#{row['number']} [{row['class']:11}] head={row['head_branch']} "
                f"evidence={row['evidence']['verdict']} files={row['file_count']} "
                f"tasks={','.join(row['board_tasks']) or '—'}"
            )
            for signal in row["signals"]:
                print(f"      · {signal}")

    if args.fail_on:
        hits = [
            row["number"]
            for row in report["prs"]  # type: ignore[union-attr]
            if row["class"] == args.fail_on.upper()
        ]
        if hits:
            print(f"pr-convergence: --fail-on {args.fail_on} matched PR(s) {hits}")
            return EXIT_FAIL_ON
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
