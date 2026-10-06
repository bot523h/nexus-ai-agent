# Gate map — reference

Source: `docs/architecture/TESTING.md`, `.github/workflows/ci.yml`, `Makefile`.

## Gates and CI jobs

| Gate | Command | CI job | Blocks merge |
|---|---|---|---|
| Lint | `ruff check .` | `lint` | yes |
| Format | `ruff format --check .` | `lint` | yes |
| Types | `mypy src` | `lint` | yes |
| Tests | `pytest -q -m "not slow"` | `test` | yes |
| Migrations on real PostgreSQL | `nexus migrate` ×2 + 3 contract suites + head assertion | `migrate-postgres` (`pgvector/pgvector:pg16`) | yes |
| Optional-extras install matrix | one leg per shipping extra + `core` | `extras-matrix` | yes |
| Python parity | the `test` selection on 3.10 / 3.11 / 3.12 | `python-parity` | yes |
| Release lineage | tags + `VERSION` + lockstep on full history | `release-lineage` | yes |

## Test taxonomy

| Layer | May touch | Speed | Purpose |
|---|---|---|---|
| `tests/architecture/` | source text only (AST + JSON, no production imports) | ms | structural boundary laws |
| `tests/unit/` | pure modules; fakes for LLM/providers; SQLite `tmp_path` | ms–s | behaviour, contracts, failure vocabularies |
| `tests/integration/` | the queue, savers, Alembic, a real FFmpeg encode | s–min | wiring unit tests cannot prove |

Determinism rules: no network in unit tests; time injected or frozen; golden files updated only by a
human-run command (`nexus golden update`); real binaries where "it worked" would be a guess.

## Known environmental-only failures

| Symptom | Cause | Fix |
|---|---|---|
| 4 migration-race tests fail (subprocess `python -m nexus_ai_agent.cli`) | package not installed | `pip install -e . --no-deps` |
| `test_version_command::test_running_version_matches_installed_distribution` fails | no distribution metadata | same |
| `ModuleNotFoundError: chromadb / sentence_transformers / llama_cpp` | heavy optional dep absent | expected; covered by fail-closed unit tests, not the default job |
| `ModuleNotFoundError: tests` / `scripts` in CI | a test imported an unpackaged namespace | move the helper into `conftest.py` or the installed package |
| pytest collection error "import file mismatch" | duplicate test basename | rename one file |

## Optional extras

`pyproject.toml` ships `pdf` (`pypdf`), `speech` (`faster-whisper`), `translate` (`argostranslate`),
and `dev` (pytest/ruff/mypy/pypdf/imageio-ffmpeg). The `extras-matrix` CI job installs one leg per
extra plus `core` and runs `tests/unit/test_optional_extras.py`. `[local-llm]` never existed as an
extra (recorded in `TESTING.md` §7).

## The pack-coverage contract (a trap for new pack tests)

`tests/unit/test_pack_coverage_contract.py::test_no_pack_test_module_is_left_out_of_the_evidence`
fails if any new test file imports `nexus_ai_agent.creative.packs.<pack>` without being either a
canonical target in `PACK_TEST_TARGETS` or listed in `HOST_LAYER_PACK_IMPORTERS`
(`src/nexus_ai_agent/continuum/pack_coverage.py:92`). Classify honestly — do not weaken the list.
Use the pack-free `build_wave1_registry()` in tests that do not need a real pack to avoid the edit.

## Gate commands for specific change types

```bash
# a pure pack operation
python -m pytest -q tests/unit/test_<pack>_pack.py tests/architecture/test_<pack>_pack_boundary.py

# a port or adapter
python -m pytest -q tests/unit/test_<port>.py tests/integration/<port>_contract.py

# a boundary law
python -m pytest -q tests/architecture/

# the agent skills under .agents/skills/ (index, frontmatter name, refs, exec bit)
python -m pytest -q tests/architecture/test_agent_skills_contract.py

# documentation
python -m pytest -q tests/unit/test_docs_integrity.py

# a release
python scripts/check_version_lockstep.py     # VERSION == pyproject == CHANGELOG head
```

## Reproducing the numbers in the docs

`docs/architecture/TESTING.md` §6 lists the commands that regenerate every count quoted in the
architecture docs (file/line inventory, `nexus packs list`, migration chain, i18n parity, docs
gates). Numbers in documentation must be reproducible from the tree.
