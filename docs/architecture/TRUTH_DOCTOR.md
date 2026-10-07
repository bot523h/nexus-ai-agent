# Truth Doctor — one authority for repository truth

The runtime already refuses to lie: verdicts fail closed, refusals are typed, and
every artifact carries a provenance passport. The *repository* had no such
guarantee. A version could drift between four declarations, a document could name
a test that had been renamed, an architecture law could point at a deleted guard,
and none of it failed a check.

The **truth doctor** closes that gap. It is a single, pure-stdlib scanner whose
every finding carries a stable code, a severity and a witness, and it is wired to
a permanent gate so drift becomes a red check instead of silent prose rot.

<!-- truth:file=src/nexus_ai_agent/diagnostics/truth.py symbol=run_doctor -->

## Entry points

| Surface | Command |
|---|---|
| Human report | `python -m nexus_ai_agent.diagnostics.truth` |
| Machine report | `python -m nexus_ai_agent.diagnostics.truth --format json` |
| Gate (CI) | `pytest -q tests/architecture/test_repo_truth_consistency.py` |
| Focused unit tests | `pytest -q tests/unit/test_truth_doctor.py` |

Exit codes: `0` clean at the requested threshold, `1` a finding at or above
`--fail-on` (default `error`), `2` misuse (an illegal `--fail-on`). `--only NAME`
runs a single registered check.

## Registered checks

| Check | Code(s) | What it proves |
|---|---|---|
| `version-lockstep` | `TRUTH001/002/003` | `VERSION` == `pyproject [project].version` == newest released `CHANGELOG` heading == `README` banner == the `continuum.json` release body. A missing declaration, a non-semantic version, or a disagreement is an error naming every value. |
| `docs-index` | `TRUTH010/011` | Every `docs/**/*.md` is indexed in `docs/README.md`, and every relative `.md` link in that index resolves on disk. |
| `law-test-resolution` | `TRUTH020/021/022` | Each boundary law in `docs/architecture/MODULE_MAP.md` §3 names at least one `test_*.py[::symbol]` that exists — the machine form of the AGENTS.md §7 rule. A law with no test, a renamed file, or a deleted symbol is an error. |
| `claim-witnesses` | `TRUTH023/024` | Every `<!-- truth:... -->` / `<!-- truth-absent:... -->` marker in the docs resolves against the tree. |
| `fail-open-defaults` | `TRUTH030` | A truthy-default verdict read (`.get("safe", True)`) in `src/` is a warning, so the silent-success class cannot spread unnoticed. |

## Documentation claim markers

A document can make a machine-checkable claim about the code. Two forms are
recognised:

```markdown
<!-- truth:file=src/nexus_ai_agent/cli.py symbol=run_bot -->
<!-- truth-absent:file=src/nexus_ai_agent/agents/planner_agent.py symbol=real_planner -->
```

`truth:` asserts the file (and, when given, the symbol) **exists**.
`truth-absent:` asserts it does **not** — the direction that keeps a "no fake
feature" claim honest. A symbol is proven present by parsing the file with
`ast`; a syntax error reads as *unknown*, never as *present*. When the code
changes so a claim no longer holds, the gate fails and names the exact
`file:line → path::symbol`.

## Accepted exceptions

`check_fail_open_defaults` reports the truthy-default class but does not fail the
build on two reviewed sites, both recorded in
`src/nexus_ai_agent/diagnostics/truth.py`:

- `orchestration/graph.py` — the read is normalised by `phi.moderate`, which
  itself fails closed, so the default is unreachable.
- `creative/packs/trust.py` — `threshold` is a policy default, not a safety
  verdict.

Any *new* occurrence is a warning, so the class is visible the moment it is
introduced.

## Design contract (law R16)

Enforced by `tests/architecture/test_repo_truth_consistency.py`:

1. the doctor imports only the standard library, so it can run before
   `pip install` (the same constraint as `scripts/check_version_lockstep.py`);
2. the check registry is non-empty and each check is callable;
3. the real repository passes the doctor;
4. the law check is not a no-op — deleting the law table produces `TRUTH020`.

## Honest limits

- The fast-rail CI job (`lint-fast`) does not yet call the doctor directly; the
  gate runs inside the normal test job. Wiring the pre-install step is a
  follow-up, blocked only by the current `ci.yml` conflict surface.
- The claim-marker check only inspects the docs tree plus `README.md` and
  `AGENTS.md`; markers elsewhere are ignored by design.
- `symbol_exists` proves a *definition* exists; it does not prove behaviour.
