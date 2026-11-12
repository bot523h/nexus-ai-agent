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
        old="            produced = builder(staged, workspace, cancel)",
        # The defect: the builder never receives the caller's token.
        new="            produced = builder(staged, workspace, None)",
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
