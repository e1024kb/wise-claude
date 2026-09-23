from __future__ import annotations

import re
from typing import Any

from .branches import is_branch_name
from .constants import EFFORT_PICKER_ORDER, GROUP_ORDER, HARNESSES, RUN_MODES, SHARED_INPUTS
from .models import catalog_model, default_effort, default_model, merged_catalog
from .scheduler import JS_WHITESPACE, evaluate_when_partial, when_conditions

Json = dict[str, Any]

PROFILE_DEFAULT = "medium"

PLAIN_ALTERNATION_RE = re.compile(r"\^\(([A-Za-z0-9_-]+(?:\|[A-Za-z0-9_-]+)+)\)\$")

DEFAULT_MARK = " (default)"
PAGE_SIZE = 4


def canonical_groups(groups: list[Json]) -> list[Json]:
    """Tuning groups in the order every workflow asks them: workflow-specific
    groups first in their declared order, then the shared ones in GROUP_ORDER."""
    rank = {gid: i for i, gid in enumerate(GROUP_ORDER)}
    return sorted(groups, key=lambda g: rank.get(g["id"], -1))


def canonical_inputs(items: list[Json]) -> list[Json]:
    """Inputs in the order every workflow asks them: workflow-specific inputs
    first in their declared order, then the shared ones in SHARED_INPUTS."""
    rank = {name: i for i, name in enumerate(SHARED_INPUTS)}
    return sorted(items, key=lambda item: rank.get(item["name"], -1))


def effort_options(efforts: list[str]) -> list[str]:
    return [e for e in EFFORT_PICKER_ORDER if e in efforts]


def mark_default(question: Json) -> Json:
    """Append the default marker to the default option's label. The option list
    itself never moves: the same question shows the same rows every run."""
    if question.get("kind") != "choice" or "default" not in question:
        return question
    options = [
        {**o, "label": o["label"] + DEFAULT_MARK}
        if o.get("value") == question["default"] and not o["label"].endswith(DEFAULT_MARK)
        else o
        for o in question.get("options", [])
    ]
    return {**question, "options": options}


def paginate(questions: list[Json]) -> list[list[str]]:
    """Fixed pages for the questions of one preflight call: at most PAGE_SIZE
    per page, stage boundaries never straddled, so a page's composition depends
    only on the workflow and the answers so far, never on the host."""
    pages: list[list[str]] = []
    current: list[str] = []
    current_stage = None
    for q in questions:
        stage = _stage_of(q["id"])
        if current and (stage != current_stage or len(current) >= PAGE_SIZE):
            pages.append(current)
            current = []
        current.append(q["id"])
        current_stage = stage
    if current:
        pages.append(current)
    return pages


def _stage_of(qid: str) -> str:
    if qid.startswith("harness."):
        return "harness"
    if qid.startswith("permissions."):
        return "permissions"
    if qid.startswith(("model.", "effort.")):
        return "tuning"
    return "inputs"


def _input_options(validate: Any) -> list[Json] | None:
    if not isinstance(validate, str):
        return None
    match = PLAIN_ALTERNATION_RE.fullmatch(validate)
    if match is None:
        return None
    values = match.group(1).split("|")
    if len(values) != len(set(values)):
        return None
    return [dict(value=value, label=value) for value in values]


def input_choice_values(item: Json) -> set[str] | None:
    options = None if item.get("extract") else _input_options(item.get("validate"))
    if options is None:
        return None
    values = {option["value"] for option in options}
    if item.get("optional"):
        values.add("")
    return values


def choice_input_preset(item: Json, context: Json | None = None) -> str | None:
    values = input_choice_values(item)
    if values is None:
        return None
    candidates = (
        resolve_from_context(item.get("from-context", ""), context),
        item.get("default"),
        "" if item.get("optional") else None,
    )
    return next(
        (value for value in candidates if isinstance(value, str) and value in values),
        None,
    )


