"""Wave 4 — WYSIWYS structured preview readback in exact rational time.

Zero mocks, zero floats.  The three properties under test are the three ways a
preview can lie:

* **arithmetic** — 10,000 cuts at NTSC/PAL/film rates must total exactly their
  frame count with zero drift (a float implementation accumulates error);
* **identity** — tampering with *any* field of the readback (including a
  bilingual label, the timebase, or the operation order) must break
  ``preview_digest``, so an approval cannot describe an edit that was not shown;
* **comparison** — diffing a revision against itself must report
  ``identical=True`` with both drifts exactly zero.

Coverage target for ``preview_readback``: ``>= 98%`` line *and* branch.
"""

from __future__ import annotations

import dataclasses
from fractions import Fraction
from typing import Any

import pytest

from nexus_ai_agent.creative.studio.preview_readback import (
    BILINGUAL_LANGUAGES,
    ParameterReadbackDelta,
    PreviewReadbackError,
    RationalTime,
    RationalTimeSpan,
    RevisionComparisonReport,
    StructuredPreviewReadback,
    StudioPreviewReadbackBuilder,
    TimelineOperationPreview,
    compare_revisions,
    frames_for,
    lcm_timebase,
    verify_wysiwys_readback,
)

NTSC = (30000, 1001)
NTSC_FILM = (24000, 1001)
PAL = (25, 1)
FILM = (24, 1)

LABELS = {"fa": "برش", "en": "Cut"}


def _builder(
    *,
    timebase: Any = NTSC,
    revision_id: str = "rev-1",
    revision_number: int = 1,
    tenant_id: str = "tenant-studio-1",
    project_id: str = "proj-cinema-1",
    principal_id: str = "prn_" + "1" * 32,
) -> StudioPreviewReadbackBuilder:
    return StudioPreviewReadbackBuilder(
        tenant_id=tenant_id,
        project_id=project_id,
        revision_id=revision_id,
        revision_number=revision_number,
        principal_id=principal_id,
        timebase=timebase,
    )


def _simple_readback(**overrides: Any) -> StructuredPreviewReadback:
    builder = _builder(**overrides)
    builder.add_operation(
        operation="timeline.cut",
        target_clip_id="clip-1",
        span=(0, "5/2"),
        labels=LABELS,
    )
    builder.add_operation(
        operation="timeline.cut",
        target_clip_id="clip-2",
        span=("5/2", "75757/30000"),
        labels=LABELS,
        deltas=[
            ParameterReadbackDelta.numeric(
                "opacity", 1, Fraction(1, 2), labels={"fa": "شفافیت", "en": "Opacity"}
            )
        ],
    )
    return builder.build_readback()


# --------------------------------------------------------------------------- #
# Exact rational time
# --------------------------------------------------------------------------- #
class TestRationalTime:
    def test_int_fraction_and_string_forms_agree(self) -> None:
        assert RationalTime.of(3).seconds == Fraction(3)
        assert RationalTime.of(Fraction(3, 2)).seconds == Fraction(3, 2)
        assert RationalTime.of("3/2").seconds == Fraction(3, 2)
        assert RationalTime.of(" 3/2 ").seconds == Fraction(3, 2)

    def test_microsecond_round_trip_is_exact(self) -> None:
        time = RationalTime.from_microseconds(1_500_000)
        assert time.seconds == Fraction(3, 2)
        assert time.microseconds == 1_500_000
        assert time.to_seconds_text() == "3/2"

    def test_microseconds_refuse_a_sub_microsecond_instant(self) -> None:
        time = RationalTime.of(Fraction(1, 3))
        with pytest.raises(PreviewReadbackError) as excinfo:
            _ = time.microseconds
        assert excinfo.value.reason == "sub_microsecond_time"

    def test_integer_seconds_render_without_a_slash(self) -> None:
        assert RationalTime.of(2).to_seconds_text() == "2"

    def test_floats_are_refused_outright(self) -> None:
        for value in (0.1, 1.5, 2.0):
            with pytest.raises(PreviewReadbackError) as excinfo:
                RationalTime.of(value)
            assert excinfo.value.reason == "invalid_time"

    def test_booleans_and_empty_strings_are_refused(self) -> None:
        with pytest.raises(PreviewReadbackError):
            RationalTime.of(True)
        with pytest.raises(PreviewReadbackError):
            RationalTime.of("   ")
        with pytest.raises(PreviewReadbackError) as excinfo:
            RationalTime.of("not-a-number")
        assert excinfo.value.reason == "invalid_time"
        with pytest.raises(PreviewReadbackError):
            RationalTime.of("1/0")

    def test_microsecond_constructor_rejects_non_ints(self) -> None:
        with pytest.raises(PreviewReadbackError):
            RationalTime.from_microseconds(True)
        with pytest.raises(PreviewReadbackError):
            RationalTime.from_microseconds(1.5)  # type: ignore[arg-type]

    def test_additions_and_ordering(self) -> None:
        first = RationalTime.of("1/3")
        second = RationalTime.of("2/3")
        assert (first + second).seconds == 1
        assert first < second
        assert first <= first
        assert (second <= first) is False


