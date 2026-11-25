# Technical Decisions & Architectural Trade-offs

This document captures the design rationale, trade-offs, and intentional scope boundaries for the Customer Entitlement & Support Case API Platform.

---

## 1. Stack Selection: Python 3 + FastAPI + SQLAlchemy Async

- **Why Python + FastAPI:** Native `asyncio` concurrency lets `GET /v1/customers/{id}` fan out to Entitlement, Usage, and Case upstreams concurrently (`asyncio.gather`), while Pydantic v2 enforces strict contract boundaries and automatically powers OpenAPI validation. Python also allows the API and the official `mcp` SDK server to share domain models, security helpers, and auth primitives without cross-language drift.
- **When Go or Java would win:** At >20k RPS sustained throughput or strict low-tail-latency budgets without GC pauses, Go would reduce memory footprint per container.

## 2. Why REST Here (vs. GraphQL or gRPC)

| Protocol | Why Chosen / Where It Fits |
|---|---|
| **REST (Chosen)** | Best fit for heterogeneous internal consumers (MuleSoft/iPaaS connectors, Postman, curl, webhook callbacks, and HTTP-backed MCP tools). Gives us native HTTP semantics: `ETag` / `If-Match` for optimistic concurrency, `If-None-Match` (`304`), `Idempotency-Key`, `Retry-After`, and RFC 9457 `application/problem+json`. |
| **GraphQL** | Better when UI teams (e.g., an internal React customer-360 console) query unpredictable nested graphs across 10+ microservices and need client-driven field selection to avoid over-fetching. Overkill for iPaaS and webhook workflows. |
| **gRPC** | Better for high-throughput internal east-west service-to-service traffic (e.g., API gateway -> billing engine) where Protobuf binary framing, strict contracts, and bidirectional streaming matter more than browser/iPaaS compatibility. |

## 3. Resource Modelling: Aggregation vs. Pass-Through + Anti-Corruption Layer

- **Aggregated (`GET /v1/customers/{id}`):** Combines NetSuite entitlement + Snowflake usage + Salesforce open-case count so callers do not execute 3-way fan-outs or implement partial-failure logic themselves.
- **Anti-Corruption Layer (`src/services/case_service.py`):** Upstream systems use inconsistent naming (`case_id`, `created_at`, `"In Progress"`, `"Waiting on Customer"` in Salesforce; `contract_end` in CSV vs `<renewal>` in XML). Our service layer normalizes everything into one canonical contract, insulating consumers from SaaS schema churn.

## 4. Resilience & Partial Failure Strategy

- **Timeouts + Exponential Backoff w/ Full Jitter (`src/utils/resilience.py`):** Prevents retry storms against struggling upstreams.
- **Per-Service Circuit Breaker:** Tracks `case-system`, `entitlement-system`, and `usage-warehouse` independently (`closed` -> `open` -> `half_open`), exposed on `GET /health`.
- **Graceful Degradation:** In `CustomerService.get_customer_summary`, Entitlement is the primary anchor (404/502/503 on failure), whereas Usage Warehouse and Case counts are non-critical enrichments. If Snowflake suspends or times out, the API still returns `200 OK` with `usage: []` and `degraded_sources: ["usage-warehouse"]`.

## 5. Cursor Pagination over Offset Pagination

`GET /v1/cases` uses opaque Base64 cursors (`next_cursor`, `has_more`, `total_count`). Unlike `OFFSET/LIMIT`, cursor pagination remains stable under concurrent case creation and scales in production databases via indexed keyset seeks (`WHERE (opened_at, id) < (:ts, :id)`).

## 6. Database-Backed Idempotency & Owned State (`src/models/database.py`)

We own three tables in SQLite (`aiosqlite` with WAL mode):
1. `idempotency_records` (PK `key`, `expires_at` index): Survives process restarts (unlike an in-memory dict) and detects `409 Conflict` when the same `Idempotency-Key` is replayed with a modified request body.
2. `audit_log` (indexed on `correlation_id` and `created_at`): Immutable compliance trail linking HTTP correlation IDs to mutations.
3. `webhook_subscriptions`: Co-located in the same transactional store as audit records.

## 7. MCP Tool Design vs. REST Resources (`mcp/server.py`)

MCP tools are **task-oriented**, not 1:1 mirrors of REST endpoints:
- **Token efficiency:** `search_cases` returns compact summaries (`id`, `account_id`, `priority`, `status`, `subject`, `age_hours`, `comment_count`, `etag`) rather than dumping multi-kilobyte comment histories into the LLM context window. `get_customer_summary` pre-computes `days_until_renewal` and `usage_trend` (`growing`/`declining`/`flat`) so the LLM does not waste tokens or make date-math mistakes.
- **Enums over free text:** `status` and `priority` use `Literal[...]` enums so the model cannot hallucinate invalid filter strings.
- **Safety-first writes:** `add_case_comment` and `escalate_case` expose an explicit `confirmed: bool = False` HITL gate and encapsulate `ETag` fetching/If-Match headers internally so the LLM cannot break optimistic concurrency.

## 8. Intentional Scope Cuts (What I Skipped & How I Would Build It)

| Skipped Capability | Production Implementation Plan |
|---|---|
| **Real IdP / JWKS (Auth0/Okta)** | Replace local HS256 secret in `src/middleware/auth.py` with RS256 JWKS key rotation + caching (`PyJWKClient`), enforcing `aud` and `iss` against the corporate IdP. |
| **Async Event Bus (Kafka / EventBridge)** | Currently `WebhookService.dispatch_event` dispatches in-request with a short timeout. In production, `escalate_case` would write an outbox row in the same DB transaction (`Transactional Outbox`), and a worker/Debezium relay would publish `case.escalated` to Kafka/EventBridge with dead-letter queues (DLQ) and exponential webhook retries. |
| **Distributed Cache & Rate Limiter (Redis)** | Replace in-process token-bucket (`RateLimitMiddleware`) and `PerAgentRateLimiter` with Redis + Lua atomic sliding-window counters so horizontal replicas share limits. |
| **Kubernetes / Helm Deployment** | Package `Dockerfile` with a Helm chart featuring `HorizontalPodAutoscaler`, `PodDisruptionBudget`, `/health` liveness and `/ready` readiness probes, and Vault/ExternalSecrets injection. |
