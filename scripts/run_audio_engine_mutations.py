#!/usr/bin/env python3
"""Controlled OMEGA STAGE-B campaign: 77 inherited + 33 new probes = 110.

Every probe has a passing unmutated control in the SAME isolated copy, an exact
unique anchor, a syntactically valid mutant and identical collected test IDs.
Only a failing test CALL can kill a mutant. Syntax/import/collection, setup,
teardown, skipped/no-test runs, timeouts and broken controls never count.
Assertion/pytest-expectation kills are distinguished from runtime exceptions.

    python scripts/run_audio_engine_mutations.py
    python scripts/run_audio_engine_mutations.py --json-out ci-artifacts/omega-mutations.json
    python scripts/run_audio_engine_mutations.py -k k3 --workers 2

A filtered run is diagnostic, not a full 110-probe acceptance. No live source is
mutated; all tests run in temporary copies with fresh Python processes and no
bytecode cache. This script never edits the branch, board or generated truth.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AUDIO = "src/nexus_ai_agent/creative/audio_engine"


def _legacy():
    name = "omega_audio_legacy_mutation_specs"
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / "mutate_omega_audio.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the inherited mutation specifications")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_OLD = _legacy()
Probe = _OLD.Probe


def _kernel(name, module, old, new, test, *, gate=None):
    return Probe(
        name,
        gate or module,
        f"{AUDIO}/{module}.py",
        old,
        new,
        test,
        f"tests/unit/test_audio_engine_{module}.py",
    )


STAGE_B_PROBES = (
    _kernel(
        "k1-acn-cosine-channel-is-sine",
        "ambisonics_binaural_fdn",
        "            result[degree * (degree + 1) + m] = base * np.cos(m * az)",
        "            result[degree * (degree + 1) + m] = base * np.sin(m * az)",
        "test_foa",
    ),
    _kernel(
        "k1-camera-inverse-ignored",
        "ambisonics_binaural_fdn",
        "        rotation = rotation.T",
        "        rotation = rotation",
        "test_camera_yaw",
    ),
    _kernel(
        "k1-binaural-itd-ear-reversed",
        "ambisonics_binaural_fdn",
        "        delay = max(0.0, itd if ear == 1 else -itd)",
        "        delay = max(0.0, -itd if ear == 1 else itd)",
        "test_binaural_cues",
    ),
    _kernel(
        "k1-fractional-delay-rounded",
        "ambisonics_binaural_fdn",
        "    fraction = delay_samples - integer",
        "    fraction = 0.0",
        "test_woodworth_delay",
    ),
    _kernel(
        "k1-head-shadow-disabled",
        "ambisonics_binaural_fdn",
        "        high_gain = 10 ** (-18.0 * abs(lateral) / 20) if contralateral else 1.0",
        "        high_gain = 1.0",
        "test_binaural_cues",
    ),
    _kernel(
        "k1-householder-not-unitary",
        "ambisonics_binaural_fdn",
        "    return np.eye(lines) - (2.0 / lines) * np.ones((lines, lines))",
        "    return np.eye(lines) - (1.0 / lines) * np.ones((lines, lines))",
        "test_householder",
    ),
    _kernel(
        "k1-fdn-decay-law-wrong",
        "ambisonics_binaural_fdn",
        "        -3\n        * np.array(delays)[:, None]",
        "        -2\n        * np.array(delays)[:, None]",
        "test_schroeder_rt60",
    ),
    _kernel(
        "k2-wola-normalization-omitted",
        "spectral_voice_restoration",
        "    output /= np.maximum(normalizer, 1e-20)",
        "    output = output",
        "test_wola",
    ),
    _kernel(
        "k2-e1-series-sign-reversed",
        "spectral_voice_restoration",
        "    result[small] = -np.euler_gamma - np.log(a) - series",
        "    result[small] = -np.euler_gamma - np.log(a) + series",
        "test_exponential_integral",
    ),
    _kernel(
        "k2-lsa-gain-floor-removed",
        "spectral_voice_restoration",
        "    return np.asarray(np.clip(gain, floor, 1.0), dtype=np.float64)",
        "    return np.asarray(np.clip(gain, 0.0, 1.0), dtype=np.float64)",
        "test_lsa",
    ),
    _kernel(
        "k2-lsa-replaced-by-wiener",
        "spectral_voice_restoration",
        "    gain = ratio * np.exp(0.5 * exponential_integral_e1(v))",
        "    gain = ratio",
        "test_lsa",
    ),
    _kernel(
        "k2-lsa-strength-ignored",
        "spectral_voice_restoration",
        "        gain = 1 - strength * (1 - gain)",
        "        gain = gain",
        "test_lsa_strength_zero",
    ),
    _kernel(
        "k2-reliable-sample-constraint-removed",
        "spectral_voice_restoration",
        "        projected[reliable] = padded[reliable]",
        "        projected[reliable] = projected[reliable]",
        "test_declip",
    ),
    _kernel(
        "k2-positive-clipping-inequality-inverted",
        "spectral_voice_restoration",
        "        projected[signs_pos] = np.maximum(projected[signs_pos], threshold)",
        "        projected[signs_pos] = np.minimum(projected[signs_pos], threshold)",
        "test_declip",
    ),
    _kernel(
        "k2-wpe-early-window-erased",
        "spectral_voice_restoration",
        "    output[:, :preserve] = data[:, :preserve]",
        "    output[:, :preserve] = 0.0",
        "test_wpe",
    ),
    _kernel(
        "k3-gcc-delay-sign-reversed",
        "av_subframe_foley_sync",
        "    return OffsetEstimate(lag, lag / sample_rate, margin)",
        "    return OffsetEstimate(-lag, -lag / sample_rate, margin)",
        "test_gcc_phat_recovers_signed_fractional_delay",
    ),
    _kernel(
        "k3-subbin-estimate-rounded",
        "av_subframe_foley_sync",
        "    lag = (index - maximum + fractional) / interpolation",
        "    lag = round((index - maximum + fractional) / interpolation)",
        "test_gcc_phat_recovers_signed_fractional_delay",
    ),
    _kernel(
        "k3-visual-timestamps-rounded-to-frames",
        "av_subframe_foley_sync",
        "    x = np.arange(length) * (frame_rate / envelope_rate)",
        "    x = np.floor(np.arange(length) * (frame_rate / envelope_rate))",
        "test_visual_interpolation_and_correction",
    ),
    _kernel(
        "k3-itu-window-wrongly-symmetric",
        "av_subframe_foley_sync",
        "    return ITU_MIN_MS <= offset_ms <= ITU_MAX_MS",
        "    return ITU_MIN_MS <= offset_ms <= -ITU_MIN_MS",
        "test_visual_frames_are_timestamped_exactly",
    ),
    _kernel(
        "k3-correction-direction-reversed",
        "av_subframe_foley_sync",
        "    delay = -estimate.offset_seconds * sample_rate",
        "    delay = estimate.offset_seconds * sample_rate",
        "test_default_rms_path",
    ),
    _kernel(
        "k3-collision-placement-off-one",
        "av_subframe_foley_sync",
        "        stop = min(length_samples, collision.sample_index + sound.size)\n"
        "        output[collision.sample_index : stop] += sound[: stop - collision.sample_index]",
        "        start = collision.sample_index + 1\n"
        "        stop = min(length_samples, start + sound.size)\n"
        "        output[start : stop] += sound[: stop - start]",
        "test_collision_placement",
    ),
    _kernel(
        "k3-contact-duration-ignored",
        "av_subframe_foley_sync",
        "    count = max(1, round(contact_seconds * sample_rate))",
        "    count = 1",
        "test_contact_duration_softens",
    ),
    _kernel(
        "k3-impulse-force-not-linear",
        "av_subframe_foley_sync",
        "    force *= impulse_ns / force.sum()",
        "    force /= force.sum()",
        "test_six_passive_materials",
    ),
    _kernel(
        "k4-bark-only-23-bands",
        "psychoacoustic_bark_mastering",
        "    return np.asarray(np.arange(24)[:, None] == indices[None, :], dtype=np.float64)",
        "    return np.asarray(np.arange(23)[:, None] == indices[None, :], dtype=np.float64)",
        "test_24_zwicker_bands",
    ),
    _kernel(
        "k4-lower-spreading-slope-wrong",
        "psychoacoustic_bark_mastering",
        "        np.where(difference < 0, 25 * difference, upper_slope * difference),",
        "        np.where(difference < 0, 10 * difference, upper_slope * difference),",
        "test_spreading_is_asymmetric",
    ),
    _kernel(
        "k4-upper-spreading-level-independent",
        "psychoacoustic_bark_mastering",
        "    upper_slope = -np.clip(27 - 0.37 * np.maximum(level - 40, 0), 10, 27)",
        "    upper_slope = -np.clip(27 - 0.0 * np.maximum(level - 40, 0), 10, 27)",
        "test_spreading_is_asymmetric",
    ),
    _kernel(
        "k4-carving-bypassed-despite-good-metadata",
        "psychoacoustic_bark_mastering",
        "    bins_gain = gains @ bands",
        "    bins_gain = np.ones((gains.shape[0], bands.shape[1]))",
        "test_conflicting_music_and_sfx",
    ),
    _kernel(
        "k4-masker-assumes-incoherent-stems",
        "psychoacoustic_bark_mastering",
        "    competing = np.mean(np.abs(spectra[1] + spectra[2]) ** 2, axis=0)",
        "    competing = power[1] + power[2]",
        "test_conflicting_music_and_sfx",
    ),
    _kernel(
        "k4-broadcast-target-is-minus-23",
        "psychoacoustic_bark_mastering",
        "BROADCAST_LKFS = -24.0",
        "BROADCAST_LKFS = -23.0",
        "test_three_stem_master",
    ),
    _kernel(
        "k4-true-peak-ceiling-is-zero",
        "psychoacoustic_bark_mastering",
        "TRUE_PEAK_LIMIT_DBTP = -1.0",
        "TRUE_PEAK_LIMIT_DBTP = 0.0",
        "test_high_crest_master",
    ),
    Probe(
        "stageb-auxiliary-recipe-hashes-dropped",
        "multi-source-recipe-binding",
        f"{AUDIO}/execution.py",
        '            "source_hashes": dict(sorted(request.source_hashes.items())),',
        '            "source_hashes": {},',
        "test_all_auxiliary_stem_hashes_bind_the_recipe",
        "tests/integration/test_audio_engine_stage_b_pack.py",
    ),
    Probe(
        "lineage-active-fence-not-checked",
        "held-host-fence",
        f"{AUDIO}/spine_lineage.py",
        "            fence.assert_active(identity)",
        "            pass",
        "test_stale_fence_has_zero_public_bytes",
        "tests/unit/test_audio_engine_spine_lineage.py",
    ),
    Probe(
        "lineage-post-verify-tamper-rehash-skipped",
        "private-byte-integrity",
        f"{AUDIO}/spine_lineage.py",
        '            if ("sha256:" + current_digest, current_size) != (expected, size):',
        "            if False:",
        "test_tampered_private_bytes_after_verify_are_never_published",
        "tests/unit/test_audio_engine_spine_lineage.py",
    ),
)
PROBES = (*_OLD.PROBES, *STAGE_B_PROBES)

# The plugin writes semantic pytest events, not exit-code guesses or scraped
# human output. Collection failures are recorded even when no test ran.
_EVIDENCE_PLUGIN = r"""
import json
import os
from pathlib import Path
import pytest

