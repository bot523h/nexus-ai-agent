# NEXUS ENGINEERING CONSTITUTION — قانون اساسی مهندسی نکسوس

| | |
|---|---|
| **Status** | accepted — binding on every agent (model or human) working in this repository |
| **Version** | 1.1.0 |
| **Date** | 2026-09-25 |
| **Owner** | repository owner (`bot523h`) |
| **Applies to** | every session that creates, deletes, reviews, or signs off a change in this repository |
| **Precedence** | this constitution prevails over every local rule, summary, and habit in this repository (its own Article 14) |
| **Language** | the Persian text is the owner's original and is **normative**; the English rendering is a translation so that non-Persian-reading agents and CI reviewers read the same law. On any divergence the Persian text wins. |
| **Hash algorithm** | sha256 over the whitespace-normalised text of each part below |
| **Content identity — Persian original** | `ede16663bc141a7950a06d85fe86851a51ad6765023186f4d1b22b51a5257ee3` |
| **Content identity — English rendering** | `b29490554650ecded4b813660adc2c7b21657e1162eb88817a143cf087f13cfb` |
| **Enforcement** | `python scripts/constitution_gate.py` (stdlib gate, wired into the `lint-fast` CI job) + `tests/unit/test_engineering_constitution.py` + the cross-guards in `tests/unit/test_docs_integrity.py` and `tests/unit/test_agent_board.py`. Editing the law is allowed — but only as a deliberate act: re-stamp the content identity (`--print-hashes`) and bump the Version row in the same commit, so a silent edit is a red gate (Articles 5, 6, 9). |

> **قانون قطعی:** هیچ عاملی — انسان یا مدل — حق ندارد در این مخزن کاری کند، فایلی بسازد، تغییر دهد یا
> حذف کند، یا کاری را «تمام» اعلام کند، بدون اینکه این ۱۴ ماده را خوانده و رعایت کرده باشد.
>
> **Binding rule:** no agent — human or model — may work in this repository, create, change, or delete a
> file, or declare anything "done", without having read and complied with these 14 articles.

---

## 0. How this document is used (برای همهٔ عامل‌ها / for every agent)

This is not background reading; it is the entry gate. Every session, before its first edit:

1. **Read the 14 articles below** (Persian original, English rendering — one of the two is enough for
   comprehension, both for exactness).
2. **Identify yourself on the board** — `python scripts/agent_board.py show`, then
   `... next --branch <your-branch>`, then `... claim <task> --branch <your-branch>` and push the board
   change immediately ([`AGENTS.md`](AGENTS.md) §1). `show`, `next`, and `claim` all print the pointer
   back to this file, so the constitution cannot be skipped by accident.
3. **Work the loop of Article 1** (`SEARCH → UNDERSTAND → COMPARE → DESIGN → IMPLEMENT → TEST →
   VERIFY`) and record the Article 12 decision record (OPTIONS / TRADEOFFS / DECISION / WHY / RISK /
   TEST) in the PR body.
4. **Close with the Final Gate of Article 14** (CODE + TEST + SECURITY + CI + OBSERVABILITY +
   DOCUMENTATION + BOARD EVIDENCE) — the checklist in §16 below is the repository's concrete form of
   that gate, and [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md) is its
   per-PR form.

### How this document stays alive (چطور این سند زنده می‌ماند)

A constitution nobody can lose is a constitution that is enforced, not merely written. This one is
pinned from five sides, and each pin has a test:

| Pin | Where | Enforcer |
|---|---|---|
| mandatory first read | [`AGENTS.md`](AGENTS.md) §0 (the agent front door) | `tests/unit/test_engineering_constitution.py` |
| contributor instructions | [`CONTRIBUTING.md`](CONTRIBUTING.md) | `tests/unit/test_engineering_constitution.py` |
| human front door | [`README.md`](README.md) → Documentation Map | `tests/unit/test_engineering_constitution.py` |
| coordination medium | `.agents/board.json` → `protocol.constitution` | `tests/unit/test_engineering_constitution.py` |
| runtime reminder | `scripts/agent_board.py` (`show` / `next` / `claim`) | `tests/unit/test_engineering_constitution.py` |
| per-PR gate | `.github/PULL_REQUEST_TEMPLATE.md` (Final Gate checklist) | review |

