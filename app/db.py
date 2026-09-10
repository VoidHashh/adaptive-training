"""Motor de base de datos y sesiones.

SQLite con un único escritor. APScheduler va con `max_instances=1` para que
dos jobs no escriban a la vez, y aun así activamos WAL y un busy_timeout
generoso: el check-in desde la PWA puede coincidir con el job de la mañana.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import TextClause

from app.models import Base
from app.settings import settings

log = logging.getLogger(__name__)


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


class SchemaDesfasado(RuntimeError):
    """La base de datos de disco no tiene lo que el código espera leer."""


def _columnas_reales(conn, tabla: str) -> set[str]:
    return {
        fila[1] for fila in conn.exec_driver_sql(f"PRAGMA table_info('{tabla}')")
    }


def _sufijo(col) -> str:
    """`NOT NULL DEFAULT x` para el ADD COLUMN, cuando se pueda ponerlo.

    Sin esto, `ALTER TABLE ADD COLUMN` compilaba solo el TIPO y tiraba a la
    basura el NOT NULL y el defecto que declara el modelo. El resultado es que
    una instalación nueva y una actualizada acaban con esquemas DISTINTOS para
    el mismo código: en la nueva, `create_all` pone `INTEGER NOT NULL DEFAULT 0`;
    en la actualizada, la columna queda nullable y las filas viejas a NULL. Los
    dos arrancan, y la diferencia solo se nota el día que algo asume que el dato
    está y en una de las dos no está.

    Solo se puede inlinear un defecto CONSTANTE: SQLite rechaza los que no lo
    son -`DEFAULT CURRENT_TIMESTAMP` entre ellos- en un ADD COLUMN. Cuando el
    defecto no es constante se devuelve "" y la columna se añade como hasta
    ahora, que sigue siendo seguro porque en ese caso admite nulos.
    """
    sd = col.server_default
    if sd is None:
        return ""
    arg = getattr(sd, "arg", None)
    if isinstance(arg, str):
        literal = arg
    elif isinstance(arg, TextClause):
        literal = arg.text
    else:
        return ""  # no constante: func.now() y compañía
    literal = literal.strip()
    if not literal:
        return ""
    return f"{'' if col.nullable else ' NOT NULL'} DEFAULT {literal}"


def ensure_schema(eng: Engine | None = None) -> list[str]:
    """Pone al día las tablas que ya existen. Devuelve lo que ha cambiado.

    POR QUÉ HACE FALTA
    ------------------
    `create_all` crea las tablas que faltan y NUNCA toca las que ya están. Una
    columna nueva en `models.py` no llega a una base de datos que ya existía, y
    eso no da error al arrancar: da error meses después, la primera vez que
    alguien lee esa columna.

    No es hipotético. En esta misma base había tres tablas desfasadas y una de
    las columnas que faltaban era `decisions.progression_json`, que es de donde
    la reconciliación nocturna saca qué ejercicios subieron por la mañana. Sin
    ella, `run_reconcile` habría reventado cada noche a las 22:30, en un hilo de
    APScheduler y sin nadie delante.

    En Umbrel el orden del desastre es este: se actualiza el contenedor, arranca
    sin quejarse, manda su mensaje de las nueve y revienta por la noche.

    LO QUE SE ARREGLA SOLO Y LO QUE NO
    ----------------------------------
    Añadir una columna que admite nulos es seguro y no destruye nada: las filas
    viejas la tienen a NULL, que es exactamente lo que significa "esto es
    anterior a que existiera este dato". Eso se hace y se avisa por WARNING.

    Una columna NOT NULL sin defecto no se puede añadir a una tabla CON FILAS
    sin inventarse su valor, así que ahí se para. Inventarlo sería meter datos
    falsos en el histórico, que es la única cosa peor que no arrancar.

    Si la tabla está VACÍA, en cambio, se rehace entera. Y no es una excepción
    cómoda: el motivo del bloqueo es que no hay con qué rellenar las filas
    existentes, y sin filas no hay nada que rellenar. Este es además el caso
    normal en una instalación que todavía no ha entrenado ningún día.
    """
    eng = eng or engine
    if eng.dialect.name != "sqlite":  # pragma: no cover
        return []

    cambios: list[str] = []
    bloqueos: list[str] = []
    anadir: list[tuple[str, str, str]] = []
    rehacer: list[str] = []

    # Se mira TODO antes de tocar NADA. Son dos pasadas a propósito: mezclarlas
    # significa que la primera tabla ya está migrada cuando la tercera resulta
    # ser un bloqueo, y entonces se para a medio camino. Reintentar después de
    # migrar a mano tiene que partir siempre del mismo sitio.
    with eng.begin() as conn:
        existentes = {
            fila[0] for fila in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for nombre, tabla in Base.metadata.tables.items():
            if nombre not in existentes:
                continue  # de esta ya se encarga create_all
            reales = _columnas_reales(conn, nombre)
            for col in tabla.columns:
                if col.name in reales:
                    continue
                if not col.nullable and col.server_default is None:
                    filas = conn.exec_driver_sql(
                        f"SELECT count(*) FROM '{nombre}'"
                    ).scalar()
                    if filas:
                        bloqueos.append(
                            f"{nombre}.{col.name} es NOT NULL y no tiene "
                            f"defecto, y la tabla tiene {filas} fila(s): no se "
                            f"puede añadir sin inventar su valor"
                        )
                    elif nombre not in rehacer:
                        rehacer.append(nombre)
                    continue
                anadir.append(
                    (nombre, col.name, col.type.compile(eng.dialect) + _sufijo(col))
                )

    if bloqueos:
        raise SchemaDesfasado(
            "la base de datos no se puede poner al día sola:\n"
            + "\n".join(f"  - {b}" for b in bloqueos)
            + "\n\nHay que migrarla a mano. Se para aquí a propósito: seguir "
            "significaría reventar más tarde, de noche y sin nadie delante."
        )

    with eng.begin() as conn:
        for nombre, columna, tipo in anadir:
            conn.exec_driver_sql(
                f'ALTER TABLE "{nombre}" ADD COLUMN "{columna}" {tipo}'
            )
            cambios.append(f"{nombre}.{columna} ({tipo})")
        for nombre in rehacer:
            conn.exec_driver_sql(f'DROP TABLE "{nombre}"')
            Base.metadata.tables[nombre].create(conn)
            cambios.append(f"{nombre} (rehecha, estaba vacía)")

    if cambios:
        log.warning(
            "base de datos puesta al día, %d cambio(s): %s",
            len(cambios), ", ".join(cambios),
        )
    return cambios


def init_db() -> None:
    """Crea las tablas que falten y pone al día las que ya estaban."""
    Base.metadata.create_all(engine)
    ensure_schema(engine)


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
