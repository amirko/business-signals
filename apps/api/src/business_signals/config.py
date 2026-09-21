from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-5-mini"
    query_timeout_seconds: int = 15
    max_query_rows: int = 500
    negligiblity_threshold_percent: float = 1.0
    web_origin: str = "http://localhost:3000"
    checkpoint_database_url: str = "postgresql://investigator:investigator@localhost:5432/catalog"
    checkpoint_schema: str = "business_signals"
    datasource_store_path: Path = Path(".business-signals/datasources.json")
    investigation_store_dir: Path = Path(".business-signals/investigations")


settings = Settings()
