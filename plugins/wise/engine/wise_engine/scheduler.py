from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .constants import TERMINAL_STEP, TRIGGER_RULES

UNKNOWN = object()
UNDEFINED = object()
_BARE_ROOTS = ("outputs", "inputs", "answers")
JS_WHITESPACE = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_SPACE = re.compile(f"[{re.escape(JS_WHITESPACE)}]")


def step_by_id(steps: Sequence[dict[str, Any]], identifier: str) -> dict[str, Any] | None:
    return next((step for step in steps if step["id"] == identifier), None)


def is_trigger_rule(rule: str) -> bool:
    return rule in TRIGGER_RULES


def trigger_rule_satisfied(rule: str, deps: Sequence[Mapping[str, Any]]) -> dict[str, bool]:
    if not deps:
        return {"runnable": True, "skip": False}
    statuses = [dep["status"] for dep in deps]
    completed = statuses.count("completed")
    failed = statuses.count("failed")
    all_terminal = all(status in TERMINAL_STEP for status in statuses)
    if rule == "one-success":
        return {"runnable": completed > 0, "skip": completed == 0 and all_terminal}
    if rule == "all-done":
        return {"runnable": all_terminal, "skip": False}
    if rule == "none-failed":
        return {"runnable": all_terminal and failed == 0, "skip": failed > 0}
    if rule == "none-failed-min-one-success":
        return {"runnable": all_terminal and failed == 0 and completed > 0, "skip": failed > 0}
    return {
        "runnable": completed == len(statuses),
        "skip": any(status in {"failed", "skipped", "cancelled"} for status in statuses),
    }


def number_text(value: int | float) -> str:
    number = float(value)
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "Infinity" if number > 0 else "-Infinity"
    if number == 0:
        return "0"
    text = repr(number)
    if 1e-6 <= abs(number) < 1e21:
        decimal = format(Decimal(text), "f")
        return decimal.rstrip("0").rstrip(".") if "." in decimal else decimal
    mantissa, exponent = text.split("e")
    return f"{mantissa.removesuffix('.0')}e{int(exponent):+d}"


