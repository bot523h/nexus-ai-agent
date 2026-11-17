"""Real code-mutation campaign for the AETHEL / ATLAS reasoning cores.

This is a *mutation* test, not an ordinary negative-input test. For each entry in
:data:`MUTANTS` a single, real source change is introduced into an isolated
temporary copy of the package; the targeted test file is then run in a
subprocess. A mutant counts as KILLED only when a relevant test actually fails
because of the injected defect. The campaign asserts every mutant is killed and
that every mutation anchor is unique in the real tree.

The temporary copy is created under ``tmp_path`` and the real repository source
is never modified. This is therefore a REAL code-mutation experiment (REAL
COMPUTE), not a simulation.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "src"
TESTS = REPO_ROOT / "tests"

#: ``(relative source path, original text, mutated text, targeted test file)``.
#: Each ``original`` must occur exactly once in the real tree.
MUTANTS: tuple[tuple[str, str, str, str], ...] = (
    # --- STN negative-cycle detection (episode_causality_lab/temporal.py) ---
    (
        "nexus_ai_agent/creative/episode_causality_lab/temporal.py",
        "if dist[a] + weight < dist[b]:",
        "if dist[a] + weight <= dist[b]:",
        "tests/creative/episode_causality_lab/test_temporal.py",
    ),
    (
        "nexus_ai_agent/creative/episode_causality_lab/temporal.py",
        "self._edges.append((con.a, con.b, con.upper, con.id))",
        "self._edges.append((con.a, con.b, con.lower, con.id))",
        "tests/creative/episode_causality_lab/test_temporal.py",
    ),
    (
        "nexus_ai_agent/creative/episode_causality_lab/temporal.py",
        "self._edges.append((con.b, con.a, -con.lower, con.id))",
        "self._edges.append((con.b, con.a, con.lower, con.id))",
        "tests/creative/episode_causality_lab/test_temporal.py",
    ),
    (
        "nexus_ai_agent/creative/episode_causality_lab/temporal.py",
        "return self.total_weight < 0 and bool(self.nodes)",
        "return self.total_weight <= 0 and bool(self.nodes)",
        "tests/creative/episode_causality_lab/test_temporal.py",
    ),
    (
        "nexus_ai_agent/creative/episode_causality_lab/temporal.py",
        "total += step[1]",
        "total -= step[1]",
        "tests/creative/episode_causality_lab/test_temporal.py",
    ),
    # --- exact rational time (composition/rational_time.py) ---
    (
        "nexus_ai_agent/creative/composition/rational_time.py",
        "return Fraction(1, 1) / time_base",
        "return Fraction(1, 1) * time_base",
        "tests/creative/composition/test_rational_time.py",
    ),
    (
        "nexus_ai_agent/creative/composition/rational_time.py",
        "return Fraction(num, den)",
        "return Fraction(den, num)",
        "tests/creative/composition/test_rational_time.py",
    ),
    (
        "nexus_ai_agent/creative/composition/rational_time.py",
        "return self.seconds < other.seconds",
        "return self.seconds > other.seconds",
        "tests/creative/composition/test_rational_time.py",
    ),
    (
        "nexus_ai_agent/creative/composition/rational_time.py",
        "return Fraction(self.value) * self.time_base",
        "return Fraction(self.value) / self.time_base",
        "tests/creative/composition/test_rational_time.py",
    ),
    (
        "nexus_ai_agent/creative/composition/rational_time.py",
        "if ticks.denominator != 1:\n"
        '            raise RateMismatchError(f"{seconds} s does not land',
        "if ticks.denominator == 1:\n"
        '            raise RateMismatchError(f"{seconds} s does not land',
        "tests/creative/composition/test_rational_time.py",
    ),
    # --- Porter-Duff factor table (composition/compositor_2d.py) ---
    (
        "nexus_ai_agent/creative/composition/compositor_2d.py",
        'PorterDuff.SRC_OVER: "1,1-alpha_s"',
        'PorterDuff.SRC_OVER: "1,1-alpha_d"',
        "tests/creative/composition/test_compositor_2d.py",
    ),
    (
        "nexus_ai_agent/creative/composition/compositor_2d.py",
        'PorterDuff.DST_OVER: "1-alpha_d,1"',
        'PorterDuff.DST_OVER: "1-alpha_s,1"',
        "tests/creative/composition/test_compositor_2d.py",
    ),
    (
        "nexus_ai_agent/creative/composition/compositor_2d.py",
        'PorterDuff.SRC_IN: "alpha_d,0"',
        'PorterDuff.SRC_IN: "alpha_s,0"',
        "tests/creative/composition/test_compositor_2d.py",
    ),
    (
        "nexus_ai_agent/creative/composition/compositor_2d.py",
        'PorterDuff.SRC_OUT: "1-alpha_d,0"',
        'PorterDuff.SRC_OUT: "1-alpha_s,0"',
        "tests/creative/composition/test_compositor_2d.py",
    ),
    # --- Viterbi montage optimizer (composition/montage_optimizer.py) ---
    (
        "nexus_ai_agent/creative/composition/montage_optimizer.py",
        "if existing is None or key < existing:",
        "if existing is None or key > existing:",
        "tests/creative/composition/test_montage_optimizer.py",
    ),
    (
        "nexus_ai_agent/creative/composition/montage_optimizer.py",
        "if i > 0:",
        "if i >= 0:",
        "tests/creative/composition/test_montage_optimizer.py",
    ),
    (
        "nexus_ai_agent/creative/composition/montage_optimizer.py",
        "best = min(beats, key=lambda a: (abs(cut - a.frame), a.frame))",
        "best = max(beats, key=lambda a: (abs(cut - a.frame), a.frame))",
        "tests/creative/composition/test_montage_optimizer.py",
    ),
    (
        "nexus_ai_agent/creative/composition/montage_optimizer.py",
        "return Fraction(slow) - tension * Fraction(slow - fast)",
        "return Fraction(slow) + tension * Fraction(slow - fast)",
        "tests/creative/composition/test_montage_optimizer.py",
    ),
    (
        "nexus_ai_agent/creative/composition/montage_optimizer.py",
        "if truncated:",
        "if not truncated:",
        "tests/creative/composition/test_montage_optimizer.py",
    ),
    # --- bounded edit planner (episode_causality_lab/plan.py) ---
    (
        "nexus_ai_agent/creative/episode_causality_lab/plan.py",
        "verified = cand.evidence_status is TriValue.TRUE and bool(cand.evidence_ref)",
        "verified = cand.evidence_status is TriValue.TRUE or bool(cand.evidence_ref)",
        "tests/creative/episode_causality_lab/test_plan.py",
    ),
    (
        "nexus_ai_agent/creative/episode_causality_lab/plan.py",
        "    if require:",
        "    if not require:",
        "tests/creative/episode_causality_lab/test_plan.py",
    ),
    # --- portrait kernels (composition/portrait_imaging.py) ---
    (
        "nexus_ai_agent/creative/composition/portrait_imaging.py",
        "SKIN_L_RANGE = (30.0, 85.0)",
        "SKIN_L_RANGE = (30.0, 20.0)",
        "tests/creative/packs/portrait/test_portrait_kernels.py",
    ),
    (
        "nexus_ai_agent/creative/composition/portrait_imaging.py",
        "if np.any(det <= 0):",
        "if np.any(det > 0):",
        "tests/creative/packs/portrait/test_portrait_kernels.py",
    ),
    # --- scene kernels (composition/scene_imaging.py) ---
    (
        "nexus_ai_agent/creative/composition/scene_imaging.py",
        "transmittance = np.exp(-beta * d)",
        "transmittance = np.exp(beta * d)",
        "tests/creative/packs/scene/test_scene_kernels.py",
    ),
    (
        "nexus_ai_agent/creative/composition/scene_imaging.py",
        "if beta < 0:",
        "if beta > 0:",
        "tests/creative/packs/scene/test_scene_adversarial.py",
    ),
    (
        "nexus_ai_agent/creative/composition/scene_imaging.py",
        "expanded = linear * gain",
        "expanded = linear / gain",
        "tests/creative/packs/scene/test_scene_kernels.py",
    ),
    # --- director shot planner (director/shot_planner.py) ---
    (
        "nexus_ai_agent/creative/director/shot_planner.py",
        "MIN_ANGLE_DELTA_DEG = 30.0",
        "MIN_ANGLE_DELTA_DEG = 10.0",
        "tests/creative/director/test_shot_planner.py",
    ),
    (
        "nexus_ai_agent/creative/director/shot_planner.py",
        "if angle < MIN_ANGLE_DELTA_DEG and focal_ratio < MIN_FOCAL_RATIO:",
        "if angle < MIN_ANGLE_DELTA_DEG or focal_ratio < MIN_FOCAL_RATIO:",
        "tests/creative/director/test_shot_planner.py",
    ),
    (
        "nexus_ai_agent/creative/director/shot_planner.py",
        "return 360.0 - delta if delta > 180.0 else delta",
        "return 360.0 - delta if delta < 180.0 else delta",
        "tests/creative/director/test_shot_planner.py",
    ),
    # --- three-valued causal engine (episode_causality_lab/causal.py) ---
    (
        "nexus_ai_agent/creative/episode_causality_lab/causal.py",
        "return TriValue.FALSE if self._closed_world else TriValue.UNKNOWN",
        "return TriValue.TRUE if self._closed_world else TriValue.UNKNOWN",
        "tests/creative/episode_causality_lab/test_causal.py",
    ),
    (
        "nexus_ai_agent/creative/episode_causality_lab/causal.py",
        "if any(f.status in (Status.NEEDS_REVIEW, Status.UNKNOWN) for f in findings):",
        "if any(f.status in (Status.PASS, Status.UNKNOWN) for f in findings):",
        "tests/creative/episode_causality_lab/test_causal.py",
    ),
    (
        "nexus_ai_agent/creative/episode_causality_lab/causal.py",
        "        elif current is TriValue.UNKNOWN:\n"
        "            findings.append(\n"
        "                Finding(\n"
        "                    code=CausalFindingCode.POSSESSION_UNKNOWN,",
        "        elif current is TriValue.TRUE:\n"
        "            findings.append(\n"
        "                Finding(\n"
        "                    code=CausalFindingCode.POSSESSION_UNKNOWN,",
        "tests/creative/episode_causality_lab/test_causal.py",
    ),
)


def test_every_mutation_anchor_is_unique() -> None:
    """Every ``original`` snippet must appear exactly once in the real tree."""
    for rel, old, _new, _test in MUTANTS:
        text = (SRC / rel).read_text(encoding="utf-8")
        assert text.count(old) == 1, f"{rel}: anchor count {text.count(old)} for {old!r}"


def test_at_least_thirty_mutants() -> None:
    assert len(MUTANTS) >= 30


@pytest.mark.parametrize(("rel", "old", "new", "target_test"), MUTANTS)
def test_mutant_is_killed(rel: str, old: str, new: str, target_test: str, tmp_path: Path) -> None:
    """Each injected defect must make the targeted test suite fail."""
    work = tmp_path / "campaign"
    shutil.copytree(SRC, work / "src")
    shutil.copytree(TESTS, work / "tests")
    shutil.copy(REPO_ROOT / "pyproject.toml", work / "pyproject.toml")

    module = work / "src" / rel
    original = module.read_text(encoding="utf-8")
    assert original.count(old) == 1
    module.write_text(original.replace(old, new, 1), encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(work / "src")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", target_test, "-q", "-p", "no:cacheprovider", "-x"],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode != 0, (
        f"mutant SURVIVED in {rel}: {old!r} -> {new!r}\nstdout tail:\n{result.stdout[-2000:]}"
    )
