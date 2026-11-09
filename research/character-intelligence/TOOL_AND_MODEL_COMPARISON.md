# Tool and Model Comparison (for a character-capsule pipeline)

Status: **VERIFIED as a comparison** — every row is a decision input, none of the
vendors' numbers were re-measured here. Claims that were not read from a primary
licence or spec are marked *vendor claim*.

| option | what it does | licence reality | fits a capsule? | decision |
|---|---|---|---|---|
| **InsightFace** `buffalo_l` / `antelopev2` | face detection + ArcFace embeddings | models **non-commercial research only** | technically excellent, legally unusable for paid output | **EXCLUDED** |
| **OpenCV YuNet** (face detection) | detection only | MIT | yes | **ACCEPTED** (candidate, see note) |
| **OpenCV SFace** (recognition) | 128-d embedding | Apache-2.0; training-data provenance undocumented | yes, with a documented residual risk | **ACCEPTED with caveat** |
| **InstantID** | identity generation from one photo | checkpoints research-only | no (licence) | **EXCLUDED** |
| **PuLID** | Flux-native identity adapter | Apache-2.0 code; depends on InsightFace encoders for some paths | conditional | **CONDITIONAL** |
| **IP-Adapter Plus Face** | identity adapter, multi-reference | Apache-2.0 | yes (generation side only) | **ACCEPTED** |
| **per-character LoRA** | best long-term identity fidelity | depends on base model licence (e.g. Flux dev = non-commercial) | yes, if base licence allows | **CONDITIONAL** |
| **CLIP-I / DINO-I** scores | image similarity | model licences vary; documented to correlate poorly with human judgement | as secondary metric only | **SECONDARY ONLY** |
| **C2PA SDK (`c2pa-rs`, `c2patool`)** | manifest signing/validation | Apache-2.0/MIT | yes | **DEFERRED** (this mission models the shape, not the container) |
| **OFIQ / ISO 29794-5 measures** | face quality | reference implementation, licence varies by release | yes, conceptually | **ADOPTED AS DESIGN** (not vendored) |
| **ffmpeg / torch / onnxruntime** | media + inference | permissive | yes | **ABSENT in this environment** — `NOT_MEASURED` |

## The one-line reason this prototype uses classical descriptors

The licences that make an InsightFace-based pipeline *good* also make it unusable
for commercial output, and the licences that make it *usable* (SFace) carry a
training-data provenance risk. Rather than ship a dependency that cannot be
defended, the prototype measures the problem with inspectable geometry and chroma
statistics, records its weak operating point honestly, and leaves the embedding
upgrade behind a clean seam (`vision.distance` / `group_stats`).

## Cost shape (no GPU here — `NOT_MEASURED` for anything GPU)

| path | what it costs | measured? |
|---|---|---|
| classical descriptor (this prototype) | ~250 ms per shot at 384×768 on 2 vCPU, 57 MB RSS | **measured** |
| SFace embedding | ~10–30 ms per face on CPU (vendor claim) | **NOT_MEASURED** |
| InstantID / PuLID generation | seconds per image on one 24 GB GPU (vendor claim) | **NOT_MEASURED** |
| per-character LoRA training | tens of minutes on one GPU (vendor claim) | **NOT_MEASURED** |

## Selection rule applied

> A component is admissible only if (a) its licence permits the intended output,
> (b) its failure mode is visible in the report, and (c) removing it does not
> require rewriting the capsule schema.

InsightFace fails (a); CLIP/DINO fail (b) as ground truth; the C2PA SDK fails (c)
only in the sense that it would add a container format we cannot test here, so it
is deferred rather than rejected.
