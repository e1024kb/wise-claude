AUTH_MODES = ("subscription", "api-key")

EFFORTS = ("low", "medium", "high", "xhigh", "max")

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

HARNESSES = ("claude", "codex", "cursor", "gemini", "grok")

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
