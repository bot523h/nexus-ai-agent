# COMPOSITION RUNTIME — canonical integration guide

Operator-facing guide for submitting a capability composition through the
**canonical** NEXUS chain. Contract: [`../architecture/COMPOSITION_RUNTIME.md`](../architecture/COMPOSITION_RUNTIME.md).
Boundary: [`../architecture/adr/0014-creative-capability-composition.md`](../architecture/adr/0014-creative-capability-composition.md).

---

## 1. What you may and may not do

| You may | You may not |
|---|---|
| submit a typed `CompositionPlan` to the canonical queue | define a capability of your own |
| pin `capability_id`, `capability_version`, `operation_schema_version` | skip the compiler's live-registry resolution |
| rely on the canonical A/B/C/D ladder and the actor grant | pass a permission, a grant or a policy of your own |
| rely on the canonical queue, worker registry and verifier | open a second queue, a second bus or a second registry |
| rely on the canonical FFmpeg allow-list | spawn a binary of your own |

---

## 2. The submission

### 2.1 The row

```python
payload = {
    "plan": {...},  # the durable CompositionPlan
    "external_inputs": {
        "src": {"path": "<abs path>", "sha256": "<hex or sha256:…>"},
    },
    "workspace_dir": "<abs path>",  # inside CREATIVE_TEMP_DIR, named creative_*
    "idempotency_key": "<durable key>",
    "actor": {"kind": "user", "actor_id": "<principal>"},  # REQUIRED
    "confirmed": False,  # the requester's own level-C confirmation; never invented
}
```

`actor` is **required**. The worker binds the canonical grant to that principal; a row
without an identity is refused at validation, so no row can execute under a
self-granted service identity. `confirmed` defaults to `False`: a level-C operation
(`media.image_to_video`) is refused *before* any media work unless the requester said so.

`workspace_dir` must satisfy the canonical workspace law
(`creative/render_jobs.py::trusted_creative_root` + `WORKSPACE_PREFIX`):

* it must live under the configured `CREATIVE_TEMP_DIR`;
* its **name** must start with `creative_`;
* the node itself must not be a symlink (the worker checks this itself — the
  queue row is durable and the two ends run in different processes).

`external_inputs[*].sha256` is re-measured by the worker; a mismatch is
refused **before** any media work.

### 2.2 The plan

```python
{
    "plan_version": 1,
    "nodes": [
        {
            "node_id": "resize",
            "operation": "media.resize_image",
            "capability_id": "media.resize_image",
            "capability_version": "1.0.0",
            "operation_schema_version": 1,
            "params": {"width": 1280, "height": 720},
            "bindings": {"source": {"external": "src"}},
        },
        {
            "node_id": "gray",
            "operation": "media.grayscale_image",
            "capability_id": "media.grayscale_image",
            "capability_version": "1.0.0",
            "operation_schema_version": 1,
            "params": {},
            "bindings": {"source": {"node_output": ["resize", "resized"]}},
        },
        {
            "node_id": "video",
            "operation": "media.image_to_video",
            "capability_id": "media.image_to_video",
            "capability_version": "1.0.0",
            "operation_schema_version": 1,
            "params": {
                "width": 1280,
                "height": 720,
                "duration_us": 1000000,
                "fps": 25,
                "encoder": "libx264",
            },
            "bindings": {"source": {"node_output": ["gray", "grayscale"]}},
        },
    ],
    "external_inputs": [{"name": "src", "media_kind": "image"}],
    "expected_outputs": [{"name": "final", "node": "video", "output_port": "video"}],
}
```

The pins are **not** optional bookkeeping: the worker re-compiles the plan
against the live registry, and a plan whose pins no longer match is refused
with `policy_refused` / `unknown_capability` rather than executed against
whatever the registry now offers.

### 2.3 The submission itself

```python
job_id = await queue.enqueue(
    job_type="creative_composition",
    idempotency_key=payload["idempotency_key"],
    payload=payload,
)
```

