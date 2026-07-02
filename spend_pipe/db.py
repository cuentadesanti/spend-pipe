"""Engine y sesión de SQLAlchemy sobre SQLite."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import Base

BASE_DIR = Path(__file__).resolve().parents[1]

engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False, future=True)


def init_db() -> None:
    """create_all directo. Solo para tests/bootstrap rápido; el schema real lo maneja Alembic."""
    Base.metadata.create_all(engine)


def upgrade_to_head() -> None:
    """Aplica las migraciones de Alembic hasta head. Es lo que corre la app al arrancar,
    así Alembic es la única fuente de verdad del schema (robusto ante cambios de MVP2+)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "alembic"))
    command.upgrade(cfg, "head")
