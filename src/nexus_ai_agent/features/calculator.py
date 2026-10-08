"""Safe arithmetic expression evaluator for NEXUS.

This module replaces the historical ``eval``-based calculator. Design
goals, in priority order:

1. **No code execution, ever.** The expression is parsed with
   :func:`ast.parse` (``mode="eval"``) and evaluated by a strict node
   whitelist. Attribute access, subscripts, strings, containers and
   every non-arithmetic node are rejected, so the classic ``eval``
   sandbox escapes (``().__class__``, ``__import__``, ...) cannot even
   be expressed in the accepted grammar.
2. **No resource abuse.** Expressions are bounded in length and node
   count, exponentiation and factorial are bounded, and results are
   magnitude-checked — inputs like ``9**9**9`` or ``factorial(10**9)``
   cannot hang or exhaust the bot process.
3. **Persian-first UX.** Persian/Arabic-Indic digits (۰-۹ / ٠-٩), the
   ``^`` exponent operator and the ``×``/``÷`` symbols are normalised
   before parsing.

The module is pure stdlib with no I/O, so it is trivially unit-testable
and safe to import from any layer of the stack.
"""

from __future__ import annotations

import ast
import math
import operator
from collections.abc import Callable
from typing import Any

#: Maximum normalised expression length (characters).
MAX_EXPRESSION_LENGTH = 200
#: Maximum number of AST nodes in one expression.
MAX_NODES = 128
#: Maximum recursion depth of the evaluator walk.
MAX_DEPTH = 64
#: Upper bound for integer exponents in ``**`` (``9 ** 1000`` ≈ 955 digits, cheap).
MAX_POW_EXPONENT = 1000
#: Largest integer result accepted (in bits); ~1233 decimal digits.
MAX_RESULT_BITS = 4096
#: Largest float result accepted.
MAX_FLOAT_MAGNITUDE = 1e308
#: ``math.factorial`` overflow guard for float consumers.
MAX_FACTORIAL = 170

#: Allowed function calls. Every entry is a pure math function.
FUNCTIONS: dict[str, Callable[..., Any]] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "pow": pow,
    "sum": sum,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "sinh": math.sinh,
    "cosh": math.cosh,
    "tanh": math.tanh,
    "sqrt": math.sqrt,
    "log": math.log,
    "log2": math.log2,
    "log10": math.log10,
    "exp": math.exp,
    "ceil": math.ceil,
    "floor": math.floor,
    "factorial": math.factorial,
    "gcd": math.gcd,
    "degrees": math.degrees,
    "radians": math.radians,
    "hypot": math.hypot,
}

#: Allowed constant names.
CONSTANTS: dict[str, float] = {
    "pi": math.pi,
    "e": math.e,
}

_BINARY_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

#: Persian/Arabic-Indic digits and friendly operator symbols → ASCII.
_NORMALIZATION_MAP: dict[str, str] = {
    "۰": "0",
    "۱": "1",
    "۲": "2",
    "۳": "3",
    "۴": "4",
    "۵": "5",
    "۶": "6",
    "۷": "7",
    "۸": "8",
    "۹": "9",
    "٠": "0",
    "١": "1",
    "٢": "2",
    "٣": "3",
    "٤": "4",
    "٥": "5",
    "٦": "6",
    "٧": "7",
    "٨": "8",
    "٩": "9",
    "^": "**",
    "×": "*",
    "÷": "/",
}


class CalculatorError(ValueError):
    """Raised for any expression the calculator refuses or cannot compute."""


class ExpressionTooComplex(CalculatorError):
    """Raised when an expression exceeds length/node/depth/magnitude bounds."""


class MathError(CalculatorError):
    """Raised for well-formed expressions that fail at runtime (e.g. 1/0)."""


def normalize_expression(expression: str) -> str:
    """Normalise Persian digits, ``^`` and ``×``/``÷`` to the accepted grammar."""
    translated = "".join(_NORMALIZATION_MAP.get(ch, ch) for ch in expression)
    return translated.strip()