Deleting, renaming, or un-linking this file fails the suite, and therefore CI
(`pytest -q tests/unit/test_engineering_constitution.py`, which runs inside the existing `test` job
alongside `tests/unit/test_docs_integrity.py`).

---

# Part I — Persian original (متن اصلی؛ معتبر است / normative)

## ماده 1 — هیچ کدی بدون تحقیق

قبل از ایجاد، حذف یا تغییر هر فایل یا قطعهٔ کد غیرtrivial:

"SEARCH → UNDERSTAND → COMPARE → DESIGN → IMPLEMENT → TEST → VERIFY"

کدنویسی از روی حدس ممنوع است.

## ماده 2 — Deep Search برای هر فایل

قبل از touching هر file:

1. کل repository را برای symbol / interface / behavior مربوطه جست‌وجو کن.
2. git history آن فایل و symbol را بخوان.
3. tests موجود را بررسی کن.
4. ADR / architecture docs / contracts را بررسی کن.
5. GitHub را برای implementationهای canonical و production-grade جست‌وجو کن.
6. documentation رسمی technology را بررسی کن.
7. منابع current web / Google-indexed technical material را بررسی کن.
8. در موارد امنیتی، CVE / advisory / OWASP / vendor guidance را بررسی کن.
9. گزینه‌های موجود را مقایسه کن.
10. سپس implementation را انتخاب کن.

برای تصمیم‌های مهم، حداقل 3 منبع مستقل معتبر لازم است.

## ماده 3 — بهترین گزینه ≠ محبوب‌ترین گزینه

انتخاب implementation باید بر اساس این معیارها باشد:

"Correctness"

"Security"

"Maintainability"

"Testability"

"Observability"

"Performance"

"Operational simplicity"

"Compatibility"

"Failure behavior"

اگر راه‌حل جدید از راه‌حل فعلی بهتر است، دلیل فنی آن را ثبت کن.

اگر راه‌حل فعلی بهتر است، تغییر نده.

## ماده 4 — Innovation با Guardrail

هر عامل باید برای هر مسئله حداقل یک گزینهٔ نوآورانه‌تر را بررسی کند؛

اما innovation فقط وقتی پذیرفته است که:

- contract را خراب نکند
- complexity بی‌دلیل نسازد
- security را تضعیف نکند
- قابل تست باشد
- rollback/recovery داشته باشد
- operationally understandable باشد

نوآوری بدون مهندسی = رد.

## ماده 5 — Evidence First

هیچ عبارت:

"fixed"

"done"

"safe"

"production-ready"

"compatible"

"faster"

بدون evidence معتبر مجاز نیست.

Evidence باید شامل چیزی از این جنس باشد:

- test
- benchmark
- CI run
- static analysis
- runtime probe
- reproducible command
- artifact
- hash/SHA
- migration/restore proof

## ماده 6 — Fail Closed

هرجا system مطمئن نیست:

"explicit refusal > fake success"

هیچ placeholder نباید خودش را به شکل موفقیت واقعی نشان دهد.

## ماده 7 — تغییر کوچک، نتیجه کامل

Small change preferred است؛

ولی ناقص بودن ممنوع.

یک patch کوچک که contract را کامل نمی‌کند از یک تغییر کمی بزرگ‌تر ولی production-grade بدتر است.

## ماده 8 — Scope Firewall

هر agent فقط owner حوزهٔ خودش است.

قبل از تغییر فایل خارج از exclusive scope باید:

- dependency واقعی اثبات شود
- دلیل ثبت شود
- overlap بررسی شود
- board ثبت شود

## ماده 9 — No Silent Debt

هر مشکل کشف‌شده یکی از این stateها را داشته باشد:

"FIXED"

"VERIFIED"

"DEFERRED_WITH_REASON"

"KNOWN_ENVIRONMENTAL"

"NOT_REPRODUCIBLE"

"FALSE_POSITIVE"

هیچ finding نباید در هوا رها شود.

## ماده 10 — Reproducibility

