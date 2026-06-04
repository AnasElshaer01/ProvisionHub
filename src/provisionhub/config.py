"""Runtime settings, loaded from env vars.

DATABASE_URL is the one knob the README promises: change it from SQLite to
Postgres and nothing else needs to move.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./provisionhub.db"

    # Bearer token expected on inbound SCIM requests.
    scim_bearer_token: str = "dev-token"

    # Connector credentials (used later by main.py when wiring the registry).
    slack_token: str = ""
    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: str = ""
    zendesk_subdomain: str = ""
    zendesk_email: str = ""
    zendesk_api_token: str = ""


settings = Settings()
