"""Generate a mock OAuth 2.0 JWT token for local API / Postman testing."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.middleware.auth import create_mock_token


def main() -> None:
    scopes = sys.argv[1:] or [
        "cases:read",
        "cases:write",
        "customers:read",
        "finance:read",
    ]
    token = create_mock_token(
        sub="dev-engineer-01",
        email="engineer@example.com",
        name="Dev Engineer",
        scopes=scopes,
    )
    print(token)


if __name__ == "__main__":
    main()
