"""Independent Agent assurance and evaluation plane.

Public objects are typed evaluation contracts and pure oracle entry points;
no runtime authority is exposed by this package.
"""

from nexus_ai_agent.evaluation.corpus import (
    ADVERSARIAL_CASES,
    AGENT_FOUNDATION_SUITE,
    FAILURE_CASES,
    GOLDEN_CASES,
)
from nexus_ai_agent.evaluation.domain import (
    EvaluationCase,
    EvaluationDimension,
    EvaluationInput,
    EvaluationReport,
    EvaluationResult,
    EvaluationRun,
    EvaluationSuite,
    ExpectedBehavior,
    FailureCode,
    FailureFinding,
    ObservedBehavior,
    OracleResult,
    Verdict,
)
from nexus_ai_agent.evaluation.oracles import evaluate_observation
from nexus_ai_agent.evaluation.runner import SubjectAdapter, run_suite

__all__ = [
    "ADVERSARIAL_CASES",
    "AGENT_FOUNDATION_SUITE",
    "EvaluationCase",
    "EvaluationDimension",
    "EvaluationInput",
    "EvaluationReport",
    "EvaluationResult",
    "EvaluationRun",
    "EvaluationSuite",
    "ExpectedBehavior",
    "FAILURE_CASES",
    "FailureCode",
    "FailureFinding",
    "GOLDEN_CASES",
    "ObservedBehavior",
    "OracleResult",
    "SubjectAdapter",
    "Verdict",
    "evaluate_observation",
    "run_suite",
]
