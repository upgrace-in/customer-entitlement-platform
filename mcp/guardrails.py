"""AI & MCP safety guardrails: prompt injection defense, PII redaction, HITL, rate limits, audit."""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.utils.security import redact_pii, sanitize_input


@dataclass(frozen=True)
class InjectionScanResult:
    detected: bool
    matched_patterns: list[str]
    sanitized_text: str


class PromptInjectionDetector:
    """Detects and neutralizes indirect prompt injection payloads embedded in case data."""

    _PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
        ("ignore_instructions", re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.I)),
        ("system_override", re.compile(r"\b(system|assistant|developer)\s*:\s*", re.I)),
        ("jailbreak_roleplay", re.compile(r"\b(you\s+are\s+now|act\s+as\s+admin|bypass\s+safety)\b", re.I)),
        ("bulk_escalate_cmd", re.compile(r"\bescalate\s+all\s+(cases|tickets)\b", re.I)),
        ("exfiltrate_cmd", re.compile(r"\b(dump|reveal|print)\s+(system\s+prompt|secrets?|tokens?|arr)\b", re.I)),
    )

    def scan(self, text: str) -> InjectionScanResult:
        matched: list[str] = []
        sanitized = text
        for name, pattern in self._PATTERNS:
            if pattern.search(text):
                matched.append(name)
                sanitized = pattern.sub("[BLOCKED_INSTRUCTION]", sanitized)
        return InjectionScanResult(
            detected=bool(matched),
            matched_patterns=matched,
            sanitized_text=sanitized,
        )


class PIIRedactor:
    """Wraps core PII redaction for untrusted data returned into LLM context windows."""

    @staticmethod
    def redact(text: str) -> str:
        return redact_pii(text)


class OutputSizeLimiter:
    """Caps serialized MCP tool output to protect LLM context budgets and prevent data dumps."""

    def __init__(self, max_chars: int = 4000) -> None:
        self.max_chars = max_chars

    def enforce(self, payload: dict[str, Any]) -> dict[str, Any]:
        serialized = json.dumps(payload, default=str)
        if len(serialized) <= self.max_chars:
            return payload

        # Trim list-valued fields progressively until within budget
        trimmed = dict(payload)
        for key in ("cases", "usage", "recent_comments"):
            val = trimmed.get(key)
            if isinstance(val, list) and len(val) > 1:
                while len(val) > 1 and len(json.dumps(trimmed, default=str)) > self.max_chars:
                    val.pop()
                trimmed["truncated"] = True
        serialized = json.dumps(trimmed, default=str)
        if len(serialized) > self.max_chars:
            trimmed = {
                "truncated": True,
                "preview": serialized[: self.max_chars - 64] + "...",
            }
        return trimmed


class PerAgentRateLimiter:
    """Sliding-window per-agent rate limiter for MCP tool invocations."""

    def __init__(self, max_calls: int = 30, window_seconds: float = 60.0) -> None:
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._calls: dict[str, deque[float]] = defaultdict(deque)

    def check_and_record(self, agent_id: str) -> None:
        now = time.monotonic()
        q = self._calls[agent_id]
        cutoff = now - self.window_seconds
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= self.max_calls:
            raise RuntimeError(
                f"Agent '{agent_id}' exceeded MCP rate limit ({self.max_calls} calls/{int(self.window_seconds)}s)."
            )
        q.append(now)


@dataclass
class ToolAuditEntry:
    timestamp: str
    agent_id: str
    user_id: str
    tool_name: str
    params: dict[str, Any]
    status: str
    result_summary: str


class AuditLogger:
    """Append-only in-memory + structured audit trail for every MCP tool call."""

    def __init__(self) -> None:
        self.entries: list[ToolAuditEntry] = []

    def record(
        self,
        *,
        agent_id: str,
        user_id: str,
        tool_name: str,
        params: dict[str, Any],
        status: str,
        result_summary: str,
    ) -> ToolAuditEntry:
        entry = ToolAuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            agent_id=agent_id,
            user_id=user_id,
            tool_name=tool_name,
            params=params,
            status=status,
            result_summary=result_summary[:300],
        )
        self.entries.append(entry)
        return entry


class ActionConfirmation:
    """Human-in-the-loop (HITL) gate for mutating / high-risk MCP tools."""

    @staticmethod
    def require_confirmation(
        *,
        action_name: str,
        target_id: str,
        risk_level: str,
        summary: str,
        confirmed: bool,
    ) -> dict[str, Any] | None:
        if confirmed:
            return None
        return {
            "status": "confirmation_required",
            "risk_level": risk_level,
            "action": action_name,
            "target_id": target_id,
            "message": (
                f"Human confirmation required before executing '{action_name}' on '{target_id}'. "
                f"Summary: {summary}. Re-invoke with confirmed=true only after explicit user approval."
            ),
        }


class InputSanitizer:
    """Sanitizes LLM-supplied arguments before forwarding to REST API."""

    @staticmethod
    def clean(text: str) -> str:
        return sanitize_input(text)


@dataclass
class GuardrailSuite:
    """Bundle of all MCP safety guardrails."""

    injection_detector: PromptInjectionDetector = field(default_factory=PromptInjectionDetector)
    pii_redactor: PIIRedactor = field(default_factory=PIIRedactor)
    size_limiter: OutputSizeLimiter = field(default_factory=OutputSizeLimiter)
    rate_limiter: PerAgentRateLimiter = field(default_factory=PerAgentRateLimiter)
    audit_logger: AuditLogger = field(default_factory=AuditLogger)
    confirmation: ActionConfirmation = field(default_factory=ActionConfirmation)
    sanitizer: InputSanitizer = field(default_factory=InputSanitizer)
