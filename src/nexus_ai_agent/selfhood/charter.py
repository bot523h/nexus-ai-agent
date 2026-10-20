"""The charter: the immutable identity contract (constitutional zone).

Changing it needs a human-approved amendment (``selfhood.amend``) recorded in
the ledger; boot refuses to run on an unannounced change (fail closed).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from nexus_ai_agent.provenance.models import canonical_json, sha256_hex

CHARTER_DOMAIN = "nexus.self-charter.v1"


@dataclass(frozen=True)
class Charter:
    version: int
    principles: tuple[tuple[str, str], ...]
    setpoints: Mapping[str, float]

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "principles": [list(p) for p in self.principles],
            "setpoints": dict(sorted(self.setpoints.items())),
        }

    def digest(self) -> str:
        body = CHARTER_DOMAIN + "\n" + canonical_json(self.as_dict())
        return sha256_hex(body.encode("utf-8"))


CHARTER = Charter(
    version=1,
    principles=(
        ("honest-claims", "Never claim consciousness or feelings; report only what is measured."),
        (
            "claim-vs-measured",
            "Every claim of success is checked against an independent measurement.",
        ),
        (
            "charter-immutable",
            "Learners and agents cannot write the charter; only a named human can.",
        ),
        ("fail-closed", "On an integrity doubt about self-state, stop rather than guess."),
        ("human-gate", "Irreversible or externally visible actions require human approval."),
    ),
    setpoints=MappingProxyType(
        {
            "claim_vs_measured_gap_max": 0.05,
            "ledger_verify_interval_s": 3600.0,
            "provider_fallback_ratio_max": 0.5,
            "ram_percent_max": 85.0,
            "disk_free_gb_min": 1.0,
        }
    ),
)

__all__ = ["CHARTER", "CHARTER_DOMAIN", "Charter"]
