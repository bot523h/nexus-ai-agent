# Nagar Agent Constitution

**CONSTITUTION_VERSION:** `1.0.0`
**AMENDED_AT:** `2026-10-05`
**STATUS:** `active` — in force from the commit that carries this file
**LOCATION:** repository root, beside `AGENTS.md`. This file must never be moved, renamed, or folded into another file without an amendment (§8).
**AUDIENCE:** every agent — AI or human — that plans, analyses, codes, tests, commits, opens a pull request, or claims completion in this repository, in any future session, with or without memory of this one.
**ENFORCER:** `tests/architecture/test_agent_constitution.py` (registered as law **R14** in
[`docs/architecture/MODULE_MAP.md`](docs/architecture/MODULE_MAP.md) §3), attacked on every push by
`scripts/agent_constitution_mutations.py` (CI job `agent-constitution-mutations`).
**LANGUAGE:** English is canonical. §11 is a Persian summary that defers to this text on conflict.

---

## 0. Bootstrap — the only unconditional law

> ### MANDATORY SESSION BOOTSTRAP
>
> Before any planning, analysis, coding, testing, Git operation, PR operation, or completion claim, every agent MUST read:
>
> 1. `AGENTS.md`
> 2. `NAGAR_AGENT_CONSTITUTION.md`
> 3. all mission-specific instructions explicitly applicable to the current task
>
> The agent MUST NOT begin implementation before reading and applying these files.
>
> These files are the persistent cross-session engineering contract for Nagar.

### L0 — BOOTSTRAP

The bootstrap is unconditional and non-delegable.

* No exemption exists for "small", "documentation-only", "obvious", "already known", or "the user is in a hurry" tasks.
* The files are read **from the current checkout**, at the revision being worked on — never from memory, never from a previous session's summary, never from a chat transcript, never from a scratch file under `.agents/` or `docs/audits/`.
* An agent that spawns, delegates to, or is spawned by another agent passes this contract down. Delegation never transfers the obligation.
* Reading is not compliance. The agent must *apply* the laws and show it did (§6, BOOTSTRAP ATTESTATION).

Four outputs are required before the first edit:

| # | Bootstrap output | Where it is recorded |
|---|---|---|
| 0.1 | **Attestation** — the three readings, named, at commit `<sha>` | completion report / PR body, §6 |
| 0.2 | **Truth pass** — branch, HEAD, working tree, owners of the paths about to be touched (board claims), the contracts and callers of the code being changed | working notes, then §6 |
| 0.3 | **Conflict check** — the new task's instructions are diffed against this file, `AGENTS.md`, and `docs/DECISION_LOG.md`; every disagreement is listed, never silently resolved | §6 CONFLICT REGISTER |
| 0.4 | **Status plan** — the completion status the agent expects to be able to claim, chosen from the closed vocabulary of §4, *before* the work starts | §6 STATUS |

---

## 1. Nagar North Star

**Nagar 1 is not a throwaway MVP.** Nagar 1 is the strongest and most durable foundation of the project: Nagar 2, 3, 4 and every later generation must be able to be built on it without a rewrite and without breaking the backbone.

Every change is judged on seven axes. A change is acceptable when it, as far as the change allows:

| Axis | Increases | Never traded away for |
|---|---|---|
| correctness | ✓ | speed of delivery |
| security | ✓ | convenience of the happy path |
| durability | ✓ | a shorter diff |
| verification | ✓ | a greener-looking report |
| extensibility | ✓ | a local shortcut |
| future compatibility | ✓ | this week's demo |
| architectural debt | **decreases** | anything |

**Decision rule.** When two designs are otherwise equal, choose the one that is harder to break, easier to verify, and cheaper to build on. When a design would force a future rewrite of the backbone, it is a defect in *this* change, not a task for later.

---

## 2. The non-negotiable laws

| ID | Law |
|---|---|
| **L0** | BOOTSTRAP (§0) |
| **L1** | TRUTH BEFORE CODE |
| **L2** | NO SUPERFICIAL WORK |
| **L3** | NO FAKE VERIFICATION |
| **L4** | ADVERSARIAL ENGINEERING |
| **L5** | AUTHORITY DISCIPLINE |
| **L6** | AI IS NOT AUTHORITY |
| **L7** | NO FAKE DEFAULTS |
| **L8** | FUTURE-PROOFING |
| **L9** | TEST HACKING IS FAILURE |
| **L10** | EXACT-HEAD PROOF |

