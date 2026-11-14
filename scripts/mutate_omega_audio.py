#!/usr/bin/env python3
"""STAGE-A mutation campaign: prove each new guard is load-bearing.

A regression test that cannot fail is documentation, not evidence.  This script
applies one *targeted* mutation per gate to a **copy** of the tree, runs the
gate's test, and requires the test to FAIL.  If a mutation survives (the suite
still passes), the gate it protects is not actually enforced by a test.

Each probe is a textual substitution against an exact anchor in the source, so a
refactor that moves the code makes the probe report ``ANCHOR_MISSING`` instead of
silently passing — an unmeasurable mutation is a failure, not a pass.

Usage::

    python scripts/mutate_omega_audio.py            # run every probe
    python scripts/mutate_omega_audio.py -k tempo   # filter by name
    python scripts/mutate_omega_audio.py --json     # machine-readable

Exit status: 0 when every probe was KILLED (the suite caught the mutation),
1 when any probe SURVIVED or INFRA_ERROR'd (an unmeasurable mutant is a
failure, never a kill), 2 on a setup problem — never 0 by accident.
"""

from __future__ import annotations

import argparse
import ast
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AUDIO = "src/nexus_ai_agent/creative/audio_engine"
SLIDESHOW = "src/nexus_ai_agent/creative/slideshow/audio.py"
TEST_FILE = "tests/unit/test_omega_stage_a_hardening.py"
ENGINE_PORT = "src/nexus_ai_agent/creative/studio/audio_engine.py"
KERNELS = "src/nexus_ai_agent/creative/audio_engine/cinema_kernels.py"
LINEAGE = "src/nexus_ai_agent/creative/audio_engine/spine_lineage.py"
KERNEL_TESTS = "tests/creative/audio_engine/test_cinema_kernels.py"
LINEAGE_TESTS = "tests/unit/test_audio_engine_spine_lineage.py"
DEGENERATE_TESTS = "tests/unit/test_audio_engine_slideshow_degenerate.py"
BANNER_TESTS = "tests/unit/test_omega_wave2_banner_hardening.py"


@dataclass(frozen=True)
class Probe:
    """One mutation: (file, exact old text, replacement, test selector).

    ``test_file`` defaults to the main hardening suite.  A probe whose named
    test lives elsewhere names that module, so a probe can target the file its
    invariant actually belongs to rather than forcing every invariant into one
    suite.
    """

    name: str
    gate: str
    path: str
    old: str
    new: str
    test: str
    test_file: str = TEST_FILE


