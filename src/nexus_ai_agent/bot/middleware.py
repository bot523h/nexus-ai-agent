"""Bot-level access control: rate limiting and one deny-by-default gate.

Import boundary: this module must stay ``telegram``-free.  It is *not* listed
in the frozen legacy baseline of ``tests/architecture/test_import_boundaries.py``,
so a single ``from telegram import ...`` here would fail the architecture test.
Everything below therefore works on duck-typed update objects, and the PTB
``TypeHandler`` that drives :class:`BotAccessGate` lives in ``bot/app.py``
(one of the grandfathered files).
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

#: ``telegram.constants.MessageEntityType.BOT_COMMAND`` spelled out as a plain
#: string so this module does not have to import ``telegram``.
_BOT_COMMAND_ENTITY = "bot_command"

#: Commands a stranger may always reach, even in an allow-list deployment.
#: These are cheap, read-only and they are the only way an unauthorized user
#: learns *why* the rest of the bot refuses them.  Anything that burns money
#: or touches a third-party API (``/ai``, ``/imagine``, ``/tts``, ``/cloud``…)
#: is deliberately absent.
PUBLIC_COMMANDS = frozenset({"start", "help", "language", "forcejoin_status"})

#: Message shown to an identified-but-unauthorized user.
DENIED_MESSAGE = (
    "⛔ این ربات در حالت دسترسی محدود اجرا می‌شود و شما در فهرست مجاز نیستید.\n"
    "اگر فکر می‌کنید این یک اشتباه است، با مالک ربات تماس بگیرید.\n\n"
    "This bot runs in allow-list mode and your Telegram id is not on it. "
    "Contact the bot owner if you believe this is a mistake."
)


class RateLimiter:
    """Bounded sliding-window limiter (per user, in-process)."""

    #: Hard ceiling on simultaneously tracked users.  Past this the least
    #: recently seen windows are dropped, so a public bot cannot be pushed
    #: into unbounded memory growth by spraying updates from many user ids.
    MAX_TRACKED_USERS = 10_000

    def __init__(self, max_messages: int = 10, window_seconds: int = 60):
        self.max_messages = max_messages
        self.window_seconds = window_seconds
        self._events: dict[int, deque[float]] = {}

    def is_allowed(self, user_id: int) -> bool:
        now = time.time()
        q = self._events.get(user_id)
        if q is None:
            self._evict_if_full()
            q = self._events.setdefault(user_id, deque())
        while q and (now - q[0]) > self.window_seconds:
            q.popleft()
        if len(q) >= self.max_messages:
            return False
        q.append(now)
        return True

    def _evict_if_full(self) -> None:
        """Drop the oldest-tracked user windows once the cap is reached.

        ``dict`` keeps insertion order, so this evicts whoever was first seen
        — bounded memory instead of a dict that grows with every user id the
        bot has ever heard from.
        """
        while len(self._events) >= self.MAX_TRACKED_USERS:
            self._events.pop(next(iter(self._events)))


@dataclass(frozen=True)
class GateDecision:
    """Outcome of one access-gate evaluation.

    ``reason`` is a stable machine-readable tag so tests and logs can assert
    on *why* an update passed or was refused.
    """

    allowed: bool
    reason: str
    user_id: int | None = None


class AuthMiddleware:
    """Allow-list auth with deny-by-default.

    - The owner (``owner_telegram_id`` != 0) is always allowed.
    - ``allowed_user_ids`` adds extra allowed users on top of the owner.
    - Empty list + owner configured → only the owner is allowed.
    - Empty list + no owner configured → nobody is allowed.
    """

    def __init__(self, allowed_user_ids: list[int], owner_telegram_id: int = 0):
        self.allowed_user_ids = list(allowed_user_ids)
        self.owner_telegram_id = owner_telegram_id

    def is_allowed(self, user_id: int) -> bool:
        if self.owner_telegram_id and user_id == self.owner_telegram_id:
            return True
        return user_id in self.allowed_user_ids

    @property
    def enforcing(self) -> bool:
        """True when an owner or an allow-list is configured.

        An unconfigured bot (no owner, empty allow-list) is a *public* bot:
        ``is_allowed`` would then deny literally everyone, so callers that
        gate the whole surface must treat "nothing configured" as "open"
        rather than "locked shut".
        """
        return bool(self.owner_telegram_id) or bool(self.allowed_user_ids)

    def is_owner(self, user_id: int) -> bool:
        return bool(self.owner_telegram_id) and user_id == self.owner_telegram_id


def command_name(update: object) -> str | None:
    """Return the lower-case command name of *update*, or ``None``.

    Handles ``/cmd``, ``/cmd@thisbot`` and ``/cmd arg`` uniformly, and looks at
    message, edited message, channel post and callback-query messages so a gate
    in front of *every* update cannot be slipped past with an edit or a button
    press.
    """
    message = None
    for attr in ("message", "edited_message", "channel_post"):
        message = getattr(update, attr, None)
        if message is not None:
            break
    if message is None:
        query = getattr(update, "callback_query", None)
        message = getattr(query, "message", None) if query is not None else None
    if message is None:
        return None

    text = getattr(message, "text", None) or getattr(message, "caption", None)
    entities: Iterable[object] = getattr(message, "entities", None) or []
    for entity in entities:
        if getattr(entity, "type", None) != _BOT_COMMAND_ENTITY:
            continue
        offset = int(getattr(entity, "offset", 0))
        length = int(getattr(entity, "length", 0))
        if length <= 0 or offset >= len(text or ""):
            continue
        raw = (text or "")[offset : offset + length]
        return _normalize_command(raw)
    return None


def _normalize_command(raw: str) -> str:
    """``"/Start@MyBot"`` → ``"start"``."""
    name = raw.lstrip("/").split("@", 1)[0].strip()
    return name.lower()


class BotAccessGate:
    """One choke point deciding whether an update may reach any handler.

    Registered by ``bot/app.py`` as a ``TypeHandler`` in PTB group ``-1``; when
    it refuses, the handler raises ``ApplicationHandlerStop`` so *no* handler
    in *any* later group sees the update.  That replaces the previous
    situation where authorization existed on two commands out of ~80.

    Policy:

    * **Public deployment** (no owner, empty allow-list): everything passes.
      A bot with no configured owner is by definition open to the public and
      locking it down would break every default install.
    * **Allow-list deployment**: only the owner and ``allowed_user_ids`` get
      through, plus :data:`PUBLIC_COMMANDS`.  Deny-by-default.
    * Updates with no identifiable user (channel posts, anonymous admins) are
      passed through: there is no identity to authorize and refusing them
      would silently break channel forwarding.
    """

    def __init__(
        self,
        auth: AuthMiddleware,
        *,
        public_commands: frozenset[str] = PUBLIC_COMMANDS,
    ) -> None:
        self._auth = auth
        self._public_commands = frozenset(public_commands)

    @property
    def enforcing(self) -> bool:
        return self._auth.enforcing

    def decide(self, update: object) -> GateDecision:
        if not self.enforcing:
            return GateDecision(True, "public_mode")

        user = getattr(update, "effective_user", None)
        raw_id = getattr(user, "id", None)
        if raw_id is None:
            return GateDecision(True, "no_user_identity")
        user_id = int(raw_id)

        if self._auth.is_allowed(user_id):
            return GateDecision(True, "authorized", user_id)

        command = command_name(update)
        if command is not None and command in self._public_commands:
            return GateDecision(True, "public_command", user_id)

        return GateDecision(False, "not_authorized", user_id)
