"""NAGAR One V1: the independent storyboard verifier refuses every tampered claim.

REAL-COMPUTE: each case starts from a real ffmpeg render. REAL-DISK: the MP4,
passport and approval ledger live in a temporary studio workspace. Each test then
changes exactly one fact and asserts the verifier refuses it with a typed reason.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from nexus_ai_agent.creative import storyboard_jobs as sj
from nexus_ai_agent.creative import storyboard_render as sr
from nexus_ai_agent.creative.storyboard_compiler import compile_storyboard
from nexus_ai_agent.creative.storyboard_render import RenderContext, issue_render_approval
from nexus_ai_agent.nagar.creative.handoff import build_storyboard_render_payload
from nexus_ai_agent.nagar.ibpc.approval import DurableApprovalLedger, durable_approval_ledger_path

SIGNING = "v" * 48
SCRIPT = "INT. KITCHEN - DAY\nSara: Hello there.\nEXT. STREET - NIGHT\nAli: Goodbye.\n"
PROFILE = {"width": 320, "height": 180, "fps": 12, "sample_rate": 16000}


@pytest.fixture()
def rendered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    workspace = tmp_path / "studio"
    workspace.mkdir()
    monkeypatch.setenv(sj.ENV_WORKSPACE, str(workspace))
    monkeypatch.setenv(sj.ENV_SIGNING_KEY, SIGNING)
    from nexus_ai_agent.creative import materializer as mz

    compiled = compile_storyboard(SCRIPT)
    src = hashlib.sha256(SCRIPT.encode("utf-8")).hexdigest()
    ledger = DurableApprovalLedger(
        durable_approval_ledger_path(workspace), now=lambda: "2026-11-19T06:00:00Z"
    )
    try:
        approval_id = issue_render_approval(
            ledger,
            project_id="proj-v",
            job_id="job-v",
            principal_id="owner-v",
            source_sha256=src,
            script_sha256=compiled.script_sha256,
            manifest_sha256=compiled.manifest_sha256,
            render_profile_hash=mz.RenderProfile.from_mapping(PROFILE).profile_hash(),
        )
        ctx = RenderContext(workspace_root=workspace, approval_ledger=ledger, signing_key=SIGNING)
        payload = build_storyboard_render_payload(
            {
                "project_id": "proj-v",
                "job_id": "job-v",
                "screenplay_text": SCRIPT,
                "source_sha256": src,
                "manifest_sha256": compiled.manifest_sha256,
                "approval_id": approval_id,
                "principal_id": "owner-v",
                **PROFILE,
            }
        )
        result = dict(sr.render_storyboard_sync(payload, ctx))
    finally:
        ledger.close()
    return {"workspace": workspace, "payload": payload, "result": result}


def _verify(
    rendered: dict[str, object], result: dict[str, object] | None = None
) -> sj.VerificationOutcome:
    return sj.storyboard_render_verifier(
        rendered["payload"],  # type: ignore[arg-type]
        dict(result if result is not None else rendered["result"]),  # type: ignore[arg-type]
    )


def test_an_honest_render_is_verified(rendered: dict[str, object]) -> None:
    outcome = _verify(rendered)
    assert outcome.ok is True
    assert outcome.summary["passport_seal_verified"] is True


def test_a_result_that_does_not_claim_a_pass_is_refused(rendered: dict[str, object]) -> None:
    bad = dict(rendered["result"])  # type: ignore[arg-type]
    bad["status"] = "failed"
    outcome = _verify(rendered, bad)
    assert outcome.ok is False
    assert outcome.reason_code == "handler_not_rendered"


def test_a_result_missing_its_artifact_identity_is_malformed(rendered: dict[str, object]) -> None:
    bad = dict(rendered["result"])  # type: ignore[arg-type]
    del bad["output_sha256"]
    outcome = _verify(rendered, bad)
    assert outcome.ok is False
    assert outcome.reason_code == "malformed_result"


def test_a_result_pointing_outside_the_workspace_is_refused(rendered: dict[str, object]) -> None:
    bad = dict(rendered["result"])  # type: ignore[arg-type]
    bad["output_rel_path"] = "../escape.mp4"
    outcome = _verify(rendered, bad)
    assert outcome.ok is False
    assert outcome.reason_code == "path_outside_workspace"


def test_a_missing_artifact_on_disk_is_refused(rendered: dict[str, object]) -> None:
    workspace = rendered["workspace"]
    result = rendered["result"]
    assert isinstance(workspace, Path) and isinstance(result, dict)
    (workspace / str(result["output_rel_path"])).unlink()
    outcome = _verify(rendered)
    assert outcome.ok is False
    assert outcome.reason_code == "artifact_missing"


def test_a_passport_that_no_longer_verifies_is_refused(rendered: dict[str, object]) -> None:
    workspace = rendered["workspace"]
    result = rendered["result"]
    assert isinstance(workspace, Path) and isinstance(result, dict)
    (workspace / str(result["passport_rel_path"])).write_text("{}", encoding="utf-8")
    outcome = _verify(rendered)
    assert outcome.ok is False
    assert str(outcome.reason_code).startswith("passport_")


def test_a_claimed_digest_that_differs_from_the_bytes_is_refused(
    rendered: dict[str, object],
) -> None:
    bad = dict(rendered["result"])  # type: ignore[arg-type]
    bad["output_sha256"] = "0" * 64
    outcome = _verify(rendered, bad)
    assert outcome.ok is False
    assert outcome.reason_code == "output_digest_mismatch"


def test_an_asset_root_that_disagrees_with_the_passport_is_refused(
    rendered: dict[str, object],
) -> None:
    bad = dict(rendered["result"])  # type: ignore[arg-type]
    bad["asset_merkle_root"] = "0" * 64
    outcome = _verify(rendered, bad)
    assert outcome.ok is False
    assert outcome.reason_code == "merkle_root_mismatch"


def test_a_media_verdict_that_is_not_pass_is_refused(
    rendered: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.creative import artifact_verifier as av

    failing = av.Mp4Verdict(status=av.VIOLATED, reasons=("forced",))
    monkeypatch.setattr(sj, "verify_mp4", lambda *_a, **_k: failing)
    outcome = _verify(rendered)
    assert outcome.ok is False
    assert outcome.reason_code == "media_verdict_not_pass"


def test_a_verifier_without_studio_configuration_refuses(
    rendered: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(sj.ENV_WORKSPACE)
    outcome = _verify(rendered)
    assert outcome.ok is False
    assert outcome.reason_code == "studio_not_configured"


@pytest.mark.parametrize(
    "value",
    ["", "short", "x" * 10],
)
def test_the_signing_key_must_be_long_enough(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(sj.ENV_WORKSPACE, "/tmp")
    monkeypatch.setenv(sj.ENV_SIGNING_KEY, value)
    with pytest.raises(sj.StoryboardJobConfigError):
        sj.studio_settings(__import__("os").environ)


def test_an_unset_workspace_is_a_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(sj.ENV_WORKSPACE, raising=False)
    monkeypatch.setenv(sj.ENV_SIGNING_KEY, "x" * 48)
    with pytest.raises(sj.StoryboardJobConfigError, match="is not set"):
        sj.studio_settings(__import__("os").environ)


def test_the_ledger_clock_is_utc_iso_seconds() -> None:
    import re

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", sj._utc_now())