class TestRationalTimeSpan:
    def test_half_open_containment(self) -> None:
        span = RationalTimeSpan.of(0, 10)
        assert span.contains(RationalTime.of(0)) is True
        assert span.contains(RationalTime.of("9/1")) is True
        assert span.contains(RationalTime.of(10)) is False

    def test_touching_spans_do_not_overlap(self) -> None:
        first = RationalTimeSpan.of(0, 5)
        second = RationalTimeSpan.of(5, 10)
        third = RationalTimeSpan.of("9/2", 10)
        assert first.overlaps(second) is False
        assert first.overlaps(third) is True

    def test_duration_is_exact(self) -> None:
        assert RationalTimeSpan.of(0, "75757/30000").duration.seconds == Fraction(75757, 30000)
        assert RationalTimeSpan.from_microseconds(0, 2_000_000).duration.seconds == 2

    def test_equal_endpoints_are_refused(self) -> None:
        with pytest.raises(PreviewReadbackError) as excinfo:
            RationalTimeSpan.of(5, 5)
        assert excinfo.value.reason == "invalid_span"

    def test_reversed_endpoints_are_refused(self) -> None:
        with pytest.raises(PreviewReadbackError):
            RationalTimeSpan.of(5, 1)

    def test_non_rational_endpoints_are_refused(self) -> None:
        with pytest.raises(PreviewReadbackError) as excinfo:
            RationalTimeSpan(start=0, end=10)  # type: ignore[arg-type]
        assert excinfo.value.reason == "invalid_time"

    def test_round_trip_dict(self) -> None:
        assert RationalTimeSpan.of(0, "3/2").to_dict() == {
            "start": "0",
            "end": "3/2",
            "duration": "3/2",
        }


class TestFrameArithmetic:
    @pytest.mark.parametrize(
        ("timebase", "expected_10000_frames_seconds"),
        [
            ((30000, 1001), Fraction(10_000 * 1001, 30000)),
            ((24000, 1001), Fraction(10_000 * 1001, 24000)),
            ((25, 1), Fraction(10_000, 25)),
            ((24, 1), Fraction(10_000, 24)),
        ],
    )
    def test_ten_thousand_cuts_land_exactly_on_their_frames(
        self, timebase: tuple[int, int], expected_10000_frames_seconds: Fraction
    ) -> None:
        """The drift test: 10,000 cuts must total exactly 10,000 frames."""
        numerator, denominator = timebase
        frame = Fraction(denominator, numerator)
        builder = _builder(timebase=timebase)
        for index in range(10_000):
            builder.add_operation(
                operation="timeline.cut",
                target_clip_id=f"clip-{index}",
                span=(frame * index, frame * (index + 1)),
                labels=LABELS,
            )
        readback = builder.build_readback()
        assert readback.frame_count() == 10_000
        assert readback.total_span.duration.seconds == expected_10000_frames_seconds
        # The cut boundary between consecutive clips is shared, not duplicated.
        assert len(readback.cut_points()) == 10_001
        report = compare_revisions(readback, readback)
        assert report.identical is True
        assert report.frame_drift == 0
        assert report.duration_drift_us == (
            0 if (expected_10000_frames_seconds * 1_000_000).denominator == 1 else 0
        )
        assert readback.preview_digest.startswith("sha256:")

    def test_frame_count_uses_half_up_rounding_and_never_a_float(self) -> None:
        assert frames_for(RationalTimeSpan.of(0, "1/3"), Fraction(24)) == 8
        assert frames_for(RationalTimeSpan.of(0, "1/2"), Fraction(25)) == 13  # 12.5 -> 13
        assert frames_for(RationalTimeSpan.of(0, 1), Fraction(30000, 1001)) == 30

    def test_frames_for_refuses_a_non_positive_timebase(self) -> None:
        with pytest.raises(PreviewReadbackError) as excinfo:
            frames_for(RationalTimeSpan.of(0, 1), Fraction(0))
        assert excinfo.value.reason == "invalid_timebase"

    def test_timebase_parsing_accepts_all_supported_spellings(self) -> None:
        for spelling in ((30000, 1001), "30000/1001", Fraction(30000, 1001)):
            builder = _builder(timebase=spelling)
            builder.add_operation(
                operation="timeline.cut", target_clip_id="c", span=(0, 1), labels=LABELS
            )
            assert builder.build_readback().timebase == Fraction(30000, 1001)

    @pytest.mark.parametrize(
        "bad",
        [
            True,
            "30000",
            (30000,),
            (30000, 1001, 7),
            (0, 1001),
            (30000, 0),
            ("x", "y"),
            ("30000/x"),
            Fraction(0),
            Fraction(-1, 2),
        ],
    )
    def test_invalid_timebases_are_refused(self, bad: Any) -> None:
        with pytest.raises(PreviewReadbackError) as excinfo:
            _builder(timebase=bad).build_readback()
        assert excinfo.value.reason == "invalid_timebase"

    def test_lcm_timebase_finds_an_exact_common_rate(self) -> None:
        assert lcm_timebase(NTSC, PAL) == Fraction(30000)
        assert lcm_timebase(NTSC, NTSC_FILM) == Fraction(120000)
        assert lcm_timebase("25/1") == Fraction(25)
        with pytest.raises(PreviewReadbackError):
            lcm_timebase()


