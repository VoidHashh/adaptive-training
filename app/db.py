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

from sqlalchemy import UniqueConstraint, create_engine, event
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


def _indices_por_columna(conn, tabla: str) -> dict[str, set[str]]:
    """columna -> índices que la tocan. SQLite no deja borrar una indexada."""
    fuera: dict[str, set[str]] = {}
    for fila in conn.exec_driver_sql(f"PRAGMA index_list('{tabla}')"):
        indice = fila[1]
        for col in conn.exec_driver_sql(f"PRAGMA index_info('{indice}')"):
            if col[2] is not None:
                fuera.setdefault(col[2], set()).add(indice)
    return fuera


def _uniques_reales(conn, tabla: str) -> set[tuple[str, ...]]:
    """Los UNIQUE que hay en el disco, vengan de un CREATE TABLE o de un índice.

    `PRAGMA index_list` los da todos juntos y su columna `origin` distingue el
    origen -'u' si viene de un UNIQUE de tabla, 'c' si de un CREATE UNIQUE
    INDEX, 'pk' si es la clave primaria-, pero aquí ese detalle no cambia nada:
    los tres rechazan el mismo INSERT con el mismo mensaje, y lo que se compara
    contra el modelo son las COLUMNAS, no de dónde salió la restricción.

    La clave primaria se queda fuera. En estas tablas es siempre un `id`
    autoincremental que el modelo declara aparte, y meterla aquí haría que toda
    tabla pareciera tener un UNIQUE de más el primer día.

    Un índice sobre una EXPRESIÓN tiene columnas a `None` en `index_info`, y de
    ésos se pasa: no hay ninguno, y adivinar a qué columna equivale una
    expresión es justo la clase de conjetura que no cabe en algo que rehace
    tablas.
    """
    fuera: set[tuple[str, ...]] = set()
    for fila in conn.exec_driver_sql(f"PRAGMA index_list('{tabla}')"):
        _, nombre, es_unico, origen, _parcial = (list(fila) + [None] * 5)[:5]
        if not es_unico or origen == "pk":
            continue
        cols = [c[2] for c in conn.exec_driver_sql(f"PRAGMA index_info('{nombre}')")]
        if all(c is not None for c in cols):
            fuera.add(tuple(sorted(cols)))
    return fuera


def _uniques_del_modelo(tabla) -> set[tuple[str, ...]]:
    """Los que el modelo declara, por las dos vías que tiene para declararlos.

    `UniqueConstraint` y `Index(..., unique=True)` producen el mismo efecto en
    SQLite y se escriben en sitios distintos del modelo. Mirar solo una de las
    dos haría que la otra pareciera sobrante y acabara borrada en el primer
    arranque, que es un fallo bastante peor que el que esto viene a arreglar.

    `unique=True` en un `mapped_column` -como `activities.garmin_activity_id`-
    llega aquí como un `UniqueConstraint` de una sola columna, así que ya está
    contado.
    """
    de_restricciones = {
        tuple(sorted(c.name for c in r.columns))
        for r in tabla.constraints
        if isinstance(r, UniqueConstraint)
    }
    de_indices = {
        tuple(sorted(c.name for c in i.columns)) for i in tabla.indexes if i.unique
    }
    de_columnas = {(c.name,) for c in tabla.columns if c.unique}
    return de_restricciones | de_indices | de_columnas


