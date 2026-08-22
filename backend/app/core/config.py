from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = Field(default="development", alias="ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    database_url: str = Field(alias="DATABASE_URL")
    direct_database_url: str = Field(alias="DIRECT_DATABASE_URL")
    redis_url: str = Field(alias="REDIS_URL")

    n8n_api_key: str = Field(default="", alias="N8N_API_KEY")
    jwt_secret: str = Field(default="dev-secret-change-me", alias="JWT_SECRET")
    access_token_expires_minutes: int = Field(default=15, alias="ACCESS_TOKEN_EXPIRES_MINUTES")
    refresh_token_expires_days: int = Field(default=7, alias="REFRESH_TOKEN_EXPIRES_DAYS")
    n8n_org_id: str = Field(default="", alias="N8N_ORG_ID")
    rate_limit_login_per_minute: int = Field(default=10, alias="RATE_LIMIT_LOGIN_PER_MINUTE")
    cors_origins: list[str] = Field(default=["http://localhost:3000"], alias="CORS_ORIGINS")


@lru_cache
def get_settings() -> Settings:
    return Settings()
