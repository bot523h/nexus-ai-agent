"""Advisory repair suggestions (Milestone D).

A suggestion is never a rewrite. It names the finding it addresses, points at
the exact source, and describes one *possible* human action. Applying a
suggestion is out of scope: the lab never edits canonical story or project
state, and never mints authority.
"""

from __future__ import annotations

from dataclasses import dataclass

from .causal import CausalFindingCode, Finding
from .temporal import TemporalResult

__all__ = ["Suggestion", "suggest_repairs"]

_SUGGESTION_LABEL = "ADVISORY_SUGGESTION"


@dataclass(frozen=True)
class Suggestion:
    """A labelled, non-authoritative repair hint tied to a finding."""

    label: str
    addresses_code: str
    source_path: str
    text: str

    def as_row(self) -> dict[str, str]:
        return {
            "label": self.label,
            "addresses": self.addresses_code,
            "source": self.source_path,
            "suggestion": self.text,
        }


def suggest_repairs(
    findings: tuple[Finding, ...],
    temporal: TemporalResult | None = None,
) -> tuple[Suggestion, ...]:
    """Return deterministic, advisory suggestions for the given findings."""
    out: list[Suggestion] = []
    for finding in sorted(findings, key=Finding.sort_key):
        if finding.code == CausalFindingCode.POSSESSION_UNKNOWN:
            out.append(
                Suggestion(
                    label=_SUGGESTION_LABEL,
                    addresses_code=finding.code,
                    source_path=finding.source.path,
                    text=(
                        "Declare an explicit possession transfer (or an initial "
                        "'possesses' fact) before this event so possession is no "
                        "longer UNKNOWN."
                    ),
                )
            )
        elif finding.code == CausalFindingCode.POSSESSION_CONFLICT:
            out.append(
                Suggestion(
                    label=_SUGGESTION_LABEL,
                    addresses_code=finding.code,
                    source_path=finding.source.path,
                    text=(
                        "Either add the missing prior transfer that gives the actor "
                        "the prop, or correct the possession fact this transfer "
                        "contradicts."
                    ),
                )
            )
        elif finding.code in (
            CausalFindingCode.KNOWLEDGE_PREREQUISITE_UNMET,
            CausalFindingCode.KNOWLEDGE_UNKNOWN,
        ):
            out.append(
                Suggestion(
                    label=_SUGGESTION_LABEL,
                    addresses_code=finding.code,
                    source_path=finding.source.path,
                    text=(
                        "Add a 'learn_fact' event for the revealing actor earlier in "
                        "the declared order so the knowledge prerequisite is "
                        "explicitly satisfied."
                    ),
                )
            )
        elif finding.code == CausalFindingCode.AMBIGUOUS_ORDER:
            out.append(
                Suggestion(
                    label=_SUGGESTION_LABEL,
                    addresses_code=finding.code,
                    source_path=finding.source.path,
                    text=(
                        "Give the two events distinct order_index values, or record "
                        "an explicit temporal constraint if their order is truly "
                        "irrelevant."
                    ),
                )
            )
        elif finding.code == CausalFindingCode.FLASHBACK_ORDER:
            out.append(
                Suggestion(
                    label=_SUGGESTION_LABEL,
                    addresses_code=finding.code,
                    source_path=finding.source.path,
                    text=(
                        "If this non-linear edit is intentional, annotate it as a "
                        "creative exception; otherwise correct the declared "
                        "story_tick."
                    ),
                )
            )

    if temporal is not None and temporal.witness is not None and temporal.witness.is_valid():
        out.append(
            Suggestion(
                label=_SUGGESTION_LABEL,
                addresses_code="TEMPORAL_NEGATIVE_CYCLE",
                source_path="temporal",
                text=(
                    "The declared bounds form a negative cycle ("
                    + ", ".join(temporal.witness.constraint_ids)
                    + "); relax or remove one of these constraints to make the "
                    "network consistent."
                ),
            )
        )
    return tuple(out)
