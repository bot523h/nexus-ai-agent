# Nagar command & pack contracts — reference

Sources: `docs/architecture/COMMAND_CAPABILITY_CONTRACT.md`, `docs/architecture/MODULE_MAP.md` §3,
`docs/architecture/CREATIVE_STUDIO.md`, `docs/architecture/adr/0005`, `adr/0006`.

## Command identity

| Field | Meaning | Authority |
|---|---|---|
| `command_id` | transport identity of one attempt; rotates on redelivery | client-generated |
| `operation` | `domain.verb` (e.g. `timeline.mark`) | must exist in the `CapabilityRegistry` |
| `capability_id` | capability owning the operation | derived from the registry; a client snapshot is advisory |
| `idempotency_key` | logical identity of the intended effect | client-generated; scoped by `(project_id, operation, key)` |

## Versioning

| Version | Value | Rule |
|---|---|---|
| external protocol id | `nagar.command.v1` | fixed; any other value (including `v2`) refused at parse |
| envelope `schema_version` | `1` (legacy) or `2` (canonical) | unknown refused; `2` requires `actor`, `target.project_id`, `provenance` |
| `operation_schema_version` | integer `>= 1`, default `1` | must equal the registered `OperationSpec.schema_version` |
| capability `version` | `MAJOR.MINOR.PATCH` | snapshot major must equal installed major and not be newer |

Schema 1 (the shape every in-process call site emits) parses and dispatches unchanged; schema 2 adds
required claims. No silent reinterpretation.

## Error taxonomy

| Error | Stage |
|---|---|
| `CommandValidationError` | 1–2 (malformed envelope, unknown version, bad input) |
| `UnknownOperationError` | 2 |
| `AuthorizationError` | 3–4 (untrusted actor/project/grant, missing permissions) |
| `UnknownCapabilityError` / `CapabilityError` / `CapabilityVersionError` | 4 |
| `ExecutionPolicyError` | 5 (unadvertised mode or A/B/C/D denial) |
| `InputReferenceError` / `ReferenceResolutionError` | 6 |
| `IdempotencyConflictError` | 7 (same key, different payload) |
| `PreconditionError` | 8 (stale revision/hash) |
| `CommandExecutionError` | 9 (handler failure, nested dispatch, identity change) |

## Idempotency formulas (proven by the contract suite)

- same project + operation + key + payload = same logical result (same `transaction_id`, no second
  handler run), even after the project advances;
- same key + different payload = deterministic `IdempotencyConflictError`, state untouched;
- the key scope includes project and operation — reuse across them is independent;
- `command_id`, `trace_id`, and the advisory `capability_snapshot` may rotate without changing the
  logical payload, but the snapshot is re-verified every attempt.

Fingerprint: canonical JSON (sorted keys, no whitespace, `allow_nan` refused) over the envelope minus
the three transport fields, SHA-256. The reservation store is per-bus in-memory; cross-process and
post-restart replay is NOT VERIFIED.

## Boundary laws and their tests

| # | Law | Enforcing test |
|---|---|---|
| R6 | studio core imports only stdlib + pydantic + itself; default registry is exactly Wave-1 | `test_nagar_studio_isolation.py` |
| R7 | each pack imports only stdlib/pydantic/studio/packs; no `subprocess`/`socket`/`ctypes`/`shutil`/`os.system`; manifests have zero executable keys | `test_pack_manifest_is_data_only.py`, `test_{edit,motion,audio,delivery}_pack_boundary.py`, `test_caption_substrate_boundary.py` |
| R8 | declared capabilities == operations actually registered; pack activates against its own manifest | `test_slideshow_adapter_boundary.py` |
| R9 | render lane is lean: import allow-list, exactly one `subprocess` site (`rendering/executor.py`), never `shell=True`, packs never spawn | `test_rendering_lane_boundary.py`, `test_slideshow_adapter_boundary.py::test_only_the_render_lane_spawns_a_process` |
| R13 | one canonical command contract; only the bus calls a handler; `TypedCommand` is the single envelope; `v2` banned | `test_command_capability_boundary.py`, `test_command_capability_contract.py` |
| R14 | the SQLite `InProcessJobQueue` row is the sole job-state/outcome authority; passports are queue-row checkpoints; checkpoint reuse re-reads/re-verifies bytes | `test_provenance_queue_recording.py`, `test_creative_execution_recovery.py`, `test_creative_passport.py` |

## Registration is fail-closed

`CapabilityRegistry` refuses: duplicate operations, cross-domain ids, non-`forbid` input models,
unknown execution modes, invalid semver, and conflicting version/availability for one capability.

## Live reconciliation

`test_live_runtime_registry_reconciles_every_operation` derives the operation set from the builders
(never a hardcoded list) and checks it against the contract. Two registries exist:
`build_wave1_registry()` (5 ops, pack-free — use it in tests that must not import a pack) and
`build_runtime_registry()` (activates packs).

## Permission ladder

A `project:read` baseline; B `project:write` (e.g. `timeline.trim`); C needs `confirmed`; D always
denied. The effective permission set also includes declared extras; the grant must cover them.
