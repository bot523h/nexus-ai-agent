# Live Truth — Character Capsule

Measured in this workspace on **2026-11-09**. Every number below comes from a
command that was executed here; anything that could not be measured is written as
`NOT_MEASURED` with the reason.

## Environment actually used

| fact | value |
|---|---|
| platform | `Linux-6.1.158+-x86_64-with-glibc2.36`, `x86_64` |
| CPU / RAM | 2 vCPU / ~3 GB (2 GB usable in practice) |
| Python | 3.11.2 |
| GPU | none — `NOT_MEASURED: no GPU device present (nvidia-smi unavailable)` |
| ffmpeg / torch / onnxruntime / opencv | absent |
| libraries used | numpy 2.4.6, pillow 13.0.0, PyNaCl, pytest 9.2.0, ruff |
| longest run | benchmark: 6.8 s wall, 57.3 MB peak RSS |

## What was executed

```
PYTHONPATH=. python -m experiments.character_capsule.benchmark \
    --work-dir experiments/character_capsule/.work
→ 12/12 scenarios passed, exit 0

PYTHONPATH=. python -m pytest tests/character_capsule --noconftest -q
→ 127 passed in 68.8 s (post-hardening, commit 757dafc)

ruff check experiments/character_capsule scripts/character_capsule tests/character_capsule
→ All checks passed
```

## Digests of the executed run

| artifact | digest |
|---|---|
| corpus (63 frames, 3 characters) | `sha256:b62debe73ce66e37d9917efc3812097229177e56a34599d33ccdf82047cb2bc1` |
| calibration | `sha256:c4abbfa644fc214e6fd89f50a0c8384f16a9d39aa1a1c1e2e4db7aeaa51b951b` |
| benchmark results | `sha256:a57faefa3d18863f3afe2df44f92f5a1867fc771d61118abb434b2eb05252c1d` |
| evidence bundle | `sha256:dda24a9da5e672234be06606c3f4a9b4cd78cd11431d773a7312bd5a514b3e36` |
| evidence claim | `sha256:35be37f371622579e31b2ccc207bade574701435cf52d5cb2a87352173ebe977` |
| capsule `NAGAR-CHAR-000001` | recomputed per run; sealed digest verified by `capsule.verify()` in tests |

Re-running the benchmark twice on separate directories produces the identical
corpus digest and identical verdicts (asserted in
`tests/character_capsule/test_benchmark_end_to_end.py::test_corpus_digest_is_reproducible`).

## What is real

* A capsule model with identity-vs-revision scoping that **refuses** identity edits
  (`ScopeViolation`) and requires a new identity version instead.
* Wardrobe timeline with era rules, ordered plan, and change events; canon with
  immutable facts, mutable facts, and an append-only story ledger.
* An independent verifier with a **closed 8-state vocabulary** and a documented
  precedence order; seven measured dimensions (face, body, hair, wardrobe, voice,
  motion, scene) plus canon.
* A benchmark of 12 scenarios (the 10 required + 2 failure-mode extras) run
  against real generated frames.
* Content-addressed evidence bundles with one hard binding and optional Ed25519
  signature; verification of tampering is tested.

## What is *not* real, and is not claimed

| claim that is NOT made | why |
|---|---|
| "photographic identities verify" | corpus is procedural vector art; thresholds do not transfer — `NOT_MEASURED` |
| "face matching is production-grade" | held-out face operating point: FAR **0.600**, FRR **0.250**, EER **0.325** on 4 genuine / 10 impostor scores. Weak, and stated as weak. |
| "hair continuity is verified" | no hair-change samples exist in the corpus, so hair has **no calibrated threshold**; it is advisory only |
| "deep-learning embeddings were compared" | no licensed embedding model offline — `NOT_MEASURED` |
| "evidence is signed" | no signing key is present in this environment; the bundle is `UNSIGNED` and says so |
| "GPU cost per shot" | no GPU — `NOT_MEASURED` |
| "human review agreed" | no human reviewer — `NOT_MEASURED` |

## Honest reading of the 12/12 scenario result

The scenarios pass **as designed**, including the look-alike and the canon
conflict, but the scenario set is easier than the held-out threshold sweep:

* most genuine scenarios use frames that are near-identical to a gallery frame
  (distance 0.000), so face verification is trivially satisfied there;
* the look-alike is rejected at distance 0.449 against a reject boundary of
  0.215 — a real decision, but with a narrow margin;
* the face dimension's own held-out numbers (FAR 0.60) show that on a harder
  sweep the same threshold would raise many false alarms.

Therefore: **the verifier is a working prototype, not a production gate.** The
benchmark demonstrates that the states, the precedence, and the evidence chain
work; it does not demonstrate production accuracy.
