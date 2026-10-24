"""
Deployment settings read from the environment / .env and validated at startup.

Anything an operator may change per deployment lives here; a bad value (unknown provider, negative limit,
unknown risk level) fails fast with a clear error instead of misbehaving later.
"""

from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Provider = Literal["claude_code", "anthropic", "bedrock"]
_RISKS = ("HIGH", "MEDIUM", "LOW")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    llm_provider: Provider = "claude_code"
    aws_region: str = "us-east-1"
    chroma_dir: str = "chroma_db"

    app_password: str = ""
    api_key: str = Field(default="", description="If set, the HTTP API requires it in the X-API-Key header.")
    max_calls_per_hour: int = Field(default=200, gt=0)
    max_questions_per_hour: int = Field(default=30, gt=0)
    max_files_per_session: int = Field(default=20, gt=0)
    scan_excluded_risks: Annotated[list[str], NoDecode] = ["HIGH"]
    claude_token_file: str = "/run/secrets/claude_code_oauth_token"  # noqa: S105 - a file path, not a secret
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    retrieval_mode: Literal["semantic", "hybrid"] = "hybrid"
    rerank: bool = False

    @field_validator("llm_provider", mode="before")
    @classmethod
    def _lower(cls, value: str) -> str:
        return str(value).strip().lower()

    @field_validator("scan_excluded_risks", mode="before")
    @classmethod
    def _parse_risks(cls, value: object) -> list[str]:
        items = value.split(",") if isinstance(value, str) else list(value)  # type: ignore[call-overload]
        risks = [str(item).strip().upper() for item in items if str(item).strip()]
        unknown = [r for r in risks if r not in _RISKS]
        if unknown:
            raise ValueError(f"unknown risk level(s) {unknown}; use HIGH, MEDIUM and/or LOW")
        return risks


def load_settings() -> Settings:
    return Settings()