# --------------------------------------------------------------------------- #
# Bilingual labels
# --------------------------------------------------------------------------- #
class TestBilingual:
    def test_every_human_string_needs_both_languages(self) -> None:
        assert BILINGUAL_LANGUAGES == ("fa", "en")
        with pytest.raises(PreviewReadbackError) as excinfo:
            _builder().add_operation(
                operation="timeline.cut",
                target_clip_id="c",
                span=(0, 1),
                labels={"en": "Cut"},
            )
        assert excinfo.value.reason == "missing_translation"

    def test_blank_translations_are_refused(self) -> None:
        with pytest.raises(PreviewReadbackError):
            TimelineOperationPreview(
                operation="timeline.cut",
                target_clip_id="c",
                span=RationalTimeSpan.of(0, 1),
                labels={"fa": "   ", "en": "Cut"},
            )

    def test_extra_languages_are_dropped_not_carried_into_the_digest(self) -> None:
        preview = TimelineOperationPreview(
            operation="timeline.cut",
            target_clip_id="c",
            span=RationalTimeSpan.of(0, 1),
            labels={"fa": "برش", "en": "Cut", "de": "Schnitt"},
        )
        assert set(preview.labels) == {"fa", "en"}
        assert set(preview.to_dict()["labels"]) == {"fa", "en"}
        assert "de" not in preview.to_dict()["labels"]

    def test_non_mapping_labels_are_refused(self) -> None:
        with pytest.raises(PreviewReadbackError):
            TimelineOperationPreview(
                operation="timeline.cut",
                target_clip_id="c",
                span=RationalTimeSpan.of(0, 1),
                labels=["fa", "en"],  # type: ignore[arg-type]
            )

    def test_render_switches_language_and_keeps_exact_times(self) -> None:
        readback = _simple_readback()
        english = readback.render("en")
        persian = readback.render("fa")
        assert "Opacity: 1 -> 1/2" in english
        assert "شفافیت: 1 -> 1/2" in persian
        assert english != persian
        with pytest.raises(PreviewReadbackError) as excinfo:
            readback.render("de")
        assert excinfo.value.reason == "unsupported_language"

    def test_language_order_must_be_a_permutation(self) -> None:
        readback = _simple_readback()
        with pytest.raises(PreviewReadbackError) as excinfo:
            dataclasses.replace(
                readback,
                language_order=("fa", "en", "de"),
                preview_digest="",
            )
        assert excinfo.value.reason == "invalid_language_order"

    def test_language_order_is_part_of_the_digest(self) -> None:
        readback = _simple_readback()
        swapped = dataclasses.replace(readback, language_order=("en", "fa"), preview_digest="")
        assert swapped.preview_digest != readback.preview_digest


# --------------------------------------------------------------------------- #
# Parameter deltas
# --------------------------------------------------------------------------- #
class TestParameterDeltas:
    def test_numeric_deltas_are_stored_as_exact_text(self) -> None:
        delta = ParameterReadbackDelta.numeric(
            "opacity", Fraction(1), Fraction(1, 3), labels={"fa": "شفافیت", "en": "Opacity"}
        )
        assert (delta.before, delta.after) == ("1", "1/3")
        assert delta.is_numeric is True
        assert delta.to_dict()["after"] == "1/3"

    def test_a_numeric_delta_from_a_float_is_refused(self) -> None:
        with pytest.raises(PreviewReadbackError):
            ParameterReadbackDelta.numeric("opacity", 0.5, 1, labels={"fa": "x", "en": "y"})

    def test_a_numeric_delta_with_a_non_numeric_text_is_refused(self) -> None:
        with pytest.raises(PreviewReadbackError) as excinfo:
            ParameterReadbackDelta(
                parameter="opacity",
                before="half",
                after="1",
                labels={"fa": "x", "en": "y"},
                is_numeric=True,
            )
        assert excinfo.value.reason == "invalid_numeric_parameter"

    def test_decimal_text_stays_a_literal_not_a_rounded_float(self) -> None:
        delta = ParameterReadbackDelta(
            parameter="gain",
            before="0.125",
            after="0.25",
            labels={"fa": "بهره", "en": "Gain"},
            unit="dB",
            is_numeric=True,
        )
        assert delta.to_dict()["before"] == "0.125"

    @pytest.mark.parametrize("field_name", ["parameter", "before", "after"])
    def test_blank_fields_are_refused(self, field_name: str) -> None:
        kwargs: dict[str, Any] = {
            "parameter": "opacity",
            "before": "1",
            "after": "1/2",
            "labels": {"fa": "x", "en": "y"},
        }
        kwargs[field_name] = "  "
        with pytest.raises(PreviewReadbackError):
            ParameterReadbackDelta(**kwargs)

    def test_duplicate_parameters_in_one_operation_are_refused(self) -> None:
        delta = ParameterReadbackDelta(
            parameter="opacity", before="1", after="1/2", labels={"fa": "x", "en": "y"}
        )
        with pytest.raises(PreviewReadbackError) as excinfo:
            TimelineOperationPreview(
                operation="timeline.cut",
                target_clip_id="c",
                span=RationalTimeSpan.of(0, 1),
                deltas=(delta, delta),
                labels=LABELS,
            )
        assert excinfo.value.reason == "duplicate_parameter"


