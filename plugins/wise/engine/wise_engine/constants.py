AUTH_MODES = ("subscription", "api-key")

EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Picker order for every effort question. Do not sort it.
EFFORT_PICKER_ORDER = ("medium", "high", "xhigh", "low", "max")

# Picker order for the tuning groups every workflow shares; a group not
# listed here is workflow-specific and is asked before these, in its
# declared order.
GROUP_ORDER = ("plan", "implement", "fix", "review", "watch", "support")

# Inputs every workflow shares, asked after the workflow-specific ones.
SHARED_INPUTS = ("base_branch", "guidance")

ERROR_CODES = (
    "WORKFLOW_NOT_FOUND",
    "WORKFLOW_INVALID",
    "RUN_NOT_FOUND",
    "GATE_STALE",
    "HARNESS_UNAVAILABLE",
    "AUTH_REQUIRED",
    "BUDGET_EXCEEDED",
    "DAEMON_VERSION_MISMATCH",
    "NOT_IMPLEMENTED",
    "ALREADY_RUNNING",
    "TOKEN_INVALID",
    "MISSING_ANSWERS",
    "REQUIRES_MISSING",
)

EVENT_TYPES = (
    "run.started",
    "step.started",
    "step.progress",
    "step.done",
    "unit.phase",
    "unit.done",
    "usage",
    "gate.opened",
    "gate.answered",
    "run.done",
    "run.failed",
    "warn",
)

# Picker order for every harness question and inventory: claude, codex,
# cursor, grok, gemini. Do not sort it.
HARNESSES = ("claude", "codex", "cursor", "grok", "gemini")

MCP_POLICIES = ("inherit", "engine-only")

PERMISSIONS = ("allowlist", "full")

PHASES = (
    "claim",
    "worktree",
    "plan",
    "implement",
    "review",
    "fix",
    "push",
    "pr",
    "request-review",
    "watch",
    "cleanup",
)
# Which phases each units pipeline runs. `ticket` and `plan` own a branch
# end to end; `pr` and `implement` attach to the checked-out branch.
PIPELINE_PHASES = {
    "ticket": PHASES,
    "plan": PHASES,
    "pr": ("claim", "watch", "cleanup"),
    "implement": ("claim", "implement", "cleanup"),
    # One epic child in the ticket-plan workflow: its branch, then its plan.
    "ticket-plan": ("claim", "worktree", "plan", "cleanup"),
}
PIPELINES = tuple(PIPELINE_PHASES)
# Pipelines that work in the checkout the run starts in, never a new worktree.
ATTACHED_PIPELINES = ("pr", "implement")

PROFILE_LEVELS = ("low", "medium", "max")

REPORT_KINDS = ("progress", "blocker", "decision", "finding")

RUN_MODES = ("approval-required", "auto", "full-access")

RUN_STATUSES = ("initializing", "running", "gated", "paused", "completed", "failed", "cancelled")

STEP_STATUSES = ("pending", "running", "completed", "failed", "skipped", "cancelled")

STEP_TYPES = ("agent", "bash", "approval", "ask", "units")

TERMINAL_RUN = frozenset(["completed", "cancelled"])

TERMINAL_STEP = frozenset(["completed", "failed", "skipped", "cancelled"])

TRIGGER_RULES = (
    "all-success",
    "one-success",
    "all-done",
    "none-failed",
    "none-failed-min-one-success",
)
