"""Text-to-Speech (gTTS) and Speech-to-Text (Gemini) integration — free, 100+ languages."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

from nexus_ai_agent.observability.logging import get_logger

log = get_logger(__name__)

# Supported TTS languages (most popular)
TTS_LANGUAGES: dict[str, str] = {
    "fa": "فارسی (Persian)",
    "en": "English",
    "ar": "العربية (Arabic)",
    "es": "Español (Spanish)",
    "fr": "Français (French)",
    "de": "Deutsch (German)",
    "ru": "Русский (Russian)",
    "zh": "中文 (Chinese)",
    "ja": "日本語 (Japanese)",
    "ko": "한국어 (Korean)",
    "pt": "Português (Portuguese)",
    "hi": "हिन्दी (Hindi)",
    "tr": "Türkçe (Turkish)",
    "it": "Italiano (Italian)",
    "nl": "Nederlands (Dutch)",
    "pl": "Polski (Polish)",
    "uk": "Українська (Ukrainian)",
    "id": "Bahasa Indonesia",
    "th": "ไทย (Thai)",
    "vi": "Tiếng Việt (Vietnamese)",
}


class SpeechEngine:
    """Free TTS (gTTS) and STT (Gemini) engine."""

    def __init__(self, output_dir: str = "data/audio") -> None:
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    async def text_to_speech(
        self,
        text: str,
        *,
        lang: str = "fa",
        slow: bool = False,
    ) -> dict[str, Any]:
        """Convert text to speech audio file using gTTS.

        Returns dict with: success, path, lang, error.
        """
        try:
            from gtts import gTTS

            # Generate filename
            h = hashlib.md5(f"{text}{lang}".encode()).hexdigest()[:10]
            filename = f"tts_{h}.mp3"
            filepath = self._output_dir / filename

            def _generate() -> None:
                tts = gTTS(text=text, lang=lang, slow=slow)
                tts.save(str(filepath))

            await asyncio.to_thread(_generate)
            size_kb = filepath.stat().st_size // 1024
            log.info("tts_generated", text_len=len(text), lang=lang, size_kb=size_kb)
            return {
                "success": True,
                "path": str(filepath),
                "lang": lang,
                "error": None,
            }
        except ImportError:
            return {
                "success": False,
                "path": None,
                "lang": lang,
                "error": "❌ gTTS نصب نشده. اجرا: pip install gTTS",
            }
        except Exception as e:
            log.error("tts_error", error=str(e))
            return {
                "success": False,
                "path": None,
                "lang": lang,
                "error": f"❌ خطای TTS: {e}",
            }

    async def speech_to_text(
        self,
        audio_path: str,
        *,
        lang: str = "fa",
        gemini_engine: Any = None,
    ) -> dict[str, Any]:
        """Transcribe audio file using Gemini.

        Returns dict with: success, text, lang, error.
        """
        filepath = Path(audio_path)
        if not filepath.exists():
            return {
                "success": False,
                "text": "",
                "lang": lang,
                "error": "❌ فایل صوتی یافت نشد.",
            }

        if gemini_engine is None or not gemini_engine.is_configured:
            # Fallback: try to use a simple transcription approach
            return {
                "success": False,
                "text": "",
                "lang": lang,
                "error": "❌ Gemini API برای تبدیل صدا به متن لازم است.",
            }

        try:
            audio_bytes = filepath.read_bytes()
            # Determine mime type
            suffix = filepath.suffix.lower()
            mime_map = {
                ".mp3": "audio/mp3",
                ".mp4": "audio/mp4",
                ".wav": "audio/wav",
                ".ogg": "audio/ogg",
                ".m4a": "audio/m4a",
                ".webm": "audio/webm",
            }
            mime_type = mime_map.get(suffix, "audio/mp3")

            prompt = (
                f"Transcribe this audio to text. Language: {lang}. Output only the transcription."
            )
            result = await gemini_engine.vision(
                audio_bytes,
                question=prompt,
                mime_type=mime_type,
            )
            return {
                "success": True,
                "text": result,
                "lang": lang,
                "error": None,
            }
        except Exception as e:
            log.error("stt_error", error=str(e))
            return {
                "success": False,
                "text": "",
                "lang": lang,
                "error": f"❌ خطای STT: {e}",
            }

    def list_languages(self) -> str:
        """Return formatted list of supported TTS languages."""
        lines = ["🔊 زبان‌های پشتیبانی‌شده:\n━━━━━━━━━━━━━━━━━"]
        for code, name in TTS_LANGUAGES.items():
            lines.append(f"  • {code} — {name}")
        lines.append("\n💡 استفاده: /tts <متن> --lang fa")
        return "\n".join(lines)

    def get_status(self) -> str:
        """Get engine status."""
        audio_count = len(list(self._output_dir.glob("tts_*.mp3")))
        return (
            f"🔊 Speech Engine\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"🎤 TTS: gTTS (رایگان، {len(TTS_LANGUAGES)}+ زبان)\n"
            f"👂 STT: Gemini (رایگان)\n"
            f"🎵 فایل‌های صوتی: {audio_count}\n"
            f"💰 هزینه: ۰"
        )
