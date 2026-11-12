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

    python scripts/omega_stage_a_mutations.py            # run every probe
    python scripts/omega_stage_a_mutations.py -k tempo   # filter by name
    python scripts/omega_stage_a_mutations.py --json     # machine-readable

Exit status: 0 when every probe was KILLED (the suite caught the mutation),
1 when any probe SURVIVED, 2 on a setup problem (unreadable file, missing
anchor) — never 0 by accident.
"""

from __future__ import annotations

import argparse
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


@dataclass(frozen=True)
class Probe:
    """One mutation: (file, exact old text, replacement, test selector)."""

    name: str
    gate: str
    path: str
    old: str
    new: str
    test: str


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
        old="            produced = builder(staged, workspace, cancel, run_dir)",
        # The defect: the builder never receives the caller's token.
        new="            produced = builder(staged, workspace, None, run_dir)",
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
        mutated = work / probe.path
        mutated.write_text(source.replace(probe.old, probe.new), encoding="utf-8")
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
                str(work / TEST_FILE),
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
    killed = completed.returncode != 0
    return {
        "name": probe.name,
        "gate": probe.gate,
        "verdict": "KILLED" if killed else "SURVIVED",
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
    survived = [r for r in results if r["verdict"] != "KILLED"]

    if args.json:
        print(json.dumps({"probes": results, "survived": len(survived)}, indent=2))
    else:
        width = max(len(str(r["name"])) for r in results)
        for result in results:
            print(f"  {str(result['name']).ljust(width)}  {result['verdict']}")
            if result["verdict"] != "KILLED":
                print(f"      {result['detail'] if 'detail' in result else result.get('tail')}")
        print()
        print(f"{len(results) - len(survived)}/{len(results)} mutations killed")

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