def invalid_choice_input_ids(definition: Json, inputs: Json) -> list[str]:
    """Inputs whose answer is outside its choices, or not a git branch name for branch inputs."""
    invalid = []
    for item in definition.get("inputs", []):
        values = input_choice_values(item)
        name = item["name"]
        if name not in inputs:
            continue
        value = inputs[name]
        if values is not None:
            if not isinstance(value, str) or value not in values:
                invalid.append(f"input.{name}")
        elif item.get("options-from") == "branches":
            if not isinstance(value, str) or not (
                is_branch_name(value) or (value == "" and item.get("optional"))
            ):
                invalid.append(f"input.{name}")
        elif item.get("validate") and not item.get("extract"):
            from .defs import validate_input

            if (
                not isinstance(value, str)
                or not validate_input(value, None, item["validate"])["ok"]
            ):
                invalid.append(f"input.{name}")
    return invalid


def fans_out(definition: Json, source: str, answers: Json, context: Json | None = None) -> bool:
    """Whether the run fans out: the `source` input holds more than one ref,
    or the conductor's context carries an epic (a ticket with children)."""
    if any(
        isinstance(ticket, dict) and ticket.get("children")
        for ticket in (context or {}).get("ticket", [])
    ):
        return True
    value = known_inputs(definition, answers, context).get(source)
    return isinstance(value, str) and sum(1 for p in re.split(r"[,;\n]", value) if p.strip()) > 1


def fanout_source(definition: Json) -> str | None:
    """The input a workflow's fan-out questions name, if it has any."""
    for item in definition.get("inputs", []):
        for key in ("needs-fanout", "unless-fanout"):
            if key in item:
                return str(item[key])
    return None


def fanout_skips(definition: Json, item: Json, answers: Json, context: Json | None) -> bool:
    """A fan-out-only input in a single-ticket run, or a single-ticket-only
    input in a fan-out run: not asked; it keeps its default."""
    if "needs-fanout" in item:
        return not fans_out(definition, item["needs-fanout"], answers, context)
    if "unless-fanout" in item:
        return fans_out(definition, item["unless-fanout"], answers, context)
    return False


def invalid_concurrency_ids(inputs: Json, asked: bool) -> list[str]:
    """Parallel children need a worktree each: an answered `concurrency`
    above 1 with `worktree_mode: current` is rejected and asked again. The
    default of a run that never asked is not an answer and is not rejected;
    the units step runs one child at a time in `current` mode."""
    value = str(inputs.get("concurrency", "1") or "1")
    if asked and inputs.get("worktree_mode") == "current" and value != "1":
        return ["input.concurrency"]
    return []


def describe_tuning(value: Json) -> str:
    return " / ".join(value[k] for k in ("harness", "model", "effort") if value.get(k)) or "inherit"


def _answer_string(value: Any) -> str | None:
    return ", ".join(value) if isinstance(value, list) else value


def _answer_list(value: Any) -> list[str] | None:
    if value is None or isinstance(value, list):
        return value
    return [p.strip(JS_WHITESPACE) for p in value.split(",") if p.strip(JS_WHITESPACE)]


