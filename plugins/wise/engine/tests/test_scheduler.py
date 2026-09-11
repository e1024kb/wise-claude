from __future__ import annotations

import copy
import math
from typing import Any

import pytest

from wise_engine.scheduler import (
    TRIGGER_RULES,
    UNKNOWN,
    UNDEFINED,
    evaluate_when,
    evaluate_when_partial,
    is_trigger_rule,
    next_wave,
    resolve_identifier,
    resolve_identifier_partial,
    step_by_id,
    trigger_rule_satisfied,
    truthy,
    when_conditions,
)


def state(
    statuses: dict[str, str], outputs: dict[str, Any] | None = None, **extra: Any
) -> dict[str, Any]:
    return {
        "steps": {key: {"status": value, "attempts": 0} for key, value in statuses.items()},
        "outputs": outputs or {},
        "inputs": {},
        "answers": {},
        **extra,
    }


def definition(*steps: dict[str, Any]) -> dict[str, Any]:
    return {"version": 2, "name": "t", "steps": list(steps)}


def bash(identifier: str, **extra: Any) -> dict[str, Any]:
    return {"id": identifier, "type": "bash", "run": "true", **extra}


@pytest.mark.parametrize(
    "rule,statuses,expected",
    [
        ("all-success", ("completed", "completed"), (True, False)),
        ("all-success", ("completed", "failed"), (False, True)),
        ("all-success", ("completed", "skipped"), (False, True)),
        ("all-success", ("completed", "cancelled"), (False, True)),
        ("all-success", ("completed", "running"), (False, False)),
        ("one-success", ("completed", "pending"), (True, False)),
        ("one-success", ("failed", "skipped"), (False, True)),
        ("one-success", ("failed", "running"), (False, False)),
        ("all-done", ("completed", "failed"), (True, False)),
        ("all-done", ("completed", "running"), (False, False)),
        ("none-failed-min-one-success", ("completed", "skipped"), (True, False)),
        ("none-failed-min-one-success", ("completed", "failed"), (False, True)),
        ("none-failed-min-one-success", ("skipped", "cancelled"), (False, False)),
        ("none-failed-min-one-success", ("completed", "running"), (False, False)),
        ("none-failed", ("completed", "completed"), (True, False)),
        ("none-failed", ("completed", "skipped"), (True, False)),
        ("none-failed", ("skipped", "skipped"), (True, False)),
        ("none-failed", ("skipped", "cancelled"), (True, False)),
        ("none-failed", ("completed", "failed"), (False, True)),
        ("none-failed", ("failed", "running"), (False, True)),
        ("none-failed", ("completed", "running"), (False, False)),
        ("none-failed", ("completed", "pending"), (False, False)),
    ],
)
def test_trigger_rule_truth_table(
    rule: str, statuses: tuple[str, ...], expected: tuple[bool, bool]
) -> None:
    assert trigger_rule_satisfied(rule, [{"status": status} for status in statuses]) == {
        "runnable": expected[0],
        "skip": expected[1],
    }


def test_trigger_registry_empty_dependencies_and_unknown_rule() -> None:
    assert set(TRIGGER_RULES) == {
        "all-success",
        "one-success",
        "all-done",
        "none-failed",
        "none-failed-min-one-success",
    }
    for rule in (*TRIGGER_RULES, "unknown"):
        assert is_trigger_rule(rule) == (rule != "unknown")
        assert trigger_rule_satisfied(rule, []) == {"runnable": True, "skip": False}
    for statuses in [("completed", "completed"), ("completed", "failed")]:
        deps = [{"status": value} for value in statuses]
        assert trigger_rule_satisfied("unknown", deps) == trigger_rule_satisfied(
            "all-success", deps
        )


def test_step_lookup_returns_first_match() -> None:
    first, second = bash("a"), bash("a")
    assert step_by_id([first, second], "a") is first
    assert step_by_id([first], "missing") is None


@pytest.mark.parametrize(
    "statuses,done,failed",
    [
        ({"a": "completed", "orphan": "pending"}, True, True),
        ({"a": "completed"}, True, False),
        ({"a": "failed"}, True, True),
        ({"a": "running"}, False, False),
        ({}, True, False),
    ],
)
def test_wave_terminal_classification(statuses: dict[str, str], done: bool, failed: bool) -> None:
    wave = next_wave(definition(bash("a")), state(statuses))
    assert wave == {"ready": [], "skipped": [], "done": done, "failed": failed, "warnings": []}


@pytest.mark.parametrize(
    "condition,value,runnable",
    [
        ("mode == 'fast'", "fast", True),
        ("mode == 'fast'", "slow", False),
        ("mode != 'fast'", "slow", True),
        ("mode != 'fast'", "fast", False),
        ("this is not a valid expr @@@", "anything", True),
        ("mode == 'fast' && other == 'x'", "slow", False),
    ],
)
def test_when_wave_semantics(condition: str, value: str, runnable: bool) -> None:
    wave = next_wave(
        definition(bash("a", when=condition)), state({"a": "pending"}, {"mode": value})
    )
    assert [step["id"] for step in wave["ready"]] == (["a"] if runnable else [])
    assert wave["skipped"] == (
        [] if runnable else [{"id": "a", "reason": f"when: {condition} is false"}]
    )
    if "@@@" in condition:
        assert wave["warnings"] == [
            'when-unparseable:a:when: unexpected character "@" at position 25 in "this is not a valid expr @@@"'
        ]


