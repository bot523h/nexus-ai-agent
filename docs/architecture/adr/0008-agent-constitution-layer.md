---
status: accepted
date: 2026-10-05
deciders: arena/01a10ba1-nexus-ai-agent (Nagar persistent engineering-memory layer)
consulted: docs/architecture/adr/0004-board-schema-2.md, AGENTS.md §1 rule 4 (gates owner), docs/architecture/TESTING.md §3 (fitness functions)
informed: every future agent session, coordination zone, docs-architecture zone
---

# 0008. The agent engineering contract is a versioned root constitution, enforced by a test

## Context and Problem Statement

Work on this repository is done by agents in isolated sandboxes that share
nothing but the git tree. Every session therefore starts with **no memory of the
previous one**: the coordination protocol in `AGENTS.md` is re-read each time,
but the engineering law — what counts as verification, what counts as
completion, what an agent may never do — lived nowhere except in the prose of
individual mission prompts and in dated audit reports. That produced three
observed failure shapes:

1. **Context loss.** A session that never saw the previous session's findings
   repeats them, or reverses them, because the findings were never in the tree.
2. **Verification laundering.** "Tests pass", "a PR exists", "CI is green"
   appear as proof without a revision, a run id, or a scope; six different
   facts (`LOCAL BRANCH`, `PR HEAD`, `CI HEAD`, `REVIEWED HEAD`, `MERGED MAIN`,
   `DEPLOYED ARTIFACT`) get conflated.
3. **Unenforceable rules.** `AGENTS.md` is already the coordination contract and
   is deliberately bilingual and dense; adding ten more engineering laws to it
   would either bury coordination or turn the entry contract into a wall of
   text nobody applies.

The docs layer already answers "where does a decision live" (ADR 0001/0002) and
"what must name its enforcer" (`docs/README.md`, "Rules of this map" §3). What
was missing was the *law itself* as a first-class, versioned, enforced artifact.

## Decision Drivers

- **Persistence across sessions.** A future agent with zero memory must be able
  to reconstruct the contract from the repository alone.
- **Enforceability.** A rule that cannot fail a build is a preference. The
  repository's own doctrine: a boundary rule names the test that guards it.
- **Separation of concerns.** Coordination (who owns what, right now) must stay
  in `AGENTS.md` + the board; engineering law (how to work) must live apart, so
  neither file drowns the other.
- **No new authority.** The layer must not override `docs/DECISION_LOG.md`, the
  board, or the single-gates-owner rule.
- **Future-proofing.** Nagar 2/3/4+ must inherit the contract unchanged.

## Considered Options

1. **Mission-prompt only** — restate the rules in every task (status quo).
2. **Grow `AGENTS.md`** — append the engineering laws to the existing contract.
3. **A dated audit report** — record the rules in `docs/audits/`.
4. **A versioned root constitution plus an architecture test** — a root
   `NAGAR_AGENT_CONSTITUTION.md` carrying the laws, the closed completion
   vocabulary, the completion gate and the amendment law, pinned by
   `tests/architecture/test_agent_constitution.py`, with `AGENTS.md` reduced to
   the bootstrap pointer that forces the reading.

## Decision Outcome

Chosen option: **4**.

- Option 1 is the defect: a prompt dies with the session, cannot be enforced,
  and cannot be cited by a reviewer.
- Option 2 buries coordination under law. `AGENTS.md` is the file an agent reads
  to learn *who owns what*; mixing in ten laws makes the ownership rules — the
  ones that prevent zero-collision failures — harder to find. It also makes
  `AGENTS.md` unversioned as a law: edits land there for coordination reasons
  and silently rewrite engineering obligations.
- Option 3 puts a living rule inside an immutable-dated record, which this
  documentation layer explicitly forbids: `docs/audits/` and `docs/history/`
  are dated records, never sources of truth.
- Option 4 gives the law a stable home, a version, an amendment record, and an
  enforcer — and it makes `AGENTS.md` the *only* door, because every agent is
  already instructed to read `AGENTS.md` first.

Concretely:

- `NAGAR_AGENT_CONSTITUTION.md` (root) carries: §0 the unconditional bootstrap
  law (L0); §1 the Nagar north star; §2 the ten non-negotiable laws L1–L10;
  §3 the authority map (source of truth / cache / projection / evidence);
  §4 the closed five-word completion vocabulary; §5 the sixteen-dimension
  completion gate; §6 the mandatory report template; §7 the future-generation
  contract; §8 the amendment (meta-)law; §9 the precedence order; §10 the
  loophole register; §11 a Persian summary; §12 the amendment record.
- `AGENTS.md` gains exactly one new block — **MANDATORY SESSION BOOTSTRAP** —
  placed above its first section, naming the three readings. It does not restate
  the laws; the enforcer fails if it ever does.
