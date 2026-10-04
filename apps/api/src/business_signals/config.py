from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ai_provider: Literal["openai", "anthropic", "openai_compatible"] = "openai"
    ai_api_key: str | None = None
    ai_model: str = "gpt-5-mini"
    ai_base_url: str = "https://api.openai.com/v1"
    query_timeout_seconds: int = 15
    max_query_rows: int = 500
    negligiblity_threshold_percent: float = 1.0
    # Below this confidence, a proposed business measure is considered
    # unresolved and must not be replaced by catalog vocabulary inference.
    metric_definition_confidence: float = Field(default=0.75, ge=0, le=1)
    investigation_max_iterations: int = Field(default=8, ge=1)
    investigation_max_sql_queries: int = Field(default=12, ge=1)
    investigation_max_external_calls: int = Field(default=2, ge=0)
    investigation_max_duration_seconds: int = Field(default=180, ge=10)
    ignore_investigation_limits: bool = False
    # Geographic weather summaries use one representative point per this many
    # square kilometres, unless the scope is a single known location.
    target_cell_area_km2: float = Field(default=2500.0, gt=0)
    # A provider request is capped independently of the geographic sampling
    # policy; callers can batch a larger set of points when supported.
    max_points_weather_api: int = Field(default=150, ge=1)
    web_origin: str = "http://localhost:3000"
    checkpoint_database_url: str = "postgresql://investigator:investigator@localhost:5432/catalog"
    checkpoint_schema: str = "business_signals"
    datasource_store_path: Path = Path(".business-signals/datasources.json")
    investigation_store_dir: Path = Path(".business-signals/investigations")
    research_agent_catalog_path: Path = Path("config/research-agents.json")

settings = Settings()
