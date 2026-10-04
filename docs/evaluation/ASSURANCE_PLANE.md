# Independent Agent Assurance / Evaluation Plane

## Purpose and non-authority

The evaluation package is an independent judge of **typed observations**. It is not the Agent and does not replace its orchestration, policy, authorization, CommandBus, persistence, recovery, artifact-verification, or receipt authorities. Its production code does not import the runtime or call I/O, execution, authorization, or persistence APIs. The architecture boundary is guarded by `tests/agent_evaluation/test_architecture_boundary.py`.

The normal flow is:

1. A versioned `EvaluationCase` supplies the request, trusted test context, independent expected contract, and relevant hard constraints.
2. An explicitly supplied `SubjectAdapter.observe(EvaluationInput)` is the only runner-to-subject call boundary. The adapter returns `ObservedBehavior`; it does not return a verdict.
3. Evaluator-owned oracles compare that observation with the case contract and emit deterministic findings and evidence references.
4. The runner aggregates separate dimension statuses and constructs an `EvaluationReport`. A missing adapter or incomplete E2E chain fails closed.

An observation is evidence presented to an oracle, not proof that its source is authentic. The adapter and its evidence-capture method therefore need independent review. This package does not cryptographically authenticate adapter identity, policy records, provenance links, or verifier measurements.

## Components and versions

| Component | Responsibility |
|---|---|
| `src/nexus_ai_agent/evaluation/domain.py` | Frozen typed input, expectation, observation, evidence, finding, result, run, readiness, and report contracts; canonical JSON and content-addressed IDs. |
| `src/nexus_ai_agent/evaluation/corpus.py` | Version `1.0.0` suite: 19 golden, 17 adversarial, and 10 failure cases (46 total). Cases contain expectations, not canned subject observations. |
| `src/nexus_ai_agent/evaluation/oracles.py` | Fifteen evaluator-owned, version-catalogued behavioral oracles for intent, constraints, strategy, IR, plan determinism, capabilities, tool state, authority, execution, artifact verification, provenance, idempotency, recovery, replanning, and execution boundaries. |
| `src/nexus_ai_agent/evaluation/runner.py` | Explicit adapter boundary, fail-closed no-adapter path, per-dimension results, and observation-derived E2E readiness. |
| `src/nexus_ai_agent/evaluation/mutation_campaign.py` | Fourteen deterministic mutations of typed observations, used to test whether critical oracle checks detect safety-relevant changes. These are **not** mutations of Arena A or production runtime code. |
| `tests/agent_evaluation/_witness.py` | Synthetic observation builder for oracle unit tests only. It never calls Arena A and is not an E2E adapter. |

The suite version is `1.0.0`, the report schema is `1.0`, and the current oracle catalog reports version `1.0.0` for each oracle ID. Bump the suite, report schema, or affected oracle version when its behavior or serialized contract changes; do not silently reinterpret old evidence.

## Case corpus

The golden cases exercise valid and recoverable requests, multilingual/paraphrased phrasing, explicit constraints, clarification, unsupported requirements, confirmation, tool unavailability, policy denial, idempotent delivery, restart recovery, provenance continuity, bounded replanning, and stable/localized content identity.

Adversarial cases cover empty and long input, malformed structured output, unknown or aliased capabilities, forged actor and approval claims, user-controlled authority fields, path traversal, shell-like text, prompt/tool-result injection, cross-project references, replay, duplicate delivery, stale execution, and provenance spoofing.

Failure cases distinguish command failure from verification failure and timeout; test zero-byte, hash, media-header, duration, missing-verifier, restart/duplicate-effect, unbounded-replan, and revision-provenance failures. A runtime failure must retain its causal `FailureCode`; a user request being supported is distinct from successful execution or verified completion.

Case IDs are derived from canonical case content. Suite identity includes suite ID/version, ordered case IDs, and required dimensions. Timestamps are neither inputs to identity nor part of the correctness decision.

## Behavioral invariants and hard gates