def _rehacer_sin_restricciones(conn, nombre: str, tabla) -> None:
    """La danza de doce pasos de SQLite, que es la única forma de quitar un UNIQUE.

    Un UNIQUE declarado en el CREATE TABLE no es un objeto que se pueda soltar:
    SQLite lo materializa como un `sqlite_autoindex_*` y se niega a borrarlo con
    DROP INDEX. No hay ALTER que lo quite. Rehacer la tabla es el camino que
    documenta el propio SQLite, y se hace aquí en el orden que evita las dos
    trampas conocidas:

      - la tabla NUEVA se crea desde el modelo, o sea SIN la restricción, ANTES
        de copiar nada. Copiando a una tabla que todavía la tuviera, una base
        con dos avisos del mismo día -que es justo la que hay que arreglar-
        reventaría al migrar;

      - se copian las columnas POR NOMBRE y no con un `SELECT *`. El orden de
        las columnas en el disco no tiene por qué coincidir con el del modelo
        después de un ADD COLUMN, y un `INSERT ... SELECT *` desalineado no da
        error: mete la fecha en el campo del texto y sigue.

    Se llama con las columnas ya reconciliadas, así que los dos lados tienen el
    mismo juego de nombres. Aun así se corta la lista contra lo que hay en el
    disco, porque una migración que rehace tablas no es sitio para dar nada por
    hecho.
    """
    reales = _columnas_reales(conn, nombre)
    cols = [c.name for c in tabla.columns if c.name in reales]
    lista = ", ".join(f'"{c}"' for c in cols)

    # Los índices no viajan con los datos: son objetos de `sqlite_master` que
    # apuntan a la tabla vieja y desaparecen con ella. `tabla.create` los vuelve
    # a poner, pero solo si sus nombres están libres, y el DROP de la tabla ya
    # se ha llevado los suyos por delante.
    conn.exec_driver_sql(f'ALTER TABLE "{nombre}" RENAME TO "_vieja_{nombre}"')
    tabla.create(conn)
    conn.exec_driver_sql(
        f'INSERT INTO "{nombre}" ({lista}) SELECT {lista} FROM "_vieja_{nombre}"'
    )
    conn.exec_driver_sql(f'DROP TABLE "_vieja_{nombre}"')


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

    Y LAS QUE SOBRAN, QUE ERA EL AGUJERO
    ------------------------------------
    Esto solo miraba en una dirección: columnas del modelo que faltaban en el
    fichero. Al revés no miraba nadie, y por eso `activities` tenía cinco
    columnas -`start_time_local`, `type_key`, `elevation_loss_m`, `max_hr` y
    `raw_json`- que el modelo había borrado meses antes y seguían ahí. El
    guardián que existía, `columnas_actividad_sin_escribir()`, no podía verlas:
    compara contra el MODELO, así que una columna que el modelo ya no declara
    le resulta invisible por definición. La base de datos y el código llevaban
    medio año discrepando sin que nada lo dijera.

    Una columna de más no es inocua. Es la que aparece en un `SELECT *`, en un
    volcado o en un `PRAGMA table_info` y parece un dato que se está guardando.
    Pasó exactamente eso: `max_hr` figuraba en la lista de columnas muertas a
    revisar cuando en el código no existía desde hacía meses.

    Sobrante y VACÍA se borra, con sus índices si los tiene. Sobrante y CON
    DATOS se para, igual que un NOT NULL sin defecto y por el mismo motivo: lo
    que hay dentro puede ser la única copia. Borrarlo sola sería la clase de
    fallo silencioso que este sistema no se puede permitir, porque un
    `ALTER TABLE DROP COLUMN` no se deshace.

    Y LAS RESTRICCIONES, QUE ERA EL AGUJERO DE DEBAJO DEL AGUJERO
    ------------------------------------------------------------
    Todo lo anterior compara COLUMNAS. Una restricción de tabla no es una
    columna, así que no la veía nada de esto, y costó un día de sistema.

    El 18 de septiembre de 2026 el check-in se guardó y la decisión reventó con
    `UNIQUE constraint failed: notifications.date, notifications.kind`. Esa
    restricción llevaba borrada del modelo desde f5e9758, con un docstring en
    `Notification` que empieza literalmente por "POR QUÉ NO HAY UNIQUE SOBRE
    (date, kind)" y que describe este fallo exacto: el mensaje de Telegram ya se
    ha enviado cuando salta, así que la excepción no impide el segundo aviso
    -sólo destruye el registro de lo que sí pasó- y encima hace `rollback` de la
    decisión entera. El usuario se queda con un plan en el móvil que no existe
    en la base de datos.

    El modelo estaba arreglado, el docstring lo explicaba, la suite pasaba, y la
    base desplegada seguía con la restricción puesta, porque `create_all` no
    toca lo que ya existe y esto sólo sabía de columnas. Es el peor sabor de
    desfase: el que tiene el arreglo escrito, probado y documentado, y no ha
    llegado al único sitio donde importa.

    Un UNIQUE de tabla no se puede soltar con un ALTER -SQLite lo materializa
    como `sqlite_autoindex_*` y rechaza el DROP INDEX-, así que la tabla se
    rehace. Eso es mover el histórico de sitio, que es más peligroso que el
    desfase que arregla, y por eso va en `_rehacer_sin_restricciones` con el
    orden escrito y con tests que cuentan las filas después.

    En la dirección contraria NO se hace nada: un UNIQUE que el modelo declara y
    el disco no tiene se queda como está. Ponerlo significaría fallar sobre las
    filas que ya lo violan, y esas filas son historial: la respuesta correcta
    ahí es mirarlas una por una, no que un arranque decida solo.
    """
    eng = eng or engine
    if eng.dialect.name != "sqlite":  # pragma: no cover
        return []

    cambios: list[str] = []
    bloqueos: list[str] = []
    anadir: list[tuple[str, str, str]] = []
    quitar: list[tuple[str, str, tuple[str, ...]]] = []
    rehacer: list[str] = []
    destrabar: list[tuple[str, list[tuple[str, ...]]]] = []

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

            if nombre in rehacer:
                continue  # se va entera, no hay nada que podar
            sobrantes = reales - {c.name for c in tabla.columns}
            indices = _indices_por_columna(conn, nombre) if sobrantes else {}
            for col_name in sorted(sobrantes):
                # `count(col)` cuenta los NO nulos: una columna entera a NULL da
                # 0 y una con un solo valor da 1. La distinción es la que decide
                # entre borrar y parar, así que se pregunta por el contenido y
                # no por si la tabla tiene filas.
                con_dato = conn.exec_driver_sql(
                    f'SELECT count("{col_name}") FROM "{nombre}"'
                ).scalar()
                if con_dato:
                    bloqueos.append(
                        f"{nombre}.{col_name} sobra -el modelo ya no la "
                        f"declara- pero tiene {con_dato} valor(es) no nulos. No "
                        f"se borra sola: eso puede ser la única copia"
                    )
                    continue
                quitar.append((nombre, col_name, tuple(sorted(indices.get(col_name, ())))))

            # Las restricciones se miran DESPUÉS de las columnas y con la
            # decisión tomada, pero se aplican al final de todo: rehacer la
            # tabla aquí dejaría la copia hecha desde un esquema que todavía no
            # tiene la columna que se está a punto de añadir.
            sobran_uq = sorted(
                _uniques_reales(conn, nombre) - _uniques_del_modelo(tabla)
            )
            if sobran_uq:
                destrabar.append((nombre, sobran_uq))

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
        for nombre, columna, indices in quitar:
            # El índice primero: SQLite rechaza el DROP COLUMN de una columna
            # indexada, y el índice de una columna que ya no existe tampoco
            # tendría a quién servir.
            for indice in indices:
                conn.exec_driver_sql(f'DROP INDEX IF EXISTS "{indice}"')
            conn.exec_driver_sql(f'ALTER TABLE "{nombre}" DROP COLUMN "{columna}"')
            cambios.append(f"{nombre}.{columna} (BORRADA, estaba vacía y sobraba)")
        for nombre in rehacer:
            conn.exec_driver_sql(f'DROP TABLE "{nombre}"')
            Base.metadata.tables[nombre].create(conn)
            cambios.append(f"{nombre} (rehecha, estaba vacía)")
        for nombre, sobran_uq in destrabar:
            if nombre in rehacer:
                continue  # acaba de nacer del modelo: ya viene sin ellas
            _rehacer_sin_restricciones(conn, nombre, Base.metadata.tables[nombre])
            cuales = ", ".join("+".join(u) for u in sobran_uq)
            cambios.append(f"{nombre} (rehecha para soltar el UNIQUE de {cuales})")

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
