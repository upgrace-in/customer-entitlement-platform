# Security & Governance Architecture

This document details the security controls, data-loss preventions, and AI safety guardrails implemented across the REST API and MCP server.

---

## 1. Authentication, Authorization & Least Privilege

- **OAuth 2.0 Bearer JWTs (`src/middleware/auth.py`):** Every business endpoint requires a valid JWT (`401 Unauthorized` if missing/expired/invalid).
- **Resource & Action Scopes:**
  - `cases:read`: List and inspect support cases.
  - `cases:write`: Add comments, escalate cases, manage webhooks.
  - `customers:read`: Read aggregated customer profiles and usage.
  - `finance:read`: Required to view `arr_usd`.
- **Per-Field Authorization (`redact_arr` in `src/utils/security.py`):** Even when a caller holds `customers:read`, `arr_usd` is stripped server-side before serialization unless `finance:read` is present in token claims.
- **On-Behalf-Of (OBO) Agent Identity (`mcp/auth.py`):** The AI agent **never** runs with a shared super-user key. `AgentAuthContext` binds the user's `sub`, `email`, and exact OAuth `scopes` from the session token and forwards that bearer token to the REST API.

## 2. Data Integrity & Concurrency Controls

- **Idempotency Keys (`POST /v1/cases/{id}/comments`):** Prevents duplicate comments on network retries. Keys are SHA-256 hashed (`hash_idempotency_key`) scoped by `(user_id, case_id, key)` and stored for 24 hours. Replaying a key with a *different* payload returns `409 Conflict`.
- **Optimistic Concurrency (`ETag` / `If-Match`):** `POST /v1/cases/{id}/escalate` rejects stale state transitions with `412 Precondition Failed`, preventing race conditions between two engineers or an agent and a human.
- **Strict State Machine (`422 Business Rule Violation`):** Only `open` and `in_progress` cases may transition to `escalated`. Closed cases reject new comments.

## 3. Webhook Security

- **HMAC-SHA256 Payload Signing (`X-Signature-256`):** Outbound `case.escalated` deliveries sign the exact serialized JSON payload using the subscription's secret (`sha256=<hex>`), verified via constant-time `hmac.compare_digest` in `verify_hmac_signature` to prevent timing attacks.
- **Replay Protection Headers:** `X-Webhook-ID` (deduplication) and `X-Webhook-Timestamp` (consumers reject events older than 5 minutes).
- **Secret Handling:** Webhook signing secrets are returned once upon creation (`POST /v1/webhooks`) and omitted from `GET /v1/webhooks` listings.

## 4. AI & MCP Safety Guardrails (`mcp/guardrails.py`)

| Threat Vector | Mitigation Implemented |
|---|---|
| **Indirect Prompt Injection** (malicious text inside case subjects/comments) | `PromptInjectionDetector` scans all upstream case text *and* write arguments for instruction overrides (`ignore previous instructions`, `system:`, `escalate all cases`, `dump system prompt`). Read paths neutralize matched patterns (`[BLOCKED_INSTRUCTION]`) and emit `security_warning`; write paths reject with `Blocked by Security Guardrail`. |
| **Unauthorized Financial Data Exposure** | Defense-in-depth: default Copilot token omits `finance:read`, API omits `arr_usd`, and `get_customer_summary` additionally checks `auth.has_scope("finance:read")` and flags `arr_redacted: true`. |
| **PII Leakage into LLM Context** | `PIIRedactor` masks emails and phone numbers before text reaches the model context window. |
| **Unintended High-Risk Actions** | `ActionConfirmation` enforces a Human-in-the-Loop (HITL) gate (`confirmed=true` required) for `add_case_comment` (medium risk) and `escalate_case` (high risk). |
| **Context Bombing / Token Exhaustion** | `OutputSizeLimiter` caps serialized MCP tool outputs at 4,000 characters by truncating list tails cleanly. |
| **Runaway Agent Loops** | `PerAgentRateLimiter` enforces sliding-window per-`agent_id` call quotas (default 30 calls/60s), layered on top of the REST API's token-bucket `RateLimitMiddleware`. |
| **Forensic Accountability** | Every REST mutation writes to SQLite `audit_log` with `X-Correlation-ID` + `user_id`; every MCP tool invocation records a `ToolAuditEntry` (`timestamp`, `agent_id`, `user_id`, `tool_name`, `params`, `status`, `result_summary`). |
