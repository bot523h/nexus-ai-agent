
## CLOSURE-04 (2026-11-03) — hermeticity + behavioral proof
- **Subprocess tests that shell out to `sys.executable` are NOT hermetic.** Any
  `importlib.metadata` / `pip check` / `pip config` assertion must run on a fresh
  `python -m venv`, or it silently measures the CI image (real torch present ⇒
  "absent torch" assertions invert; a `--no-deps` job ⇒ ambient `pip check` dirty ⇒
  mutation baseline "not green"). This red-ed `test`, `python-parity`, and
  `trust-mutations` on PR #308.
- A textual guard (grep the script) is not a behavioral guard: mutants that remove
  behavior while leaving the string survive. Execute the real postcondition against
  synthetic `*.dist-info` metadata and run the real installer through a `python` shim.
- `pip --isolated` is required to neutralize `PIP_INDEX_URL`/`PIP_EXTRA_INDEX_URL`/
  `PIP_FIND_LINKS`/`pip.conf`; an explicit `--index-url` alone does NOT (verified pip 26.x).
- Board: `python scripts/check_board_integrity.py` + `tests/unit/test_agent_board.py`
  must both pass after any board edit; a new exclusive path must ALSO be added to its
  zone's `paths` (test_exclusive_paths_belong_to_a_declared_zone).
- Audit docs `docs/audits/NEXUS_OMNI_*.md` are on branch `arena/omni-audit-reports`
  (PR #306), not on the CI-torch branch.
