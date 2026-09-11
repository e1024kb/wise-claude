from __future__ import annotations

from typing import Any

from .constants import HARNESSES, RUN_MODES
from .models import catalog_for, catalog_model, default_effort, default_model
from .scheduler import evaluate_when_partial, when_conditions

Json = dict[str, Any]
LOGIN_CMDS = {
    "claude": "claude auth login",
    "codex": "codex login",
    "cursor": "cursor-agent login",
    "gemini": "gemini (interactive, then /auth)",
    "grok": "grok login",
}
PROFILE_DEFAULT = "medium"


def describe_tuning(value: Json) -> str:
    return " / ".join(value[k] for k in ("harness", "model", "effort") if value.get(k)) or "inherit"


def _answer_string(value: Any) -> str | None:
    return ", ".join(value) if isinstance(value, list) else value


def _answer_list(value: Any) -> list[str] | None:
    if value is None or isinstance(value, list):
        return value
    return [p.strip() for p in value.split(",") if p.strip()]


def _groups(definition: Json) -> list[Json]:
    return definition.get("tuning", {}).get("groups", [])


def _group_base(definition: Json, group: Json) -> Json:
    return {
        **group.get("default", {}),
        **definition.get("profiles", {})
        .get(PROFILE_DEFAULT, {})
        .get("tuning", {})
        .get(group["id"], {}),
    }


def _stage_harness(
    definition: Json,
    group: Json,
    answers: Json,
    installed: list[str] | None,
    logged_out: list[str] | None = None,
) -> Json:
    base = _group_base(definition, group)
    default = base.get("harness", "claude")
    offered = [default, *[h for h in installed or [] if h != default]]
    answer = _answer_string(answers.get(f"harness.{group['id']}"))
    if answer in HARNESSES:
        return {"base": base, "harness": answer}
    if len(offered) <= 1:
        return {"base": base, "harness": default}
    options = []
    for harness in offered:
        what = "the workflow's default" if harness == default else f"run these steps on {harness}"
        login = (
            f"; not logged in, run `{LOGIN_CMDS[harness]}` first"
            if harness in (logged_out or [])
            else ""
        )
        options.append(dict(value=harness, label=harness, description=what + login))
    return {
        "base": base,
        "question": dict(
            id=f"harness.{group['id']}",
            kind="choice",
            label=f"Which CLI runs: {group.get('label', group['id'])}?",
            options=options,
            default=default,
        ),
    }


def _stage_group(
    definition: Json,
    group: Json,
    answers: Json,
    installed: list[str] | None = None,
    logged_out: list[str] | None = None,
) -> Json:
    first = _stage_harness(definition, group, answers, installed, logged_out)
    base = first["base"]
    stage: Json = {"base": base, "questions": []}
    if "question" in first:
        stage["questions"].append(first["question"])
        return stage
    harness = first.get("harness", base.get("harness", "claude"))
    stage["harness"] = harness
    pinned = base.get("model") if harness == base.get("harness", "claude") else None
    model = catalog_model(harness, _answer_string(answers.get(f"model.{group['id']}")))
    catalog = catalog_for(harness)
    label = group.get("label", group["id"])
    if model is None and len(catalog) > 1:
        stage["questions"].append(
            dict(
                id=f"model.{group['id']}",
                kind="choice",
                label=f"Which {harness} model: {label}?",
                options=[
                    {k: m[k] for k in ("label", "description")} | {"value": m["id"]}
                    for m in catalog
                ],
                default=default_model(harness, pinned)["id"],
            )
        )
        return stage
    model = model or default_model(harness, pinned)
    stage["model"] = model
    effort = _answer_string(answers.get(f"effort.{group['id']}"))
    if effort in model["efforts"]:
        stage["effort"] = effort
    elif len(model["efforts"]) > 1:
        stage["questions"].append(
            dict(
                id=f"effort.{group['id']}",
                kind="choice",
                label=f"Effort for {model['label']}: {label}?",
                options=[dict(value=e, label=e) for e in model["efforts"]],
                default=default_effort(model, base.get("effort")),
            )
        )
    else:
        effort = default_effort(model, base.get("effort"))
        if effort is not None:
            stage["effort"] = effort
    return stage


def permission_default(definition: Json) -> str:
    return {"full": "full-access", "allowlist": "approval-required"}.get(
        definition.get("preflight", {}).get("permissions"), "auto"
    )


def _legacy_permission_answer(answers: Json) -> str | None:
    value = answers.get("permissions")
    if not isinstance(value, str):
        return None
    return {"full": "full-access", "allowlist": "approval-required"}.get(
        value, value if value in RUN_MODES else None
    )


def invalid_provider_permission_answers(answers: Json) -> list[str]:
    return [
        f"permissions.{h}"
        for h in HARNESSES
        if f"permissions.{h}" in answers and answers[f"permissions.{h}"] not in RUN_MODES
    ]


