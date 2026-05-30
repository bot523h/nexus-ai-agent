from __future__ import annotations

import logging
import os
import textwrap
from io import BytesIO
from typing import Any

import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont

from nexus_ai_agent.config.settings import get_settings

logger = logging.getLogger(__name__)


class AIStoryGenerator:
    """AI Story Generator with RTL Persian support."""

    def __init__(self, gemini_engine: Any | None = None) -> None:
        self.settings = get_settings()
        self.font_path = self.settings.vazir_font_path
        self.gemini = gemini_engine

    async def create_story(self, user_id: int, text: str, style: str = "motivational") -> bytes:
        """Generate a story image with properly rendered Persian text."""
        _ = user_id
        _ = style
        
        # 1. Create a dark gradient background
        width, height = 1080, 1920
        image = Image.new("RGB", (width, height), color="#0f172a")
        draw = ImageDraw.Draw(image)

        # 2. Reshape and Bidi for Persian text
        reshaped_text = arabic_reshaper.reshape(text)

        # 3. Load font
        try:
            font_size = 60
            font = ImageFont.truetype(self.font_path, font_size)
        except Exception:
            logger.warning("Vazirmatn font not found, using default")
            font = ImageFont.load_default()

        # 4. Wrap text (handling RTL)
        wrapped_lines = textwrap.wrap(text, width=30)
        
        y_offset = height // 3
        for line in wrapped_lines:
            # Re-process each line for RTL
            r_line = arabic_reshaper.reshape(line)
            b_line = get_display(r_line)
            
            # Center text
            w = draw.textlength(b_line, font=font)
            draw.text(((width - w) // 2, y_offset), b_line, font=font, fill="#f8fafc")
            y_offset += font_size + 20

        # 5. Add a footer
        footer_text = get_display(arabic_reshaper.reshape("ساخته شده توسط NEXUS AI"))
        footer_font = ImageFont.truetype(self.font_path, 30) if os.path.exists(self.font_path) else font
        fw = draw.textlength(footer_text, font=footer_font)
        draw.text(((width - fw) // 2, height - 100), footer_text, font=footer_font, fill="#64748b")

        # 6. Save to bytes
        buf = BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()

    async def generate_story_image(self, text: str, output_path: str) -> None:
        """Used by Celery worker to save to disk."""
        image_bytes = await self.create_story(0, text)
        with open(output_path, "wb") as f:
            f.write(image_bytes)