@pytest.mark.parametrize(
    "conditions,outputs,runnable",
    [
        (
            ["readiness == 'gaps'", "gap_mode == 'ask'"],
            {"readiness": "gaps", "gap_mode": "ask"},
            True,
        ),
        (
            ["readiness == 'gaps'", "gap_mode == 'ask'"],
            {"readiness": "gaps", "gap_mode": "defaults"},
            False,
        ),
        (
            ["readiness == 'gaps'", "gap_mode == 'ask'"],
            {"readiness": "ready", "gap_mode": "ask"},
            False,
        ),
        (["review_mode == 'ask'", "user_comments != ''"], {"review_mode": "auto"}, False),
        (
            ["review_mode == 'ask'", "user_comments != ''"],
            {"review_mode": "ask", "user_comments": "tweak X"},
            True,
        ),
        (["mode == 'fast'", "not a valid expr @@@"], {"mode": "fast"}, True),
    ],
)
def test_legacy_condition_lists_are_anded(
    conditions: list[str], outputs: dict[str, Any], runnable: bool
) -> None:
    wave = next_wave(definition(bash("a", when=conditions)), state({"a": "pending"}, outputs))
    assert [step["id"] for step in wave["ready"]] == (["a"] if runnable else [])
    assert [step["id"] for step in wave["skipped"]] == ([] if runnable else ["a"])


def test_dependency_waves_skip_propagation_and_no_mutation() -> None:
    workflow = definition(
        bash("a"),
        bash("b", depends_on=["a"]),
        bash("c", depends_on=["a"]),
        bash("d", depends_on=["b", "c"]),
        bash("e", depends_on=["b", "c"], **{"trigger-rule": "all-done"}),
    )
    steps = {key: "pending" for key in "abcde"}
    assert [item["id"] for item in next_wave(workflow, state(steps))["ready"]] == ["a"]
    steps["a"] = "completed"
    assert [item["id"] for item in next_wave(workflow, state(steps))["ready"]] == ["b", "c"]
    steps.update(b="failed", c="running")
    snapshot = state(steps)
    before = copy.deepcopy(snapshot)
    wave = next_wave(workflow, snapshot)
    assert snapshot == before
    assert wave["ready"] == []
    assert wave["skipped"] == [
        {"id": "d", "reason": "trigger-rule all-success not satisfied: b=failed, c=running"}
    ]
    assert not wave["done"]
    steps.update(c="completed", d="skipped")
    assert [item["id"] for item in next_wave(workflow, state(steps))["ready"]] == ["e"]
    steps["e"] = "completed"
    final = next_wave(workflow, state(steps))
    assert final["done"] and final["failed"]


def test_missing_dependency_and_input_answer_scope() -> None:
    assert (
        next_wave(definition(bash("b", depends_on=["ghost"])), state({"b": "pending"}))["ready"][0][
            "id"
        ]
        == "b"
    )
    workflow = definition(
        bash("a", when="inputs.mode == 'fast' && answers.profile == 'low'"),
        bash("b", when="mode == 'fast' && profile == 'max'"),
    )
    snapshot = state(
        {"a": "pending", "b": "pending"}, inputs={"mode": "fast"}, answers={"profile": "low"}
    )
    assert [item["id"] for item in next_wave(workflow, snapshot)["ready"]] == ["a"]