def _permission_question(definition: Json, harness: str) -> Json:
    return dict(
        id=f"permissions.{harness}",
        kind="choice",
        label=f"Minimum permissions for {harness}?",
        options=[
            dict(
                value="auto",
                label="Auto (recommended)",
                description="workspace-scoped automatic execution; higher step requirements still win",
            ),
            dict(
                value="approval-required",
                label="Approval required",
                description="keep restrictive step modes; headless permission requests may be denied",
            ),
            dict(
                value="full-access",
                label="Bypass permissions",
                description="run this provider without its permission checks or sandbox",
            ),
        ],
        default=permission_default(definition),
    )


def active_harnesses(
    definition: Json,
    enabled: set[str],
    active_groups: set[str],
    answers: Json,
    installed: list[str] | None = None,
    when_scope: Json | None = None,
) -> list[str]:
    groups = {g["id"]: g for g in _groups(definition)}
    group_harness = {
        gid: _stage_harness(definition, g, answers, installed).get(
            "harness", g.get("default", {}).get("harness", "claude")
        )
        for gid, g in groups.items()
        if gid in active_groups
    }
    out = []

    def add(h):
        if h not in out:
            out.append(h)

    for step in definition["steps"]:
        if step["id"] not in enabled or not _may_run(step, when_scope or {}):
            continue
        gid = step.get("group")
        if step["type"] == "agent":
            add(step.get("harness", group_harness.get(gid, "claude")))
            for h in step.get("fallback", groups.get(gid, {}).get("fallback", [])):
                add(h)
        elif step["type"] == "units":
            if "harness" in step:
                add(step["harness"])
            else:
                gids = list(step["groups"].values())
                if not gids:
                    add("claude")
                for gid in gids:
                    add(
                        group_harness.get(
                            gid, groups.get(gid, {}).get("default", {}).get("harness", "claude")
                        )
                    )
                    for h in groups.get(gid, {}).get("fallback", []):
                        add(h)
            for h in step.get("fallback", []):
                add(h)
    return out


def provider_permissions(answers: Json) -> Json:
    legacy = _legacy_permission_answer(answers)
    out = {h: legacy for h in HARNESSES} if legacy is not None else {}
    for harness in HARNESSES:
        value = _answer_string(answers.get(f"permissions.{harness}"))
        if value in RUN_MODES:
            out[harness] = value
    return out


def optional_step_ids(definition: Json) -> list[str]:
    declared = definition.get("step-select", {}).get("optional")
    return (
        list(declared)
        if declared is not None
        else [s["id"] for s in definition["steps"] if s.get("optional") is True]
    )


def enabled_step_ids(definition: Json, selected: list[str] | None = None) -> set[str]:
    optional = optional_step_ids(definition)
    return {
        s["id"]
        for s in definition["steps"]
        if s["id"] not in optional or selected is None or s["id"] in selected
    }


def resolve_from_context(path: str, context: Json | None = None) -> str | None:
    if not context:
        return None
    if path == "guidance":
        return context.get("guidance", "").strip() or None
    if path == "links[]":
        return "\n".join(context.get("links", [])) or None
    if path in ("ticket[].ref", "ticket[].title", "ticket[].body", "ticket[].url"):
        field = path.split(".")[1]
        return ", ".join(t[field] for t in context.get("ticket", []) if t.get(field)) or None
    if path.startswith("decisions.") and len(path) > 10:
        return context.get("decisions", {}).get(path[10:])
    return None


def known_inputs(definition: Json, answers: Json, context: Json | None = None) -> Json:
    known = {}
    for item in definition.get("inputs", []):
        value = _answer_string(answers.get(f"input.{item['name']}"))
        if value is None:
            value = resolve_from_context(item.get("from-context", ""), context)
        if value is None:
            value = item.get("default", "" if item.get("optional") else None)
        if value is not None:
            known[item["name"]] = value
    return known


def _may_run(step: Json, scope: Json) -> bool:
    for condition in when_conditions(step.get("when")):
        try:
            if evaluate_when_partial(condition, scope) is False:
                return False
        except ValueError:
            pass
    return True


def active_group_ids(
    definition: Json, enabled: set[str], when_scope: Json | None = None
) -> set[str]:
    bound: dict[str, bool] = {}
    for step in definition["steps"]:
        on = step["id"] in enabled and _may_run(step, when_scope or {})
        gids = (
            [step["group"]]
            if step["type"] == "agent" and "group" in step
            else list(step.get("groups", {}).values())
            if step["type"] == "units"
            else []
        )
        for gid in gids:
            bound[gid] = bound.get(gid, False) or on
    return {g["id"] for g in _groups(definition) if bound.get(g["id"], True)}


