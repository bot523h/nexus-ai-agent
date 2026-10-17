# 08 — Module inventory and dispositions (Phase 8)

Recomputed at head `7fd4112` against `origin/main@6b39856` with `scripts/module_inventory.py` (same method as the closeout package; heuristic limits documented there and unchanged: a module is paired only with same-commit tests that import or name it; package-level imports under-count references).

| Metric | Closeout (`4cf8bbc`) | Now (`7fd4112`) |
|---|---|---|
| files inventoried | 594 | 599 |
| production modules | 283 | 284 (+`execution/asset_library.py`) |
| tests / scripts | 277 / 24 | 279 / 26 (+`test_agent_board_ref.py`, `test_asset_library.py`; +`pr_census.py`, `board_merge_preflight.py`) |
| REMEDIATED_WITH_TESTS | 34 | 36 |
| VERIFIED / VERIFIED_WITH_LIMITATIONS | 21 / 39 | 21 / 39 |
| CHANGED_UNREVIEWED | 50 | 50 |
| DEFERRED | 139 | 138 |
| production modules (>5 LOC) with no test reference | 35 | 35 |

## Dispositions (by class, each row in `module_inventory.csv` carries its own)
* **CHANGED_UNREVIEWED (50):** every one is inside commits whose full suite, mutation jobs and `mypy --strict` passed on exact heads (runs 38777822652, 38796133745). Disposition: `COVERED_BY_GATES_NOT_BY_DEDICATED_TEST`; the 10 largest by LOC are the NEXT review queue (`sort -t, -k loc`).
* **DEFERRED (138):** unchanged on this branch; 50 are `__init__`/≤10-LOC modules (`loc` column); 114 have at least one test file referencing them (`test_files_referencing>0`) but no Mission-2 verdict; the rest are the untested list below. Disposition: `OUT_OF_SCOPE_THIS_MANDATE`, ordered by `test_files_referencing==0` first.
* **Untested (35, list in the JSON):** notably `creative/spine/*` (referenced through the package import — heuristic under-count; `tests/unit/test_creative_studio.py` exercises it), `nagar/cognition/*`, `storage/providers/{huggingface,mega,rclone}` (network providers — behaviour tests need recorded fixtures), `bot/*_handlers.py` (Telegram surfaces). Rule kept: behaviour tests through real entry points, never import-only tests.

Reachability from composition roots (`cli.py`, `worker.py`, `bot/handlers.py`, `api/*`): unchanged from the closeout analysis; no module was deleted or added to the roots tonight except the Asset Library, which is **not yet reachable** from any root (by design until wired — see `04_…`).
