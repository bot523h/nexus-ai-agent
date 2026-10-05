"""The CLI ``intent`` command end-to-end, with only the model faked.

This is the real host caller: it builds the production ``CommandBus`` +
runtime registry + host authorizer, the real cognition gateway and the real
free-text slice, records the cognition decision to a real causal journal, and
writes a real receipt.

Only the *external model* is replaced (by a scripted ``LLMPort``); the
substitution is declared here and reaches the system through the production
composition root, so the composition path itself is exercised.  No network is
touched and no credential is read.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from nexus_ai_agent.cli import app
from nexus_ai_agent.nagar.composition import ProviderStatus
from nexus_ai_agent.provenance.journal import CausalJournal
from nexus_ai_agent.provenance.models import EventKind


class ScriptedProvider:
    """A declared test double for the external model seam only."""

    def __init__(self, response: str) -> None:
        self._response = response
        self.calls = 0

    async def complete(self, prompt: str, *, idempotency_key: str | None = None) -> str:
        self.calls += 1
        return self._response


def _proposal(**overrides: object) -> str:
    payload: dict[str, object] = {
        "schema_id": "nagar.gateway.proposal.v1",
        "schema_version": 1,
        "operation": "timeline.trim",
        "input": {"clip_asset_id": "src", "in_point_us": 1_000_000, "out_point_us": 8_000_000},
        "rationale": "trim 1s..8s per the operator",
        "confidence": 0.9,
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_cli_intent_model_kill_reports_clarification_and_writes_receipt(tmp_path) -> None:
    receipt = tmp_path / "receipt.json"
    result = CliRunner().invoke(
        app, ["intent", "trim from 1s to 8s", "--receipt-out", str(receipt)]
    )
    assert result.exit_code == 0, result.output
    assert "status: clarification_required" in result.output
    payload = json.loads(receipt.read_text())
    assert payload["status"] == "clarification_required"
    assert payload["state_revision"] == 0
    assert payload["verification"]["verified"] is False


def test_cli_intent_applies_and_writes_verified_receipt(tmp_path, monkeypatch) -> None:
    provider = ScriptedProvider(_proposal())
    monkeypatch.setattr(
        "nexus_ai_agent.nagar.composition.build_cognition_provider",
        lambda settings=None: (provider, ProviderStatus.AVAILABLE),
    )
    journal_db = tmp_path / "journal.sqlite3"
    receipt = tmp_path / "receipt.json"

    result = CliRunner().invoke(
        app,
        [
            "intent",
            "trim from 1s to 8s",
            "--receipt-out",
            str(receipt),
            "--journal-db",
            str(journal_db),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "status: applied" in result.output
    assert "verified: True" in result.output
    assert provider.calls == 1

    payload = json.loads(receipt.read_text())
    assert payload["status"] == "applied"
    assert payload["operation"] == "timeline.trim"
    assert payload["result"]["asset_id"]
    assert payload["result"]["content_sha256"]
    assert payload["verification"]["verified"] is True
    assert payload["state_revision"] >= 1

    # The real causal journal recorded the cognition decision as an observation.
    journal = CausalJournal(str(journal_db))
    from nexus_ai_agent.creative.studio.models import ActorIdentity
    from nexus_ai_agent.nagar.creative import derive_intent_idempotency_key

    key = derive_intent_idempotency_key(
        "nagar-free-text-demo",
        ActorIdentity(kind="user", actor_id="local-operator"),
        "trim from 1s to 8s",
    )
    records = journal.records_for_job(f"intent-{key.split(':', 1)[-1]}")
    assert len(records) == 1
    assert records[0].kind is EventKind.JOB_RESERVED
    assert records[0].payload["detail"]["decision"] == "proposal_accepted"


def test_cli_intent_refuses_hostile_model_output(tmp_path, monkeypatch) -> None:
    provider = ScriptedProvider(_proposal(operation="shell", command="rm -rf /"))
    monkeypatch.setattr(
        "nexus_ai_agent.nagar.composition.build_cognition_provider",
        lambda settings=None: (provider, ProviderStatus.AVAILABLE),
    )
    receipt = tmp_path / "receipt.json"
    result = CliRunner().invoke(
        app, ["intent", "do something dangerous", "--receipt-out", str(receipt)]
    )
    assert result.exit_code == 1, result.output
    assert "status: refused" in result.output
    payload = json.loads(receipt.read_text())
    assert payload["status"] == "refused"
    assert payload["state_revision"] == 0


def test_cli_intent_rejects_nonfinite_source_duration() -> None:
    # ``--duration-us`` is bounded by typer (min=1); zero/negative never reaches
    # the slice, so the demo project is always a valid, non-empty source.
    result = CliRunner().invoke(app, ["intent", "trim", "--duration-us", "0"])
    assert result.exit_code != 0