A law may be narrowed by a *recorded* exception (§8.3), never by silence, never by a chat message, never by "the mission said so" without the five exception fields.

### L1 — TRUTH BEFORE CODE

Before writing code, the agent inspects: the repository, the architecture documents, the callers, the contracts, the tests, the Git state, and the constraints.

* Old documentation is **not** assumed true. `docs/audits/*`, `docs/history/*`, dated root reports, and any memory of a previous session are records, not truth.
* Truth is, in this order: the code and tests at the exact revision, CI at that exact revision, `docs/DECISION_LOG.md`, then the living architecture views.
* A fact used in a decision is one the agent has re-established in this session, or one it cites with a reproducible command and the revision it was run at.

### L2 — NO SUPERFICIAL WORK

The existence of code, of a class, of a test, or of a pull request is **not** completion. A capability is complete when it is traceable, end to end, through:

```text
Caller → Intent → Decision → Authority → Execution → State → Verification → Evidence → Artifact
```

Each arrow must be answerable with a named file, symbol, command, or record. If a link in the chain cannot be named, the capability is `HARDENED_BUT_NOT_COMPLETE` at best (§4), never `VERIFIED`.

### L3 — NO FAKE VERIFICATION

An agent must **not**:

* write `VERIFIED` about something it did not verify;
* present an old CI run as proof for a new commit;
* present a local test run as proof of production, or of a PR head, or of `main`;
* verify metadata (a manifest, a row, a report, a hash of a hash) *instead of* the artifact the claim is about;
* declare success on the strength of its own report.

Verification measures the thing claimed. Where the artifact is bytes, the bytes are re-measured; where it is a state transition, the durable row is read back; where it is a capability, the caller path is exercised.

### L4 — ADVERSARIAL ENGINEERING

The agent deliberately tries to break its own implementation, and records what it tried:

| Class | Minimum probes |
|---|---|
| negative cases | the refused, unauthorized, and unsupported paths fail *loudly and typed* |
| malformed input | truncated, oversized, wrongly-typed, hostile-encoding input |
| duplicate requests | the same idempotency key twice → one effect, honest duplicate |
| retries | retry after partial success; retry after terminal state |
| stale state | a decision made on a snapshot that has since moved |
| restart | process restart mid-flight → recovery or honest failure, never silent loss |
| crash recovery | interrupted write → no half-committed truth, no fabricated continuity |
| concurrency | two actors on the same entity → one wins, the loser is told |
| tampering | edited bytes, edited records, edited metadata → detected and reported |
| security abuse | privilege escalation, path escape, SSRF, secret leakage, forged identity |
| boundary conditions | empty, zero, one, max, off-by-one, clock edges, unicode/RTL |

A guard that has never been attacked is not evidence. Where the repository provides a mutation harness (`scripts/pack_trust_mutations.py`, `scripts/continuum_mutations.py`, `scripts/llm_queue_mutations.py`), use it and record the kill count. **This constitution attacks itself**: `python scripts/agent_constitution_mutations.py` runs twenty-one bypass mutants — bootstrap buried below §1, law renamed to guidance, law deleted, version bumped silently, mutual pin broken, gate dimension dropped, status definition removed, `gates_owner` deference removed, entry points silenced, the CI campaign job deleted or made non-blocking, and the file moved out of the root — and passes only when every one of them turns the enforcer red and every file is restored byte for byte. CI runs it on every push (`agent-constitution-mutations` job).

### L5 — AUTHORITY DISCIPLINE

For every state, name the four roles and keep them distinct:

| Role | Meaning |
|---|---|
| **Source of truth** | the one place whose value *is* the state |
| **Cache** | a derived, disposable copy — may be stale, may be dropped |
| **Projection** | a read-only view computed from the truth — may be wrong only if the truth is |
| **Evidence** | a measurement of the truth or of an artifact, never a substitute for it |

No subsystem may create a second, conflicting authority. Adding an authority requires a `docs/DECISION_LOG.md` entry that names the old one and its disposition. §3 fixes the current map.

### L6 — AI IS NOT AUTHORITY