هر کاری که انجام شد باید قابل بازسازی باشد:

- command
- inputs
- environment
- output
- commit SHA

## ماده 11 — Security Before Convenience

در تعارض میان convenience و security:

امنیت، integrity و data correctness اولویت دارند.

## ماده 12 — مالکیت تصمیم

عامل اجازه دارد implementation را خودش انتخاب کند؛

اما باید برای تصمیم خود evidence بدهد:

"OPTIONS"

"TRADEOFFS"

"DECISION"

"WHY"

"RISK"

"TEST"

قرار نیست فقط دستور اجرا کند؛

قرار است مثل owner subsystem تصمیم مهندسی بگیرد.

## ماده 13 — Never Optimize the Metric

هدف:

"healthy system"

نه:

"green dashboard"

بنابراین:

- test حذف نمی‌شود
- assertion تضعیف نمی‌شود
- skip مصنوعی ساخته نمی‌شود
- log جعل نمی‌شود
- coverage با trick بالا نمی‌رود
- failure پنهان نمی‌شود

## ماده 14 — Final Gate

هیچ laneای DONE نیست مگر اینکه:

"CODE"

"TEST"

"SECURITY"

"CI"

"OBSERVABILITY"

"DOCUMENTATION"

"BOARD EVIDENCE"

همگی با هم موجود باشند.

این قانون اساسی است و در تمام تصمیم‌های معماری بر قوانین محلی مقدم است.

---

# Part II — English rendering (ترجمهٔ مرجع / reference translation)

The Persian original in Part I is the law. This part exists so that every agent — and every CI
reviewer — reads the same obligations.

## Article 1 — No code without research

Before creating, deleting, or changing any non-trivial file or piece of code:

"SEARCH → UNDERSTAND → COMPARE → DESIGN → IMPLEMENT → TEST → VERIFY"

Coding by guesswork is forbidden.

## Article 2 — Deep search for every file

Before touching any file:

1. search the whole repository for the relevant symbol / interface / behaviour;
2. read the git history of that file and symbol;
3. review the existing tests;
4. review the ADRs / architecture docs / contracts;
5. search GitHub for canonical, production-grade implementations;
6. review the technology's official documentation;
7. review current web / Google-indexed technical material;
8. for security matters, review CVE / advisory / OWASP / vendor guidance;
9. compare the available options;
10. then choose the implementation.

Important decisions require at least three independent, authoritative sources.

## Article 3 — The best option is not the most popular option

The choice of implementation must rest on these criteria:

"Correctness" · "Security" · "Maintainability" · "Testability" · "Observability" ·
"Performance" · "Operational simplicity" · "Compatibility" · "Failure behavior"

If a new solution is better than the current one, record the technical reason.

If the current solution is better, do not change it.

## Article 4 — Innovation with guardrails

Every agent must examine at least one more innovative option for every problem;

but innovation is accepted only when it:

- does not break the contract;
- does not create unjustified complexity;
- does not weaken security;
- is testable;
- has rollback / recovery;
- is operationally understandable.

Innovation without engineering is rejected.

## Article 5 — Evidence first

No claim of:

"fixed" · "done" · "safe" · "production-ready" · "compatible" · "faster"

is permitted without valid evidence.

Evidence must include something of this kind:

- test;
- benchmark;
- CI run;
- static analysis;
- runtime probe;
- reproducible command;
- artifact;
- hash / SHA;
- migration / restore proof.

## Article 6 — Fail closed

Wherever the system is unsure:

"explicit refusal > fake success"

No placeholder may present itself as real success.

## Article 7 — Small change, complete result

A small change is preferred;

but incompleteness is forbidden.

A small patch that does not complete the contract is worse than a slightly larger, production-grade
change.

## Article 8 — Scope firewall

Every agent owns only its own domain.

Before changing a file outside the exclusive scope:

- the real dependency must be proven;
- the reason must be recorded;
- the overlap must be checked;
- the board must be updated.

## Article 9 — No silent debt

Every discovered problem must carry one of these states:

"FIXED" · "VERIFIED" · "DEFERRED_WITH_REASON" · "KNOWN_ENVIRONMENTAL" · "NOT_REPRODUCIBLE" ·
"FALSE_POSITIVE"