def _groups(definition: Json) -> list[Json]:
    return canonical_groups(definition.get("tuning", {}).get("groups", []))


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
    from .auth import LOGIN_CMDS

    base = _group_base(definition, group)
    default = base.get("harness", "claude")
    # Default first, then every other installed harness in HARNESSES order,
    # whatever order the caller listed them in.
    offered = [default, *[h for h in HARNESSES if h in (installed or []) and h != default]]
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
            label=f"Which harness runs: {group.get('label', group['id'])}?",
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
    models: Json | None = None,
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
    discovered = (models or {}).get(harness)
    model = catalog_model(harness, _answer_string(answers.get(f"model.{group['id']}")), discovered)
    catalog = merged_catalog(harness, discovered)
    label = group.get("label", group["id"])
    if model is None and len(catalog) > 1:
        stage["questions"].append(
            dict(
                id=f"model.{group['id']}",
                kind="choice",
                label=f"Which {harness} model: {label}?",
                options=[
                    {k: m[k] for k in ("label", "description", "source")} | {"value": m["id"]}
                    for m in catalog
                ],
                default=default_model(harness, pinned, discovered)["id"],
            )
        )
        return stage
    model = model or default_model(harness, pinned, discovered)
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
                options=[dict(value=e, label=e) for e in effort_options(model["efforts"])],
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
                value="full-access",
                label="Bypass permissions",
                description="run this provider without its permission checks or sandbox",
            ),
            dict(
                value="auto",
                label="Auto",
                description="workspace-scoped automatic execution; higher step requirements still win",
            ),
            dict(
                value="approval-required",
                label="Approval required",
                description="keep restrictive step modes; headless permission requests may be denied",
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
    # One permission question per provider, always in the picker order.
    return sorted(out, key=HARNESSES.index)


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
        return context.get("guidance", "").strip(JS_WHITESPACE) or None
    if path == "links[]":
        links = context.get("links", [])
        return "\n".join(links) if links else None
    if path in ("ticket[].ref", "ticket[].title", "ticket[].body", "ticket[].url"):
        # Top-level tickets only: expanded epic children carry `parent` and
        # are fanned out by the engine, never typed into a ticket input.
        field = path.split(".")[1]
        return (
            ", ".join(
                t[field] for t in context.get("ticket", []) if t.get(field) and not t.get("parent")
            )
            or None
        )
    if (
        path.startswith("decisions.")
        and len(path) > 10
        and not any(c in path for c in "\n\r\u2028\u2029")
    ):
        return context.get("decisions", {}).get(path[10:])
    return None


def known_inputs(definition: Json, answers: Json, context: Json | None = None) -> Json:
    known = {}
    for item in definition.get("inputs", []):
        answer_id = f"input.{item['name']}"
        worktree_value = (
            worktree_answer(definition, answers) if item["name"] == "worktree_mode" else None
        )
        value = worktree_value or _answer_string(answers.get(answer_id))
        if answer_id not in answers and worktree_value is None:
            if input_choice_values(item) is not None:
                value = choice_input_preset(item, context)
            else:
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


def worktree_default(definition: Json) -> str:
    declared = next(
        (
            item.get("default")
            for item in definition.get("inputs", [])
            if item.get("name") == "worktree_mode"
        ),
        None,
    )
    return (
        declared
        if declared in ("current", "new")
        else definition.get("preflight", {}).get("worktree", "current")
    )


def worktree_locked(definition: Json) -> bool:
    return definition.get("preflight", {}).get("lock-worktree") is True


def worktree_answer(definition: Json, answers: Json) -> str | None:
    if worktree_locked(definition):
        return worktree_default(definition)
    value = answers.get("worktree", answers.get("input.worktree_mode"))
    return value if value in ("current", "new") else None


def invalid_worktree_answers(answers: Json) -> list[str]:
    key = "worktree" if "worktree" in answers else "input.worktree_mode"
    return [key] if key in answers and answers[key] not in ("current", "new") else []


def _worktree_question(definition: Json) -> Json:
    return dict(
        id="worktree",
        kind="choice",
        label="Where should this workflow make changes?",
        options=[
            dict(
                value="current",
                label="Current checkout",
                description="make changes in the checkout where the workflow starts",
            ),
            dict(
                value="new",
                label="Separate worktree",
                description="make changes on a new branch in a separate Git worktree",
            ),
        ],
        default=worktree_default(definition),
    )


def _branch_input_question(item: Json, branches: Json | None) -> Json:
    q: Json = dict(id=f"input.{item['name']}", kind="text", label=item["prompt"])
    if item.get("optional"):
        q["optional"] = True
    if branches and branches.get("options"):
        q.update(kind="choice", options=list(branches["options"]), allow_text=True)
        q["default"] = branches["default"]
    elif item.get("default") is not None:
        q["default"] = item["default"]
    elif item.get("optional"):
        q["default"] = ""
    return q


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
        q = mark_default(q)
        questions.append(q)
        if "default" in q:
            defaults[q["id"]] = q["default"]

    if worktree_answer(definition, answers) is None:
        # Asked again when the given answer is invalid, so not through push.
        question = mark_default(_worktree_question(definition))
        questions.append(question)
        defaults[question["id"]] = question["default"]
    optional = optional_step_ids(definition)
    if optional:
        push(_step_select_question(definition, optional))
    selected = _answer_list(answers.get("step-select"))
    enabled = enabled_step_ids(definition, selected)
    for item in canonical_inputs(list_inputs(definition)):
        if item["name"] == "worktree_mode":
            continue
        if "needs-steps" in item:
            # Asked only once step-select is settled and one of its steps runs.
            if optional and selected is None:
                continue
            if not set(item["needs-steps"]) & enabled:
                continue
        if fanout_skips(definition, item, answers, ctx.get("context")):
            continue
        if item.get("options-from") == "branches":
            push(_branch_input_question(item, ctx.get("branches")))
            continue
        options = None if item.get("extract") else _input_options(item.get("validate"))
        q = dict(
            id=f"input.{item['name']}",
            kind="choice" if options else "text",
            label=item["prompt"],
        )
        if options:
            if item.get("optional"):
                options = [*options, {"value": "", "label": "Leave unset"}]
            q["options"] = options
        if item.get("optional"):
            q["optional"] = True
        preset = resolve_from_context(item.get("from-context", ""), ctx.get("context"))
        fallback = item.get("default", "" if item.get("optional") else None)
        if options:
            preset = choice_input_preset(item, ctx.get("context"))
        elif preset is None:
            preset = fallback
        if (
            item["name"] == "concurrency"
            and (worktree_answer(definition, answers) or worktree_default(definition)) == "current"
        ):
            # One checkout runs one child at a time.
            preset = "1"
        if preset is not None:
            q["default"] = preset
        push(q)
    result: Json = {"questions": questions, "defaults": defaults}
    if optional and selected is None:
        return _paged(result)
    scope = {"inputs": known_inputs(definition, answers, ctx.get("context")), "answers": answers}
    source = fanout_source(definition)
    if source is not None:
        # `fanout` is the expansion step's output at run time; pre-flight
        # predicts it so a step gated on it drops its tuning questions.
        scope["inputs"]["fanout"] = (
            "yes" if fans_out(definition, source, answers, ctx.get("context")) else "no"
        )
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
        return _paged(result)
    if _legacy_permission_answer(answers) is None:
        for h in active_harnesses(
            definition, enabled, active, answers, ctx.get("harnesses"), scope
        ):
            push(_permission_question(definition, h))
    if any(q["id"].startswith("permissions.") for q in questions):
        return _paged(result)
    # Model and effort run as a chain, group by group: the first page asks
    # the first group's model; every next page asks the previous group's
    # effort (its options depend on the model just chosen) together with the
    # next group's model; the last page asks the last group's effort alone.
    for group in _groups(definition):
        if group.get("locked") or group["id"] not in active:
            continue
        stage = _stage_group(
            definition,
            group,
            answers,
            ctx.get("harnesses"),
            ctx.get("logged_out"),
            ctx.get("models"),
        )
        for q in stage["questions"]:
            push(q)
        if any(q["id"].startswith("model.") for q in stage["questions"]):
            break
    return _paged(result)


def _paged(result: Json) -> Json:
    return {**result, "pages": paginate(result["questions"])}


def retry_questions(definition: Json, ctx: Json, answers: Json, wanted: list[str]) -> list[Json]:
    """The questions for every `wanted` id, rebuilt on top of `answers`.

    Model questions come one group per page, so a found model is answered by
    its default before the next rebuild until every wanted model is seen."""
    found: dict[str, Json] = {}
    pending = set(wanted)
    answers = dict(answers)
    for _ in range(len(wanted) + 1):
        for question in build_questionary(definition, ctx, answers)["questions"]:
            if question["id"] in pending:
                found[question["id"]] = question
                pending.discard(question["id"])
        if not pending:
            break
        fillable = [
            key
            for key, question in found.items()
            if key.startswith("model.") and key not in answers and "default" in question
        ]
        if not fillable:
            break
        for key in fillable:
            answers[key] = found[key]["default"]
    return list(found.values())


def apply_answers(definition: Json, answers: Json, ctx: Json | None = None) -> Json:
    tuning = {}
    models = (ctx or {}).get("models")
    for group in _groups(definition):
        if group.get("locked"):
            tuning[group["id"]] = _group_base(definition, group)
            continue
        stage = _stage_group(definition, group, answers, models=models)
        base = stage["base"]
        harness = stage.get("harness", base.get("harness", "claude"))
        model = stage.get("model") or default_model(
            harness,
            base.get("model") if harness == base.get("harness", "claude") else None,
            (models or {}).get(harness),
        )
        effort = stage.get("effort", default_effort(model, base.get("effort")))
        value = {**base, "harness": harness, "model": model["id"]}
        if effort is not None:
            value["effort"] = effort
        else:
            value.pop("effort", None)
        tuning[group["id"]] = value
    inputs = {}
    enabled = enabled_step_ids(definition, _answer_list(answers.get("step-select")))
    for item in definition.get("inputs", []):
        if item["name"] == "worktree_mode":
            continue
        input_value = _answer_string(answers.get(f"input.{item['name']}"))
        if "needs-steps" in item and not set(item["needs-steps"]) & enabled:
            input_value = None
        if input_value is None:
            input_value = item.get("default")
        if input_value is not None:
            inputs[item["name"]] = input_value
    worktree = worktree_answer(definition, answers) or worktree_default(definition)
    inputs["worktree_mode"] = worktree
    return dict(
        profile=PROFILE_DEFAULT,
        worktree=worktree,
        tuning=tuning,
        provider_permissions=provider_permissions(answers),
        enabled_steps=enabled,
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


EARLIER_STAGES = ("worktree", "step-select", "harness.", "permissions.")


def chosen_harnesses(definition: Json, answers: Json) -> list[str]:
    chosen = []
    for group in _groups(definition):
        if group.get("locked"):
            continue
        answer = _answer_string(answers.get(f"harness.{group['id']}"))
        chosen.append(
            answer
            if answer in HARNESSES
            else _group_base(definition, group).get("harness", "claude")
        )
    return list(dict.fromkeys(chosen))


def invalid_model_answer_ids(definition: Json, answers: Json, models: Json | None) -> list[str]:
    """Explicit model answers that no catalog row (predefined or discovered) backs."""
    invalid = []
    for group in _groups(definition):
        if group.get("locked"):
            continue
        key = f"model.{group['id']}"
        wanted = _answer_string(answers.get(key))
        if not wanted:
            continue
        harness = _answer_string(answers.get(f"harness.{group['id']}"))
        if harness not in HARNESSES:
            harness = _group_base(definition, group).get("harness", "claude")
        if catalog_model(harness, wanted, (models or {}).get(harness)) is None:
            invalid.append(key)
    return invalid


def model_stage_reached(definition: Json, ctx: Json, answers: Json) -> bool:
    if any(key.startswith("model.") for key in answers):
        return True
    questions = build_questionary(definition, ctx, answers)["questions"]
    return not any(question["id"].startswith(EARLIER_STAGES) for question in questions)


async def with_discovered_models(
    definition: Json, ctx: Json, answers: Json, lookup: Any, cache: Json | None = None
) -> Json:
    if "models" in ctx or not model_stage_reached(definition, ctx, answers):
        return ctx
    from .models import discover_models

    harnesses = chosen_harnesses(definition, answers)
    return {**ctx, "models": await discover_models(harnesses, lookup, cache)}


async def build_questionary_with_auth(
    definition: Json, ctx: Json, answers: Json, lookup: Any, cache: Json | None = None
) -> Json:
    from .auth import logged_out_harnesses

    ctx = await with_discovered_models(definition, ctx, answers, lookup, cache)
    questionary = build_questionary(definition, ctx, answers)
    group_ids = [
        question["id"][len("harness.") :]
        for question in questionary["questions"]
        if question["id"].startswith("harness.")
    ]
    if not group_ids:
        return questionary
    groups = {group["id"]: group for group in _groups(definition)}
    defaults = [
        _group_base(definition, groups[gid]).get("harness", "claude") if gid in groups else "claude"
        for gid in group_ids
    ]
    harnesses = list(dict.fromkeys([*ctx.get("harnesses", []), *defaults]))
    logged_out = await logged_out_harnesses(harnesses, lookup)
    return (
        build_questionary(definition, {**ctx, "logged_out": logged_out}, answers)
        if logged_out
        else questionary
    )
