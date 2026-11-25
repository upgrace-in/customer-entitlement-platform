"""OAuth 2.0 mock authentication middleware for local development.

Provides:

* Mock JWT creation (HS256) so integration tests and local dev do not
  require an external identity provider.
* A FastAPI dependency (:func:`get_current_user`) that extracts and
  validates the ``Authorization: Bearer <token>`` header.
* A dependency factory (:func:`require_scopes`) that enforces
  scope-based access control.

.. warning::

   The signing key is hard-coded for **local/test use only**.  In
   production, replace the token verification with your IdP's JWKS
   endpoint and RS256 validation.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuration — override via environment in production.
# ---------------------------------------------------------------------------

_JWT_SECRET: str = "dev-secret-do-not-use-in-production"
_JWT_ALGORITHM: str = "HS256"
_JWT_ISSUER: str = "entitlements-api-dev"
_JWT_AUDIENCE: str = "entitlements-api"
_JWT_EXPIRY_MINUTES: int = 60


# ---------------------------------------------------------------------------
# User info model
# ---------------------------------------------------------------------------

class CurrentUser(BaseModel):
    """Decoded identity and granted scopes for the authenticated caller."""

    sub: str = Field(description="Subject (user ID).")
    email: str = Field(default="", description="Email claim, if present.")
    name: str = Field(default="", description="Display name claim, if present.")
    scopes: list[str] = Field(
        default_factory=list,
        description="OAuth scopes granted to this token.",
    )
    raw_claims: dict[str, Any] = Field(
        default_factory=dict,
        description="Complete decoded JWT payload.",
    )


# ---------------------------------------------------------------------------
# Mock JWT generation (for tests & local dev)
# ---------------------------------------------------------------------------

def create_mock_token(
    sub: str,
    *,
    email: str = "",
    name: str = "",
    scopes: list[str] | None = None,
    extra_claims: dict[str, Any] | None = None,
    expires_in_minutes: int = _JWT_EXPIRY_MINUTES,
) -> str:
    """Create a signed HS256 JWT for local development and testing.

    Parameters
    ----------
    sub:
        Subject / user identifier.
    email:
        Optional email claim.
    name:
        Optional display-name claim.
    scopes:
        OAuth scopes to embed in the ``scope`` claim (space-separated).
    extra_claims:
        Arbitrary additional claims merged into the payload.
    expires_in_minutes:
        Token lifetime.

    Returns
    -------
    str
        Encoded JWT string.
    """
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": sub,
        "iss": _JWT_ISSUER,
        "aud": _JWT_AUDIENCE,
        "iat": now,
        "exp": now + timedelta(minutes=expires_in_minutes),
        "email": email,
        "name": name,
        "scope": " ".join(scopes or []),
    }
    if extra_claims:
        payload.update(extra_claims)

    return jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALGORITHM)


# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------

_bearer_scheme = HTTPBearer(auto_error=False)


def _decode_token(token: str) -> dict[str, Any]:
    """Decode and verify an HS256 JWT.

    Raises
    ------
    HTTPException
        401 on any verification failure.
    """
    try:
        payload = jwt.decode(
            token,
            _JWT_SECRET,
            algorithms=[_JWT_ALGORITHM],
            issuer=_JWT_ISSUER,
            audience=_JWT_AUDIENCE,
            options={"require": ["exp", "sub", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

async def get_current_user(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ] = None,
) -> CurrentUser:
    """FastAPI dependency that returns the authenticated :class:`CurrentUser`.

    Extracts the ``Authorization: Bearer <token>`` header, verifies the
    JWT, and returns structured user info including scopes.

    Raises
    ------
    HTTPException
        401 if the header is missing or the token is invalid/expired.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = _decode_token(credentials.credentials)

    return CurrentUser(
        sub=payload.get("sub", ""),
        email=payload.get("email", ""),
        name=payload.get("name", ""),
        scopes=payload.get("scope", "").split() if payload.get("scope") else [],
        raw_claims=payload,
    )


def require_scopes(*required: str):
    """Dependency factory that enforces scope-based access control.

    Usage::

        @router.get(
            "/cases",
            dependencies=[Depends(require_scopes("cases:read"))],
        )
        async def list_cases(...):
            ...

    Parameters
    ----------
    *required:
        One or more scope strings that the token **must** contain.

    Returns
    -------
    Callable
        A FastAPI dependency that raises 403 if scopes are insufficient.
    """

    async def _check(
        user: Annotated[CurrentUser, Depends(get_current_user)],
    ) -> CurrentUser:
        missing = set(required) - set(user.scopes)
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient scopes. Missing: {', '.join(sorted(missing))}",
            )
        return user

    return Depends(_check)
