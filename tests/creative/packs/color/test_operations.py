from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.speech_cinema.color_imaging import (
    hdr_to_sdr_filmic_tonemap,
    load_color_png,
    matrix_sqrt_spd,
    save_color_png,
    shot_to_shot_color_match_ot,
    validate_spd_covariance,
    verify_color_png,
    white_balance_chromatic_adaptation,
)


def test_gray_world_bradford_neutralizes_cast_without_clipping() -> None:
    image = np.full((4, 5, 3), [2.0, 1.0, 0.5], dtype=float)
    result = white_balance_chromatic_adaptation(image)
    assert np.allclose(result.mean(axis=(0, 1)), result.mean(), rtol=1e-6, atol=1e-6)
    assert result.max() > 1.0  # no implicit clipping


def test_kelvin_range_and_alias_validation() -> None:
    image = np.ones((2, 2, 3), dtype=float)
    assert np.all(np.isfinite(white_balance_chromatic_adaptation(image, kelvin=6500)))
    with pytest.raises(ValueError):
        white_balance_chromatic_adaptation(image, kelvin=40001)
    with pytest.raises(ValueError):
        white_balance_chromatic_adaptation(image, method="bad")  # type: ignore[arg-type]


def test_filmic_tonemap_is_nonnegative_and_luminance_monotonic() -> None:
    hdr = np.stack([np.linspace(0, 100, 101)] * 3, axis=-1)
    mapped = hdr_to_sdr_filmic_tonemap(hdr)
    lum = mapped @ np.array([0.2126, 0.7152, 0.0722])
    assert np.all(np.isfinite(mapped)) and np.all(mapped >= 0)
    assert np.all(np.diff(lum) >= -1e-12)
    assert mapped[-1, 0] < 1.0


def test_ot_match_reproduces_reference_mean_and_covariance() -> None:
    rng = np.random.default_rng(4)
    source = rng.normal(size=(500, 3)) @ np.array(
        [[1.0, 0.2, 0.0], [0.0, 1.0, 0.1], [0.0, 0.0, 1.0]]
    )
    reference = rng.normal(size=(500, 3)) @ np.array(
        [[0.8, 0.1, 0.0], [0.0, 0.7, 0.1], [0.0, 0.0, 0.9]]
    ) + [0.2, 0.4, 0.1]
    matched = shot_to_shot_color_match_ot(source, reference)
    assert np.allclose(matched.mean(0), reference.mean(0), atol=1e-10)
    assert np.allclose(
        np.cov(matched, rowvar=False, bias=True),
        np.cov(reference, rowvar=False, bias=True),
        atol=1e-8,
    )
    assert np.allclose(shot_to_shot_color_match_ot(source, reference, alpha=0), source)


def test_spd_validation_and_square_root() -> None:
    covariance = np.diag([1.0, 2.0, 3.0])
    root = matrix_sqrt_spd(covariance)
    assert np.allclose(root @ root, covariance)
    with pytest.raises(ValueError):
        validate_spd_covariance(np.diag([1.0, 0.0, 2.0]))


def test_png_helpers(tmp_path) -> None:
    image = np.array([[[0.0, 0.5, 1.0], [0.2, 0.4, 0.6]]])
    path = tmp_path / "color.png"
    save_color_png(image, path)
    assert verify_color_png(path)
    assert np.allclose(load_color_png(path), image, atol=1 / 255)
    with pytest.raises(ValueError):
        save_color_png(image * 2, tmp_path / "bad.png")
