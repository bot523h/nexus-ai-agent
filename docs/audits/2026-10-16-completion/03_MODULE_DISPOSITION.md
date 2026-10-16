# 03 — 284/284 production modules, one current disposition each

The overnight package left **188** production modules in `DEFERRED` (138) or
`CHANGED_UNREVIEWED` (50) and recorded **35** without a test reference. This
document closes that: every production module now carries a disposition derived
from the tree, and the six buckets sum to 284 with **zero** modules deferred.

## How a row is produced (reproducible)

```bash
python scripts/module_inventory.py               # what exists, what changed (input)
    --base 6b398565cc1ce0c51fac77d7623350b299c3938f \
    --prior docs/audits/2026-10-15-closeout/mission2_module_status_6b398565.csv \
    --remediation docs/audits/2026-10-15-overnight-stabilization/remediation_map.json \
    --out docs/audits/2026-10-16-completion/module_inventory
python scripts/module_disposition.py             # one row per production module
    --inventory docs/audits/2026-10-16-completion/module_inventory.json \
    --base 6b398565cc1ce0c51fac77d7623350b299c3938f \
    --review docs/audits/2026-10-16-completion/review_records.json \
    --out docs/audits/2026-10-16-completion/module_disposition
```

Inputs the new script adds, and why each is weaker than it looks:

| Column | Meaning | Honest limitation |
|---|---|---|
| `change_class` | `UNCHANGED`, `ANNOTATION_ONLY` (AST identical after stripping annotations, docstrings and `from __future__ import annotations`), `BEHAVIOUR_CHANGED_RECORDED`, `…_UNRECORDED`, `NEW_MODULE[_RECORDED]` | the AST comparison proves *shape* equality, not semantic equivalence of an unchanged shape; a value-only edit is caught, an equal-shape rewrite is not |
| `reachable` / `reachable_from` | module is in the import closure of the five composition roots (`bot/app.py`, `worker.py`, `cli.py`, `api/app.py`, `adapters/in_process_job_queue.py`), resolved with relative-import and re-export handling | a module reached only through `importlib` with a computed name is invisible to it; the repository's own reachability law (`tests/architecture/test_no_orphan_modules.py`) remains the authority for "is this reachable at all" |
| `tests_referencing` / `tests_dynamic_only` | test files that import the module or a name it defines (submodule-through-`__init__` included); the second column counts files that only name it as a string next to `importlib` | a test that exercises a module *indirectly* (through a caller) counts as zero — the column is "direct reference", never "coverage" |
| `risk_flags` | capability markers read from the module text (process-exec, dynamic-eval, network, filesystem-mutation, credentials, concurrency, sql, symlink, ffmpeg, persistence) | a marker is a prompt for review, not a finding |
| `disposition` | the verdict, with its evidence string | see the bucket rules below |

Bucket rules (each row in `module_disposition.csv` carries the evidence string):

1. `REMEDIATED_WITH_TESTS` — the diff is named by a remediation record or a review
   record that cites the finding **and** the regression test.
2. `REVIEWED_NON_BEHAVIOURAL` — the diff was read line by line and cannot alter
   runtime behaviour (annotations, `ClassVar`, `Generic` parameters, `noqa`
   removal, `Decimal(0)` for `Decimal("0")`, `re.DOTALL|IGNORECASE` for `re.S|re.I`).
3. `REVIEWED_WITH_NEW_TESTS` — the module received its **first** behaviour test
   during this audit (13 modules; see [`04_DEFECTS_AND_FIXES.md`](04_DEFECTS_AND_FIXES.md)).
4. `VERIFIED` / `VERIFIED_WITH_LIMITATIONS` — unchanged against the base and carried
   from the mission-2 pass that reviewed the module individually; the limitation is
   whatever that pass recorded.
5. `REVIEWED_NO_DEFECT_FOUND` — behaviour unchanged (or reviewed and confirmed
   intentional) with at least a direct test reference.

## The table

| Disposition | Modules |
|---|---|
| `REMEDIATED_WITH_TESTS` | 54 |
| `REVIEWED_NO_DEFECT_FOUND` | 126 |
| `VERIFIED_WITH_LIMITATIONS` | 39 |
| `REVIEWED_NON_BEHAVIOURAL` | 31 |
| `VERIFIED` | 21 |
| `REVIEWED_WITH_NEW_TESTS` | 13 |
| **total** | **284** (0 deferred, 0 unassessed) |

