"""Canonical job lifecycle for the Modular Monolith (task-178).

This package is the **Job Layer** of the one canonical chain:

    Command
      → Job
      → Runtime Execution
      → Artifact Verification
      → Result

It owns the state-machine contract, the side-effect boundary definition, the
artifact-verification contract, and the failure/retry classification
(``failure_semantics``: RETRYABLE/TERMINAL — no scheduler in this repo). It
sits ON TOP of the runtime truth (``creative/rendering/*`` LaneIR →
compiler → executor → measured artifact) and never redefines runtime
semantics: the verification evidence functions (probe / sha256) are the
runtime's own.
"""