def _check_pow_guard(base: Any, exponent: Any) -> None:
    """Reject exponentiations that would allocate absurd amounts of memory."""
    if isinstance(base, bool) or isinstance(exponent, bool):
        raise CalculatorError("boolean operands are not allowed")
    if isinstance(base, int) and isinstance(exponent, int):
        if abs(exponent) > MAX_POW_EXPONENT:
            raise ExpressionTooComplex(f"exponent too large (max {MAX_POW_EXPONENT})")
        if exponent > 0:
            # Rough digit estimate: digits ≈ exponent * log10(|base|).
            if base not in (0, 1, -1) and exponent * (base.bit_length() // 4 + 1) > MAX_RESULT_BITS:
                raise ExpressionTooComplex("power result would be too large")
    elif isinstance(base, float) or isinstance(exponent, float):
        # 10.0 ** 400 overflows to inf/OverflowError; 999999 digits of a
        # float pow is never useful. Bound both int and float exponents.
        if abs(exponent) > MAX_POW_EXPONENT:
            raise ExpressionTooComplex(f"exponent too large (max {MAX_POW_EXPONENT})")


def _check_result_magnitude(value: Any) -> None:
    """Bound the final result so a huge integer cannot be materialised lazily."""
    if isinstance(value, bool):
        raise CalculatorError("boolean results are not allowed")
    if isinstance(value, int) and value.bit_length() > MAX_RESULT_BITS:
        raise ExpressionTooComplex("result too large")
    if isinstance(value, float) and (math.isinf(value) or abs(value) > MAX_FLOAT_MAGNITUDE):
        raise ExpressionTooComplex("result too large")
    if isinstance(value, complex):
        raise CalculatorError("complex results are not supported")


class SafeCalculator:
    """Strict AST-whitelist arithmetic evaluator. Never executes code."""

    def evaluate(self, expression: str) -> int | float:
        """Evaluate *expression* and return an ``int`` or ``float``.

        Raises:
            CalculatorError: syntax error, disallowed construct, math error.
            ExpressionTooComplex: length/node/depth/magnitude bound violated.
        """
        if not isinstance(expression, str) or not expression.strip():
            raise CalculatorError("empty expression")
        normalized = normalize_expression(expression)
        if len(normalized) > MAX_EXPRESSION_LENGTH:
            raise ExpressionTooComplex(f"expression too long (max {MAX_EXPRESSION_LENGTH} chars)")

        try:
            tree = ast.parse(normalized, mode="eval")
        except SyntaxError as exc:
            raise CalculatorError(f"invalid expression: {exc.msg}") from None

        node_count = 0
        for _node in ast.walk(tree):
            node_count += 1
            if node_count > MAX_NODES:
                raise ExpressionTooComplex(f"expression too complex (max {MAX_NODES} nodes)")

        result = self._eval(tree.body, depth=0)
        _check_result_magnitude(result)
        return result

    # -- internals ---------------------------------------------------------

    def _eval(self, node: ast.expr, depth: int) -> Any:
        if depth > MAX_DEPTH:
            raise ExpressionTooComplex("expression too deeply nested")

        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise CalculatorError(f"constant {node.value!r} is not allowed")
            return node.value

        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = self._eval(node.operand, depth + 1)
            return +value if isinstance(node.op, ast.UAdd) else -value

        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPS:
            left = self._eval(node.left, depth + 1)
            right = self._eval(node.right, depth + 1)
            if isinstance(node.op, ast.Pow):
                _check_pow_guard(left, right)
            try:
                return _BINARY_OPS[type(node.op)](left, right)
            except ZeroDivisionError:
                raise MathError("division by zero") from None
            except (OverflowError, ValueError) as exc:
                raise MathError(str(exc) or "math error") from None

        if isinstance(node, ast.Name):
            if node.id in CONSTANTS:
                return CONSTANTS[node.id]
            raise CalculatorError(f"unknown name: {node.id}")

        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS and not node.keywords:
                args = [self._eval(arg, depth + 1) for arg in node.args]
                return self._call_function(node.func.id, args)
            raise CalculatorError("only whitelisted math calls are allowed")

        raise CalculatorError(f"disallowed expression construct: {type(node).__name__}")

    def _call_function(self, name: str, args: list[Any]) -> Any:
        func: Callable[..., Any] = FUNCTIONS[name]
        if name == "factorial":
            if len(args) != 1:
                raise CalculatorError("factorial takes exactly one argument")
            value = args[0]
            if isinstance(value, bool) or not isinstance(value, int):
                raise CalculatorError("factorial expects an integer")
            if value < 0 or value > MAX_FACTORIAL:
                raise CalculatorError(f"factorial argument must be 0..{MAX_FACTORIAL}")
            return func(value)
        if name == "pow":
            if len(args) == 2:
                _check_pow_guard(args[0], args[1])
            try:
                return func(*args)
            except (OverflowError, ValueError) as exc:
                raise MathError(str(exc) or "math error") from None
        try:
            return func(*args)
        except (OverflowError, ValueError, TypeError) as exc:
            raise MathError(str(exc) or "math error") from None


def format_result(value: int | float) -> str:
    """Human-friendly result formatting (trims float noise like ``0.30000000000000004``)."""
    if isinstance(value, float):
        return f"{value:.10g}"
    return str(value)


def evaluate_expression(expression: str) -> int | float:
    """Module-level convenience wrapper around :class:`SafeCalculator`."""
    return SafeCalculator().evaluate(expression)
