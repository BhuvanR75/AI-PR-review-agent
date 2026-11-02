"""Configuration. Every external edge is behind a provider switch so the whole spine
can run offline (LLM_PROVIDER=fake, EMBEDDING_PROVIDER=fake, GITHUB_CLIENT_MODE=dry_run)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

HitlMode = Literal["always", "confidence_gated"]
Provider = Literal["fake", "openai"]
GithubMode = Literal["dry_run", "live"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "ai-pr-review-agent"
    environment: Literal["dev", "test", "prod"] = "dev"

    # --- storage -------------------------------------------------------------
    # Plain postgres:// DSN. Repositories that need SQLAlchemy use .sqlalchemy_url.
    database_url: str = "postgresql://postgres:postgres@localhost:5433/prreview"
    redis_url: str = "redis://localhost:6380/0"
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10

    # --- github --------------------------------------------------------------
    github_webhook_secret: str = "dev-webhook-secret"
    github_token: str = ""
    github_api_base: str = "https://api.github.com"
    github_client_mode: GithubMode = "dry_run"
    outbox_dir: str = ".local/outbox"
    inbox_dir: str = ".local/inbox"
    # Comma-separated. Empty means "review every repo we are asked to".
    repo_allowlist: str = ""

    # --- llm -----------------------------------------------------------------
    llm_provider: Provider = "fake"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    model_routes: str = (
        "security=gpt-4o-mini,quality=gpt-4o-mini,tests=gpt-4o-mini,"
        "docs=gpt-4o-mini,aggregator=gpt-4o-mini"
    )
    llm_timeout_seconds: float = 45.0
    llm_max_retries: int = 3

    # --- embeddings ----------------------------------------------------------
    embedding_provider: Provider = "fake"
    embedding_model: str = "text-embedding-3-large"
    embedding_dim: int = 256

    # --- retrieval -----------------------------------------------------------
    top_k_chunks: int = 12
    rrf_k: int = 60

    # --- orchestration -------------------------------------------------------
    agent_timeout_seconds: float = 90.0
    recursion_limit: int = 25

    # --- human in the loop ---------------------------------------------------
    hitl_mode: HitlMode = "always"
    hitl_confidence_threshold: float = 0.75
    max_comments_per_review: int = 20

    # --- economics -----------------------------------------------------------
    daily_budget_usd: float = 25.0
    cost_per_1k_input_tokens: float = 0.00015
    cost_per_1k_output_tokens: float = 0.0006

    # --- api -----------------------------------------------------------------
    api_token: str = "dev-api-token"
    cors_origins: str = "http://localhost:3000"

    # --- derived -------------------------------------------------------------
    @property
    def sqlalchemy_url(self) -> str:
        """SQLAlchemy's asyncpg driver spells the scheme differently."""
        url = self.database_url
        if url.startswith("postgresql+asyncpg://"):
            return url
        if url.startswith("postgresql://"):
            return "postgresql+asyncpg://" + url[len("postgresql://") :]
        if url.startswith("postgres://"):
            return "postgresql+asyncpg://" + url[len("postgres://") :]
        return url

    @property
    def model_route_map(self) -> dict[str, str]:
        routes: dict[str, str] = {}
        for pair in self.model_routes.split(","):
            if "=" in pair:
                agent, model = pair.split("=", 1)
                routes[agent.strip()] = model.strip()
        return routes

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def repo_allowlist_set(self) -> set[str]:
        return {r.strip() for r in self.repo_allowlist.split(",") if r.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    """Used by tests and the CLI after mutating the environment."""
    get_settings.cache_clear()
    return get_settings()
