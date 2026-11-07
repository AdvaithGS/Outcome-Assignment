"""A tiny, safe expression language for computed fields.

Stage 0 (schema analysis) writes a formula per computed field, e.g.
    months_between(lease_commencement_date, lease_expiration_date)
    round_each(div(mul(rent_schedule_monthly_rent, 12), rentable_square_footage), 2)
    sum(mul(rent_schedule_monthly_rent, months_each(rent_schedule_start_dates, rent_schedule_end_dates)))
    round(div(asking_price, units))
Names are operands (other field keys or helper quantities). The expression is parsed with `ast` and evaluated
by walking a whitelist of node types and functions: no `eval`, no attribute access, no comprehensions.
"""
from __future__ import annotations

import ast
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any


class FormulaError(ValueError):
    pass


def _d(x) -> date:
    if isinstance(x, date):
        return x
    if isinstance(x, str):
        return date.fromisoformat(x[:10])
    raise FormulaError(f"not a date: {x!r}")


def _num(x) -> float:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise FormulaError(f"not a number: {x!r}")
    return float(x)


def months_between(start, end) -> int:
    """Whole months from start to end, counting the end day (May 1, 1993 → Apr 30, 1998 = 60)."""
    s, e = _d(start), _d(end) + timedelta(days=1)
    m = (e.year - s.year) * 12 + (e.month - s.month)
    if e.day < s.day:
        m -= 1
    return m


def _elementwise(op):
    def f(a, b):
        if isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                raise FormulaError(f"list lengths differ: {len(a)} vs {len(b)}")
            return [op(_num(x), _num(y)) for x, y in zip(a, b)]
        if isinstance(a, list):
            return [op(_num(x), _num(b)) for x in a]
        if isinstance(b, list):
            return [op(_num(a), _num(y)) for y in b]
        return op(_num(a), _num(b))
    return f


def _div(x, y):
    if y == 0:
        raise FormulaError("division by zero")
    return x / y


def _round(x, n=0):
    q = Decimal(1).scaleb(-int(n))
    r = float(Decimal(str(_num(x))).quantize(q, rounding=ROUND_HALF_UP))
    return int(r) if int(n) == 0 else r


FUNCTIONS = {
    "add": _elementwise(lambda a, b: a + b),
    "sub": _elementwise(lambda a, b: a - b),
    "mul": _elementwise(lambda a, b: a * b),
    "div": _elementwise(_div),
    "sum": lambda xs: sum(_num(x) for x in xs),
    "len": lambda xs: len(xs),
    "min": lambda xs: min(_num(x) for x in xs),
    "max": lambda xs: max(_num(x) for x in xs),
    "abs": lambda x: abs(_num(x)),
    "round": _round,
    "round_each": lambda xs, n=0: [_round(x, n) for x in xs],
    "count_nonzero": lambda xs: sum(1 for x in xs if x not in (None, 0, 0.0, "")),
    "months_between": months_between,
    "months_each": lambda starts, ends: [months_between(s, e) for s, e in zip(starts, ends)],
    "year": lambda d: _d(d).year,
}

FUNCTION_DOC = """add(a,b) sub(a,b) mul(a,b) div(a,b)  (element-wise when an argument is a list)
sum(list) len(list) min(list) max(list) abs(x) count_nonzero(list)
round(x, digits=0) round_each(list, digits=0)   (half-up rounding)
months_between(start_date, end_date)            (whole months, end day inclusive)
months_each(start_dates, end_dates)             (list of months_between)
year(date)
Operators + - * / work on numbers. Names are operands. Numeric literals are allowed."""

KEYWORDS = {"round": ("digits",), "round_each": ("digits",)}   # the only named arguments allowed

_BINOPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b, ast.Div: _div}


def parse(formula: str) -> ast.Expression:
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"syntax: {exc.msg}") from exc
    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.Name, ast.Load, ast.List, ast.USub, ast.UAdd)):
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            continue
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            continue
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            continue
        if type(node) in _BINOPS:
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS and all(
                kw.arg in KEYWORDS.get(node.func.id, ()) for kw in node.keywords):
            continue
        if isinstance(node, ast.keyword):
            continue
        raise FormulaError(f"not allowed: {type(node).__name__}")
    return tree


def names(formula: str) -> set[str]:
    tree = parse(formula)
    calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call)}
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} - calls


def evaluate(formula: str, values: dict[str, Any]) -> Any:
    tree = parse(formula)

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in values or values[node.id] is None or values[node.id] == []:
                raise FormulaError(f"missing operand: {node.id}")
            return values[node.id]
        if isinstance(node, ast.List):
            return [ev(e) for e in node.elts]
        if isinstance(node, ast.UnaryOp):
            v = _num(ev(node.operand))
            return -v if isinstance(node.op, ast.USub) else v
        if isinstance(node, ast.BinOp):
            return _BINOPS[type(node.op)](_num(ev(node.left)), _num(ev(node.right)))
        if isinstance(node, ast.Call):
            args = [ev(a) for a in node.args] + [ev(kw.value) for kw in node.keywords]   # digits= is the 2nd arg
            return FUNCTIONS[node.func.id](*args)
        raise FormulaError(f"not allowed: {type(node).__name__}")

    try:
        return ev(tree)
    except FormulaError:
        raise
    except (TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
        raise FormulaError(str(exc)) from exc