# --------------------------------------------------------------------------- #
# Operation preview
# --------------------------------------------------------------------------- #
class TestOperationPreview:
    def test_destructive_and_reversible_are_mutually_exclusive(self) -> None:
        with pytest.raises(PreviewReadbackError) as excinfo:
            TimelineOperationPreview(
                operation="timeline.delete",
                target_clip_id="c",
                span=RationalTimeSpan.of(0, 1),
                labels=LABELS,
                reversible=True,
                destructive=True,
            )
        assert excinfo.value.reason == "destructive_is_irreversible"

    def test_a_destructive_operation_is_explicit_about_it(self) -> None:
        preview = TimelineOperationPreview(
            operation="timeline.delete",
            target_clip_id="c",
            span=RationalTimeSpan.of(0, 1),
            labels=LABELS,
            reversible=False,
            destructive=True,
        )
        assert preview.to_dict()["destructive"] is True

    @pytest.mark.parametrize("field_name", ["operation", "target_clip_id"])
    def test_blank_identifiers_are_refused(self, field_name: str) -> None:
        kwargs: dict[str, Any] = {
            "operation": "timeline.cut",
            "target_clip_id": "c",
            "span": RationalTimeSpan.of(0, 1),
            "labels": LABELS,
        }
        kwargs[field_name] = " "
        with pytest.raises(PreviewReadbackError):
            TimelineOperationPreview(**kwargs)

    def test_a_non_span_is_refused(self) -> None:
        with pytest.raises(PreviewReadbackError):
            TimelineOperationPreview(
                operation="timeline.cut",
                target_clip_id="c",
                span=(0, 1),  # type: ignore[arg-type]
                labels=LABELS,
            )

    def test_non_delta_items_are_refused(self) -> None:
        with pytest.raises(PreviewReadbackError):
            TimelineOperationPreview(
                operation="timeline.cut",
                target_clip_id="c",
                span=RationalTimeSpan.of(0, 1),
                deltas=("not-a-delta",),  # type: ignore[arg-type]
                labels=LABELS,
            )


