# PRD Template

Use this template as the structural backbone for all PRDs. Adapt section depth based on the product's complexity — a simple internal tool needs less detail than a public-facing platform. But every section must be present, even if brief.

---

```markdown
# [Product/Feature Name] — Product Requirements Document

## Metadata

| Field | Value |
|-------|-------|
| **Document Owner** | [Name / Role] |
| **Status** | Draft |
| **Version** | v0.1 |
| **Target Release** | [Date or milestone] |
| **Last Updated** | [Date] |

## Changelog

| Date | Version | Changes |
|------|---------|---------|
| [YYYY-MM-DD] | v0.1 | Initial draft |

---

## 1. Executive Summary

[2-3 sentences maximum. What is this product/feature, and what user pain does it solve? A busy executive should understand the entire initiative from this section alone.]

**Key facts:**
- **Problem:** [one sentence]
- **Solution:** [one sentence]
- **Primary user:** [who]
- **Target outcome:** [the single most important metric]

---

## 2. Problem Statement

### The Problem
[Describe the specific user pain. Be concrete — what happens today that shouldn't? What can't users do that they need to?]

### Why Now
[What has changed that makes solving this urgent? Market shift, user growth, competitive pressure, regulatory deadline?]

### Current State
[How do users solve this today? What's broken about the current approach? Include workarounds users have built.]

---

## 3. Goals & Success Metrics

### Goals
[2-4 goals, each starting with a verb. These are outcomes, not features.]

1. [Reduce / Increase / Enable / Eliminate ... ]
2. [...]

### Success Metrics

| Metric | Current Baseline | Target | Timeframe |
|--------|-----------------|--------|-----------|
| [metric name] | [current value or "N/A"] | [specific target] | [when to measure] |
| [metric name] | [current value or "N/A"] | [specific target] | [when to measure] |

### Anti-Goals
[What are we explicitly NOT optimizing for? This prevents scope creep during implementation.]

---

## 4. Target Audience

### Primary Persona: [Name and Role]
- **Who they are:** [demographics, role, context]
- **Their situation:** [what their day looks like, relevant workflow]
- **Current pain:** [specific frustration with the status quo]
- **What success looks like for them:** [their words, not yours]

### Secondary Persona: [Name and Role]
[Same structure — only include if genuinely different needs affect requirements]

### Who This Is NOT For
[Explicitly list user types you are not designing for in v1. This prevents "but what about..." scope creep.]

---

## 5. Scope

### In Scope (v1)
[Bullet list of what this version will deliver. Be specific enough that there's no ambiguity.]

- [ ] [Feature/capability 1]
- [ ] [Feature/capability 2]
- [ ] [Feature/capability 3]

### Out of Scope (v1)
[Bullet list of features explicitly excluded. Include brief rationale for each — this prevents the same debates from recurring.]

- **[Feature X]** — Deferred because [reason]. Planned for [v2 / future evaluation / never].
- **[Feature Y]** — Excluded because [reason].

### Dependencies
[External systems, teams, approvals, or decisions this work depends on.]

| Dependency | Owner | Status | Impact if Delayed |
|------------|-------|--------|-------------------|
| [dependency] | [who owns it] | [status] | [what happens if it's late] |

---

## 6. User Stories & Acceptance Criteria

### Story 1: [Brief title]

**As a** [persona], **I want** [goal], **so that** [benefit].

**Acceptance Criteria:**

```gherkin
Given [initial context]
When [action the user takes]
Then [expected outcome]

Given [alternative context]
When [action the user takes]
Then [expected outcome]
```

**Edge cases:**
- [What happens when X fails?]
- [What happens with empty/invalid input?]

---

### Story 2: [Brief title]

[Same structure. Include as many stories as needed to fully cover the In Scope items.]

---

## 7. Functional Requirements

[Specific, testable "the system shall" statements. Number them for easy reference in design and testing.]

| ID | Requirement | Priority | Related Story |
|----|-------------|----------|---------------|
| FR-01 | The system shall [specific behavior] | Must Have | Story 1 |
| FR-02 | The system shall [specific behavior] | Must Have | Story 1 |
| FR-03 | The system shall [specific behavior] | Should Have | Story 2 |
| FR-04 | The system shall [specific behavior] | Could Have | Story 3 |

### Priority Definitions
- **Must Have:** Launch blocker. The product doesn't work without this.
- **Should Have:** Important for a good experience. Ship without it only if absolutely necessary.
- **Could Have:** Nice to have. Include if time permits.

---

## 8. Non-Functional Requirements

### Performance
| Metric | Requirement | Measurement Method |
|--------|-------------|--------------------|
| [e.g., API response time] | [e.g., < 200ms at p95] | [e.g., APM monitoring] |
| [e.g., Page load time] | [e.g., < 2s on 3G] | [e.g., Lighthouse CI] |

### Security
- [Specific security requirements: encryption standards, auth methods, data handling]
- [e.g., All PII encrypted at rest with AES-256]
- [e.g., Authentication via OAuth 2.0 with MFA support]

### Scalability
- [Expected load: concurrent users, requests/sec, data volume]
- [Growth projections over 6-12 months]

### Reliability
- [Uptime requirements: e.g., 99.9% availability]
- [Recovery requirements: e.g., RPO < 1 hour, RTO < 4 hours]

### Compliance
- [Regulatory requirements: GDPR, SOC2, HIPAA, PCI-DSS, etc.]
- [Data residency requirements]
- [Audit logging requirements]

### Accessibility
- [e.g., WCAG 2.1 AA compliance]

---

## 9. UX/Design Requirements

[High-level UX requirements — not wireframes, but constraints the design must satisfy.]

- [e.g., The primary workflow must be completable in under 3 clicks]
- [e.g., Mobile-responsive down to 375px width]
- [e.g., Must support dark mode]
- [e.g., Error messages must include a clear next action]

---

## 10. Open Questions

[Questions that need answers before or during implementation. Don't hide these — surface them prominently.]

| # | Question | Impact | Owner | Deadline |
|---|----------|--------|-------|----------|
| 1 | [question] | [what's blocked without an answer] | [who should answer] | [when needed by] |
| 2 | [question] | [what's blocked without an answer] | [who should answer] | [when needed by] |

---

## 11. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| [risk description] | [High/Med/Low] | [High/Med/Low] | [what to do about it] |

---

## 12. Release Criteria

[What must be true before this ships?]

- [ ] All "Must Have" functional requirements implemented and tested
- [ ] Performance benchmarks met under expected load
- [ ] Security review completed
- [ ] Acceptance criteria verified for all user stories
- [ ] Documentation updated (user-facing and internal)
- [ ] Monitoring and alerting configured
- [ ] Rollback plan documented
```

---

## Template Usage Notes

- **Section depth scales with complexity.** An internal admin tool might have 2 user stories and minimal NFRs. A public API might have 20 stories and detailed compliance requirements. Adjust accordingly.
- **Every section must exist.** Even if a section is brief ("N/A — internal tool with no compliance requirements"), its presence confirms you considered it rather than forgot it.
- **Numbers over adjectives.** Wherever the template shows a quantitative field, fill it with a number. "Fast" is not a requirement. "< 200ms p95" is.
- **Open Questions are a feature, not a bug.** A PRD that honestly surfaces what's unknown is more useful than one that papers over gaps with confident-sounding guesses.