Other measured facts from the same run:

* production modules reachable from a composition root: **227 / 284**;
* modules with **no** direct test reference after this audit: **2** —
  `creative/image_post.py` (remediated under A-12 and exercised by
  `tests/unit/test_async_offload.py`, which patches the module's blocking calls
  rather than importing it) and `llm/local_llama_cpp.py` (needs the `llama-cpp`
  extra, which is not installed in CI) — both named with residual risk in the CSV;
* change classes: `UNCHANGED` 197, `ANNOTATION_ONLY` 7,
  `BEHAVIOUR_CHANGED_RECORDED` 31, `BEHAVIOUR_CHANGED_UNRECORDED` 45 → **every one
  resolved by a review record** (they remain 45 in the column because the column
  records what the machine saw, not what the reviewer concluded — the `disposition`
  column carries the conclusion), `NEW_MODULE_RECORDED` 4: `execution/asset_library.py`
  and `storage/clock.py` (A-13 clock contract), `bot/surface/moderation.py` and
  `bot/surface/viral.py` (A-03 wired surfaces).

## What the review of the 45 unrecorded diffs found

Every one was read as a diff (496 changed lines in total) and classified in
[`review_records.json`](review_records.json):

| Group | Files | Verdict |
|---|---|---|
| A-02 — the per-module `create_engine(f"sqlite:///{db_path}")` disappears in favour of `storage.db.sync_engine` | 8 feature modules + `bot/feature_handlers.py` | recorded, tests `test_sync_engine_resolution.py` (which resolves the modules by name through `importlib`) |
| A-12 — blocking file work moved off the event loop | `creative/image_post.py`, `bot/slideshow_notify.py`, `worker.py`, `storage/ai_storage_manager.py`, `storage/unified_cloud.py`, `features/speech.py` | recorded, tests `test_async_offload.py` |
| A-06 — dashboard fails closed, page size bounded | `api/dashboard.py` | recorded, tests `test_dashboard_api.py` |
| A-19 — `include_object` hook for reflected foreign tables | `storage/migration_metadata.py` | recorded, tests `test_migration_metadata.py` |
| CAS wiring of the passport archive | `jobs/creative_passport.py` | recorded as `P4-CAS`, tests `test_creative_passport.py` |
| A-13/A-14 ratchet work (annotations, `ClassVar`, `noqa`, literal spellings, an import path) | 23 modules | `REVIEWED_NON_BEHAVIOURAL` |
| `agents/store/agent_manager.py` | 1 | `session.exec`/`naive_utcnow` contract, recorded, `test_time_contract.py` |
| `llm/local_llama_cpp.py` | 1 | real normalisation (embedding ints → floats) — reviewed, **no test**, residual risk recorded |
| `agent/self_monitor.py`, `storage/checkpoint_lifecycle_pg_store.py` | 2 | lazy `%s` logging and a `list()` around `fetchall()`; reviewed, no defect found |

No unrecorded diff was waved through: where the change had a real effect on
behaviour, it is either linked to an existing regression test or recorded with the
gap that remains (the two rows above).

## Modules that remain a registered risk

| Module | Why | Next action (owner: this branch / next session) |
|---|---|---|
| `llm/local_llama_cpp.py` | optional extra, no test, embedding type normalised | a test with a stub embedding session asserting the float contract — acceptance: `pytest tests/unit/test_local_llama_cpp.py` fails on the pre-audit coercion |
| `creative/image_post.py` | no direct test reference; A-12 remediation only | a behaviour test around `generate_image_post` with a fake provider — acceptance: temp file removed on failure, no `unlink` on the loop |
| `features/{analytics,engagement,personality,anonymous_chat,ads,games,gamification}.py` | exercised through the dynamic engine-resolution test, not through their own public methods | per-feature behaviour tests over a `migrated_sqlite` fixture — acceptance: each public method's row is asserted in the database |

Everything else in this list is tracked inside `module_disposition.csv` with a
`next_action` and an `acceptance` column, so the next session can pick a row up
without re-deriving the context.
