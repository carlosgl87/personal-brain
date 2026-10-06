from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8",
        extra="ignore", env_ignore_empty=True, hide_input_in_errors=True,
    )
    database_internal_url: SecretStr | None = Field(default=None, validation_alias="DATABASE_URL")
    database_public_url: SecretStr | None = Field(default=None, validation_alias="DATABASE_PUBLIC_URL")
    telegram_bot_token: SecretStr | None = None
    telegram_user_id: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None
    openrouter_stt_model: str = Field(default="openai/whisper-large-v3-turbo", min_length=1, max_length=200)
    openrouter_embedding_model: str | None = Field(default=None, max_length=200)
    openrouter_embedding_dimensions: int = Field(default=2560, ge=1, le=16000)
    memory_chunk_version: str = Field(default="paragraph-v1", min_length=1, max_length=80)
    memory_chunk_size: int = Field(default=6000, ge=4000, le=7000)
    memory_chunk_overlap: int = Field(default=350, ge=0, le=1000)
    telegram_document_max_bytes: int = Field(default=20971520, ge=1024, le=20971520)
    source_processing_worker_enabled: bool = True
    memory_auto_index: bool = False
    extraction_max_chars: int = Field(default=30000, ge=1000, le=100000)
    reasoning_context_max_chars: int = Field(default=100000, ge=5000, le=180000)
    hierarchical_extraction_enabled: bool = True
    hierarchical_max_chunks: int = Field(default=40, ge=1, le=100)
    hierarchical_part_max_tokens: int = Field(default=4096, ge=1024, le=8192)
    hierarchical_consolidation_max_items: int = Field(default=300, ge=10, le=1000)
    hierarchical_consolidation_max_chars: int = Field(default=160000, ge=10000, le=300000)
    hierarchical_consolidation_max_tokens: int = Field(default=12000, ge=4096, le=24000)
    project_memory_enabled: bool = True
    project_memory_debounce_seconds: int = Field(default=300, ge=0, le=3600)
    project_memory_max_chars: int = Field(default=12000, ge=1000, le=24000)
    project_memory_incremental_max_sources: int = Field(default=20, ge=1, le=100)
    project_memory_reconciliation_hour: int = Field(default=3, ge=0, le=23)
    project_memory_timezone: str = "America/Lima"
    stt_language: str = "es"
    audio_max_bytes: int = Field(default=20 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    audio_max_seconds: int = Field(default=600, ge=1, le=600)
    llm_api_key: SecretStr | None = None
    llm_provider: str = "anthropic"
    llm_model: str | None = None

    @field_validator("project_memory_timezone")
    @classmethod
    def valid_memory_timezone(cls, value):
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(value)
        except Exception:
            raise ValueError("PROJECT_MEMORY_TIMEZONE debe ser una zona IANA valida.") from None
        return value

    def telegram_credentials(self) -> tuple[str, int]:
        try:
            if self.telegram_bot_token is None or self.telegram_user_id is None:
                raise ValueError
            token = self.telegram_bot_token.get_secret_value()
            user_id = int(self.telegram_user_id.get_secret_value())
            if not token or user_id <= 0 or any(char.isspace() for char in token):
                raise ValueError
            return token, user_id
        except Exception:
            raise ValueError("Configura TELEGRAM_BOT_TOKEN y TELEGRAM_USER_ID validos en .env.") from None

    def llm_credentials(self) -> tuple[str, str]:
        if (self.llm_provider.casefold() != "anthropic" or self.llm_api_key is None
                or not self.llm_api_key.get_secret_value().strip()
                or not self.llm_model or not self.llm_model.strip() or len(self.llm_model.strip()) > 200):
            raise ValueError("Configura LLM_PROVIDER=anthropic, LLM_MODEL y LLM_API_KEY.")
        return self.llm_api_key.get_secret_value(), self.llm_model.strip()

    @property
    def database_url(self) -> URL:
        secret = self.database_internal_url or self.database_public_url
        if secret is None:
            raise ValueError("Configura DATABASE_URL o DATABASE_PUBLIC_URL.")
        try:
            url = make_url(secret.get_secret_value())
            if url.drivername not in {"postgres", "postgresql", "postgresql+psycopg"}:
                raise ValueError
            return url.set(drivername="postgresql+psycopg")
        except Exception:
            raise ValueError("La configuraciÃ³n de PostgreSQL no es vÃ¡lida.") from None


@lru_cache
def get_settings() -> Settings:
    return Settings()
