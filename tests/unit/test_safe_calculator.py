"""Behavioural + security tests for the AST-safe calculator.

The calculator previously used ``eval(expr, {"__builtins__": {}}, ...)``
with a regex pre-filter — a pattern that invites sandbox escapes and
resource-abuse (``9**9**9``). These tests pin down the contract of the
replacement (:mod:`nexus_ai_agent.features.calculator`): real arithmetic,
Persian-first input, hard security rejections, and DoS bounds.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.features.calculator import (
    CalculatorError,
    ExpressionTooComplex,
    MathError,
    SafeCalculator,
    evaluate_expression,
    format_result,
    normalize_expression,
)
from nexus_ai_agent.features.tools import Calculator

# ═══════════════════════════════════════════════════════════════════════
# Correct arithmetic (behavioural: /calc must actually compute)
# ═══════════════════════════════════════════════════════════════════════


class TestArithmetic:
    def test_precedence(self) -> None:
        assert evaluate_expression("2+2*3") == 8

    def test_parentheses(self) -> None:
        assert evaluate_expression("(2+2)*3") == 12

    def test_float_division(self) -> None:
        assert evaluate_expression("10/4") == 2.5

    def test_floor_division_and_mod(self) -> None:
        assert evaluate_expression("10//3") == 3
        assert evaluate_expression("10 % 3") == 1

    def test_caret_is_exponent(self) -> None:
        assert evaluate_expression("2^10") == 1024

    def test_starstar_is_exponent(self) -> None:
        assert evaluate_expression("2**10") == 1024

    def test_unary_minus(self) -> None:
        assert evaluate_expression("-5+3") == -2

    def test_float_noise_trimmed_in_ui(self) -> None:
        assert format_result(evaluate_expression("0.1+0.2")) == "0.3"

    def test_int_stays_int(self) -> None:
        assert isinstance(evaluate_expression("7/2"), float)
        assert isinstance(evaluate_expression("4/2"), float)
        assert evaluate_expression("2+2") == 4

    def test_constants(self) -> None:
        assert abs(evaluate_expression("pi") - 3.141592653589793) < 1e-12
        assert abs(evaluate_expression("e") - 2.718281828459045) < 1e-12


class TestFunctions:
    def test_sqrt(self) -> None:
        assert evaluate_expression("sqrt(144)") == 12.0

    def test_trig(self) -> None:
        assert abs(evaluate_expression("sin(pi/2)") - 1.0) < 1e-12
        assert abs(evaluate_expression("cos(0)") - 1.0) < 1e-12

    def test_min_max(self) -> None:
        assert evaluate_expression("min(3,1,2)") == 1
        assert evaluate_expression("max(3,1,2)") == 3

    def test_factorial(self) -> None:
        assert evaluate_expression("factorial(5)") == 120

    def test_gcd(self) -> None:
        assert evaluate_expression("gcd(48, 18)") == 6

    def test_round(self) -> None:
        assert evaluate_expression("round(3.7)") == 4

    def test_unknown_function_rejected(self) -> None:
        with pytest.raises(CalculatorError):
            evaluate_expression("exec('x=1')")

    def test_unknown_name_rejected(self) -> None:
        with pytest.raises(CalculatorError):
            evaluate_expression("abc")


# ═══════════════════════════════════════════════════════════════════════
# Persian-first input
# ═══════════════════════════════════════════════════════════════════════


class TestPersianInput:
    def test_persian_digits(self) -> None:
        assert evaluate_expression("۱۲ + ۸") == 20

    def test_arabic_indic_digits(self) -> None:
        assert evaluate_expression("٣ * ٤") == 12

    def test_persian_multiply_symbol(self) -> None:
        assert evaluate_expression("۲ × ۳") == 6

    def test_persian_divide_symbol(self) -> None:
        assert evaluate_expression("۸ ÷ ۲") == 4.0

    def test_normalize_strips_whitespace(self) -> None:
        assert normalize_expression("  2+2 ") == "2+2"


# ═══════════════════════════════════════════════════════════════════════
# Security: classic eval escapes must be unrepresentable
# ═══════════════════════════════════════════════════════════════════════


class TestSecurity:
    def test_attr_access_rejected(self) -> None:
        with pytest.raises(CalculatorError):
            evaluate_expression("().__class__")

    def test_subclass_chain_rejected(self) -> None:
        with pytest.raises(CalculatorError):
            evaluate_expression("().__class__.__bases__")

    def test_import_rejected(self) -> None:
        with pytest.raises(CalculatorError):
            evaluate_expression("__import__('os')")

    def test_string_literal_rejected(self) -> None:
        with pytest.raises(CalculatorError):
            evaluate_expression("'a'+'b'")

    def test_string_concat_with_name_rejected(self) -> None:
        with pytest.raises(CalculatorError):
            evaluate_expression("str(1)")

    def test_list_literal_rejected(self) -> None:
        with pytest.raises(CalculatorError):
            evaluate_expression("sum([1,2,3])")

    def test_subscript_rejected(self) -> None:
        with pytest.raises(CalculatorError):
            evaluate_expression("min(3,1)[0]")

    def test_lambda_rejected(self) -> None:
        with pytest.raises(CalculatorError):
            evaluate_expression("(lambda: 1)()")

    def test_bool_constant_rejected(self) -> None:
        with pytest.raises(CalculatorError):
            evaluate_expression("True+1")

    def test_keyword_call_rejected(self) -> None:
        with pytest.raises(CalculatorError):
            evaluate_expression("round(1, ndigits=0)")


# ═══════════════════════════════════════════════════════════════════════
# DoS bounds: the process must stay responsive
# ═══════════════════════════════════════════════════════════════════════


class TestDoSBoundaries:
    def test_tower_of_powers_rejected(self) -> None:
        with pytest.raises(ExpressionTooComplex):
            evaluate_expression("9**9**9")

    def test_huge_integer_exponent_rejected(self) -> None:
        with pytest.raises(ExpressionTooComplex):
            evaluate_expression("2 ** 10**9")

    def test_huge_float_exponent_rejected(self) -> None:
        with pytest.raises(ExpressionTooComplex):
            evaluate_expression("1.5 ** 999999")

    def test_huge_factorial_rejected(self) -> None:
        with pytest.raises(CalculatorError):
            evaluate_expression("factorial(1000)")

    def test_negative_factorial_rejected(self) -> None:
        with pytest.raises(CalculatorError):
            evaluate_expression("factorial(-1)")

    def test_overflowing_pow_rejected(self) -> None:
        # 10.0 ** 400 overflows float → must error, never return inf.
        with pytest.raises(CalculatorError):
            evaluate_expression("10.0 ** 400")

    def test_inf_result_rejected(self) -> None:
        with pytest.raises(ExpressionTooComplex):
            evaluate_expression("1e308 * 10")

    def test_long_expression_rejected(self) -> None:
        with pytest.raises(ExpressionTooComplex):
            evaluate_expression("1+" * 500 + "1")

    def test_deeply_nested_rejected(self) -> None:
        with pytest.raises(ExpressionTooComplex):
            evaluate_expression("(" * 100 + "1" + ")" * 100)

    def test_empty_rejected(self) -> None:
        with pytest.raises(CalculatorError):
            evaluate_expression("   ")


class TestMathErrors:
    def test_division_by_zero(self) -> None:
        with pytest.raises(MathError):
            evaluate_expression("1/0")

    def test_sqrt_of_negative(self) -> None:
        with pytest.raises(MathError):
            evaluate_expression("sqrt(-1)")

    def test_asin_out_of_domain(self) -> None:
        with pytest.raises(MathError):
            evaluate_expression("asin(2)")

    def test_syntax_error(self) -> None:
        with pytest.raises(CalculatorError):
            evaluate_expression("2+")


# ═══════════════════════════════════════════════════════════════════════
# UI wrapper (features/tools.Calculator) keeps the Persian UX contract
# ═══════════════════════════════════════════════════════════════════════


class TestCalculatorWrapper:
    def test_success_message(self) -> None:
        assert Calculator().evaluate("2+2*3") == "🧮 2+2*3 = 8"

    def test_math_error_message(self) -> None:
        assert "خطا در محاسبه" in Calculator().evaluate("1/0")

    def test_invalid_message(self) -> None:
        assert Calculator().evaluate("__import__('os')") == "❌ عبارت نامعتبر است."

    def test_too_complex_message(self) -> None:
        assert "نامعتبر" in Calculator().evaluate("9**9**9")

    def test_no_bare_eval_in_engine(self) -> None:
        # Regression guard: the engine must not call bare eval() (the
        # historical RCE vector). "_eval" is our own walker method.
        import inspect
        import re

        source = inspect.getsource(SafeCalculator)
        assert re.search(r"(?<![\w.])eval\(", source) is None
