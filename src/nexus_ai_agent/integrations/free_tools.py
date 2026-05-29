"""Free tools: Weather (wttr.in), Currency (exchange-rate), News (NewsAPI/DDG), YouTube.

All tools use free APIs with no paid dependencies.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# A) WeatherTool — wttr.in (free, no API key)
# ═══════════════════════════════════════════════════════════════════════


class WeatherTool:
    """Fetch current weather from wttr.in (free, no API key)."""

    async def get_weather(self, city: str) -> dict[str, Any]:
        """Return current weather data for *city*.

        Returns a dict with keys: city, temp_c, feels_like, humidity,
        description, wind_kph, visibility, uv, source.
        """
        url = f"https://wttr.in/{city}?format=j1"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()

            current = data.get("current_condition", [{}])[0]
            area = data.get("nearest_area", [{}])[0]
            city_name = area.get("areaName", [{}])[0].get("value", city)

            return {
                "city": city_name,
                "temp_c": current.get("temp_C", "N/A"),
                "feels_like": current.get("FeelsLikeC", "N/A"),
                "humidity": current.get("humidity", "N/A"),
                "description": current.get("weatherDesc", [{}])[0].get("value", "N/A"),
                "wind_kph": current.get("windspeedKmph", "N/A"),
                "visibility": current.get("visibility", "N/A"),
                "uv": current.get("uvIndex", "N/A"),
                "source": "wttr.in",
            }
        except Exception as exc:
            logger.warning("weather_error", city=city, error=str(exc))
            return {"city": city, "error": str(exc), "source": "wttr.in"}

    def format_weather(self, data: dict[str, Any]) -> str:
        """Format weather data as a Persian-friendly Telegram message."""
        if "error" in data:
            return f"🌤 خطا در دریافت آب‌وهوا برای {data['city']}: {data['error']}"
        return (
            f"🌤 **آب‌وهوا: {data['city']}**\n\n"
            f"🌡 دما: {data['temp_c']}°C\n"
            f"🤔 حس‌شده: {data['feels_like']}°C\n"
            f"💧 رطوبت: {data['humidity']}%\n"
            f"💨 باد: {data['wind_kph']} km/h\n"
            f"👁 دید: {data['visibility']} km\n"
            f"☀️ UV: {data['uv']}\n"
            f"📝 {data['description']}\n\n"
            f"📍 منبع: {data['source']}"
        )


# ═══════════════════════════════════════════════════════════════════════
# B) CurrencyTool — exchangerate-api.com (free tier)
# ═══════════════════════════════════════════════════════════════════════


class CurrencyTool:
    """Fetch exchange rates from exchangerate-api.com (free tier)."""

    # Free API base URL (no key required for latest rates)
    _BASE_URL = "https://api.exchangerate-api.com/v4/latest"

    async def get_rate(self, base: str = "USD") -> dict[str, Any]:
        """Get latest exchange rates for *base* currency."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(f"{self._BASE_URL}/{base}")
                resp.raise_for_status()
                data = resp.json()
            return {
                "base": data.get("base", base),
                "rates": data.get("rates", {}),
                "date": data.get("date", "N/A"),
                "source": "exchangerate-api.com",
            }
        except Exception as exc:
            logger.warning("currency_rate_error", base=base, error=str(exc))
            return {"base": base, "error": str(exc), "source": "exchangerate-api.com"}

    async def get_usd_to_irr(self) -> dict[str, Any]:
        """Get USD to IRR (Iranian Rial) rate specifically."""
        result = await self.get_rate("USD")
        if "error" in result:
            return result
        rates = result.get("rates", {})
        irr = rates.get("IRR", "N/A")
        # Also get Toman (1/10 of Rial)
        try:
            toman = float(irr) / 10 if irr != "N/A" else "N/A"
        except (ValueError, TypeError):
            toman = "N/A"
        return {
            "usd_to_irr": irr,
            "usd_to_toman": toman,
            "date": result.get("date", "N/A"),
            "source": result.get("source", ""),
        }

    async def convert(self, amount: float, from_curr: str, to_curr: str) -> dict[str, Any]:
        """Convert *amount* from *from_curr* to *to_curr*."""
        result = await self.get_rate(from_curr)
        if "error" in result:
            return result
        rates = result.get("rates", {})
        rate = rates.get(to_curr)
        if rate is None:
            return {
                "from": from_curr,
                "to": to_curr,
                "error": f"Rate for {to_curr} not found",
                "source": result.get("source", ""),
            }
        converted = float(amount) * float(rate)
        return {
            "amount": amount,
            "from": from_curr,
            "to": to_curr,
            "rate": float(rate),
            "result": round(converted, 2),
            "source": result.get("source", ""),
        }

    def format_rate(self, data: dict[str, Any]) -> str:
        """Format USD to IRR/Toman rate as a Persian message."""
        if "error" in data:
            return f"💰 خطا در دریافت نرخ: {data['error']}"
        irr = data.get("usd_to_irr", "N/A")
        toman = data.get("usd_to_toman", "N/A")
        return (
            f"💰 **نرخ دلار**\n\n"
            f"💵 1 USD = {irr} IRR (ریال)\n"
            f"💵 1 USD = {toman} Toman (تومان)\n\n"
            f"📅 تاریخ: {data.get('date', 'N/A')}\n"
            f"📍 منبع: {data.get('source', '')}"
        )

    def format_convert(self, data: dict[str, Any]) -> str:
        """Format conversion result as a Persian message."""
        if "error" in data:
            return f"💱 خطا در تبدیل: {data['error']}"
        return (
            f"💱 **تبدیل ارز**\n\n"
            f"{data['amount']} {data['from']} = "
            f"{data['result']:,.2f} {data['to']}\n"
            f"نرخ: 1 {data['from']} = {data['rate']:,.4f} {data['to']}\n\n"
            f"📍 منبع: {data.get('source', '')}"
        )


