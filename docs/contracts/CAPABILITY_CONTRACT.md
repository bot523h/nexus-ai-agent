# Capability Contract — Canonical

> **Owner:** Agent 2  
> **Location:** `src/nexus_ai_agent/creative/contracts/capability.py`  
> **Version:** 1.0.0  
> **Evidence:** TDD + live `build_runtime_registry()` 57 ops

## Concept

```
Capability
    ↓
Operation
    ↓
Input Schema
    ↓
Policy
    ↓
Execution Adapter
```

Capability is **not** just a string registry; it carries policy, locality, permissions, availability.

## Schema

```python
class CapabilityContract:
    capability_id: str               # e.g. nexus.edit.timeline
    version: str                     # capability contract version
    display_name: str
    supported_operations: tuple[str] # operation IDs
    schema_versions: tuple[str]      # envelope versions supported
    execution_class: ExecutionClass  # pure_reducer | render_lane | analysis_onnx | native_worker | external_binary
    locality_policy: LocalityPolicy  # LOCAL_ONLY | PREFER_LOCAL | CLOUD_ALLOWED | EXPLICIT_CLOUD
    permission_requirement: PermissionRequirement  # A/B/C/D
    required_packs: tuple[str]
    supported_targets: tuple[str]
    preview_available: bool
    reversibility: Reversibility     # reversible | non_reversible | preview_required | review_required
    idempotency_guaranteed: bool
    availability: AvailabilityState  # AVAILABLE | PENDING | DEPRECATED | BLOCKED | NOT_IMPLEMENTED
    requires_confirmation: bool
    egress_allowed: bool
    external_binaries: tuple[str]
    evidence_location: str
    notes: str
```

## Locality / Privacy Semantics

* **LOCAL_ONLY** — private media must never leave device. Consumer: execution adapter, SSRF guard. Validation: `allow_cloud=False`. Security: prevents silent egress. Test: `test_locality_violation`.
* **PREFER_LOCAL** — local preferred, cloud fallback allowed with explicit opt-in.
* **CLOUD_ALLOWED** — cloud allowed but not required.
* **EXPLICIT_CLOUD** — requires explicit cloud consent.

Product rule: "private media نباید silently به cloud route شود."

If implementation incomplete: contract level recorded, gap recorded, test boundary created, heavy implementation deferred to next Gate.

## Reversibility

* **reversible** — undo via revision+snapshot
* **non_reversible** — destructive, requires confirmation
* **preview_required** — must preview before commit
* **review_required** — needs human review

No AI operation may guarantee destructive action merely because command is valid.

## Capability List (Canonical 8)

| Capability ID | Ops | Execution | Locality | Availability | Evidence |
|---|---|---|---|---|---|
| nexus.edit.timeline | 10 | pure_reducer | LOCAL_ONLY | AVAILABLE | src/nexus_ai_agent/creative/packs/edit/ |
| nexus.vision.portrait | 10 | analysis_onnx | LOCAL_ONLY | NOT_IMPLEMENTED | MISSING |
| nexus.vision.scene | 10 | analysis_onnx | LOCAL_ONLY | NOT_IMPLEMENTED | MISSING |
| nexus.motion.graphics | 10 | render_lane | LOCAL_ONLY | AVAILABLE | src/.../motion/ |
| nexus.audio.studio | 10 | analysis_onnx | LOCAL_ONLY | AVAILABLE | src/.../audio/ |
| nexus.language.caption | 10 | native_worker | LOCAL_ONLY | AVAILABLE | src/.../caption/ |
| nexus.color.delivery | 10 | render_lane | LOCAL_ONLY | AVAILABLE (7/10 in runtime) | src/.../delivery/ + 3 missing |
| nexus.slideshow.compose | 6 | render_lane | LOCAL_ONLY | AVAILABLE | extra outside 70 |

Total distinct operations from capabilities: 76 (70 TDD + 6 slideshow). Runtime implements 57 of those.

## Validation

* Unknown capability → `UnavailableCapabilityError`
* Capability exists but operation not in its supported_operations → mismatch
* Locality violation → `LocalityViolationError`
* Confirmation required but not provided → authorization gate

## Tests

* `test_unavailable_capability` — known op, unknown capability → reject
* `test_locality_violation` — LOCAL_ONLY vs EXPLICIT_CLOUD → reject
* `test_registry_product_reconciliation` — 70 vs 57 formula

## Policy Boundary

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

If part of chain missing in repo: recorded as GAP, not fake implementation.
