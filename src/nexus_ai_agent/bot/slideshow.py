"""Pure Telegram-facing surface for ``/slideshow`` (Wave 2.5, r7 item 2).

Limits, session bookkeeping and every user-facing string live here — free of
``telegram`` imports — so they are unit-testable in isolation and the PTB
module (``bot/slideshow_handlers.py``) stays thin glue over this logic.
The numeric envelope is imported from the queue-side adapter, so bot and
worker can never disagree about the ceiling.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from nexus_ai_agent.creative.slideshow.worker_adapter import (
    BOT_TARGET_DURATION_US,
    DEFAULT_RESOLUTION,
    ERROR_CODES,
    MAX_IMAGES,
    SLIDESHOW_JOB_TYPE,
)

__all__ = [
    "BOT_TARGET_DURATION_US",
    "DEFAULT_RESOLUTION",
    "MAX_IMAGES",
    "SLIDESHOW_JOB_TYPE",
    "SlideshowSessionStore",
    "friendly_render_error",
    "friendly_success",
    "photo_extension",
    "usage_text",
    "validate_prompt",
    "parse_slideshow_options",
]

#: Telegram photo formats the render lane can read (mirror of the pack probe).
_ACCEPTED_PHOTO_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"})

#: A session expires after this long without its final ``/slideshow`` command.
SESSION_TTL_SECONDS = 30 * 60.0

#: Total open sessions across all chats — a bounded in-memory surface.
MAX_OPEN_SESSIONS = 512

#: Project-name grammar; matches the payload model in the queue adapter.
_PROJECT_NAME_RE = re.compile(r"[\w][\w .\-:]{0,59}\Z")


@dataclass
class _Session:
    file_ids: list[str] = field(default_factory=list)
    last_activity: float = field(default_factory=time.monotonic)


class SlideshowSessionStore:
    """Per-chat/user buffer of uploaded photo ids: rolling, capped, deduped.

    The store keeps at most :data:`MAX_IMAGES` file ids per user and expires
    idle sessions lazily; the whole map is FIFO-capped so abandoned sessions
    cannot grow memory without bound.  Nothing here touches the network.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = SESSION_TTL_SECONDS,
        max_sessions: int = MAX_OPEN_SESSIONS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_sessions = max_sessions
        self._clock = clock
        self._sessions: dict[tuple[int, int], _Session] = {}

    def _live(self, key: tuple[int, int]) -> _Session | None:
        session = self._sessions.get(key)
        if session is None:
            return None
        if self._clock() - session.last_activity > self._ttl:
            del self._sessions[key]
            return None
        return session

    def start(self, key: tuple[int, int]) -> None:
        """Open (or restart) a collection session — an old buffer is dropped."""
        while len(self._sessions) >= self._max_sessions:
            self._sessions.pop(next(iter(self._sessions)))
        self._sessions[key] = _Session(last_activity=self._clock())

    def is_active(self, key: tuple[int, int]) -> bool:
        return self._live(key) is not None

    def add_image(self, key: tuple[int, int], file_id: str) -> int | None:
        """Add one upload to the session.

        Returns the new count, ``-1`` when the session is already full (the
        upload must be refused — the hard cap lives here, not in the render),
        or ``None`` when no session is open for this user.
        """
        session = self._live(key)
        if session is None:
            return None
        if file_id in session.file_ids:
            session.last_activity = self._clock()
            return len(session.file_ids)
        if len(session.file_ids) >= MAX_IMAGES:
            return -1
        session.file_ids.append(file_id)
        session.last_activity = self._clock()
        return len(session.file_ids)

    def count(self, key: tuple[int, int]) -> int:
        session = self._live(key)
        return len(session.file_ids) if session else 0

    def take_images(self, key: tuple[int, int]) -> list[str]:
        """Consume and close the session, returning its collected ids."""
        session = self._sessions.pop(key, None)
        if session is None or self._clock() - session.last_activity > self._ttl:
            return []
        return list(session.file_ids)

    def clear(self) -> None:
        self._sessions.clear()


def usage_text() -> str:
    return (
        "🎬 ساخت اسلایدشو:\n"
        "۱) این پیام را بفرستید تا جمع‌آوری تصویر شروع شود.\n"
        f"۲) تا {MAX_IMAGES} تصویر بفرستید (در این گفتگو).\n"
        f"۳) سپس /slideshow <عنوان> بفرستید تا رندر در صف بنشیند "
        f"(حداکثر {BOT_TARGET_DURATION_US // 1_000_000} ثانیه، {DEFAULT_RESOLUTION}).\n"
        "اختیاری: /slideshow --upscale 2 <عنوان>\n"
        "یا: /slideshow --slides 5 --fill <عنوان>\n"
        "گزینهٔ --fill اجازهٔ ارسال عنوان به سرویس تولید تصویر و تولید کمبودهاست؛ "
        "در سرویس پولیِ فعال‌شده توسط مدیر، هزینه دارد. عکس‌های شما ارسال نمی‌شوند."
    )


