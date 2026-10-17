# 02 — Independent audit of PR #218 (269 files, +39 211 / −1 784)

Re-done from the diff, not from the PR description. Commands are reproducible from
the repository root; the base is `main` = `6b398565`, the head audited is
`8a733a77c7ac35f9a6cc40b7a7cc80a89193f300` plus the commits this package adds.

## Commit sequence and authorship

```
$ git log --oneline --format="%h %ad %an | %s" --date=short 6b398565..HEAD
8a733a7 bot523h       Evidence package: record CI 38806486544 (bd8aab5, 21/21) and the wiring commit
fae25fc bot523h       Phase 4: wire the Asset Library into archive_trim_evidence (CAS-backed, immutable attempt links)
bd8aab5 bot523h       Overnight stabilization evidence package + scheduled dependency audit workflow
7fd4112 bot523h       Phase 4: content-addressed Asset Library …
5bc146e bot523h       A-04: claim board moves to coordination ref refs/nexus/board (ADR 0012) + PR census
4cf8bbc bot523h       docs(closeout): evidence package for PR #218 + reproducible module inventory
7bfd628 … a357b0f     arena-agent  fix(audit …): A-01…A-19 remediation (18 commits)
2c2ebaf arena-agent   board(task-270): owner-mandated full remediation lease
```

24 commits: 18 remediation commits from the earlier sessions, 5 from last night's
stabilization, plus this package. Authorship is preserved (no history was rewritten;
no force-push was used at any point).

## File inventory by area

| Kind | Count | Notes |
|---|---|---|
| added | 78 | 63 unit tests, 16 integration/architecture tests, 12 scripts, the Asset Library, 2 migrations, the audit packages |
| modified | 176 | production modules, docs, `ci.yml`, `maintenance.yml` |
| deleted | 9 | the A-10 orphan removal (`agent/feedback.py`, `agents/chat_agent.py`, `agents/planner_agent.py`, `core/async_db.py`, `domain/retention.py`, `i18n/loader.py`, `memory/short_term.py`, `storage/model_fetch.py`, `adapters/caption_unavailable.py`) — each with a reachability law test (`tests/architecture/test_no_orphan_modules.py`) |
| renamed | 6 | root-level audit reports moved to `docs/audits/`, content byte-identical (`R100`) |

Largest additions are machine-readable evidence (the two inventories), not code.

## Security review

* **Secrets**: `git diff 6b398565..HEAD | grep -E '^\+.*(api_key|secret|password|token|private key|aws_)'`
  over 46 727 diff lines returns exactly one hit — `telegram_bot_token="CHANGE_ME"` in a test
  (a placeholder). No credential, token or connection string is added.
* **Workflows** gained least privilege (`permissions: contents: read`), a `push:`
  filter narrowed to `main` + `v*` tags, explicit `timeout-minutes` on every job,
  and SHA-pinned actions. No workflow gains write scopes, no `pull_request_target`,
  no `--admin`/`--force` in any step.
* **New migrations** are additive and dialect-guarded (`b5d8e2f4a917` is a no-op on
  SQLite, PostgreSQL-only `INTEGER → BIGINT` for the 33 Telegram-id columns, with
  the column list kept in lockstep with the ORM by `tests/unit/test_telegram_id_width.py`;
  `c7a1e5d2b9f3` adds the conversation-history table). Single head, single schema
  authority (`storage/migration_metadata.py` excludes reflected foreign tables so
  autogenerate can never propose dropping LangGraph's tables — finding A-19).
* **Fail-closed surfaces**: the dashboard API now answers `503` when no token is
  configured (A-06); the Studio gate reuses that principal and returns the same
  `404` for "unknown" and "unauthorized" projects (no existence oracle —
  now pinned by `tests/unit/test_studio_api_scope.py`).

## Test-quality review (the important part)

Deleting or weakening a test to go green would have been the easiest way to make
this PR look finished. Mechanically checked instead:

* 30 `assert` lines are removed across the whole diff, 621 added. Each removed line
  was matched against the additions in the same file:
  `python - <<'PY'` … normalising `# noqa`/comments … → 21 survivors, all read by hand:
  `assert X == {…}` → `assert {…} == X` (ruff-ratchet style), `assert heads == ["7c2f9d41e8a3"]`
  → the new head `["c7a1e5d2b9f3"]`, `assert [hit.doc_id …][0] == "d1"` → `next(...)`,
  `assert calls == []` → `assert steps == []` (renamed variable, same assertion), and
  `test_dashboard_api` replacing "works without a token" with a *stronger* full-body
  assertion under a token. No assertion was dropped without an equal-or-stronger
  replacement.
* **No** `pytest.skip`/`xfail` was added without an environment reason: every added
  skip marker is environment-gated — `requires PostgreSQL` (×2), `needs procfs`,
  `FIFOs are POSIX-only` (×3). The suite still reports **37 skipped**, the same
  number as the `main` baseline (CI run on `4cf8bbc`: 4103 passed / 37 skipped).
* Every new test asserts observable behaviour (a file on disk, a recorded Bot API
  call, a raised typed error, a database row), never the presence of a symbol.

## The two mechanisms this PR exists for

**Asset Library (CAS) wiring** — `archive_trim_evidence` admits both evidence files
into `AssetLibrary(<artifact_root>)` and publishes the unchanged attempt keys as
hard links to the blob:

* identity returned to the passport is the library's measured `sha256:<hex>` and size,
  on top of the pre-existing "archived bytes must equal measured source/render" check;
* dedup across attempts is proven by inode identity and `st_nlink ≥ 3`;
* `AssetTooLargeError` keeps the passport's own typed error and leaks no staging file;
* an occupied key with different bytes fails closed; `EXDEV`/`EPERM` falls back to
  the previous verified streaming copy (test patches `os.link` to fail only outside
  `cas/`, so the library's own publish path stays real);
* consequence: archived evidence is `0o444`. Three tests that *simulate corruption*
  by writing to an archived file now `chmod(0o644)` first — the tamper-detection
  assertions are untouched, and the change is disclosed rather than hidden.

**Board authority (A-04)** — `scripts/agent_board.py` stores claims in
`refs/nexus/board` through `git push --force-with-lease` (compare-and-swap, ≤3
retries), `check` reads the ref only, an unreadable authority exits 2, and CI runs a
`guard` step against a frozen tree hash. This session read the real ref
(`b1a5fac`, one active claim, generation 3) and the tests (`36` ref-board tests)
exercise stale claims, simultaneous claims, expired leases, malicious
self-exclusion, incompatible generations and bypass attempts. What remains open is
the **production proof**: a real foreign claim through the ref after merge — until
that exists the remedy is `REMEDIED_CI_VERIFIED_PENDING_MERGE`, not "verified".

## Documentation / generated-file agreement

`docs/audits/**/module_inventory.json` are generated, so they are re-derived here
rather than trusted: `scripts/module_inventory.py` was re-run on `8a733a7`
(599 → 601 files: two new migrations + the new workflow), and the numbers in
[`03_MODULE_DISPOSITION.md`](03_MODULE_DISPOSITION.md) come from that run. The
inventory still records the *history* of a finding; the new disposition file
records its *current* state, which is why both are shipped.