The handler is the one registered in `worker.py::default_job_handlers()`; the
verifier is the one registered in
`adapters/in_process_job_queue.py::default_artifact_verifiers()`. Nothing
else has to be wired.

---

## 3. The result

On success the row carries the canonical artifact-claim dialect plus the
composition evidence:

| Key | Meaning |
|---|---|
| `artifact_path` | the produced file |
| `artifact_kind` | `video` or `image` |
| `sha256` / `size_bytes` | the recorded identity |
| `duration_us` | the measured duration |
| `operation` | the last node's canonical operation |
| `output_asset_id` | the derived asset id on the canonical project |
| `plan_fingerprint` / `registry_fingerprint` | the reproducibility record |
| `integrity_verified` / `semantic_verified` | the two verification layers |
| `semantic_measurements` | the independently measured properties |
| `lineage` | the ordered `node:operation@version` chain |
| `evidence` | the full proof-carrying artifact |
| `resource_estimate` | the declared cost |
| `artifact_verification` | the queue's own verification block |

On failure the row carries a typed `error_code`:

| Code | Meaning |
|---|---|
| `invalid_request` | the row, the workspace or a staged input is not admissible |
| `unknown_capability` | the plan names an operation the live registry does not have |
| `policy_refused` | the plan is refused (a drifted pin, a bad binding, a wrong type) |
| `execution_failed` | a node failed (encoder error, timeout, size ceiling, path escape) |
| `verification_failed` | the artifact does not match its media contract |

---

## 4. The passport

A composition job is an ordinary queue row, so the **existing** passport
authority projects it:

```python
from nexus_ai_agent.provenance import CausalJournal, PassportBuilder, QueueLedgerObserver

journal = CausalJournal("jobs.sqlite3.causal.sqlite3")
queue = InProcessJobQueue("jobs.sqlite3", causal_observer=QueueLedgerObserver(journal))
passport = PassportBuilder(journal, queue).build(job_id)
```

The passport re-measures the artifact's bytes; a tampered artifact flips the
status to `COMPROMISED`. The `timeline.trim`-specific durable passport
(`jobs/creative_passport.py::build_passport`) does **not** apply to a
composition row — it is the creative-request passport, and a composition is
not a trim request.

---

## 5. Operating notes

| Note | Detail |
|---|---|
| FFmpeg | resolved by `resolve_ffmpeg_bin()`: explicit override → `NEXUS_FFMPEG_BIN` → `PATH` → the `imageio-ffmpeg` wheel. The binary must be absolute. |
| Environment | the media subprocess inherits an allow-list only. `LD_LIBRARY_PATH` is excluded on purpose. |
| Disk | every node writes into its own scratch directory inside the job workspace; the workspace cleanup belongs to the queue. |
| Concurrency | a composition executes its nodes **one at a time**; the peak RAM is the largest node, not the sum. |
| Timeouts | the per-node timeout is the wall-clock bound of the encoder process; a timeout kills the process **group**. |
| Idempotency | re-enqueueing the same key with the same payload returns the same row; a changed payload under the same key is a conflict. |

---

## 6. Troubleshooting

| Symptom | Likely cause | Where to look |
|---|---|---|
| `invalid_request` with "workspace outside creative temp dir" | the workspace is not under `CREATIVE_TEMP_DIR` | the worker's `trusted_workspace` |
| `invalid_request` with "workspace name missing reserved prefix" | the directory is not named `creative_*` | the worker's `trusted_workspace` |
| `invalid_request` with "staged input digest mismatch" | the file changed after admission | the worker's `guarded_input` |
| `unknown_capability` | the plan names an operation the live registry does not have | `build_runtime_registry().list_operations()` |
| `policy_refused` with "registry drift" | the plan was compiled against another registry | `plan.registry_fingerprint` |
| `execution_failed` with "does not match its media contract" | the produced media is not what the plan promised | the node's `semantic.findings` in the receipt |
| `verification_failed` | the exposed artifact failed the semantic pass | `semantic_measurements` in the result |
