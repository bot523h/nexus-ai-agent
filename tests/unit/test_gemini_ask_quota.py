"""P1-B — GeminiEngine.ask() must account quota exactly like its sibling paths.

Regression for the overnight audit finding: ``ask()`` consulted
``is_allowed()`` but never called ``record()``, so the per-user quota was never
consumed on that path (``chat()``/``translate()``/``summarize()``/``code()``
all record).  The contract asserted here:

* a successful ``ask()`` records exactly one unit (``max_daily=1`` ⇒ second is
  rejected);
* a provider failure leaves the quota untouched (no fake success);
* a blocked request creates no count;
* the queued path and the direct path each count exactly once — no double count;
* the Telegram ``/ask`` command still routes through the recorded ``chat()``
  path (the engine-level ``ask()`` is reached via ``LLMProvider.generate()``).
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus_ai_agent.features.ai_chat import GeminiEngine


@pytest.fixture()
def engine(monkeypatch: pytest.MonkeyPatch) -> GeminiEngine:
    eng = GeminiEngine(api_key="test-key", max_rpm=1, max_daily=1)

    async def _fake_call(contents: Any, system_instruction: str = "") -> str:
        return "OK"

    monkeypatch.setattr(eng, "_call_gemini", _fake_call)
    return eng


# ── the core accounting bug ───────────────────────────────────────────


async def test_ask_records_the_first_success_and_rejects_the_second(
    engine: GeminiEngine,
) -> None:
    first = await engine.ask("one", user_id=42)
    second = await engine.ask("two", user_id=42)
    assert first == "OK"
    assert second != "OK", (
        "rate limiter bypass on ask(): max_daily=1 but the second request "
        "was served — record() is missing on this path"
    )


async def test_ask_consumes_the_user_quota(engine: GeminiEngine) -> None:
    await engine.ask("one", user_id=42)
    rem = engine._limiter.remaining(42)
    assert rem["daily_remaining"] == 0 and rem["rpm_remaining"] == 0, (
        f"ask() did not record usage for user 42: remaining={rem}"
    )


async def test_blocked_ask_creates_no_count(engine: GeminiEngine) -> None:
    await engine.ask("one", user_id=42)  # consumes the single allowed unit
    rem_before = engine._limiter.remaining(42)
    await engine.ask("two", user_id=42)  # blocked
    rem_after = engine._limiter.remaining(42)
    assert rem_before == rem_after, (
        f"a blocked request must not create a successful count: {rem_before} -> {rem_after}"
    )


async def test_provider_error_is_not_recorded(
    engine: GeminiEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(contents: Any, system_instruction: str = "") -> str:
        raise RuntimeError("provider down")

    monkeypatch.setattr(engine, "_call_gemini", _boom)
    with pytest.raises(RuntimeError):
        await engine.ask("one", user_id=42)
    rem = engine._limiter.remaining(42)
    assert rem["daily_remaining"] == 1 and rem["rpm_remaining"] == 1, (
        f"a failed provider call must not count as usage: remaining={rem}"
    )


# ── no double count across the direct and queued paths ────────────────


class _OneShotQueue:
    """Minimal stand-in for GeminiRequestQueue: runs the submit() lambda once."""

    async def submit(self, fn, *, user_id: int, priority: Any = None) -> Any:
        _ = (user_id, priority)
        return await fn()


async def test_queued_ask_counts_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = GeminiEngine(api_key="test-key", max_rpm=5, max_daily=5, request_queue=_OneShotQueue())

    async def _fake_call(contents: Any, system_instruction: str = "") -> str:
        return "OK"

    monkeypatch.setattr(eng, "_call_gemini", _fake_call)
    await eng.ask("one", user_id=42)
    rem = eng._limiter.remaining(42)
    assert rem["daily_remaining"] == 4 and rem["rpm_remaining"] == 4, (
        f"the queued path must count exactly once: remaining={rem}"
    )


async def test_direct_ask_counts_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = GeminiEngine(api_key="test-key", max_rpm=5, max_daily=5)

    async def _fake_call(contents: Any, system_instruction: str = "") -> str:
        return "OK"

    monkeypatch.setattr(eng, "_call_gemini", _fake_call)
    await eng.ask("one", user_id=42)
    rem = engine_remaining = eng._limiter.remaining(42)
    assert engine_remaining["daily_remaining"] == 4, (
        f"the direct path must count exactly once: remaining={rem}"
    )


# ── controls: the sibling paths and /ask dispatch stay intact ─────────


async def test_chat_sibling_still_records_once(engine: GeminiEngine) -> None:
    await engine.chat("one", conv_id="t", user_id=42)
    rem = engine._limiter.remaining(42)
    assert rem["daily_remaining"] == 0, f"chat() must keep recording: remaining={rem}"


async def test_two_users_keep_independent_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = GeminiEngine(api_key="test-key", max_rpm=1, max_daily=1)

    async def _fake_call(contents: Any, system_instruction: str = "") -> str:
        return "OK"

    monkeypatch.setattr(eng, "_call_gemini", _fake_call)
    await eng.ask("one", user_id=42)
    second_user = await eng.ask("hello", user_id=43)
    assert second_user == "OK", "per-user buckets must not interfere"


async def test_telegram_ask_command_still_routes_through_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    from types import SimpleNamespace

    from nexus_ai_agent.bot import handlers as handlers_module
    from nexus_ai_agent.config.settings import Settings
    from nexus_ai_agent.presence import PresenceStore

    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
    seen: dict[str, str] = {}

    async def _spy_chat(self, text, *, conv_id="", user_id=0):
        seen["path"] = "chat"
        return "OK"

    async def _spy_ask(self, text, *, user_id=0):
        seen["path"] = "ask"
        return "OK"

    monkeypatch.setattr(GeminiEngine, "chat", _spy_chat)
    monkeypatch.setattr(GeminiEngine, "ask", _spy_ask)

    built = handlers_module.build_handlers(
        object(),
        lambda: None,
        Settings(gemini_api_key="test-key"),
        PresenceStore(),
        object(),
    )
    ask_cb = None
    for h in built:
        if hasattr(h, "commands") and "ask" in getattr(h, "commands", set()):
            ask_cb = h.callback
    assert ask_cb is not None, "no /ask CommandHandler registered"

    msg = SimpleNamespace(text="/ask hello", reply_text=None)

    async def _reply_text(text, **kwargs):
        pass

    msg.reply_text = _reply_text
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42),
        message=msg,
        edited_message=None,
        callback_query=None,
    )
    await ask_cb(update, SimpleNamespace(args=["hello"]))
    assert seen.get("path") == "chat", (
        f"/ask must route through chat() (the recorded path); observed {seen}"
    )
