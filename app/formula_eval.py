"""
A small, safe Excel formula evaluator.

It covers what a control book typically uses: cell references, ranges,
+ - * / ^, parentheses, and SUM / AVERAGE / MIN / MAX / ABS / ROUND / IF / IFERROR.
Anything it does not understand raises FormulaError, which the checker reports
instead of guessing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from openpyxl.utils import column_index_from_string, get_column_letter


class FormulaError(Exception):
    pass


class RefError(FormulaError):
    pass


TOKEN_RE = re.compile(
    r"""\s*(?:
        (?P<err>\#REF!|\#DIV/0!|\#N/A|\#VALUE!|\#NAME\?)|
        (?P<range>\$?[A-Z]{1,3}\$?\d+:\$?[A-Z]{1,3}\$?\d+)|
        (?P<ref>\$?[A-Z]{1,3}\$?\d+)(?![A-Z(])|
        (?P<num>\d+\.?\d*(?:[eE][+-]?\d+)?%?)|
        (?P<func>[A-Z][A-Z0-9.]*)\s*\(|
        (?P<str>"[^"]*")|
        (?P<op><>|<=|>=|[-+*/^(),=<>&])
    )""",
    re.VERBOSE,
)

CELL_RE = re.compile(r"\$?([A-Z]{1,3})\$?(\d+)")


def split_ref(ref: str) -> tuple[str, int]:
    m = CELL_RE.fullmatch(ref)
    if not m:
        raise FormulaError(f"bad reference {ref}")
    return m.group(1), int(m.group(2))


def expand_range(a: str, b: str) -> list[str]:
    ca, ra = split_ref(a)
    cb, rb = split_ref(b)
    c1, c2 = sorted([column_index_from_string(ca), column_index_from_string(cb)])
    r1, r2 = sorted([ra, rb])
    return [f"{get_column_letter(c)}{r}" for c in range(c1, c2 + 1) for r in range(r1, r2 + 1)]


def tokenize(formula: str) -> list[tuple[str, str]]:
    s = formula[1:] if formula.startswith("=") else formula
    pos, out = 0, []
    while pos < len(s):
        if s[pos:].strip() == "":
            break
        m = TOKEN_RE.match(s, pos)
        if not m:
            raise FormulaError(f"cannot parse near '{s[pos:pos + 12]}'")
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        pos = m.end()
    return out


def references(formula: str) -> list[str]:
    """All single cells a formula reads, in order, ranges expanded. Duplicates kept."""
    refs = []
    for kind, val in tokenize(formula):
        if kind == "ref":
            refs.append(val.replace("$", ""))
        elif kind == "range":
            a, b = val.replace("$", "").split(":")
            refs.extend(expand_range(a, b))
    return refs


def has_error_literal(formula: str) -> bool:
    return any(k == "err" for k, _ in tokenize(formula))


def relative_signature(formula: str, host: str) -> str:
    """Formula rewritten with row/col offsets from the host cell (like R1C1 notation).

    Two cells whose formulas do 'the same thing' relative to their position share a signature.
    """
    hc, hr = split_ref(host)
    hci = column_index_from_string(hc)
    parts = []
    for kind, val in tokenize(formula):
        if kind in ("ref", "range"):
            cells = val.replace("$", "").split(":")
            rel = []
            for cell in cells:
                c, r = split_ref(cell)
                rel.append(f"R[{r - hr}]C[{column_index_from_string(c) - hci}]")
            parts.append(":".join(rel))
        else:
            parts.append(val)
    return "".join(parts)


# ------------------------------------------------------------------ parser / evaluator

@dataclass
class Node:
    kind: str
    value: object = None
    children: list | None = None


class Parser:
    def __init__(self, tokens):
        self.t = tokens
        self.i = 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self, val=None):
        tok = self.peek()
        if val is not None and tok[1] != val:
            raise FormulaError(f"expected '{val}'")
        self.i += 1
        return tok

    def parse(self):
        node = self.compare()
        if self.i != len(self.t):
            raise FormulaError("unexpected trailing tokens")
        return node

    def compare(self):
        node = self.additive()
        while self.peek()[1] in ("=", "<>", "<", ">", "<=", ">="):
            op = self.take()[1]
            node = Node("bin", op, [node, self.additive()])
        return node

    def additive(self):
        node = self.term()
        while self.peek()[1] in ("+", "-", "&"):
            op = self.take()[1]
            node = Node("bin", op, [node, self.term()])
        return node

    def term(self):
        node = self.power()
        while self.peek()[1] in ("*", "/"):
            op = self.take()[1]
            node = Node("bin", op, [node, self.power()])
        return node

    def power(self):
        node = self.unary()
        while self.peek()[1] == "^":
            self.take()
            node = Node("bin", "^", [node, self.unary()])
        return node

    def unary(self):
        if self.peek()[1] in ("-", "+"):
            op = self.take()[1]
            n = self.unary()
            return Node("neg", None, [n]) if op == "-" else n
        return self.atom()

    def atom(self):
        kind, val = self.peek()
        if kind == "num":
            self.take()
            return Node("num", float(val[:-1]) / 100 if val.endswith("%") else float(val))
        if kind == "str":
            self.take()
            return Node("str", val[1:-1])
        if kind == "ref":
            self.take()
            return Node("ref", val.replace("$", ""))
        if kind == "range":
            self.take()
            a, b = val.replace("$", "").split(":")
            return Node("range", (a, b))
        if kind == "err":
            self.take()
            return Node("err", val)
        if kind == "func":
            self.take()
            args = []
            if self.peek()[1] != ")":
                args.append(self.compare())
                while self.peek()[1] == ",":
                    self.take()
                    args.append(self.compare())
            self.take(")")
            return Node("func", val.upper(), args)
        if val == "(":
            self.take()
            n = self.compare()
            self.take(")")
            return n
        raise FormulaError(f"unexpected token {val}")


class Evaluator:
    """Evaluates formulas against a value lookup function get(cell) -> number."""

    def __init__(self, get):
        self.get = get

    def eval_formula(self, formula: str):
        return self.ev(Parser(tokenize(formula)).parse())

    def flat(self, node):
        if node.kind == "range":
            return [self.num(self.get(c)) for c in expand_range(*node.value)]
        return [self.ev(node)]

    @staticmethod
    def num(v):
        if v is None or v == "":
            return 0.0
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str) and v.startswith("#"):
            raise RefError(v)
        try:
            return float(v)
        except (TypeError, ValueError):
            raise FormulaError(f"non-numeric value '{v}'")

    def ev(self, n: Node):
        k = n.kind
        if k == "num" or k == "str":
            return n.value
        if k == "err":
            raise RefError(n.value)
        if k == "ref":
            return self.num(self.get(n.value))
        if k == "range":
            raise FormulaError("range used outside a function")
        if k == "neg":
            return -self.ev(n.children[0])
        if k == "bin":
            a = self.ev(n.children[0])
            b = self.ev(n.children[1])
            op = n.value
            if op == "+": return a + b
            if op == "-": return a - b
            if op == "*": return a * b
            if op == "/":
                if b == 0:
                    raise RefError("#DIV/0!")
                return a / b
            if op == "^": return a ** b
            if op == "&": return f"{a}{b}"
            if op == "=": return a == b
            if op == "<>": return a != b
            if op == "<": return a < b
            if op == ">": return a > b
            if op == "<=": return a <= b
            if op == ">=": return a >= b
        if k == "func":
            f, args = n.value, n.children
            if f == "IFERROR":
                try:
                    return self.ev(args[0])
                except FormulaError:
                    return self.ev(args[1])
            if f == "IF":
                cond = self.ev(args[0])
                if cond:
                    return self.ev(args[1])
                return self.ev(args[2]) if len(args) > 2 else False
            vals = [v for a in args for v in self.flat(a)]
            if f == "SUM": return sum(vals)
            if f == "AVERAGE": return sum(vals) / len(vals) if vals else 0.0
            if f == "MIN": return min(vals)
            if f == "MAX": return max(vals)
            if f == "ABS": return abs(vals[0])
            if f == "ROUND": return round(vals[0], int(vals[1]) if len(vals) > 1 else 0)
            raise FormulaError(f"function {f} not supported")
        raise FormulaError(f"cannot evaluate {k}")
