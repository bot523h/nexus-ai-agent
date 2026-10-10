"""P1-C — /stt carries the real Telegram user identity to the metering layer.

Regression for the overnight audit finding: ``stt_cmd`` extracted the caller id
but dropped it before ``SpeechEngine.speech_to_text()``, so
``gemini_engine.vision(user_id=0)`` metered every transcription in the shared
bucket ``0``.  The contract now asserted here:

* ``stt_cmd`` passes the effective user id into ``speech_to_text()``;
* ``speech_to_text()`` forwards that id to ``gemini_engine.vision()``;
* a request from user 424242 charges bucket 424242 — never bucket 0;
* missing identity fails closed before any provider call (no anonymous bucket);
* success/failure dict shape and MIME mapping stay unchanged.

Only the network layer (``GeminiEngine._call_gemini``) is stubbed: the dispatch
handler, the speech layer, ``vision()`` and the real ``_RateLimiter`` run.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus_ai_agent.bot.handlers import build_handlers
from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.features.ai_chat import GeminiEngine
from nexus_ai_agent.features.speech import SpeechEngine
from nexus_ai_agent.presence import PresenceStore

REAL_USER = 424242


# ── engine layer ──────────────────────────────────────────────────────


def test_speech_to_text_signature_carries_user_id() -> None:
    sig = inspect.signature(SpeechEngine.speech_to_text)
    assert "user_id" in sig.parameters, (
        "SpeechEngine.speech_to_text has no user_id parameter — the caller "
        "identity cannot reach the provider layer at all"
    )


async def test_speech_to_text_forwards_identity_to_vision(tmp_path: Path) -> None:
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"OggS-fake-audio")

    captured: dict[str, object] = {}

    class _CaptureEngine:
        is_configured = True

        async def vision(self, image_bytes, *, question="", user_id=None, mime_type=""):
            captured["user_id"] = user_id
            captured["mime_type"] = mime_type
            return "hello world"

    engine = SpeechEngine(output_dir=str(tmp_path / "audio"))
    result = await engine.speech_to_text(
        str(audio), lang="fa", gemini_engine=_CaptureEngine(), user_id=REAL_USER
    )
    assert result["success"] is True
    assert result["text"] == "hello world"
    assert captured.get("user_id") == REAL_USER, (
        f"speech_to_text() dropped the caller identity — vision() got "
        f"user_id={captured.get('user_id')!r}, expected {REAL_USER}"
    )


async def test_speech_to_text_fails_closed_without_identity(tmp_path: Path) -> None:
    """No valid identity ⇒ abort before the provider call; never bucket 0."""
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"OggS-fake-audio")

    class _BoomEngine:
        is_configured = True

        async def vision(self, *args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("vision() must not be called without a valid user id")

    engine = SpeechEngine(output_dir=str(tmp_path / "audio"))
    for missing in (None, 0):
        result = await engine.speech_to_text(
            str(audio), lang="fa", gemini_engine=_BoomEngine(), user_id=missing
        )
        assert result["success"] is False, f"user_id={missing!r} must fail closed"
        assert result["text"] == ""


# ── dispatch layer (the registered /stt handler) ──────────────────────


def _build_stt_callback(monkeypatch: pytest.MonkeyPatch):
    import os

    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
    engines_seen: list[GeminiEngine] = []
    original_init = GeminiEngine.__init__

    def _spy_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        engines_seen.append(self)

    monkeypatch.setattr(GeminiEngine, "__init__", _spy_init)
    handlers = build_handlers(
        object(),
        lambda: None,
        Settings(gemini_api_key="test-key"),
        PresenceStore(),
        object(),
    )
    for h in handlers:
        if hasattr(h, "commands") and "stt" in getattr(h, "commands", set()):
            return h.callback, engines_seen
    raise AssertionError("no /stt CommandHandler registered")


async def test_stt_dispatch_meters_the_real_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # SpeechEngine writes data/audio relative to CWD

    async def _fake_call_gemini(self, contents, system_instruction="") -> str:
        return "transcribed text"

    monkeypatch.setattr(GeminiEngine, "_call_gemini", _fake_call_gemini)

    captured: dict[str, object] = {}
    original_vision = GeminiEngine.vision

    async def _spy_vision(self, image_bytes, *, question="", user_id=0, mime_type=""):
        captured["user_id"] = user_id
        return await original_vision(
            self, image_bytes, question=question, user_id=user_id, mime_type=mime_type
        )

    monkeypatch.setattr(GeminiEngine, "vision", _spy_vision)

    callback, engines_seen = _build_stt_callback(monkeypatch)

    replies: list[str] = []

    class _File:
        async def download_as_bytearray(self) -> bytearray:
            return bytearray(b"OggS-fake-audio")

    class _Voice:
        mime_type = "audio/ogg"

        async def get_file(self):
            return _File()

    msg = SimpleNamespace(
        text=None,
        reply_to_message=SimpleNamespace(voice=_Voice(), audio=None),
        voice=None,
    )

    async def _reply_text(text, **kwargs):
        replies.append(text)

    msg.reply_text = _reply_text
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=REAL_USER),
        effective_chat=SimpleNamespace(id=REAL_USER),
        message=msg,
        edited_message=None,
        callback_query=None,
    )
    await callback(update, SimpleNamespace(args=[]))

    assert any("Transcription" in r for r in replies), f"no transcription: {replies}"
    assert engines_seen, "the /stt handler did not build a GeminiEngine"
    limiter = engines_seen[-1]._limiter
    rem_user = limiter.remaining(REAL_USER)
    rem_zero = limiter.remaining(0)
    assert rem_user["daily_remaining"] == 1500 - 1, (
        f"the /stt request must be metered against user {REAL_USER}: "
        f"remaining(user)={rem_user}; remaining(0)={rem_zero}; "
        f"vision() saw user_id={captured.get('user_id')!r}"
    )
    assert rem_zero["daily_remaining"] == 1500, (
        f"bucket 0 must stay untouched for a real-user request: {rem_zero}"
    )


async def test_stt_dispatch_fails_closed_for_anonymous_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An update without a user id must not reach the provider at all."""
    monkeypatch.chdir(tmp_path)

    async def _boom(self, contents, system_instruction="") -> str:  # pragma: no cover
        raise AssertionError("provider must not be called for an anonymous update")

    monkeypatch.setattr(GeminiEngine, "_call_gemini", _boom)
    callback, engines_seen = _build_stt_callback(monkeypatch)

    replies: list[str] = []

    class _File:
        async def download_as_bytearray(self) -> bytearray:
            return bytearray(b"OggS-fake-audio")

    class _Voice:
        mime_type = "audio/ogg"

        async def get_file(self):
            return _File()

    msg = SimpleNamespace(
        text=None,
        reply_to_message=SimpleNamespace(voice=_Voice(), audio=None),
        voice=None,
    )

    async def _reply_text(text, **kwargs):
        replies.append(text)

    msg.reply_text = _reply_text
    update = SimpleNamespace(
        effective_user=None,
        effective_chat=SimpleNamespace(id=1),
        message=msg,
        edited_message=None,
        callback_query=None,
    )
    await callback(update, SimpleNamespace(args=[]))
    assert not any("Transcription" in r for r in replies), (
        f"anonymous update must not be transcribed: {replies}"
    )