AI produces **intent** and **proposals**. AI output is not, by itself, authority or permission. The architecture keeps the boundaries intact:

```text
Intent → Policy → Command → Execution → Verification
```

* An intent may be generated anywhere; only Policy may authorize; only Command may name an action; only Execution may touch the world; only Verification may claim a result.
* A model's assertion that something is allowed, present, or true is data, never a decision.
* In this repository that boundary is already enforced by the canonical command contract (`nagar.command.v1`, `CommandBus`, `CapabilityRegistry`, `PermissionLevel`) and by the prohibition on AI modules importing Nagar executors or pack handlers — R13 in [`docs/architecture/MODULE_MAP.md`](docs/architecture/MODULE_MAP.md) §3.

### L7 — NO FAKE DEFAULTS

For authoritative data, no fabricated value, no artificial zero, no empty fallback, and no fake success:

* a missing measurement is `unknown` and fails closed, not `0`, `""`, `[]`, or `True`;
* an unsupported operation is a typed failure, never a plausible-looking success;
* a fallback that makes a broken path look healthy is a defect, even when the happy path is green.

### L8 — FUTURE-PROOFING

Before calling a change complete, answer the four questions of §7.3. A change that fails any of them ships only with the failure recorded as a limitation and a task, never as a silent assumption.

### L9 — TEST HACKING IS FAILURE

Forbidden: deleting or weakening an assertion; suppressing, skipping, or `xfail`-ing a failure to get green; mocking the primary path so the test passes without exercising it; loosening a guard so its test passes; changing production behaviour *in order to* pass a test; deleting a test to remove a signal.

A red test is information. The only legitimate responses are: fix the production code, fix the test's mistaken assumption (and say so), or record the defect as `BLOCKED`/`DEFERRED` with an owner.

### L10 — EXACT-HEAD PROOF

These are **different facts** and must never be conflated:

```text
LOCAL BRANCH   ≠  PR HEAD  ≠  CI HEAD  ≠  REVIEWED HEAD  ≠  MERGED MAIN  ≠  DEPLOYED ARTIFACT
```

Every claim is bound to the exact revision it was measured at: local results name the branch and the local HEAD SHA; CI results name the workflow, the run id, and the commit SHA the run tested; merged work names the merge commit on `main`; a deployed claim names the artifact and its digest. "Tests pass" without all four coordinates is not evidence.

---

## 3. Authority map (current)

Roles (L5): **source of truth** is the state; **cache** is a disposable copy that may be stale;
**projection** is a read-only view computed from the truth; **evidence** measures the truth or the
artifact and never replaces it. A dash means the role has no instance, not that it is unknown.

| State | Source of truth | Cache | Projection | Evidence |
|---|---|---|---|---|
| Job state and outcome | the durable queue row (SQLite/PostgreSQL via `JobQueuePort`) | the in-memory worker view | the operator-facing job view | queue contract + integration tests, `docs/architecture/JOB_LIFECYCLE.md` |
| Artifact existence and bytes | the bytes on storage | a local working copy | the recorded digest | re-measurement of the bytes (the Artifact Passport re-measures rather than trusts) |
| Causal history | the append-only provenance journal | — | the Artifact Passport | `verify_chain` + the tamper matrix — evidence, never authority |
| Capability-pack activation | the trust root (`creative/packs/trust_root.json`) + Ed25519 verification | — | `VerificationReport` (a report; `ok` is not `trusted`) | `scripts/pack_trust_mutations.py`. `creative/packs/delivery/signing.py` is a **transport seam**, never an activation authority |
| Command authorization | `CommandBus` + `CapabilityRegistry` + `PermissionLevel` | — | the command response text | `tests/unit/test_command_capability_contract.py`, `tests/architecture/test_command_capability_boundary.py` |
| Database schema | the Alembic chain under `migrations/` | — | SQLModel table definitions | `nexus migrate` against real PostgreSQL, migration tests |
| Release identity | `VERSION` == `pyproject.toml` == newest `CHANGELOG` heading | — | the README banner, the installed distribution metadata | `scripts/check_version_lockstep.py`, `scripts/release_lineage.py` |
| Who may touch which file | `.agents/board.json` via `scripts/agent_board.py` | — | the `AGENTS.md` board summary | `python scripts/agent_board.py check --files …` |
| Architecture decisions | `docs/DECISION_LOG.md` | — | `docs/architecture/*` (living views) | the enforcing test each rule names |

