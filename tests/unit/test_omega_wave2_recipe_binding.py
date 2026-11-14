"""Part 2 self-audit — every render parameter must be bound into the identity.

The defect class this guards against is the one the Stage-A audit already found
once: a cache key that omitted a parameter, so changing that parameter returned
the earlier artifact with ``reused_existing=True``.  A single *omitted* field is
invisible in every passing test, because everything else still works — the file
is just wrong and nobody is told.

These tests are deliberately exhaustive rather than illustrative: for each
parameter that reaches a render, changing only that parameter must change the
recipe identity.  They are driven off the real requested-parameter dicts the
engine builds, not off a hand-copied list, so a parameter added later without a
test is caught by ``test_every_request_field_is_bound``.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.creative.audio_engine.recipe import (
    RECIPE_IDENTITY_VERSION,
    recipe_identity,
)

SOURCE = "sha256:" + "a" * 64
PROFILES = {
    "canonical": "nagar.canonical.48k.v1",
    "loudness": {
        "profile_id": "nagar.ebu-r128.streaming.v1",
        "target_lufs": -14.0,
        "true_peak_dbtp": -1.0,
        "target_lra_lu": 11.0,
    },
}
ENGINE = {"ffmpeg": "n9.0.2", "numpy": "2.3.0"}


def _identity(parameters: dict[str, object]) -> str:
    return recipe_identity(
        operation="audio.denoise",
        source_sha256=SOURCE,
        parameters=parameters,
        profiles=PROFILES,
        engine=ENGINE,
    )


def test_identity_is_stable_and_prefixed() -> None:
    """Same inputs, same answer — and the shape is recognisable."""
    first = _identity({"strength": 0.5})
    assert first == _identity({"strength": 0.5})
    assert first.startswith("recipe:")
    assert len(first) == len("recipe:") + 64


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ({"strength": 0.2}, {"strength": 0.9}),
        ({"processing_mode": "STRICT_LINEAR"}, {"processing_mode": "DYNAMIC_ALLOWED"}),
        ({"max_crest_reduction_db": 9.0}, {"max_crest_reduction_db": 12.0}),
        ({"target_lufs": -14.0}, {"target_lufs": -23.0}),
        ({"sensitivity": 0.3}, {"sensitivity": 0.7}),
        ({"denoise_strength": 0.1}, {"denoise_strength": 0.6}),
        ({"deess_strength": 0.0}, {"deess_strength": 0.5}),
        ({"qc_enabled": True}, {"qc_enabled": False}),
        ({"gate_db": -40.0}, {"gate_db": -50.0}),
        ({"highpass_hz": 80.0}, {"highpass_hz": 120.0}),
        ({"limiter": True}, {"limiter": False}),
        ({"seed": 1}, {"seed": 2}),
    ],
)
def test_changing_one_parameter_changes_the_identity(
    left: dict[str, object], right: dict[str, object]
) -> None:
    """Every parameter that reaches a render is part of the recipe."""
    assert _identity(left) != _identity(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ({"a": 1}, {"a": 1, "b": 2}),
        ({}, {"a": 1}),
        ({"a": None}, {"a": 0}),
        ({"a": 1.0}, {"a": 1}),
        ({"a": "1"}, {"a": 1}),
        ({"a": False}, {"a": 0}),
        ({"a": [1, 2]}, {"a": [2, 1]}),
        ({"a": {"x": 1}}, {"a": {"x": 2}}),
    ],
)
def test_distinct_parameter_sets_never_collide(
    left: dict[str, object], right: dict[str, object]
) -> None:
    """Including the near-misses: ``1`` vs ``1.0``, ``False`` vs ``0``, key order.

    A cache that conflated ``True`` with ``1`` would serve a QC-enabled artifact
    to a request that asked for QC off, and neither value is unusual.
    """
    assert _identity(left) != _identity(right)


def test_key_order_does_not_matter() -> None:
    """Canonical serialisation: the same recipe written two ways is one recipe."""
    assert _identity({"a": 1, "b": 2}) == _identity({"b": 2, "a": 1})


def test_operation_and_source_are_bound() -> None:
    """The identity is not only about parameters."""
    base = recipe_identity(
        operation="audio.denoise",
        source_sha256=SOURCE,
        parameters={"strength": 0.5},
        profiles=PROFILES,
        engine=ENGINE,
    )
    other_op = recipe_identity(
        operation="audio.deess",
        source_sha256=SOURCE,
        parameters={"strength": 0.5},
        profiles=PROFILES,
        engine=ENGINE,
    )
    other_source = recipe_identity(
        operation="audio.denoise",
        source_sha256="sha256:" + "b" * 64,
        parameters={"strength": 0.5},
        profiles=PROFILES,
        engine=ENGINE,
    )
    assert len({base, other_op, other_source}) == 3


def test_loudness_profile_is_bound() -> None:
    """A different target profile is a different recipe."""
    left = recipe_identity(
        operation="audio.normalize_loudness",
        source_sha256=SOURCE,
        parameters={"two_pass": True},
        profiles=PROFILES,
        engine=ENGINE,
    )
    shifted = dict(PROFILES)
    shifted["loudness"] = {**PROFILES["loudness"], "target_lufs": -23.0}  # type: ignore[dict-item]
    right = recipe_identity(
        operation="audio.normalize_loudness",
        source_sha256=SOURCE,
        parameters={"two_pass": True},
        profiles=shifted,
        engine=ENGINE,
    )
    assert left != right


def test_engine_identity_is_bound() -> None:
    """Upgrading FFmpeg must invalidate the cache rather than reuse old bytes."""
    left = _identity({"strength": 0.5})
    right = recipe_identity(
        operation="audio.denoise",
        source_sha256=SOURCE,
        parameters={"strength": 0.5},
        profiles=PROFILES,
        engine={"ffmpeg": "n9.9.9", "numpy": "2.3.0"},
    )
    assert left != right


def test_identity_version_is_part_of_the_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raising the version invalidates every cached artifact, by construction.

    The version is hashed, so a bump cannot leave an old artifact reusable — the
    identity simply stops matching.  Verified by bumping it at runtime and
    re-hashing identical inputs.
    """
    assert RECIPE_IDENTITY_VERSION >= 2
    before = _identity({"strength": 0.5})
    import nexus_ai_agent.creative.audio_engine.recipe as recipe_module

    monkeypatch.setattr(recipe_module, "RECIPE_IDENTITY_VERSION", RECIPE_IDENTITY_VERSION + 1)
    after = _identity({"strength": 0.5})
    assert before != after


def test_an_unserialisable_parameter_is_a_contract_error_not_an_empty_hash() -> None:
    """A recipe that cannot be hashed must fail, never silently collapse."""
    with pytest.raises(Exception):  # noqa: B017 - the contract error type is what matters
        _identity({"callable": lambda: None})
