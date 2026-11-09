# Licence and Model-Weight Matrix

Status: **VERIFIED** for the licence *facts* cited (read from the projects' own
repositories/pages during this mission). Status for *use in production*: see the
"verdict" column. No model weight is vendored in this repository.

| component | licence | commercial output allowed? | weight shipped here? | verdict |
|---|---|---|---|---|
| InsightFace `buffalo_l`, `buffalo_s`, `antelopev2` | non-commercial research only (project statements + downstream issue reports) | **no** | no | not admissible for paid output |
| InstantID checkpoints | research-only per distribution notes | **no** | no | excluded |
| InsightFace `inswapper` / face-swap weights | non-commercial | **no** | no | excluded |
| OpenCV **YuNet** face detector | MIT | yes | no | admissible |
| OpenCV **SFace** face recogniser | Apache-2.0 (training-data provenance undocumented) | yes, with documented residual risk | no | admissible **with caveat** |
| **PuLID** code | Apache-2.0; some paths require InsightFace encoders | conditional | no | conditional |
| **IP-Adapter Plus Face** | Apache-2.0 | yes | no | admissible |
| **Flux dev** base weights | non-commercial | **no** | no | excluded for paid output |
| **SDXL** base | CreativeML OpenRAIL++-M (use restrictions) | yes, with restrictions | no | admissible with restrictions |
| **CLIP** (ViT-B/32 etc.) | MIT (OpenAI release) | yes | no | admissible as secondary metric |
| **DINOv2** | Apache-2.0 (code), weights under Apache-2.0 per release | yes | no | admissible as secondary metric |
| **PyNaCl / libsodium** | Apache-2.0 | yes | no (dependency) | admissible |
| **numpy / Pillow** | BSD-family / HPND | yes | no (dependency) | admissible |
| **C2PA SDK** (`c2pa-rs`) | Apache-2.0 / MIT | yes | no | deferred (not needed by the shape) |
| **ISO/IEC 29794-5 / OFIQ** | standard text paywalled; reference implementation licence varies | depends | no | adopted as design guidance only |

## Rules this mission enforced

1. **Research-only weights never reach commercial output.** InsightFace packs are
   excluded from the pipeline, not merely "to be replaced later".
2. **No weight is committed.** The prototype generates its corpus procedurally; no
   third-party model file exists in the tree.
3. **Provenance is recorded even when the licence is permissive.** SFace's
   training-data provenance is undocumented; the verdict carries that caveat rather
   than presenting Apache-2.0 as a clean bill of health.
4. **Model lineage belongs in the evidence bundle**, not in a wiki page: a bundle
   records digests of the artifacts and the calibration; a production deployment
   would add a `model_lineage` assertion naming base model, adapter and version.

## What a reviewer should check before shipping

* Re-read each project's licence at the version actually pinned (licences change;
  this table is a 2026-11 snapshot, not a legal opinion).
* Confirm the base model's commercial terms for the region of distribution.
* Confirm that training-data provenance for any recogniser is documented, or accept
  the residual risk in writing.
* Confirm that synthetic marking (EU AI Act Art. 50(2), from 2026-08-02) is present
  in every exported asset — the capsule carries the flag, the exporter must apply it.

**This document is an engineering record, not legal advice.**
