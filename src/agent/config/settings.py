import json
import logging

import boto3
from botocore.exceptions import ClientError
from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


def _fetch_secret(secret_name: str, region: str) -> str:
    """Fetch a secret value from AWS Secrets Manager.

    Handles both plain-string secrets and JSON secrets that contain an
    ANTHROPIC_API_KEY (or CLAUDE_API_KEY) field.  Returns the raw key string.
    Raises on any Secrets Manager error so the service fails fast at startup
    rather than silently running without credentials.
    """
    client = boto3.client("secretsmanager", region_name=region)
    try:
        response = client.get_secret_value(SecretId=secret_name)
    except ClientError as e:
        raise RuntimeError(
            f"Failed to fetch secret '{secret_name}' from Secrets Manager: {e}"
        ) from e

    raw = response.get("SecretString") or ""
    try:
        data = json.loads(raw)
        return (
            data.get("ANTHROPIC_API_KEY")
            or data.get("CLAUDE_API_KEY")
            or data.get("api_key")
            or raw
        )
    except (json.JSONDecodeError, AttributeError):
        return raw


def _fetch_langfuse_secrets(secret_name: str, region: str) -> dict:
    """Fetch Langfuse API keys from AWS Secrets Manager.

    Returns a dict with LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY.
    Langfuse is optional — returns an empty dict and logs a warning on failure
    rather than raising, so the service can still start without it.
    """
    client = boto3.client("secretsmanager", region_name=region)
    try:
        response = client.get_secret_value(SecretId=secret_name)
    except ClientError as e:
        logger.warning(
            "Langfuse secret '%s' not found in Secrets Manager: %s — prompt management disabled",
            secret_name,
            e,
        )
        return {}
    raw = response.get("SecretString") or ""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, AttributeError):
        return {}


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Server
    host: str = "0.0.0.0"
    port: int = 8001
    env: str = "dev"  # "dev" | "prod"

    # Claude API
    # Accept either ANTHROPIC_API_KEY (the canonical SDK name) or CLAUDE_API_KEY,
    # from the environment or src/agent/.env.  If neither is set, the key is
    # fetched from AWS Secrets Manager using anthropic_secret_name below.
    claude_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"),
    )
    claude_model: str = "claude-opus-4-8"  # most capable current model
    claude_max_tokens: int = 4096  # room for adaptive thinking + the structured answer
    claude_effort: str = "high"  # low | medium | high | max — thinking/spend tradeoff
    agent_max_tool_iterations: int = 5  # cap on the tool-calling loop

    # AWS Secrets Manager — set this to override the secret name per environment
    anthropic_secret_name: str = "agentify/dev/anthropic"

    # Backend service (for the agent's tool callbacks)
    backend_url: str = "http://localhost:8080"

    # Log platform (Glue/Athena test harness, ADR 0021) — empty means unconfigured,
    # log_router.get_logs() then always uses the live cluster. See infra/kubernetes/agent.yaml.
    athena_workgroup: str = Field(default="", validation_alias=AliasChoices("ATHENA_WORKGROUP"))
    athena_database: str = Field(default="", validation_alias=AliasChoices("ATHENA_DATABASE"))
    athena_table: str = Field(default="", validation_alias=AliasChoices("ATHENA_TABLE"))

    # Glue-based dependency mining (ADR 0029, ROADMAP P15/P18 use case #2) —
    # a periodic background task, gated on the same athena_* config above
    # (skips itself when unconfigured). Default matches Glue's hour-level
    # partition granularity (_partition_predicate) so each cycle scans
    # exactly the newly-landed partition rather than re-scanning old data.
    dependency_mining_interval_seconds: int = Field(
        default=3600, validation_alias=AliasChoices("DEPENDENCY_MINING_INTERVAL_SECONDS")
    )

    # Langfuse prompt management (optional — falls back to local strings if not set)
    # Keys are resolved from LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY env vars,
    # or fetched from AWS Secrets Manager using langfuse_secret_name below.
    langfuse_public_key: str = Field(default="", validation_alias=AliasChoices("LANGFUSE_PUBLIC_KEY"))
    langfuse_secret_key: str = Field(default="", validation_alias=AliasChoices("LANGFUSE_SECRET_KEY"))
    langfuse_base_url: str = Field(
        default="https://us.cloud.langfuse.com",
        validation_alias=AliasChoices("LANGFUSE_BASE_URL"),
    )
    langfuse_secret_name: str = "agentify/dev/langfuse"

    # Voyage AI — embedding model for semantic memory (P8).
    # Set VOYAGE_API_KEY to enable; omit to run without semantic similarity search.
    voyage_api_key: str = Field(default="", validation_alias=AliasChoices("VOYAGE_API_KEY"))
    voyage_model: str = "voyage-3-lite"   # 512-dim, cheapest; ~$0.00002 per trace

    # AWS
    aws_region: str = "ap-southeast-2"
    cloudwatch_log_group: str = "/agentify/agent"

    # Redis (for caching, future use)
    redis_url: str = "redis://localhost:6379"

    # Database (if agent needs to persist state)
    db_url: str = "postgresql://localhost/agentify"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False

    @model_validator(mode="after")
    def resolve_api_keys(self) -> "Settings":
        """Fetch API keys from Secrets Manager if not set via env/file."""
        if not self.claude_api_key:
            logger.info(
                "ANTHROPIC_API_KEY not set in environment; fetching from Secrets Manager",
                extra={"secret": self.anthropic_secret_name, "region": self.aws_region},
            )
            self.claude_api_key = _fetch_secret(self.anthropic_secret_name, self.aws_region)

        if not self.langfuse_public_key or not self.langfuse_secret_key:
            logger.info(
                "LANGFUSE keys not set in environment; fetching from Secrets Manager",
                extra={"secret": self.langfuse_secret_name, "region": self.aws_region},
            )
            lf = _fetch_langfuse_secrets(self.langfuse_secret_name, self.aws_region)
            if not self.langfuse_public_key:
                self.langfuse_public_key = lf.get("LANGFUSE_PUBLIC_KEY", "")
            if not self.langfuse_secret_key:
                self.langfuse_secret_key = lf.get("LANGFUSE_SECRET_KEY", "")

        return self


def get_settings() -> Settings:
    """Get application settings."""
    return Settings()
