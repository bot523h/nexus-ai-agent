# Execution Fabric (provider-neutral execution boundary)

> **One line.** The Execution Fabric hands an already-authorised, already-fenced
> Nexus request to an external workflow engine (today: Hatchet) and returns the
> engine's **raw** staged witness. The engine executes; it never mints Nexus
> authority.

The fabric exists so that the *where/how a job runs* (in-process today, an
external workflow engine tomorrow) can change without moving the *who may run
it, whether it committed, and what evidence proves it*. It is the mechanical
expression of the split between **execution** and **authority**.

## 1. Why a boundary at all

The canonical chain already owns every authority-bearing act (`docs/DECISION_LOG.md`,
`docs/architecture/JOB_LIFECYCLE.md`, law **R14**): the SQLite job-queue row is
the job-state and outcome authority, attempt history and verified creative
passports are queue-row checkpoints, and provenance is a post-commit observer.
An external engine must therefore sit **strictly below** that line: it may run a
step and hand back a witness, and nothing more.

Putting the engine behind a port does three things:

* it keeps provider SDK types out of the domain, application, and authority layers;
* it makes "the provider must not set a Nexus authority field" a *tested* law,
  not a convention; and
* it lets a second provider (or an in-process provider) slot in behind the same
  contract without touching the authority path.

## 2. The contract

`src/nexus_ai_agent/integrations/execution/contract.py` is provider-neutral and
imports no provider:

| Type | Role |
|---|---|
| `ExecutionIdentity` | The Nexus identity of one attempt: `request_id`, `idempotency_key`, `job_id`, `try_id`, `fencing_token`, `backend`, and an evidence-only `provider_run_id`. |
| `ExecutionAttempt` | A bounded description of the attempt (`provider_attempt`) that is **metadata**, never an authority key. |
| `ExecutionRequest` | The canonical, already-fenced request handed to a backend (`identity`, `operation`, `payload`, `staging_dir`). |
| `ExecutionResult` | Raw staged output plus a `witness`; `is_success_claim` is a **claim for the verifier**, not a commit. |
| `ExecutionFailure` | A typed, fail-closed failure (`code`, `detail`). |

`ExecutionIdentity.identity_matches` ignores `provider_run_id` and
`provider_attempt` **on purpose**: an engine's internal retry counter can never
be mistaken for a Nexus try or fencing epoch.

## 3. The port and the in-process provider

`src/nexus_ai_agent/integrations/execution/backends.py` defines the port
(`ExecutionBackend`) and an `InProcessBackend` that simply runs the canonical
handler locally. The in-process backend never verifies, publishes, or touches an
authority module either — it is a *baseline* provider, not a bypass.

`src/nexus_ai_agent/integrations/execution/staging.py` owns the only file-system
seam: it resolves an attempt's staging directory under a base, so a witness's
artifact path can be checked for containment (rule 10) before it is trusted.

## 4. The Hatchet provider adapter

`src/nexus_ai_agent/integrations/hatchet/`:

| Module | Responsibility |
|---|---|
| `_sdk.py` | The **only** module that imports `hatchet_sdk`. Refuses the embedded engine unless the explicit `NEXUS_HATCHET_ALLOW_EMBEDDED` dev flag is set (`HatchetEmbeddedForbidden`). |
| `worker_task.py` | Builds the workflow (`FabricInput` + one `run-canonical` step). The step dispatches to the **canonical** repo handler (`worker.default_job_handlers`), normalises the handler's artifact fields, and returns a witness. It re-implements no handler behaviour. |
| `adapter.py` | `HatchetExecutionAdapter` (a backend): triggers the canonical request, awaits the result under a bounded timeout, and maps the raw output through `reconcile_provider_output` — with **no** verification and **no** authority. |

### What the adapter deliberately does **not** do

* It does not authorise, verify, publish, or record lineage — those stay on the
  authoritative side of the fence.
* It does not treat a provider retry as a Nexus try, nor an engine "success" as
  a committed result.
* A missing/unknown/uncertain provider result maps to `UNKNOWN` (fail closed),
  never to a success claim.

`reconcile_provider_output(identity, raw, request)` is the whole result seam. Its
law: an absent witness is `UNKNOWN`; an explicit falsy witness is `FAILED`; a
truthy witness is a success *claim* transported upward for the verifier. The
provider's own retry count is recorded verbatim in `attempt.provider_attempt`
and never overwrites the identity.

## 5. Identity separation (the load-bearing table)

| Field | Authority | May the provider change it? |
|---|---|---|
| `request_id` | Nexus | No |
| `idempotency_key` | Nexus | No |
| `job_id` | Nexus queue | No |
| `try_id` | Nexus queue | No |
| `fencing_token` | Nexus queue | No |
| `backend` | Nexus | No |
| `provider_run_id` | provider | Yes — **evidence only**, ignored by identity matching |
| `provider_attempt` | provider | Yes — **metadata only** |

## 6. Enforcement

| Law | Enforcing test |
|---|---|
| Contract/port modules import no provider; the fabric imports no authority module | `tests/architecture/test_execution_fabric_boundary.py` |
| Contract identity, retry-vs-fencing mapping, and fail-closed reconciliation | `tests/unit/test_execution_contract.py` |
| Adapter maps a provider refusal to a typed error and a stale/unknown result to fail-closed | `tests/integration/test_hatchet_adapter.py` |
| Embedded engine is refused without the dev flag and is never reachable from product code | `tests/architecture/test_execution_fabric_boundary.py`, `_sdk.py` guard |
| Real end-to-end execution through the embedded engine returns a single fencing identity and a witness (dev/CI proof) | `tests/integration/test_hatchet_embedded_e2e.py` (gated by `NEXUS_HATCHET_E2E=1`) |

## 7. Honest limits

* The Hatchet SDK (`hatchet-sdk`) is an **optional runtime dependency**, not a
  declared packaging extra (the extras matrix is owned by another zone). Installing
  it is the operator's action; the adapter fails with a typed guard when it is absent.
* The embedded engine is a **dev/CI proof only**. Product entrypoints never import
  it; the guard is enforced at import time.
* The end-to-end test is a **reproducible local proof**, not a CI gate; the CI
  default skips it. Reproduce it with:
  `NEXUS_HATCHET_E2E=1 python tests/integration/_hatchet_embedded_e2e.py`
  (expects a `RESULT_JSON=` line with `state=succeeded`, `is_claim=true`, one
  `fencing_token`, and a non-null `witness.artifact_sha256`).