def js_string(value: Any) -> str:
    if value is UNDEFINED:
        return "undefined"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return number_text(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ",".join(
            "" if item is None or item is UNDEFINED else js_string(item) for item in value
        )
    return "[object Object]"


def truthy(value: Any) -> bool:
    if value is UNDEFINED or value is None:
        return False
    if isinstance(value, str):
        return value != ""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0 and not math.isnan(value)
    if isinstance(value, list):
        return len(value) > 0
    return True


def _values_equal(left: Any, right: Any) -> bool:
    if left is right:
        return True
    if left is UNDEFINED or right is UNDEFINED or left is None or right is None:
        return False
    if isinstance(left, (dict, list)) or isinstance(right, (dict, list)):
        return False
    return js_string(left) == js_string(right)


def _field(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, UNDEFINED)
    if isinstance(value, list) and key == "length":
        return len(value)
    return UNDEFINED


def _owner_of(head: str, scope: Mapping[str, Any]) -> Any:
    if head in scope:
        return scope
    for root in _BARE_ROOTS:
        bucket = scope.get(root)
        if _field(bucket, head) is not UNDEFINED:
            return bucket
        if isinstance(bucket, Mapping) and head in bucket:
            return bucket
    return UNDEFINED


def resolve_identifier(name: str, scope: Mapping[str, Any]) -> Any:
    path = name.split(".")
    value = _owner_of(path[0], scope)
    for segment in path:
        value = _field(value, segment)
    return value


def resolve_identifier_partial(name: str, scope: Mapping[str, Any]) -> Any:
    path = name.split(".")
    value = _owner_of(path[0], scope)
    if value is UNDEFINED:
        return UNKNOWN
    for segment in path:
        if isinstance(value, Mapping) and segment in value:
            value = value[segment]
        elif isinstance(value, list) and segment == "length":
            value = len(value)
        else:
            return UNKNOWN
    return value


def _when_error(expr: str, message: str, position: int) -> ValueError:
    offset = len(expr[:position].encode("utf-16-le", errors="surrogatepass")) // 2
    return ValueError(
        f"when: {message} at position {offset} in {json.dumps(expr, ensure_ascii=False)}"
    )


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    position: int


def _tokenize(expr: str) -> list[_Token]:
    tokens = []
    position = 0
    while position < len(expr):
        char = expr[position]
        start = position
        if _SPACE.fullmatch(char):
            position += 1
            continue
        if char in "()":
            tokens.append(_Token("lparen" if char == "(" else "rparen", char, start))
            position += 1
        elif char in "\"'":
            end = expr.find(char, position + 1)
            if end < 0:
                raise _when_error(expr, "unterminated string", start)
            tokens.append(_Token("string", expr[position + 1 : end], start))
            position = end + 1
        elif "0" <= char <= "9":
            end = position
            while end < len(expr) and expr[end] in "0123456789.":
                end += 1
            text = expr[position:end]
            if re.fullmatch(r"[0-9]+(\.[0-9]+)?", text) is None:
                raise _when_error(expr, f"bad number {text}", start)
            tokens.append(_Token("number", text, start))
            position = end
        elif re.fullmatch(r"[A-Za-z_]", char):
            end = position
            while end < len(expr) and re.fullmatch(r"[A-Za-z0-9_.]", expr[end]):
                end += 1
            text = expr[position:end]
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*", text) is None:
                raise _when_error(expr, f"bad identifier {text}", start)
            tokens.append(_Token("ident", text, start))
            position = end
        else:
            two = expr[position : position + 2]
            if two in {"==", "!=", "&&", "||"}:
                tokens.append(_Token("op", two, start))
                position += 2
            elif char == "!":
                tokens.append(_Token("op", char, start))
                position += 1
            else:
                encoded = json.dumps(char, ensure_ascii=False)
                if ord(char) > 0xFFFF:
                    high = char.encode("utf-16-le")[:2].decode("utf-16-le", errors="surrogatepass")
                    encoded = json.dumps(high)
                raise _when_error(expr, f"unexpected character {encoded}", start)
    tokens.append(_Token("end", "", len(expr)))
    return tokens


def _truthy3(value: Any) -> bool | None:
    return None if value is UNKNOWN else truthy(value)


class _Evaluator:
    def __init__(self, expr: str, resolve: Callable[[str], Any]) -> None:
        self.expr = expr
        self.resolve = resolve
        self.tokens = _tokenize(expr)
        self.index = 0

    def peek(self) -> _Token:
        return self.tokens[self.index]

    def take(self) -> _Token:
        token = self.peek()
        self.index += 1
        return token

    def is_op(self, value: str) -> bool:
        return self.peek().kind == "op" and self.peek().value == value

    def primary(self) -> Any:
        token = self.take()
        if token.kind == "lparen":
            value = self.parse_or()
            if self.peek().kind != "rparen":
                raise _when_error(self.expr, "expected ')'", self.peek().position)
            self.index += 1
            return value
        if token.kind == "string":
            return token.value
        if token.kind == "number":
            return float(token.value)
        if token.kind == "ident":
            if token.value in {"true", "false"}:
                return token.value == "true"
            return self.resolve(token.value)
        if token.kind == "end":
            raise _when_error(self.expr, "unexpected end of expression", token.position)
        raise _when_error(self.expr, f"unexpected {json.dumps(token.value)}", token.position)

    def unary(self) -> Any:
        if self.is_op("!"):
            self.index += 1
            value = _truthy3(self.unary())
            return UNKNOWN if value is None else not value
        return self.primary()

    def equality(self) -> Any:
        left = self.unary()
        while self.is_op("==") or self.is_op("!="):
            operation = self.take().value
            right = self.unary()
            if left is UNKNOWN or right is UNKNOWN:
                left = UNKNOWN
            else:
                equal = _values_equal(left, right)
                left = equal if operation == "==" else not equal
        return left

    def parse_and(self) -> Any:
        left = self.equality()
        while self.is_op("&&"):
            self.index += 1
            lhs, rhs = _truthy3(left), _truthy3(self.equality())
            left = (
                False
                if lhs is False or rhs is False
                else (True if lhs is True and rhs is True else UNKNOWN)
            )
        return left

    def parse_or(self) -> Any:
        left = self.parse_and()
        while self.is_op("||"):
            self.index += 1
            lhs, rhs = _truthy3(left), _truthy3(self.parse_and())
            left = (
                True
                if lhs is True or rhs is True
                else (False if lhs is False and rhs is False else UNKNOWN)
            )
        return left

    def evaluate(self) -> bool | None:
        result = self.parse_or()
        tail = self.peek()
        if tail.kind != "end":
            raise _when_error(self.expr, f"unexpected {json.dumps(tail.value)}", tail.position)
        return _truthy3(result)


def evaluate_when(expr: str, scope: Mapping[str, Any]) -> bool:
    return _Evaluator(expr, lambda name: resolve_identifier(name, scope)).evaluate() is True


def evaluate_when_partial(expr: str, scope: Mapping[str, Any]) -> bool | None:
    return _Evaluator(expr, lambda name: resolve_identifier_partial(name, scope)).evaluate()


def when_scope(state: Mapping[str, Any]) -> dict[str, Any]:
    return {key: state.get(key, UNDEFINED) for key in _BARE_ROOTS}


def when_conditions(when: Any = UNDEFINED) -> list[str]:
    if isinstance(when, list):
        return [js_string(condition) for condition in when]
    if when is UNDEFINED or when is None or when == "":
        return []
    return [js_string(when)]


def next_wave(definition: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    ready, skipped, warnings = [], [], []
    scope = when_scope(state)
    for step in definition["steps"]:
        current = state["steps"].get(step["id"])
        if current is None or current.get("status") != "pending":
            continue
        deps = [
            {"id": key, "status": state["steps"][key].get("status", UNDEFINED)}
            for key in (step.get("depends_on") or [])
            if state["steps"].get(key) is not None
        ]
        rule = step.get("trigger-rule")
        if rule is None:
            rule = "all-success"
        verdict = trigger_rule_satisfied(rule, deps)
        if verdict["skip"]:
            summary = ", ".join(f"{dep['id']}={dep['status']}" for dep in deps)
            skipped.append(
                {"id": step["id"], "reason": f"trigger-rule {rule} not satisfied: {summary}"}
            )
            continue
        if not verdict["runnable"]:
            continue
        for condition in when_conditions(step.get("when")):
            try:
                holds = evaluate_when(condition, scope)
            except ValueError as error:
                warnings.append(f"when-unparseable:{step['id']}:{error}")
                continue
            if not holds:
                skipped.append({"id": step["id"], "reason": f"when: {condition} is false"})
                break
        else:
            ready.append(step)
    statuses = [step.get("status", UNDEFINED) for step in state["steps"].values()]
    done = failed = False
    if not ready and not skipped and "running" not in statuses:
        if "failed" in statuses:
            done = failed = True
        elif all(status in TERMINAL_STEP for status in statuses):
            done = True
        elif "pending" in statuses:
            done = failed = True
    return {
        "ready": ready,
        "skipped": skipped,
        "done": done,
        "failed": failed,
        "warnings": warnings,
    }
