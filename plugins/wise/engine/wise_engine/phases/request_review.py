from ..yaml_compat import js_string
from .common import Json, err_text, gh, json_of, ok, pass_

# Remote review bots are never requested: wise reviews locally instead and
# handles a bot's comments only when the repository runs it on its own.
REVIEW_BOTS = frozenset(
    {"copilot", "copilot-pull-request-reviewer", "coderabbitai", "coderabbitai[bot]"}
)


async def request_review_phase(ctx: Json) -> Json:
    pr = ctx["unit"].get("pr")
    if not pr:
        ctx["log"]("request-review: no PR recorded, skipped")
        return pass_()
    if not ctx["config"]["reviewers"]:
        ctx["log"]("request-review: no reviewers configured, skipped")
        return pass_()
    result = json_of(
        await gh(ctx, ["pr", "view", js_string(pr["number"]), "--json", "reviewRequests"])
    )
    have: set[str] = set()
    if isinstance(result, dict) and isinstance(result.get("reviewRequests"), list):
        for row in result["reviewRequests"]:
            if isinstance(row, dict):
                have.update(
                    row[key].lower() for key in ("login", "name") if isinstance(row.get(key), str)
                )
    for login in ctx["config"]["reviewers"]:
        if login.lower() in REVIEW_BOTS:
            ctx["log"](f"request-review: {login} is a review bot, never requested")
            continue
        if login.lower() in have:
            ctx["log"](f"request-review: {login} already requested")
            continue
        res = await gh(ctx, ["pr", "edit", js_string(pr["number"]), "--add-reviewer", login])
        ctx["log"](
            f"request-review: {login} attached"
            if ok(res)
            else f"request-review: {login} unavailable ({err_text(res, 120)})"
        )
    return pass_()
