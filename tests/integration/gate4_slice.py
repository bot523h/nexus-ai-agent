"""Gate 4 — cross-layer vertical slice core (Integration-owned, consume-only).

This module is the deterministic core of the Gate-4 harness. It implements
**no** runtime, compiler, LaneIR, LUT, burn-in, OTIO, hashing, ffprobe,
capability or rendering logic of its own — every step consumes the existing
layers (Agent 1 runtime, Agent 2 command/capability, Agent 3 job lifecycle)
and records evidence.

T-id rule (engineered, since no file in the repository defined the ids)
-----------------------------------------------------------------------
``T01..T70`` are the rows of the seven pack tables in
``docs/NAGAR_70_OPERATIONS_TDD.md`` ("کاتالوگ ۷۰ عملیات"), in document order,
zero-padded. The parser below re-derives them from the document every run, so
the mapping can never drift from the catalogue it claims to describe:

    T01 = timeline.split_at_playhead   T02 = timeline.trim
    T22 = scene.remove_object          T31 = motion.add_transition
    T64 = color.match_shot

Cell verdicts (exactly four legal values)
-----------------------------------------
``PASS``       proven end-to-end in this run, with machine-checked evidence.
``FAIL``       exercised and the layer's invariant did not hold.
``MISSING``    the production product has no path for this cell (proven by a
               typed negative probe — never faked as verified).
``NOT_VERIFIED`` a path exists in the tree but this gate could not verify it
               (environment or restart limits) — recorded verbatim.

Layer semantics: *Product* = intent entry (surface mapper); *Command* =
schema-2 TypedCommand through the bus gates; *Capability* = registered +
resolved (lifecycle) + dispatched; *Job* = durable queue terminal row;
*Runtime* = real FFmpeg lane execution; *Artifact* = verified non-empty bytes
with recomputed sha256 and probe facts; *Reopen* = fresh-context re-read of
job + artifact truth; *E2E* = Product→Job→Runtime→Artifact in one take.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TDD_PATH = REPO_ROOT / "docs" / "NAGAR_70_OPERATIONS_TDD.md"

#: The Gate-4 vertical slice, asserted against the parsed catalogue.
EXPECTED_SLICE: dict[str, str] = {
    "T01": "timeline.split_at_playhead",
    "T02": "timeline.trim",
    "T22": "scene.remove_object",
    "T31": "motion.add_transition",
    "T64": "color.match_shot",
}

SLICE_IDS: tuple[str, ...] = tuple(EXPECTED_SLICE)

LAYERS: tuple[str, ...] = (
    "Product",
    "Command",
    "Capability",
    "Job",
    "Runtime",
    "Artifact",
    "Reopen",
    "E2E",
)

#: Per-stage distinctions required by the Gate-4 brief.
DISTINCTIONS: tuple[str, ...] = (
    "CONTRACT",
    "DOMAIN",
    "JOB",
    "REAL_RUNTIME",
    "ARTIFACT",
    "E2E",
)

LEGAL_VERDICTS = frozenset({"PASS", "FAIL", "MISSING", "NOT_VERIFIED"})

#: Baseline composition of this run (owner-directed full-stack baseline).
BASELINE = {
    "main": "035a896dd2ed1293de6accf2ef4309da2fd64c89",
    "agent1_runtime_pr67": "9c3a34f107b4b5f3c878a0ec2a7d82e8c24614d3",
    "agent2_command_pr68": "eef827641b0627f3581976d494eb0e71f4dd16b6",
    "branch": "arena/01a0d3af-nexus-ai-agent",
}

_ROW_RE = re.compile(r"^\|\s*`([a-z_]+\.[a-z_0-9]+)`\s*\|", re.MULTILINE)


def load_tdd_catalog(path: Path = TDD_PATH) -> list[str]:
    """Parse the 70-operation catalogue rows in document order."""
    rows = _ROW_RE.findall(path.read_text(encoding="utf-8"))
    if len(rows) != 70:
        raise AssertionError(
            f"expected exactly 70 catalogue rows in {path.name}, parsed {len(rows)}"
        )
    return rows


def resolve_slice(catalog: list[str] | None = None) -> dict[str, str]:
    """Resolve T01/T02/T22/T31/T64 against the parsed catalogue."""
    rows = catalog if catalog is not None else load_tdd_catalog()
    resolved = {t_id: rows[int(t_id[1:]) - 1] for t_id in SLICE_IDS}
    if resolved != EXPECTED_SLICE:
        raise AssertionError(
            f"slice drift: catalogue now says {resolved}, gate expects {EXPECTED_SLICE}"
        )
    return resolved


@dataclass
class StepEvidence:
    """What this gate proved for one slice step, per distinction."""

    t_id: str
    operation: str
    distinctions: dict[str, str] = field(
        default_factory=lambda: {
            **{d: "NOT_VERIFIED" for d in DISTINCTIONS},
            # Honest pre-evidence defaults for layers a step may never reach:
            # no product entry and nothing to reopen until a job exists.
            "PRODUCT": "MISSING",
            "REOPEN": "MISSING",
        }
    )
    notes: dict[str, str] = field(default_factory=dict)
    provenance: dict[str, object] = field(default_factory=dict)

    def set(self, distinction: str, verdict: str, note: str = "") -> None:
        if verdict not in LEGAL_VERDICTS:
            raise ValueError(f"illegal verdict {verdict!r}")
        self.distinctions[distinction] = verdict
        if note:
            self.notes[distinction] = note


def new_step_evidence() -> dict[str, StepEvidence]:
    return {t_id: StepEvidence(t_id=t_id, operation=op) for t_id, op in resolve_slice().items()}


def build_matrix(steps: dict[str, StepEvidence]) -> dict[str, dict[str, str]]:
    """Layer × step matrix derived from the per-step distinctions.

    Mapping (documented so the derivation is auditable):

    Product      ← E2E gate at the surface: mapper-accepted intent
    Command      ← CONTRACT (envelope through the bus gates)
    Capability   ← DOMAIN (registered + lifecycle-resolved + dispatched)
    Job/.../E22E ← JOB / REAL_RUNTIME / ARTIFACT / E2E distinctions
    Reopen       ← its own recorded distinction
    """
    matrix: dict[str, dict[str, str]] = {}
    matrix["Product"] = {
        t_id: steps[t_id].distinctions.get("PRODUCT", "MISSING") for t_id in SLICE_IDS
    }
    matrix["Command"] = {t_id: steps[t_id].distinctions["CONTRACT"] for t_id in SLICE_IDS}
    matrix["Capability"] = {t_id: steps[t_id].distinctions["DOMAIN"] for t_id in SLICE_IDS}
    matrix["Job"] = {t_id: steps[t_id].distinctions["JOB"] for t_id in SLICE_IDS}
    matrix["Runtime"] = {t_id: steps[t_id].distinctions["REAL_RUNTIME"] for t_id in SLICE_IDS}
    matrix["Artifact"] = {t_id: steps[t_id].distinctions["ARTIFACT"] for t_id in SLICE_IDS}
    matrix["Reopen"] = {t_id: steps[t_id].distinctions["REOPEN"] for t_id in SLICE_IDS}
    matrix["E2E"] = {t_id: steps[t_id].distinctions["E2E"] for t_id in SLICE_IDS}
    for layer, row in matrix.items():
        for t_id, verdict in row.items():
            if verdict not in LEGAL_VERDICTS:
                raise AssertionError(f"illegal verdict {layer}/{t_id}: {verdict!r}")
    return {layer: matrix[layer] for layer in LAYERS}


def render_markdown(matrix: dict[str, dict[str, str]]) -> str:
    """Render the Truth Matrix exactly in the Gate-4 brief's shape."""
    header = "Layer| " + "| ".join(SLICE_IDS)
    sep = "|".join(["---"] * (len(SLICE_IDS) + 1))
    lines = [header, sep]
    for layer in LAYERS:
        row = matrix[layer]
        lines.append(f"{layer}| " + "| ".join(row[t_id] for t_id in SLICE_IDS))
    return "\n".join(lines)