No finding may be left hanging.

## Article 10 — Reproducibility

Everything that was done must be reconstructible:

- command;
- inputs;
- environment;
- output;
- commit SHA.

## Article 11 — Security before convenience

In a conflict between convenience and security:

security, integrity, and data correctness take priority.

## Article 12 — Decision ownership

The agent may choose the implementation itself;

but it must provide evidence for its decision:

"OPTIONS" · "TRADEOFFS" · "DECISION" · "WHY" · "RISK" · "TEST"

It is not meant to merely execute an instruction;

it is meant to make an engineering decision like the owner of the subsystem.

## Article 13 — Never optimize the metric

The goal is:

"healthy system"

not:

"green dashboard"

Therefore:

- tests are not deleted;
- assertions are not weakened;
- artificial skips are not created;
- logs are not forged;
- coverage is not inflated with tricks;
- failures are not hidden.

## Article 14 — Final gate

No lane is DONE unless:

"CODE" + "TEST" + "SECURITY" + "CI" + "OBSERVABILITY" + "DOCUMENTATION" + "BOARD EVIDENCE"

are all present together.

This law is fundamental, and in all architecture decisions it prevails over local rules.

---

## 15. Enforcement map (article → the thing that keeps it true)

Per the documentation rule "every structural claim has an enforcer"
([`docs/README.md`](docs/README.md), rule 3), each article names its guard in this repository:

| Article | Guard in this repository |
|---|---|
| 1 — research loop | PR body must name the search/compare step; `docs/architecture/adr/` + `docs/DECISION_LOG.md` are the record |
| 2 — deep search | Article 2 checklist is part of the PR template; architecture docs are indexed by `tests/unit/test_docs_integrity.py` |
| 3 — best ≠ popular | decision records (Article 12) with rejected alternatives |
| 4 — innovation with guardrails | ADR process: consequences + confirmation section mandatory |
| 5 — evidence first | PR body must state commands **and** their observed result; `tests/unit/test_engineering_constitution.py` keeps the gate wording present |
| 6 — fail closed | typed-failure tests (`tests/unit/test_failure_semantics.py`, `test_job_lifecycle.py`), fail-closed guards |
| 7 — small but complete | acceptance criteria are mandatory on the board (`tests/unit/test_agent_board.py`) |
| 8 — scope firewall | `python scripts/agent_board.py check --files <files> --branch <you>` (exit 1 on overlap) + board leases |
| 9 — no silent debt | board `deferred_log` + the six finding states above |
| 10 — reproducibility | CI jobs (`test`, `extras-matrix`, `python-parity`, `migrate-postgres`, `release-lineage`) and dated audits |
| 11 — security before convenience | `docs/architecture/SECURITY.md` + the security-boundary tests |
| 12 — decision ownership | Article 12 record in the PR body; ADR / DECISION_LOG for durable decisions |
| 13 — never optimize the metric | `tests/unit/test_ci_lint_parity.py`, skip-inflation audit (`scripts/extras_matrix.py audit-skips`), mutation probes (`scripts/gate5_mutation_probes.py`) |
| 14 — final gate | `.github/PULL_REQUEST_TEMPLATE.md` + the CI job set |

## 16. Article 14 in practice — the Final Gate checklist

A lane is DONE only when all seven exist together. In this repository that means:

| Gate | Concrete form |
|---|---|
| CODE | the change is on the session branch, committed, with no placeholder standing in for a real implementation |
| TEST | `pytest -q -m "not slow"` (or the focused subset) with the exact command and result recorded |
| SECURITY | the security boundary is untouched or re-verified; `docs/architecture/SECURITY.md` review checklist applied |
| CI | the workflow jobs (`lint`, `test`, `extras-matrix`, `python-parity`, `migrate-postgres`, `release-lineage`, `lint-fast`) green on the head SHA |
| OBSERVABILITY | structured events / metrics for new behaviour, per `docs/architecture/OBSERVABILITY.md` |
| DOCUMENTATION | living views updated in the same PR (`docs/architecture/*`), index and links still green |
| BOARD EVIDENCE | claim + release (or deferral) recorded in `.agents/board.json`, acceptance criteria named, commands and results in the PR body |

