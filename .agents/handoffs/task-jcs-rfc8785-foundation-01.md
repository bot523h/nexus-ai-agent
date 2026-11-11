# Handoff — task-jcs-rfc8785-foundation-01

RFC 8785 (JCS) foundation with legacy hash integrity proof.

| Field | Value |
|---|---|
| Task | `task-jcs-rfc8785-foundation-01` |
| Zone | `provenance-jcs-foundation-01` |
| Branch | `nagar-jcs-rfc8785-foundation-01` |
| Base (`main`) | `5fe9451228ad6e4d5ac067aaa4cbd2df8c633cd4` |
| Generation | 1 (claim at 2026-11-11) |
| Implementation head | `a6dd22c739147b63a4ba5518316387a8f3767eb7` |
| Status | implemented, gates run locally; PR open, **not merged** |

## Completed milestones

1. **Additive JCS API** — `src/nexus_ai_agent/provenance/jcs.py` exposes
   `canonical_bytes(value) -> bytes`, `canonical_text(value) -> str` and
   `jcs_digest(value) -> "sha256:<hex>"`, re-exported from
   `nexus_ai_agent.provenance`. The digest is `sha256` over exactly the bytes
   returned by `canonical_bytes`.
2. **Fail-closed JSON-value boundary** — an exact-type gate (`type(x) is ...`)
   refuses tuples, sets, `bytes`, `Decimal`, `IntEnum`, `str`/`dict` subclasses
   and non-string object keys **before** the library runs (the library would
   silently coerce several of these). Cycles are refused too. Non-finite floats,
   unsafe integers and lone surrogates are refused by the library and translated
   to the same typed `JcsError`.
3. **Lazy dependency import** — `import rfc8785` happens inside the call, so the
   `--no-deps` CI lanes (mutation jobs) still import
   `nexus_ai_agent.provenance` and get a typed `JcsError` only on use.
4. **Dependency declared and pinned** — `rfc8785==0.1.4` in `pyproject.toml`
   (see the supply-chain record below).
5. **Frozen legacy contract** — `provenance.models.canonical_json` / `digest_of`
   / `sha256_hex` and the IBPC re-exports are unchanged; pre-change goldens are
   pinned by literals in `tests/unit/test_provenance_legacy_hash_compatibility.py`.
6. **Isolation guard** — `tests/architecture/test_jcs_legacy_isolation.py`
   (AST-based) proves only `provenance/jcs.py` and `provenance/__init__.py`
   reference the JCS surface, and that no frozen legacy module does.

## Files

| Path | Change |
|---|---|
| `src/nexus_ai_agent/provenance/jcs.py` | new — JCS API + fail-closed boundary |
| `src/nexus_ai_agent/provenance/__init__.py` | additive re-export |
| `pyproject.toml` | `rfc8785==0.1.4` pin |
| `tests/unit/test_provenance_jcs.py` | new — RFC vectors, boundary, digest |
| `tests/unit/test_provenance_legacy_hash_compatibility.py` | new — frozen goldens |
| `tests/architecture/test_jcs_legacy_isolation.py` | new — isolation guard |
| `.agents/handoffs/task-jcs-rfc8785-foundation-01.md` | this file |

## Commands and observed results

See the PR body for the exact commands, their observed output and the SHA each
was run against. Summary of the local run:

* `pytest -q tests/unit/test_provenance_jcs.py tests/unit/test_provenance_legacy_hash_compatibility.py tests/architecture/test_jcs_legacy_isolation.py` → **48 passed**
* `pytest -q tests/architecture/ tests/unit/test_agent_board.py tests/unit/test_docs_integrity.py` → **611 passed**
* `ruff check .` and `ruff format --check .` → clean on the changed files
* `mypy src` → no error in any changed file (pre-existing `import-not-found`
  errors for optional deps absent from the minimal local install)
* `deptry src` → no finding for `rfc8785` / the changed files
* `python scripts/agent_board.py check --files <changed> --branch ...` → exit 0

## Supply-chain record (acceptance criterion 5)

* **Package**: `rfc8785` — a pure-Python implementation of RFC 8785 (JCS).
* **Pin**: `==0.1.4` (exact). **RFC basis**: RFC 8785 (JSON Canonicalization
  Scheme), §3.2.2.3 number serialization, §3.2.3 sorting.
* **License**: Apache-2.0 (`License :: OSI Approved :: Apache Software License`).
* **Maintainer**: Trail of Bits (`opensource@trailofbits.com`,
  `github.com/trailofbits/rfc8785.py`). `Development Status :: 4 - Beta`.
* **Python range**: `Requires-Python: >=3.8` (project requires `>=3.10`).
* **Runtime dependencies**: none (extras only).
* **Supply-chain limitations**: single-maintainer, beta-status, small package —
  hence the exact pin and the `dependency-audit` gate. No `uv.lock` is added
  (main has no lockfile contract; `pyproject.toml` is the authority).

## Limitations / NOT_PROVEN

* **NOT_PROVEN**: the full `make test` suite was **not** run in this session
  (30-minute budget); only the focused suites above were run. CI runs the full
  suite on the PR head.
* **NOT_PROVEN**: the PostgreSQL / extras-matrix lanes were not run locally.
* No caller was migrated to JCS (by design). `pyproject.toml` overlaps open
  PR #193 (stale) and PR #307 (dependency update); neither branch was modified.

## Precise next step

Open (already open) the PR for `nagar-jcs-rfc8785-foundation-01` against `main`,
drive CI green, resolve every review thread, then release the board claim.
Do **not** wire JCS into any legacy call site.
