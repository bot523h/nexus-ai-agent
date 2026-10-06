# Docs & contract governance — reference

Sources: `docs/README.md`, `docs/architecture/TESTING.md` §4, `docs/architecture/MODULE_MAP.md` §3,
`docs/architecture/adr/0001`, `adr/0002`, `adr/0004`, `scripts/check_version_lockstep.py`.

## What `tests/unit/test_docs_integrity.py` asserts

| Assertion | Why |
|---|---|
| every file under `docs/` is indexed in `docs/README.md` | an unindexed document is a document nobody reads |
| every relative link inside `docs/` resolves to a real file | dead links turn documentation into fiction |
| every `mermaid` fence is balanced, non-empty, and starts with a known diagram type | catches empty/copy-paste diagrams |
| no `TODO`/`FIXME`/placeholder tokens in architecture docs | architecture pages are claims, not notes |
| `VERSION` == `pyproject.toml` == latest `CHANGELOG` heading | the release-drift guard (task-111) |

Note: the gate scans `docs/` only. Files elsewhere in the repo (e.g. `.agents/skills/`) are not
indexed by it, but a relative link *inside* `docs/` must still resolve.

## ADR vs DECISION_LOG

| Question | Where |
|---|---|
| Does it change **system behaviour**? | `docs/DECISION_LOG.md` |
| Is it about the **documentation/board layer**? | `docs/architecture/adr/NNNN-slug.md` |

The ADR template is MADR 4.0-lite (`docs/architecture/adr/template.md`); the **Confirmation**
section is mandatory and names the test or command that keeps the decision true. ADRs are numbered
and listed in `docs/architecture/adr/README.md` + `docs/README.md`.

## The rule → test register (MODULE_MAP §3)

| # | Law (short) | Test |
|---|---|---|
| R1 | `domain/`/`application/ports/` never import adapters; frozen baseline | `test_import_boundaries.py` |
| R1b | `provenance/` observes only; owns no execution path | `test_provenance_boundary.py` |
| R2 | `langgraph`/`sqlmodel`/`telegram` confined to roots + sanctioned adapters | `test_import_boundaries.py` + `legacy_baseline.json` |
| R3 | raw LangGraph savers only in `storage/langgraph_checkpoint.py` + `adapters/langgraph/` | `test_saver_boundary.py` |
| R4 | no Celery/Redis; compose is exactly `{bot, dashboard}` | `test_modular_monolith.py` |
| R5 | ports keep typed surface; destructive ops need an idempotency key | `test_port_signatures.py` |
| R6 | studio core imports only stdlib+pydantic+itself; default registry = Wave-1 | `test_nagar_studio_isolation.py` |
| R7 | pack import allow-list; no process/socket/os; manifests data-only | `test_pack_manifest_is_data_only.py` + per-pack gates |
| R8 | declared capabilities == registered operations; pack activates against its manifest | `test_slideshow_adapter_boundary.py` |
| R9 | render lane lean; exactly one `subprocess` site; never `shell=True` | `test_rendering_lane_boundary.py` |
| R10 | image gen never imports bot/storage, directly or dynamically | `test_image_gen_boundary.py` |
| R11 | domain glossary + retention constants stay live | `test_glossary_liveness.py` |
| R12 | `bot/surface/` importable without `telegram`; every stub-replaced command resolves | `test_surface_onboarding.py`, `test_surface_ptb.py`, `test_surface_registration.py` |
| R13 | one canonical command contract; only the bus calls a handler; `v2` banned | `test_command_capability_boundary.py`, `test_command_capability_contract.py` |
| R14 | SQLite queue row is the sole job-state authority; passports are checkpoints | `test_provenance_queue_recording.py`, `test_creative_execution_recovery.py`, `test_creative_passport.py` |
| R15 | every law in this table names a *live* guard (file + optional `::symbol` resolve) | `test_module_map_law_coverage.py` |
| R16 | `.agents/skills/` stays discoverable + consistent (index, name==dir, description, refs, exec bit) | `test_agent_skills_contract.py` |

## Release lockstep chain

```
VERSION  ==  pyproject.toml [project].version  ==  latest CHANGELOG.md heading
```

Guarded by `scripts/check_version_lockstep.py`, `tests/unit/test_version_lockstep.py`, and the
docs-integrity version assertion. `release-lineage` CI job additionally checks tags on full history.

## i18n parity

`src/nexus_ai_agent/i18n/locales/` holds 15 locales × 79 keys. `tests/unit/test_i18n_parity.py`
fails on any missing key. Add new keys to every locale in the same PR.

## Living vs dated

| Directory | Nature | Rule |
|---|---|---|
| `docs/architecture/` | living view | update in the same PR as the code that changed it |
| `docs/architecture/adr/` | dated decisions | immutable once accepted; supersede, do not rewrite |
| `docs/audits/` | dated records | immutable; never a source of truth |
| `docs/history/` | archived | read-only; never a source of truth |
| `docs/DECISION_LOG.md` | authoritative | wins on any disagreement |

## Docs-as-code toolchain decision

ADR 0002 deliberately keeps docs enforcement in Python (`test_docs_integrity.py`) and defers the
Node/Chromium toolchain (Vale, markdownlint-cli2, mermaid-lint, Lychee) to a recorded trigger:
`docs/` growing past ~30 files. Do not add that toolchain without re-opening the ADR.
