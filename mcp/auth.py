"""Authentication and On-Behalf-Of (OBO) scope propagation for the MCP server.

The agent NEVER runs as a super-user. Every MCP tool call executes under an
``AgentAuthContext`` derived from the invoking user's OAuth 2.0 bearer token,
propagating both identity (``sub``) and granted ``scopes`` to the backend REST API.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from fastapi import HTTPException

from src.middleware.auth import _decode_token, create_mock_token


@dataclass(frozen=True)
class AgentAuthContext:
    """Identity and scope context carried from User -> Agent -> MCP -> REST API."""

    user_id: str
    agent_id: str
    email: str
    scopes: tuple[str, ...]
    bearer_token: str
    confirmed_actions: tuple[str, ...] = field(default_factory=tuple)

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


def validate_agent_token(
    token: str,
    *,
    agent_id: str = "support-copilot-v1",
) -> AgentAuthContext:
    """Decode and validate an OAuth 2.0 JWT and bind it to an ``AgentAuthContext``."""
    try:
        claims = _decode_token(token)
    except HTTPException as exc:
        raise PermissionError(f"Agent token validation failed: {exc.detail}") from exc

    raw_scope = claims.get("scope", "")
    scopes = tuple(s for s in raw_scope.split() if s)
    return AgentAuthContext(
        user_id=claims.get("sub", "unknown"),
        agent_id=agent_id,
        email=claims.get("email", ""),
        scopes=scopes,
        bearer_token=token,
    )


def default_env_auth_context() -> AgentAuthContext:
    """Construct an auth context from environment variables (or safe least-privilege dev token).

    Note: By default, ``finance:read`` is NOT granted so ARR remains redacted
    unless the caller explicitly presents a token carrying ``finance:read``.
    """
    agent_id = os.environ.get("MCP_AGENT_ID", "support-copilot-v1")
    token = os.environ.get("API_TOKEN", "").strip()
    if not token:
        token = create_mock_token(
            sub="copilot-user-01",
            email="support.analyst@example.com",
            name="Support Analyst (via Copilot)",
            scopes=["cases:read", "cases:write", "customers:read"],
        )
    return validate_agent_token(token, agent_id=agent_id)
