"""Internationalization (i18n) system — multi-language support for NEXUS AI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nexus_ai_agent.observability.logging import get_logger

log = get_logger(__name__)

# Default language
DEFAULT_LANG = "en"

# Supported languages with native names
SUPPORTED_LANGUAGES: dict[str, str] = {
    "en": "English 🇬🇧",
    "fa": "فارسی 🇮🇷",
    "ar": "العربية 🇸🇦",
    "es": "Español 🇪🇸",
    "fr": "Français 🇫🇷",
    "de": "Deutsch 🇩🇪",
    "ru": "Русский 🇷🇺",
    "zh": "中文 🇨🇳",
    "ja": "日本語 🇯🇵",
    "ko": "한국어 🇰🇷",
    "pt": "Português 🇧🇷",
    "hi": "हिन्दी 🇮🇳",
    "tr": "Türkçe 🇹🇷",
    "id": "Bahasa Indonesia 🇮🇩",
    "it": "Italiano 🇮🇹",
}

# Fallback language map for Telegram language_code
_TELEGRAM_LANG_MAP: dict[str, str] = {
    "en": "en",
    "fa": "fa",
    "ar": "ar",
    "es": "es",
    "fr": "fr",
    "de": "de",
    "ru": "ru",
    "zh": "zh",
    "ja": "ja",
    "ko": "ko",
    "pt": "pt",
    "pt-br": "pt",
    "hi": "hi",
    "tr": "tr",
    "id": "id",
    "it": "it",
    "uk": "ru",
    "ur": "ar",
    "ms": "id",
}


def _get_locales_dir() -> Path:
    """Get the locales directory."""
    return Path(__file__).parent / "locales"


class I18n:
    """Multi-language i18n loader with fallback."""

    def __init__(self) -> None:
        self._translations: dict[str, dict[str, str]] = {}
        self._loaded = False

    def _load_all(self) -> None:
        """Load all language files from the locales directory."""
        if self._loaded:
            return
        locales_dir = _get_locales_dir()
        if not locales_dir.exists():
            log.warning("locales_dir_missing", path=str(locales_dir))
            self._loaded = True
            return
        for lang_file in locales_dir.glob("*.json"):
            lang_code = lang_file.stem
            try:
                data = json.loads(lang_file.read_text(encoding="utf-8"))
                self._translations[lang_code] = data
                log.info("i18n_loaded", lang=lang_code, keys=len(data))
            except Exception as e:
                log.error("i18n_load_error", lang=lang_code, error=str(e))
        self._loaded = True

    def t(self, key: str, *, lang: str = DEFAULT_LANG, **kwargs: Any) -> str:
        """Get translated text for a key.

        Args:
            key: Translation key (dot-separated, e.g., "menu.chat")
            lang: Language code
            **kwargs: Format variables

        Returns:
            Translated string, or key itself if not found.
        """
        self._load_all()
        # Try requested language
        text = self._translations.get(lang, {}).get(key)
        if text is not None:
            if kwargs:
                try:
                    return text.format(**kwargs)
                except (KeyError, IndexError):
                    return text
            return text
        # Fallback to English
        text = self._translations.get(DEFAULT_LANG, {}).get(key)
        if text is not None:
            if kwargs:
                try:
                    return text.format(**kwargs)
                except (KeyError, IndexError):
                    return text
            return text
        # Return key as last resort
        return key

    def get_available_languages(self) -> list[str]:
        """Get list of available language codes."""
        self._load_all()
        return sorted(self._translations.keys())

    def detect_language(self, telegram_lang: str | None) -> str:
        """Detect language from Telegram user language_code."""
        if not telegram_lang:
            return DEFAULT_LANG
        code = telegram_lang.lower().split("-")[0]
        result = _TELEGRAM_LANG_MAP.get(
            code, _TELEGRAM_LANG_MAP.get(telegram_lang.lower(), DEFAULT_LANG)
        )
        return result

    def format_language_list(self) -> str:
        """Format the list of supported languages for display."""
        self._load_all()
        lines = ["🌍 زبان‌های پشتیبانی‌شده:", "━━━━━━━━━━━━━━━━━"]
        available = self.get_available_languages()
        for code in available:
            name = SUPPORTED_LANGUAGES.get(code, code)
            status = "✅" if code in self._translations else "⏳"
            lines.append(f"  {status} /lang_{code} — {name}")
        lines.append("\n💡 زبان خودتون رو انتخاب کنید:")
        lines.append("مثال: /lang_fa یا /lang_en")
        return "\n".join(lines)


# Singleton
i18n = I18n()
