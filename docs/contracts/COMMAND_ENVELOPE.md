# Command Envelope — Canonical Typed Command

> **Owner:** Agent 2  
> **Location:** `src/nexus_ai_agent/creative/contracts/command_envelope.py`  
> **Version:** `nagar.command.v2` (canonical), backward compatible with `nagar.command.v1`  
> **Baseline:** `035a896dd2ed1293de6accf2ef4309da2fd64c89`

## Why not raw LLM output?

* LLM must not build shell command, control UI directly, assume uninstalled capability, send private asset to cloud, or claim "implemented" merely because name exists.
* Typed envelope is the **only** input the studio accepts.

## Fields — Necessity / Consumer / Validation / Security / Test

| Field | Necessity | Consumer | Validation | Security Impact | Test |
|---|---|---|---|---|---|
| command_id | idempotency, audit | bus, history | min_length=1, unique | prevents replay confusion | test_idempotency |
| operation_id | what to execute | registry | must exist in registry | prevents arbitrary code | test_unknown_operation |
| capability_id | which pack provides it | pack registry | must match operation's pack | prevents pack confusion | test_unavailable_capability |
| envelope_version | version negotiation | bus, validation | must be known (v1/v2) | prevents downgrade | test_schema_mismatch |
| schema_version | operation schema evolution | validation | semver | prevents breaking change | test_schema_mismatch |
| project_id | scoping | bus, storage | must be valid | prevents cross-project access | test_authorization |
| base_revision / base_state_hash | optimistic concurrency | bus | must match current | prevents lost updates | test_revision_conflict |
| actor / principal | who | auth gate | principal_id required | prevents escalation | test_authorization |
| authorization context | confirmation | permission gate | explicit bool | prevents C-level bypass | test_authorization |
| input | operation params | handler | Pydantic per op, extra=forbid | no shell, no code | test_schema_mismatch |
| target | where | handler | no path traversal | prevents file escape | test_schema_mismatch |
| moment_range | when | resolver | timecode >=0, range ordered | prevents VFR drift | test_schema_mismatch |
| policy_context | local/cloud | adapter | locality consistency | prevents silent egress | test_locality_violation |
| idempotency_key | exactly-once | bus | deterministic | prevents double apply | test_idempotency |
| dry_run / preview_intent | safe preview | bus | bool | prevents destructive surprise | test_preview |
| provenance / trace | audit | observability | trace_id present | no PII leak | test_provenance |

No field added merely for "beautiful architecture" — each has necessity, consumer, validation, security, test.

## Schema (Pydantic)

```python
class CommandEnvelope:
    command_id: str
    operation_id: str
    capability_id: str | None
    envelope_version: Literal["nagar.command.v1", "nagar.command.v2"] = "nagar.command.v2"
    schema_version: str = "1.0.0"
    project_id: str | None
    base_revision: int | None
    base_state_hash: str | None
    actor: ActorContext
    authorization: AuthorizationContext
    input: dict[str, Any]
    target: TargetRef
    moment_range: MomentRange | None
    policy_context: PolicyContext
    idempotency_key: str | None
    dry_run: bool
    preview_intent: bool
    provenance: Provenance
```

Supporting contexts:

* **ActorContext** — principal_id, actor_type (user/agent/system), roles
* **AuthorizationContext** — confirmed, confirmation_token, permission_level
* **PolicyContext** — locality (LOCAL_ONLY/PREFER_LOCAL/CLOUD_ALLOWED/EXPLICIT_CLOUD), allow_cloud, require_preview, reversible_only
* **TargetRef** — project_id, timeline_id, track_id, clip_id, asset_id, subject_id, extra
* **MomentRange** — timecode_us, start_us, end_us, frame_number, captured_at_command
* **Provenance** — trace_id, session_id, parent_command_id, llm_model, confidence

## Versioning Strategy

* **Envelope version:** `nagar.command.v2` canonical, `v1` still accepted for backward compat. From architecture: `src/nexus_ai_agent/creative/studio/models.py` PROTOCOL_VERSION = `nagar.command.v1`; new envelope extends it.
* **Operation schema version:** per operation input model, e.g. `timeline.trim` schema 1.0.0
* **Capability contract version:** `CapabilityContract.version`, e.g. `1.0.0`

Single versioning system — no parallel versioning.

## Authorization Boundary (Enforced Order)

```
LLM output
  ↓ Parse
  ↓ Schema validation
  ↓ Capability existence
  ↓ Authorization
  ↓ Policy (locality, reversibility, confirmation)
  ↓ Resource / locality checks
  ↓ Idempotency
  ↓ Command Bus
  ↓ Execution
```

If part of chain missing: recorded as GAP, not fake impl.

## Locality / Privacy

* LOCAL_ONLY — private media must not silently go to cloud
* PREFER_LOCAL — local preferred
* CLOUD_ALLOWED — cloud allowed
* EXPLICIT_CLOUD — requires explicit consent

Product rule enforced in `PolicyContext` validator: LOCAL_ONLY cannot have allow_cloud=True, EXPLICIT_CLOUD requires allow_cloud=True.

## Reversibility

* reversible — undo via revision+snapshot
* non_reversible — destructive
* preview_required — must preview
* review_required — needs human

No AI operation may guarantee destructive action merely because command is valid.

## Idempotency

* command_id — unique per command
* idempotency_key — deterministic, e.g. `split-p1-clip01-12500000`
* base_revision — optimistic concurrency

Duplicate command with same key + same payload → replay cached result (bus). Same key + different payload → rejected (queue checks payload type+content).

## Example

```json
{
  "command_id": "cmd_split_01",
  "operation_id": "timeline.split_at_playhead",
  "capability_id": "nexus.edit.timeline",
  "envelope_version": "nagar.command.v2",
  "schema_version": "1.0.0",
  "project_id": "p1",
  "base_revision": 41,
  "base_state_hash": "sha256:timeline-before",
  "actor": {"principal_id": "user_123", "actor_type": "user"},
  "authorization": {"confirmed": false},
  "input": {"at": {"timecode_us": 12500000, "captured_at_command": true}},
  "target": {"track_id": "video_01", "clip_id": "clip_01"},
  "moment_range": {"timecode_us": 12500000, "captured_at_command": true},
  "policy_context": {"locality": "LOCAL_ONLY", "allow_cloud": false},
  "idempotency_key": "split-p1-clip01-12500000",
  "dry_run": false,
  "preview_intent": false,
  "provenance": {"trace_id": "tr_abc123", "session_id": "session_01"}
}
```

## Validation Functions

* `validate_envelope()` — pure, no I/O, raises typed errors
* Errors: `UnknownOperationError`, `UnavailableCapabilityError`, `AuthorizationError`, `LocalityViolationError`, `RevisionConflictError`

## Tests (Gate 2 A-J)

* A — registry ↔ product reconciliation
* B — unknown operation → reject
* C — unavailable capability → reject
* D — schema mismatch → reject
* E — authorization → reject without principal
* F — locality violation → reject
* G — idempotency deterministic
* H — revision conflict → reject
* I — T20 identity canonical
* J — 70 reconciliation drift detectable

## Vertical Slice Readiness

Contract supports future flow:

```
T01 Import
 ↓
T02 Transcribe
 ↓
T22 Remove Filler (future)
 ↓
T31 Generate Subtitles (caption.generate_srt)
 ↓
T64 Export (delivery.render_master_4k)
```

Each step's contract/readiness is in matrix, not fake impl.
