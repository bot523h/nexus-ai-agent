"""Creative copy: complete, localized, and impossible to leak as a raw key.

The required key set is *derived from the code* (the surface's failure map, the
queue envelope and the notifier's closed vocabulary) instead of being copied
here, so adding a user-visible creative message without translating it fails.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus_ai_agent.bot.creative_notify import KNOWN_FAILURE_CODES
from nexus_ai_agent.bot.creative_surface import MESSAGE_KEYS
from nexus_ai_agent.i18n import SUPPORTED_LANGUAGES, I18n

LOCALES = Path(__file__).resolve().parents[2] / "src" / "nexus_ai_agent" / "i18n" / "locales"


def required_keys() -> set[str]:
    keys = {key for key in MESSAGE_KEYS.values()}
    keys.add("creative.queued")
    keys.add("creative.completed")
    keys.update(f"creative.failed.{code}" for code in KNOWN_FAILURE_CODES)
    return keys


def _load(code: str) -> dict[str, str]:
    return json.loads((LOCALES / f"{code}.json").read_text(encoding="utf-8"))


def test_required_keys_are_complete_in_every_locale() -> None:
    needed = required_keys()
    for code in sorted(SUPPORTED_LANGUAGES):
        data = _load(code)
        missing = sorted(needed - set(data))
        assert not missing, f"{code}: untranslated creative keys {missing}"


def test_creative_key_sets_are_identical_across_locales() -> None:
    english = {key for key in _load("en") if key.startswith("creative.")}
    assert english == required_keys()
    for code in sorted(SUPPORTED_LANGUAGES):
        if code == "en":
            continue
        local = {key for key in _load(code) if key.startswith("creative.")}
        assert local == english, f"{code}: creative key drift {local ^ english}"


def test_no_creative_message_is_verbatim_english_outside_english() -> None:
    english = {key: value for key, value in _load("en").items() if key.startswith("creative.")}
    for code in sorted(SUPPORTED_LANGUAGES):
        if code == "en":
            continue
        local = _load(code)
        untranslated = sorted(key for key, value in english.items() if local.get(key) == value)
        assert not untranslated, f"{code}: still English: {untranslated}"


@pytest.mark.parametrize("code", sorted(SUPPORTED_LANGUAGES))
def test_every_creative_key_resolves_to_a_translation(code: str) -> None:
    engine = I18n()
    for key in sorted(required_keys()):
        resolved = engine.t(
            key, lang=code, job_id="j", command="edit", operation="trim", seconds="30", sha256="abc"
        )
        assert resolved != key, f"{code}:{key} fell back to the raw key"
        assert "creative." not in resolved, f"{code}:{key} leaked its key into the copy"


def test_missing_key_detection_is_not_vacuous() -> None:
    """The check above really fails when a key is removed (mutation in memory)."""
    data = _load("fa")
    data.pop("creative.failed.render_failed")
    missing = sorted(required_keys() - set(data))
    assert missing == ["creative.failed.render_failed"]