def _step_select_question(definition: Json, optional: list[str]) -> Json:
    by_id = {s["id"]: s for s in definition["steps"]}
    options = []
    for ident in optional:
        step = by_id.get(ident, {})
        option = dict(value=ident, label=step.get("description", ident))
        if step.get("description"):
            option["description"] = ident
        options.append(option)
    return dict(
        id="step-select",
        kind="multi",
        label=definition.get("step-select", {}).get("prompt", "Which optional steps should run?"),
        options=options,
        default=list(optional),
    )


def build_questionary(
    definition: Json, ctx: Json | None = None, answers: Json | None = None
) -> Json:
    from .defs import list_inputs

    ctx, answers = ctx or {}, answers or {}
    questions: list[Json] = []
    defaults: Json = {}

    def push(q):
        if q["id"] in answers:
            return
        questions.append(q)
        if "default" in q:
            defaults[q["id"]] = q["default"]

    optional = optional_step_ids(definition)
    if optional:
        push(_step_select_question(definition, optional))
    for item in list_inputs(definition):
        q = dict(id=f"input.{item['name']}", kind="text", label=item["prompt"])
        if item.get("optional"):
            q["optional"] = True
        preset = resolve_from_context(item.get("from-context", ""), ctx.get("context"))
        if preset is None:
            preset = item.get("default", "" if item.get("optional") else None)
        if preset is not None:
            q["default"] = preset
        push(q)
    result = {"questions": questions, "defaults": defaults}
    selected = _answer_list(answers.get("step-select"))
    if optional and selected is None:
        return result
    scope = {"inputs": known_inputs(definition, answers, ctx.get("context")), "answers": answers}
    enabled = enabled_step_ids(definition, selected)
    active = active_group_ids(definition, enabled, scope)
    for group in _groups(definition):
        if group.get("locked") or group["id"] not in active:
            continue
        stage = _stage_harness(
            definition, group, answers, ctx.get("harnesses"), ctx.get("logged_out")
        )
        if "question" in stage:
            push(stage["question"])
    if any(q["id"].startswith("harness.") for q in questions):
        return result
    if _legacy_permission_answer(answers) is None:
        for h in active_harnesses(
            definition, enabled, active, answers, ctx.get("harnesses"), scope
        ):
            push(_permission_question(definition, h))
    if any(q["id"].startswith("permissions.") for q in questions):
        return result
    for group in _groups(definition):
        if group.get("locked") or group["id"] not in active:
            continue
        for q in _stage_group(
            definition, group, answers, ctx.get("harnesses"), ctx.get("logged_out")
        )["questions"]:
            push(q)
    return result


def apply_answers(definition: Json, answers: Json) -> Json:
    tuning = {}
    for group in _groups(definition):
        if group.get("locked"):
            tuning[group["id"]] = _group_base(definition, group)
            continue
        stage = _stage_group(definition, group, answers)
        base = stage["base"]
        harness = stage.get("harness", base.get("harness", "claude"))
        model = stage.get("model") or default_model(
            harness, base.get("model") if harness == base.get("harness", "claude") else None
        )
        effort = stage.get("effort", default_effort(model, base.get("effort")))
        value = {**base, "harness": harness, "model": model["id"]}
        if effort is not None:
            value["effort"] = effort
        else:
            value.pop("effort", None)
        tuning[group["id"]] = value
    inputs = {}
    for item in definition.get("inputs", []):
        input_value = _answer_string(answers.get(f"input.{item['name']}"))
        if input_value is None:
            input_value = item.get("default")
        if input_value is not None:
            inputs[item["name"]] = input_value
    return dict(
        profile=PROFILE_DEFAULT,
        tuning=tuning,
        provider_permissions=provider_permissions(answers),
        enabled_steps=enabled_step_ids(definition, _answer_list(answers.get("step-select"))),
        inputs=inputs,
        caps=dict(definition.get("profiles", {}).get(PROFILE_DEFAULT, {}).get("caps", {})),
    )


def fill_answers(questions: list[Json], given: Json) -> Json:
    answers = dict(given)
    missing = []
    for q in questions:
        if q.get("locked") or q["id"] in answers:
            continue
        if "default" in q:
            answers[q["id"]] = q["default"]
        elif not (q["kind"] == "text" and q.get("optional")):
            missing.append(q["id"])
    return dict(
        answers=answers,
        inputs={
            k[6:]: v for k, v in answers.items() if k.startswith("input.") and isinstance(v, str)
        },
        missing=missing,
    )


def complete_answers(definition: Json, ctx: Json, given: Json) -> Json:
    answers = dict(given)
    seen: Json = {}
    filled: Json = {}
    for _ in range(4 * len(_groups(definition)) + 4):
        q = build_questionary(definition, ctx, answers)
        for question in q["questions"]:
            seen[question["id"]] = question
        filled = fill_answers(q["questions"], answers)
        grew = len(filled["answers"]) > len(answers)
        answers = filled["answers"]
        if not grew:
            break
    return {**filled, "questions": list(seen.values())}