SCOPE = {
    "outputs": {"team": "solo", "count": 3, "flag": True, "empty": "", "zero": 0},
    "inputs": {"mode": "fast", "team": "inputs-team"},
    "answers": {"profile": "low"},
}


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("outputs.team == 'solo'", True),
        ('inputs.mode == "fast"', True),
        ("answers.profile != 'max'", True),
        ("team == 'solo'", True),
        ("mode == 'fast'", True),
        ("profile == 'low'", True),
        ("count == 3", True),
        ("count == '3'", True),
        ("count != 4", True),
        ("flag == true", True),
        ("flag != false", True),
        ("true", True),
        ("false", False),
        ("1.5 == 1.5", True),
        ("team == 'x' && mode == 'y' || profile == 'low'", True),
        ("profile == 'low' || team == 'x' && mode == 'y'", True),
        ("team == 'x' || profile == 'low' && mode == 'y'", False),
        ("(team == 'x' || profile == 'low') && mode == 'y'", False),
        ("(team == 'x' || profile == 'low') && mode == 'fast'", True),
        ("((profile == 'low'))", True),
        ("team", True),
        ("empty", False),
        ("zero", False),
        ("count", True),
        ("flag", True),
        ("!empty", True),
        ("!team", False),
        ("!!team", True),
        ("!(team == 'solo')", False),
        ("!team == 'solo'", False),
        ("missing", False),
        ("!missing", True),
        ("missing == 'x'", False),
        ("missing != 'x'", True),
        ("missing == ''", False),
        ("missing != ''", True),
        ("outputs.nested.deep == 'x'", False),
        ("nothing.here", False),
        ("'' == ''", True),
        ("'a b' != 'a  b'", True),
        ("flag == 1", False),
        ("zero == false", False),
    ],
)
def test_expression_semantics(expression: str, expected: bool) -> None:
    assert evaluate_when(expression, SCOPE) is expected


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("msg == 'hello world'", True),
        ('msg == "hello world"', True),
        ("msg != 'hello  world'", True),
        ('q == "it\'s"', True),
    ],
)
def test_quoted_strings(expression: str, expected: bool) -> None:
    assert evaluate_when(expression, {"outputs": {"msg": "hello world", "q": "it's"}}) is expected


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("review_mode == 'ask'", False),
        ("inputs.review_mode == 'auto'", True),
        ("implement_choice == 'yes'", None),
        ("review_mode == 'ask' && user_comments != ''", False),
        ("implement_mode != 'plan-only' && implement_choice == 'yes'", False),
        ("implement_mode == 'plan-only' && implement_choice == 'yes'", None),
        ("readiness == 'gaps' || review_mode == 'auto'", True),
        ("readiness == 'gaps' || findings != 0", None),
        ("review_mode == 'x' || review_mode == 'y'", False),
        ("!findings_path", None),
        ("findings_path", None),
        ("!review_mode", False),
        ("(true || findings != 0) && review_mode == 'auto'", True),
    ],
)
def test_partial_expression_unknowns(expression: str, expected: bool | None) -> None:
    scope = {"inputs": {"review_mode": "auto", "implement_mode": "plan-only"}, "answers": {}}
    assert evaluate_when_partial(expression, scope) is expected


@pytest.mark.parametrize(
    "expression,error",
    [
        ("mode == ", "unexpected end of expression at position 8"),
        ("mode == 'fast", "unterminated string at position 8"),
        ("(mode == 'fast'", "expected ')' at position 15"),
        ("mode == 'fast')", 'unexpected ")" at position 14'),
        ("mode = 'fast'", 'unexpected character "=" at position 5'),
        ("mode == 'fast' other", 'unexpected "other" at position 15'),
        ("this is not a valid expr @@@", 'unexpected character "@" at position 25'),
        ("a & b", 'unexpected character "&" at position 2'),
        ("1.2.3 == 1", "bad number 1.2.3 at position 0"),
        ("a. == 'x'", "bad identifier a. at position 0"),
        ("", "unexpected end of expression at position 0"),
        ("'🦉' @", 'unexpected character "@" at position 5'),
    ],
)
def test_parser_errors_keep_positions(expression: str, error: str) -> None:
    for evaluator in (evaluate_when, evaluate_when_partial):
        with pytest.raises(ValueError) as caught:
            evaluator(expression, SCOPE)
        assert error in str(caught.value)


def test_unset_null_and_unknown_are_distinct() -> None:
    scope = {"inputs": {"guidance": "", "nullish": None, "unset": UNDEFINED}}
    assert evaluate_when_partial("guidance", scope) is False
    assert evaluate_when_partial("unset", scope) is False
    assert evaluate_when_partial("missing", scope) is None
    assert evaluate_when("missing == nullish", scope) is False
    assert evaluate_when("missing == unset", scope) is True
    assert resolve_identifier("missing", scope) is UNDEFINED
    assert resolve_identifier_partial("missing", scope) is UNKNOWN
    assert resolve_identifier_partial("inputs.nullish.anything", scope) is UNKNOWN


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, False),
        (UNDEFINED, False),
        ("", False),
        ("0", True),
        (0, False),
        (-1, True),
        (False, False),
        (True, True),
        ([], False),
        ([False], True),
        ({}, True),
        (math.nan, False),
        (math.inf, True),
    ],
)
def test_engine_truthiness(value: Any, expected: bool) -> None:
    assert truthy(value) is expected


def test_reference_equality_and_scope_precedence() -> None:
    shared: dict[str, Any] = {}
    scope = {
        "value": "top",
        "outputs": {"value": "output", "a": shared, "b": shared, "c": {}, "list": [1, 2]},
    }
    assert evaluate_when("value == 'top'", scope)
    assert evaluate_when("a == b", scope)
    assert not evaluate_when("a == c", scope)
    assert evaluate_when("list.length == 2", scope)
    assert evaluate_when_partial("list.length == 2", scope)


def test_condition_coercion_uses_javascript_text() -> None:
    assert when_conditions() == []
    assert when_conditions(None) == []
    assert when_conditions("") == []
    assert when_conditions(False) == ["false"]
    assert when_conditions([True, 1.0, None, ["a", None, "b"], {}]) == [
        "true",
        "1",
        "null",
        "a,,b",
        "[object Object]",
    ]