Dimensions remain separate; no aggregate score can mask a critical failure. The report uses `PASS`, `FAIL`, `NOT_APPLICABLE`, and `BLOCKED` for case/dimension/oracle status. A hard finding makes the overall machine gate fail. The no-adapter state also fails closed: its dimensions and E2E stages are `BLOCKED`, not `PASS`.

Critical checks include:

- hard requirements survive each declared stage and are independently measured in inspectable IR or command evidence;
- unknown capabilities are never remapped or dispatched; argument types and required values match the independent case allowlist;
- the trusted actor is not taken from user/model/tool claims, and approvals/policy decisions have the required evidence source and command binding;
- execution is observed through the canonical CommandBus boundary, with no duplicate causal effects, stale completion, or invented execution success;
- successful artifact completion requires measured integrity and an independent-verifier observation; false-green status, missing verification, hash/header/size/duration faults, and stale artifacts fail;
- provenance binds request, project, revision, transaction, trace, and artifact; cross-project references, replay, and injected revision faults fail;
- retries are bounded and reauthorized; restart recovery retains causal identity/history and does not repeat committed effects;
- the evaluator itself has no runtime authority or direct I/O path.

Failure taxonomy is represented by `FailureCode`; expected refusal, clarification, confirmation, tool-unavailable, policy-denied, execution-failed, timeout, and verification-failed states remain distinct. Tests target outcomes and evidence relationships, not merely object creation or invocation.

## Machine report and commands

The JSON report includes schema version, suite ID/version, run ID, supplied subject SHA, adapter ID, ordered case IDs, oracle versions, per-case expected and observed facts, per-oracle and per-dimension statuses, evidence IDs, failed invariants, hard findings, E2E stage readiness, blocked-condition codes, and the gate verdict. Canonical JSON sorts mapping keys and contains no wall-clock timestamp.

From the repository root:

```sh
PYTHONPATH=src python -m nexus_ai_agent.evaluation corpus
PYTHONPATH=src python -m nexus_ai_agent.evaluation gate --subject-sha "$(git rev-parse HEAD)" > assurance-report.json
```

The `gate` command writes the JSON report to stdout and a concise gate status to stderr. Without an adapter, it returns exit code `1` and `FAIL`; every real-subject case is `BLOCKED`, all ten E2E stages are `BLOCKED`, and the report includes `E2E_ADAPTER_UNAVAILABLE`. The failing exit is intentional, not a test-suite failure and not evidence that Arena A was evaluated. The CLI accepts a SHA argument but does not resolve it against Git or prove that an adapter executed that exact revision; the caller must bind the SHA to the subject run.

Run the isolated evaluator tests with:

```sh
pytest -q tests/agent_evaluation
```

If the repository-wide `tests/conftest.py` cannot load because the local environment lacks the full project test dependencies, the evaluator-only fallback is:

```sh
PYTHONPATH=src:tests/agent_evaluation pytest --confcutdir=tests/agent_evaluation -q tests/agent_evaluation
```

The fallback deliberately excludes the root conftest; report that scope rather than presenting it as the repository-wide test command.

## E2E readiness and present limitation

The ten readiness stages are request, typed intent, strategy, Creative IR, plan, authorization, CommandBus, execution, independent verification, and durable evidence/receipt. A stage is `AVAILABLE` only when required typed observations and passing oracle dimensions exist; object presence alone is insufficient.

No approved Arena A runtime adapter is implemented or registered in this repository state. The CLI therefore uses `adapter=None`; it does not run the synthetic witness and does not fabricate an execution or verification chain. Tests that construct synthetic observations verify oracle behavior and readiness predicates only. Their `AVAILABLE` result is test-fixture evidence, not an Arena A E2E result.

The current adapter boundary is an explicit caller-supplied object with a non-empty `adapter_id` and an `observe` method returning `ObservedBehavior`. The package has no allowlist or cryptographic approval registry: caller approval and observation-source authenticity are out-of-band responsibilities. Until a reviewed adapter can safely invoke and independently collect Arena A evidence at the exact subject SHA, report the stages as `BLOCKED` or `DEFERRED` and do not claim E2E readiness.
