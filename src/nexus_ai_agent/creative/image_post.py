from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

try:
    from playwright.async_api import async_playwright as _async_playwright
except ImportError:  # pragma: no cover - environment dependent
    async_playwright: Any = None
else:
    async_playwright = _async_playwright

from nexus_ai_agent.config.settings import get_settings


async def generate_image_post(text: str, image_url: str | None, template: str) -> str:
    if async_playwright is None:
        raise RuntimeError("Playwright not installed")

    settings = get_settings()
    temp_dir = Path(settings.creative_temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    fd, output_path = tempfile.mkstemp(
        prefix="creative-post-",
        suffix=".png",
        dir=temp_dir,
    )
    os.close(fd)
    Path(output_path).unlink(missing_ok=True)

    image_markup = ""
    if image_url:
        image_markup = (
            f'<img src="{image_url}" alt="post image" '
            'style="max-width: 100%; max-height: 320px; object-fit: cover; border-radius: 16px;" />'
        )

    html = f"""
    <html lang="fa" dir="rtl">
      <body style="margin:0;background:#0f172a;color:#f8fafc;font-family:sans-serif;">
        <main style="width:1080px;height:1080px;padding:64px;display:flex;
                     flex-direction:column;gap:32px;">
          <section style="padding:48px;border-radius:32px;background:#1e293b;display:flex;
                          flex:1;flex-direction:column;justify-content:center;gap:24px;">
            <div style="font-size:24px;opacity:0.7;">{template}</div>
            <div style="font-size:56px;line-height:1.4;font-weight:700;
                         white-space:pre-wrap;">{text}</div>
            {image_markup}
          </section>
        </main>
      </body>
    </html>
    """

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1080, "height": 1080})
        await page.set_content(html, wait_until="networkidle")
        await page.screenshot(path=output_path, full_page=True)
        await browser.close()

    return output_path
