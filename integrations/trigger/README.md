# NEXUS ⇄ Trigger.dev — thin execution boundary

This directory is the **adapter**, not the domain. It exists so NEXUS can hand a
*decided* intent to an external background runner and receive back a **raw
witness**. It owns:

- no authority (NEXUS mints the intent id and the idempotency key),
- no evidence, no lineage, no passport (NEXUS produces these),
- no business logic (the task echoes what it was given).

Trigger.dev owns: scheduling, retries, concurrency, run lifecycle, the worker
and observability. That division is the whole point of the boundary.

## Layout

```
trigger.config.ts              # project ref, runtime node-24, dirs=[./src/trigger]
src/trigger/nexus-boundary.ts  # the two tasks (nexus-boundary, nexus-retry-proof)
.env                           # TRIGGER_SECRET_KEY (dev) — gitignored, never committed
```

## The boundary contract

```
NEXUS core (decided intent · idempotency key · verifier · evidence · passport · lineage)
      │  enqueue only
      ▼
Trigger.dev (schedule · retry · concurrency · run lifecycle · worker · observability)
      ▼
background execution  ──►  raw witness  ──►  NEXUS verifier signs it
```

Rules:

1. NEXUS decides the idempotency key and passes it in `options.idempotencyKey`.
2. The boundary returns a **raw witness** (`{intent, attempt, ranAt, …}`); it never
   asserts a verdict. NEXUS's verifier is the only thing that signs.
3. No NEXUS business logic is duplicated inside a task.

## Run it

```bash
cd integrations/trigger
npm install                     # if node_modules is absent
npx trigger.dev@latest login    # once, per machine
npx trigger.dev@latest dev      # start the local worker

# in another shell, with TRIGGER_SECRET_KEY from .env:
curl -s -X POST https://api.trigger.dev/api/v1/tasks/nexus-boundary/trigger \
  -H "Authorization: Bearer $TRIGGER_SECRET_KEY" -H 'Content-Type: application/json' \
  -d '{"payload":{"intent":"demo/1","data":{"k":"v"}}}'
```

Inspect runs: `npx trigger.dev@latest runs list -e dev` and
`runs get <run_id> -e dev`, or the dashboard at
`https://cloud.trigger.dev/projects/v3/proj_wvkvtuiomwzvperhokmn`.

## Proven in development (2026-10-08)

| task | run id | status | attempts | witness |
|---|---|---|---|---|
| `nexus-boundary` | `run_06ghmacj0vf7q5lu1trgoj7e01` | COMPLETED | 1 | `{"ok":true,"witness":{"intent":"proof/intent-001","attempt":1,"environment":"DEVELOPMENT"}}` |
| `nexus-retry-proof` | `run_06ghma3huj54712npeu76qvn01` | COMPLETED | 2 | `{"ok":true,"attempt":2,"failedPreviously":true}` (attempt 1 threw) |
| `nexus-boundary` (idempotent) | `run_06ghmac6tcjm4g8j26qc30dc01` | COMPLETED | 1 | two triggers with `idempotencyKey=nexus-idem-proof-001` → **same run id** |

All runs executed on dev worker version `20261008.2`, runtime `node-24`.
