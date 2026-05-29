"""Free AI image generation via Pollinations.ai — no API key required."""

from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from typing import Any

import httpx

from nexus_ai_agent.observability.logging import get_logger

log = get_logger(__name__)

# Style presets for image generation
STYLES: dict[str, str] = {
    "realistic": "photorealistic, 8k, ultra detailed, professional photography",
    "anime": "anime style, vibrant colors, detailed illustration, studio ghibli",
    "digital": "digital art, concept art, artstation, trending",
    "oil": "oil painting, classical art, renaissance style, detailed brushstrokes",
    "watercolor": "watercolor painting, soft colors, artistic, delicate",
    "pixel": "pixel art, 16-bit, retro game style, nostalgic",
    "3d": "3d render, octane render, unreal engine, cinematic lighting",
    "comic": "comic book style, bold lines, vibrant, graphic novel",
    "minimal": "minimalist, clean, simple, modern design",
    "fantasy": "fantasy art, magical, ethereal, epic, dramatic lighting",
}

# Size presets
SIZES: dict[str, str] = {
    "square": "1024x1024",
    "landscape": "1792x1024",
    "portrait": "1024x1792",
    "wide": "1536x640",
    "tall": "640x1536",
}

# Rate limiting: max 1 request per user per 30 seconds
_last_request: dict[int, float] = {}


def _check_rate_limit(user_id: int, cooldown: float = 30.0) -> bool:
    """Return True if user can make a request."""
    now = time.monotonic()
    last = _last_request.get(user_id, 0)
    if now - last < cooldown:
        return False
    _last_request[user_id] = now
    return True


def _rate_limit_remaining(user_id: int, cooldown: float = 30.0) -> int:
    """Seconds until user can make another request."""
    now = time.monotonic()
    last = _last_request.get(user_id, 0)
    remaining = int(cooldown - (now - last))
    return max(0, remaining)


class ImageGenEngine:
    """Pollinations.ai free image generation — zero cost, no API key."""

    BASE_URL = "https://image.pollinations.ai/prompt"

    def __init__(self, output_dir: str = "data/images") -> None:
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    async def generate(
        self,
        prompt: str,
        *,
        style: str = "realistic",
        size: str = "1024x1024",
        seed: int | None = None,
        user_id: int = 0,
    ) -> dict[str, Any]:
        """Generate an image from text prompt.

        Returns dict with keys: success, path, prompt, style, size, error.
        """
        if not _check_rate_limit(user_id):
            remaining = _rate_limit_remaining(user_id)
            return {
                "success": False,
                "path": None,
                "prompt": prompt,
                "style": style,
                "size": size,
                "error": f"⏳ لطفاً {remaining} ثانیه صبر کنید.",
            }

        # Build enhanced prompt with style
        style_suffix = STYLES.get(style, "")
        full_prompt = f"{prompt}, {style_suffix}" if style_suffix else prompt

        # Build URL
        encoded_prompt = httpx.URL(full_prompt, params={}).path.strip("/")
        w, h = size.split("x") if "x" in size else ("1024", "1024")
        params: dict[str, str] = {
            "width": w,
            "height": h,
            "nologo": "true",
            "nofeed": "true",
        }
        if seed is not None:
            params["seed"] = str(seed)

        url = httpx.URL(
            f"{self.BASE_URL}/{encoded_prompt}",
            params=params,
        )

        try:
            async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
                resp = await client.get(str(url))
                if resp.status_code != 200:
                    return {
                        "success": False,
                        "path": None,
                        "prompt": prompt,
                        "style": style,
                        "size": size,
                        "error": f"❌ خطای API: {resp.status_code}",
                    }
                # Save image
                img_hash = hashlib.md5(f"{prompt}{style}{time.time()}".encode()).hexdigest()[:12]
                filename = f"img_{img_hash}.png"
                filepath = self._output_dir / filename
                filepath.write_bytes(resp.content)
                log.info("image_generated", prompt=prompt, style=style, size=len(resp.content))
                return {
                    "success": True,
                    "path": str(filepath),
                    "prompt": prompt,
                    "style": style,
                    "size": size,
                    "error": None,
                }
        except asyncio.TimeoutError:
            return {
                "success": False,
                "path": None,
                "prompt": prompt,
                "style": style,
                "size": size,
                "error": "⏳ زمان تولید عکس به پایان رسید. لطفاً دوباره تلاش کنید.",
            }
        except Exception as e:
            log.error("image_gen_error", error=str(e))
            return {
                "success": False,
                "path": None,
                "prompt": prompt,
                "style": style,
                "size": size,
                "error": f"❌ خطا: {e}",
            }

    def list_styles(self) -> str:
        """Return formatted list of available styles."""
        lines = ["🎨 استایل‌های موجود:\n━━━━━━━━━━━━━━━━━"]
        for key, desc in STYLES.items():
            lines.append(f"  • {key} — {desc.split(',')[0].strip()}")
        lines.append("\n💡 استفاده: /image <توضیح> --style <استایل>")
        lines.append(f"📏 سایزها: {', '.join(SIZES.keys())}")
        return "\n".join(lines)

    def get_status(self) -> str:
        """Get engine status."""
        img_count = len(list(self._output_dir.glob("*.png")))
        return (
            f"🎨 Image Generation Engine\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"🌐 سرویس: Pollinations.ai (رایگان)\n"
            f"💰 هزینه: ۰\n"
            f"🔑 API Key: لازم نیست\n"
            f"🖼️ عکس‌های تولیدشده: {img_count}\n"
            f"📐 سایز: 1024x1024 پیش‌فرض"
        )
