#!/usr/bin/env bash
# wise writing-style hook (SessionStart + SubagentStart).
#
# Adds a short ASD-STE100 writing rule to the session context, so that a
# user who installs wise gets the same output style with no manual setup.
# The full rule set lives in references/ste100.md and /wise-ste.
#
# HARD CONSTRAINTS - keep them true (see CONTRIBUTING.md §2.4):
#   • No LLM, no network, no Python, no dependency bootstrap.
#   • Constant, tiny work: print one fixed JSON object.
#   • ALWAYS exits 0 and never blocks the session.
#   • Opt-out: WISE_STE=off (also 0, false, no) prints nothing.
#   • Bash 3.2 compatible (macOS default).

set +e

# Drain the hook payload so the caller never sees a broken pipe.
cat >/dev/null 2>&1

case "$(printf '%s' "${WISE_STE:-on}" | tr '[:upper:]' '[:lower:]')" in
  off|0|false|no) exit 0 ;;
esac

EVENT="${1:-SessionStart}"
case "$EVENT" in
  SessionStart|SubagentStart) ;;
  *) exit 0 ;;
esac

# Keep this text plain ASCII with no double quotes or backslashes: it is
# embedded in JSON as-is.
CONTEXT="WISE WRITING STYLE (ASD-STE100). Write all human-readable text in ASD-STE100 Simplified Technical English. This applies to PR descriptions, PR and review comments, commit bodies, tickets, chat messages, docs, and reports. Rules: max 20 words in an instruction sentence and 25 in a descriptive sentence. Use active voice and simple tenses. Write one instruction per sentence and start it with a verb. Use one term for one thing. Keep articles. Put a warning before the step it applies to. Write max 6 sentences per paragraph. Use numbered lists for sequences. Keep code, commands, paths, identifiers, numbers, and error text verbatim. When you present 3 or more findings, decisions, options, risks, questions, or actions, give each a short code (F1, D1, O1, R1, Q1, A1) and keep the codes stable in the conversation. If another active style asks for shorter chat replies, follow it in chat and keep STE100 as the floor. Full rules: the wise references/ste100.md file. Rewrite command: /wise-ste. Disable: set WISE_STE=off."

printf '{"hookSpecificOutput":{"hookEventName":"%s","additionalContext":"%s"}}\n' "$EVENT" "$CONTEXT"
exit 0