Two authorities that disagree are a **defect to report**, not a choice to make silently (§9.2).

---

## 4. Completion vocabulary (closed)

Exactly five statuses exist. An agent must not invent a sixth, must not redefine these, and must not change a status to make a report look better. Downgrading honestly is always permitted; upgrading requires new evidence.

| Status | Definition | Required to claim it |
|---|---|---|
| `VERIFIED` | Every gate dimension applicable to the change (§5) was checked, at the exact head commit, with named evidence; no unresolved defect remains inside the declared scope. | exact-head evidence per applicable dimension; all non-applicable dimensions explicitly marked `N/A` with a reason; no known open defect in scope |
| `VERIFIED_WITH_LIMITATIONS` | The core claim is true and evidenced at the exact head, **and** named parts of the world were not reachable (no credentials, no live service, single-process only, one platform only). The limitations are outside the claimed fact, not inside it. | as `VERIFIED` for the claimed scope, plus a LIMITATIONS list where every entry names what was not covered and why it does not weaken the claim |
| `HARDENED_BUT_NOT_COMPLETE` | Measurable hardening, tests, or guards were delivered and evidenced, but the capability does not yet meet its acceptance criteria (missing integration, missing surface, missing authority, deferred dependency). | what was hardened (with evidence) + precisely what is missing + the task/owner that closes it |
| `BLOCKED` | Work cannot proceed: a missing authority, a lease held by another agent, absent credentials, an unavailable service, or a decision that has not been made. | the blocker, its owner, the resume condition, and what is already proven |
| `DEFERRED` | The work was deliberately not done now, by decision, and is recorded so it is not forgotten. | the reason, the trigger that resurrects it, and the owner. `DEFERRED` is not a success claim. |

**Anti-shopping rules.**

1. A status is chosen *before* the work starts (§0.4) and may only be upgraded with new evidence.
2. "It works on my machine", "the code exists", "the PR is open", and "CI was green on an earlier commit" map to none of the five; they map to `HARDENED_BUT_NOT_COMPLETE` at best.
3. A claim about `main` requires evidence from `main`, not from the branch.
4. **Domain vocabularies are not replaced by this one.** The Artifact Passport already speaks
   `VERIFIED` / `VERIFIED_WITH_LIMITATIONS` / `INCOMPLETE` / `COMPROMISED`
   ([`docs/architecture/PROVENANCE_LEDGER.md`](docs/architecture/PROVENANCE_LEDGER.md)), with its own
   definitions and its own tests. Those stay authoritative *inside their domain*; this vocabulary
   governs how an agent reports the status of **its work**. Where the two could be read differently,
   name the domain (`mission status: VERIFIED_WITH_LIMITATIONS` vs `passport: COMPROMISED`) instead of
   letting one word carry two meanings.

---

## 5. Completion Gate

For any non-trivial mission — a behaviour change, a new capability, a security or data-integrity change, a cross-cutting refactor, or a claim about production — the agent walks this gate and reports each row. Proportionality applies to *depth*, never to existence: every row is answered, either with evidence or with `N/A` plus a one-line reason. Omitting a row silently is a violation of L3.

| # | Dimension | What must be shown | `N/A` only when |
|---|---|---|---|
| 1 | implementation | the code does what the change claims; named entry points | — |
| 2 | integration | reachable from a real caller through the real chain (L2) | pure internal refactor with no caller-visible change |
| 3 | regression | the existing suite is green, with the command and count | — |
| 4 | negative cases | refusal and unsupported paths fail typed, not silently | no new decision point |
| 5 | boundary cases | empty / zero / one / max / off-by-one / encoding edges | pure data-only change |
| 6 | failure & recovery | retry, restart, crash mid-write, partial success | no new state or I/O |
| 7 | security | trust boundary, authorization, injection, secret handling, SSRF | no new input, surface, path, or secret |
| 8 | persistence | durable state written and read back; schema path stated | no durable state touched |
| 9 | concurrency | duplicate, parallel, and stale-actor behaviour | single-threaded, process-local, provably unreachable concurrently |
| 10 | adversarial review | the L4 probe list, with what was tried and what broke | — (a mission is never exempt from attacking itself) |
| 11 | diff hygiene | only necessary files changed; no drive-by edits, no generated or scratch files, no secrets | — |
| 12 | exact commit | branch and local HEAD SHA of the work | — |
| 13 | exact-head CI | workflow + run id + the SHA that run tested (L10). Run by the gates owner only; everyone else records "deferred to gates owner" and treats local diagnostics as non-proof | no commit was pushed |
| 14 | review feedback | review comments answered and, where accepted, fixed with evidence | not yet reviewed (say so) |
| 15 | limitations | what is *not* covered, and why it does not weaken the claim | none — and then the status must be `VERIFIED` |
| 16 | future-generation impact | the four questions of §7.3 answered for this change | — |

