from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-5-mini"
    query_timeout_seconds: int = 15
    max_query_rows: int = 500
    web_origin: str = "http://localhost:3000"


settings = Settings()
