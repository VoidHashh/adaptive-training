"""Que una base de datos vieja no arranque fingiendo estar al día.

`create_all` crea las tablas que faltan y NUNCA toca las que ya existen. Una
columna nueva en `models.py` no llega a una base de datos anterior, y eso no da
error al arrancar: lo da meses después, la primera vez que alguien la lee.

No es hipotético. La base de este repositorio tenía tres tablas desfasadas y una
de las columnas ausentes era `decisions.progression_json`, que es de donde la
reconciliación nocturna saca qué ejercicios subieron por la mañana. Sin ella,
`run_reconcile` habría reventado cada noche a las 22:30, en un hilo de
APScheduler y sin nadie delante.

En Umbrel el orden del desastre es: se actualiza el contenedor, arranca sin
quejarse, manda su mensaje de las nueve y revienta por la noche.

Los tests de aquí construyen bases de datos DESFASADAS a propósito -creando las
tablas y quitando columnas después- porque una base recién creada por
`create_all` no demuestra nada: siempre está al día.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.schema import CreateColumn

from app.db import SchemaDesfasado, ensure_schema
from app.models import Base


def _columnas(eng, tabla: str) -> set[str]:
    with eng.begin() as c:
        return {f[1] for f in c.exec_driver_sql(f"PRAGMA table_info('{tabla}')")}


@pytest.fixture
def vieja(tmp_path):
    """Una base como la que dejaría una versión anterior del código.

    Se crea entera y luego se le quitan columnas rehaciendo la tabla, que es la
    única forma de simular de verdad "esta base es de antes": SQLite no sabe
    borrar una columna en las versiones que nos importan.
    """
    ruta = tmp_path / "vieja.db"
    eng = create_engine(f"sqlite:///{ruta}", future=True)
    Base.metadata.create_all(eng)

    def envejecer(tabla: str, quitar: set[str]) -> None:
        cols = [c for c in Base.metadata.tables[tabla].columns if c.name not in quitar]
        nombres = ", ".join(f'"{c.name}"' for c in cols)

        # El DDL de cada columna lo escribe SQLAlchemy, no este test. Copiarlo a
        # mano se dejaba por el camino los `server_default` (`computed_at` es
        # `func.now()`, no un literal), y una tabla sin ellos no se parece a
        # ninguna real: los INSERT empezaban a fallar por un motivo que no es el
        # que se está probando.
        defs = ", ".join(
            str(CreateColumn(c).compile(dialect=eng.dialect)) for c in cols
        )
        # La PRIMARY KEY es una restricción de tabla, no parte de la columna, y
        # sin ella el `id` INTEGER deja de autoincrementarse en SQLite.
        pks = [c.name for c in cols if c.primary_key]
        if pks:
            defs += ", PRIMARY KEY (" + ", ".join(f'"{n}"' for n in pks) + ")"
        with eng.begin() as c:
            c.exec_driver_sql(f'CREATE TABLE "_tmp" ({defs})')
            c.exec_driver_sql(f'INSERT INTO "_tmp" SELECT {nombres} FROM "{tabla}"')
            c.exec_driver_sql(f'DROP TABLE "{tabla}"')
            c.exec_driver_sql(f'ALTER TABLE "_tmp" RENAME TO "{tabla}"')

    eng.envejecer = envejecer  # type: ignore[attr-defined]
    return eng


# ---------------------------------------------------------------------------
# Lo que se arregla solo
# ---------------------------------------------------------------------------


def test_una_columna_que_admite_nulos_se_anade_sola(vieja):
    """El caso real: `decisions.progression_json` faltaba en la base del repo."""
    vieja.envejecer("decisions", {"progression_json"})
    assert "progression_json" not in _columnas(vieja, "decisions")

    cambios = ensure_schema(vieja)

    assert "progression_json" in _columnas(vieja, "decisions")
    assert any("progression_json" in c for c in cambios), (
        "una migración que no dice lo que ha hecho es indistinguible de ninguna"
    )


def test_las_filas_viejas_sobreviven_a_la_migracion(vieja):
    """Añadir la columna no puede costar el histórico.

    Las filas anteriores se quedan a NULL, que es justo lo que significa: son de
    antes de que ese dato existiera.
    """
    vieja.envejecer("decisions", {"progression_json"})
    with vieja.begin() as c:
        c.execute(text(
            "INSERT INTO decisions (date, light, source, is_current) "
            "VALUES ('2026-09-07', 'green', 'checkin', 1)"
        ))

    ensure_schema(vieja)

    with vieja.begin() as c:
        filas = list(c.execute(text(
            "SELECT date, light, progression_json FROM decisions"
        )))
    assert len(filas) == 1
    assert filas[0][1] == "green", "la decisión guardada sigue siendo la misma"
    assert filas[0][2] is None


def test_una_base_ya_al_dia_no_se_toca(vieja):
    """Si migrar no fuera idempotente, cada arranque reharía tablas."""
    assert ensure_schema(vieja) == []
    assert ensure_schema(vieja) == []


def test_una_base_vacia_no_es_asunto_suyo(tmp_path):
    """De las tablas que NO existen se encarga `create_all`, y por eso esto no
    puede tocarlas: intentar poner al día una tabla ausente acabaría en un
    `ALTER TABLE` contra la nada. Es el primer arranque de una instalación
    nueva, así que tiene que salir bien sin haber creado nada todavía."""
    eng = create_engine(f"sqlite:///{tmp_path / 'nueva.db'}", future=True)

    assert ensure_schema(eng) == []

    with eng.begin() as c:
        tablas = list(c.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ))
    assert tablas == [], "ha creado tablas por su cuenta"


def test_migrar_dos_veces_seguidas_no_cambia_nada(vieja):
    vieja.envejecer("decisions", {"progression_json"})
    primera = ensure_schema(vieja)
    assert primera
    assert ensure_schema(vieja) == [], "la segunda pasada no tiene nada que hacer"


# ---------------------------------------------------------------------------
# Lo que NO se arregla solo
# ---------------------------------------------------------------------------


def test_una_columna_obligatoria_con_filas_detiene_el_arranque(vieja):
    """Rellenarla exigiría inventarse el valor de las filas que ya están.

    Meter datos falsos en el histórico es la única cosa peor que no arrancar:
    el histórico es lo que se mira para entender por qué el sistema decidió algo,
    y un valor inventado ahí no se distingue de uno real.
    """
    vieja.envejecer("rule_states", {"notify"})
    with vieja.begin() as c:
        c.execute(text(
            "INSERT INTO rule_states (rule_name, entity, active_from) "
            "VALUES ('retirada_peso_muerto', 'peso_muerto_smith', '2026-09-07')"
        ))

    with pytest.raises(SchemaDesfasado) as exc:
        ensure_schema(vieja)

    msg = str(exc.value)
    assert "rule_states.notify" in msg
    assert "1 fila" in msg, "el aviso tiene que decir cuántos datos hay en juego"


def test_si_hay_que_parar_no_se_migra_nada_a_medias(vieja):
    """Media migración es peor sitio para quedarse que el de antes.

    Las tablas que SÍ se podían arreglar no se tocan, para que reintentar
    después de migrar a mano parta siempre del mismo estado.

    Se prueban los dos arreglos a la vez porque son caminos distintos: una
    columna se añade con `ALTER TABLE` y una tabla vacía se DESTRUYE y se
    rehace. El segundo es el que de verdad duele si ocurre y luego se aborta.
    """
    vieja.envejecer("decisions", {"progression_json"})       # se arreglaría con ALTER
    vieja.envejecer("exercise_targets", {"routine_key"})     # se reharía entera
    vieja.envejecer("rule_states", {"notify"})               # el bloqueo
    with vieja.begin() as c:
        c.execute(text(
            "INSERT INTO rule_states (rule_name, entity, active_from) "
            "VALUES ('retirada_peso_muerto', 'peso_muerto_smith', '2026-09-07')"
        ))

    with pytest.raises(SchemaDesfasado):
        ensure_schema(vieja)

    assert "progression_json" not in _columnas(vieja, "decisions"), (
        "se ha añadido una columna aunque había que detenerse"
    )
    assert "routine_key" not in _columnas(vieja, "exercise_targets"), (
        "se ha rehecho una tabla aunque había que detenerse"
    )


def test_una_tabla_vacia_se_rehace_aunque_la_columna_sea_obligatoria(vieja):
    """El bloqueo es no poder rellenar las filas existentes. Sin filas, no hay
    nada que rellenar, y este es el caso normal en una instalación nueva."""
    vieja.envejecer("exercise_targets", {"routine_key"})

    cambios = ensure_schema(vieja)

    assert "routine_key" in _columnas(vieja, "exercise_targets")
    assert any("exercise_targets" in c for c in cambios)


def test_rehacer_una_tabla_vacia_no_arrastra_a_las_demas(vieja):
    """Se rehace la tabla desfasada, no la base entera."""
    vieja.envejecer("exercise_targets", {"routine_key"})
    with vieja.begin() as c:
        c.execute(text(
            "INSERT INTO decisions (date, light, source, is_current) "
            "VALUES ('2026-09-07', 'amber', 'checkin', 1)"
        ))

    ensure_schema(vieja)

    with vieja.begin() as c:
        assert c.execute(text("SELECT light FROM decisions")).scalar() == "amber"


# ---------------------------------------------------------------------------
# El modelo y el disco, comparados sin excepciones
# ---------------------------------------------------------------------------


def test_tras_migrar_no_queda_ni_una_columna_por_debajo(vieja):
    """La comprobación de conjunto: se envejecen varias tablas a la vez y se
    exige que después NINGUNA table quede corta. Un test por columna se olvida
    de la que se añada mañana."""
    vieja.envejecer("decisions", {"progression_json"})
    vieja.envejecer("exercise_targets", {"routine_key", "last_compliant"})
    vieja.envejecer("rule_states", {"action_json"})

    ensure_schema(vieja)

    for nombre, tabla in Base.metadata.tables.items():
        faltan = {c.name for c in tabla.columns} - _columnas(vieja, nombre)
        assert not faltan, f"{nombre} sigue sin {sorted(faltan)}"
