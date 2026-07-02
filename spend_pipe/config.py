"""Settings por variables de entorno. NADA de secretos en git (lección de finanzas-ai/config.js)."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SPENDPIPE_", extra="ignore")

    # Staging
    database_url: str = "sqlite:///./spend_pipe.db"
    inbox_dir: str = "./inbox"
    artifacts_dir: str = "./artifacts"   # donde se escriben los batch-<id>.json para el worker Node

    # Actual @ PikaPods — los consume el worker Node vía este mismo entorno.
    actual_server_url: str = ""
    actual_password: str = ""
    actual_sync_id: str = ""


settings = Settings()