# --------------------------------------------------------------------------- #
# WYSIWYS: the digest must break on ANY tampering
# --------------------------------------------------------------------------- #
class TestWysiwys:
    def test_a_freshly_built_readback_verifies(self) -> None:
        readback = _simple_readback()
        assert readback.preview_digest.startswith("sha256:")
        assert verify_wysiwys_readback(readback) is True
        assert verify_wysiwys_readback(readback, expected_digest=readback.preview_digest) is True

    def test_a_wrong_expected_digest_fails(self) -> None:
        readback = _simple_readback()
        assert verify_wysiwys_readback(readback, expected_digest="sha256:" + "0" * 64) is False

    def test_a_non_readback_fails_closed(self) -> None:
        assert verify_wysiwys_readback(None) is False
        assert verify_wysiwys_readback({"preview_digest": "sha256:" + "0" * 64}) is False
        assert verify_wysiwys_readback("readback") is False

    @pytest.mark.parametrize(
        "field_name",
        [
            "readback_id",
            "tenant_id",
            "project_id",
            "revision_id",
            "principal_id",
            "revision_number",
        ],
    )
    def test_tampering_with_an_identity_field_breaks_the_digest(self, field_name: str) -> None:
        readback = _simple_readback()
        forged = dataclasses.replace(readback, preview_digest="")
        object.__setattr__(forged, "preview_digest", readback.preview_digest)
        # Keep the *old* digest while changing the content: exactly the forgery a
        # compromised UI would attempt.
        object.__setattr__(
            forged,
            field_name,
            readback.revision_number + 1
            if field_name == "revision_number"
            else str(getattr(readback, field_name)) + "-tampered",
        )
        assert forged.preview_digest == readback.preview_digest
        assert verify_wysiwys_readback(forged) is False

    def test_tampering_with_the_timebase_breaks_the_digest(self) -> None:
        readback = _simple_readback()
        # A forger would paste a *raw* timebase tuple onto the stored object, not
        # go through the coercing constructor — that is what this reproduces.
        forged = dataclasses.replace(readback, preview_digest="")
        object.__setattr__(forged, "timebase", (25, 1))
        object.__setattr__(forged, "preview_digest", readback.preview_digest)
        assert verify_wysiwys_readback(forged) is False

    def test_tampering_with_a_bilingual_label_breaks_the_digest(self) -> None:
        readback = _simple_readback()
        original = readback.operations[0]
        swapped = dataclasses.replace(original, labels={"fa": "بریدن", "en": "Trim"})
        forged = dataclasses.replace(
            readback, operations=(swapped, *readback.operations[1:]), preview_digest=""
        )
        object.__setattr__(forged, "preview_digest", readback.preview_digest)
        assert verify_wysiwys_readback(forged) is False

    def test_reordering_operations_breaks_the_digest(self) -> None:
        readback = _simple_readback()
        forged = dataclasses.replace(
            readback, operations=tuple(reversed(readback.operations)), preview_digest=""
        )
        assert forged.operations != readback.operations
        assert forged.preview_digest != readback.preview_digest
        # And an old digest cannot be pasted onto the reordered content.
        object.__setattr__(forged, "preview_digest", readback.preview_digest)
        assert verify_wysiwys_readback(forged) is False

    def test_moving_a_cut_breaks_the_digest(self) -> None:
        readback = _simple_readback()
        moved = dataclasses.replace(
            readback.operations[0], span=RationalTimeSpan.of(0, "75757/30000")
        )
        forged = dataclasses.replace(
            readback, operations=(moved, *readback.operations[1:]), preview_digest=""
        )
        object.__setattr__(forged, "preview_digest", readback.preview_digest)
        assert verify_wysiwys_readback(forged) is False

    def test_forging_supplied_and_content_digests_cannot_agree(self) -> None:
        readback = _simple_readback()
        with pytest.raises(PreviewReadbackError) as excinfo:
            StructuredPreviewReadback(
                readback_id="rdb_forged",
                tenant_id=readback.tenant_id,
                project_id=readback.project_id,
                revision_id=readback.revision_id,
                revision_number=readback.revision_number,
                principal_id=readback.principal_id,
                timebase=readback.timebase,
                operations=readback.operations,
                summary=readback.summary,
                preview_digest="sha256:" + "1" * 64,
            )
        assert excinfo.value.reason == "preview_digest_mismatch"

    def test_a_lie_about_the_schema_is_refused(self) -> None:
        readback = _simple_readback()
        with pytest.raises(PreviewReadbackError) as excinfo:
            dataclasses.replace(readback, schema="nexus.studio.preview_readback.v0")
        assert excinfo.value.reason == "invalid_schema"

    def test_a_readback_rebuilt_from_its_own_digest_is_trusted(self) -> None:
        """The digest-matches branch: an honest serialisation round trip verifies."""
        original = _simple_readback()
        payload = original.to_dict()
        rebuilt = StructuredPreviewReadback(
            readback_id=payload["readback_id"],
            tenant_id=payload["tenant_id"],
            project_id=payload["project_id"],
            revision_id=payload["revision_id"],
            revision_number=payload["revision_number"],
            principal_id=payload["principal_id"],
            timebase=payload["timebase"],
            operations=original.operations,
            summary=payload["summary"],
            language_order=tuple(payload["language_order"]),
            preview_digest=payload["preview_digest"],
        )
        assert rebuilt.preview_digest == original.preview_digest
        assert verify_wysiwys_readback(rebuilt) is True

    def test_verification_fails_closed_when_the_readback_cannot_be_hashed(self) -> None:
        readback = _simple_readback()
        # A field so corrupted that canonicalisation itself raises: verification
        # must answer False, never propagate into the approval path.
        object.__setattr__(readback, "timebase", "not-a-timebase")
        assert verify_wysiwys_readback(readback) is False

    def test_the_digest_is_stable_across_equivalent_constructions(self) -> None:
        first = _simple_readback()
        second = _simple_readback()
        assert first.preview_digest == second.preview_digest
        assert first.readback_id == second.readback_id
        assert first.to_dict() == second.to_dict()

    def test_the_digest_covers_the_whole_canonical_payload(self) -> None:
        readback = _simple_readback()
        payload = readback.canonical_payload()
        assert "preview_digest" not in payload
        assert payload["timebase"] == "30000/1001"
        assert payload["language_order"] == ["fa", "en"]
        assert payload["summary"]["en"]
        assert readback.to_dict()["preview_digest"] == readback.preview_digest

    @pytest.mark.parametrize(
        "field_name", ["readback_id", "tenant_id", "project_id", "revision_id", "principal_id"]
    )
    def test_blank_identity_fields_are_refused(self, field_name: str) -> None:
        readback = _simple_readback()
        kwargs = {
            "readback_id": readback.readback_id,
            "tenant_id": readback.tenant_id,
            "project_id": readback.project_id,
            "revision_id": readback.revision_id,
            "revision_number": readback.revision_number,
            "principal_id": readback.principal_id,
            "timebase": readback.timebase,
            "operations": readback.operations,
            "summary": readback.summary,
        }
        kwargs[field_name] = "  "
        with pytest.raises(PreviewReadbackError):
            StructuredPreviewReadback(**kwargs)  # type: ignore[arg-type]

    @pytest.mark.parametrize("revision_number", [0, -1, True, "1", 1.0])
    def test_invalid_revision_numbers_are_refused(self, revision_number: Any) -> None:
        readback = _simple_readback()
        with pytest.raises(PreviewReadbackError) as excinfo:
            StructuredPreviewReadback(
                readback_id=readback.readback_id,
                tenant_id=readback.tenant_id,
                project_id=readback.project_id,
                revision_id=readback.revision_id,
                revision_number=revision_number,
                principal_id=readback.principal_id,
                timebase=readback.timebase,
                operations=readback.operations,
                summary=readback.summary,
            )
        assert excinfo.value.reason == "invalid_revision_number"

    def test_non_operation_items_are_refused(self) -> None:
        readback = _simple_readback()
        with pytest.raises(PreviewReadbackError):
            StructuredPreviewReadback(
                readback_id=readback.readback_id,
                tenant_id=readback.tenant_id,
                project_id=readback.project_id,
                revision_id=readback.revision_id,
                revision_number=readback.revision_number,
                principal_id=readback.principal_id,
                timebase=readback.timebase,
                operations=("not-an-operation",),  # type: ignore[arg-type]
                summary=readback.summary,
            )


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #
class TestBuilder:
    def test_operations_accumulate_in_order(self) -> None:
        builder = _builder()
        assert builder.operation_count == 0
        builder.add_operation(
            operation="timeline.cut", target_clip_id="a", span=(0, 1), labels=LABELS
        ).add_operation(operation="timeline.cut", target_clip_id="b", span=(1, 2), labels=LABELS)
        assert builder.operation_count == 2
        readback = builder.build_readback()
        assert [op.target_clip_id for op in readback.operations] == ["a", "b"]

    def test_default_labels_are_generated_in_both_languages(self) -> None:
        builder = _builder()
        builder.add_operation(operation="timeline.mark", target_clip_id="clip-9", span=(0, 1))
        labels = builder.build_readback().operations[0].labels
        assert set(labels) == {"fa", "en"}
        assert "clip-9" in labels["en"]

    def test_default_summary_counts_the_operations(self) -> None:
        builder = _builder()
        builder.add_operation(operation="timeline.cut", target_clip_id="a", span=(0, 1))
        readback = builder.build_readback()
        assert "1" in readback.summary["en"]
        assert readback.summary["fa"]

    def test_a_custom_summary_is_honoured_and_needs_both_languages(self) -> None:
        builder = _builder()
        builder.with_summary({"fa": "ویرایش", "en": "Edit"})
        assert builder.build_readback().summary == {"fa": "ویرایش", "en": "Edit"}
        with pytest.raises(PreviewReadbackError):
            builder.with_summary({"en": "Edit"})

    def test_the_readback_id_can_be_pinned_or_derived(self) -> None:
        derived = _builder().build_readback()
        assert derived.readback_id.startswith("rdb_")
        pinned = _builder().with_readback_id("rdb_explicit").build_readback()
        assert pinned.readback_id == "rdb_explicit"
        with pytest.raises(PreviewReadbackError):
            _builder().with_readback_id("  ")

    def test_span_accepts_a_span_object_or_a_pair(self) -> None:
        builder = _builder()
        builder.add_operation(
            operation="timeline.cut",
            target_clip_id="a",
            span=RationalTimeSpan.of(0, 1),
            labels=LABELS,
        )
        builder.add_operation(
            operation="timeline.cut", target_clip_id="b", span=("1", "2"), labels=LABELS
        )
        readback = builder.build_readback()
        assert readback.operations[1].span.to_dict()["start"] == "1"

    @pytest.mark.parametrize("bad_span", [("0",), "0-1", [0, 1], None])
    def test_invalid_spans_are_refused(self, bad_span: Any) -> None:
        with pytest.raises(PreviewReadbackError) as excinfo:
            _builder().add_operation(
                operation="timeline.cut",
                target_clip_id="a",
                span=bad_span,
                labels=LABELS,
            )
        assert excinfo.value.reason == "invalid_span"

    def test_an_empty_readback_is_representable_and_hashes(self) -> None:
        readback = _builder().build_readback()
        assert readback.operations == ()
        assert readback.frame_count() == 0
        assert readback.total_span.to_dict() == {"start": "0", "end": "1", "duration": "1"}
        assert verify_wysiwys_readback(readback) is True
        assert readback.cut_points() == ()
        assert readback.render("en")