PROBES: tuple[Probe, ...] = (
    Probe(
        name="publish-before-verify",
        gate="verify-before-publish",
        path=f"{AUDIO}/execution.py",
        old=(
            "            passport = self._verify(\n"
            "                operation=operation,\n"
            "                asset_id=output_asset_id,\n"
            "                path=produced,"
        ),
        # The old ordering: publish first, then ask the verifier.
        new=(
            "            _published_early = workspace.publish(\n"
            "                produced, destination, replace=True, protected=(staged,)\n"
            "            )\n"
            "            passport = self._verify(\n"
            "                operation=operation,\n"
            "                asset_id=output_asset_id,\n"
            "                path=_published_early.path,"
        ),
        test="test_no_unverified_byte_sequence_ever_appears_at_the_published_path",
    ),
    Probe(
        name="refusal-does-not-publish",
        gate="verify-before-publish",
        path=f"{AUDIO}/execution.py",
        old="""            if not passport.verified:""",
        # Swallow the refusal: an unverified artifact would flow on to publish.
        new="""            if False and not passport.verified:""",
        test="test_an_unverified_passport_never_publishes_and_names_the_reason",
    ),
    Probe(
        name="recipe-identity-drops-parameters",
        gate="recipe-identity",
        path=f"{AUDIO}/recipe.py",
        old="""        "parameters": parameters,""",
        new="""        "parameters": {},""",
        test="test_recipe_identity_covers_every_discriminating_field",
    ),
    Probe(
        name="recipe-identity-drops-source",
        gate="recipe-identity",
        path=f"{AUDIO}/recipe.py",
        old="""        "source_sha256": source_sha256,""",
        new="""        "source_sha256": "constant",""",
        test="test_recipe_identity_covers_every_discriminating_field",
    ),
    Probe(
        name="recipe-identity-drops-engine",
        gate="recipe-identity",
        path=f"{AUDIO}/recipe.py",
        old="""        "engine": engine,""",
        new="""        "engine": {},""",
        test="test_recipe_identity_covers_every_discriminating_field",
    ),
    Probe(
        name="recipe-marker-ignored-on-reuse",
        gate="recipe-identity",
        path=f"{AUDIO}/execution.py",
        old="        if destination.is_file() and _read_recipe_marker(marker) == identity:",
        # The defect: any existing file at the path counts as the artifact.
        new="        if destination.is_file():",
        test="test_changing_a_recipe_parameter_never_returns_the_previous_artifact",
    ),
    Probe(
        name="recipe-marker-overwritten-with-current",
        gate="recipe-identity",
        path=f"{AUDIO}/execution.py",
        old="        _write_recipe_marker(marker, identity)",
        new="        _write_recipe_marker(marker, 'recipe:constant')",
        test="test_the_same_recipe_is_still_reused",
    ),
    Probe(
        name="cancel-token-dropped",
        gate="cancellation",
        path=f"{AUDIO}/execution.py",
        old="            outcome = _as_render_outcome(builder(staged, workspace, cancel, run_dir))",
        # The defect: the builder never receives the caller's token.
        new="            outcome = _as_render_outcome(builder(staged, workspace, None, run_dir))",
        test="test_the_callers_cancel_token_reaches_the_builder",
    ),
    Probe(
        name="cancel-check-removed",
        gate="cancellation",
        path=f"{AUDIO}/execution.py",
        old="            if cancel is not None and cancel.cancelled:",
        new="            if False:",
        test="test_a_cancel_that_fires_during_the_render_publishes_nothing",
    ),
    Probe(
        name="lag-k-finite-difference",
        gate="tempo-lag-k",
        path=SLIDESHOW,
        old="            gaps = (ordered[step:] - ordered[:-step]) / step",
        # The defect: a k-th finite difference instead of a mean k-lag gap.
        new="            gaps = np.diff(ordered, n=step) / step",
        test="test_lag_candidates_are_uncontaminated_period_estimates",
    ),
    Probe(
        name="lag-k-unsorted-onsets",
        gate="tempo-lag-k",
        path=SLIDESHOW,
        old="    ordered = np.sort(np.asarray(onset_times, dtype=np.float64).reshape(-1))",
        new="    ordered = np.asarray(onset_times, dtype=np.float64).reshape(-1)",
        test="test_lag_candidates_sort_their_input",
    ),
    Probe(
        name="probe-json-missing-key-defaults",
        gate="ffprobe-fail-closed",
        path=f"{AUDIO}/ffmpeg_ops.py",
        old="""        if missing:
            raise AudioProbeError(""",
        new="""        if False:
            raise AudioProbeError(""",
        test="test_every_malformed_ffprobe_document_fails_closed",
    ),
    Probe(
        name="probe-json-video-stream-accepted",
        gate="ffprobe-fail-closed",
        path=f"{AUDIO}/ffmpeg_ops.py",
        old='        if stream.get("pix_fmt") is not None:',
        new="        if False:",
        test="test_every_malformed_ffprobe_document_fails_closed",
    ),
    Probe(
        name="probe-json-silently-absent",
        gate="ffprobe-fail-closed",
        path=f"{AUDIO}/ffmpeg_ops.py",
        old="        if binary is None:\n            raise AudioEngineUnavailableError(",
        # Pretend a missing ffprobe is a banner-capable environment.
        new=(
            "        if binary is None:\n"
            "            return self.probe(source, cancel=cancel)\n"
            "        if False:\n"
            "            raise AudioEngineUnavailableError("
        ),
        test="test_probe_json_refuses_when_no_ffprobe_exists",
    ),
    Probe(
        name="loudness-measures-raw-source",
        gate="canonical-before-loudness",
        path=f"{AUDIO}/execution.py",
        old=(
            "        measurement = self._transcoder.measure_loudnorm_pass(\n"
            "            measured_source, profile, cancel=cancel\n"
            "        )"
        ),
        # The defect: pass 1 measures the source layout.
        new=(
            "        measurement = self._transcoder.measure_loudnorm_pass(\n"
            "            staged, profile, cancel=cancel\n"
            "        )"
        ),
        test="test_loudness_pass_one_measures_the_canonical_channel_count",
    ),
    Probe(
        name="canonical-conversion-skipped",
        gate="canonical-before-loudness",
        path=f"{AUDIO}/execution.py",
        old="        if not needs_rate and not needs_channels:\n            return staged",
        new="        return staged",
        test="test_loudness_pass_one_measures_the_canonical_channel_count",
    ),
    Probe(
        name="post-publish-hash-not-rechecked",
        gate="publish-integrity",
        path=f"{AUDIO}/execution.py",
        old="            if published.sha256 != passport.output_sha256:",
        new="            if False:",
        test="test_bytes_that_change_between_verification_and_publication_are_refused",
    ),
    Probe(
        name="workspace-bypasses-the-authority",
        gate="single-filesystem-authority",
        path=f"{AUDIO}/workspace.py",
        old=(
            "        try:\n"
            "            return self._fs.resolve(self._relative(candidate))\n"
            "        except FilesystemBoundaryError as error:\n"
            "            raise WorkspaceBoundaryError(str(error)) from error"
        ),
        # A second containment rule: a hand-rolled resolve that never asks the
        # authority, so a symlinked leaf passes.
        new="        return Path(os.path.normpath(str(candidate)))",
        test="test_the_workspace_delegates_resolution_to_the_canonical_authority",
    ),
    Probe(
        name="traversal-not-sanitised",
        gate="workspace-boundary",
        path=f"{AUDIO}/workspace.py",
        old=(
            "    stem = Path(raw).name\n"
            '    if stem in ("", ".", "..") or "/" in stem or "\\\\" in stem'
            ' or "\\x00" in stem:\n'
            "        stem = default"
        ),
        # The defect: the raw asset id reaches the path, with no sanitiser at
        # all.  (Mutating only ``Path(raw).name`` would survive because
        # ``Path.name`` already strips traversal — which is why the probe
        # attacks the whole sanitiser, not one line of it.)
        new="    stem = raw",
        test="test_a_traversal_asset_id_cannot_escape_the_media_directory",
    ),
    Probe(
        name="replace-false-becomes-overwrite",
        gate="workspace-boundary",
        path=f"{AUDIO}/workspace.py",
        old="        if replaced and not replace:",
        new="        if False:",
        test="test_an_existing_destination_is_not_overwritten_without_replace",
    ),
    Probe(
        name="protected-input-not-checked",
        gate="workspace-boundary",
        path=f"{AUDIO}/workspace.py",
        old="        if target_rel in protected_rels:",
        new="        if False:",
        test="test_publish_refuses_to_replace_a_run_input",
    ),
    Probe(
        name="run-directory-not-cleaned",
        gate="workspace-boundary",
        path=f"{AUDIO}/workspace.py",
        old="            self._remove_run(relative)",
        new="            pass",
        test="test_a_run_directory_removes_itself_even_when_the_run_raises",
    ),
    Probe(
        name="unsafe-run-id-accepted",
        gate="workspace-boundary",
        path=f"{AUDIO}/workspace.py",
        old=(
            '        if "/" in identifier or "\\\\" in identifier or identifier in ("", ".", ".."):'
        ),
        new="        if False:",
        test="test_an_unsafe_run_id_is_refused",
    ),
    Probe(
        name="staged-input-hash-not-checked",
        gate="workspace-boundary",
        path=f"{AUDIO}/workspace.py",
        old="        if expected_sha256 is not None and digest != expected_sha256:",
        new="        if False:",
        test="test_staging_an_input_is_content_addressed_and_idempotent",
    ),
    Probe(
        name="rss-reported-as-zero",
        gate="resource-budget",
        path=f"{AUDIO}/process.py",
        old="                        return int(parts[1]) * 1024  # /proc reports KiB",
        # The defect: a wrong unit (KiB reported as bytes) — the plausibility
        # assertion in the RSS test must catch it.
        new="                        return int(parts[1])",
        test="test_peak_rss_is_measured_or_explicitly_not_measured",
    ),
    Probe(
        name="staging-quota-not-enforced",
        gate="resource-budget",
        path=f"{AUDIO}/execution.py",
        old="        used = _tree_bytes(workspace.staging_dir)",
        new="        used = 0",
        test="test_the_staging_quota_refuses_before_it_copies",
    ),
    Probe(
        name="threads-not-in-argv",
        gate="resource-budget",
        path=f"{AUDIO}/ffmpeg_ops.py",
        old="        if executable == self.binary and self.threads is not None:",
        new="        if False:",
        test="test_a_capped_thread_count_reaches_the_ffmpeg_argv",
    ),
    Probe(
        name="render-writes-into-inputs",
        gate="resource-budget",
        path=f"{AUDIO}/execution.py",
        old=(
            "            destination = _run_artifact_path("
            'run_dir, staged, f"denoise{stream.container}")'
        ),
        # The old behaviour: render next to the staged input (i.e. in inputs/).
        new='            destination = staged.parent / f"denoise{stream.container}"',
        test="test_a_refused_render_leaves_no_artifact_in_the_immutable_inputs_directory",
    ),
    Probe(
        name="budget-never-reported",
        gate="resource-budget",
        path=f"{AUDIO}/execution.py",
        old="            budget=None if budget is None else budget.report(),",
        new="            budget=None,",
        test="test_the_budget_reports_measurements_not_estimates",
    ),
    Probe(
        name="cwd-is-the-media-root",
        gate="resource-budget",
        path=f"{AUDIO}/execution.py",
        old='        media = (root.parent / "media").resolve(strict=False)',
        new='        media = Path.cwd() / "media"',
        test="test_the_media_root_is_not_the_process_working_directory",
    ),
    Probe(
        name="analysis-budget-dropped",
        gate="resource-budget",
        path=f"{AUDIO}/execution.py",
        old="                budget=self._analysis_budget(",
        new="                budget=_NO_BUDGET(",
        test="test_beat_analysis_reports_what_it_consumed",
    ),
    Probe(
        name="confidence-unlabelled",
        gate="confidence-not-probability",
        path=f"{ENGINE_PORT}",
        old='    confidence_kind: Literal["normalized_margin"] = "normalized_margin"',
        new="",
        test="test_confidence_is_labelled_a_margin_not_a_probability",
    ),
    Probe(
        name="pack-claims-production-proven",
        gate="earned-status",
        path="src/nexus_ai_agent/creative/studio/lifecycle.py",
        old='    "nexus.audio.studio": OperationEvidenceState.ARTIFACT_VERIFIED,',
        new='    "nexus.audio.studio": OperationEvidenceState.PRODUCTION_PROVEN,',
        test="test_the_pack_evidence_ceiling_is_artifact_verified_not_production_proven",
    ),
    Probe(
        name="pack-reason-contradicts-evidence",
        gate="earned-status",
        path="src/nexus_ai_agent/creative/studio/lifecycle.py",
        old=(
            '        reason="native engine: loudness/denoise/beats run real DSP and publish "\n'
            '        "verifier-approved artifacts (ARTIFACT_VERIFIED); awaiting independent "\n'
            '        "production corroboration for PRODUCTION_PROVEN",'
        ),
        new='        reason="duck/loudnorm twins compile; DSP/beat media paths unmapped",',
        test="test_the_audio_pack_state_matches_its_measured_evidence",
    ),
    Probe(
        name="loudness-measured-before-canonical-conversion",
        gate="crest-domain",
        path=f"{AUDIO}/execution.py",
        old=(
            "        measured_source = self._canonical_form("
            "staged, stream, cancel=cancel, run_dir=run_dir)"
        ),
        # The defect §4.5 is about: measure the *source* layout, then render the
        # canonical one.  On a mono programme that is 3.0103 dB of pure error, and
        # it makes the reported crest reduction (and the loudness target itself)
        # describe a file that is never published.
        new="        measured_source = staged  # measured before the canonical conversion",
        test="test_loudness_pass_one_measures_the_canonical_channel_count",
    ),
    Probe(
        name="stereo-source-crest-tainted-by-the-upmix-term",
        gate="crest-measured",
        path=f"{AUDIO}/execution.py",
        old='        source_crest_db = measurement["input_tp"] - measurement["input_i"]\n',
        # The 3.0103 dB dual-mono term belongs to a mono source measured before
        # an up-mix. Applying it unconditionally taints the 1:1 stereo case with
        # phantom crest reduction that no processing performed.
        new=(
            "        source_crest_db = (\n"
            '            measurement["input_tp"] - measurement["input_i"] + 3.0103\n'
            "        )\n"
        ),
        test="test_dynamic_allowed_on_a_stereo_source_measures_the_1_to_1_case",
    ),
    Probe(
        name="crest-reduction-always-none",
        gate="crest-measured",
        path=f"{AUDIO}/verify.py",
        old=(
            "    crest_reduction_db: float | None = None\n"
            "    if input_crest_factor_db is not None and measurement.true_peak_dbtp is not None:"
        ),
        new=(
            "    crest_reduction_db: float | None = None\n"
            "    if False and input_crest_factor_db is not None:  #"
        ),
        test="test_the_crest_reduction_is_arithmetic_on_the_measured_file",
    ),
    Probe(
        name="strict-linear-secretly-dynamic",
        gate="linear-vs-dynamic",
        path=f"{AUDIO}/execution.py",
        old=(
            "        if projected_peak_dbtp > profile.true_peak_dbtp "
            "+ TRUE_PEAK_TOLERANCE_DB and not dynamic:"
        ),
        # Allow the limiter for every profile: the broadcast target is then
        # reachable by deleting crest factor, which is what STRICT_LINEAR forbids.
        new="        if False:",
        test="test_the_split_discriminates_on_the_same_signal",
    ),
    Probe(
        name="crest-destruction-bound-removed",
        gate="linear-vs-dynamic",
        path=f"{AUDIO}/execution.py",
        old="            if limit is not None and needed_reduction_db > limit:",
        new="            if False:",
        test="test_a_programme_that_would_have_to_be_crushed_is_refused_not_mastered",
    ),
    Probe(
        name="preset-renamed-a-standard",
        gate="preset-not-a-standard",
        path=f"{AUDIO}/models.py",
        old='    profile_id="nagar_streaming_preset",',
        new='    profile_id="streaming_master",',
        test="test_the_streaming_preset_is_named_a_preset_and_the_broadcast_profile_is_not",
    ),
    # -- §4.7 tempo: octave resolution (Part 1.2) -------------------------- #
    Probe(
        name="octave-subdivision-ratio-widened",
        gate="section-4.7-octave",
        path=SLIDESHOW,
        old="SUBDIVISION_STRENGTH_RATIO = 0.80",
        # 0.999 accepts every candidate whose leftover onsets are *any* weaker,
        # which folds an isochronous 240 BPM stream down to 120 — measured 0.98
        # for a genuinely isochronous pulse, so this threshold cannot separate it
        # from a subdivision (0.65-0.71) any more.
        new="SUBDIVISION_STRENGTH_RATIO = 0.999",
        test="test_an_isochronous_fast_pulse_is_not_folded_down",
    ),
    Probe(
        name="octave-subdivision-test-disabled",
        gate="section-4.7-octave",
        path=SLIDESHOW,
        old="        if unexplained > 0.0 and subdivision_ratio <= SUBDIVISION_STRENGTH_RATIO:",
        new="        if False:",
        test="test_subdivisions_under_a_backbeat_are_folded_onto_the_beat",
    ),
    Probe(
        name="octave-coverage-gate-removed",
        gate="section-4.7-octave",
        path=SLIDESHOW,
        old="        if doubled_coverage >= TACTUS_COVERAGE:",
        new="        if True:",
        test="test_an_isochronous_fast_pulse_is_not_folded_down",
    ),
    Probe(
        name="tempo-prior-ignores-the-measurement",
        gate="section-4.7-octave",
        path=SLIDESHOW,
        old="    octaves = np.log2(bpm / TEMPO_PRIOR_CENTRE_BPM)",
        # A prior that returns a constant is not a prior: it makes every octave
        # candidate score identically and leaves the decision to chance.
        new="    return 1.0\n    octaves = np.log2(bpm / TEMPO_PRIOR_CENTRE_BPM)",
        test="test_uncertainty_is_reported_and_expanded_by_the_declared_coverage_factor",
    ),
    # -- §4.7 tempo: GUM uncertainty (Part 1.2) ---------------------------- #
    Probe(
        name="gum-type-a-drops-the-sqrt-m",
        gate="section-4.7-gum",
        path=SLIDESHOW,
        old="    type_a = (60.0 / mean_period**2) * dispersion / np.sqrt(count)",
        # Forgetting the 1/sqrt(M) is the classic Type-A error: it makes the
        # stated uncertainty independent of how many intervals were averaged,
        # i.e. it stops being a standard error.
        new="    type_a = (60.0 / mean_period**2) * dispersion",
        test="test_type_a_follows_the_gum_formula_exactly",
    ),
    Probe(
        name="gum-type-b-uses-a-normal-divisor",
        gate="section-4.7-gum",
        path=SLIDESHOW,
        old="RECTANGULAR_SD = 12.0**0.5",
        # sqrt(3) is the Gaussian/either-side convention; a hop quantisation is a
        # rectangular distribution and its standard deviation is dt/sqrt(12).
        # Using sqrt(3) understates u_B by a factor of 2.
        new="RECTANGULAR_SD = 3.0**0.5",
        test="test_type_b_follows_the_hop_quantisation_model",
    ),
    Probe(
        name="gum-expansion-factor-wrong",
        gate="section-4.7-gum",
        path=SLIDESHOW,
        old="COVERAGE_FACTOR_95 = 1.96",
        # 1.0 silently turns a 95 % expanded uncertainty into a 68 % one.
        new="COVERAGE_FACTOR_95 = 1.0",
        test="test_uncertainty_is_reported_and_expanded_by_the_declared_coverage_factor",
    ),
    Probe(
        name="gum-combines-by-addition-not-quadrature",
        gate="section-4.7-gum",
        path=SLIDESHOW,
        old="    combined = float(np.sqrt(type_a**2 + type_b**2))",
        new="    combined = float(type_a + type_b)",
        test="test_uncertainty_is_reported_and_expanded_by_the_declared_coverage_factor",
    ),
    Probe(
        name="uncertainty-reported-when-nothing-was-detected",
        gate="section-4.7-gum",
        path=SLIDESHOW,
        old=(
            "    if count < 2 or mean_period <= 0 or hop_seconds <= 0:\n"
            "        return 0.0, 0.0, 0.0, 0.0, 1.0"
        ),
        # Return a small non-zero number instead of zero: an undetected tempo
        # would then look like a precise measurement.
        new=(
            "    if count < 2 or mean_period <= 0 or hop_seconds <= 0:\n"
            "        return 0.01, 0.01, 0.014, 0.03, 1.0"
        ),
        test="test_uncertainty_is_zero_when_no_tempo_was_detected",
    ),
    # -- Part 3A: BS.1770 meter -------------------------------------------- #
    Probe(
        name="bs1770-shelf-normalised-like-a-highpass",
        gate="bs1770-conformance",
        path=f"{AUDIO}/loudness_bs1770.py",
        old="    b0 = (vh + vb * k / _SHELF_Q + k * k) / denominator",
        # Dropping the shelf's DC normalisation makes it a different filter: the
        # measured coefficients no longer match the standard at all.
        new="    b0 = vh + vb * k / _SHELF_Q + k * k",
        test="test_k_weighting_reproduces_the_standard_coefficients",
    ),
    Probe(
        name="bs1770-relative-gate-without-absolute-gate",
        gate="bs1770-gating",
        path=f"{AUDIO}/loudness_bs1770.py",
        old="    above_absolute = values[values > absolute_floor]",
        # Applying the relative gate to the *ungated* mean is the classic
        # shortcut and shifts the integrated value on any programme with a
        # long quiet passage.
        new="    above_absolute = values",
        test="test_two_stage_gating_matches_a_hand_computation",
    ),
    Probe(
        name="bs1770-single-stage-gating",
        gate="bs1770-gating",
        path=f"{AUDIO}/loudness_bs1770.py",
        old="    gated = above_absolute[above_absolute > relative_floor]",
        new="    gated = above_absolute",
        test="test_relative_gate_ignores_a_quiet_passage",
    ),
    Probe(
        name="bs1770-surround-weight-flattened",
        gate="bs1770-conformance",
        path=f"{AUDIO}/loudness_bs1770.py",
        old="        return np.array([1.0, 1.0, 1.0, 0.0, 1.41, 1.41], dtype=np.float64)",
        # Flat weights drop the +1.5 dB surround boost and bring the LFE back in.
        new="        return np.ones(6, dtype=np.float64)",
        test="test_channel_weights_apply_the_surround_boost",
    ),
    Probe(
        name="bs1770-true-peak-is-sample-peak",
        gate="bs1770-true-peak",
        path=f"{AUDIO}/loudness_bs1770.py",
        old="    peak = float(np.max(np.abs(data)))\n    if factor <= 1 or data.size < 16:",
        # Returning the sample peak is a 3 dB under-read on a signal that peaks
        # between samples, which is the entire case a true-peak meter exists for.
        new="    peak = float(np.max(np.abs(data)))\n    if True:",
        test="test_true_peak_is_exact_for_tones_the_grid_misses",
    ),
    Probe(
        name="bs1770-lra-uses-gating-blocks",
        gate="bs1770-lra",
        path=f"{AUDIO}/loudness_bs1770.py",
        old=(
            "    series = _short_term_series(\n"
            "        block_mean_squares, sample_rate=sample_rate, hop_seconds=hop_seconds\n"
            "    )\n"
            "    if series.size < 3:\n"
            "        return None\n"
            "    loudness = np.array("
        ),
        # Using 400 ms blocks directly returns 0.003 LU where the standard says
        # 20 LU: the sliding 3 s window is what LRA is defined over.
        new=(
            "    series = np.asarray(block_mean_squares, dtype=np.float64).reshape(-1)\n"
            "    if series.size < 3:\n"
            "        return None\n"
            "    loudness = np.array("
        ),
        test="test_loudness_range_measures_a_twenty_decibel_swing_as_twenty_lu",
    ),
    Probe(
        name="bs1770-cross-check-tolerance-huge",
        gate="bs1770-cross-check",
        path=f"{AUDIO}/verify.py",
        old="LOUDNESS_CROSS_CHECK_TOLERANCE_LU = 0.1",
        # A 50 LU tolerance makes the cross-check decorative: it can never fire.
        new="LOUDNESS_CROSS_CHECK_TOLERANCE_LU = 50.0",
        test="test_cross_verification_catches_a_wrong_ffmpeg_number",
    ),
    Probe(
        name="bs1770-cross-check-passes-without-samples",
        gate="bs1770-cross-check",
        path=f"{AUDIO}/verify.py",
        old=(
            "    if samples is None or not sample_rate:\n"
            "        return (\n"
            "            CODE_LOUDNESS_METER_DISAGREEMENT,"
        ),
        # Fail-open: not having the samples becomes a pass, which is the failure
        # mode "fail-closed" exists to rule out.
        new="    if False:\n        return (\n            CODE_LOUDNESS_METER_DISAGREEMENT,",
        test="test_cross_verification_is_fail_closed_without_samples",
    ),
    # -- Part 2: STRICT_LINEAR crest invariance ---------------------------- #
    Probe(
        name="strict-linear-crest-tolerance-huge",
        gate="strict-linear-crest",
        path=f"{AUDIO}/verify.py",
        old="STRICT_LINEAR_CREST_TOLERANCE_DB = 0.25",
        # 20 dB of tolerance means no limiter is ever detectable.
        new="STRICT_LINEAR_CREST_TOLERANCE_DB = 20.0",
        test="test_a_compressed_master_claiming_strict_linear_is_refused",
    ),
    Probe(
        name="strict-linear-crest-check-skipped",
        gate="strict-linear-crest",
        path=f"{AUDIO}/verify.py",
        old='            if claimed_target.processing_mode == "STRICT_LINEAR":',
        new="            if False:",
        test="test_a_compressed_master_claiming_strict_linear_is_refused",
    ),
    Probe(
        name="strict-linear-refuses-only-with-a-source",
        gate="strict-linear-crest",
        path=f"{AUDIO}/verify.py",
        old="    if source is None or not Path(source).exists():",
        # Fail-open on the missing-evidence branch: an unverifiable claim passes.
        new="    if False:",
        test="test_a_missing_source_refuses_rather_than_passing",
    ),
    # -- Part 3B: Wiener denoiser ------------------------------------------ #
    Probe(
        name="wiener-symmetric-window",
        gate="wiener-overlap-add",
        path=f"{AUDIO}/wiener_denoise.py",
        old="    return np.hanning(frame_length + 1)[:-1]",
        # The symmetric Hann ripples by 1.3e-4 and reconstructs at 10 % error.
        new="    return np.hanning(frame_length)",
        test="test_reconstruction_is_exact",
    ),
    Probe(
        name="wiener-noise-estimate-is-a-time-minimum",
        gate="wiener-noise-estimate",
        path=f"{AUDIO}/wiener_denoise.py",
        old=(
            "    time_percentile = np.percentile(power, percentile, axis=0)\n"
            "    if neighbour_bins == 0 or time_percentile.size < 2 * neighbour_bins + 1:"
        ),
        # A running minimum is the speech-standard estimator and is wrong for
        # music: a sustained tone *is* the minimum in its own bin.
        new=(
            "    time_percentile = np.min(power, axis=0)\n"
            "    if neighbour_bins == 0 or time_percentile.size < 2 * neighbour_bins + 1:"
        ),
        test="test_noise_estimate_is_spectro_temporal",
    ),
    Probe(
        name="wiener-noise-estimate-drops-the-neighbourhood",
        gate="wiener-noise-estimate",
        path=f"{AUDIO}/wiener_denoise.py",
        old="        estimate = windows.min(axis=1)",
        # Without the spectral neighbourhood a sustained harmonic lifts its own
        # estimate to 18053 against a floor of 5.5 and the gains delete it.
        new="        estimate = time_percentile",
        test="test_noise_estimate_is_spectro_temporal",
    ),
    Probe(
        name="wiener-gain-not-floored",
        gate="wiener-overlap-add",
        path=f"{AUDIO}/wiener_denoise.py",
        old="        gain = np.maximum(gain, floor)",
        # An unfloored Wiener gain is the textbook route to musical noise.
        new="        gain = gain",
        test="test_wiener_gain_is_bounded_and_floored",
    ),
    Probe(
        name="wiener-strength-ignored",
        gate="wiener-overlap-add",
        path=f"{AUDIO}/wiener_denoise.py",
        old="        gain = 1.0 - strength * (1.0 - gain)",
        # Ignoring strength makes every request a full denoise.
        new="        gain = gain",
        test="test_strength_scales_suppression_monotonically",
    ),
    # -- Part 3C/3D: stereo, HPSS, QC ------------------------------------- #
    Probe(
        name="side-channel-sign-flipped",
        gate="mid-side-exactness",
        path=f"{AUDIO}/stereo_hpss.py",
        old="    return (left_data + right_data) / 2.0, (left_data - right_data) / 2.0",
        new="    return (left_data + right_data) / 2.0, (right_data - left_data) / 2.0",
        test="test_mid_side_is_an_exact_change_of_basis",
    ),
    Probe(
        name="hpss-masks-not-complementary",
        gate="hpss-mask-sum",
        path=f"{AUDIO}/stereo_hpss.py",
        old="    mask_percussive = 1.0 - mask_harmonic",
        # Computing the second mask independently breaks M_H + M_P = 1 and the
        # two stems stop summing to the input.
        new="    mask_percussive = percussive_power / safe",
        test="test_hpss_reconstruction_is_exact",
    ),
    Probe(
        name="hpss-both-filters-run-along-time",
        gate="hpss-separation",
        path=f"{AUDIO}/stereo_hpss.py",
        old="    percussive = median_filter(magnitude, kernel_percussive, axis=1)",
        # Filtering both along time keeps only horizontal structure: nothing is
        # percussive any more.
        new="    percussive = median_filter(magnitude, kernel_percussive, axis=0)",
        test="test_hpss_separates_a_tone_from_clicks",
    ),
    Probe(
        name="mono-foldback-risk-threshold-disabled",
        gate="forensic-qc",
        path=f"{AUDIO}/forensic_qc.py",
        old="MONO_FOLDBACK_WARNING_CORRELATION = -0.25",
        # A threshold of -1.1 can never be crossed, so anti-phase material passes.
        new="MONO_FOLDBACK_WARNING_CORRELATION = -1.1",
        test="test_out_of_phase_material_is_flagged",
    ),
    Probe(
        name="clip-run-flattened-across-channels",
        gate="forensic-qc",
        path=f"{AUDIO}/forensic_qc.py",
        old=(
            "    max_clipped_run = max(\n"
            "        (longest_true_run(clipped[channel]) for channel in range(clipped.shape[0])),\n"
            "        default=0,\n"
            "    )"
        ),
        # Flattening joins the end of one channel to the start of the next.
        new="    max_clipped_run = longest_true_run(clipped)",
        test="test_clipping_is_counted_with_its_longest_run",
    ),
    Probe(
        name="programme-silence-counted-as-dropout",
        gate="forensic-qc",
        path=f"{AUDIO}/forensic_qc.py",
        old="    interior = zeros[:, first:last]",
        # Counting leading silence as a dropped buffer flags every clean file.
        new="    interior = zeros",
        test="test_leading_and_trailing_silence_is_not_a_dropout",
    ),
    Probe(
        name="de-esser-attacks-the-voice-band",
        gate="deess-band",
        path=f"{AUDIO}/forensic_qc.py",
        old="SIBILANCE_LOW_HZ = 4_500.0",
        # A 500 Hz corner turns the de-esser into a low-pass on the programme.
        new="SIBILANCE_LOW_HZ = 500.0",
        test="test_de_esser_leaves_the_voice_band_untouched",
    ),
    Probe(
        name="de-esser-gain-applied-outside-the-band",
        gate="deess-band",
        path=f"{AUDIO}/forensic_qc.py",
        old="        output[channel] = low_band + high_scaled",
        # Applying the gain to the whole signal instead of the high band is the
        # difference between de-essing and ducking.
        new=("        output[channel] = (low_band + high_scaled) * gains[0] + 0.0 * low_band"),
        test="test_de_esser_leaves_the_voice_band_untouched",
    ),
    # -- Part 2: loudnorm rate guard --------------------------------------- #
    Probe(
        name="loudnorm-rate-guard-removed",
        gate="loudnorm-rate-guard",
        path=f"{AUDIO}/models.py",
        old=(
            "        return (\n"
            "            ()\n"
            "            if self.sample_rate == self.LOUDNORM_INTERNAL_RATE\n"
            '            else (f"aresample={self.sample_rate}",)\n'
            "        )"
        ),
        # Returning nothing leaves the file at 192 kHz, which the canonical
        # profile check then refuses — the guard is what keeps the render usable.
        new="        return ()\n        return (",
        test="test_canonical_profile_reasserts_its_rate_after_loudnorm",
    ),
    Probe(
        name="wiener-noise-floor-is-tiny-not-a-power",
        gate="wiener-noise-floor",
        path=f"{AUDIO}/wiener_denoise.py",
        old=("    return np.asarray(np.maximum(estimate, NOISE_FLOOR_POWER), dtype=np.float64)"),
        # A floor of float64.tiny is non-zero, so it reads as a floor, but every
        # real signal divided by 2.2e-308 overflows and the WOLA comes back NaN.
        new=(
            "    return np.asarray(\n"
            "        np.maximum(estimate, np.finfo(np.float64).tiny), dtype=np.float64\n"
            "    )"
        ),
        test="test_silence_still_floors_the_estimate_finitely",
    ),
    Probe(
        name="recipe-identity-ignores-parameters",
        gate="recipe-binding",
        path=f"{AUDIO}/recipe.py",
        old='        "parameters": parameters,',
        # Dropping parameters from the identity re-introduces the exact defect
        # the Stage-A audit found: a cached artifact reused across recipes.
        new='        "parameters": {},',
        test="test_changing_one_parameter_changes_the_identity",
    ),
    Probe(
        name="banner-channels-only-understands-named-layouts",
        gate="ffmpeg-banner-portability",
        path=f"{AUDIO}/ffmpeg_ops.py",
        old="""    counted = _COUNT_LAYOUT_RE.match(lowered)
    if counted is not None:
        count = int(counted.group(1))
        return count if count >= 1 else None""",
        # The exact original defect: understand ``mono``/``stereo`` only, so an
        # FFmpeg 6.1.1 banner ("1 channels") yields None and probe() fails
        # closed.  A 9.x-only test suite cannot see this.
        new="""    return None""",
        test="test_a_6_1_1_banner_is_no_longer_none",
        test_file="tests/unit/test_omega_wave2_ffmpeg_banner_versions.py",
    ),
    # ------------------------------------------------------------------ #
    # WAVE-B1: spine_lineage — RFC 6962 Merkle lineage
    # ------------------------------------------------------------------ #
    Probe(
        name="merkle-leaf-prefix-swapped-to-node",
        gate="the leaf commitment must use 0x00, not 0x01",
        path=LINEAGE,
        old='_LEAF_PREFIX = b"\\x00"',
        # Swapping the domains makes a leaf hash indistinguishable from an
        # interior node: the second-preimage weakness RFC 6962 2.1 prevents.
        new='_LEAF_PREFIX = b"\\x01"',
        test="test_leaves_and_nodes_are_domain_separated",
        test_file=LINEAGE_TESTS,
    ),
    Probe(
        name="merkle-node-prefix-dropped",
        gate="interior nodes must be domain-separated by 0x01",
        path=LINEAGE,
        old="    return hashlib.sha256(_NODE_PREFIX + left + right).digest()",
        new="    return hashlib.sha256(left + right).digest()",
        test="test_leaves_and_nodes_are_domain_separated",
        test_file=LINEAGE_TESTS,
    ),
    Probe(
        name="merkle-split-uses-half-instead-of-power-of-two",
        gate="RFC 6962 splits at the largest power of two below n",
        path=LINEAGE,
        old="    return 1 << ((width - 1).bit_length() - 1)",
        # n // 2 agrees with the RFC on power-of-two trees and is wrong on
        # every other size: the defect that made every odd tree unprovable.
        new="    return width // 2",
        test="test_every_leaf_of_every_tree_size_proves_inclusion",
        test_file=LINEAGE_TESTS,
    ),
    Probe(
        name="merkle-proof-length-ignores-the-index",
        gate="the audit-path length depends on the index, not just the size",
        path=LINEAGE,
        old="    if len(proof) != _proof_length(index, total_leaves):",
        new="    if len(proof) != _proof_length(0, total_leaves):",
        test="test_every_leaf_of_every_tree_size_proves_inclusion",
        test_file=LINEAGE_TESTS,
    ),
    Probe(
        name="merkle-proof-not-bound-to-its-tree-size",
        gate="a proof must not replay at a different total_leaves",
        path=LINEAGE,
        old=(
            "    current = merkle_leaf_hash("
            "leaf, total_leaves=total_leaves if bind_tree_size else None)"
        ),
        # An audit path of length k is valid for every tree size whose own path
        # for that index is also k: an 8-leaf proof for index 0 verified at
        # total_leaves 5, 6 and 7 before the size was bound into the leaf domain.
        new="    current = merkle_leaf_hash(leaf)",
        test="test_a_proof_cannot_be_replayed_at_a_smaller_tree_size",
        test_file=LINEAGE_TESTS,
    ),
    Probe(
        name="leaf-drops-the-on-disk-hash",
        gate="a leaf must commit to both the PCM and the WAV hash",
        path=LINEAGE,
        old=(
            '            "pcm_sha256": self.pcm_sha256,\n            "wav_sha256": self.wav_sha256,'
        ),
        # A tree over only the PCM hash cannot detect a re-encode whose bytes
        # changed while the samples did not.
        new='            "pcm_sha256": self.pcm_sha256,',
        test="test_a_leaf_commits_to_both_hashes",
        test_file=LINEAGE_TESTS,
    ),
    Probe(
        name="leaf-accepts-an-uppercase-digest",
        gate="content identity is lowercase hex, exactly 64 characters",
        path=LINEAGE,
        old=(
            "            if len(value) != 64 or any("
            'character not in "0123456789abcdef" for character in value):'
        ),
        new="            if len(value) != 64:",
        test="test_a_malformed_digest_is_refused",
        test_file=LINEAGE_TESTS,
    ),
    Probe(
        name="stale-fence-is-accepted",
        gate="a superseded render must not publish",
        path=LINEAGE,
        old="        if not self.fence.is_current(token):",
        new="        if False:",
        test="test_a_stale_fence_quarantines_and_retains_the_artifact",
        test_file=LINEAGE_TESTS,
    ),
    Probe(
        name="quarantine-deletes-instead-of-retaining",
        gate="tamper evidence is retained, never deleted",
        path=LINEAGE,
        old="            shutil.move(str(self.staging_dir), str(target))",
        # Deleting the loser makes "two renders raced" indistinguishable from
        # "one render happened".
        new="            shutil.rmtree(self.staging_dir, ignore_errors=True)",
        test="test_a_stale_fence_quarantines_and_retains_the_artifact",
        test_file=LINEAGE_TESTS,
    ),
    Probe(
        name="production-authorizer-fails-open",
        gate="an unconfigured production authorizer must refuse",
        path=LINEAGE,
        old='            return False, "no authorizer configured for production publication"',
        new='            return True, "no authorizer configured"',
        test="test_production_without_an_authorizer_fails_closed",
        test_file=LINEAGE_TESTS,
    ),
    # ------------------------------------------------------------------ #
    # WAVE-B1: slideshow/audio — decoding and refusal arithmetic
    # ------------------------------------------------------------------ #
    Probe(
        name="24-bit-sample-not-sign-extended",
        gate="a negative 24-bit sample must stay negative",
        path=SLIDESHOW,
        old="        signed[signed >= 0x800000] -= 0x1000000",
        # Without this the top half of the range reads as positive: every
        # negative sample becomes a large positive one.
        new="        pass",
        test="test_a_24_bit_wav_sign_extends_and_normalises",
        test_file=DEGENERATE_TESTS,
    ),
    Probe(
        name="24-bit-sample-scaled-wrongly",
        gate="24-bit normalises by 2**23, not 2**24",
        path=SLIDESHOW,
        old="        data = signed.astype(np.float32) / 8388608.0",
        new="        data = signed.astype(np.float32) / 16777216.0",
        test="test_a_24_bit_wav_sign_extends_and_normalises",
        test_file=DEGENERATE_TESTS,
    ),
    Probe(
        name="8-bit-wav-treated-as-signed",
        gate="8-bit WAV is unsigned and must be recentred",
        path=SLIDESHOW,
        old=(
            "        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0"
        ),
        new="        data = np.frombuffer(raw, dtype=np.int8).astype(np.float32) / 128.0",
        test="test_an_8_bit_wav_is_unsigned_and_recentred",
        test_file=DEGENERATE_TESTS,
    ),
    Probe(
        name="playback-duration-loses-its-rate-guard",
        gate="a zero or negative rate has no duration",
        path=SLIDESHOW,
        old="        if self.sample_rate <= 0:\n            return 0",
        new="        if False:\n            return 0",
        test="test_a_non_positive_rate_has_no_duration",
        test_file=DEGENERATE_TESTS,
    ),
    Probe(
        name="beat-grid-returns-milliseconds",
        gate="the beat grid is in microseconds",
        path=SLIDESHOW,
        old=(
            "        int(round(value * MICROSECONDS_PER_SECOND)) for value in times"
            " if value <= duration_seconds"
        ),
        new="        int(round(value * 1000.0)) for value in times if value <= duration_seconds",
        test="test_a_normal_period_yields_a_clipped_grid",
        test_file=DEGENERATE_TESTS,
    ),
    Probe(
        name="beat-grid-accepts-an-impossible-period",
        gate="a non-positive period yields no beats",
        path=SLIDESHOW,
        old="    if period <= 0:\n        return ()",
        new="    if False:\n        return ()",
        test="test_an_impossible_period_yields_no_beats",
        test_file=DEGENERATE_TESTS,
    ),
    Probe(
        name="tempo-window-guard-removed",
        gate="an impossible tempo window must be refused",
        path=SLIDESHOW,
        old="    if min_bpm <= 0 or max_bpm <= min_bpm:",
        new="    if False:",
        test="test_an_impossible_tempo_window_is_refused",
        test_file=DEGENERATE_TESTS,
    ),
    # ------------------------------------------------------------------ #
    # WAVE-B1: ffmpeg_ops — label-dependent parsing
    # ------------------------------------------------------------------ #
    Probe(
        name="summary-reader-accepts-the-wrong-unit",
        gate="a value whose unit does not match its label must be refused",
        path="src/nexus_ai_agent/creative/audio_engine/ffmpeg_ops.py",
        old="    if accepted is not None and unit not in accepted:",
        # Threshold appears under both the integrated and LRA section; a
        # unit-blind read of the wrong block yields a plausible number under
        # the wrong name.
        new="    if False:",
        test="test_a_wrong_unit_for_the_label_refuses_rather_than_guessing",
        test_file=BANNER_TESTS,
    ),
    # ------------------------------------------------------------------ #
    # WAVE-B2: cinema_kernels K1 — HOA3 and the FDN
    # ------------------------------------------------------------------ #
    Probe(
        name="hoa3-omni-channel-is-root-two",
        gate="SN3D leaves Y_0^0 = 1.0 (N3D would give sqrt(2))",
        path=KERNELS,
        old="            delta = 1.0 if m == 0 else 0.0",
        new="            delta = 0.0 if m == 0 else 1.0",
        test="test_the_omnidirectional_channel_is_exactly_one",
        test_file=KERNEL_TESTS,
    ),
    Probe(
        name="hoa3-double-counts-the-horizontal-taper",
        gate="P_l^m already carries cos(el)^m",
        path=KERNELS,
        old="            values.append(normalisation * legendre * angular)",
        # Multiplying by cos_el^|m| again squares the taper and the encoded
        # energy falls with elevation: the source fades as it rises.
        new=(
            "            values.append(normalisation * legendre * angular "
            "* (math.cos(elevation) ** abs(m)))"
        ),
        test="test_the_directional_energy_norm_is_direction_invariant",
        test_file=KERNEL_TESTS,
    ),
    Probe(
        name="householder-factor-is-two-over-m-minus-one",
        gate="H = I - (2/M)11^T is the unitary choice",
        path=KERNELS,
        old="    return np.eye(channels, dtype=np.float64) - (2.0 / channels) * ones",
        new="    return np.eye(channels, dtype=np.float64) - (2.0 / (channels - 1)) * ones",
        test="test_the_householder_factor_is_two_over_m",
        test_file=KERNEL_TESTS,
    ),
    Probe(
        name="fdn-delay-lengths-share-a-factor",
        gate="FDN delay lengths must be pairwise coprime",
        path=KERNELS,
        old="            delays.append(candidate)",
        # Common factors make the modes reinforce at shared periods: a
        # metallic ring instead of a diffuse tail.
        new="            delays.append(candidate + 1)",
        test="test_the_fdn_delay_lengths_are_pairwise_coprime",
        test_file=KERNEL_TESTS,
    ),
    Probe(
        name="sabine-constant-is-wrong",
        gate="Sabine is 0.161 V / (S alpha)",
        path=KERNELS,
        old="    return 0.161 * volume_m3 / (surface_m2 * absorption)",
        new="    return 0.6 * volume_m3 / (surface_m2 * absorption)",
        test="test_the_sabine_constant_is_the_published_one",
        test_file=KERNEL_TESTS,
    ),
    Probe(
        name="early-reflection-mirrors-the-wrong-sign",
        gate="an image source is mirrored through its wall",
        path=KERNELS,
        old="        (2 * lx - sx, sy, sz),",
        new="        (lx + sx, sy, sz),",
        test="test_the_six_early_reflections_arrive_after_the_direct_sound",
        test_file=KERNEL_TESTS,
    ),
    Probe(
        name="woodworth-itd-drops-the-theta-term",
        gate="ITD = (a/c)(sin t + t), not (a/c) sin t",
        path=KERNELS,
        old="    return (HEAD_RADIUS_M / SPEED_OF_SOUND_M_S) * (math.sin(azimuth) + azimuth)",
        new="    return (HEAD_RADIUS_M / SPEED_OF_SOUND_M_S) * math.sin(azimuth)",
        test="test_the_woodworth_maximum_matches_the_head_geometry",
        test_file=KERNEL_TESTS,
    ),
    # ------------------------------------------------------------------ #
    # WAVE-B2: cinema_kernels K2 — restoration
    # ------------------------------------------------------------------ #
    Probe(
        name="wiener-gain-floor-removed",
        gate="the Wiener gain is floored, never allowed to reach zero",
        path=KERNELS,
        old="        current = np.maximum(xi / (1.0 + xi), floor)",
        new="        current = xi / (1.0 + xi)",
        test="test_the_wiener_gain_never_falls_below_its_floor",
        test_file=KERNEL_TESTS,
    ),
    Probe(
        name="declipper-overwrites-clean-samples",
        gate="unclipped samples are measured values and stay exact",
        path=KERNELS,
        old="        work[clean_mask] = mono[clean_mask]",
        # The inverse mutation: instead of *not* restoring the measured values,
        # actively smooth every sample. (`pass` in place of the clamp is a
        # no-op — clean samples are never written by the loop above, so removing
        # the clamp changes nothing and that probe measured nothing.)
        new='        work = np.convolve(work, np.ones(3) / 3.0, mode="same")',
        test="test_the_declipper_preserves_unclipped_samples",
        test_file=KERNEL_TESTS,
    ),
    Probe(
        name="declipper-inverts-the-peak-polarity",
        gate="a peak's polarity is measured, not inferred",
        path=KERNELS,
        old="                sign = 1.0 if mono[index] >= 0.0 else -1.0",
        new="                sign = 1.0",
        test="test_the_declipper_preserves_the_measured_sign",
        test_file=KERNEL_TESTS,
    ),
    Probe(
        name="stft-frame-count-truncates-the-tail",
        gate="the STFT must cover the signal exactly",
        path=KERNELS,
        old="    frames = 1 + int(np.ceil(max(0, needed - size) / hop))",
        new="    frames = 1 + (max(0, needed - size)) // hop",
        test="test_the_stft_round_trip_is_lossless_on_any_length",
        test_file=KERNEL_TESTS,
    ),
    Probe(
        name="click-detector-treats-silence-as-clicks",
        gate="a degenerate MAD must not flag everything",
        path=KERNELS,
        old="    if sigma <= 1e-9 * peak_second:",
        # Digital silence gives a nonzero but ~1e-16 MAD; an unguarded
        # threshold * sigma bar sits below every sample and the whole file
        # is reported as clicks.
        new="    if False:",
        test="test_digital_silence_is_not_reported_as_a_recording_full_of_clicks",
        test_file=KERNEL_TESTS,
    ),
    # ------------------------------------------------------------------ #
    # WAVE-B2: cinema_kernels K3 — modal foley
    # ------------------------------------------------------------------ #
    Probe(
        name="hertzian-amplitude-uses-linear-mass",
        gate="Hertzian amplitude scales as m^0.6, not m",
        path=KERNELS,
        old="    return float((effective_mass_kg**0.6) * (rel_velocity_m_s**1.2))",
        new="    return float(effective_mass_kg * (rel_velocity_m_s**1.2))",
        test="test_hertzian_amplitude_is_sublinear_in_velocity",
        test_file=KERNEL_TESTS,
    ),
    Probe(
        name="modal-event-lets-an-alias-through",
        gate="a partial above Nyquist is skipped, not aliased",
        path=KERNELS,
        old="        if partial >= sample_rate / 2.0:",
        new="        if partial >= sample_rate * 4.0:",
        test="test_a_partial_above_nyquist_is_skipped_not_aliased",
        test_file=KERNEL_TESTS,
    ),
    Probe(
        name="modal-event-decay-has-the-wrong-sign",
        gate="modes decay, they do not grow",
        path=KERNELS,
        old=(
            "        out += weight * amplitude * np.exp(-decay * time)"
            " * np.sin(2.0 * math.pi * partial * time)"
        ),
        new=(
            "        out += weight * amplitude * np.exp(decay * time)"
            " * np.sin(2.0 * math.pi * partial * time)"
        ),
        test="test_every_material_syncs_within_one_millisecond",
        test_file=KERNEL_TESTS,
    ),
    Probe(
        name="foley-onset-measured-by-global-peak",
        gate="onset is where the event starts, not where it is loudest",
        path=KERNELS,
        old="            crossing = np.flatnonzero(envelope >= 0.5 * peak_level)",
        new="            crossing = np.flatnonzero(envelope >= peak_level)",
        test="test_every_material_syncs_within_one_millisecond",
        test_file=KERNEL_TESTS,
    ),
    # ------------------------------------------------------------------ #
    # WAVE-B2: cinema_kernels K4 — Bark and mastering
    # ------------------------------------------------------------------ #
    Probe(
        name="zwicker-scale-loses-its-second-arctangent",
        gate="the Zwicker scale is the sum of two arctangents",
        path=KERNELS,
        old="    return 13.0 * math.atan(0.00076 * frequency_hz) + 3.5 * math.atan(",
        new="    return 13.0 * math.atan(0.00076 * frequency_hz) + 0.0 * math.atan(",
        test="test_the_zwicker_scale_matches_published_values",
        test_file=KERNEL_TESTS,
    ),
    Probe(
        name="spreading-slopes-are-symmetric",
        gate="masking spreads +27 dB/Bark down and -12 dB/Bark up",
        path=KERNELS,
        old=(
            "    if delta_bark >= 0.0:\n        return -12.0 * delta_bark"
            "\n    return -27.0 * abs(delta_bark)"
        ),
        new="    return -12.0 * abs(delta_bark)",
        test="test_the_spreading_slopes_are_asymmetric",
        test_file=KERNEL_TESTS,
    ),
    Probe(
        name="limiter-clamps-samples-instead-of-true-peaks",
        gate="the ceiling is a true-peak ceiling",
        path=KERNELS,
        old="    peak = true_peak_dbtp(stereo, oversample=oversample)",
        new="    peak = 20.0 * math.log10(max(float(np.max(np.abs(stereo))), 1e-12))",
        test="test_the_limiter_uses_the_oversampled_true_peak_not_the_sample_peak",
        test_file=KERNEL_TESTS,
    ),
    Probe(
        name="limiter-applies-one-global-scale-factor",
        gate="the limiter must be time-varying or it destroys loudness",
        path=KERNELS,
        old="    limited = stereo * smoothed[:, None]",
        # A single scalar ratio measured 35 dB of reduction on a signal that
        # needed a few dB on a handful of peaks.
        new="    limited = stereo * float(smoothed.min())",
        test="test_the_limiter_preserves_most_of_the_signal_energy",
        test_file=KERNEL_TESTS,
    ),
    Probe(
        name="speech-band-range-is-wrong",
        gate="presence compensation is bounded to the speech bands",
        path=KERNELS,
        old="SPEECH_BANDS: Final[tuple[int, ...]] = tuple(range(4, 17))",
        new="SPEECH_BANDS: Final[tuple[int, ...]] = tuple(range(0, 24))",
        test="test_presence_compensation_is_bounded",
        test_file=KERNEL_TESTS,
    ),
)


