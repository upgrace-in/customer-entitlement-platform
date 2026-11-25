"""Application configuration using Pydantic Settings."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Centralized application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Customer Entitlement & Support Case API"
    app_version: str = "1.0.0"
    app_env: str = "development"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000

    database_url: str = "sqlite+aiosqlite:///./entitlements.db"
    jwt_secret: str = "dev-secret-do-not-use-in-production"
    log_level: str = "INFO"
    log_json: bool = True

    rate_limit_capacity: float = 100.0
    rate_limit_refill_rate: float = 10.0

    upstream_timeout_seconds: float = 3.0
    upstream_max_retries: int = 2
    circuit_breaker_threshold: int = 5
    circuit_breaker_recovery_seconds: float = 30.0

    idempotency_ttl_hours: int = 24


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings singleton."""
    return Settings()