def validate_prompt(raw: str) -> tuple[str | None, str | None]:
    """Validate the free-text argument; return ``(project_name, error)``.

    Per r7 item 5 the caption names the project (`PlanningRequest.project_name`)
    — the render lane has no prompt field and none is invented.  An empty
    caption is legal and means "no name" (``None``).
    """
    text = raw.strip()
    if not text:
        return None, None
    if len(text) > 60:
        return None, "❌ عنوان ویدیو نباید بیشتر از ۶۰ نویسه باشد."
    if not _PROJECT_NAME_RE.match(text):
        return None, ("❌ عنوان فقط می‌تواند شامل حروف، اعداد، فاصله، نقطه، دونقطه و خط تیره باشد.")
    return text, None


def photo_extension(file_path: str | None) -> str:
    """Pick a safe disk extension for a downloaded Telegram photo."""
    if file_path:
        suffix = Path(file_path).suffix.lower()
        if suffix in _ACCEPTED_PHOTO_SUFFIXES:
            return suffix
    return ".jpg"


#: Code -> plain Persian user message (r7 item 7).  Details, paths and raw
#: exception text never reach the chat; the queue persists them for operators.
_FRIENDLY_ERRORS: Mapping[str, str] = {
    "ffmpeg_unavailable": (
        "❌ موتور ویدیو (FFmpeg) روی این سرور در دسترس نیست؛ مدیر را مطلع کنید."
    ),
    "render_failed": (
        "❌ ساخت ویدیو ناموفق بود (خطای انکودر یا پایان زمان مجاز). لطفاً دوباره تلاش کنید."
    ),
    "unusable_image": "❌ خطا در ساخت ویدیو: فرمت تصویر پشتیبانی نمی‌شود.",
    "invalid_request": (
        "❌ درخواست ساخت ویدیو نامعتبر بود (تصاویر یا تنظیمات خارج از محدودهٔ مجاز)."
    ),
    "image_generation_failed": "❌ تولید تصویرهای تکمیلی ناموفق بود؛ دوباره تلاش کنید.",
    "internal": "❌ خطای داخلی هنگام ساخت ویدیو. درخواست ثبت شد؛ بعداً دوباره تلاش کنید.",
}


def friendly_render_error(code: str | None) -> str:
    """Map a worker ``error_code`` to a user message; unknown codes read as internal."""
    if isinstance(code, str) and code in ERROR_CODES:
        return _FRIENDLY_ERRORS[code]
    return _FRIENDLY_ERRORS["internal"]


def friendly_success(result: Mapping[str, object]) -> str:
    """Format the completion caption for a successful render (measured values only)."""

    def _num(key: str, default: int = 0) -> int:
        try:
            return int(str(result.get(key, default)))
        except (TypeError, ValueError):
            return default

    duration_s = max(1, round(_num("duration_us") / 1_000_000))
    shots = _num("shot_count")
    size_mb = _num("size_bytes") / (1024 * 1024)
    width, height = _num("width"), _num("height")
    resolution = f"{width}×{height}" if width and height else DEFAULT_RESOLUTION
    return (
        "✅ اسلایدشوی شما آماده شد 🎬\n"
        f"⏱ {duration_s} ثانیه · 🖼 {shots} شات · 📐 {resolution} · 💾 {size_mb:.1f} مگابایت"
    )


@dataclass(frozen=True)
class SlideshowOptions:
    project_name: str | None
    target_images: int | None = None
    generate_missing: bool = False
    upscale_factor: int | None = None

    def validate_count(self, count: int) -> None:
        if self.target_images is not None:
            if self.target_images < count:
                raise ValueError("❌ تعداد اسلاید نباید کمتر از تعداد عکس‌های ارسالی باشد.")
            if self.target_images > count and not self.generate_missing:
                raise ValueError("❌ برای تولید عکس‌های کمبود، گزینهٔ --fill را اضافه کنید.")


def parse_slideshow_options(args: list[str]) -> SlideshowOptions:
    """Flags precede the title; --fill is consent, never inferred from a count."""
    args = list(args)
    target: int | None = None
    fill = False
    upscale: int | None = None
    while args and args[0].startswith("--"):
        flag = args.pop(0)
        if flag == "--fill" and not fill:
            fill = True
        elif flag == "--upscale" and upscale is None and args:
            try:
                upscale = int(args.pop(0))
            except ValueError:
                raise ValueError("❌ --upscale باید عددی بین ۲ تا ۴ باشد.") from None
            if not 2 <= upscale <= 4:
                raise ValueError("❌ --upscale باید عددی بین ۲ تا ۴ باشد.")
        elif flag == "--slides" and target is None and args:
            try:
                target = int(args.pop(0))
            except ValueError:
                raise ValueError("❌ --slides باید عددی بین ۱ تا ۵ باشد.") from None
            if not 1 <= target <= MAX_IMAGES:
                raise ValueError("❌ --slides باید عددی بین ۱ تا ۵ باشد.")
        else:
            raise ValueError("❌ استفاده: /slideshow --upscale 2 --slides 5 --fill <عنوان>")
    project_name, error = validate_prompt(" ".join(args))
    if error:
        raise ValueError(error)
    if fill and (target is None or not project_name):
        raise ValueError("❌ --fill به --slides و عنوان نیاز دارد.")
    return SlideshowOptions(project_name, target, fill, upscale)