# --------------------------------------------------------------------------- #
# Revision comparison
# --------------------------------------------------------------------------- #
def _rebuild(
    base: StructuredPreviewReadback,
    *,
    revision_id: str,
    revision_number: int,
    replace: dict[str, RationalTimeSpan] | None = None,
    drop: set[str] | None = None,
) -> StructuredPreviewReadback:
    """Re-emit ``base`` as a new revision, moving or dropping named cuts.

    Every untouched operation keeps its exact span, so a "moved one cut" edit has
    zero duration drift — which is what makes the changed/added/removed split
    meaningful instead of conflated with a length change.
    """
    builder = _builder(
        revision_id=revision_id, revision_number=revision_number, timebase=base.timebase
    )
    for operation in base.operations:
        if drop and operation.target_clip_id in drop:
            continue
        span = (replace or {}).get(operation.target_clip_id, operation.span)
        builder.add_operation(
            operation=operation.operation,
            target_clip_id=operation.target_clip_id,
            span=span,
            deltas=operation.deltas,
            labels=operation.labels,
            reversible=operation.reversible,
            destructive=operation.destructive,
        )
    return builder.build_readback()


def _comparison_labels() -> dict[str, str]:
    return {"fa": "مقایسه", "en": "Comparison"}


class TestCompareRevisions:
    def test_a_revision_compared_with_itself_is_identical(self) -> None:
        readback = _simple_readback()
        report = compare_revisions(readback, readback)
        assert report.identical is True
        assert report.frame_drift == 0
        assert report.duration_drift_us == 0
        assert report.added_operations == ()
        assert report.removed_operations == ()
        assert report.changed_operations == ()
        assert report.timebase_changed is False
        assert report.comparison_digest.startswith("sha256:")
        assert set(report.labels) == {"fa", "en"}
        assert report.to_dict()["identical"] is True

    def test_two_builds_of_the_same_design_compare_identical(self) -> None:
        assert compare_revisions(_simple_readback(), _simple_readback()).identical is True

    def test_a_moved_cut_is_reported_as_changed_not_add_plus_remove(self) -> None:
        base = _simple_readback()
        moved_first = dataclasses.replace(base.operations[0], span=RationalTimeSpan.of("1/2", 3))
        head = _builder(revision_id="rev-2", revision_number=2)
        head.add_operation(
            operation=moved_first.operation,
            target_clip_id=moved_first.target_clip_id,
            span=moved_first.span,
            labels=moved_first.labels,
        )
        head.add_operation(
            operation="timeline.cut",
            target_clip_id="clip-2",
            span=base.operations[1].span,
            labels=LABELS,
            deltas=base.operations[1].deltas,
        )
        report = compare_revisions(base, head.build_readback())
        assert report.changed_operations == ("timeline.cut@clip-1",)
        assert report.added_operations == ()
        assert report.removed_operations == ()
        assert report.identical is False

    def test_an_added_operation_is_reported(self) -> None:
        base = _simple_readback()
        builder = _builder(revision_id="rev-2", revision_number=2)
        for operation in base.operations:
            builder.add_operation(
                operation=operation.operation,
                target_clip_id=operation.target_clip_id,
                span=operation.span,
                deltas=operation.deltas,
                labels=operation.labels,
            )
        builder.add_operation(
            operation="timeline.mark", target_clip_id="clip-3", span=(0, 1), labels=LABELS
        )
        report = compare_revisions(base, builder.build_readback())
        assert report.added_operations == ("timeline.mark@clip-3",)
        assert report.removed_operations == ()
        assert report.identical is False

    def test_a_removed_operation_is_reported(self) -> None:
        base = _simple_readback()
        report = compare_revisions(
            base, _rebuild(base, revision_id="rev-2", revision_number=2, drop={"clip-2"})
        )
        assert report.removed_operations == ("timeline.cut@clip-2",)
        assert report.identical is False

    def test_a_changed_parameter_is_a_changed_operation(self) -> None:
        base = _simple_readback()
        builder = _builder(revision_id="rev-2", revision_number=2)
        for operation in base.operations:
            deltas = tuple(
                dataclasses.replace(delta, after="1") if delta.parameter == "opacity" else delta
                for delta in operation.deltas
            )
            builder.add_operation(
                operation=operation.operation,
                target_clip_id=operation.target_clip_id,
                span=operation.span,
                deltas=deltas,
                labels=operation.labels,
            )
        report = compare_revisions(base, builder.build_readback())
        assert report.changed_operations == ("timeline.cut@clip-2",)

    def test_duration_drift_is_exact_microseconds(self) -> None:
        base = _builder(revision_id="rev-1")
        base.add_operation(operation="timeline.cut", target_clip_id="c", span=(0, 1), labels=LABELS)
        head = _builder(revision_id="rev-2", revision_number=2)
        head.add_operation(
            operation="timeline.cut",
            target_clip_id="c",
            span=(0, "1000001/1000000"),
            labels=LABELS,
        )
        report = compare_revisions(base.build_readback(), head.build_readback())
        assert report.duration_drift_us == 1
        assert report.duration_drift_us_exact is True
        assert report.duration_drift_seconds == "1/1000000"
        assert report.identical is False

    def test_a_timebase_change_is_reported(self) -> None:
        base = _builder(revision_id="rev-1", timebase=(30000, 1001))
        base.add_operation(operation="timeline.cut", target_clip_id="c", span=(0, 1), labels=LABELS)
        head = _builder(revision_id="rev-2", revision_number=2, timebase=(25, 1))
        head.add_operation(operation="timeline.cut", target_clip_id="c", span=(0, 1), labels=LABELS)
        report = compare_revisions(base.build_readback(), head.build_readback())
        assert report.timebase_changed is True
        assert report.frame_drift != 0
        assert report.identical is False
        assert "timebase changed" in report.labels["en"]
        assert set(report.summary) == {"fa", "en"}

    def test_custom_comparison_labels_are_used(self) -> None:
        readback = _simple_readback()
        report = compare_revisions(readback, readback, labels=_comparison_labels())
        assert report.labels == _comparison_labels()

    def test_comparison_labels_need_both_languages(self) -> None:
        readback = _simple_readback()
        with pytest.raises(PreviewReadbackError):
            compare_revisions(readback, readback, labels={"en": "Comparison"})

    def test_a_tampered_readback_is_never_compared(self) -> None:
        base = _simple_readback()
        head = _simple_readback()
        object.__setattr__(head, "preview_digest", "sha256:" + "2" * 64)
        with pytest.raises(PreviewReadbackError) as excinfo:
            compare_revisions(base, head)
        assert excinfo.value.reason == "readback_tampered"

    def test_non_readbacks_are_refused(self) -> None:
        readback = _simple_readback()
        with pytest.raises(PreviewReadbackError) as excinfo:
            compare_revisions(readback, "not-a-readback")  # type: ignore[arg-type]
        assert excinfo.value.reason == "invalid_readback"

    def test_the_comparison_report_is_itself_hashable(self) -> None:
        readback = _simple_readback()
        report = compare_revisions(readback, readback)
        forged = dataclasses.replace(report, frame_drift=5, comparison_digest="")
        assert forged.comparison_digest != report.comparison_digest
        with pytest.raises(PreviewReadbackError) as excinfo:
            dataclasses.replace(report, frame_drift=5)  # keeps the old digest
        assert excinfo.value.reason == "comparison_digest_mismatch"

    def test_report_identity_fields_are_validated(self) -> None:
        readback = _simple_readback()
        report = compare_revisions(readback, readback)
        with pytest.raises(PreviewReadbackError):
            dataclasses.replace(report, base_revision_id="  ", comparison_digest="")
        with pytest.raises(PreviewReadbackError) as excinfo:
            dataclasses.replace(report, schema="v0", comparison_digest="")
        assert excinfo.value.reason == "invalid_schema"

    def test_operation_lists_are_normalised_to_sorted_tuples(self) -> None:
        readback = _simple_readback()
        report = compare_revisions(readback, readback)
        forged = dataclasses.replace(
            report,
            added_operations=("b@1", "a@1"),
            comparison_digest="",
        )
        assert forged.added_operations == ("a@1", "b@1")

    def test_a_report_rebuilt_from_its_own_digest_is_trusted(self) -> None:
        report = compare_revisions(_simple_readback(), _simple_readback())
        payload = report.to_dict()
        rebuilt = RevisionComparisonReport(
            base_revision_id=payload["base_revision_id"],
            head_revision_id=payload["head_revision_id"],
            base_revision_number=payload["base_revision_number"],
            head_revision_number=payload["head_revision_number"],
            added_operations=tuple(payload["added_operations"]),
            removed_operations=tuple(payload["removed_operations"]),
            changed_operations=tuple(payload["changed_operations"]),
            frame_drift=payload["frame_drift"],
            duration_drift_seconds=payload["duration_drift_seconds"],
            duration_drift_us=payload["duration_drift_us"],
            duration_drift_us_exact=payload["duration_drift_us_exact"],
            timebase_changed=payload["timebase_changed"],
            labels=payload["labels"],
            summary=payload["summary"],
            comparison_digest=payload["comparison_digest"],
        )
        assert rebuilt.comparison_digest == report.comparison_digest

    def test_the_report_is_a_revision_comparison_report(self) -> None:
        report = compare_revisions(_simple_readback(), _simple_readback())
        assert isinstance(report, RevisionComparisonReport)
        assert report.base_revision_number == 1
        assert report.head_revision_number == 1


