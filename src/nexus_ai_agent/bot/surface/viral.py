"""Telegram surface for the viral content engine.

Commands: ``/viral_preview``, ``/viral_stats``, ``/viral_post``.

Replaces the canned strings ``bot/handlers.py`` shipped for these commands::

    async def viral_preview_cmd(...) -> "🔥 Preview: Top AI trends of the week..."
    async def viral_stats_cmd(...)   -> "🔥 Viral Engine: 12 posts sent, 450 likes total."
    async def viral_post_cmd(...)    -> "📋 Pending viral posts: 3 in queue."

with the real :class:`~nexus_ai_agent.features.viral_engine.ViralEngine` queue
(audit finding A-03, 2026-10-15). ``/viral_now`` (generate + enqueue) already
used the engine and stays in ``handlers.py``.

Semantics
---------
* ``/viral_preview`` shows the best pending post of *this* chat (highest viral
  score) — what ``/viral_post publish`` would send next.
* ``/viral_stats`` reports the real counters (``total / pending / posted /
  failed``) for this chat.
* ``/viral_post`` lists the queue; ``/viral_post publish [id]`` sends the best
  (or the given) pending post to the chat and marks it posted. Publishing is
  owner-only like ``/viral_now``; previews and stats are read-only.

Framework-free (see :mod:`._ptb`) and non-blocking (engine CRUD is synchronous
SQLite, so every call goes through :func:`asyncio.to_thread`).
"""

from __future__ import annotations

import asyncio
from typing import Any

from nexus_ai_agent.features.owner_control import is_owner
from nexus_ai_agent.features.viral_engine import ViralEngine

from ._ptb import args, bot_of, chat_id, reply, user_id

__all__ = [
    "format_preview",
    "format_queue",
    "format_stats",
    "viral_post_cmd",
    "viral_preview_cmd",
    "viral_stats_cmd",
]

_DENIED = "⛔ Access denied"
_SEPARATOR = "━━━━━━━━━━━━━━━━"
_EMPTY = "📭 صف پست‌های وایرال این گفتگو خالی است. با /viral_now پست بسازید."


def format_preview(post: dict[str, Any] | None) -> str:
    """Render the next post to go out (highest score first)."""
    if post is None:
        return _EMPTY
    return (
        f"🔥 پیش‌نمایش پست بعدی (id={post['id']}, امتیاز {float(post['viral_score']):.1f})\n"
        f"{_SEPARATOR}\n{post['text']}"
    )


def format_stats(stats: dict[str, int]) -> str:
    """Render :meth:`ViralEngine.get_stats` for this chat."""
    return (
        f"📊 آمار موتور وایرال\n{_SEPARATOR}\n"
        f"کل: {stats.get('total', 0)}\n"
        f"در صف: {stats.get('pending', 0)}\n"
        f"ارسال‌شده: {stats.get('posted', 0)}\n"
        f"ناموفق: {stats.get('failed', 0)}"
    )


def format_queue(posts: list[dict[str, Any]]) -> str:
    """Render the pending queue (score-ordered)."""
    if not posts:
        return _EMPTY
    lines = [f"📋 صف پست‌های وایرال ({len(posts)})", _SEPARATOR]
    for post in posts:
        lines.append(f"#{post['id']} · {float(post['viral_score']):.1f} · {post['text']}")
    lines.append("برای ارسال: /viral_post publish [id]")
    return "\n".join(lines)


async def viral_preview_cmd(update: Any, context: Any) -> None:
    """``/viral_preview`` — the best pending post of this chat."""
    chat = chat_id(update)
    if chat is None:
        return
    posts = await asyncio.to_thread(ViralEngine.get_pending_posts, chat, 1)
    await reply(update, format_preview(posts[0] if posts else None))


async def viral_stats_cmd(update: Any, context: Any) -> None:
    """``/viral_stats`` — real counters for this chat."""
    chat = chat_id(update)
    if chat is None:
        return
    stats = await asyncio.to_thread(ViralEngine.get_stats, chat)
    await reply(update, format_stats(stats))


async def viral_post_cmd(update: Any, context: Any) -> None:
    """``/viral_post`` lists the queue; ``/viral_post publish [id]`` sends one (owner)."""
    chat, uid = chat_id(update), user_id(update)
    if chat is None or uid is None:
        return
    command_args = args(context)
    if not command_args:
        posts = await asyncio.to_thread(ViralEngine.get_pending_posts, chat, 10)
        await reply(update, format_queue(posts))
        return
    if command_args[0].lower() != "publish":
        await reply(update, "استفاده: /viral_post — فهرست صف | /viral_post publish [id]")
        return
    if not is_owner(uid):
        await reply(update, _DENIED)
        return
    posts = await asyncio.to_thread(ViralEngine.get_pending_posts, chat, 50)
    chosen: dict[str, Any] | None = None
    if len(command_args) > 1:
        try:
            wanted = int(command_args[1])
        except ValueError:
            await reply(update, "شناسهٔ پست باید عدد باشد.")
            return
        chosen = next((p for p in posts if p["id"] == wanted), None)
        if chosen is None:
            await reply(update, f"پست #{wanted} در صف این گفتگو نیست.")
            return
    elif posts:
        chosen = posts[0]
    if chosen is None:
        await reply(update, _EMPTY)
        return
    bot = bot_of(context)
    send = getattr(bot, "send_message", None)
    if send is None:
        await reply(update, "❌ ارسال ممکن نیست: ربات در دسترس نیست.")
        return
    full = await asyncio.to_thread(ViralEngine.get_post, int(chosen["id"]))
    try:
        await send(chat_id=chat, text=full["text"] if full else chosen["text"])
    except Exception as exc:  # noqa: BLE001 — surface the API failure to the owner
        await reply(update, f"❌ ارسال ناموفق بود: {exc}")
        return
    await asyncio.to_thread(ViralEngine.mark_posted, int(chosen["id"]))
    await reply(update, f"✅ پست #{chosen['id']} ارسال و به‌عنوان ارسال‌شده ثبت شد.")
