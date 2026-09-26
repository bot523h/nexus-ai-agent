"""Telegram surface for factual runtime status: ``/model``, ``/storage``, ``/story_style``.

What this replaces
------------------
Three commands in ``bot/handlers.py`` that were registered — so Telegram
advertised them — and then did nothing, or worse::

    async def storage_cmd(update, context) -> None:
        pass                      # registered at handlers.py:1304, silent

    async def model_cmd(update, context) -> None:
        pass                      # registered at handlers.py:1305, silent

    async def story_style_cmd(update, context) -> None:
        await _reply(update, "🎨 استایل فعلی: Motivational\\n"
                             "گزینه‌ها: Motivational | Romantic | Success")

``pass`` is the worst possible failure mode for a chat command: the user gets
*no* reply, which is indistinguishable from the bot being down. There is no
error, no log line, nothing to debug against.

``/story_style`` is the opposite failure: a confident answer that is false.
``AIStoryGenerator.create_story()`` opens with ``_ = style`` and then renders a
single hard-coded look (``#0f172a`` background, Vazirmatn, centred RTL text).
The three offered styles do not exist anywhere in the renderer, so no value the
user could pick would change a single pixel.

Decisions a reader should be able to find
-----------------------------------------
* **Every command answers.** Silence is not an acceptable response.
* **Facts only, derived at call time.** ``/model`` renders
  :func:`~nexus_ai_agent.llm.litellm_provider.build_routing_chain` — the very
  function ``build_llm_provider`` uses — so the reply cannot drift from the
  chain that actually serves requests. ``/storage`` reports which cloud
  providers hold credentials *right now*.
* **Never print a secret.** Only :attr:`Deployment.name` and
  :attr:`Deployment.litellm_model` are shown; ``api_key`` is not read. Cloud
  providers are reported as configured / not configured — never the token.
* **``/story_style`` tells the truth about the renderer** and says the styles
  are not implemented, rather than presenting a menu that does nothing.
* **``telegram`` is never imported** (see :mod:`._ptb`).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from nexus_ai_agent.config.settings import Settings, get_settings

from ._ptb import reply

__all__ = [
    "format_model_status",
    "format_storage_status",
    "format_story_style",
    "model_cmd",
    "storage_cmd",
    "story_style_cmd",
]

_SEPARATOR = "━━━━━━━━━━━━━━━━"

#: Exact strings/behaviours ``bot/handlers.py`` used to have. The empty string
#: marks the two commands whose whole body was ``pass``;
#: ``tests/unit/test_surface_registration.py`` additionally asserts that no
#: registered handler has an empty body.
STUB_STRINGS = ("🎨 استایل فعلی: Motivational\nگزینه‌ها: Motivational | Romantic | Success",)

#: Cloud settings field → display name, in the order ``UnifiedCloudStorage``
#: prefers them. Values are *field names*, never the secrets they hold.
_CLOUD_FIELDS: tuple[tuple[str, str], ...] = (
    ("mega_email", "MEGA"),
    ("dropbox_token", "Dropbox"),
    ("pcloud_token", "pCloud"),
    ("internxt_token", "Internxt"),
    ("rclone_remote", "Google Drive (rclone)"),
    ("github_token", "GitHub Releases"),
)


def _dir_size_mb(path: Path) -> float | None:
    """Best-effort size of ``path`` in MiB; ``None`` when it does not exist."""
    try:
        if path.is_file():
            return path.stat().st_size / 1_048_576
        if path.is_dir():
            total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            return total / 1_048_576
    except OSError:
        return None
    return None


# ── pure rendering ─────────────────────────────────────────────────────────


def format_model_status(chain: list[tuple[str, str]], *, routing_enabled: bool) -> str:
    """Render the resolved LLM routing chain. ``chain`` is ``(name, model)``."""
    lines = ["🧠 موتور زبانی", _SEPARATOR]
    if not routing_enabled:
        lines.append("  مسیریابی چندارائه‌دهنده: غیرفعال (NEXUS_LLM_ROUTING_ENABLED=false)")
    if not chain:
        lines.append("  هیچ ارائه‌دهنده‌ای پیکربندی نشده است.")
        lines.append("  ربات روی FakeLLM کار می‌کند: پاسخ‌ها قطعی و آفلاین‌اند.")
        lines.append(_SEPARATOR)
        lines.append("برای فعال‌سازی: GROQ_API_KEY یا GEMINI_API_KEY یا OLLAMA_MODEL را تنظیم کنید.")
        return "\n".join(lines)
    lines.append(f"  زنجیرهٔ اولویت ({len(chain)} ارائه‌دهنده):")
    for position, (name, model) in enumerate(chain, start=1):
        lines.append(f"   {position}. {name} → {model}")
    lines.append("  لایهٔ بیرونی: FakeLLM (وقتی همهٔ موارد بالا سهمیه‌شان تمام شود)")
    lines.append(_SEPARATOR)
    lines.append("ℹ️ کلیدهای API هرگز نمایش داده نمی‌شوند.")
    return "\n".join(lines)


def format_storage_status(
    *,
    db_path: str,
    db_mb: float | None,
    cache_dir: str,
    cache_mb: float | None,
    clouds: list[tuple[str, bool]],
) -> str:
    """Render local storage footprint plus which cloud backends are configured."""
    missing = " (ساخته نشده)"
    db_size = f" ({db_mb:.1f} MB)" if db_mb is not None else missing
    cache_size = f" ({cache_mb:.1f} MB)" if cache_mb is not None else missing
    lines = ["💾 وضعیت ذخیره‌سازی", _SEPARATOR, "  محلی:"]
    lines.append(f"   پایگاه داده: {db_path}{db_size}")
    lines.append(f"   کش: {cache_dir}{cache_size}")
    configured = [name for name, ok in clouds if ok]
    lines.append("  ابری:")
    if configured:
        for name, ok in clouds:
            lines.append(f"   {'✅' if ok else '➖'} {name}")
    else:
        lines.append("   هیچ ارائه‌دهندهٔ ابری پیکربندی نشده — همه چیز فقط محلی است.")
    lines.append(_SEPARATOR)
    lines.append("ℹ️ توکن‌ها نمایش داده نمی‌شوند؛ فقط پیکربندی‌شده/نشده.")
    return "\n".join(lines)


def format_story_style() -> str:
    """State what the story renderer actually does — one fixed look."""
    return "\n".join(
        [
            "🎨 استایل استوری",
            _SEPARATOR,
            "  رندرر فعلی یک ظاهر ثابت دارد:",
            "   • پس‌زمینهٔ تیره (#0f172a)، ۱۰۸۰×۱۹۲۰",
            "   • فونت وزیرمتن، متن فارسی راست‌چین و وسط‌چین",
            _SEPARATOR,
            "⚠️ انتخاب استایل هنوز پیاده‌سازی نشده است:",
            "   AIStoryGenerator.create_story پارامتر style را نادیده می‌گیرد.",
            "   این پیام عمداً گزینه‌ای پیشنهاد نمی‌کند تا وعدهٔ بی‌پشتوانه ندهد.",
            "برای ساخت استوری: /story <متن>",
        ]
    )


# ── commands ───────────────────────────────────────────────────────────────


async def model_cmd(update: Any, context: Any) -> None:
    """Report the LLM routing chain that will actually serve the next request."""
    settings = get_settings()

    def _chain() -> list[tuple[str, str]]:
        # Imported lazily: litellm_provider pulls the whole llm package, and
        # build_routing_chain is pure settings → deployments (no litellm import).
        from nexus_ai_agent.llm.litellm_provider import build_routing_chain

        return [(d.name, d.litellm_model) for d in build_routing_chain(settings)]

    try:
        chain = await asyncio.to_thread(_chain)
    except Exception as exc:  # noqa: BLE001 - never leave the command silent
        await reply(update, f"❌ خواندن زنجیرهٔ مدل ناموفق بود: {exc}")
        return
    await reply(
        update, format_model_status(chain, routing_enabled=bool(settings.llm_routing_enabled))
    )


async def storage_cmd(update: Any, context: Any) -> None:
    """Report real on-disk footprint and which cloud backends are configured."""
    settings: Settings = get_settings()

    def _collect() -> tuple[float | None, float | None, list[tuple[str, bool]]]:
        db_mb = _dir_size_mb(Path(settings.db_path))
        cache_mb = _dir_size_mb(Path(settings.cache_dir))
        clouds = [(label, bool(getattr(settings, field, None))) for field, label in _CLOUD_FIELDS]
        return db_mb, cache_mb, clouds

    try:
        db_mb, cache_mb, clouds = await asyncio.to_thread(_collect)
    except Exception as exc:  # noqa: BLE001 - never leave the command silent
        await reply(update, f"❌ خواندن وضعیت ذخیره‌سازی ناموفق بود: {exc}")
        return
    await reply(
        update,
        format_storage_status(
            db_path=settings.db_path,
            db_mb=db_mb,
            cache_dir=settings.cache_dir,
            cache_mb=cache_mb,
            clouds=clouds,
        ),
    )


async def story_style_cmd(update: Any, context: Any) -> None:
    """Describe the single real story style instead of offering three fake ones."""
    await reply(update, format_story_style())
