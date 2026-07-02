"""Settings por variables de entorno. NADA de secretos en git (lección de finanzas-ai/config.js)."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[1]
_LEGACY_INBOX_DIRS = {None, "", "./inbox", "inbox"}
_LEGACY_ARTIFACT_DIRS = {None, "", "./artifacts", "artifacts"}


def _resolve_path(raw: str) -> Path:
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p
    return (BASE_DIR / p).resolve()


def _normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://") and "+psycopg" not in url:
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SPENDPIPE_", extra="ignore")

    # Staging
    database_url: str = "sqlite:///./spend_pipe.db"
    storage_path: str | None = None
    inbox_dir: str | None = None
    artifacts_dir: str | None = None   # donde se escriben los batch-<id>.json para el worker Node
    rules_file: str = "./rules/categorize.yaml"   # reglas de categorización (YAML en git)

    # Actual @ PikaPods — los consume el worker Node vía este mismo entorno.
    actual_server_url: str = ""
    actual_password: str = ""
    actual_sync_id: str = ""

    @property
    def sqlalchemy_database_url(self) -> str:
        return _normalize_database_url(self.database_url)

    @property
    def is_sqlite(self) -> bool:
        return self.sqlalchemy_database_url.startswith("sqlite")

    @property
    def storage_root(self) -> Path:
        if self.storage_path:
            return _resolve_path(self.storage_path)
        return BASE_DIR

    @property
    def resolved_inbox_dir(self) -> Path:
        if self.storage_path and self.inbox_dir in _LEGACY_INBOX_DIRS:
            return self.storage_root / "inbox"
        if self.inbox_dir:
            return _resolve_path(self.inbox_dir)
        return self.storage_root / "inbox"

    @property
    def resolved_artifacts_dir(self) -> Path:
        if self.storage_path and self.artifacts_dir in _LEGACY_ARTIFACT_DIRS:
            return self.storage_root / "artifacts"
        if self.artifacts_dir:
            return _resolve_path(self.artifacts_dir)
        return self.storage_root / "artifacts"

    @property
    def resolved_rules_file(self) -> Path:
        return _resolve_path(self.rules_file)


settings = Settings()