## 17. Article 9 in practice — finding states

Every discovered problem is closed in exactly one of these states, and the state is written down:

`FIXED` · `VERIFIED` · `DEFERRED_WITH_REASON` · `KNOWN_ENVIRONMENTAL` · `NOT_REPRODUCIBLE` ·
`FALSE_POSITIVE`

`DEFERRED_WITH_REASON` uses the board's bilingual deferral note
(`python scripts/agent_board.py defer <task> --branch <you> --fa "…" --resume-when "…"`), so a
deferral is a record, not a silence.

## 18. Article 12 in practice — the decision record

For any non-trivial decision, the PR body (or the ADR) carries:

| Field | Content |
|---|---|
| `OPTIONS` | the alternatives that were actually considered, including the "do nothing" option |
| `TRADEOFFS` | what each option costs, against the nine criteria of Article 3 |
| `DECISION` | the chosen option, stated in one sentence |
| `WHY` | the technical reason, tied to the drivers |
| `RISK` | what can still go wrong, and the rollback / recovery path (Article 4) |
| `TEST` | the evidence: command, observed result, and where it is recorded |

## 19. Registration record for this document (ماده 12 applied to itself)

| Field | Record |
|---|---|
| `OPTIONS` | (a) keep the constitution in chat only; (b) paste it into `AGENTS.md` as prose; (c) register it as a first-class repository document with pointers and an enforcement test |
| `TRADEOFFS` | (a) is lost the moment the session ends — fails Article 10; (b) survives but has no enforcement, so it drifts and can be silently deleted — fails Articles 5/14; (c) costs one new file plus five pointer edits and one test file, and makes the law un-losable |
| `DECISION` | (c): `ENGINEERING_CONSTITUTION.md` at the repository root, next to `AGENTS.md` (the agent front door), wired into `AGENTS.md` §0, `CONTRIBUTING.md`, `README.md`, `.agents/board.json → protocol.constitution`, the `agent_board.py` `show`/`next`/`claim` output, and `.github/PULL_REQUEST_TEMPLATE.md` |
| `WHY` | the constitution must be read *before* work, so it has to sit where every arriving agent already looks (`AGENTS.md`, the board CLI) rather than in a document tree an agent may never open; and it must be enforced, because an unenforced law is exactly the "green dashboard" Article 13 forbids optimizing for |
| `RISK` | a second canonical text could drift from `AGENTS.md`/`docs/` — mitigated by the constitution being the *only* normative governance text and the pointers being test-checked; the Persian original plus English rendering doubles the reading surface — mitigated by the declared precedence rule in the header |
| `TEST` | `pytest -q --noconftest tests/unit/test_engineering_constitution.py tests/unit/test_docs_integrity.py tests/unit/test_agent_board.py tests/unit/test_agent_board_active_in_review.py tests/unit/test_agent_board_pr_visibility.py` — see §0 for what each pin asserts; the same selection runs in the CI `test` job |

## More information

- [`CONSTITUTION_ENFORCEMENT_AUDIT_2026-09-25.md`](CONSTITUTION_ENFORCEMENT_AUDIT_2026-09-25.md) —
  the adversarial audit that proved which of these articles were actually enforced, the bypass and
  mutation results, and the hardening that closed the proven gaps (task-185)
- [`AGENTS.md`](AGENTS.md) — the multi-agent coordination contract (protocol v2); §0 is the entry gate
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — local quality rails and the multi-agent rules
- [`docs/MULTI_AGENT_PROTOCOL.md`](docs/MULTI_AGENT_PROTOCOL.md) /
  [`docs/MULTI_AGENT_PROTOCOL.fa.md`](docs/MULTI_AGENT_PROTOCOL.fa.md) — the coordination protocol
- [`docs/architecture/SECURITY.md`](docs/architecture/SECURITY.md) — the security boundary this
  constitution's Article 11 protects
- [`docs/architecture/TESTING.md`](docs/architecture/TESTING.md) — the gate taxonomy and the
  documentation gates
