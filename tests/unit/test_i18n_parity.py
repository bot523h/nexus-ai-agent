"""Parity gate for the 15 i18n locales (task-129).

English (``en.json``) is the source of truth: every other locale must carry
the exact same key set in the same order, with identical ``{placeholder}``
sets per key, so ``I18n.t`` never silently falls back to English and
``str.format`` never raises at runtime.
"""

from __future__ import annotations

import json
import string
from pathlib import Path

import pytest

from nexus_ai_agent.i18n import SUPPORTED_LANGUAGES, I18n

LOCALES_DIR = Path(__file__).resolve().parents[2] / "src" / "nexus_ai_agent" / "i18n" / "locales"


def _load(code: str) -> dict[str, str]:
    return json.loads((LOCALES_DIR / f"{code}.json").read_text(encoding="utf-8"))


def _placeholders(text: str) -> set[str]:
    return {
        name for _, name, _, _ in string.Formatter().parse(text) if name is not None and name != ""
    }


EN = _load("en")
CODES = sorted(SUPPORTED_LANGUAGES)


def test_supported_languages_match_locale_files() -> None:
    files = sorted(p.stem for p in LOCALES_DIR.glob("*.json"))
    assert files == CODES
    assert len(CODES) == 15


def test_all_locales_are_flat_non_empty_string_maps() -> None:
    for code in CODES:
        data = _load(code)
        assert data, f"{code}: empty locale"
        for key, value in data.items():
            assert isinstance(key, str) and key, f"{code}: bad key {key!r}"
            assert isinstance(value, str) and value.strip(), f"{code}:{key}: empty"


@pytest.mark.parametrize("code", [c for c in CODES if c != "en"])
def test_key_set_and_order_match_english(code: str) -> None:
    data = _load(code)
    assert set(data) == set(EN), (
        f"{code}: missing={sorted(set(EN) - set(data))} extra={sorted(set(data) - set(EN))}"
    )
    assert list(data) == list(EN), f"{code}: key order drifted from en"


@pytest.mark.parametrize("code", [c for c in CODES if c != "en"])
def test_placeholders_match_english(code: str) -> None:
    data = _load(code)
    for key, en_text in EN.items():
        assert _placeholders(data[key]) == _placeholders(en_text), (
            f"{code}:{key}: placeholders {_placeholders(data[key])} != en {_placeholders(en_text)}"
        )


def test_every_value_formats_with_its_placeholders() -> None:
    for code in CODES:
        data = _load(code)
        for value in data.values():
            value.format(**{name: "X" for name in _placeholders(value)})


#: Values audited (task-129) as legitimately identical to English:
#: brand/version strings plus real loanwords ("Gamification"/"Moderation"
#: are German words; "Referral" is standard Indonesian/Italian app usage;
#: Spanish "Error" shares its spelling with English).
LEGIT_EN_IDENTICAL: frozenset[tuple[str, str]] = frozenset(
    [
        ("ar", "bot.name"),
        ("ar", "bot.version"),
        ("de", "bot.name"),
        ("de", "bot.version"),
        ("de", "menu.gamification"),
        ("de", "menu.moderation"),
        ("es", "bot.name"),
        ("es", "bot.version"),
        ("es", "error.general"),
        ("fa", "bot.name"),
        ("fa", "bot.version"),
        ("fr", "bot.name"),
        ("fr", "bot.version"),
        ("fr", "menu.gamification"),
        ("hi", "bot.name"),
        ("hi", "bot.version"),
        ("id", "bot.name"),
        ("id", "bot.version"),
        ("id", "menu.referral"),
        ("it", "bot.name"),
        ("it", "bot.version"),
        ("it", "menu.gamification"),
        ("it", "menu.referral"),
        ("ja", "bot.name"),
        ("ja", "bot.version"),
        ("ko", "bot.name"),
        ("ko", "bot.version"),
        ("pt", "bot.name"),
        ("pt", "bot.version"),
        ("ru", "bot.name"),
        ("ru", "bot.version"),
        ("tr", "bot.name"),
        ("tr", "bot.version"),
        ("zh", "bot.name"),
        ("zh", "bot.version"),
    ]
)


def test_no_untranslated_english_copy_paste() -> None:
    for code in CODES:
        if code == "en":
            continue
        data = _load(code)
        for key, en_text in EN.items():
            if data[key] == en_text:
                assert (code, key) in LEGIT_EN_IDENTICAL, (
                    f"{code}:{key} is verbatim English — translate it or allowlist it"
                )


def test_loader_resolves_every_key_in_every_language() -> None:
    engine = I18n()
    assert sorted(engine.get_available_languages()) == CODES
    for code in CODES:
        for key in EN:
            resolved = engine.t(key, lang=code)
            assert resolved != key, f"{code}:{key} unresolved"


def test_persian_audit_regressions() -> None:
    fa = _load("fa")
    # No untranslated English leftovers.
    assert "Heartbeat" not in fa["status.online"]
    # ai.activated bullet keeps the 💬 emoji like en.
    assert "• 💬 مکالمه هوشمند" in fa["ai.activated"]
    # image.rate_limited keeps the full clause + placeholder.
    assert "{seconds}" in fa["image.rate_limited"]
    assert "عکس بعدی" in fa["image.rate_limited"]