**Coordination deference.** Gate 13 obeys `AGENTS.md` §1 rule 4: exactly one agent (the `gates_owner`) runs the shared gates on main-bound work. This constitution adds engineering obligations; it never transfers file ownership, never overrides a board lease, and never authorizes racing another agent's CI job.

---

## 6. Mandatory completion report

Any completion claim, pull-request body, or handoff note carries these sections, in this order:

```text
FILES CREATED / CHANGED   — exact paths, one line each, no "and others"
BOOTSTRAP ATTESTATION     — the three readings of §0, at commit <sha>
WHAT WAS VERIFIED         — dimension → command → observed result → revision (§5 rows 1–14)
LIMITATIONS               — every uncovered part, and why the claim survives it
CONFLICT REGISTER         — every disagreement found in §0.3, and how it was resolved
EXCEPTIONS                — any L-exception invoked, with its five fields (§8.3), or "none"
FUTURE-GENERATION IMPACT  — the four answers of §7.3
GIT PROOF                 — branch + exact commit SHA (+ PR number, + exact-head CI run id)
STATUS                    — one word from §4 + one line of justification
```

---

## 7. Future-generation contract — Nagar 2, 3, 4+

### 7.1 Backbone invariants

Later generations may replace internals, but not these. Each names its guardian.

| Invariant | Why later generations depend on it | Guardian |
|---|---|---|
| The six ports in `application/ports/` are the only way the application layer reaches the world | Nagar 3 can swap an adapter without touching a use case | R1/R5, `tests/architecture/test_port_signatures.py` |
| `TypedCommand` (`nagar.command.v1`) is the single command envelope; `CommandBus` is the only caller of an operation handler in the creative tree | Nagar 2 can add commands without a second dispatch path | R13, `tests/architecture/test_command_capability_boundary.py` |
| Packs are data-only; the pack trust root is the only activation authority | Nagar 4 can add capabilities without duplicating trust | ADR 0006, `tests/architecture/test_pack_trust_boundary.py` |
| The job lifecycle state machine owns completion; artifacts are verified by re-measurement | Nagar 2/3 can add job types without a second completion authority | `docs/architecture/JOB_LIFECYCLE.md`, `tests/unit/test_job_lifecycle.py` |
| Provenance is append-only evidence and never authority | Nagar 4 can extend the ledger without it being able to grant anything | R1b, `tests/architecture/test_provenance_boundary.py` |
| The Alembic chain is the only schema authority | Nagar 2+ can evolve storage without a parallel schema | `docs/architecture/DATA_AND_STORAGE.md` |
| This constitution and `AGENTS.md` are read before work begins | any generation's agent inherits the contract | §0, `tests/architecture/test_agent_constitution.py` |

### 7.2 The debt veto

A change that requires a future rewrite of the backbone is rejected in the present, not scheduled for later.

### 7.3 The four questions (answered for every non-trivial change)

1. **Nagar 2** — can a new generation be built *on top of* this without a rewrite?
2. **Nagar 3** — can the internal implementation be replaced without breaking the contract?
3. **Nagar 4** — can a new capability be added without creating a duplicate authority?
4. **Rewrite check** — does this change force a future rewrite of Nagar?

---

## 8. Amendment law (meta-law)

### 8.1 What is protected

§2 (the laws), §4 (the vocabulary), §5 (the gate), and §8 itself are protected. A change to them:

