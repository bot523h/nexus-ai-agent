"""Telegram surface for the viral content engine (``features/viral_engine.py``).

Commands: ``/viral_preview``, ``/viral_stats``, ``/viral_post``.

What this replaces
------------------
Three commands in ``bot/handlers.py`` answered with invented numbers::

    async def viral_preview_cmd(...) -> "🔥 Preview: Top AI trends of the week..."
    async def viral_stats_cmd(...)   -> "🔥 Viral Engine: 12 posts sent, 450 likes total."
    async def viral_post_cmd(...)    -> "📋 Pending viral posts: 3 in queue."

``12 posts sent`` and ``3 in queue`` were constants: the answer never changed,
whether the queue held zero rows or nine hundred. ``450 likes total`` was worse
than wrong — **no column in the schema records likes at all** (``ViralPost``
stores ``chat_id``, ``text``, ``viral_score``, ``status``, ``posted_at``), so
the number could not have been produced by any query. The same class of defect
as ``"📊 Ad Stats: 5k impressions, 200 clicks."``, which
``surface/ads.py`` already removed.

``/viral_now`` was *not* a stub — it already called the real engine — which is
precisely why the drift was invisible: one command in the group was honest and
three were not.

Decisions a reader should be able to find
-----------------------------------------
* **Only metrics the schema can answer.** :func:`format_stats` prints
  ``total``/``pending``/``posted``/``failed`` row counts from
  :meth:`ViralEngine.get_stats` and says plainly that reactions are not
  measured, instead of inventing a likes counter.
* **Preview generates, it does not promise.** ``/viral_preview`` renders a real
  candidate from the template engine together with its computed viral score,
  and states that nothing was saved — the old text implied a scheduled post.
* **Chat-scoped.** Stats and the pending queue are read for
  ``chat_id(update)``; viral rows are per chat.
* **Owner-gated writes, open reads.** ``/viral_post`` can mark a row as posted,
  so it is owner-only; the listing and the stats are reads.
* **The event loop is never blocked** — the engine is synchronous SQLite CRUD,
  so every call goes through :func:`asyncio.to_thread`.
* **``telegram`` is never imported** (see :mod:`._ptb`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from nexus_ai_agent.features.owner_control import is_owner
from nexus_ai_agent.features.viral_engine import ViralEngine

from ._ptb import args, chat_id, reply, user_id

__all__ = [
    "format_pending",
    "format_preview",
    "format_stats",
    "parse_post_id",
    "viral_post_cmd",
    "viral_preview_cmd",
    "viral_stats_cmd",
]

_SEPARATOR = "━━━━━━━━━━━━━━━━"

#: The exact strings ``bot/handlers.py`` used to answer with; held by
#: ``tests/unit/test_surface_registration.py`` so they cannot come back.
STUB_STRINGS = (
    "🔥 Preview: Top AI trends of the week...",
    "🔥 Viral Engine: 12 posts sent, 450 likes total.",
    "📋 Pending viral posts: 3 in queue.",
)

_DENIED = "⛔ این فرمان فقط برای مالک ربات است."
_NO_CHAT = "⚠️ این فرمان باید داخل یک گفتگو اجرا شود."
#: How many pending rows ``/viral_post`` lists at once.
PENDING_LIMIT = 10


# ── parsing (pure) ─────────────────────────────────────────────────────────


def parse_post_id(command_args: Sequence[str]) -> int | None:
    """Return the ``/viral_post <id>`` argument, or ``None`` for "just list"."""
    for raw in command_args:
        candidate = raw.strip()
        if candidate.isdigit():
            return int(candidate)
    return None


# ── pure rendering ─────────────────────────────────────────────────────────


def format_preview(text: str, score: float) -> str:
    """Render a generated candidate and state that nothing was persisted."""
    return "\n".join(
        [
            f"🔥 پیش‌نمایش پست (امتیاز ویرال: {score:.1f})",
            _SEPARATOR,
            text,
            _SEPARATOR,
            "ℹ️ این متن فقط تولید شد و ذخیره نشده است. برای ذخیره: /viral_now",
        ]
    )


def format_stats(stats: dict[str, int]) -> str:
    """Render :meth:`ViralEngine.get_stats` — row counts only."""
    total = int(stats.get("total", 0))
    if total == 0:
        return "🔥 هنوز هیچ پست ویرالی برای این گفتگو ثبت نشده است. با /viral_now شروع کنید."
    return "\n".join(
        [
            "🔥 آمار موتور ویرال این گفتگو",
            _SEPARATOR,
            f"  کل پست‌ها: {total}",
            f"  در صف: {int(stats.get('pending', 0))}",
            f"  منتشرشده: {int(stats.get('posted', 0))}",
            f"  ناموفق: {int(stats.get('failed', 0))}",
            _SEPARATOR,
            "ℹ️ واکنش/لایک اندازه‌گیری نمی‌شود؛ جدول ViralPost چنین ستونی ندارد.",
        ]
    )


def format_pending(rows: Sequence[dict[str, Any]]) -> str:
    """Render the real pending queue, highest viral score first."""
    if not rows:
        return "📋 صف پست‌های ویرال خالی است. با /viral_now یک پست بسازید."
    lines = [f"📋 پست‌های در صف ({len(rows)} مورد)", _SEPARATOR]
    for row in rows:
        preview = " ".join(str(row.get("text", "")).split())[:60]
        lines.append(f"  #{row.get('id')} · امتیاز {float(row.get('viral_score', 0.0)):.1f}")
        lines.append(f"     {preview}…")
    lines.append(_SEPARATOR)
    lines.append("برای علامت‌زدن به‌عنوان منتشرشده: /viral_post <شناسه>")
    return "\n".join(lines)


# ── commands ───────────────────────────────────────────────────────────────


async def viral_preview_cmd(update: Any, context: Any) -> None:
    """Generate one real candidate post and score it — persisting nothing."""

    def _generate() -> tuple[str, float]:
        text = ViralEngine.generate_post()
        return text, ViralEngine.calculate_viral_score(text)

    text, score = await asyncio.to_thread(_generate)
    await reply(update, format_preview(text, score))


async def viral_stats_cmd(update: Any, context: Any) -> None:
    """Report the counts the ``ViralPost`` table can actually answer."""
    chat = chat_id(update)
    if chat is None:
        await reply(update, _NO_CHAT)
        return
    stats = await asyncio.to_thread(ViralEngine.get_stats, chat)
    await reply(update, format_stats(stats))


async def viral_post_cmd(update: Any, context: Any) -> None:
    """List the pending queue, or mark one row as posted (owner only)."""
    chat = chat_id(update)
    if chat is None:
        await reply(update, _NO_CHAT)
        return

    post_id = parse_post_id(args(context))
    if post_id is None:
        rows = await asyncio.to_thread(ViralEngine.get_pending_posts, chat, PENDING_LIMIT)
        await reply(update, format_pending(rows))
        return

    caller = user_id(update)
    if caller is None or not is_owner(caller):
        await reply(update, _DENIED)
        return

    def _mark() -> bool:
        pending = {int(row["id"]) for row in ViralEngine.get_pending_posts(chat, 1000) if row["id"]}
        if post_id not in pending:
            return False
        ViralEngine.mark_posted(post_id)
        return True

    marked = await asyncio.to_thread(_mark)
    if not marked:
        await reply(update, f"🚫 پست #{post_id} در صف این گفتگو نیست.")
        return
    await reply(update, f"✅ پست #{post_id} به‌عنوان منتشرشده علامت خورد.")
