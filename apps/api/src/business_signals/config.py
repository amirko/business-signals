from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Research-agent configurations refer to credential names rather than secret
# values. Their generic executor reads those names from the process
# environment, so load the same local .env file used by Settings without
# overwriting explicitly exported environment variables.
load_dotenv(".env", override=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ai_provider: Literal["openai", "anthropic", "openai_compatible"] = "openai"
    ai_api_key: str | None = None
    ai_model: str = "gpt-5-mini"
    ai_base_url: str = "https://api.openai.com/v1"
    # LangSmith is strictly optional observability. It must never prevent an
    # investigation from running when unavailable or not configured.
    langsmith_tracing: bool = False
    langsmith_api_key: str | None = None
    langsmith_project: str = "business-signals"
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    langsmith_capture_content: bool = False
    query_timeout_seconds: int = 15
    max_query_rows: int = 500
    # The maximum number of distinct values a text field may have before it is
    # treated as free text rather than a controlled business vocabulary.
    schema_categorical_value_limit: int = Field(default=50, ge=1)
    negligiblity_threshold_percent: float = 1.0
    report_percent_decimal_places: int = Field(default=2, ge=0, le=8)
    source_reliability_default: float = Field(default=0.65, ge=0, le=1)
    # Below this confidence, a proposed business measure is considered
    # unresolved and must not be replaced by catalog vocabulary inference.
    metric_definition_confidence: float = Field(default=0.75, ge=0, le=1)
    # Limits for provider calls and bounded query repair. Keeping these here
    # makes performance and cost trade-offs deploy-time decisions.
    external_http_timeout_seconds: float = Field(default=12.0, gt=0)
    external_research_max_period_days: int = Field(default=3660, ge=1)
    anthropic_http_timeout_seconds: float = Field(default=60.0, gt=0)
    anthropic_max_tokens: int = Field(default=4096, ge=1)
    query_repair_max_attempts: int = Field(default=2, ge=1)
    entity_lookup_max_rows: int = Field(default=100, ge=1)
    cross_datasource_lookup_max_values: int = Field(default=500, ge=1)
    direct_answer_display_max_rows: int = Field(default=500, ge=1)
    retained_entity_reference_max_count: int = Field(default=100, ge=1)
    cross_datasource_relation_sample_size: int = Field(default=100, ge=1)
    cross_datasource_relation_min_sample_values: int = Field(default=5, ge=1)
    cross_datasource_relation_min_coverage: float = Field(default=0.95, ge=0, le=1)
    # Confidence cutoffs that govern when the workflow can finish with a
    # causal conclusion. They are policy choices, not model constants.
    supported_hypothesis_confidence_threshold: float = Field(default=0.78, ge=0, le=1)
    insufficient_evidence_confidence_threshold: float = Field(default=0.55, ge=0, le=1)
    direct_evidence_conclusion_confidence_threshold: float = Field(default=0.75, ge=0, le=1)
    indirect_evidence_confidence_ceiling: float = Field(default=0.74, ge=0, le=1)
    unavailable_data_confidence: float = Field(default=0.95, ge=0, le=1)
    investigation_max_iterations: int = Field(default=8, ge=1)
    investigation_max_sql_queries: int = Field(default=12, ge=1)
    investigation_max_external_calls: int = Field(default=2, ge=0)
    # Execute external checks in small batches so every finding can be
    # assessed and attached to its claim before more are planned.
    external_research_checks_per_batch: int = Field(default=1, ge=1)
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
    checkpoint_pool_min_size: int = Field(default=1, ge=1)
    checkpoint_pool_max_size: int = Field(default=10, ge=1)
    datasource_store_path: Path = Path(".business-signals/datasources.json")
    investigation_store_dir: Path = Path(".business-signals/investigations")
    research_agent_catalog_path: Path = Path("config/research-agents.json")


settings = Settings()
