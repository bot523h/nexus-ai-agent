"""W10X scientific mutation campaign (30 mutants, bytecode-cache safe).

Each mutant replaces one source snippet with a semantically wrong variant and
runs only that wave's focused test module inside a disposable copy of ``src`` and
the selected tests.  A mutant is *killed* only by exit code 1 with a real test
failure; collection errors, syntax errors and other infrastructure failures are
classified ``invalid`` rather than counted as kills.  Source bytes are restored
in ``finally`` and verified against their pre-flight SHA-256; bytecode caches are
purged before each mutation, after writing, and after restoring.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
_INFRA_TOKENS = (
    "ERROR collecting",
    "ImportError",
    "ModuleNotFoundError",
    "SyntaxError",
    "no tests ran",
    "collected 0 items",
    "INTERNALERROR",
)
_FAILED_PATTERN = re.compile(r"\b[1-9]\d*\s+failed\b")


@dataclass(frozen=True, slots=True)
class Mutation:
    name: str
    wave: int
    source: str
    test: str
    old: str
    new: str


def _wave(wave: int, filename: str) -> str:
    return f"tests/unit/test_w10x_wave{wave}_{filename}.py"


MUTATIONS: tuple[Mutation, ...] = (
    # Wave 1 — SQLite pragma calculus and Kingman bounds.
    Mutation(
        "w1_queue_instability_off_by_one",
        1,
        "src/nexus_ai_agent/storage/sqlite_pragma_calculus.py",
        _wave(1, "sqlite_pragma_calculus"),
        "    if rho >= 1.0:",
        "    if rho > 1.0:",
    ),
    Mutation(
        "w1_wait_denominator_corrupted",
        1,
        "src/nexus_ai_agent/storage/sqlite_pragma_calculus.py",
        _wave(1, "sqlite_pragma_calculus"),
        "mean_wait_ms = (rho / (1.0 - rho)) * ((1.0 + cv * cv) / 2.0) * mean_ms",
        "mean_wait_ms = (rho / (1.0 + rho)) * ((1.0 + cv * cv) / 2.0) * mean_ms",
    ),
    Mutation(
        "w1_foreign_keys_not_enforced",
        1,
        "src/nexus_ai_agent/storage/sqlite_pragma_calculus.py",
        _wave(1, "sqlite_pragma_calculus"),
        "    fk_expected = 1 if policy.foreign_keys else 0",
        "    fk_expected = 0",
    ),
    # Wave 2 — Kahan geometry and forensic vector digest.
    Mutation(
        "w2_tamper_digest_ignored",
        2,
        "src/nexus_ai_agent/memory/riemannian_vector_fallback.py",
        _wave(2, "riemannian_vector_fallback"),
        "        if actual == stored_digest:",
        "        if True:",
    ),
    Mutation(
        "w2_kahan_compensation_dropped",
        2,
        "src/nexus_ai_agent/memory/riemannian_vector_fallback.py",
        _wave(2, "riemannian_vector_fallback"),
        "    result = total + compensation",
        "    result = total",
    ),
    Mutation(
        "w2_identity_geodesic_artefact",
        2,
        "src/nexus_ai_agent/memory/riemannian_vector_fallback.py",
        _wave(2, "riemannian_vector_fallback"),
        "    if dot >= 1.0 - 1e-12:\n        return 0.0",
        "    if False:\n        return 0.0",
    ),
    # Wave 3 — idempotent inbox DFA and callback HMAC.
    Mutation(
        "w3_byzantine_payload_mismatch_allowed",
        3,
        "src/nexus_ai_agent/adapters/sqlite_update_inbox.py",
        _wave(3, "idempotent_inbox_dfa"),
        "            if receipt.payload_digest != digest:",
        "            if False:",
    ),
    Mutation(
        "w3_committed_terminal_replays_as_applied",
        3,
        "src/nexus_ai_agent/adapters/sqlite_update_inbox.py",
        _wave(3, "idempotent_inbox_dfa"),
        "            if receipt.state is InboxState.COMMITTED:\n"
        "                return InboxDecision(receipt=receipt, replayed=True, applied=False)",
        "            if False:\n"
        "                return InboxDecision(receipt=receipt, replayed=True, applied=False)",
    ),
    Mutation(
        "w3_callback_hmac_not_verified",
        3,
        "src/nexus_ai_agent/domain/idempotent_inbox.py",
        _wave(3, "idempotent_inbox_dfa"),
        "        if not hmac.compare_digest(expected, tag):",
        "        if False:",
    ),
    # Wave 4 — BS.1770 relative gate and JS flash rejection.
    Mutation(
        "w4_loudness_offset_corrupted",
        4,
        "src/nexus_ai_agent/creative/perception/psychoacoustics_bs1770.py",
        _wave(4, "perception_lane"),
        "    return -0.691 + 10.0 * math.log10(energy)",
        "    return -6.91 + 10.0 * math.log10(energy)",
    ),
    Mutation(
        "w4_flash_rejection_disabled",
        4,
        "src/nexus_ai_agent/creative/perception/radiometric_scene_cut.py",
        _wave(4, "perception_lane"),
        "            and skip < config.flash_return_ratio * distance",
        "            and False",
    ),
    Mutation(
        "w4_lookahead_branch_disabled",
        4,
        "src/nexus_ai_agent/creative/perception/radiometric_scene_cut.py",
        _wave(4, "perception_lane"),
        "    if after is not None:",
        "    if False:",
    ),
    # Wave 5 — Beer-Lambert inversion and OKLab hue protection.
    Mutation(
        "w5_melanin_cross_term_sign_flipped",
        5,
        "src/nexus_ai_agent/creative/temporal/biophysical_portrait.py",
        _wave(5, "vision_biophysics_ot"),
        "    raw_melanin = (gram_11 * projection_mel - gram_01 * projection_hem) / determinant",
        "    raw_melanin = (gram_11 * projection_mel + gram_01 * projection_hem) / determinant",
    ),
    Mutation(
        "w5_hemoglobin_cross_term_sign_flipped",
        5,
        "src/nexus_ai_agent/creative/temporal/biophysical_portrait.py",
        _wave(5, "vision_biophysics_ot"),
        "    raw_hemoglobin = (gram_00 * projection_hem - gram_01 * projection_mel) / determinant",
        "    raw_hemoglobin = (gram_00 * projection_hem + gram_01 * projection_mel) / determinant",
    ),
    Mutation(
        "w5_oklab_hue_bound_bypassed",
        5,
        "src/nexus_ai_agent/creative/temporal/biophysical_portrait.py",
        _wave(5, "vision_biophysics_ot"),
        "    if delta <= max_delta_deg or graded.chroma <= 1e-6:",
        "    if True:",
    ),
    # Wave 6 — exact rational time, ripple edits and speed-ramp calculus.
    Mutation(
        "w6_rational_time_addition_subtracts",
        6,
        "src/nexus_ai_agent/nagar/p0/semantic_time.py",
        _wave(6, "nagar_p0_rational_calculus"),
        "        return RationalTime(self.seconds + other.seconds)",
        "        return RationalTime(self.seconds - other.seconds)",
    ),
    Mutation(
        "w6_ripple_insert_does_not_shift",
        6,
        "src/nexus_ai_agent/nagar/p0/nel_algebra.py",
        _wave(6, "nagar_p0_rational_calculus"),
        "                    Interval(clip.interval.start + inserted, "
        "clip.interval.end + inserted),",
        "                    Interval(clip.interval.start, clip.interval.end),",
    ),
    Mutation(
        "w6_speed_ramp_slope_corrupted",
        6,
        "src/nexus_ai_agent/nagar/p0/nel_algebra.py",
        _wave(6, "nagar_p0_rational_calculus"),
        "        return (self.v1 - self.v0) / self.span",
        "        return (self.v0 + self.v1) / self.span",
    ),
    # Wave 7 — Pearl back-door and bounded director retention.
    Mutation(
        "w7_backdoor_descendant_check_removed",
        7,
        "src/nexus_ai_agent/nagar/causal_memory/do_calculus.py",
        _wave(7, "causal_do_calculus_director"),
        "    if any(node in descendants for node in adjustment):\n        return False",
        "    if False:\n        return False",
    ),
    Mutation(
        "w7_d_separation_self_identity",
        7,
        "src/nexus_ai_agent/nagar/causal_memory/do_calculus.py",
        _wave(7, "causal_do_calculus_director"),
        "    if first == second:\n        return False",
        "    if first == second:\n        return True",
    ),
    Mutation(
        "w7_director_retention_gate_removed",
        7,
        "src/nexus_ai_agent/nagar/director/bounded_director.py",
        _wave(7, "causal_do_calculus_director"),
        "                if measured < action.required_retention:",
        "                if False:",
    ),
    # Wave 8 — conformal quantile, abstention and geodesic similarity.
    Mutation(
        "w8_conformal_quantile_rank_wrong",
        8,
        "src/nexus_ai_agent/retrieval/conformal_hybrid.py",
        _wave(8, "conformal_hybrid_retrieval"),
        "        rank = math.ceil((count + 1) * (1.0 - self.alpha))",
        "        rank = math.ceil(count * (1.0 - self.alpha))",
    ),
    Mutation(
        "w8_conformal_gate_accepts_everything",
        8,
        "src/nexus_ai_agent/retrieval/conformal_hybrid.py",
        _wave(8, "conformal_hybrid_retrieval"),
        "            if value <= threshold:",
        "            if True:",
    ),
    Mutation(
        "w8_geodesic_similarity_replaced_by_cosine",
        8,
        "src/nexus_ai_agent/retrieval/conformal_hybrid.py",
        _wave(8, "conformal_hybrid_retrieval"),
        "    return 1.0 - math.acos(cosine) / math.pi",
        "    return 1.0 - cosine",
    ),
    # Wave 9 — PID clamp, SPRT trip and SLO freeze.
    Mutation(
        "w9_pid_clamp_removed",
        9,
        "src/nexus_ai_agent/llm/gateway/control_theory_governor.py",
        _wave(9, "gateway_control_and_slo"),
        "        limit = max(self._minimum, min(self._maximum, raw))",
        "        limit = raw",
    ),
    Mutation(
        "w9_sprt_never_trips",
        9,
        "src/nexus_ai_agent/llm/gateway/control_theory_governor.py",
        _wave(9, "gateway_control_and_slo"),
        "        if self._ratio >= config.upper_threshold:",
        "        if False:",
    ),
    Mutation(
        "w9_slo_budget_exhaustion_ignored",
        9,
        "src/nexus_ai_agent/observability/slo_burn_calculus.py",
        _wave(9, "gateway_control_and_slo"),
        "        exhausted = global_total > 0 and allowed > 0.0 "
        "and global_failures >= allowed - _TOLERANCE",
        "        exhausted = False",
    ),
    # Wave 10 — license top, port typing and duration conservation.
    Mutation(
        "w10_license_top_not_absorbing",
        10,
        "src/nexus_ai_agent/creative/composition/license_lattice.py",
        _wave(10, "composition_runtime"),
        "    if _TOP in (first, second):\n        return _TOP",
        "    if _TOP in (first, second):\n        return first",
    ),
    Mutation(
        "w10_port_subtyping_disabled",
        10,
        "src/nexus_ai_agent/creative/composition/compiler.py",
        _wave(10, "composition_runtime"),
        "            if not produced.is_subtype_of(required):",
        "            if False:",
    ),
    Mutation(
        "w10_duration_conservation_ignored",
        10,
        "src/nexus_ai_agent/creative/composition/verifier.py",
        _wave(10, "composition_runtime"),
        "        duration_sum = sum(receipt.durations.values(), Fraction(0, 1))",
        "        duration_sum = receipt.master_duration",
    ),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _purge_bytecode_cache(source_path: Path) -> None:
    try:
        cached = Path(importlib.util.cache_from_source(str(source_path)))
    except (NotImplementedError, ValueError):  # pragma: no cover - exotic layouts
        cached = None
    if cached is not None:
        cached.unlink(missing_ok=True)
        cache_dir = cached.parent
        if cache_dir.is_dir():
            for stale in cache_dir.glob(f"{source_path.stem}.*.pyc"):
                stale.unlink(missing_ok=True)
    importlib.invalidate_caches()


def preflight(repo_root: Path = ROOT) -> list[Mutation]:
    seen: set[str] = set()
    root = Path(repo_root)
    for mutation in MUTATIONS:
        if mutation.name in seen:
            raise SystemExit(f"duplicate mutation name: {mutation.name}")
        seen.add(mutation.name)
        source = root / mutation.source
        test = root / mutation.test
        if not source.is_file():
            raise SystemExit(f"missing source: {mutation.source}")
        if not test.is_file():
            raise SystemExit(f"missing test: {mutation.test}")
        if mutation.old == mutation.new:
            raise SystemExit(f"no-op mutation: {mutation.name}")
        occurrences = source.read_text(encoding="utf-8").count(mutation.old)
        if occurrences != 1:
            raise SystemExit(
                f"mutation {mutation.name!r} matches {occurrences} times in {mutation.source}"
            )
    return list(MUTATIONS)


def run_mutation(mutation: Mutation, repo_root: Path = ROOT) -> str:
    root = Path(repo_root)
    source = root / mutation.source
    original = source.read_bytes()
    original_digest = hashlib.sha256(original).hexdigest()
    _purge_bytecode_cache(source)
    try:
        mutated = original.decode("utf-8").replace(mutation.old, mutation.new, 1)
        source.write_bytes(mutated.encode("utf-8"))
        _purge_bytecode_cache(source)
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", mutation.test],
                cwd=root,
                env=_test_env(root),
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return "not_run"
        return _classify_mutant_outcome(completed)
    finally:
        source.write_bytes(original)
        _purge_bytecode_cache(source)
        if _sha256(source) != original_digest:  # pragma: no cover - restoration guard
            raise SystemExit(f"FAILED to restore {mutation.source} byte-for-byte")
        importlib.invalidate_caches()


def _test_env(repo_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    source_root = str(Path(repo_root) / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = source_root if not existing else source_root + os.pathsep + existing
    return env


def _classify_mutant_outcome(completed: subprocess.CompletedProcess[str]) -> str:
    """Only a focused test failure with exit code 1 counts as a killed mutant."""

    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode == 0:
        return "survived"
    if completed.returncode != 1:
        return "invalid"
    if any(token in output for token in _INFRA_TOKENS):
        return "invalid"
    if not (_FAILED_PATTERN.search(output) or "FAILED" in output):
        return "invalid"
    return "killed"


def run_baseline(repo_root: Path = ROOT, mutations: Sequence[Mutation] | None = None) -> bool:
    root = Path(repo_root)
    selected = tuple(mutations) if mutations is not None else tuple(preflight(root))
    tests = sorted({mutation.test for mutation in selected})
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *tests],
            cwd=root,
            env=_test_env(root),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    if completed.returncode != 0:
        print("BASELINE FAILURE", flush=True)
        print((completed.stdout or "")[-4000:], flush=True)
        print((completed.stderr or "")[-2000:], flush=True)
    return completed.returncode == 0


def _prepare_sandbox(repo_root: Path, mutations: Sequence[Mutation]) -> Path:
    sandbox = Path(tempfile.mkdtemp(prefix="w10x-mutations-"))
    shutil.copytree(
        Path(repo_root) / "src",
        sandbox / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for relative in sorted({mutation.test for mutation in mutations}):
        destination = sandbox / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(repo_root) / relative, destination)
    return sandbox


def run_campaign(repo_root: Path = ROOT) -> int:
    mutations = tuple(preflight(repo_root))
    sandbox = _prepare_sandbox(repo_root, mutations)
    try:
        if not run_baseline(sandbox, mutations):
            print("BASELINE FAILED: refusing to classify mutants", flush=True)
            return 1
        counts = {"killed": 0, "survived": 0, "invalid": 0, "not_run": 0}
        for index, mutation in enumerate(mutations, 1):
            outcome = run_mutation(mutation, repo_root=sandbox)
            counts[outcome] += 1
            print(f"[{index:02d}/{len(mutations)}] {outcome:8s} {mutation.name}", flush=True)
            if outcome == "survived":
                print("  !! mutant survived: inspect the focused suite", flush=True)
            if outcome == "invalid":
                print("  !! invalid mutant run: not evidence of a kill", flush=True)
        print(
            f"SUMMARY total={len(mutations)} killed={counts['killed']} "
            f"survived={counts['survived']} invalid={counts['invalid']} "
            f"not_run={counts['not_run']}"
        )
        return 0 if counts["killed"] == len(mutations) else 1
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def main() -> int:
    return run_campaign()


if __name__ == "__main__":
    raise SystemExit(main())
