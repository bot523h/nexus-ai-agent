"""Delivery signing spike — wave-4 step2 (+ wave-5 task-142 coverage close-out).

Covers the fail-closed contract without requiring PyNaCl to be installed:
the HMAC fallback and the NaCl path share the same ``sign``/``verify``
interface (base64 transport, constant-time compare, :class:`SigningError`
on every failure), so every test is green without the extra and an
installed NaCl is exercised without any code change.

Wave-5 task-142 (measured with ``scripts/pack_coverage.py``): the delivery
pack was the weakest pack at 87.19% — ``signing.py`` 68.89% and
``operations.py`` ~88% — below the 95% project bar.  This module closes
that gap *without touching the implementation*: key-material edge cases,
a deterministic PyNaCl test double for the Ed25519 seam, and the uncovered
validation/execution branches of the delivery operations handlers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import json
import secrets
import sys
import types

import pytest

from nexus_ai_agent.creative.packs.delivery import signing as sut
from nexus_ai_agent.creative.packs.delivery.operations import build_delivery_registry
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    CommandValidationError,
    Project,
    Timeline,
    TypedCommand,
    new_project,
)


def _set_key(monkeypatch: pytest.MonkeyPatch) -> str:
    key_hex = secrets.token_hex(32)  # 64 hex chars → 32 bytes
    monkeypatch.setenv("NEXUS_SIGNING_KEY", key_hex)
    return key_hex


def test_round_trip_dict_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    manifest = {"package_id": "nexus.color.delivery", "version": "1.0.0", "ops": ["export_otio"]}
    sig = sut.sign_manifest(manifest)
    # base64 must decode
    assert base64.b64decode(sig, validate=True)
    # verify must not raise
    sut.verify_manifest(manifest, sig)


def test_round_trip_bytes_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    manifest = b'{"hello":"world"}'
    sig = sut.sign_manifest(manifest)
    sut.verify_manifest(manifest, sig)


def test_canonicalisation_is_order_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    a = {"b": 1, "a": 2}
    b = {"a": 2, "b": 1}
    # Same logical JSON must yield same signature
    assert sut.sign_manifest(a) == sut.sign_manifest(b)
    # Cross-verify
    sut.verify_manifest(b, sut.sign_manifest(a))


def test_tamper_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    manifest = {"seq": [1, 2, 3], "name": "master"}
    sig = sut.sign_manifest(manifest)
    tampered = {"seq": [1, 2, 999], "name": "master"}
    with pytest.raises(sut.SigningError, match="tampered|verification failed"):
        sut.verify_manifest(tampered, sig)
    # Bit-flip in the signature itself must also be rejected
    bad = sig[:-4] + ("AAAA" if sig[-4:] != "AAAA" else "BBBB")
    with pytest.raises(sut.SigningError):
        sut.verify_manifest(manifest, bad)


def test_missing_key_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_SIGNING_KEY", raising=False)
    with pytest.raises(sut.SigningError, match="NEXUS_SIGNING_KEY is not set"):
        sut.sign_manifest({"x": 1})
    with pytest.raises(sut.SigningError, match="NEXUS_SIGNING_KEY is not set"):
        sut.verify_manifest({"x": 1}, "abcd")


def test_empty_key_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_SIGNING_KEY", "   ")
    with pytest.raises(sut.SigningError, match="not set"):
        sut.sign_manifest({"x": 1})


def test_malformed_base64_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    with pytest.raises(sut.SigningError, match="base64"):
        sut.verify_manifest({"x": 1}, "!!!not-base64!!!")


def test_signed_manifest_dataclass_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    manifest = {"k": "v"}
    sig = sut.sign_manifest(manifest)
    sm = sut.SignedManifest(manifest=manifest, signature=sig)
    sm.verify()  # must not raise
    sm_tampered = sut.SignedManifest(manifest={"k": "other"}, signature=sig)
    with pytest.raises(sut.SigningError):
        sm_tampered.verify()


def test_different_keys_produce_different_signatures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_SIGNING_KEY", secrets.token_hex(32))
    a = sut.sign_manifest({"x": 1})
    monkeypatch.setenv("NEXUS_SIGNING_KEY", secrets.token_hex(32))
    b = sut.sign_manifest({"x": 1})
    assert a != b


def test_non_ascii_manifest_is_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    manifest = {"title": "سلام دنیا — نگار", "fps": 24}
    sig = sut.sign_manifest(manifest)
    # JSON with ensure_ascii=False must round-trip through utf-8
    canonical = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    sut.verify_manifest(canonical, sig)


# ── wave-5 task-142: key-material and canonicalisation edge coverage ──────


def test_base64_key_fallback_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-hex ``NEXUS_SIGNING_KEY`` falls back to base64 decoding."""
    raw_key = b"nexus-wave5-task142-key-bytes!"  # 30 raw bytes, arbitrary
    key_b64 = base64.b64encode(raw_key).decode("ascii")
    assert not all(c in "0123456789abcdefABCDEF" for c in key_b64) or len(key_b64) != 64
    monkeypatch.setenv("NEXUS_SIGNING_KEY", key_b64)
    assert sut.get_signing_key_bytes() == raw_key
    # same canonicalisation, so sign/verify round-trips through the fallback
    manifest = {"fallback": "base64", "n": 42}
    sut.verify_manifest(manifest, sut.sign_manifest(manifest))


