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
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and "+psycopg" not in url:
        url = "postgresql+psycopg://" + url[len("postgresql://"):]

    if url.startswith("postgresql"):
        import urllib.parse
        try:
            parsed = urllib.parse.urlsplit(url)
            username = parsed.username
            password = parsed.password
            hostname = parsed.hostname
            port = parsed.port

            netloc = ""
            if username is not None:
                netloc += urllib.parse.quote(username, safe="")
                if password is not None:
                    netloc += ":" + urllib.parse.quote(password, safe="")
                netloc += "@"

            if hostname is not None:
                netloc += hostname
                if port is not None:
                    netloc += f":{port}"

            parsed = parsed._replace(netloc=netloc)
            return urllib.parse.urlunsplit(parsed)
        except Exception:
            return url
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

    # IA (opcional): sin key, los fallbacks de IA se desactivan y el pipeline
    # sigue 100% funcional. Modelo elegido por costo (diseño ai-assist-design.md);
    # subible a claude-opus-4-8 vía env si se quiere más calidad.
    anthropic_key: str = ""          # SPENDPIPE_ANTHROPIC_KEY
    ai_model: str = "claude-haiku-4-5"

    # MCP (opcional): expone el pipeline como tools para claude.ai / ChatGPT.
    # El servidor se monta bajo /mcp-<secret> (el path ES la autenticación, patrón
    # Zapier-MCP: URL secreta sobre HTTPS). Sin secret, el MCP queda apagado.
    mcp_secret: str = ""             # SPENDPIPE_MCP_SECRET

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
