# Customer Entitlement & Support Case API Platform

A contract-first REST platform and Model Context Protocol (MCP) server unifying **Support Cases** (Salesforce-like), **Customer Entitlements** (NetSuite-like CSV + XML), and **Product Usage** (Snowflake-like telemetry) for both traditional iPaaS/API consumers and AI Support Copilot agents.

---

## Architecture Overview

```text
  ┌─────────────────────────┐          ┌──────────────────────────────────┐
  │  Traditional Consumers  │          │   AI Agent / Support Copilot     │
  │  (MuleSoft, Portal, QA) │          │   (Claude Desktop / Cursor)      │
  └────────────┬────────────┘          └────────────────┬─────────────────┘
               │ HTTPS + OAuth2 JWT                     │ MCP (stdio / HTTP)
               │                                        ▼
               │                       ┌──────────────────────────────────┐
               │                       │        MCP Server (mcp/)         │
               │                       │  • 4 Deliberate LLM Tools        │
               │                       │  • OBO Identity & Scope Flow     │
               │                       │  • Prompt Injection & PII Guard  │
               │                       │  • HITL Confirmation & Audit     │
               │                       └────────────────┬─────────────────┘
               │                                        │ Bearer Token
               ▼                                        ▼
  ┌───────────────────────────────────────────────────────────────────────┐
  │               FastAPI Platform Layer (src/)                           │
  │  • CorrelationIdMiddleware -> RateLimitMiddleware (Token Bucket)      │
  │  • OAuth2 Scope Guard + Per-Field Auth (finance:read -> arr_usd)     │
  │  • Anti-Corruption Layer (ACL) normalizing upstream schemas           │
  │  • Resilience: Timeout + Retry w/ Jitter + Per-Service CircuitBreaker │
  │  • Owned State (SQLite): Idempotency, Audit Log, Webhook Subs         │
  └──────────┬──────────────────────────┬────────────────────────┬────────┘
             │                          │                        │
             ▼                          ▼                        ▼
  ┌────────────────────┐   ┌────────────────────────┐   ┌─────────────────┐
  │  Case Mock (JSON)  │   │ Entitlement Mock       │   │ Usage Warehouse │
  │  (Salesforce-like) │   │ (NetSuite CSV + XML)   │   │ (Snowflake-like)│
  └────────────────────┘   └────────────────────────┘   └─────────────────┘
```

---

## Quick Start (Under 5 Minutes)

### Option 1: Local Virtual Environment (`make`)
```bash
make install        # Creates .venv and installs runtime + dev dependencies
make run            # Starts FastAPI on http://localhost:8000 (Swagger at /docs)
```

### Option 2: Docker Compose
```bash
docker compose up --build
```

### Run Tests & Contract Checks
```bash
make test           # Runs all 29 unit, contract, and integration tests + coverage
make demo           # Runs reproducible Support Copilot MCP demo transcript
```

### Generate a Dev JWT for `curl` or Postman
```bash
.venv/bin/python scripts/generate_dev_token.py
# Or without finance:read to verify ARR redaction:
.venv/bin/python scripts/generate_dev_token.py cases:read customers:read cases:write
```

---

## API Endpoints Summary

| Method & Path | Scope | Description | Key Protections |
|---|---|---|---|
| `GET /v1/customers/{id}` | `customers:read` | Aggregated entitlement, usage, and open case count | Partial-failure degradation (`degraded_sources`), `arr_usd` gated by `finance:read`, `ETag` / `304` |
| `GET /v1/cases` | `cases:read` | Filter, sort, and cursor-paginate cases | Opaque Base64 cursor, ACL normalization |
| `GET /v1/cases/{id}` | `cases:read` | Fetch single case | Emits `ETag` header |
| `POST /v1/cases/{id}/comments` | `cases:write` | Idempotent comment append | `Idempotency-Key` (24h TTL, `409` on mismatch), input sanitization |
| `POST /v1/cases/{id}/escalate` | `cases:write` | Escalate open/in-progress case | `If-Match` ETag guard (`412`), SLA routing, `case.escalated` webhook |
| `POST /v1/webhooks` | `cases:write` | Register webhook subscription | Secret returned once; HMAC-SHA256 delivery |
| `GET /health`, `GET /ready` | Public | Liveness (with circuit-breaker states) & DB readiness | Kubernetes-ready probes |

---

## Project Structure

```text
.
├── openapi.yaml                 # Part A: Contract-first OpenAPI 3.1 specification
├── pyproject.toml               # Dependencies and pytest configuration
├── Makefile / Dockerfile        # Single-command run, test, and container setup
├── docker-compose.yml
├── mock_services/               # Simulated Salesforce, NetSuite (CSV+XML), Snowflake
│   └── data/                    # JSON, CSV, and XML fixtures
├── src/
│   ├── main.py                  # FastAPI app, lifespan, RFC 9457 ProblemDetail handlers
│   ├── config.py                # Pydantic Settings
│   ├── api/                     # Routers: customers, cases, webhooks, health
│   ├── middleware/              # OAuth2 JWT + scopes, token-bucket rate limiter
│   ├── models/                  # Pydantic v2 schemas + async SQLite/SQLAlchemy tables
│   ├── services/                # ACL normalization, resilience, aggregation, webhooks
│   └── utils/                   # Correlation ID, circuit breaker, retry, HMAC, PII/ARR redaction
├── mcp/
│   ├── server.py                # Part C: MCP 2.x server (4 deliberate LLM tools)
│   ├── guardrails.py            # Prompt injection, PII, size limit, HITL, rate limit, audit
│   ├── auth.py                  # On-Behalf-Of (OBO) identity & scope propagation
│   ├── demo_transcript.py       # End-to-end executable Copilot demo
│   └── mcp_config.json          # Claude Desktop / Cursor MCP configuration
├── tests/                       # Unit, OpenAPI contract, and integration test suites
├── postman/                     # Postman collection + local environment
├── DECISIONS.md                 # Architectural trade-offs & skipped items
├── SECURITY.md                  # Security controls & AI governance
├── TESTEVALS.md                 # MCP evaluation suite & failure case analysis
└── AI_USAGE.md                  # AI coding assistant log & verification workflow
```