# ═══════════════════════════════════════════════════════════════════════
# C) NewsTool — NewsAPI (free 100 req/day) or DuckDuckGo fallback
# ═══════════════════════════════════════════════════════════════════════


class NewsTool:
    """Fetch news from NewsAPI (if key available) or DuckDuckGo fallback."""

    def __init__(self, news_api_key: str | None = None) -> None:
        self._api_key = news_api_key

    async def get_news(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        """Fetch news articles. Uses NewsAPI if key is available,
        otherwise falls back to DuckDuckGo.
        """
        if self._api_key:
            return await self._newsapi(query, max_results)
        return await self._ddg_fallback(query, max_results)

    async def _newsapi(self, query: str, max_results: int) -> list[dict[str, str]]:
        """Fetch from NewsAPI.org (100 requests/day free tier)."""
        results: list[dict[str, str]] = []
        try:
            url = "https://newsapi.org/v2/everything"
            params: dict[str, str | int] = {
                "q": query,
                "language": "fa",
                "sortBy": "publishedAt",
                "pageSize": str(max_results),
                "apiKey": self._api_key or "",
            }
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()

            for article in data.get("articles", [])[:max_results]:
                results.append(
                    {
                        "title": article.get("title", ""),
                        "description": article.get("description", ""),
                        "url": article.get("url", ""),
                        "source": article.get("source", {}).get("name", "NewsAPI"),
                        "published_at": article.get("publishedAt", ""),
                    }
                )
        except Exception as exc:
            logger.warning("newsapi_error", error=str(exc))
            # Fall back to DDG
            return await self._ddg_fallback(query, max_results)

        return results

    async def _ddg_fallback(self, query: str, max_results: int) -> list[dict[str, str]]:
        """Fallback: search DuckDuckGo for news articles."""
        results: list[dict[str, str]] = []
        try:
            from bs4 import BeautifulSoup

            ddg_url = "https://html.duckduckgo.com/html/"
            async with httpx.AsyncClient(
                timeout=15.0,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                    ),
                },
                follow_redirects=True,
            ) as client:
                resp = await client.post(ddg_url, data={"q": f"{query} خبر", "kl": "wt-wt"})
                resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")
            for item in soup.select(".result"):
                if len(results) >= max_results:
                    break
                title_el = item.select_one(".result__a")
                snippet_el = item.select_one(".result__snippet")
                if title_el is None:
                    continue
                title = title_el.get_text(strip=True)
                snippet = snippet_el.get_text(strip=True) if snippet_el else ""
                raw_url: str = str(title_el.get("href", ""))
                if "uddg=" in raw_url:
                    import urllib.parse

                    parsed = urllib.parse.parse_qs(urllib.parse.urlparse(raw_url).query)
                    raw_url = parsed.get("uddg", [raw_url])[0]
                results.append(
                    {
                        "title": title,
                        "description": snippet,
                        "url": raw_url,
                        "source": "DuckDuckGo",
                        "published_at": "",
                    }
                )
        except Exception as exc:
            logger.warning("ddg_news_error", error=str(exc))

        return results

    def format_news(self, results: list[dict[str, str]], query: str) -> str:
        """Format news results as a Persian-friendly message."""
        if not results:
            return f"📰 خبری یافت نشد برای: {query}"
        text = f"📰 **اخبار: {query}**\n\n"
        for i, r in enumerate(results, 1):
            title = r.get("title", "")
            desc = r.get("description", "")
            url = r.get("url", "")
            source = r.get("source", "")
            if url:
                text += f"{i}. [{title}]({url})\n"
            else:
                text += f"{i}. **{title}**\n"
            if desc:
                text += f"   {desc}\n"
            text += f"   📍 {source}\n\n"
        return text


