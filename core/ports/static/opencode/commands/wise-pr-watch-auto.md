---
description: "Autonomous variant of `/wise-pr-watch` — watch the current branch's PR pipelines, auto-fix failing checks (lint / tests / other), commit + push, then trigger + wait for the bot reviews (Copilot strictly; CodeRabbit best-effort — bypassed when out of credits, retried-then-given-up on a rate limit) and handle every bot review comment by severity (minors fixed, majors via a considered decision, false positives dismissed with a reasoned reply)."
---

Load and follow the `wise-pr-watch-auto` skill with arguments: $ARGUMENTS