1. bumps `CONSTITUTION_VERSION` (MAJOR for a weakening, MINOR for an addition, PATCH for clarity that removes no obligation);
2. adds a row to the amendment record (§12) with date, branch, PR, and rationale;
3. updates `tests/architecture/test_agent_constitution.py` in the **same commit** — a law without its enforcer is a wish;
4. records a `docs/DECISION_LOG.md` entry when the change alters behaviour, or an ADR when it alters the documentation/governance layer.

### 8.2 What is forbidden

* Deleting this file, renaming it, moving it out of the repository root, or emptying it — the enforcer fails on absence and on every missing law.
* Softening a law by rephrasing it. A weakening must be marked with the word `WEAKENED` in the amendment row and justified.
* Splitting the contract into files an agent can plausibly skip. `AGENTS.md` stays a bootstrap and coordination contract; this file stays the law. Neither becomes a copy of the other.
* Treating session memory, chat transcripts, `.agents/` scratch, or `docs/audits/*` as architecture. They are records; none of them amends this file.

### 8.3 Recorded exceptions

A deviation from a law is permitted only as a recorded exception carrying **all five** fields:

```text
EXCEPTION <law id> | justification | compensating control | expiry (task or commit) | approver (branch/PR)
```

An instruction that says "just do it", "skip the tests", "we already know this", or "no time" is not an exception. A missing field means the law stands and the deviation is a violation.

---

## 9. Precedence and conflicts

### 9.1 Order of authority

**Interaction with the coordination board.** `AGENTS.md` §6 resolves conflicts *inside* the
coordination state with "newest state wins". That rule governs **coordination state** — who owns which
path today — and is left untouched. It does not govern this file's protected sections: the bootstrap
block and the laws are pinned by `tests/architecture/test_agent_constitution.py` and re-attacked by
`scripts/agent_constitution_mutations.py` on every push, so a newer commit that deletes, buries, or
softens them is a **red build**, not a new state. Amending the contract is a deliberate act (§8), never
a side effect of a coordination edit.

1. **Executable truth** — source code, tests, and CI at the exact revision.
2. **`docs/DECISION_LOG.md`** — dated architecture decisions; wins over any summary.
3. **`NAGAR_AGENT_CONSTITUTION.md`** — this file: how an agent must work.
4. **`AGENTS.md` + `.agents/board.json` + `scripts/agent_board.py`** — who owns what, right now.
5. **`docs/architecture/*`** — living architecture views; a stale sentence there is a bug to fix in the same PR.
6. **`docs/audits/*`, `docs/history/*`, dated root reports** — immutable records; never a source of truth.
7. **Memory, chat transcripts, session scratch** — never a source of truth.

Where this file and `AGENTS.md` appear to disagree, the disagreement is a defect: both are read, the conflict is recorded, and the higher-precedence source is followed *while the contradiction is reported*, never hidden.

### 9.2 Contradiction handling

1. Do not silently pick a side.
2. Record the contradiction in the CONFLICT REGISTER with both citations.
3. Follow the higher-precedence source for the work at hand.
4. Fix the lower-precedence document in the same PR when it is a living view; file it as a task when it is a dated record or another agent's zone.
5. Never edit this constitution to dissolve a contradiction with code or CI truth: if the code disagrees with the law, the code is the fact and the law is the standard — report both.

---

## 10. Loophole register

Each entry is a way a future session could bypass this contract, and the closure that makes the bypass fail loudly. The closures are checked by the enforcer; a new bypass discovered in a later session is added here in the same PR that closes it.

