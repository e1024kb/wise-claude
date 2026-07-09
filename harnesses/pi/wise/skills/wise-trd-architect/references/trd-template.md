# TRD Template

Use this template as the structural backbone. Adapt section depth to the system's complexity — a simple API endpoint needs less detail than a distributed event pipeline. Every section must be present, even if brief.

---

```markdown
# [System/Feature Name] — Technical Requirements Document

## Metadata

| Field | Value |
|-------|-------|
| **Document Owner** | [Name / Role] |
| **Status** | Draft |
| **Version** | v0.1 |
| **Last Updated** | [Date] |
| **Companion PRD** | [Link to PRD, or "N/A — standalone"] |
| **Target Branch** | [branch name, if applicable] |

## Changelog

| Date | Version | Changes |
|------|---------|---------|
| [YYYY-MM-DD] | v0.1 | Initial draft |

---

## 1. Executive Summary

[2-3 sentences. What system are we building/changing, what's the core technical challenge, and what approach are we taking?]

**Key technical decisions:**
- **Architecture:** [one sentence — e.g., "event-driven microservice extending the existing order pipeline"]
- **Primary data store:** [e.g., "PostgreSQL with read replicas"]
- **Key trade-off:** [the single most important trade-off — e.g., "eventual consistency for 10x throughput"]

---

## 2. Context & References

### Problem Context
[Brief recap of WHAT we're building and WHY — reference the PRD if one exists rather than duplicating it.]

### Relevant PRD Requirements
[List the PRD requirements that drive technical decisions. Reference by ID.]

| PRD Ref | Requirement Summary | Technical Implication |
|---------|--------------------|-----------------------|
| FR-01 | [requirement] | [what this means technically] |
| NFR-02 | [requirement] | [what this means technically] |

### Existing System Context
[What currently exists that this builds on or interacts with.]

---

## 3. Architecture Overview

### System Diagram

```
[ASCII diagram showing components, data flow, and external dependencies]

┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Client    │────►│  API        │────►│  Service    │
│   (Web/App) │     │  Gateway    │     │    Layer    │
└─────────────┘     └──────┬──────┘     └──────┬──────┘
                           │                    │
                    ┌──────▼──────┐      ┌──────▼──────┐
                    │   Auth      │      │  Database   │
                    │   Service   │      │  (Postgres) │
                    └─────────────┘      └─────────────┘
