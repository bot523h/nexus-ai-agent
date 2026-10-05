"""Ratchet against handlers that swallow a failure or invent a default.

Context (board task-234). An AST sweep of every exception handler in ``src/``
found 74 that either swallowed the exception (``pass`` / ``continue``) or
returned a fabricated default (``[]``, ``{}``, ``0``, ``""``, ``False``,
``None``). Sixty-one of them did it **silently** — no log line, no re-raise — so
the runtime could not tell "this failed" from "there was nothing to do".

That is how two real defects stayed invisible:

* ``PhiAgent.moderate`` approved content whenever the verdict could not be
  parsed — a safety gate failing **open** (fixed).
* the storage layer reported an unreachable provider as an empty bucket and an
  unmeasurable quota as a confident ``0 / 2 GiB`` (fixed).

This test is the ratchet. A handler is allowed to degrade **only if it records
the failure** (a logging call) or re-raises. Every remaining silent one is
frozen in ``silent_failure_baseline.json`` with a written reason: new silent
handlers fail the build, and fixing a baselined one forces its entry out
(baseline entries that no longer match anything are a failure too).
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SRC = ROOT / "src" / "nexus_ai_agent"
BASELINE_PATH = Path(__file__).parent / "silent_failure_baseline.json"
APPROVAL = "FAILSAFE_APPROVED"

# Attributes/methods that count as "the failure was recorded".
_RECORDER_ATTRS = {
    "debug",
    "info",
    "warning",
    "error",
    "exception",
    "critical",
    "log",
    "warning_once",
}
_RECORDER_NAMES = {
    "log_lifecycle_event",
    "redacted_log",
    "emit",
    "_log_typed_failure",  # integrations/free_tools.py: records a typed degradation
}


@dataclass(frozen=True)
class Offender:
    """One exception handler that neither records nor re-raises."""

    file: str
    symbol: str
    action: str
    caught: str
    lineno: int = 0
    ordinal: int = 0  # disambiguates two identical handlers inside one function
    observable: bool = False  # logs the failure or re-raises → allowed without a baseline entry

    @property
    def key(self) -> str:
        """A key that survives line drift but follows the code it describes."""
        return f"{self.file}::{self.symbol}::{self.action}::{self.caught}#{self.ordinal}"


def _fabricated(value: ast.expr) -> str | None:
    """Describe ``value`` if it is a default the handler invented, else None."""
    if isinstance(value, ast.Constant):
        if value.value is None:
            return "None"
        if value.value is False:
            return "False"
        if value.value == 0 or value.value == 0.0:  # noqa: PLR2004 - literal zero by definition
            return "0"
        if value.value == "":
            return '""'
        return None
    if isinstance(value, ast.Dict):
        keys = {k.value for k in value.keys if isinstance(k, ast.Constant)}
        if keys & {"error", "success"}:
            # A structured failure report ({"success": False, "error": "why"})
            # is a *recorded* failure, not an invented success.
            return None
        if any(isinstance(node, ast.Name) for node in ast.walk(value)):
            return None  # built from real state, not invented
        return ast.unparse(value)[:40]
    if isinstance(value, (ast.List, ast.Set, ast.Tuple)):
        if any(isinstance(node, ast.Name) for node in ast.walk(value)):
            return None  # built from real state, not invented
        return ast.unparse(value)[:40]
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in {"list", "dict", "set", "tuple", "str", "int", "float"}
        and not value.args
    ):
        return f"{value.func.id}()"
    return None


def _records_failure(handler: ast.ExceptHandler) -> bool:
    for node in ast.walk(handler):
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in _RECORDER_ATTRS:
                return True
            if isinstance(func, ast.Name) and func.id in _RECORDER_NAMES:
                return True
    return False


def _handler_action(handler: ast.ExceptHandler) -> str | None:
    body = handler.body
    if len(body) == 1 and isinstance(body[0], ast.Pass):
        return "pass"
    if len(body) == 1 and isinstance(body[0], ast.Continue):
        return "continue"
    if body and isinstance(body[-1], ast.Return) and body[-1].value is not None:
        invented = _fabricated(body[-1].value)
        if invented is not None:
            return f"return {invented}"
    return None


class _Visitor(ast.NodeVisitor):
    def __init__(self, relpath: str, include_all: bool = False) -> None:
        self.relpath = relpath
        self.include_all = include_all  # True → also report handlers that record the failure
        self.offenders: list[Offender] = []
        self._stack: list[str] = []
        self._seen: dict[tuple[str, str, str], int] = {}

    def _visit_scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    visit_FunctionDef = _visit_scope
    visit_AsyncFunctionDef = _visit_scope

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        action = _handler_action(node)
        if action is not None:
            records = _records_failure(node)
            if self.include_all or not records:
                symbol = self._stack[-1] if self._stack else "<module>"
                caught = ast.unparse(node.type) if node.type else "Exception"
                identity = (symbol, action, caught)
                seen = self._seen.get(identity, 0)
                self._seen[identity] = seen + 1
                self.offenders.append(
                    Offender(
                        file=self.relpath,
                        symbol=symbol,
                        action=action,
                        caught=caught,
                        lineno=node.lineno,
                        ordinal=seen,
                        observable=records,
                    )
                )
        self.generic_visit(node)


def collect_handlers(include_all: bool = False) -> list[Offender]:
    """Every handler in ``src/`` that swallows or fabricates a default.

    With ``include_all`` the handlers that *record* the failure (log or
    re-raise) are reported too — that is how the baseline keeps its measured
    totals honest instead of quoting numbers from a report.
    """
    offenders: list[Offender] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        visitor = _Visitor(str(path.relative_to(ROOT)).replace("\\", "/"), include_all)
        visitor.visit(tree)
        offenders.extend(visitor.offenders)
    return offenders


def collect_offenders() -> list[Offender]:
    """Every handler in ``src/`` that swallows or fabricates without a trace."""
    return collect_handlers(include_all=False)


def load_baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


# One pass over src/ (≈0.5 s) shared by every test below; the architecture suite
# is expected to stay in the sub-second range, so nothing here re-scans per test.
OFFENDERS = collect_offenders()
OFFENDER_KEYS = {offender.key for offender in OFFENDERS}


def baseline_keys(baseline: dict) -> set[str]:
    return {
        f"{e['file']}::{e['symbol']}::{e['action']}::{e['caught']}#{e.get('ordinal', 0)}"
        for e in baseline["entries"]
    }


def test_baseline_is_approved() -> None:
    baseline = load_baseline()
    assert baseline["approval"] == APPROVAL, "the frozen baseline must carry its approval marker"


def test_no_new_silent_handlers() -> None:
    """A handler that degrades must leave a trace; new silent ones are not accepted."""
    known = baseline_keys(load_baseline())
    unknown = [o for o in OFFENDERS if o.key not in known]
    assert not unknown, "silent handlers outside the frozen baseline:\n" + "\n".join(
        f"  {o.key}" for o in unknown
    )


@pytest.mark.parametrize("entry_key", sorted(baseline_keys(load_baseline())))
def test_baseline_entry_is_still_justified(entry_key: str) -> None:
    """Ratchet: an entry whose handler is gone (fixed, or made observable) must be deleted."""
    assert entry_key in OFFENDER_KEYS, (
        f"{entry_key} is no longer silent — remove it from "
        f"{BASELINE_PATH.name} so the debt can only shrink"
    )
