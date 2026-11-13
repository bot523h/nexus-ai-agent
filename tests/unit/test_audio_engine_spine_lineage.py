"""Private-byte publication: real FFmpeg, signatures, held fences and no public leaks."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace

import omega_media as media
import pytest

from nexus_ai_agent.creative.audio_engine import spine_lineage as spine
from nexus_ai_agent.creative.audio_engine.ed25519_sign import derive_public_key
from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import FfmpegTranscoder
from nexus_ai_agent.creative.audio_engine.models import CanonicalStreamProfile
from nexus_ai_agent.creative.audio_engine.process import CancelToken, ProcessCancelled
from nexus_ai_agent.creative.audio_engine.verify import PassportKeyring
from nexus_ai_agent.creative.audio_engine.workspace import (
    AtomicPublishError,
    RuntimeWorkspace,
    hash_file,
)


class HeldFence:
    """Test host authority. Invalidation takes the SAME lock as publication."""

    def __init__(self):
        self.lock = threading.RLock()
        self.token = 7
        self.held = False
        self.on_enter = None
        self.events = []

    @contextmanager
    def hold(self, identity):
        with self.lock:
            self.held = True
            self.events.append("hold")
            try:
                if self.on_enter:
                    self.on_enter()
                yield
            finally:
                self.held = False
                self.events.append("release")

    def assert_active(self, identity):
        assert self.held
        self.events.append("check")
        if identity.fencing_token != self.token:
            raise spine.AudioPublishRefused("stale fencing token")


class AppendOnlyWriter:
    def __init__(self, fence):
        self.fence = fence
        self.rows = []
        self.fail = False

    def record_asset_version(self, record, *, passport_json, publication):
        assert self.fence.held
        assert publication.path.is_file()
        assert publication.sha256 == record.output_sha256
        self.fence.events.append("lineage")
        if self.fail:
            raise RuntimeError("canonical writer unavailable")
        self.rows.append((record, passport_json))


class PublishHarness:
    def __init__(self, root):
        self.source = media.write_sine(root / "source.wav", seconds=2)
        self.workspace = RuntimeWorkspace(root / "work").prepare()
        self.destination = self.workspace.media_dir / "version.flac"
        self.engine = FfmpegTranscoder()
        self.fence = HeldFence()
        self.writer = AppendOnlyWriter(self.fence)
        self.seed = bytes(range(32))
        self.identity = spine.AudioPublishIdentity(
            "project",
            "revision-1",
            "revision-2",
            "attempt-7",
            7,
            "sha256:" + "a" * 64,
            hash_file(self.source),
        )
        self.produced = None
        self.on_render = None

    def render(self, source, directory):
        assert not self.destination.exists()
        self.produced = directory / "render.flac"
        self.engine.render(
            source=source,
            destination=self.produced,
            audio_filters=(),
            profile_args=CanonicalStreamProfile().format_arguments(),
            container_hint="flac",
        )
        if self.on_render:
            self.on_render(source, self.produced)
        return self.produced

    def publish(self, **overrides):
        kwargs = dict(
            workspace=self.workspace,
            source=self.source,
            destination=self.destination,
            identity=self.identity,
            operation="audio.test",
            asset_id="asset",
            render=self.render,
            engine=self.engine,
            fence=self.fence,
            lineage=self.writer,
            signing_key=self.seed,
            signer_key_id="test-key",
            keyring=PassportKeyring({"test-key": derive_public_key(self.seed)}),
        )
        kwargs.update(overrides)
        return spine.execute_fenced_staged_audio_publish(**kwargs)

    def assert_refused_cleanly(self):
        assert not self.destination.exists()
        assert self.writer.rows == []
        assert self.workspace.leftovers() == []


@pytest.fixture
def publisher(tmp_path):
    return PublishHarness(tmp_path)


def test_signed_private_bytes_publish_then_emit_revision_bound_immutable_lineage(publisher):
    receipt = publisher.publish()
    record = receipt.asset_version
    assert receipt.publication.path == publisher.destination
    assert record.output_sha256 == hash_file(publisher.destination)
    assert record.input_sha256 == hash_file(publisher.source)
    assert record.project_id == "project"
    assert (record.parent_revision_id, record.produced_revision_id) == ("revision-1", "revision-2")
    assert (record.attempt_id, record.fencing_token) == ("attempt-7", 7)
    assert record.recipe_digest == publisher.identity.recipe_digest
    assert record.measured_lufs < -10 and record.measured_true_peak_dbtp < -1
    assert record.verification_tier == spine.VERIFICATION_TIER
    passport = spine.AudioArtifactPassport.model_validate_json(receipt.passport_json)
    assert passport.payload_sha256 == record.passport_digest
    assert passport.transactional_identity == publisher.identity.binding
    assert publisher.writer.rows == [(record, receipt.passport_json)]
    assert publisher.fence.events == ["hold", "check", "lineage", "release"]
    assert record.version_id == replace(record).version_id
    assert replace(record, produced_revision_id="revision-3").version_id != record.version_id
    assert replace(record, recipe_digest="sha256:" + "b" * 64).version_id != record.version_id
    assert replace(record, passport_digest="sha256:" + "b" * 64).version_id != record.version_id
    with pytest.raises(FrozenInstanceError):
        record.measured_lufs = 0
    assert publisher.workspace.leftovers() == []


def test_stale_fence_has_zero_public_bytes_even_after_successful_verification(
    publisher, monkeypatch
):
    publisher.fence.token = 8
    monkeypatch.setattr(
        publisher.workspace,
        "publish",
        lambda *a, **kw: pytest.fail("stale attempt reached publication"),
    )
    with pytest.raises(spine.AudioPublishRefused, match="stale fencing token"):
        publisher.publish()
    publisher.assert_refused_cleanly()


def test_tampered_private_bytes_after_verify_are_never_published(publisher, monkeypatch):
    monkeypatch.setattr(
        publisher.workspace,
        "publish",
        lambda *a, **kw: pytest.fail("unverified bytes reached publication"),
    )

    def tamper():
        assert publisher.produced is not None
        publisher.produced.write_bytes(b"unverified staged bytes")

    publisher.fence.on_enter = tamper
    with pytest.raises(spine.AudioPublishRefused, match="changed after verification"):
        publisher.publish()
    publisher.assert_refused_cleanly()


def test_canonical_verify_passport_failure_cannot_emit_a_version(publisher):
    with pytest.raises(spine.AudioPublishRefused, match="private passport verification refused"):
        publisher.publish(keyring=PassportKeyring({}))
    publisher.assert_refused_cleanly()


@pytest.mark.parametrize("missing_pcm", [False, True])
def test_untargeted_publish_must_earn_independent_meter_verification(
    publisher, monkeypatch, missing_pcm
):
    decode = spine.decode_for_analysis

    def failed_measurement(*args, **kwargs):
        assert not publisher.destination.exists()
        samples, rate, channels = decode(*args, **kwargs)
        # An untargeted passport normally skips verify_passport's target check.
        # It must still not confer CROSS_VERIFIED on absent or silent PCM.
        return (None if missing_pcm else samples * 0), rate, channels

    monkeypatch.setattr(spine, "decode_for_analysis", failed_measurement)
    with pytest.raises(spine.AudioPublishRefused, match="private loudness cross-verification"):
        publisher.publish()
    publisher.assert_refused_cleanly()


def test_writer_failure_retracts_only_this_attempts_published_name(publisher):
    publisher.writer.fail = True
    with pytest.raises(RuntimeError, match="writer unavailable"):
        publisher.publish()
    publisher.assert_refused_cleanly()


def test_existing_version_is_not_overwritten(publisher):
    publisher.destination.write_bytes(b"previous verified version")
    # The normal renderer explicitly asserts no previous output: use a wrapper
    # to render in isolation while preserving the existing immutable name.
    original = publisher.destination

    def render(source, directory):
        target = directory / "render.flac"
        publisher.engine.render(
            source=source,
            destination=target,
            audio_filters=(),
            profile_args=CanonicalStreamProfile().format_arguments(),
            container_hint="flac",
        )
        return target

    with pytest.raises(AtomicPublishError, match="already exists"):
        publisher.publish(render=render)
    assert original.read_bytes() == b"previous verified version"
    assert publisher.writer.rows == []
    assert publisher.workspace.leftovers() == []


@pytest.mark.parametrize("when", ["before", "after_render", "at_fence"])
def test_cancellation_has_zero_public_bytes(publisher, when):
    token = CancelToken()
    if when == "before":
        token.cancel()
    elif when == "after_render":
        publisher.on_render = lambda *args: token.cancel()
    else:
        publisher.fence.on_enter = token.cancel
    with pytest.raises(ProcessCancelled):
        publisher.publish(cancel=token)
    publisher.assert_refused_cleanly()


@pytest.mark.parametrize("attack", ["outside", "empty", "input", "staging_copy"])
def test_renderer_and_input_identity_refusals(publisher, monkeypatch, attack):
    if attack == "outside":
        overrides = {"render": lambda source, directory: source}
    elif attack == "staging_copy":
        original = publisher.workspace.stage_input

        def corrupt(*args, **kwargs):
            path = original(*args, **kwargs)
            path.write_bytes(b"tamper while copying")
            return path

        monkeypatch.setattr(publisher.workspace, "stage_input", corrupt)
        overrides = {}
    else:

        def corrupt(source, path):
            (source if attack == "input" else path).write_bytes(
                b"tamper" if attack == "input" else b""
            )

        publisher.on_render = corrupt
        overrides = {}
    with pytest.raises(spine.AudioPublishRefused):
        publisher.publish(**overrides)
    publisher.assert_refused_cleanly()


def test_no_publish_outside_media_namespace(publisher):
    with pytest.raises(spine.AudioPublishRefused, match="inside media"):
        publisher.publish(destination=publisher.workspace.root / "inputs" / "not-output.flac")
    publisher.assert_refused_cleanly()


@pytest.mark.parametrize("attack", ["verified", "binding", "loudness", "peak", "publication"])
def test_defensive_evidence_bindings_are_fail_closed(publisher, monkeypatch, attack):
    build = spine.build_passport
    verify = spine.verify_passport

    def changed(*args, **kwargs):
        passport = build(*args, **kwargs)
        if attack == "verified":
            return passport.model_copy(update={"verified": False})
        if attack == "binding":
            return passport.model_copy(update={"transactional_identity": {"project_id": "other"}})
        if attack in ("loudness", "peak"):
            measurement = (
                None
                if attack == "loudness"
                else passport.loudness.model_copy(update={"true_peak_dbtp": None})
            )
            return passport.model_copy(update={"loudness": measurement})
        return passport

    def accept(*args, **kwargs):
        # Independently prove that a broken/compromised verifier is insufficient
        # to bypass the adapter's binding checks. Main verification tests above
        # use the actual signature, byte and independent metering paths.
        report = verify(*args, **kwargs)
        return report.model_copy(update={"passed": True})

    monkeypatch.setattr(spine, "build_passport", changed)
    monkeypatch.setattr(spine, "verify_passport", accept)
    if attack == "publication":
        publish = publisher.workspace.publish

        def changed_publish(*args, **kwargs):
            return replace(publish(*args, **kwargs), sha256="sha256:" + "0" * 64)

        monkeypatch.setattr(publisher.workspace, "publish", changed_publish)
    with pytest.raises(spine.AudioPublishRefused):
        publisher.publish()
    publisher.assert_refused_cleanly()


@pytest.mark.parametrize(
    "changes",
    [
        {"project_id": ""},
        {"parent_revision_id": "\x00"},
        {"produced_revision_id": "revision-1"},
        {"attempt_id": " "},
        {"fencing_token": 0},
        {"fencing_token": True},
        {"fencing_token": 1.2},
        {"recipe_digest": "request-not-content"},
        {"input_sha256": "a" * 64},
    ],
)
def test_lineage_identity_validation(changes):
    identity = spine.AudioPublishIdentity(
        "project",
        "revision-1",
        "revision-2",
        "attempt",
        1,
        "sha256:" + "a" * 64,
        "sha256:" + "b" * 64,
    )
    with pytest.raises(ValueError):
        replace(identity, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"asset_id": ""},
        {"output_sha256": "not-content"},
        {"passport_digest": "not-passport"},
        {"measured_lufs": float("nan")},
        {"measured_true_peak_dbtp": float("inf")},
        {"verification_tier": "TRUST_ME"},
    ],
)
def test_asset_version_validation(changes):
    record = spine.AudioAssetVersionRecord(
        "project",
        "r1",
        "r2",
        "attempt",
        1,
        "sha256:" + "a" * 64,
        "sha256:" + "b" * 64,
        "asset",
        "sha256:" + "c" * 64,
        "sha256:" + "d" * 64,
        -24,
        -1,
    )
    with pytest.raises(ValueError):
        replace(record, **changes)
    assert json.loads(json.dumps(record.binding))["project_id"] == "project"