def _run_probe(probe: Probe) -> dict[str, object]:
    target = REPO / probe.path
    source = target.read_text(encoding="utf-8")
    if probe.old not in source:
        return {
            "name": probe.name,
            "gate": probe.gate,
            "verdict": "ANCHOR_MISSING",
            "detail": "the mutated anchor is not in the file (refactor invalidated the probe)",
        }
    if source.count(probe.old) != 1:
        return {
            "name": probe.name,
            "gate": probe.gate,
            "verdict": "AMBIGUOUS_ANCHOR",
            "detail": f"the anchor occurs {source.count(probe.old)} times",
        }
    with tempfile.TemporaryDirectory(prefix="omega_mut_") as scratch:
        work = Path(scratch) / "tree"
        # Copy only what the test needs: the package, the tests, the config.
        for name in ("src", "tests", "pyproject.toml"):
            origin = REPO / name
            if origin.is_dir():
                shutil.copytree(origin, work / name, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(origin, work / name)
        mutated_source = source.replace(probe.old, probe.new)
        try:
            ast.parse(mutated_source, filename=probe.path)
        except SyntaxError as exc:
            return {
                "name": probe.name,
                "gate": probe.gate,
                "verdict": "INFRA_ERROR",
                "detail": f"mutant does not parse ({exc.msg}, line {exc.lineno}); "
                "an unparseable mutation proves nothing",
            }
        mutated = work / probe.path
        mutated.write_text(mutated_source, encoding="utf-8")
        environment = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "PYTHONPATH": f"{work / 'src'}:{work / 'tests' / 'unit'}",
            "HOME": scratch,
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(work / probe.test_file),
                "-k",
                probe.test,
                "-x",
                "-q",
                "-p",
                "no:cacheprovider",
            ],
            cwd=work,
            capture_output=True,
            text=True,
            env=environment,
            timeout=600,
        )
    # A mutant that pytest rejected with an assertion failure (exit 1) is
    # KILLED.  Exit 0 means the suite passed — the gate is not enforced.
    # Exit >= 2 (collection, import, syntax, internal error) is an
    # INFRA_ERROR: it must never be counted as a kill.
    if completed.returncode == 0:
        verdict = "SURVIVED"
    elif completed.returncode == 1:
        verdict = "KILLED"
    else:
        verdict = "INFRA_ERROR"
    return {
        "name": probe.name,
        "gate": probe.gate,
        "verdict": verdict,
        "exit_code": completed.returncode,
        "tail": (completed.stdout or completed.stderr or "").strip().splitlines()[-3:],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-k", "--filter", default="", help="substring of the probe name")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    selected = [p for p in PROBES if args.filter in p.name]
    if not selected:
        print(f"no probe matches {args.filter!r}", file=sys.stderr)
        return 2

    results = [_run_probe(probe) for probe in selected]
    survived = [r for r in results if r["verdict"] == "SURVIVED"]
    infra = [r for r in results if r["verdict"] == "INFRA_ERROR"]

    if args.json:
        print(
            json.dumps(
                {"probes": results, "survived": len(survived), "infra_errors": len(infra)},
                indent=2,
            )
        )
    else:
        width = max(len(str(r["name"])) for r in results)
        for result in results:
            print(f"  {str(result['name']).ljust(width)}  {result['verdict']}")
            if result["verdict"] != "KILLED":
                print(f"      {result['detail'] if 'detail' in result else result.get('tail')}")
        print()
        killed = sum(1 for r in results if r["verdict"] == "KILLED")
        print(f"{killed}/{len(results)} mutations killed ({len(infra)} INFRA_ERROR)")

    if infra:
        print(
            f"INFRA_ERROR: {len(infra)} mutation(s) could not be measured "
            "(collection/import/syntax failure or unparseable mutant) — not counted as kills",
            file=sys.stderr,
        )
        return 1
    if survived:
        print(
            f"FAIL: {len(survived)} mutation(s) survived — the gate they attack is not "
            "enforced by a test",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
