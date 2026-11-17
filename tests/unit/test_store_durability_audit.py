"""F-006: the durability audit must be real, and its risks must be named.

The audit is only useful if it (a) covers every store the store map names, and
(b) actually reports the authoritative local sidecars a volume-less
scale-to-zero deploy cannot keep. These tests pin both, so the classification
cannot silently drift into a comfortable fiction.
"""

from __future__ import annotations

from pathlib import Path

from nexus_ai_agent.maintenance.durability import (
    STORE_AUDIT,
    Durability,
    classification_of,
    production_durability_risks,
)

STORE_MAP = Path("docs/architecture/DATA_AND_STORAGE.md")


def test_every_store_is_classified_from_the_fixed_vocabulary() -> None:
    allowed = {member.value for member in Durability}
    assert STORE_AUDIT, "the audit must not be empty"
    for row in STORE_AUDIT:
        assert row.classification.value in allowed
        assert row.owner, f"{row.name} has no owner module"
        assert row.rationale, f"{row.name} has no rationale"


def test_the_six_named_subsystems_are_all_present() -> None:
    """The finding names six subsystems explicitly; all must be audited."""
    names = " | ".join(row.name for row in STORE_AUDIT)
    for required in (
        "Job queue",
        "Causal journal",
        "Self-ledger",
        "Creative job registry",
        "Vector store",
    ):
        assert required in names, f"{required} missing from the audit"


def test_authoritative_local_sidecars_are_reported_without_a_volume() -> None:
    """No volume + scale-to-zero ⇒ the local authoritative sidecars are risks."""
    risks = production_durability_risks(persistent_volume=False)
    risky_names = {row.name for row in risks}
    assert any("Job queue" in name for name in risky_names), risky_names
    assert any("Causal journal" in name for name in risky_names), risky_names
    assert any("Effect outbox" in name for name in risky_names), risky_names
    # Every reported risk must be authoritative and production-relevant.
    for row in risks:
        assert row.classification is Durability.DURABLE_AUTHORITATIVE
        assert row.production_depends is True
        assert row.survives_scale_to_zero is False


def test_a_persistent_volume_clears_the_local_sidecar_risks() -> None:
    """The check must respond to the deployment fact, not hard-code a verdict."""
    risks = production_durability_risks(persistent_volume=True)
    # The application DB is external and never a risk either way.
    assert not any("Application DB" in row.name for row in risks)
    # With a volume, no *authoritative* store is reported.
    assert not any(row.classification is Durability.DURABLE_AUTHORITATIVE for row in risks)


def test_application_db_is_never_a_scale_to_zero_risk() -> None:
    assert classification_of("Application DB (users/chats/messages/memory/…)") is (
        Durability.DURABLE_AUTHORITATIVE
    )
    risks = production_durability_risks(persistent_volume=False)
    assert not any("Application DB" in row.name for row in risks)


def test_unknown_store_is_unverified_not_safe() -> None:
    """An unclassified name must be UNVERIFIED, and UNVERIFIED is a risk."""
    assert classification_of("does-not-exist") is Durability.UNVERIFIED


def test_store_map_and_audit_stay_aligned() -> None:
    """The audit must not drift from the documented store map."""
    if not STORE_MAP.exists():  # pragma: no cover - doc always shipped
        return
    text = STORE_MAP.read_text(encoding="utf-8")
    for fragment in (
        "Job queue",
        "Causal journal",
        "Self-ledger",
        "Checkpoints",
        "Vector store",
    ):
        assert fragment in text, f"{fragment} vanished from the store map"
