"""Hyperion 30-mutant code-mutation campaign.

Each mutant is a single, exact textual change to one production module.  The mutated
package is a private copy under ``tmp_path``; the real Hyperion test zone runs against it in
a subprocess.  A mutant is KILLED when the zone suite fails, and SURVIVED when it passes.
The harness first proves the subprocess imports the copy (not the repo) and that the
unmutated zone is green, so a kill can only come from the mutation.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE = REPO_ROOT / "src" / "nexus_ai_agent"
ZONE_TESTS = [
    "tests/creative/speech_cinema",
    "tests/storage/artifact_vault",
    "tests/creative/packs/color",
]
CAMPAIGN_FILE = "tests/creative/speech_cinema/test_hyperion_mutation_campaign.py"

# (module, exact old text, exact new text, label).  Each old text must occur exactly once.
MUTANTS: list[tuple[str, str, str, str]] = [
    (
        "storage/artifact_vault/galois_erasure.py",
        "PRIMITIVE_POLYNOMIAL = 0x11D",
        "PRIMITIVE_POLYNOMIAL = 0x11B",
        "M01 GF(256) polynomial 0x11D->0x11B",
    ),
    (
        "storage/artifact_vault/galois_erasure.py",
        "if k < 1 or m < 1 or k + m > 255:",
        "if k < 1 or m < 1 or k + m > 254:",
        "M02 RS bound k+m<=255 -> 254",
    ),
    (
        "storage/artifact_vault/galois_erasure.py",
        "    if not raw:\n",
        "    if False:\n",
        "M03 empty-payload refusal removed",
    ),
    (
        "storage/artifact_vault/galois_erasure.py",
        "        vandermonde[point, 0] = 1\n",
        "        vandermonde[point, 0] = 0\n",
        "M04 Vandermonde seed column 1->0 (singular top block)",
    ),
    (
        "storage/artifact_vault/galois_erasure.py",
        'raise ValueError("GF(256) elements must be in [0, 255]")',
        "pass",
        "M05 GF element range check removed",
    ),
    (
        "storage/artifact_vault/vault_manager.py",
        'hashlib.sha256(b"\\x00" + value).digest()',
        'hashlib.sha256(b"\\x01" + value).digest()',
        "M06 RFC6962 leaf prefix 0x00->0x01",
    ),
    (
        "storage/artifact_vault/vault_manager.py",
        'hashlib.sha256(b"\\x01" + left + right).digest()',
        'hashlib.sha256(b"\\x00" + left + right).digest()',
        "M07 RFC6962 node prefix 0x01->0x00",
    ),
    (
        "storage/artifact_vault/vault_manager.py",
        'raise ValueError("artifact cannot be healed: fewer than k valid shards")',
        "pass",
        "M08 heal k-threshold refusal removed",
    ),
    (
        "storage/artifact_vault/vault_manager.py",
        'raise ValueError("artifact is unavailable")',
        "pass",
        "M09 unavailable-artifact refusal removed",
    ),
    (
        "storage/artifact_vault/cdc_chunker.py",
        "self._mask = avg_size - 1",
        "self._mask = avg_size",
        "M10 CDC boundary mask off-by-one",
    ),
    (
        "storage/artifact_vault/cdc_chunker.py",
        "value = (value + 0x9E3779B97F4A7C15)",
        "value = (value + 0x9E3779B97F4A7C16)",
        "M11 gear-table seed constant",
    ),
    (
        "creative/speech_cinema/vad_detector.py",
        "return float(-(p * np.log(p + 1e-12)).sum()",
        "return float((p * np.log(p + 1e-12)).sum()",
        "M12 spectral-entropy sign flipped",
    ),
    (
        "creative/speech_cinema/vad_detector.py",
        "(entropy <= cfg.entropy_threshold)",
        "(entropy >= cfg.entropy_threshold)",
        "M13 VAD entropy gate inverted",
    ),
    (
        "creative/speech_cinema/vad_detector.py",
        "(energy_db >= cfg.energy_threshold_db)",
        "(energy_db < cfg.energy_threshold_db)",
        "M14 VAD energy comparator inverted",
    ),
    (
        "creative/speech_cinema/vad_detector.py",
        "width = min(max(1, int(cfg.smoothing_frames)), int(raw.size))",
        "width = max(1, int(cfg.smoothing_frames))",
        "M15 short-clip window clamp removed (regression)",
    ),
    (
        "creative/speech_cinema/vad_detector.py",
        "bounded = i > 0 and j < len(smooth)",
        "bounded = True",
        "M16 edge-silence fill restored (regression)",
    ),
    (
        "creative/speech_cinema/forced_aligner.py",
        "best, p = min(opts, key=lambda z: (z[0], z[1]))",
        "best, p = max(opts, key=lambda z: (z[0], z[1]))",
        "M17 DTW recurrence min->max",
    ),
    (
        "creative/speech_cinema/forced_aligner.py",
        "band = max(n, m) if band is None else max(abs(n - m), int(band))",
        "band = max(n, m) if band is None else int(band)",
        "M18 DTW band widening removed",
    ),
    (
        "creative/speech_cinema/isochronous_dubbing.py",
        "ratio <= 0.2 or ratio >= 4.0",
        "ratio <= 0.2 or ratio > 40.0",
        "M19 stretch upper bound 4.0 -> 40.0",
    ),
    (
        "creative/speech_cinema/isochronous_dubbing.py",
        "pos = np.linspace(0, len(x) - 1, n)",
        "pos = np.linspace(0, len(x), n)",
        "M20 WSOLA endpoint off-by-one",
    ),
    (
        "creative/speech_cinema/isochronous_dubbing.py",
        "a = (pos - lo).astype(np.float32)",
        "a = 0.999 * (pos - lo).astype(np.float32)",
        "M21 interpolation weight perturbed",
    ),
    (
        "creative/speech_cinema/subtitle_compiler.py",
        "len(proposed) > max_chars",
        "len(proposed) >= max_chars",
        "M22 42-char limit made exclusive",
    ),
    (
        "creative/speech_cinema/subtitle_compiler.py",
        "if end <= start:",
        "if end < start:",
        "M23 zero-length cue accepted",
    ),
    (
        "creative/speech_cinema/subtitle_compiler.py",
        "return float(whole) + int(fraction) / float(10 ** len(fraction))",
        "return float(whole) + int(fraction) / 1000.0",
        "M24 fractional seconds scale fixed to /1000",
    ),
    (
        "creative/speech_cinema/color_imaging.py",
        "headroom: Array = np.minimum(1.0, 1.0 / np.maximum(peak, np.finfo(np.float64).tiny))",
        "headroom: Array = np.ones_like(peak)",
        "M25 filmic [0,1] bound removed (regression)",
    ),
    (
        "creative/speech_cinema/color_imaging.py",
        "0.22, 0.30, 0.10, 0.20, 0.01",
        "0.22, 0.30, 0.10, 0.20, 0.02",
        "M26 filmic shoulder constant e",
    ),
    (
        "creative/speech_cinema/color_imaging.py",
        "[[0.8951, 0.2664, -0.1614]",
        "[[0.8952, 0.2664, -0.1614]",
        "M27 Bradford coefficient 0.8951->0.8952",
    ),
    (
        "creative/speech_cinema/color_imaging.py",
        "powers: Array = np.asarray(values ** (-0.5 if inverse else 0.5)",
        "powers: Array = np.asarray(values ** (-0.5 if not inverse else 0.5)",
        "M28 matrix sqrt inverse flag inverted",
    ),
    (
        "creative/speech_cinema/color_imaging.py",
        "result: Array = (1.0 - alpha) * src_flat + alpha * matched",
        "result: Array = alpha * src_flat + (1.0 - alpha) * matched",
        "M29 OT blend weights swapped",
    ),
    (
        "creative/speech_cinema/color_imaging.py",
        "np.rint(arr * 255.0).astype(np.uint8)",
        "np.floor(arr * 255.0).astype(np.uint8)",
        "M30 PNG quantisation rint->floor",
    ),
]


def _env(base: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(base)
    env.pop("PYTEST_ADDOPTS", None)
    return env


def _run_zone(base: Path) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *ZONE_TESTS,
        f"--ignore={CAMPAIGN_FILE}",
        "-q",
        "-x",
        "-p",
        "no:cacheprovider",
        "-o",
        "addopts=",
    ]
    return subprocess.run(
        cmd, cwd=REPO_ROOT, env=_env(base), capture_output=True, text=True, timeout=600, check=False
    )


@pytest.mark.slow
def test_thirty_mutants_are_all_killed_by_the_hyperion_zone(tmp_path: Path) -> None:
    assert len(MUTANTS) == 30
    base = tmp_path / "pkg"
    shutil.copytree(PACKAGE, base / "nexus_ai_agent", ignore=shutil.ignore_patterns("__pycache__"))

    probe = subprocess.run(
        [sys.executable, "-c", "import nexus_ai_agent; print(nexus_ai_agent.__file__)"],
        cwd=REPO_ROOT,
        env=_env(base),
        capture_output=True,
        text=True,
        check=True,
    )
    assert probe.stdout.strip().startswith(str(base)), "subprocess must import the mutable copy"

    baseline = _run_zone(base)
    assert baseline.returncode == 0, f"unmutated zone must be green:\n{baseline.stdout[-1500:]}"

    survivors: list[str] = []
    for module, old, new, label in MUTANTS:
        path = base / "nexus_ai_agent" / module
        original = path.read_text(encoding="utf-8")
        assert original.count(old) == 1, f"{label}: anchor must occur exactly once in {module}"
        try:
            path.write_text(original.replace(old, new, 1), encoding="utf-8")
            result = _run_zone(base)
        finally:
            path.write_text(original, encoding="utf-8")
        if result.returncode == 0:
            survivors.append(label)

    assert survivors == [], f"surviving mutants ({len(survivors)}/30): {survivors}"