```

### Component Breakdown

| Component | Responsibility | New/Existing | Technology |
|-----------|---------------|--------------|------------|
| [name] | [what it does] | [New / Extend / Unchanged] | [tech choice] |

### What Stays the Same
[Explicitly list components and behaviors that should NOT change. This prevents accidental regressions.]

- [component/behavior] — unchanged because [reason]

---

## 4. Technical Decisions

Record every significant architectural choice. Each decision should be traceable to a requirement or constraint.

### Decision 1: [Decision Name]

| Aspect | Detail |
|--------|--------|
| **Decision** | [what was decided] |
| **Options Considered** | [A, B, C] |
| **Choice** | [which option] |
| **Rationale** | [why — tied to requirements, constraints, or evidence] |

**Why not [rejected option]:** [brief explanation grounded in the project's actual constraints]

### Decision 2: [Decision Name]
[Same structure]

### Decision Summary Table

| # | Decision | Choice | Key Rationale |
|---|----------|--------|---------------|
| D1 | [decision] | [choice] | [one-line rationale] |
| D2 | [decision] | [choice] | [one-line rationale] |

---

## 5. Data Model

### Schema

[Define tables/collections with field types, constraints, and relationships]

#### `[table_name]`

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `id` | UUID | PK | Primary identifier |
| `[field]` | [type] | [NOT NULL, FK, UNIQUE, etc.] | [purpose] |
| `created_at` | TIMESTAMP | NOT NULL, DEFAULT NOW() | Record creation time |
| `updated_at` | TIMESTAMP | NOT NULL | Last modification time |

**Indexes:**
- `idx_[table]_[field]` on `[field]` — [why this index exists, expected query pattern]

**Relationships:**
- `[table_a].[field]` → `[table_b].[field]` (one-to-many)

### Data Volume Estimates

| Entity | Current Count | Projected (12mo) | Growth Rate |
|--------|--------------|-------------------|-------------|
| [entity] | [count] | [projection] | [rate] |

### Migration Strategy
[How to get from current schema to target schema without downtime]

- **Approach:** [online migration / blue-green / expand-contract]
- **Steps:** [ordered list of migration steps]
- **Rollback plan:** [how to undo if something goes wrong]

---

## 6. API Contracts

### [Endpoint Group Name]

#### `[METHOD] /api/v1/[path]`

**Purpose:** [what this endpoint does]

**Authentication:** [required auth — e.g., Bearer token, API key]

**Request:**
```json
{
  "field_name": "string (required) — description",
  "optional_field": "number (optional, default: 10) — description"
}
```

**Response (200):**
```json
{
  "id": "uuid",
  "field_name": "string",
  "created_at": "ISO 8601 timestamp"
}
```

**Error Responses:**

| Status | Code | Description |
|--------|------|-------------|
| 400 | `INVALID_INPUT` | [when this occurs] |
| 404 | `NOT_FOUND` | [when this occurs] |
| 429 | `RATE_LIMITED` | [rate limit details] |

**Rate Limits:** [requests per second/minute per client]

---

## 7. Integration Points

| External System | Direction | Protocol | Auth | Failure Mode |
|----------------|-----------|----------|------|-------------|
| [system] | [Inbound/Outbound/Bidirectional] | [REST/gRPC/Kafka/etc.] | [auth method] | [what happens when it's down] |

### [Integration Name] Details

- **Endpoint/Topic:** [specific endpoint or message topic]
- **Data format:** [JSON schema, Protobuf definition, Avro schema]
- **Retry strategy:** [exponential backoff, dead letter queue, circuit breaker]
- **SLA dependency:** [what latency/availability we depend on]

---

## 8. Security Architecture

### Authentication & Authorization
- **Auth mechanism:** [OAuth2, JWT, API keys, mTLS]
- **Authorization model:** [RBAC, ABAC, resource-based]
- **Token lifecycle:** [expiration, refresh, revocation]

### Data Protection
- **Encryption at rest:** [algorithm, key management]
- **Encryption in transit:** [TLS version, certificate management]
- **PII handling:** [which fields, how masked/encrypted, retention policy]

### Security Controls
- **Input validation:** [approach — schema validation, sanitization]
- **Rate limiting:** [strategy and thresholds]
- **Audit logging:** [what's logged, retention, access]
- **Secret management:** [vault, env vars, KMS — how secrets are stored and rotated]

---

## 9. Performance & Scalability

### Performance Targets

| Operation | Target (p50) | Target (p95) | Target (p99) | Measurement |
|-----------|-------------|-------------|-------------|-------------|
| [operation] | [ms] | [ms] | [ms] | [how measured] |

### Scalability Design
- **Horizontal scaling:** [what scales and how — auto-scaling triggers, instance limits]
- **Bottleneck analysis:** [identified bottlenecks and mitigations]
- **Caching strategy:** [what's cached, TTL, invalidation approach]
- **Connection pooling:** [database connections, HTTP clients — pool sizes and rationale]

### Load Estimates

| Metric | Current | Launch | 6 Months | 12 Months |
|--------|---------|--------|----------|-----------|
| Requests/sec (peak) | [n] | [n] | [n] | [n] |
| Concurrent users | [n] | [n] | [n] | [n] |
| Data storage (GB) | [n] | [n] | [n] | [n] |

---

## 10. Deployment & Infrastructure

### Deployment Model
- **Environment:** [cloud provider, region(s)]
- **Containerization:** [Docker, image registry]
- **Orchestration:** [K8s, ECS, serverless, bare metal]
- **CI/CD pipeline:** [how code gets from merge to production]

### Infrastructure Requirements
- **Compute:** [instance types, counts, auto-scaling rules]
- **Storage:** [database hosting, object storage, volume sizes]
- **Networking:** [VPC, subnets, load balancers, CDN]
- **Estimated monthly cost:** [breakdown by component]

### Rollout Strategy
- **Approach:** [blue-green, canary, rolling, feature flags]
- **Rollback trigger:** [what conditions trigger automatic rollback]
- **Rollback procedure:** [steps to reverse the deployment]

---

## 11. Observability

### Monitoring
- **Health checks:** [endpoints, frequency, alert thresholds]
- **Key metrics:** [what to track — latency, error rate, throughput, saturation]
- **Dashboards:** [what dashboards to create]

### Logging
- **Log format:** [structured JSON, log levels]
- **Key events to log:** [what and at what level]
- **Retention:** [duration, storage]

### Alerting

| Alert | Condition | Severity | Response |
|-------|-----------|----------|----------|
| [alert name] | [trigger condition] | [P1/P2/P3] | [runbook or action] |

---

## 12. Implementation Roadmap

Order phases by dependency. Each phase should be independently deployable and testable.

### Phase 1: [Name] — [estimated duration]
**Depends on:** nothing (foundation)
**Delivers:** [what becomes available after this phase]
- [ ] [task 1 — specific and actionable]
- [ ] [task 2]
- [ ] [verification step]

### Phase 2: [Name] — [estimated duration]
**Depends on:** Phase 1
**Delivers:** [what becomes available]
- [ ] [tasks]

### Phase 3: [Name] — [estimated duration]
**Depends on:** Phase 1, Phase 2
**Delivers:** [what becomes available]
- [ ] [tasks]

---

## 13. Technical Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| [risk] | [High/Med/Low] | [High/Med/Low] | [specific action, not "be careful"] |

---

## 14. Assumptions to Validate

[Unproven premises the design depends on. Each should have a validation method.]

| Assumption | Impact if Wrong | Validation Method | Owner |
|-----------|-----------------|-------------------|-------|
| [assumption] | [what breaks] | [how to test it] | [who] |

---

## 15. Open Technical Questions

| # | Question | Blocks | Proposed Resolution | Owner |
|---|----------|--------|--------------------| ------|
| 1 | [question] | [what's blocked] | [suggested approach] | [who] |
```

---

## Template Usage Notes

- **Section depth scales with risk.** A payment system's security section should be detailed. An internal admin tool's can be brief. Scale the analysis to the consequences.
- **Every section must exist.** Even "N/A — single-region deployment" in the multi-region section confirms you considered it.
- **Diagrams over prose.** For system interactions, data flow, and deployment topology, an ASCII diagram communicates more clearly than paragraphs.
- **Link, don't duplicate.** If the PRD already specifies something, reference it: "per FR-03 in [PRD link]".
- **Assumptions are first-class.** Every design is built on assumptions. Making them explicit means you can validate them early rather than discovering they're wrong during implementation.
