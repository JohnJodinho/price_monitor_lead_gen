import re
from functools import lru_cache
from typing import Literal
from pydantic import SecretStr, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    environment: Literal["development", "staging", "production"] = Field(
        default="production", alias="ENV"
    )

    DATABASE_URL: str = Field(..., alias="DATABASE_URL")

    GROQ_API_KEY: SecretStr = Field(..., alias="GROQ_API_KEY")

    # ── homepage crawler tunables ─────────────────────────────────────────────
    HOMEPAGE_MAX_PAGES: int = Field(default=8, alias="HOMEPAGE_MAX_PAGES")

    # Timeout in seconds for the "http" FetcherSession
    HOMEPAGE_T1_TIMEOUT: int = Field(default=30, alias="HOMEPAGE_T1_TIMEOUT")

    # Timeout in seconds for the "stealth" AsyncStealthySession
    # (converted to milliseconds inside configure_sessions())
    HOMEPAGE_T2_TIMEOUT: int = Field(default=60, alias="HOMEPAGE_T2_TIMEOUT")

    # Spider concurrency settings
    HOMEPAGE_CONCURRENT_REQUESTS: int = Field(default=1, alias="HOMEPAGE_CONCURRENT_REQUESTS")
    HOMEPAGE_CONCURRENT_REQUESTS_PER_DOMAIN: int = Field(
        default=0, alias="HOMEPAGE_CONCURRENT_REQUESTS_PER_DOMAIN"
    )
    HOMEPAGE_DOWNLOAD_DELAY: float = Field(default=0.5, alias="HOMEPAGE_DOWNLOAD_DELAY")
    HOMEPAGE_ROBOTS_TXT_OBEY: bool = Field(default=True, alias="HOMEPAGE_ROBOTS_TXT_OBEY")

    # Comma-delimited contact-page keywords; assembled into regex at startup
    HOMEPAGE_CONTACT_KEYWORDS: str = Field(
        default="contact,about,team,support,help,reach,people",
        alias="HOMEPAGE_CONTACT_KEYWORDS",
    )
    # The compiled pattern — populated by the validator below; not set from env
    HOMEPAGE_CONTACT_PATTERN: re.Pattern | None = Field(default=None, exclude=True)

    @field_validator("HOMEPAGE_CONTACT_KEYWORDS")
    @classmethod
    def keywords_not_empty(cls, v: str) -> str:
        keywords = [k.strip() for k in v.split(",") if k.strip()]
        if not keywords:
            raise ValueError(
                "HOMEPAGE_CONTACT_KEYWORDS must contain at least one keyword"
            )
        return v

    @field_validator("HOMEPAGE_CONTACT_PATTERN", mode="before")
    @classmethod
    def build_contact_pattern(cls, v, info) -> re.Pattern:
        raw = info.data.get("HOMEPAGE_CONTACT_KEYWORDS", "")
        keywords = [re.escape(k.strip()) for k in raw.split(",") if k.strip()]
        pattern_str = r"(?i)/(" + "|".join(keywords) + r")[^/]*/?([?#].*)?$"
        return re.compile(pattern_str)

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