# --------------------------------------------------------------------------- #
# Integration with the pinned-revision vocabulary
# --------------------------------------------------------------------------- #
class TestPinnedRevisionIntegration:
    def test_a_readback_digest_can_be_pinned_into_a_revision_envelope(self) -> None:
        """The readback digest is the ``preview_digest`` a revision envelope pins."""
        from nexus_ai_agent.creative.studio.revision_pinning import PinnedRevisionEnvelope

        readback = _simple_readback()
        envelope = PinnedRevisionEnvelope(
            revision_id="rev-1",
            tenant_id=readback.tenant_id,
            project_id=readback.project_id,
            session_id="sess-1",
            turn_id="turn-1",
            revision_number=readback.revision_number,
            parent_revision_id=None,
            plan_id="plan-1",
            plan_jcs_sha256="sha256:" + "1" * 64,
            parameters_jcs_sha256="sha256:" + "2" * 64,
            preview_digest=readback.preview_digest,
            input_asset_pins=(),
            revision_jcs_sha256="",
            created_at_utc="2026-11-26T02:00:00+00:00",
        )
        assert envelope.verify_envelope_integrity() is True
        assert envelope.preview_digest == readback.preview_digest
        assert verify_wysiwys_readback(readback, expected_digest=envelope.preview_digest)
