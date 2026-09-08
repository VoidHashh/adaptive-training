"""Motor de base de datos y sesiones.

SQLite con un único escritor. APScheduler va con `max_instances=1` para que
dos jobs no escriban a la vez, y aun así activamos WAL y un busy_timeout
generoso: el check-in desde la PWA puede coincidir con el job de la mañana.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.settings import settings


def _make_engine(url: str) -> Engine:
    is_sqlite = url.startswith("sqlite")
    if is_sqlite:
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        url,
        future=True,
        # check_same_thread=False porque APScheduler trabaja en otro hilo
        # que el de FastAPI.
        connect_args={"check_same_thread": False, "timeout": 30} if is_sqlite else {},
    )

    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _set_pragmas(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


engine = _make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """Crea las tablas si no existen."""
    Base.metadata.create_all(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Sesión transaccional: commit al salir bien, rollback si algo peta."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """Dependencia para FastAPI."""
    with session_scope() as session:
        yield session