- The completion vocabulary is **closed** and pre-existing: `VERIFIED`,
  `VERIFIED_WITH_LIMITATIONS`, `HARDENED_BUT_NOT_COMPLETE`, `BLOCKED`,
  `DEFERRED`. The first two already carry those exact meanings in
  `docs/architecture/PROVENANCE_LEDGER.md`; this record generalises them to
  every mission instead of inventing a parallel vocabulary.
- Gate 13 (exact-head CI) explicitly defers to `AGENTS.md` §1 rule 4: only the
  `gates_owner` runs the shared gates on main-bound work. The constitution adds
  obligations; it transfers no ownership and authorises no CI race.

### Consequences

- (+) A session with no memory reconstructs the whole contract from two files.
- (+) The contract fails the build when it is deleted, moved, softened, buried,
  or contradicted by an entry point that stops pointing at it.
- (+) Reporting becomes comparable across sessions: same vocabulary, same
  report sections, same gate dimensions.
- (−) One more root-level document, and one more test to keep in sync — the
  amendment law (§8.1.3) makes that cost explicit rather than accidental.
- (−) The constitution is a *standard*, not a fact: when code disagrees with it,
  both must be reported (§9.2). It cannot make the code true by asserting it.
- (~) Numbering note: `0007` is reserved in the `job-lifecycle-gate5-closure`
  zone (`docs/architecture/adr/0007-failure-semantics-and-job-outcomes.md`) and
  has not landed; this record takes `0008` so the two never collide.

## Confirmation

- `tests/architecture/test_agent_constitution.py` — stdlib-only, runs in the
  standard `pytest` job: root presence and version pin; all eleven laws under
  their canonical headings; the closed vocabulary and each definition row; all
  sixteen gate dimensions; the traceability chain and the authority pipeline;
  the six exact-head states; the amendment record; the bootstrap block present,
  well-formed and above `## 1.` in `AGENTS.md`; `AGENTS.md` neither restates the
  laws nor outgrows the constitution; `CONTRIBUTING.md`, `docs/README.md` and
  this file's §3 all name the constitution; the mutual pin between the
  constitution and its enforcer; and two positive controls proving a removed
  law, a softened law, and a buried bootstrap block are all detected.
- `docs/architecture/MODULE_MAP.md` §3 registers the pair as **R14**, so the
  law→test mapping is discoverable in the boundary register.
- `scripts/agent_constitution_mutations.py` — twenty-six adversarial mutants of the contract
  itself (bootstrap buried or softened, a law renamed or deleted, a silent version bump,
  the mutual pin broken, a gate dimension and a status definition dropped, the
  `gates_owner` deference removed, an entry point silenced, the CI campaign job deleted or
  made non-blocking, the campaign gutted or blinded, the **guard marked `slow` so CI's
  `-m "not slow"` deselects it**, the guard ignored from the pytest configuration, pytest
  recursion pointed away from its directory, and the file moved out of the root). The campaign passes only if every mutant turns the enforcer red **and**
  every file is restored byte for byte; it is stdlib-only and needs nothing but pytest.
- CI job `agent-constitution-mutations` in `.github/workflows/ci.yml` runs that campaign
  on every push and then asserts `git diff --exit-code`, so a mutant that edits the tree
  and leaves it edited is a red build.
- Amendment discipline: a change to §2, §4, §5 or §8 of the constitution bumps
  `CONSTITUTION_VERSION`, adds an amendment-record row, and updates the test in
  the same commit; the test fails otherwise, which is the intended signal.

## Pros and Cons of the Options

### Option 1 — mission-prompt only

- (+) No repository change.
- (−) Dies with the session; unenforceable; unreviewable.

### Option 2 — grow `AGENTS.md`

- (+) One file to read.
- (−) Buries coordination; makes law editable by coordination edits; no version.

### Option 3 — dated audit report

- (+) Fits the existing filing habit.
- (−) A dated record is explicitly "never a source of truth" in this docs layer.

### Option 4 (chosen) — root constitution + enforcer

- (+) Persistent, versioned, enforced, and discoverable from the file every
  agent already reads first.
- (−) Two artifacts to keep in sync, by design and under an amendment law.

## More Information

- [`../../../NAGAR_AGENT_CONSTITUTION.md`](../../../NAGAR_AGENT_CONSTITUTION.md) — the law.
- [`../../../AGENTS.md`](../../../AGENTS.md) — bootstrap entry + coordination contract.
- [`../../DECISION_LOG.md`](../../DECISION_LOG.md) — remains authoritative for
  decisions that change system behaviour; this record does not restate any.
- [`0004-board-schema-2.md`](0004-board-schema-2.md) — the same "declared, not
  requested" philosophy applied to the coordination board.
- [`../TESTING.md`](../TESTING.md) §3 — fitness functions: why these laws are
  tests and not prose.