def test_malformed_key_raises_signing_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key that is neither 64-hex nor valid base64 fails closed."""
    monkeypatch.setenv("NEXUS_SIGNING_KEY", "!!!never-decodable!!!")
    with pytest.raises(sut.SigningError, match="malformed"):
        sut.get_signing_key_bytes()
    with pytest.raises(sut.SigningError, match="malformed"):
        sut.sign_manifest({"x": 1})


def test_get_signing_key_bytes_hex(monkeypatch: pytest.MonkeyPatch) -> None:
    key_hex = _set_key(monkeypatch)
    assert sut.get_signing_key_bytes() == bytes.fromhex(key_hex)
    assert len(sut.get_signing_key_bytes()) == 32


def test_str_manifest_canonicalisation(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    raw = '{"ops":["export_otio"],"package_id":"nexus.color.delivery"}'
    sig = sut.sign_manifest(raw)
    sut.verify_manifest(raw, sig)
    # the byte-equal canonical form of the dict verifies against the str signature
    sut.verify_manifest({"ops": ["export_otio"], "package_id": "nexus.color.delivery"}, sig)
    # a byte-different string of the same JSON value is a *different* manifest
    spaced = json.dumps(
        {"ops": ["export_otio"], "package_id": "nexus.color.delivery"}, sort_keys=True
    )
    assert spaced != raw  # default separators add spaces
    with pytest.raises(sut.SigningError):
        sut.verify_manifest(spaced, sig)


# ── wave-5 task-142: Ed25519 seam via a deterministic PyNaCl test double ──


class _FakeNaclBadSig(Exception):
    """Test double for ``nacl.exceptions.BadSignatureError``."""


class _FakeNaclSignedMessage:
    def __init__(self, signature: bytes) -> None:
        self.signature = signature


def _fake_signature(seed: bytes, canonical: bytes) -> bytes:
    """Deterministic 64-byte stand-in for an Ed25519 signature."""
    digest = hashlib.sha256(seed + canonical).digest()
    return digest + digest


class _FakeNaclSigningKey:
    """Semantically faithful double for ``nacl.signing.SigningKey``."""

    def __init__(self, seed: bytes) -> None:
        if not seed:
            raise ValueError("seed must not be empty")
        self._seed = bytes(seed)
        self.verify_key = _FakeNaclVerifyKey(self._seed)

    def sign(self, canonical: bytes) -> _FakeNaclSignedMessage:
        return _FakeNaclSignedMessage(_fake_signature(self._seed, canonical))


class _FakeNaclVerifyKey:
    """Test double for ``nacl.signing.VerifyKey`` (raises like PyNaCl)."""

    def __init__(self, seed: bytes) -> None:
        self._seed = bytes(seed)

    def verify(self, canonical: bytes, signature: bytes) -> bytes:
        if not hmac.compare_digest(_fake_signature(self._seed, canonical), signature):
            raise _FakeNaclBadSig("Signature was forged or corrupt")
        return canonical


@pytest.fixture()
def fake_nacl_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route ``sign_manifest``/``verify_manifest`` through the NaCl seam."""
    monkeypatch.setattr(sut, "_NaclSigningKey", _FakeNaclSigningKey)
    monkeypatch.setattr(sut, "_NaclVerifyKey", _FakeNaclVerifyKey)
    monkeypatch.setattr(sut, "_NaclBadSig", _FakeNaclBadSig)
    monkeypatch.setattr(sut, "_HAVE_NACL", True)


