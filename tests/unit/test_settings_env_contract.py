"""Environment-variable contract for :class:`~nexus_ai_agent.config.settings.Settings`.

The model declares no ``env_prefix``. Every field therefore has to name its
environment variable explicitly via ``validation_alias``, and a field that
forgets is silently readable under its *bare field name* instead — a spelling
nobody documents and nobody sets.

``creative_temp_dir`` was exactly that case: 72 of the 73 settings read
``NEXUS_*``, and that one was reachable only as ``CREATIVE_TEMP_DIR``. An
operator following the house convention set ``NEXUS_CREATIVE_TEMP_DIR``, saw no
error, and got the default — with the creative workspace still pointing at a
predictable path in world-writable ``/tmp``.

These tests make the convention mechanical instead of a thing reviewers have to
notice.
"""

from __future__ import annotations

import pytest
from pydantic import AliasChoices
from pydantic.fields import FieldInfo

from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.config.settings import Settings


def _aliases(field: FieldInfo) -> list[str]:
    alias = field.validation_alias
    if alias is None:
        return []
    if isinstance(alias, str):
        return [alias]
    if isinstance(alias, AliasChoices):
        return [choice for choice in alias.choices if isinstance(choice, str)]
    return []


@pytest.mark.parametrize("name", sorted(Settings.model_fields))
def test_every_setting_declares_its_env_var(name: str) -> None:
    """No ``env_prefix`` means an un-aliased field has an undocumented name."""
    assert _aliases(Settings.model_fields[name]), (
        f"{name} declares no validation_alias, so its only environment variable "
        f"is the bare field name {name.upper()!r} — inconsistent with every "
        f"other setting and effectively undiscoverable"
    )


@pytest.mark.parametrize("name", sorted(Settings.model_fields))
def test_every_setting_is_reachable_with_the_nexus_prefix(name: str) -> None:
    """The house convention is ``NEXUS_<FIELD>``; deviations must be deliberate."""
    aliases = _aliases(Settings.model_fields[name])
    assert any(alias.startswith("NEXUS_") for alias in aliases), (
        f"{name} has no NEXUS_-prefixed alias (got {aliases}); an operator "
        f"following the documented convention would be silently ignored"
    )


def test_creative_temp_dir_accepts_both_spellings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prefixed name must work — and the legacy one must not break."""
    settings_module.get_settings.cache_clear()
    monkeypatch.setenv("NEXUS_CREATIVE_TEMP_DIR", "/srv/prefixed")
    assert settings_module.Settings().creative_temp_dir == "/srv/prefixed"

    monkeypatch.delenv("NEXUS_CREATIVE_TEMP_DIR")
    monkeypatch.setenv("CREATIVE_TEMP_DIR", "/srv/legacy")
    assert settings_module.Settings().creative_temp_dir == "/srv/legacy"
    settings_module.get_settings.cache_clear()


def test_the_prefixed_alias_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    settings_module.get_settings.cache_clear()
    monkeypatch.setenv("NEXUS_CREATIVE_TEMP_DIR", "/srv/prefixed")
    monkeypatch.setenv("CREATIVE_TEMP_DIR", "/srv/legacy")
    assert settings_module.Settings().creative_temp_dir == "/srv/prefixed"
    settings_module.get_settings.cache_clear()


def test_creative_scratch_does_not_default_into_shared_tmp() -> None:
    """A predictable path in world-writable /tmp is a symlink-swap target.

    ``get_settings()`` mkdir -p's this directory at import time, so the
    CWE-377/CWE-59 race was available on every process start, not just when a
    creative job ran.
    """
    default = Settings.model_fields["creative_temp_dir"].default
    assert not str(default).startswith("/tmp"), default


@pytest.mark.parametrize(
    "name",
    ["db_path", "checkpoint_path", "vector_path", "cache_dir", "creative_temp_dir"],
)
def test_writable_paths_are_relative_or_app_owned(name: str) -> None:
    """Every directory ``get_settings()`` creates must be inside the app's own tree."""
    default = str(Settings.model_fields[name].default)
    assert not default.startswith("/tmp"), f"{name} defaults into shared /tmp: {default}"
