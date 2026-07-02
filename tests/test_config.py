from pathlib import Path

from spend_pipe.config import BASE_DIR, Settings


def test_postgres_url_is_normalized_for_sqlalchemy():
    settings = Settings(
        database_url="postgresql://postgres:secret@example.com:5432/postgres",
        actual_server_url="",
        actual_password="",
        actual_sync_id="",
    )
    assert settings.sqlalchemy_database_url == "postgresql+psycopg://postgres:secret@example.com:5432/postgres"


def test_storage_path_drives_default_dirs():
    settings = Settings(
        storage_path="/tmp/spend-pipe-data",
        actual_server_url="",
        actual_password="",
        actual_sync_id="",
    )
    assert settings.resolved_inbox_dir == Path("/tmp/spend-pipe-data/inbox")
    assert settings.resolved_artifacts_dir == Path("/tmp/spend-pipe-data/artifacts")


def test_relative_storage_overrides_resolve_from_repo():
    settings = Settings(
        storage_path="deploy-data",
        actual_server_url="",
        actual_password="",
        actual_sync_id="",
    )
    assert settings.storage_root == (BASE_DIR / "deploy-data").resolve()