def test_nacl_backend_round_trip(monkeypatch: pytest.MonkeyPatch, fake_nacl_backend: None) -> None:
    _set_key(monkeypatch)
    manifest = {"backend": "ed25519", "ops": ["export_otio"]}
    sig = sut.sign_manifest(manifest)
    # The NaCl seam signs a 64-byte signature, base64-transported like HMAC.
    assert len(base64.b64decode(sig, validate=True)) == 64
    sut.verify_manifest(manifest, sig)
    sut.verify_manifest(dict(reversed(list(manifest.items()))), sig)


def test_nacl_backend_tamper_rejected(
    monkeypatch: pytest.MonkeyPatch, fake_nacl_backend: None
) -> None:
    _set_key(monkeypatch)
    manifest = {"film": "master"}
    sig = sut.sign_manifest(manifest)
    with pytest.raises(sut.SigningError, match="tampered"):
        sut.verify_manifest({"film": "other"}, sig)
    # bit-flip inside an otherwise valid base64 signature
    raw = bytearray(base64.b64decode(sig, validate=True))
    raw[0] ^= 0xFF
    forged = base64.b64encode(bytes(raw)).decode("ascii")
    with pytest.raises(sut.SigningError, match="tampered"):
        sut.verify_manifest(manifest, forged)


def test_nacl_backend_bad_base64_rejected(
    monkeypatch: pytest.MonkeyPatch, fake_nacl_backend: None
) -> None:
    _set_key(monkeypatch)
    with pytest.raises(sut.SigningError, match="base64"):
        sut.verify_manifest({"x": 1}, "%%%not-base64%%%")