EVENTS = {"collected": [], "collection_errors": [], "reports": []}

def pytest_collection_finish(session):
    EVENTS["collected"] = [item.nodeid for item in session.items]

def pytest_collectreport(report):
    if report.failed:
        EVENTS["collection_errors"].append(str(report.longrepr))

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    EVENTS["reports"].append({
        "nodeid": report.nodeid, "phase": report.when, "outcome": report.outcome,
        "exception": None if call.excinfo is None else call.excinfo.type.__name__,
        "detail": str(report.longrepr) if report.failed else "",
        "xfail": hasattr(report, "wasxfail"),
    })

def pytest_sessionfinish(session, exitstatus):
    EVENTS["exit_code"] = int(exitstatus)
    Path(os.environ["OMEGA_MUTATION_EVENTS"]).write_text(json.dumps(EVENTS), encoding="utf-8")
"""


def _classify(control, mutant):
    control_calls = [r for r in control.get("reports", []) if r["phase"] == "call"]
    if (
        control.get("exit_code") != 0
        or not control.get("collected")
        or control.get("collection_errors")
        or len(control_calls) != len(control["collected"])
        or any(r["outcome"] != "passed" or r.get("xfail") for r in control_calls)
        or any(r["outcome"] != "passed" for r in control.get("reports", []))
    ):
        return "BASELINE_FAILED", []
    if (
        mutant.get("exit_code") not in (0, 1)
        or mutant.get("collection_errors")
        or mutant.get("collected") != control["collected"]
        or any(r["outcome"] != "passed" for r in mutant.get("reports", []) if r["phase"] != "call")
        or any(r.get("xfail") or r["outcome"] == "skipped" for r in mutant.get("reports", []))
        or any(
            r.get("exception")
            in ("ImportError", "ModuleNotFoundError", "SyntaxError", "IndentationError", "TabError")
            for r in mutant.get("reports", [])
        )
    ):
        return "INFRASTRUCTURE_ERROR", []
    failures = [
        r for r in mutant.get("reports", []) if r["phase"] == "call" and r["outcome"] == "failed"
    ]
    if mutant["exit_code"] == 1 and failures:
        return "KILLED", failures
    calls = [r for r in mutant.get("reports", []) if r["phase"] == "call"]
    if (
        mutant["exit_code"] == 0
        and len(calls) == len(control_calls)
        and all(r["outcome"] == "passed" for r in calls)
    ):
        return "SURVIVED", []
    return "INFRASTRUCTURE_ERROR", []


def _pytest(work, scratch, probe, label, *, timeout=600):
    artifact = scratch / f"{label}.json"
    environment = dict(os.environ)
    environment.update(
        PYTHONPATH=os.pathsep.join((str(work / "src"), str(work), str(work / "tests" / "unit"))),
        HOME=str(scratch),
        PYTHONDONTWRITEBYTECODE="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        OMEGA_MUTATION_EVENTS=str(artifact),
    )
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                probe.test_file,
                "-k",
                probe.test,
                "-x",
                "-q",
                "-p",
                "no:cacheprovider",
                "-p",
                "omega_mutation_events",
                # The repository has an async autouse DB-disposal fixture,
                # including for synchronous DSP tests. Preserve its real plugin
                # rather than disabling fixtures to make controls pass.
                "-p",
                "pytest_asyncio.plugin",
            ],
            cwd=work,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"exit_code": "timeout", "tail": "pytest exceeded its subprocess deadline"}
    report = json.loads(artifact.read_text()) if artifact.is_file() else {}
    report["exit_code"] = result.returncode  # real process status is authoritative
    report["tail"] = (result.stdout + result.stderr)[-4000:]
    return report


def run_probe(probe, *, root=None, timeout=600):
    root = REPO if root is None else Path(root)
    started = time.perf_counter()
    base = dict(
        name=probe.name,
        gate=probe.gate,
        path=probe.path,
        selector=f"{probe.test_file} -k {probe.test}",
    )
    try:
        source = (root / probe.path).read_text(encoding="utf-8")
    except OSError as error:
        return dict(base, verdict="SOURCE_UNREADABLE", detail=str(error))
    count = source.count(probe.old)
    if count != 1:
        return dict(
            base,
            verdict="ANCHOR_MISSING" if count == 0 else "AMBIGUOUS_ANCHOR",
            detail=f"anchor count {count}",
        )
    mutated = source.replace(probe.old, probe.new)
    try:
        compile(mutated, probe.path, "exec")
    except SyntaxError as error:
        return dict(base, verdict="INVALID_MUTANT", detail=str(error))
    with tempfile.TemporaryDirectory(prefix="omega_controlled_") as directory:
        scratch = Path(directory)
        work = scratch / "tree"
        for name in ("src", "tests", "pyproject.toml"):
            origin = root / name
            if origin.is_dir():
                shutil.copytree(
                    origin,
                    work / name,
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
                )
            else:
                shutil.copy2(origin, work / name)
        (work / "omega_mutation_events.py").write_text(_EVIDENCE_PLUGIN, encoding="utf-8")
        control = _pytest(work, scratch, probe, "control", timeout=timeout)
        control_verdict, _ = _classify(control, {})
        if control_verdict == "BASELINE_FAILED":
            return dict(base, verdict=control_verdict, control=control)
        (work / probe.path).write_text(mutated, encoding="utf-8")
        mutant = _pytest(work, scratch, probe, "mutant", timeout=timeout)
        verdict, failures = _classify(control, mutant)
    exceptions = sorted({f["exception"] for f in failures})
    assertion_kill = bool(failures) and all(e in ("AssertionError", "Failed") for e in exceptions)
    return dict(
        base,
        verdict=verdict,
        control_passed=len(control["collected"]),
        control_test_ids=control["collected"],
        mutant_exit_code=mutant["exit_code"],
        kill_kind="ASSERTION_OR_EXPECTATION"
        if assertion_kill
        else "RUNTIME_EXCEPTION"
        if failures
        else None,
        exceptions=exceptions,
        failures=failures,
        elapsed_seconds=round(time.perf_counter() - started, 4),
        detail=mutant.get("tail", "") if verdict != "KILLED" else "",
    )


def _input_manifest(root):
    """Bind the copied source/tests/config and both runner specifications.

    A campaign that sees live edits between its controls is not one coherent
    experiment. Ignore only the caches excluded by the isolated-copy operation.
    """
    files = {root / "pyproject.toml"}
    for directory in ("src", "tests"):
        files.update(
            path
            for path in (root / directory).rglob("*")
            if path.is_file() and not {"__pycache__", ".pytest_cache"}.intersection(path.parts)
        )
    files.update(
        path
        for path in (
            root / "scripts" / "mutate_omega_audio.py",
            root / "scripts" / "run_audio_engine_mutations.py",
        )
        if path.is_file()
    )
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(files)
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-k", "--filter", default="")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= 8 or len(PROBES) != 110 or len({p.name for p in PROBES}) != 110:
        parser.error("require 1..8 workers and exactly 110 uniquely named probes")
    selected = [p for p in PROBES if args.filter in p.name]
    if not selected:
        parser.error("filter selected no probes")
    inputs = _input_manifest(REPO)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(run_probe, selected))
    inputs_unchanged = inputs == _input_manifest(REPO)
    counts = Counter(result["verdict"] for result in results)
    sources = {p.path: hashlib.sha256((REPO / p.path).read_bytes()).hexdigest() for p in selected}
    report = dict(
        schema="omega.audio-controlled-mutations.v1",
        full_campaign=len(selected) == 110,
        accepted=len(selected) == 110 and counts["KILLED"] == 110 and inputs_unchanged,
        inputs_unchanged=inputs_unchanged,
        input_manifest=inputs,
        input_manifest_sha256=hashlib.sha256(
            json.dumps(inputs, sort_keys=True).encode()
        ).hexdigest(),
        python=sys.version.split()[0],
        probes=results,
        counts=dict(counts),
        kill_kinds=dict(Counter(r["kill_kind"] for r in results if r.get("kill_kind"))),
        source_manifest=sources,
        source_manifest_sha256=hashlib.sha256(
            json.dumps(sources, sort_keys=True).encode()
        ).hexdigest(),
    )
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for result in results:
            print(f"{result['name']:<52} {result['verdict']}")
            if result["verdict"] != "KILLED":
                print(str(result.get("detail") or result.get("control"))[-2000:])
        print(
            f"\n{counts['KILLED']}/{len(selected)} KILLED; {dict(counts)}; "
            f"kinds={report['kill_kinds']}"
        )
        if not inputs_unchanged:
            print("FAIL: source/test/harness inputs changed during the campaign", file=sys.stderr)
        if not report["full_campaign"]:
            print("DIAGNOSTIC FILTER: not a full-campaign acceptance")
    return 0 if counts["KILLED"] == len(selected) and inputs_unchanged else 1


if __name__ == "__main__":
    raise SystemExit(main())
