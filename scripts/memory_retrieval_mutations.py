#!/usr/bin/env python3
"""Mutation probes for the long-term-memory retrieval transformation.

Each mutant re-introduces one specific defect that the transformation removed,
in a throwaway copy of the package, and asserts that a named behavioural test
fails. A mutant that *survives* means the test suite does not actually defend
that invariant — which is exactly how the original defect shipped: the recall
harness was calibrated so that losing semantic retrieval could not fail.

The harness never edits the working tree. Exit status 0 requires a green
baseline, every mutant killed, and a green restored copy.

Usage::

    python scripts/memory_retrieval_mutations.py
    python scripts/memory_retrieval_mutations.py --list
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "nexus_ai_agent"
TEST_FILE = "tests/unit/test_memory_retrieval_truth.py"

LONG_TERM = Path("memory") / "long_term.py"
EVAL = Path("memory") / "eval.py"
ROUTER = Path("orchestration") / "router.py"


@dataclass(frozen=True)
class Mutation:
    name: str
    relative: Path
    old: str
    new: str
    test: str
    invariant: str


MUTATIONS: tuple[Mutation, ...] = (
    # ── the headline defect: ranking by a single dense lane ───────────────
    Mutation(
        "dense_only_ranking_restored",
        LONG_TERM,
        "        if lexical and dense:\n"
        "            fused = reciprocal_rank_fusion(\n"
        "                [lexical, dense],\n"
        "                weights=[self._lexical_weight, self._vector_weight],\n"
        "                limit=top_k,\n"
        "            )\n"
        "            mode = RetrievalMode.HYBRID_FUSION\n"
        "            lane_of = self._lane_membership(lexical, dense)\n",
        "        if lexical and dense:\n"
        "            fused = dense[:top_k]\n"
        "            mode = RetrievalMode.DENSE_VECTOR\n"
        "            lane_of = {item.doc_id: 'dense' for item in dense}\n",
        "test_recall_beats_chance_with_the_default_noise_embedder",
        "the lexical lane must participate: dense-only over hash-noise vectors ranks below chance",
    ),
    Mutation(
        "lexical_lane_dropped_from_fusion",
        LONG_TERM,
        "        elif lexical:\n"
        "            fused = lexical[:top_k]\n"
        "            mode = RetrievalMode.LEXICAL_BM25\n",
        "        elif lexical:\n"
        "            fused = []\n"
        "            mode = RetrievalMode.RECENCY_DEGRADED\n",
        "test_store_never_loses_a_memory_when_the_embedder_is_down",
        "BM25 over stored content is the guaranteed floor when no vector exists",
    ),
    # ── silent degradation ───────────────────────────────────────────────
    Mutation(
        "recency_fallback_not_marked_degraded",
        LONG_TERM,
        "                mode=RetrievalMode.RECENCY_DEGRADED,\n"
        "                hits=tuple(hits),\n"
        "                degraded=True,\n",
        "                mode=RetrievalMode.RECENCY_DEGRADED,\n"
        "                hits=tuple(hits),\n"
        "                degraded=False,\n",
        "test_recency_fallback_is_labelled_degraded_not_disguised",
        "newest-first must never be reported as if it were a semantic hit",
    ),
    Mutation(
        "strategy_not_reported",
        LONG_TERM,
        "            mode = RetrievalMode.HYBRID_FUSION\n",
        "            mode = RetrievalMode.LEXICAL_BM25\n",
        "test_recall_names_its_strategy",
        "a recall must name the strategy that actually produced it",
    ),
    # ── trust boundary: thread isolation ─────────────────────────────────
    Mutation(
        "vector_cache_shared_across_threads",
        LONG_TERM,
        "        self._vectors[thread_id] = scoped\n"
        "        self._vectors_loaded_for.add(thread_id)\n"
        "        return scoped\n",
        "        self._vectors[thread_id] = scoped\n"
        "        self._vectors_loaded_for.add(thread_id)\n"
        "        merged: dict[int, list[float]] = {}\n"
        "        for bucket in self._vectors.values():\n"
        "            merged.update(bucket)\n"
        "        return merged\n",
        "test_dense_lane_cache_is_thread_scoped",
        "one conversation's vectors must never be scored while answering another",
    ),
    Mutation(
        "hydrate_ignores_thread_scope",
        LONG_TERM,
        '            f"WHERE thread_id=? AND id IN ({placeholders})",\n'
        "            [thread_id, *ids],\n",
        '            f"WHERE id IN ({placeholders})",\n            ids,\n',
        "test_hydrate_refuses_ids_from_another_thread",
        "materialising conversation content is the last place a leak can be stopped",
    ),
    # ── offline-first (Q1) ───────────────────────────────────────────────
    Mutation(
        "store_propagates_embedder_failure",
        LONG_TERM,
        "            return list(await self._llm.embed(text))\n"
        "        except Exception:\n"
        "            return None\n",
        "            return list(await self._llm.embed(text))\n"
        "        except Exception:\n"
        "            raise\n",
        "test_store_never_loses_a_memory_when_the_embedder_is_down",
        "an unreachable embedder must not cost the user the memory itself",
    ),
    # ── the dimension / structural contract ──────────────────────────────
    Mutation(
        "dimension_contract_unenforced",
        LONG_TERM,
        "            if len(vector) == self.DIM and _validate_vector(vector, self.DIM):\n",
        "            if vector:\n",
        "test_embedder_returning_the_wrong_width_does_not_poison_the_store",
        "a provider swap that changes vector width must be refused, not stored",
    ),
    Mutation(
        "zero_norm_vectors_accepted",
        LONG_TERM,
        "        norm += value * value\n    return norm > 0.0\n",
        "        norm += value * value\n    return True\n",
        "test_structurally_invalid_vectors_never_enter_the_dense_lane",
        "a zero-norm or non-finite vector makes cosine meaningless",
    ),
    Mutation(
        "blob_width_guard_removed",
        LONG_TERM,
        "    if len(blob) % 4:\n        return None\n",
        "    if False:\n        return None\n",
        "test_corrupt_stored_blob_is_refused_not_reinterpreted",
        "a truncated blob must be refused by width, not handed to struct.unpack",
    ),
    # ── provenance ───────────────────────────────────────────────────────
    Mutation(
        "metadata_discarded_again",
        LONG_TERM,
        '        kind = str(meta.get("kind") or "turn")\n'
        '        source = meta.get("source")\n'
        "        source = str(source) if source is not None else None\n",
        '        _ = meta\n        kind = "turn"\n        source = None\n',
        "test_metadata_provenance_round_trips",
        "a memory whose origin is unknown cannot later be trusted or explained",
    ),
    # ── the repaired feedback loop ───────────────────────────────────────
    Mutation(
        "recall_baseline_softened",
        EVAL,
        "_BASELINE_RECALL_K = 1.0\n",
        "_BASELINE_RECALL_K = 0.25\n",
        "test_degradation_modes_now_fail_the_regression_guard",
        "the guard must fail when retrieval degrades — that was the blind spot",
    ),
    Mutation(
        "chance_floor_zeroed",
        EVAL,
        "    if not queries or not corpus or k < 1:\n        return 0.0\n",
        "    if True:\n        return 0.0\n",
        "test_chance_floor_is_analytical_and_stable",
        "a retrieval metric must know the score that luck alone achieves",
    ),
    # ── the corrected read policy ────────────────────────────────────────
    Mutation(
        "memory_read_gated_on_intent_again",
        ROUTER,
        "    _ = intent, text\n    return True\n",
        '    _ = text\n    return intent in ("task", "memory")\n',
        "test_should_read_memory_is_total_over_every_intent",
        "ordinary chat must consult memory: relevance is not an intent question",
    ),
)


def _run_pytest(package_root: Path, test: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    python_paths = [str(package_root), str(ROOT / "src")]
    if env.get("PYTHONPATH"):
        python_paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_paths)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "asyncio_mode=auto",
            test,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def _tail(output: str, lines: int = 8) -> str:
    return "\n".join(output.strip().splitlines()[-lines:])


def _is_test_failure(result: subprocess.CompletedProcess[str]) -> bool:
    output = result.stdout + result.stderr
    return result.returncode == 1 and re.search(r"(?m)^\d+ failed(?:,|\s|$)", output) is not None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        for mutation in MUTATIONS:
            print(f"{mutation.name}: {mutation.invariant}")
        return 0

    with tempfile.TemporaryDirectory(prefix="nexus-memory-mutations-") as temporary:
        package_root = Path(temporary) / "src"
        shutil.copytree(PACKAGE, package_root / "nexus_ai_agent")

        print("baseline ... ", end="", flush=True)
        baseline = _run_pytest(package_root, TEST_FILE)
        if baseline.returncode != 0:
            print("RED — refusing to mutate a failing baseline")
            print(_tail(baseline.stdout + baseline.stderr, lines=30))
            return 2
        print("GREEN")

        killed = 0
        survivors: list[str] = []
        for mutation in MUTATIONS:
            target = package_root / "nexus_ai_agent" / mutation.relative
            current = target.read_text(encoding="utf-8")
            if mutation.old not in current:
                print(f"{mutation.name}: DOES NOT APPLY — source drifted")
                survivors.append(mutation.name)
                continue

            target.write_text(current.replace(mutation.old, mutation.new, 1), encoding="utf-8")
            try:
                result = _run_pytest(package_root, f"{TEST_FILE}::{mutation.test}")
            finally:
                target.write_text(current, encoding="utf-8")

            if result.returncode == 0:
                print(f"{mutation.name}: SURVIVED — {mutation.invariant}")
                survivors.append(mutation.name)
            elif _is_test_failure(result):
                killed += 1
                print(f"{mutation.name}: killed")
            else:
                print(f"{mutation.name}: ERROR — mutant did not produce a pytest failure")
                print(_tail(result.stdout + result.stderr, lines=30))
                return 4

        print("restored baseline ... ", end="", flush=True)
        restored = _run_pytest(package_root, TEST_FILE)
        if restored.returncode != 0:
            print("RED — restored copy failed")
            print(_tail(restored.stdout + restored.stderr, lines=30))
            return 3
        print("GREEN")

    if survivors:
        print(f"{killed}/{len(MUTATIONS)} mutants killed; survivors: {', '.join(survivors)}")
        return 1
    print(f"{killed}/{len(MUTATIONS)} mutants killed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