| # | Bypass attempt | Closure |
|---|---|---|
| B1 | "I already know the rules" — skip the reading | L0 has no exemption clause; the bootstrap is unconditional and pinned in `AGENTS.md` above its first section; §6 requires a named attestation at a commit SHA |
| B2 | Delete or soften the enforcing test, or drop the CI job that attacks it | the constitution names its enforcer and the enforcer pins the constitution's laws (mutual pin); the law→test pair is registered as R14 in `MODULE_MAP.md` §3; and `scripts/agent_constitution_mutations.py` re-runs all twenty-one bypass mutants in CI, so removing either side of the pin — or the pin itself — fails the build |
| B3 | "The mission overrides the constitution" | §9.1 precedence + §8.3: mission instructions may add constraints, never remove one, except through a five-field recorded exception |
| B4 | Invent a friendlier status word | §4 is a closed vocabulary; the enforcer pins the five words and the closure rule |
| B5 | Read but do not apply | §0 requires four outputs (attestation, truth pass, conflict check, status plan) and §6 requires them in the report |
| B6 | Delegate to a sub-agent that never read the file | L0 is non-delegable: the delegating agent passes the contract and remains accountable |
| B7 | Read a remembered or older copy | the bootstrap names the current checkout; the version header is pinned by the enforcer, so a stale copy is detectable |
| B8 | Rename or move the file | the enforcer asserts the exact root path, and every entry point it checks (`README.md`, `CONTRIBUTING.md`, `docs/README.md`, `docs/architecture.md`, `docs/MULTI_AGENT_PROTOCOL.md`, `MODULE_MAP.md`) names it verbatim |
| B9 | "It is only documentation, the gate does not apply" | §5: proportionality applies to depth, never to existence; every row is answered or marked `N/A` with a reason |
| B10 | Ship the Markdown and call the mission complete | §2 (no superficial work) + §4 + §6: a document is evidence only of itself; the gate still has to be walked and reported |
| B11 | Hide a contradiction to keep the report clean | §9.2 makes the CONFLICT REGISTER mandatory and §6 makes it visible in every report |
| B12 | Treat session memory or a previous audit as architecture | §8.2 and §9.1 rank memory and dated records below executable truth; the enforcer pins the precedence list |

---

## 11. خلاصهٔ فارسی

این فایل «قانون اساسیِ دائمیِ عامل‌ها» برای نگار است و در ریشهٔ مخزن، کنار `AGENTS.md` می‌ماند. متن انگلیسی بالا متنِ مرجع است و در صورت تعارض، انگلیسی مقدم است.

* **§۰ قانون آغازین (L0):** هر عامل پیش از هر برنامه‌ریزی، تحلیل، کدنویسی، تست، عملیات گیت، عملیات PR یا ادعای اتمام، باید `AGENTS.md`، این فایل و دستورالعمل‌های مأموریت را از **همین checkout** بخواند و اجرا کند؛ هیچ معافیتی برای کار «کوچک» یا «فقط مستندات» وجود ندارد.
* **§۱ ستارهٔ قطبی:** نگار ۱ یک MVP دورریختنی نیست؛ باید زیربنایی باشد که نگار ۲/۳/۴ بدون بازنویسی روی آن ساخته شوند. هر تغییر باید درستی، امنیت، دوام، verifiability، توسعه‌پذیری و سازگاری آینده را افزایش و بدهی معماری را کاهش دهد.
* **§۲ قوانین غیرقابل مذاکره (L1–L10):** حقیقت پیش از کد؛ نبودِ کارِ سطحی (زنجیرهٔ Caller → Intent → Decision → Authority → Execution → State → Verification → Evidence → Artifact)؛ نبودِ تأیید جعلی؛ مهندسی تقابلی؛ انضباط مرجعیت؛ هوش مصنوعی مرجع نیست؛ نبودِ مقدار جعلی؛ آینده‌نگری؛ دستکاری تست یعنی شکست؛ اثباتِ دقیقِ همان commit.
* **§۴ واژگان اتمام بسته است:** `VERIFIED`، `VERIFIED_WITH_LIMITATIONS`، `HARDENED_BUT_NOT_COMPLETE`، `BLOCKED`، `DEFERRED`. تغییر وضعیت برای زیباتر شدن گزارش ممنوع است.
* **§۵ گیت اتمام:** ۱۶ بُعد از implementation تا future-generation impact؛ هر بُعد یا با مدرک پاسخ دارد یا با `N/A` و دلیل. اجرای گیت‌های مشترک فقط با دارندهٔ `gates_owner` است.
* **§۶–§۱۰:** قالب اجباری گزارش، قرارداد نسل‌های بعد، قانون اصلاح این فایل، ترتیب مرجعیت و ثبتِ راه‌های دور زدن.

---

## 12. Amendment record

| Version | Date | Branch / PR | Change |
|---|---|---|---|
| `1.0.0` | 2026-10-05 | `arena/01a10ba1-nexus-ai-agent` | Initial constitution: bootstrap law (L0), laws L1–L10, authority map, closed completion vocabulary, 16-dimension completion gate, report template, future-generation contract, amendment law, precedence, loophole register. Enforced by `tests/architecture/test_agent_constitution.py` (R14). |
