# Examples: knowledge retrieval CLI (copy-paste, offline)

Real commands against the shipped CLI (the outputs below are the actual shapes the commands
print; ids/sha values are illustrative). No API keys, no network: the default hash embedder
is pure arithmetic. See [`../architecture/KNOWLEDGE_RETRIEVAL.md`](../architecture/KNOWLEDGE_RETRIEVAL.md)
for semantics and [`../ops/KNOWLEDGE_RETRIEVAL_RUNBOOK.md`](../ops/KNOWLEDGE_RETRIEVAL_RUNBOOK.md)
for procedures.

## 1. Point the subsystem at an allowlisted root

```bash
export NEXUS_KNOWLEDGE_SOURCE_ROOTS="$HOME/nexus-kb"          # must exist
export NEXUS_KNOWLEDGE_INDEX_PATH="$HOME/.nexus/knowledge_index"
nexus migrate                                                  # creates knowledgesource
```

## 2. Index (incremental, idempotent)

```console
$ nexus knowledge index --json
{
  "run_id": "6f0c1a2b9d3e4f50",
  "scanned": 3,
  "ingested": 3,
  "new": 3,
  "modified": 0,
  "unchanged": 0,
  "removed": 0,
  "unsupported": 0,
  "rejected": 1,
  "failed": 0,
  "chunks_written": 7,
  "truncated": false,
  "errors": [".env: secret"],
  "duration_s": 0.041,
  "rebuilt": false
}
```

`rejected` counts refused paths (secrets, symlinks, traversal, oversize); `errors` is capped
and never contains file content. Re-running immediately reports `unchanged: 3`.

## 3. Search with verified evidence

```console
$ nexus knowledge search "rollback previous image" --json
{
  "query": "rollback previous image",
  "state": "ok",
  "dropped_stale": 0,
  "filtered_unrelated": 0,
  "index_generation": 0,
  "evidence": [
    {
      "source_id": "3b1f…",
      "path": "runbook.md",
      "root": "/home/user/nexus-kb",
      "sha256": "9c2e…",
      "chunk_index": 1,
      "start": 121,
      "end": 486,
      "score": 0.412,
      "page": null,
      "excerpt": "Step two: redeploy the previous immutable image tag…",
      "citation": "runbook.md@9c2e41f0a7b3#1"
    }
  ]
}
```

Human output:

```console
$ nexus knowledge search "rollback previous image"
[0.4120] runbook.md @ 9c2e41f0a7b3 #chunk 1 (121:486)
    Step two: redeploy the previous immutable image tag…
```

Defined empty states (exit code `0`, empty `evidence`):

```console
$ nexus knowledge search "   " --json | jq .state
"empty_query"
$ nexus knowledge search "xylophone quantum" --json | jq .state
"no_matches"
```

`dropped_stale > 0` means registry sha256 mismatches were refused (stale content is never
served); `filtered_unrelated > 0` means nearest-neighbour hits without shared distinctive
vocabulary were discarded.

## 4. Status and inventory

```console
$ nexus knowledge status --json | jq '{degraded, degraded_reason, source_count, node_count, generation}'
{
  "degraded": false,
  "degraded_reason": null,
  "source_count": 3,
  "node_count": 7,
  "generation": 0
}

$ nexus knowledge sources list --json | jq '.[0]'
{
  "source_id": "3b1f…",
  "root": "/home/user/nexus-kb",
  "path": "runbook.md",
  "sha256": "9c2e…",
  "size_bytes": 1441,
  "status": "ingested",
  "chunks": 3,
  "indexed_at": "2026-10-23T20:11:04+00:00",
  "updated_at": "2026-10-23T20:11:04+00:00",
  "last_error": null
}
```

## 5. Remove one source / rebuild everything

```console
$ nexus knowledge sources remove runbook.md
✓ unregistered runbook.md

$ nexus knowledge sources remove nope.md
error[not_found]: no registered source matches 'nope.md'

$ nexus knowledge rebuild --yes --json | jq '{rebuilt, ingested}'
{
  "rebuilt": true,
  "ingested": 2
}
# without --yes the command prompts for confirmation first (fail-safe)
```

## 6. Fail-closed examples

```console
$ NEXUS_KNOWLEDGE_ENABLED=false nexus knowledge index
error[disabled]: knowledge retrieval is disabled
  hint: set NEXUS_KNOWLEDGE_ENABLED=true to operate the subsystem

$ NEXUS_KNOWLEDGE_SOURCE_ROOTS= nexus knowledge index
error[misconfigured]: knowledge retrieval is not configured
  hint: set NEXUS_KNOWLEDGE_SOURCE_ROOTS to at least one existing directory
```

Both exit `1` with no stack trace; `nexus knowledge status --json` still answers honestly in
both cases.
