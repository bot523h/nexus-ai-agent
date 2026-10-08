# Runbook: Hardening Batch (Wave-4)

> **Scope:** Wave-4 hardening batch (G, 2026-09-21) — version lockstep, delivery
> signing, storage resilience, memory eval, creative surface, bench gates.
> Companion to `DEPLOY_RUNBOOK.md`, `NEON_LIFECYCLE_RUNBOOK.md`, `r2-storage.md`.

## 1) Version-lockstep guard

**Why:** hygiene pass fixed 3.12.0-vs-3.13.0 drift; the guard prevents recurrence.

* `VERSION`, `pyproject.toml:project.version`, latest `CHANGELOG.md` `## [x.y.z]`
  must match.  `tests/unit/test_version_command.py::test_versions_in_lockstep_on_repo_root`
  is the single source of truth — CI `lint` job runs it with `pytest -k lockstep`.

**Release bump procedure**

1. Edit `VERSION` (e.g. `3.14.0`).
2. Edit `pyproject.toml` `version = "3.14.0"`.
3. Add `## [3.14.0] — YYYY-MM-DD` heading to `CHANGELOG.md` (above `## [Unreleased]`).
4. Run `pytest -q tests/unit/test_version_command.py -k lockstep` locally — must be green.
5. `pre-commit run --all-files` (ruff + mypy) and `pytest -q` (full suite) before push.

**Failure mode:** lockstep test prints `version lockstep drift: VERSION=... pyproject=... CHANGELOG latest=...`.
Fix by bumping the lagging file.

## 2) Delivery signing (ed25519)

**Env:** `NEXUS_SIGNING_KEY` — 64 hex chars (32-byte seed).  Generate:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

* Empty/missing → fail-closed `SigningError` with actionable hint (never silent).
* Sign: `sign_manifest(dict|bytes)` → base64.  Verify: `verify_manifest(dict|bytes, sig)` → `SigningError` on tamper.
* PyNaCl is optional — without it the module falls back to HMAC-SHA256 over the
  same key + same canonical JSON (sorted keys, compact separators, UTF-8).
  Every test is green without PyNaCl; with PyNaCl the Ed25519 path is exercised
  transparently (no code change).

**Rotation:** set new `NEXUS_SIGNING_KEY` in env, restart workers, re-sign manifests.
Old signatures verify only against the old key — consumers must pick the matching
public value (derived from the same seed).

## 3) Storage resilience

* `NEXUS_STORAGE_MAX_RETRIES` (default 3), `NEXUS_STORAGE_BACKOFF_BASE_MS` (200),
  `NEXUS_STORAGE_BACKOFF_MAX_MS` (5000) — tune without code change.
* `retry_with_backoff` is idempotent-only — `idempotent=False` disables retries
  for non-idempotent writes.
* `idempotency_key(user_id, object_key, content?)` is `sha256` hex — include it
  as a header/metadata so a retried upload never duplicates.
* Secret scrubbing: `redact_secrets` strips `api_key/secret/token/password` values
  and any 64-hex signing key before the message reaches structlog (defence in depth).

**Chaos test:** `tests/unit/test_storage_resilience.py` drives a fake transport
that fails N times then succeeds — green without any cloud.

## 4) Memory eval harness

* Fixture: 6 memories + 4 queries in `memory/eval.py` (deterministic, offline).
* `evaluate_long_term_recall(k=3)` → `recall@k`; committed baseline `0.75`,
  tolerance `0.15` — `tests/unit/test_memory_recall.py` fails when recall regresses.
* Architecture guard: `tests/architecture/test_memory_boundaries.py` asserts
  `memory/` never imports `features/` or `bot/` (AST scan).

## 5) Creative surface

* New file `bot/creative_surface.py` — never touches `bot/handlers.py`.
* Commands: `/edit trim|speed|reverse`, `/caption transcribe|burnin`,
  `/grade lut|exposure|proxy|otio`.  Limits mirror slideshow (`30 s` / typed
  `CreativeFailure` → i18n key).  Every command enqueues via `JobQueuePort`
  (`creative_render`) and replies with `Queued … job <id>` or a typed error.
* Wire in `bot/app.py` alongside `feature_handlers` (one shared `JobQueuePort`
  instance).  No new dependency.

## 6) Bench gates

* `scripts/bench_render.py` (IR compile) + `scripts/bench_caption.py` (SRT format)
  — pure, deterministic, CPU-only.
* Baselines: `tests/bench/baseline_{render,caption}.json` (committed).
* CI gate: `pytest -q tests/bench/test_render_bench.py` (50 % slack in unit test;
  real gate is `scripts/bench_render.py --baseline ... --check` with 15 % threshold).

## 7) Operations

* **Smoke E2E:** `scripts/smoke_e2e.py` (healthz + fake Bot API one-flow) — see
  `docs/ops/DEPLOY_RUNBOOK.md` for the full preflight → deploy → smoke → rollback.

## 8) Incident: secret leak

If a signing key or R2 credential appears in logs:

1. Rotate the credential immediately (new env value, restart).
2. Search logs: `grep -r "\[REDACTED\]"` should hide the value — if a raw
   64-hex appears, the `redact_secrets` path missed it; file a follow-up.

---

*Owner:* agent G (wave-4 hardening).  Next steps 7–10 (pack coverage, ops
runbook, e2e, opgap) are queued in `.agents/board.json → ten_forward_tasks_wave4`.