# ═══════════════════════════════════════════════════════════════════════
# D) YouTubeSearchTool — yt-dlp (free, no API key)
# ═══════════════════════════════════════════════════════════════════════


class YouTubeSearchTool:
    """Search YouTube or fetch video info using yt-dlp (free, no API key)."""

    async def get_video_info(self, url: str) -> dict[str, Any]:
        """Fetch video metadata and available formats using yt-dlp."""
        try:
            import asyncio

            proc = await asyncio.create_subprocess_exec(
                "yt-dlp",
                "--dump-json",
                "--no-download",
                url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
            if proc.returncode != 0:
                return {
                    "url": url,
                    "error": stderr.decode()[:500],
                    "source": "yt-dlp",
                }
            data = json.loads(stdout)
            return {
                "title": data.get("title", ""),
                "duration": data.get("duration_string", ""),
                "description": (data.get("description") or "")[:1000],
                "uploader": data.get("uploader", ""),
                "view_count": data.get("view_count", 0),
                "like_count": data.get("like_count", 0),
                "thumbnail": data.get("thumbnail", ""),
                "url": url,
                "source": "yt-dlp",
            }
        except FileNotFoundError:
            return {
                "url": url,
                "error": "yt-dlp نصب نیست. ابتدا: pip install yt-dlp",
                "source": "yt-dlp",
            }
        except Exception as exc:
            logger.warning("youtube_info_error", url=url, error=str(exc))
            return {"url": url, "error": str(exc), "source": "yt-dlp"}

    async def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        """Search YouTube via DuckDuckGo and return video links."""
        results: list[dict[str, str]] = []
        try:
            from bs4 import BeautifulSoup

            ddg_url = "https://html.duckduckgo.com/html/"
            async with httpx.AsyncClient(
                timeout=15.0,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                    ),
                },
                follow_redirects=True,
            ) as client:
                resp = await client.post(
                    ddg_url,
                    data={"q": f"{query} site:youtube.com", "kl": "wt-wt"},
                )
                resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")
            for item in soup.select(".result"):
                if len(results) >= max_results:
                    break
                title_el = item.select_one(".result__a")
                snippet_el = item.select_one(".result__snippet")
                if title_el is None:
                    continue
                title = title_el.get_text(strip=True)
                snippet = snippet_el.get_text(strip=True) if snippet_el else ""
                raw_url: str = str(title_el.get("href", ""))
                if "uddg=" in raw_url:
                    import urllib.parse

                    parsed = urllib.parse.parse_qs(urllib.parse.urlparse(raw_url).query)
                    raw_url = parsed.get("uddg", [raw_url])[0]
                if "youtube.com" in raw_url:
                    results.append(
                        {
                            "title": title,
                            "snippet": snippet,
                            "url": raw_url,
                        }
                    )
        except Exception as exc:
            logger.warning("youtube_search_error", error=str(exc))

        return results

    def format_video_info(self, data: dict[str, Any]) -> str:
        """Format video info as a Persian-friendly message."""
        if "error" in data:
            return f"🎬 خطا: {data['error']}"
        views = data.get("view_count", 0)
        likes = data.get("like_count", 0)
        return (
            f"🎬 **{data.get('title', 'ویدیو')}**\n\n"
            f"👤 {data.get('uploader', 'نامشخص')}\n"
            f"⏱ مدت: {data.get('duration', 'نامشخص')}\n"
            f"👁 بازدید: {views:,}\n"
            f"👍 لایک: {likes:,}\n\n"
            f"{data.get('description', '')[:500]}\n\n"
            f"🔗 {data.get('url', '')}"
        )

    def format_search_results(self, results: list[dict[str, str]], query: str) -> str:
        """Format YouTube search results."""
        if not results:
            return f"🎬 ویدیویی یافت نشد برای: {query}"
        text = f"🎬 **نتایج YouTube: {query}**\n\n"
        for i, r in enumerate(results, 1):
            text += f"{i}. [{r.get('title', '')}]({r.get('url', '')})\n"
            if r.get("snippet"):
                text += f"   {r['snippet'][:100]}\n"
            text += "\n"
        return text
