"""Measured behaviour of the classical descriptor: framing, quality, separation.

These tests use the real renderer and the real measurement code. They assert
*properties* (ordering, determinism, applicability) rather than magic numbers,
because a threshold copied into a test stops being a measurement.
"""

# ruff: noqa: E402 - _support.ensure_path() must run before these imports
from __future__ import annotations

import _support

_support.ensure_path()

import numpy as np
import pytest

from experiments.character_capsule import dataset
from experiments.character_capsule.vision import distance, group_stats, observe


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory):
    work = tmp_path_factory.mktemp("capsule-corpus")
    return dataset.build_corpus(work, write=True)


@pytest.fixture(scope="module")
def observations(corpus):
    return {record.key: observe(corpus.load(record)) for record in corpus.frames}


def test_framing_is_measured_not_declared(observations):
    assert observations["000001-ref-front"].framing == "portrait"
    assert observations["000001-ref-fullbody"].framing == "full_body"
    assert observations["000001-shot-fullbody"].framing == "full_body"


def test_face_quality_follows_resolution_and_clipping(observations):
    assert observations["000001-ref-front"].face_quality > 0.5
    # a full-body frame has a face too small to measure; it must say so
    assert observations["000001-ref-fullbody"].face_quality == 0.0
    assert observations["000001-ref-fullbody"].face_reason
    # a heavily clipped frame loses quality rather than scoring silently
    assert (
        observations["000001-shot-light"].face_quality
        < observations["000001-ref-front"].face_quality
    )


def test_body_is_measurable_only_when_it_is_visible(observations):
    assert observations["000001-ref-fullbody"].body_quality > 0.5
    assert observations["000001-ref-front"].measurable("body") is False
    assert observations["000001-ref-fullbody"].measurable("body") is True


def test_hair_and_wardrobe_are_not_claimed_without_a_localised_face(observations):
    full_body = observations["000001-ref-fullbody"]
    assert full_body.measurable("hair") is False
    assert full_body.measurable("wardrobe") is False


def test_observation_is_deterministic(corpus):
    first = observe(corpus.load_key("000001-ref-front"))
    second = observe(corpus.load_key("000001-ref-front"))
    assert first.to_dict() == second.to_dict()


def test_genuine_frames_are_closer_than_a_different_character(corpus, observations):
    portraits = [
        observations[record.key]
        for record in corpus.frames
        if record.purpose in {"reference", "reference_alt"}
        and record.kind == "portrait"
        and observations[record.key].face_quality > 0.0
    ]
    stats = group_stats(portraits, "face")
    reference = observations["000001-ref-front"]
    genuine = distance(reference, observations["000001-shot-expression"], "face", stats=stats)
    impostor = distance(reference, observations["000003-ref-front"], "face", stats=stats)
    assert genuine < impostor


def test_body_silhouette_separates_characters(corpus, observations):
    full_body = [
        observations[record.key]
        for record in corpus.frames
        if record.kind == "full_body" and observations[record.key].body_quality > 0.0
    ]
    stats = group_stats(full_body, "body")
    same = distance(
        observations["000001-ref-fullbody"],
        observations["000001-shot-fullbody"],
        "body",
        stats=stats,
    )
    other = distance(
        observations["000001-ref-fullbody"],
        observations["000003-ref-fullbody"],
        "body",
        stats=stats,
    )
    assert same < other


def test_distance_requires_calibration_statistics(corpus, observations):
    with pytest.raises(ValueError):
        distance(
            observations["000001-ref-front"],
            observations["000001-shot-angle"],
            "face",
            stats={"appearance": {"mean": [0.0], "std": [1.0]}},
        )


def test_white_balance_does_not_invert_the_mask():
    from experiments.character_capsule.vision import _skin_mask, _white_balance

    image = np.full((32, 32, 3), 120, dtype=np.uint8)
    image[8:24, 8:24] = np.array([230, 180, 150], dtype=np.uint8)
    mask = _skin_mask(_white_balance(image))
    assert mask.mean() > 0.05  # the skin patch is still found after balancing
