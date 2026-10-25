# Knowledge retrieval runbook: operate, diagnose, recover

Operator procedures for the knowledge-retrieval subsystem (design:
[`../architecture/KNOWLEDGE_RETRIEVAL.md`](../architecture/KNOWLEDGE_RETRIEVAL.md)).
Every command is the shipped CLI; `--json` output is machine-readable, logs stay on stderr.

## 1. Configuration (fail-closed by default)

| Variable | Default | Meaning |
|---|---|---|
| `NEXUS_KNOWLEDGE_ENABLED` | `true` | `false` ⇒ every mutating command exits `error[disabled]` |
| `NEXUS_KNOWLEDGE_SOURCE_ROOTS` | empty | comma-separated or JSON list of allowlisted roots; empty ⇒ `error[misconfigured]` |
| `NEXUS_KNOWLEDGE_INDEX_PATH` | `data/knowledge_index` | derived store (safe to delete) |
| `NEXUS_KNOWLEDGE_EMBEDDING_PROVIDER` | `hash` | `hash` offline; `sentence-transformers` needs a local model dir |
| `NEXUS_KNOWLEDGE_EMBEDDING_MODEL_PATH` | empty | existing directory only — never auto-downloaded |
| `NEXUS_KNOWLEDGE_MAX_FILES`, `..._MAX_FILE_BYTES`, `..._CHUNK_TOKENS`, `..._CHUNK_OVERLAP`, `..._SEARCH_TOP_K` | bounded | input limits; violated limits fail at settings validation |

Schema: `alembic upgrade head` (table `knowledgesource`); `nexus migrate` applies it.

## 2. Routine operations

```bash
nexus knowledge index --json          # incremental; idempotent
nexus knowledge search "rollback runbook" --json
nexus knowledge status --json         # once the service constructs, status never raises —
                                       # degradation is reported honestly; *construction*
                                       # failures (unknown provider, invalid limits, unwritable
                                       # index dir) exit 1 with error[misconfigured]/error[internal]
nexus knowledge sources list --json   # registry: path, sha256, status, chunks
nexus knowledge sources remove path/within/root.md   # unregister one source
nexus knowledge rebuild --yes         # wipe derived store + reindex (generation + 1)
```

Exit contract: `0` for success and defined empty states (`empty_query`, `empty_index`,
`no_matches`); `1` with `error[code]: …` + `hint` on stderr for typed failures; never a
stack trace.

## 3. Reading `status`

| Field | Healthy | Investigate when |
|---|---|---|
| `degraded` / `degraded_reason` | `false` / `null` | see §7 of the architecture doc: `index_store_corrupt` → §4 below; `embedding_model_directory_missing` → prefetch the model; `embedding_model_changed_rebuild_required` → `nexus knowledge rebuild --yes` (embedding space swapped); `index_missing_rebuild_required` → restore files or rebuild; `index_inspect_failed` → filesystem permissions |
| `store_present` | `true` after first index | `false` with `source_count > 0` ⇒ rebuild |
| `error_count` | `0` | per-file failures listed by `sources list` (`status=error`); fix the file and rerun `index` — next run converges |
| `node_count` | observed count of nodes currently in the store | it *decreases* legitimately when a source is unregistered or removed — compare against `source_count`/registry `chunks`, not against history; only rebuild if `store_present` is true but `node_count` is 0 while `ingested_count > 0` |
| `generation` | bumps by 1 on each `rebuild` (monotonic) | unchanged generation after an expected rebuild ⇒ the rebuild never ran |

## 4. Recovery procedures

- **Corrupt store** (search exits `error[index_unavailable]` … "corrupt", status shows
  `index_store_corrupt`): `nexus knowledge rebuild --yes`. Sources and registry are
  untouched; the store is derived.
- **Store directory deleted**: `nexus knowledge index` recreates it; no data loss.
- **Interrupted index run**: rerun `index`. Ingest writes the index first and commits the
  registry second, so a crash converges on the next run; rows in `error` self-heal when the
  file is indexable again.
- **Incomplete scan** (`index --json` shows `scan_complete: false`, non-empty
  `incomplete_roots`, and a `scan incomplete: …` error note): a root was missing,
  unreadable, or failed mid-walk — **nothing was deleted** there (presence unknown, not
  absent; registry rows and index evidence preserved). Restore access/mount and rerun
  `index` to reconcile, or remove the root from `NEXUS_KNOWLEDGE_SOURCE_ROOTS` if it is
  gone for good — dropping the root from config is what licenses stale cleanup for it on
  the next complete run.
- **Source root rotated/renamed**: update `NEXUS_KNOWLEDGE_SOURCE_ROOTS`; the next `index`
  drops registry rows whose files no longer exist (`removed` counter) — evidence for them
  disappears from search immediately.
- **Database restored from backup**: registry and store may disagree on sha256; search
  drops mismatches (`dropped_stale`) instead of serving them — run `rebuild` to resync.
- **Provider/model changed** (status shows `embedding_model_changed_rebuild_required`,
  commands exit `error[rebuild_required]`): the store's vectors belong to the previous
  embedding space — run `nexus knowledge rebuild --yes`; plain `index` deliberately
  refuses rather than mixing spaces.
- **Manifest stamp write fails** (`embedding_model_manifest_write_failed` in the index report):
  the file is not accepted as indexed. Restore write access/space for the derived index path, then
  run `nexus knowledge rebuild --yes` to recreate vectors and their identity together.
- **Config swap to `sentence-transformers`** fails: reason is in status; prefetch §4 of the
  architecture doc, or stay on `hash`.

## 5. Safety notes

- Never point `NEXUS_KNOWLEDGE_SOURCE_ROOTS` at secret material: the scanner rejects
  `.env`, key and PEM patterns, symlinks and traversal attempts (they appear as
  `rejected` in the index report), but allowlisting is the first line of defense.
- Retrieved excerpts are untrusted text: they are evidence, not instructions, and they
  never authorize actions. Redaction applies to logs; excerpts are not logged.
- Deleting `NEXUS_KNOWLEDGE_INDEX_PATH` is always safe (derived state).

## 6. Log events to watch

`knowledge_index_run` (counters: ingested/unchanged/removed/rejected/failed, `duration_s`),
`knowledge_search` (`state`, `dropped_stale`, `filtered_unrelated` — a persistently high
`filtered_unrelated` means queries and corpus share no vocabulary), `knowledge_index_rebuild`,
`knowledge_source_unregistered`.