def test_nacl_import_probe_success_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the ``try`` branch of the PyNaCl probe at module top-level.

    Lines 64-69 of ``signing.py`` only execute when ``import nacl`` succeeds;
    injecting a faithful fake into ``sys.modules`` and reloading covers them
    in a venv where PyNaCl is not installed.  State is restored in a finally
    block so later tests observe the original import truth again.
    """
    probe_names = ("nacl", "nacl.signing", "nacl.exceptions")
    stashed = {name: sys.modules.get(name) for name in probe_names}

    fake_root = types.ModuleType("nacl")
    fake_signing_mod = types.ModuleType("nacl.signing")
    fake_signing_mod.SigningKey = _FakeNaclSigningKey  # type: ignore[attr-defined]
    fake_signing_mod.VerifyKey = _FakeNaclVerifyKey  # type: ignore[attr-defined]
    fake_exceptions_mod = types.ModuleType("nacl.exceptions")
    fake_exceptions_mod.BadSignatureError = _FakeNaclBadSig  # type: ignore[attr-defined]
    fake_root.signing = fake_signing_mod  # type: ignore[attr-defined]
    fake_root.exceptions = fake_exceptions_mod  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "nacl", fake_root)
    monkeypatch.setitem(sys.modules, "nacl.signing", fake_signing_mod)
    monkeypatch.setitem(sys.modules, "nacl.exceptions", fake_exceptions_mod)
    try:
        importlib.reload(sut)
        assert sut._HAVE_NACL is True
        monkeypatch.setenv("NEXUS_SIGNING_KEY", secrets.token_hex(32))
        manifest = {"probe": "success"}
        sig = sut.sign_manifest(manifest)
        sut.verify_manifest(manifest, sig)
        with pytest.raises(sut.SigningError, match="tampered"):
            sut.verify_manifest({"probe": "forged"}, sig)
    finally:
        for name, original in stashed.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
        importlib.reload(sut)
    assert sut._HAVE_NACL is False  # PyNaCl is not installed in this venv


# ── wave-5 task-142: delivery operations branch coverage (fail-closed) ────


def _delivery_bus() -> CommandBus:
    """Minimal project like ``test_delivery_pack`` — one video, one audio."""
    registry = build_delivery_registry()
    video = AssetRecord(
        asset_id="clip_v1",
        media_kind="video",
        content_sha256="sha256:wave5video01",
        duration_us=4_000_000,
    )
    audio = AssetRecord(
        asset_id="audio_a1",
        media_kind="audio",
        content_sha256="sha256:wave5audio01",
        duration_us=4_000_000,
    )
    timeline = Timeline(timeline_id="tl_wave5", duration_us=4_000_000)
    project: Project = new_project("p_wave5_signing_cov", "Coverage Project", timeline)
    project = project.model_copy(update={"assets": [video, audio]})
    return CommandBus(project, registry=registry)


def test_apply_lut_unknown_asset_rejected() -> None:
    bus = _delivery_bus()
    cmd = TypedCommand(
        command_id="cmd_cov_lut_unknown",
        operation="color.apply_lut",
        input={"clip_asset_id": "clip_missing", "lut_name": "cinematic", "intensity": 0.5},
    )
    with pytest.raises(CommandValidationError, match="unknown clip asset"):
        bus.dispatch(cmd)


def test_apply_lut_requires_video_asset() -> None:
    bus = _delivery_bus()
    cmd = TypedCommand(
        command_id="cmd_cov_lut_audio",
        operation="color.apply_lut",
        input={"clip_asset_id": "audio_a1", "lut_name": "cinematic", "intensity": 0.5},
    )
    with pytest.raises(CommandValidationError, match="requires video asset"):
        bus.dispatch(cmd)


def test_adjust_exposure_unknown_clip_rejected() -> None:
    bus = _delivery_bus()
    cmd = TypedCommand(
        command_id="cmd_cov_exp_unknown",
        operation="color.adjust_exposure",
        input={"clip_asset_id": "clip_missing", "exposure_ev": 0.5, "temperature_k": 5600},
    )
    with pytest.raises(CommandValidationError, match="unknown clip asset"):
        bus.dispatch(cmd)


def test_auto_balance_execution() -> None:
    bus = _delivery_bus()
    cmd = TypedCommand(
        command_id="cmd_cov_autobal",
        operation="color.auto_balance",
        input={"clip_asset_id": "clip_v1", "preserve_skin_tones": False},
    )
    res = bus.dispatch(cmd)
    assert res.status == "applied"
    derived_id = res.output["asset_id"]
    graded = next(a for a in bus.project.assets if a.asset_id == derived_id)
    assert graded.media_kind == "video"
    assert graded.parent_asset_ids == ("clip_v1",)
    assert graded.provenance["auto_balance"] is True
    assert graded.provenance["preserve_skin_tones"] is False
    assert graded.provenance["processor"] == "nagar.color.autobalance.v1"
    assert graded.duration_us == 4_000_000


def test_auto_balance_unknown_clip_rejected() -> None:
    bus = _delivery_bus()
    cmd = TypedCommand(
        command_id="cmd_cov_autobal_unknown",
        operation="color.auto_balance",
        input={"clip_asset_id": "clip_missing"},
    )
    with pytest.raises(CommandValidationError, match="unknown clip asset"):
        bus.dispatch(cmd)


def test_match_shot_unknown_source_rejected() -> None:
    bus = _delivery_bus()
    cmd = TypedCommand(
        command_id="cmd_cov_match_src",
        operation="color.match_shot",
        input={"source_clip_id": "clip_missing", "reference_clip_id": "clip_v1"},
    )
    with pytest.raises(CommandValidationError, match="unknown source clip"):
        bus.dispatch(cmd)


def test_match_shot_unknown_reference_rejected() -> None:
    bus = _delivery_bus()
    cmd = TypedCommand(
        command_id="cmd_cov_match_ref",
        operation="color.match_shot",
        input={"source_clip_id": "clip_v1", "reference_clip_id": "clip_missing"},
    )
    with pytest.raises(CommandValidationError, match="unknown reference clip"):
        bus.dispatch(cmd)


def test_make_proxy_unknown_video_rejected() -> None:
    bus = _delivery_bus()
    cmd = TypedCommand(
        command_id="cmd_cov_proxy_unknown",
        operation="delivery.make_proxy_480p",
        input={"video_asset_id": "clip_missing"},
    )
    with pytest.raises(CommandValidationError, match="unknown video asset"):
        bus.dispatch(cmd)


def test_render_master_4k_handler_requires_confirmation_flag() -> None:
    """Level C envelope passes the bus gate; the handler still fails closed
    when the *input payload* omits the explicit ``confirmed=true`` flag."""
    bus = _delivery_bus()
    cmd = TypedCommand(
        command_id="cmd_cov_master_unconfirmed",
        operation="delivery.render_master_4k",
        confirmed=True,  # envelope-level confirmation (bus permission gate)
        input={"width": 3840, "height": 2160},  # payload confirmed defaults to False
    )
    with pytest.raises(CommandValidationError, match="explicit user confirmation"):
        bus.dispatch(cmd)