# ── human quota paths: handler-level fail-closed (no or-0 sentinel) ───


async def test_ai_code_translate_handlers_fail_closed_without_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/ai, /code, /translate must not reach the engine without a real user id.

    The ``_user_id(update) or 0`` fallback is the shared-bucket sentinel: it
    must never book quota or touch the provider.  A missing identity fails
    closed at the handler with an error reply (same policy as /stt).
    """
    calls: list[str] = []

    async def _boom_chat(self, text, **kwargs):
        calls.append(f"chat:{kwargs.get('user_id')!r}")
        return "should-not-happen"

    async def _boom_code(self, prompt, **kwargs):
        calls.append(f"code:{kwargs.get('user_id')!r}")
        return "should-not-happen"

    async def _boom_translate(self, text, **kwargs):
        calls.append(f"translate:{kwargs.get('user_id')!r}")
        return "should-not-happen"

    monkeypatch.setattr(GeminiEngine, "chat", _boom_chat)
    monkeypatch.setattr(GeminiEngine, "code", _boom_code)
    monkeypatch.setattr(GeminiEngine, "translate", _boom_translate)

    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
    handlers = build_handlers(
        object(),
        lambda: None,
        Settings(gemini_api_key="test-key"),
        PresenceStore(),
        object(),
    )
    wanted = {"ai", "code", "translate"}
    callbacks = {}
    for h in handlers:
        cmds = getattr(h, "commands", set())
        for name in wanted:
            if name in cmds:
                callbacks[name] = h.callback
    assert set(callbacks) == wanted, f"missing handlers: {wanted - set(callbacks)}"

    replies: list[str] = []

    async def _reply_text(text, **kwargs):
        replies.append(text)

    msg = SimpleNamespace(text="/ai hello", reply_text=_reply_text)
    update = SimpleNamespace(
        effective_user=None,
        effective_chat=SimpleNamespace(id=1),
        message=msg,
        edited_message=None,
        callback_query=None,
    )
    for cb in callbacks.values():
        await cb(update, SimpleNamespace(args=["hello"]))
    assert calls == [], (
        f"identity-less updates reached the engine layer with {calls!r} — the or-0 sentinel is back"
    )
    assert any("هویت" in r or "identity" in r.lower() for r in replies), (
        f"fail-closed must tell the user, not silently drop: {replies!r}"
    )


async def test_ai_handler_with_real_identity_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity: a real identity still flows through /ai (no over-blocking)."""
    seen: list[int | None] = []

    async def _chat(self, text, **kwargs):
        seen.append(kwargs.get("user_id"))
        return "answer"

    monkeypatch.setattr(GeminiEngine, "chat", _chat)
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
    handlers = build_handlers(
        object(),
        lambda: None,
        Settings(gemini_api_key="test-key"),
        PresenceStore(),
        object(),
    )
    cb = next(h.callback for h in handlers if "ai" in getattr(h, "commands", set()))
    replies: list[str] = []

    async def _reply_text(text, **kwargs):
        replies.append(text)

    msg = SimpleNamespace(text="/ai hello", reply_text=_reply_text)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=REAL_USER),
        effective_chat=SimpleNamespace(id=REAL_USER),
        message=msg,
        edited_message=None,
        callback_query=None,
    )
    await cb(update, SimpleNamespace(args=["hello"]))
    assert seen == [REAL_USER], f"/ai must pass the real identity: {seen!r}"
