# Character Intelligence — Research Notes

Scope: what the field actually offers for *character continuity* (identity +
wardrobe + body across shots), what is licence-usable, and what this mission chose
and rejected. Sources were read during this mission; where a page could not be
re-read, the claim is marked and not used as a number.

## 1. Two different problems, often conflated

| problem | question | typical tooling |
|---|---|---|
| **identity generation** | produce a *new* image of the same person | LoRA / IP-Adapter / InstantID / PuLID |
| **identity verification** | decide whether an image *is* the same person | face embeddings (ArcFace/SFace), quality gates |

A character pipeline needs both, and they have opposite failure modes: generators
are tuned to make identity *look* right, verifiers must be able to say "no". Using
the generator as its own verifier is the central methodological error this mission
avoids.

## 2. Identity adapters (generation side)

Reported strengths on public comparisons (no vendor re-run here; treat as claims):

* **InstantID** — strongest raw identity similarity in third-party comparisons
  (~0.78 face similarity), but pose-locked to the reference.
* **PuLID** — slightly lower similarity (~0.69–0.72), best style flexibility,
  Flux-native, Apache-2.0 code.
* **IP-Adapter FaceID (Plus v2)** — weakest identity (~0.62), but the most
  multi-reference friendly; Apache-2.0 variant exists.
* **Trained per-character LoRA** — best for a long-lived persona; costs training
  time and a per-character artifact.

Critical caveat found while checking licences: the *identity encoders* these rely
on (InsightFace `antelopev2` / `buffalo_l`, and the InstantID checkpoints) are
**non-commercial research** licences. See
[LICENSE_AND_MODEL_WEIGHT_MATRIX.md](LICENSE_AND_MODEL_WEIGHT_MATRIX.md).

**None of these carry wardrobe or body.** An identity adapter reproduces a face;
it knows nothing about "the same coat, now wet" or "the same shoulder width". That
gap is exactly what a capsule exists to make explicit.

## 3. Verification side

* **ISO/IEC 29794-5** (face image quality, 2023, revised 2025) with **OFIQ** as a
  reference implementation defines quality measures (single face, eyes open,
  natural colour, pose, luminance moments). The operational lesson adopted here:
  **prune low-quality samples instead of scoring them** — quality-threshold pruning
  improves FNMR/FMR at the cost of discarded samples. Implemented as
  `QUALITY_GATES` + `INSUFFICIENT_EVIDENCE`.
* **DreamBench++ / RefVNLI / DSH-Bench** all report that CLIP-I / DINO-I style
  metrics correlate poorly with human judgement: they reward global shape and
  style and miss facial detail. Consequence adopted here: such metrics may be
  reported as *secondary* signals only, never as ground truth. This prototype goes
  further and does not use CLIP/DINO at all, because no weights are available
  offline and their correlation with judgement is poor.
* Metric choice in the benchmark: per-dimension distance + FAR/FRR/EER at the
  calibrated operating point, reported on a **held-out character**.

## 4. Provenance and disclosure law (what a capsule must carry)

* **EU AI Act Art. 50** applies from **2 Aug 2026** (provisional Digital Omnibus
  grace to 2 Dec 2026 for pre-existing systems): providers must mark synthetic
  image/audio/video machine-readably (50(2)), and deployers must disclose deepfakes
  (50(4)). A capsule therefore carries `synthetic_marking` and disclosure metadata;
  the evidence bundle repeats it so it survives outside the database.
* **C2PA 2.x** manifest structure was read from the specification: assertions +
  claim + claim signature (COSE_Sign1), exactly **one hard binding**
  (`c2pa.hash.data` and friends) that excludes the manifest's own bytes, plus
  ingredient manifests for lineage. Two consequences adopted: (a) model lineage +
  a hard-binding digest instead of inventing a format; (b) absence of a manifest
  proves nothing, so `UNSIGNED` is a distinct state from `VALID`.
* **ELVIS Act** (Tennessee, in force 1 Jul 2024) covers real *and simulated* voice
  and reaches tool providers; **NO FAKES Act** S.4591 advanced in June 2026 but is
  not law; an **FCC** ruling (Feb 2024) brings AI voices under TCPA. Practical
  rule adopted: a real person's capsule requires written, specific, informed
  consent that names AI cloning, and synthetic-only corpora must say so explicitly.

## 5. What this mission built, given the above

| decision | reason |
|---|---|
| classical descriptors, no learned embeddings | no licensed model offline; keeps the verifier inspectable and dependency-light |
| per-dimension calibration with genuine/impostor splits | thresholds must be measured on *this* corpus, not borrowed |
| quality gate + `INSUFFICIENT_EVIDENCE` | ISO 29794-5 pruning; a bad frame must not become a drift alarm |
| one hard binding + lineage in the evidence bundle | C2PA-compatible shape without pretending to implement C2PA |
| no reliance on any vendor's "identity score" | those scores are optimised for looking right, not for saying no |

## 6. Open research questions (honest list)

1. Does the group-weighted classical descriptor survive a photographic corpus?
   `NOT_MEASURED` — no photographic data, no licence to use any.
2. What is the real cross-domain FAR for this band once embeddings are allowed?
   `NOT_MEASURED`.
3. How should a *body* descriptor be built so that pose and wardrobe decouple?
   Currently pose changes silhouette; a differentiable body model is **DEFERRED**.
4. How does wardrobe continuity behave under occlusion (coat over a bag)?
   Not tested; the corpus has no occlusion.
