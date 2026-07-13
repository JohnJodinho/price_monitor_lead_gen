from functools import lru_cache
from typing import Literal
from pydantic import SecretStr, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    environment: Literal["development", "staging", "production"] = Field(
        default="production", alias="ENV"
    )

    DATABASE_URL: str = Field(..., alias="DATABASE_URL")

    GROQ_API_KEY: SecretStr = Field(..., alias="GROQ_API_KEY")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
    )


@lru_cache()
def get_settings() -> Settings:
    """Get application settings with caching."""

    return Settings()
