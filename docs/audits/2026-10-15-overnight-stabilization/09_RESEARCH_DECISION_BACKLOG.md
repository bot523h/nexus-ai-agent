# 09 — Research-informed decision backlog (Phase 9)

Rule: no library is adopted because research mentions it; each item has a trigger and a proof obligation. Horizon: NOW = this PR/next session; NEXT = after #218 merges; LATER = needs an owner decision or an external dependency.

| # | Decision | Horizon | Trigger / proof obligation |
|---|---|---|---|
| 1 | Keep the coordination ref (`refs/nexus/board`) as the only board authority; fallback = GitHub Git-Data fast-forward `PATCH` | NOW (done) | trigger for fallback: a push to `refs/nexus/*` refused by hosting for any agent account |
| 2 | Wire the CAS Asset Library into `archive_trim_evidence` (hard-link attempt files to blobs) | NEXT | proof: existing passport tests unchanged + `fsck` consistent after N trims with duplicate inputs |
| 3 | Durable Creative Graph as a **projection** of journal + passports, adding only the missing edge kinds (Intent→Attempt, Asset admission) as journal events | NEXT (design), LATER (code) | proof: chain verification unchanged for old journals; graph rebuildable from scratch |
| 4 | Per-command deadline + envelope byte cap in the CommandBus | NEXT | proof: a sleeping handler is refused without partial apply (test) |
| 5 | `correlation_id` as a separate optional field through RequestContext → job payload → journal | NEXT | proof: one Telegram message yields one id across log, row, journal (test) |
| 6 | Sign the Artifact Passport (reuse `packs/ed25519.py`); add `verifier_version`; wrong-attempt/duplicate negative tests | LATER (key management decision) | trigger: artifacts leave the host (delivery pack) |
| 7 | Hash-locked install (`requirements-lock.txt --require-hashes`) + builder stage in the image | LATER (owner: Python version to lock) | proof: CI `container` job installs with hashes; image size and build time recorded |
| 8 | Small offline eval set for the provider router (10–20 prompts, golden shapes, no model download) | NEXT | proof: runs in CI under 60 s with the mocked providers; a live run is a separate, labelled job |
| 9 | RAG store: keep chromadb until the `sqlite_vec` lane covers the same queries | LATER | trigger: parity test over the docs corpus passes on `sqlite_vec` |
| 10 | OpenTelemetry SDK | LATER | trigger: second process or external collector; mapping defined in `06_…` |

Explicitly **not** adopted: OpenAssistant, MinIO, IPFS, Temporal, Ray, SeaweedFS, Prefect, Redis (no demonstrated need; each would add a second authority or an external dependency without a proof obligation met).
