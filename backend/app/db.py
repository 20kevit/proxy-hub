"""SQLite engine/session. Control-plane only; proxy traffic is handled by Xray/sing-box."""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import get_settings

Base = declarative_base()
_engine = None
_SessionFactory = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{settings.db_path}",
            connect_args={"check_same_thread": False},
            future=True,
        )
    return _engine


def get_session_factory():
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)
    return _SessionFactory


def init_db() -> None:
    from . import models  # noqa: F401  (register tables)

    Base.metadata.create_all(get_engine())
    # Lightweight additive migration for existing DBs (no alembic yet).
    import sqlalchemy as _sa

    with get_engine().begin() as conn:
        cols = {c["name"] for c in _sa.inspect(conn).get_columns("proxy_nodes")}
        if "egress_ok" not in cols:
            conn.execute(_sa.text("ALTER TABLE proxy_nodes ADD COLUMN egress_ok BOOLEAN DEFAULT 0"))
        if "last_success" not in cols:
            conn.execute(_sa.text("ALTER TABLE proxy_nodes ADD COLUMN last_success DATETIME"))
        if "success_count" not in cols:
            conn.execute(_sa.text("ALTER TABLE proxy_nodes ADD COLUMN success_count INTEGER DEFAULT 0"))


def reset_engine() -> None:
    """Test helper: drop cached engine so a new DB path can be used."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None
