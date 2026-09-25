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

from datetime import date

import pytest
from sqlalchemy import UniqueConstraint, create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateColumn

import app.db as db
from app.analysis.rendimiento import PERCEPCION_PEOR
from app.db import SchemaDesfasado, ensure_schema
from app.models import Base
from app.repository import adopciones_sin_contar, guardar_adopciones


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
        # QUITAR UNA COLUMNA QUE YA NO EXISTE NO ES QUITAR NADA.
        # -------------------------------------------------------------------
        # El filtro de abajo es `c.name not in quitar`: si el nombre que se pide
        # quitar deja de ser una columna del modelo -se renombra, se borra, se
        # escribe con una errata-, el filtro no descarta ninguna y la base
        # "vieja" sale IDÉNTICA a la nueva. A partir de ahí `ensure_schema` no
        # tiene nada que migrar, devuelve la lista de cambios vacía, y todos los
        # tests que solo comprueban el estado FINAL pasan: la columna está,
        # porque nunca se fue. La migración deja de estar probada y la suite
        # sigue verde.
        #
        # De los diecisiete sitios que llaman a esto, solo unos pocos asertan
        # que `cambios` no esté vacío. Los demás -incluidos los dos que
        # comparan el esquema entero columna a columna- se quedarían mudos.
        existentes = {c.name for c in Base.metadata.tables[tabla].columns}
        assert quitar <= existentes, (
            f"se pide envejecer {tabla} quitando {sorted(quitar - existentes)}, "
            f"que no son columnas del modelo. La base 'vieja' saldría idéntica "
            f"a la nueva y el test no probaría ninguna migración."
        )
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

    def con_unique(tabla: str, columnas: tuple[str, ...], nombre: str) -> None:
        """Rehace la tabla con un UNIQUE de tabla que el modelo ya no declara.

        Hace falta un camino aparte de `envejecer` porque un UNIQUE declarado en
        el CREATE TABLE no es un índice que se pueda soltar: no sale en
        `sqlite_master` como objeto propio -sale como `sqlite_autoindex_*`, que
        SQLite se niega a borrar- y la única forma de quitarlo es rehacer la
        tabla. Ésa es justo la razón por la que este desfase sobrevivió a todo.
        """
        existentes = {c.name for c in Base.metadata.tables[tabla].columns}
        assert set(columnas) <= existentes, (
            f"se pide un UNIQUE sobre {columnas} de {tabla} y alguna no es "
            f"columna del modelo: la base 'vieja' saldría sin restricción y el "
            f"test no probaría nada."
        )
        declaradas = {
            tuple(sorted(c.name for c in r.columns))
            for r in Base.metadata.tables[tabla].constraints
            if isinstance(r, UniqueConstraint)
        }
        assert tuple(sorted(columnas)) not in declaradas, (
            f"el modelo YA declara un UNIQUE sobre {columnas} en {tabla}: la "
            f"base 'vieja' saldría idéntica a la nueva y no habría desfase."
        )
        cols = list(Base.metadata.tables[tabla].columns)
        nombres = ", ".join(f'"{c.name}"' for c in cols)
        defs = ", ".join(
            str(CreateColumn(c).compile(dialect=eng.dialect)) for c in cols
        )
        pks = [c.name for c in cols if c.primary_key]
        if pks:
            defs += ", PRIMARY KEY (" + ", ".join(f'"{n}"' for n in pks) + ")"
        defs += (
            f', CONSTRAINT "{nombre}" UNIQUE ('
            + ", ".join(f'"{n}"' for n in columnas)
            + ")"
        )
        with eng.begin() as c:
            c.exec_driver_sql(f'CREATE TABLE "_tmp" ({defs})')
            c.exec_driver_sql(f'INSERT INTO "_tmp" SELECT {nombres} FROM "{tabla}"')
            c.exec_driver_sql(f'DROP TABLE "{tabla}"')
            c.exec_driver_sql(f'ALTER TABLE "_tmp" RENAME TO "{tabla}"')

    eng.envejecer = envejecer  # type: ignore[attr-defined]
    eng.con_unique = con_unique  # type: ignore[attr-defined]
    return eng


def _uniques_reales(eng, tabla: str) -> set[tuple[str, ...]]:
    """Los UNIQUE que hay EN EL DISCO, vengan de donde vengan.

    `PRAGMA index_list` los da todos juntos y el `origin` dice de dónde salen:
    'u' de un UNIQUE de tabla, 'c' de un CREATE UNIQUE INDEX, 'pk' de la clave
    primaria. Para lo que aquí se compara da igual entre los dos primeros —los
    dos rechazan el mismo INSERT con el mismo mensaje—, pero la clave primaria
    hay que dejarla fuera.

    Y no por pulcritud. `job_runs.job_id` es una primary key de TEXTO, y SQLite
    respalda ésas con un `sqlite_autoindex_*` UNIQUE de verdad. Contándola, esa
    tabla parece arrastrar un UNIQUE sobrante en una base recién creada, y la
    migración se pondría a rehacerla en cada arranque para quitar algo que el
    modelo sí declara —sólo que lo declara como `primary_key=True`—.
    """
    fuera = set()
    with eng.begin() as c:
        for fila in c.exec_driver_sql(f"PRAGMA index_list('{tabla}')"):
            _, nombre, es_unico, origen, _parcial = (list(fila) + [None] * 5)[:5]
            if not es_unico or origen == "pk":
                continue
            cols = [f[2] for f in c.exec_driver_sql(f"PRAGMA index_info('{nombre}')")]
            if all(c is not None for c in cols):
                fuera.add(tuple(sorted(cols)))
    return fuera


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


def _ddl(eng, tabla: str) -> dict[str, tuple]:
    """Por columna: (tipo, notnull, defecto). Lo que de verdad hay en la base."""
    with eng.begin() as c:
        return {
            f[1]: (f[2], f[3], f[4])
            for f in c.exec_driver_sql(f"PRAGMA table_info('{tabla}')")
        }


def _dif_ddl(eng_migrada, eng_nueva, tabla: str) -> dict:
    """Columnas en las que la base migrada y una recién creada no coinciden."""
    actualizada, recien_creada = _ddl(eng_migrada, tabla), _ddl(eng_nueva, tabla)
    return {
        k: (recien_creada.get(k), actualizada.get(k))
        for k in set(recien_creada) | set(actualizada)
        if recien_creada.get(k) != actualizada.get(k)
    }


def test_una_instalacion_actualizada_queda_igual_que_una_nueva(vieja, tmp_path):
    """Migrar tiene que dejar la MISMA tabla que crearla de cero.

    El `ALTER TABLE ... ADD COLUMN` compilaba solo el tipo de la columna y se
    dejaba por el camino el NOT NULL y el defecto declarados en el modelo. Los
    dos caminos arrancaban sin quejarse y producían esquemas distintos: en una
    instalación nueva `sessions_since_progress` era `INTEGER NOT NULL DEFAULT 0`,
    y en una actualizada quedaba nullable con las filas viejas a NULL.

    Es la peor forma de divergencia porque no la ve nadie: el desarrollo va
    contra una base recién creada y Umbrel contra una migrada, así que el sitio
    donde el esquema está mal es justo el único donde no se prueba.
    """
    vieja.envejecer("exercise_targets", {"sessions_since_progress", "current_sets_json"})
    with vieja.begin() as c:
        c.execute(text(
            "INSERT INTO exercise_targets (routine_key, exercise_key, clean_streak) "
            "VALUES ('dia_1', 'prensa_horizontal', 2)"
        ))

    ensure_schema(vieja)

    nueva = create_engine(f"sqlite:///{tmp_path / 'nueva.db'}", future=True)
    Base.metadata.create_all(nueva)

    dif = _dif_ddl(vieja, nueva, "exercise_targets")
    assert not dif, f"nueva vs actualizada difieren en {dif}"

    # Y la fila que ya estaba tiene el defecto, no NULL: es una cola en la que
    # el ejercicio lleva cero sesiones esperando, no una cola desconocida.
    with vieja.begin() as c:
        fila = list(c.execute(text(
            "SELECT clean_streak, sessions_since_progress FROM exercise_targets"
        )))
    assert fila == [(2, 0)], f"la fila vieja no se ha rellenado con el defecto: {fila}"


def test_una_base_anterior_a_la_adopcion_de_cargas_se_pone_al_dia(vieja, tmp_path):
    """Las dos columnas de la carga ejecutada, contra una base CON histórico.

    Es el caso de despliegue de verdad: la base lleva meses decidiendo, llega la
    versión que adopta lo que se levantó en Hevy y `exercise_targets` no tiene
    dónde guardar la racha por debajo. Sin la columna, el error no sale al
    arrancar -sale en el SELECT de la reconciliación, a las 22:30.

    Los dos defectos de las filas viejas NO son intercambiables, y por eso se
    miran por separado:

      - la racha entra a 0, que es "este ejercicio no lleva ninguna sesión por
        debajo del plan". A NULL la comparación contra `down_after_sessions`
        reventaría, y ahí no hay nadie mirando.
      - el mejor peso entra a NULL, que es "no hay ninguna sesión por debajo".
        Un 0 aquí no sería la ausencia del dato: sería un dato, y del peor tipo,
        porque es el peso que se adoptaría el día que tocara bajar.
    """
    vieja.envejecer("exercise_targets", {"below_plan_streak", "below_plan_best_kg"})
    with vieja.begin() as c:
        c.execute(text(
            "INSERT INTO exercise_targets (routine_key, exercise_key, clean_streak) "
            "VALUES ('dia_1', 'hip_thrust_barra', 2)"
        ))

    cambios = ensure_schema(vieja)

    assert any("below_plan_streak" in x for x in cambios), cambios
    assert any("below_plan_best_kg" in x for x in cambios), cambios

    nueva = create_engine(f"sqlite:///{tmp_path / 'nueva.db'}", future=True)
    Base.metadata.create_all(nueva)
    dif = _dif_ddl(vieja, nueva, "exercise_targets")
    assert not dif, f"nueva vs actualizada difieren en {dif}"

    with vieja.begin() as c:
        fila = list(c.execute(text(
            "SELECT clean_streak, below_plan_streak, below_plan_best_kg "
            "FROM exercise_targets"
        )))
    assert fila == [(2, 0, None)], (
        f"la racha tiene que entrar a cero y el mejor peso a NULL, y hay: {fila}"
    )


def test_los_entrenos_de_antes_del_cierre_del_dia_nacen_cerrados(vieja, tmp_path):
    """`workout_log.cerrado`, contra una base con entrenos ya reconciliados.

    El código de antes del 25/09/2026 movía rachas y pesos en cuanto un entreno
    llegaba. Esas filas YA aplicaron sus efectos: si la columna nueva entrara a
    0, el primer cierre de cada día las aplicaría otra vez y cada racha contaría
    doble, en una espalda con hernia. Por eso el defecto de la base es 1 y el
    del código, para las filas nuevas, es 0.
    """
    vieja.envejecer("workout_log", {"cerrado"})
    with vieja.begin() as c:
        c.execute(text(
            "INSERT INTO workout_log (hevy_workout_id, date, routine_key, unplanned) "
            "VALUES ('w-viejo', '2026-09-22', 'dia_2', 0)"
        ))

    cambios = ensure_schema(vieja)
    assert any("cerrado" in x for x in cambios), cambios

    with vieja.begin() as c:
        fila = list(c.execute(text("SELECT cerrado FROM workout_log")))
    assert fila == [(1,)], f"un entreno de antes ha nacido abierto: {fila}"


def test_una_base_anterior_al_backfill_aprende_a_marcar_lo_recuperado(vieja, tmp_path):
    """`daily_metrics.recovered_at`, contra una base que ya tiene wellness.

    La columna parece contabilidad y no lo es. Una fila rellenada a posteriori
    puede tener huecos que la del día no habría tenido -el body battery deja de
    servirse a los cuatro meses, medido-, y sin la marca un hueco de los dos
    tipos es la misma celda vacía: no se puede distinguir "esa noche no llevaba
    el reloj" de "se preguntó demasiado tarde".

    Y las filas que ya estaban entran a NULL, que aquí significa exactamente lo
    que tiene que significar: se escribieron el día que les tocaba. Una fecha
    inventada las convertiría a todas en recuperadas, que es la afirmación
    contraria a la verdadera.
    """
    vieja.envejecer("daily_metrics", {"recovered_at"})
    with vieja.begin() as c:
        c.execute(text(
            "INSERT INTO daily_metrics (date, hrv, rhr, fetch_status) "
            "VALUES ('2026-06-01', 58.0, 47.0, 'ok')"
        ))

    cambios = ensure_schema(vieja)
    assert any("recovered_at" in x for x in cambios), cambios

    nueva = create_engine(f"sqlite:///{tmp_path / 'nueva.db'}", future=True)
    Base.metadata.create_all(nueva)
    dif = _dif_ddl(vieja, nueva, "daily_metrics")
    assert not dif, f"nueva vs actualizada difieren en {dif}"

    with vieja.begin() as c:
        fila = list(c.execute(text(
            "SELECT hrv, recovered_at FROM daily_metrics"
        )))
    assert fila == [(58.0, None)], (
        f"lo que ya estaba se capturó en su día, no se recuperó: {fila}"
    )


def test_la_tabla_de_previsualizaciones_llega_a_una_base_que_ya_existia(
    vieja, monkeypatch
):
    """`previews` es tabla NUEVA, y llega a un despliegue donde la base SOBREVIVE.

    Es el caso exacto de este proyecto en Umbrel: el código va horneado en la
    imagen y la base de datos está montada fuera, así que al reconstruir el
    contenedor llega un `models.py` con una tabla que el disco no tiene. Si
    nadie la crea, el arranque no se queja: lo que revienta es el primer
    PREVISUALIZAR, delante del usuario y a las siete de la mañana.

    Y aquí perder el dato es perder la funcionalidad entera, no un adorno:
    estas filas SON la medida de si el sistema y el usuario convergen. No se
    reconstruyen después desde ningún sitio, porque una previsualización que no
    se guardó no dejó rastro en ninguna otra tabla -ese es justamente su diseño-.

    Se escribe y se lee una fila de verdad: `sqlite_master` solo demostraría que
    la tabla tiene nombre.
    """
    from app.models import Preview

    with vieja.begin() as c:
        c.exec_driver_sql('DROP TABLE "previews"')

    assert ensure_schema(vieja) == [], (
        "de las tablas AUSENTES no se encarga esta función"
    )

    monkeypatch.setattr(db, "engine", vieja)
    db.init_db()

    Sesion = sessionmaker(bind=vieja, future=True)
    with Sesion() as s:
        s.add(Preview(
            date=date(2026, 9, 10), seq=1,
            answers_json='{"fatigue": 4}', light="red", session_type="full",
            decision_json="{}", disagreed=True,
            disagreement_reason="hoy puedo mas",
            override_session_type="full", forced_on_red=True,
        ))
        s.commit()

    with Sesion() as s:
        fila = s.query(Preview).one()
        assert fila.disagreed is True
        assert fila.forced_on_red is True
        assert fila.decision_id is None, "previsualizada y no enviada"
        # Los defectos tienen que venir del servidor y no solo de Python.
        assert fila.created_at is not None

    with vieja.begin() as c:
        # Dos filas el mismo día. Es el caso que la tabla existe para admitir,
        # así que no basta con que la tabla esté: tiene que estar SIN el UNIQUE
        # que la haría rechazar la segunda previsualización de la mañana.
        c.exec_driver_sql(
            "INSERT INTO previews (date, seq, light) VALUES ('2026-09-10', 2, 'amber')"
        )
        assert c.exec_driver_sql(
            "SELECT count(*) FROM previews WHERE date = '2026-09-10'"
        ).scalar() == 2


def test_la_tabla_de_rendimiento_llega_a_una_base_que_ya_existia(vieja, monkeypatch):
    """`session_performance` es tabla NUEVA, y de las que no pueden perder nada.

    Vale lo mismo que para `load_adoptions`: `ensure_schema` no crea tablas y
    `create_all` no toca las que están, así que el reparto solo cierra si se
    llama a `init_db`. Por eso el test llama a `init_db` y no a las piezas.

    Lo que se perdería es peor que un dato. Esta tabla es el contador de cuántas
    veces la percepción fue peor que el rendimiento real, y es un contador que
    se mira justo la mañana en que uno se levanta convencido de que no puede
    entrenar. Si la tabla no existe, no revienta el arranque: revienta la
    escritura de después de la sesión, en un hilo del scheduler, y el contador
    se queda en el número de hace meses sin que nada lo diga.

    Se escribe y se lee una fila de verdad, con el `unique` de `source_key`
    incluido, porque `sqlite_master` solo demuestra que la tabla tiene nombre.
    """
    from app.models import SessionPerformance

    with vieja.begin() as c:
        c.exec_driver_sql('DROP TABLE "session_performance"')

    assert ensure_schema(vieja) == [], (
        "de las tablas AUSENTES no se encarga esta función"
    )

    monkeypatch.setattr(db, "engine", vieja)
    db.init_db()

    Sesion = sessionmaker(bind=vieja, future=True)
    with Sesion() as s:
        s.add(SessionPerformance(
            date=date(2026, 9, 10), kind="strength",
            source_key="strength:2026-09-10:dia_1", routine_key="dia_1",
            perceived_fatigue=4, perception_pct=12.0,
            performance_pct=68.0, gap_pct=56.0,
            direction=PERCEPCION_PEOR, dissociation=True, n_sessions_base=22,
        ))
        s.commit()

    with Sesion() as s:
        fila = s.query(SessionPerformance).one()
        assert fila.dissociation is True
        # El vocabulario de `direction` es el de `app.analysis.rendimiento`, y se
        # importa de allí en vez de escribirlo a mano. Una cadena inventada aquí
        # pasaría el test igual -la columna solo guarda texto- y dejaría en el
        # repositorio un valor que no existe en ningún sitio, listo para que
        # alguien lo copie el día que escriba la consulta del contador.
        assert fila.direction == "perception_worse"
        assert fila.gap_pct == 56.0
        # Los defectos tienen que venir del servidor, no solo de Python: una
        # columna NOT NULL sin `server_default` se añade bien en una base nueva
        # y revienta al migrar una que ya tiene filas.
        assert fila.n_sessions_base == 22
        assert fila.reported_at is None, (
            "recién escrita no se ha contado todavía en ningún Telegram"
        )

    with vieja.begin() as c:
        indices = {
            f[1] for f in c.exec_driver_sql(
                "PRAGMA index_list('session_performance')"
            )
        }
    assert "ix_session_performance_pendientes" in indices, (
        f"sin el índice, buscar lo no reportado es un recorrido entero: {indices}"
    )


def test_lo_no_reportado_no_se_puede_escribir_dos_veces(vieja, monkeypatch):
    """El `unique` de `source_key` tiene que sobrevivir a la creación.

    Aquí sí puede haber UNIQUE -y en `notifications` no- porque esta fila se
    escribe ANTES de cualquier efecto irreversible. En `notifications` el UNIQUE
    saltaba después de haber mandado el mensaje, y entonces destruía el registro
    de lo que de verdad había pasado; aquí lo único que impide es contar dos
    veces la misma sesión, que es exactamente lo que se quiere impedir.
    """
    from sqlalchemy.exc import IntegrityError

    from app.models import SessionPerformance

    with vieja.begin() as c:
        c.exec_driver_sql('DROP TABLE "session_performance"')
    monkeypatch.setattr(db, "engine", vieja)
    db.init_db()

    Sesion = sessionmaker(bind=vieja, future=True)
    with Sesion() as s:
        s.add(SessionPerformance(
            date=date(2026, 9, 10), kind="bike",
            source_key="bike:2026-09-10:9911", garmin_activity_id=9911,
        ))
        s.commit()

    with Sesion() as s:
        s.add(SessionPerformance(
            date=date(2026, 9, 10), kind="bike",
            source_key="bike:2026-09-10:9911", garmin_activity_id=9911,
        ))
        with pytest.raises(IntegrityError):
            s.commit()


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
# Una tabla entera nueva
# ---------------------------------------------------------------------------


def test_una_tabla_nueva_llega_a_una_base_que_ya_existia(vieja, monkeypatch):
    """`load_adoptions` no la añade `ensure_schema`, y aun así tiene que llegar.

    Aquí se reparten el trabajo dos funciones y ninguna de las dos lo hace
    entero: `create_all` crea las tablas que faltan y no toca las que están,
    `ensure_schema` pone al día las que están y no crea ninguna. El reparto solo
    cierra si alguien llama a las dos, y ese alguien es `init_db`. Por eso este
    test llama a `init_db` y no a las piezas: una versión futura que se dejase
    el `create_all` pasaría todos los demás tests de este fichero.

    Y el fallo sería el de siempre, pero con una tabla entera: el contenedor se
    actualiza, arranca sin quejarse, manda su mensaje de las nueve, y a las
    22:30 `guardar_adopciones` escribe contra una tabla que no existe. Se pierde
    la adopción y, con ella, la única explicación de por qué mañana el hip
    thrust pide 62,5 en vez de 60.

    Por eso no basta con mirar `sqlite_master`: se escribe y se lee una adopción
    de verdad por el mismo camino que usa la reconciliación.
    """
    with vieja.begin() as c:
        c.exec_driver_sql('DROP TABLE "load_adoptions"')

    assert ensure_schema(vieja) == [], (
        "de las tablas AUSENTES no se encarga esta función, y si empezara a "
        "hacerlo estaría rehaciendo a ciegas lo que create_all ya sabe crear"
    )

    monkeypatch.setattr(db, "engine", vieja)
    db.init_db()

    Sesion = sessionmaker(bind=vieja, future=True)
    with Sesion() as s:
        guardar_adopciones(s, date(2026, 9, 10), [{
            "routine": "dia_1", "key": "hip_thrust_barra", "direction": "up",
            "prescribed_kg": 60.0, "executed_kg": 62.5,
            "before_kg": 60.0, "after_kg": 62.5,
            "applied": True, "reason": "se levantó eso de verdad",
        }])
        s.commit()
        pendientes = adopciones_sin_contar(s)

    assert [(p["key"], p["after_kg"]) for p in pendientes] == [
        ("hip_thrust_barra", 62.5)
    ], f"la tabla existe pero no sirve para lo que existe: {pendientes}"


def test_crear_la_tabla_que_falta_no_toca_el_historico(vieja, monkeypatch):
    """Rellenar un hueco no puede costar lo que ya había en las demás tablas."""
    with vieja.begin() as c:
        c.exec_driver_sql('DROP TABLE "load_adoptions"')
        c.execute(text(
            "INSERT INTO decisions (date, light, source, is_current) "
            "VALUES ('2026-09-07', 'amber', 'checkin', 1)"
        ))

    monkeypatch.setattr(db, "engine", vieja)
    db.init_db()

    with vieja.begin() as c:
        assert c.execute(text("SELECT light FROM decisions")).scalar() == "amber"


# ---------------------------------------------------------------------------
# Las que SOBRAN, que es la otra dirección y no la miraba nadie
# ---------------------------------------------------------------------------
#
# Todo lo de arriba compara en un solo sentido: columnas del modelo que faltan
# en el fichero. Al revés no miraba nadie, y por eso `activities` tenía cinco
# columnas -`start_time_local`, `type_key`, `elevation_loss_m`, `max_hr` y
# `raw_json`- que el modelo había borrado meses antes y seguían en el disco.
#
# El guardián que ya existía, `columnas_actividad_sin_escribir()`, no podía
# verlas ni en principio: compara contra el MODELO, así que una columna que el
# modelo ya no declara le resulta invisible por definición. La base y el código
# llevaban medio año discrepando sin que nada lo dijera.
#
# Una columna de más no es inocua: es la que sale en un `SELECT *`, en un
# volcado o en un `PRAGMA table_info` y parece un dato que se está guardando.
# Pasó exactamente eso -`max_hr` figuraba en la lista de columnas muertas a
# revisar cuando en el código no existía desde hacía meses.


def _anadir_columna_muerta(eng, tabla: str, columna: str, tipo: str = "FLOAT") -> None:
    """Una columna que el modelo NO declara, como la dejaría una versión vieja."""
    with eng.begin() as c:
        c.exec_driver_sql(f'ALTER TABLE "{tabla}" ADD COLUMN "{columna}" {tipo}')


def test_una_columna_que_sobra_y_esta_vacia_se_borra(vieja):
    """El caso real y el más común: la columna se quitó del modelo y quedó ahí.

    Se usa `readiness` a propósito, que es la que motivó esto. Estuvo NULL los
    ciento setenta y nueve días del backfill porque la calcula el reloj y este
    reloj no la calcula, así que borrarla no cuesta un dato: cuesta una columna
    vacía que parecía una medición.
    """
    _anadir_columna_muerta(vieja, "daily_metrics", "readiness")
    assert "readiness" in _columnas(vieja, "daily_metrics")

    cambios = ensure_schema(vieja)

    assert "readiness" not in _columnas(vieja, "daily_metrics")
    assert any("readiness" in c and "BORRADA" in c for c in cambios), (
        f"borrar una columna en silencio es justo lo que no puede pasar: {cambios}"
    )


def test_una_columna_que_sobra_CON_datos_detiene_el_arranque(vieja):
    """Lo que hay dentro puede ser la única copia, y un DROP COLUMN no se deshace.

    Es el mismo criterio que el de una NOT NULL sin defecto y por el mismo
    motivo: ante la duda, parar. La diferencia con el test de arriba no es si la
    TABLA tiene filas sino si la COLUMNA tiene valores, que es lo que decide si
    se está tirando algo.
    """
    _anadir_columna_muerta(vieja, "daily_metrics", "spo2_nocturno")
    with vieja.begin() as c:
        c.execute(text(
            "INSERT INTO daily_metrics (date, hrv, fetch_status, spo2_nocturno) "
            "VALUES ('2026-06-01', 58.0, 'ok', 94.0)"
        ))

    with pytest.raises(SchemaDesfasado) as exc:
        ensure_schema(vieja)

    msg = str(exc.value)
    assert "daily_metrics.spo2_nocturno" in msg
    assert "1 valor" in msg, "hay que decir cuántos datos están en juego"
    assert "spo2_nocturno" in _columnas(vieja, "daily_metrics"), (
        "ha borrado la columna a pesar de tener que pararse"
    )


def test_una_columna_vacia_en_una_tabla_CON_filas_si_se_borra(vieja):
    """`count(col)` cuenta los NO nulos, y esa es exactamente la pregunta.

    Este es el caso de verdad: `activities` tenía cincuenta y ocho filas y las
    cinco columnas muertas estaban a NULL en las cincuenta y ocho. Preguntar por
    si la tabla tiene filas -en vez de por si la columna tiene valores- habría
    bloqueado el arranque para no borrar nada.
    """
    _anadir_columna_muerta(vieja, "daily_metrics", "readiness")
    with vieja.begin() as c:
        c.execute(text(
            "INSERT INTO daily_metrics (date, hrv, fetch_status) "
            "VALUES ('2026-06-01', 58.0, 'ok')"
        ))

    ensure_schema(vieja)

    assert "readiness" not in _columnas(vieja, "daily_metrics")
    with vieja.begin() as c:
        assert c.execute(text("SELECT hrv FROM daily_metrics")).scalar() == 58.0, (
            "podar una columna vacía no puede costar la fila"
        )


def test_las_dos_fechas_muertas_del_objetivo_se_van_solas(vieja):
    """`exercise_targets` tenía dos fechas declaradas que nadie escribió jamás.

    `last_progressed_date` y `last_session_date` prometían contestar «¿cuándo se
    entrenó / subió esto por última vez?» y contestaban NULL siempre, porque en
    todo el código no había una sola línea que las rellenara. La respuesta buena
    vive en las sesiones y las decisiones.

    Aquí se comprueba lo único que hace falta para que quitarlas del modelo no
    pida nada a mano en Umbrel: que la base que YA las tiene se las quite sola,
    incluso teniendo filas, porque las filas tienen esas dos a NULL por
    construcción.
    """
    _anadir_columna_muerta(vieja, "exercise_targets", "last_progressed_date", "DATE")
    _anadir_columna_muerta(vieja, "exercise_targets", "last_session_date", "DATE")
    with vieja.begin() as c:
        c.execute(text(
            "INSERT INTO exercise_targets "
            "(routine_key, exercise_key, current_target_kg, clean_streak, "
            " sessions_since_progress, below_plan_streak) "
            "VALUES ('dia_1', 'extension_cuadriceps', 55.0, 1, 0, 0)"
        ))

    ensure_schema(vieja)

    quedan = _columnas(vieja, "exercise_targets")
    assert "last_progressed_date" not in quedan
    assert "last_session_date" not in quedan
    with vieja.begin() as c:
        assert c.execute(
            text("SELECT current_target_kg FROM exercise_targets")
        ).scalar() == 55.0, "la carga vigente no se puede ir con las fechas muertas"


def test_una_columna_que_sobra_se_lleva_su_indice_por_delante(vieja):
    """Sin esto el arranque fallaba: SQLite rechaza el DROP de una indexada.

    Y no es un caso rebuscado, es el que había: `activities.type_key` arrastraba
    `ix_activities_type_key`. Un `ALTER TABLE DROP COLUMN` a secas habría
    reventado en el arranque -o sea, en Umbrel, al actualizar el contenedor- con
    un error de SQLite que no explica nada.

    El índice se va con ella porque un índice sobre una columna que ya no existe
    tampoco tendría a quién servir.
    """
    _anadir_columna_muerta(vieja, "activities", "type_key", "VARCHAR")
    with vieja.begin() as c:
        c.exec_driver_sql(
            'CREATE INDEX "ix_activities_type_key" ON "activities" ("type_key")'
        )

    cambios = ensure_schema(vieja)

    assert "type_key" not in _columnas(vieja, "activities"), cambios
    with vieja.begin() as c:
        indices = {
            f[1] for f in c.exec_driver_sql("PRAGMA index_list('activities')")
        }
    assert "ix_activities_type_key" not in indices, (
        f"el índice ha sobrevivido a su columna: {indices}"
    )


def test_si_una_columna_que_sobra_bloquea_no_se_migra_nada_a_medias(vieja):
    """El bloqueo por columna sobrante para igual que el otro, y para TODO.

    Las dos direcciones se miran en la misma pasada de lectura y se ejecutan en
    la misma de escritura, justo para que esto se cumpla: lo que una tabla
    podría arreglar no se arregla si otra tabla obliga a parar.
    """
    vieja.envejecer("decisions", {"progression_json"})   # se arreglaría con ALTER
    _anadir_columna_muerta(vieja, "daily_metrics", "spo2_nocturno")
    with vieja.begin() as c:
        c.execute(text(
            "INSERT INTO daily_metrics (date, fetch_status, spo2_nocturno) "
            "VALUES ('2026-06-01', 'ok', 94.0)"
        ))

    with pytest.raises(SchemaDesfasado):
        ensure_schema(vieja)

    assert "progression_json" not in _columnas(vieja, "decisions"), (
        "se ha añadido una columna aunque había que detenerse"
    )


def test_las_dos_direcciones_se_arreglan_en_la_misma_pasada(vieja):
    """Una que falta y otra que sobra, a la vez. Es el despliegue de verdad.

    Una versión que quita una columna y añade otra deja la base descuadrada por
    los dos lados el mismo día. Si cada dirección necesitara su propio arranque,
    el primero acabaría en una base que sigue sin cuadrar y nadie volvería a
    mirarla.
    """
    vieja.envejecer("daily_metrics", {"recovered_at"})
    _anadir_columna_muerta(vieja, "daily_metrics", "readiness")

    cambios = ensure_schema(vieja)

    cols = _columnas(vieja, "daily_metrics")
    assert "recovered_at" in cols and "readiness" not in cols, cambios


def test_una_base_al_dia_no_ve_columnas_sobrantes_donde_no_las_hay(vieja):
    """El riesgo de la comprobación nueva es que borre lo que no debe.

    Una base recién creada por `create_all` coincide con el modelo por los dos
    lados, así que la pasada tiene que salir sin un solo cambio. Si esto fallara,
    el fallo no sería un test en rojo: sería `ensure_schema` borrando columnas
    buenas en cada arranque.
    """
    assert ensure_schema(vieja) == []


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


def test_tras_migrar_no_sobra_ni_una_columna(vieja):
    """La misma comprobación de conjunto, en la dirección que faltaba.

    El test de arriba lleva meses pasando y la base real tenía cinco columnas de
    más: comprobar solo un sentido deja pasar exactamente la mitad de las
    discrepancias, y encima la mitad que no da error nunca.

    Se ensucian tres tablas a la vez, con una indexada entre ellas, porque el
    caso que de verdad ocurre es el de una versión que quita varias columnas en
    el mismo despliegue.
    """
    _anadir_columna_muerta(vieja, "daily_metrics", "readiness")
    _anadir_columna_muerta(vieja, "activities", "type_key", "VARCHAR")
    _anadir_columna_muerta(vieja, "decisions", "score_interno")
    with vieja.begin() as c:
        c.exec_driver_sql(
            'CREATE INDEX "ix_activities_type_key" ON "activities" ("type_key")'
        )

    ensure_schema(vieja)

    for nombre, tabla in Base.metadata.tables.items():
        sobran = _columnas(vieja, nombre) - {c.name for c in tabla.columns}
        assert not sobran, f"{nombre} arrastra {sorted(sobran)}"


# ---------------------------------------------------------------------------
# Las RESTRICCIONES, que es lo que no miraba nadie en ninguna de las dos
# direcciones
# ---------------------------------------------------------------------------

# Todo lo de arriba compara COLUMNAS. Una restricción de tabla no es una
# columna, así que nada de lo anterior la ve, y eso costó un día entero de
# sistema.
#
# El 18 de septiembre de 2026 el check-in se guardó y la decisión reventó con
# `UNIQUE constraint failed: notifications.date, notifications.kind`. Esa
# restricción llevaba borrada del modelo desde f5e9758, con un docstring en
# `Notification` que empieza por "POR QUÉ NO HAY UNIQUE SOBRE (date, kind)" y
# cuenta este fallo exacto -el mensaje ya se ha mandado cuando salta, así que
# la excepción no impide el segundo aviso: solo destruye el registro de lo que
# sí pasó-. El modelo estaba arreglado, los tests pasaban, y la base de datos
# desplegada seguía teniendo la restricción, porque `create_all` no toca las
# tablas que ya existen y `ensure_schema` solo sabía de columnas.
#
# Es el peor sabor de desfase que hay: el que tiene el arreglo escrito, probado
# y documentado, y no ha llegado al único sitio donde importa.


def test_un_unique_que_el_modelo_ya_no_declara_se_va(vieja):
    """El fallo del 18 de septiembre, en una línea.

    La restricción se quitó del modelo hace meses y siguió viva en el disco. No
    la veía nadie: no es una columna, y comparar columnas es lo único que había.
    """
    vieja.con_unique("notifications", ("date", "kind"), "uq_notification_date_kind")
    assert ("date", "kind") in _uniques_reales(vieja, "notifications")

    cambios = ensure_schema(vieja)

    assert ("date", "kind") not in _uniques_reales(vieja, "notifications")
    assert any("notifications" in c for c in cambios), (
        f"la restricción se fue sin que `ensure_schema` lo contara: {cambios}. "
        f"Una migración muda es la que nadie encuentra cuando algo sale mal."
    )


def test_quitar_el_unique_no_se_lleva_por_delante_el_historico(vieja):
    """Lo que hay dentro de `notifications` es el registro de lo que se envió.

    Rehacer una tabla es copiar filas de una a otra, y ahí es donde se pierden
    los historiales. Este test existe porque el arreglo del desfase es más
    peligroso que el desfase: la restricción sobrante solo rompe el día que se
    decide dos veces, y una copia mal hecha se lleva el año entero.
    """
    vieja.con_unique("notifications", ("date", "kind"), "uq_notification_date_kind")
    Session = sessionmaker(bind=vieja, future=True)
    with Session() as s:
        s.execute(
            text(
                "INSERT INTO notifications (date, kind, channel, status, body) "
                "VALUES ('2026-09-17', 'decision', 'telegram', 'sent', 'el de ayer')"
            )
        )
        s.commit()

    ensure_schema(vieja)

    with vieja.begin() as c:
        filas = list(
            c.exec_driver_sql(
                "SELECT date, kind, channel, status, body FROM notifications"
            )
        )
    assert filas == [("2026-09-17", "decision", "telegram", "sent", "el de ayer")]


def test_sin_el_unique_caben_dos_decisiones_del_mismo_dia(vieja):
    """La prueba que de verdad importa: que el segundo check-in del día entre.

    Los dos tests de arriba miran el esquema. Éste mira lo único que el usuario
    nota, que es si rehacer el check-in le contesta con la decisión o con un
    error de SQLAlchemy. Se escribe aparte porque un `PRAGMA` que dice lo que
    uno quiere oír y un INSERT que pasa no son la misma afirmación.
    """
    vieja.con_unique("notifications", ("date", "kind"), "uq_notification_date_kind")

    ensure_schema(vieja)

    with vieja.begin() as c:
        for cual in ("el de las nueve", "el de rehacer el check-in"):
            c.exec_driver_sql(
                "INSERT INTO notifications (date, kind, channel, status, body) "
                f"VALUES ('2026-09-18', 'decision', 'telegram', 'sent', '{cual}')"
            )
        assert c.exec_driver_sql(
            "SELECT count(*) FROM notifications WHERE date='2026-09-18'"
        ).scalar() == 2


def test_un_unique_sobrante_con_datos_que_lo_violarian_no_se_puede_dar(vieja):
    """Rehacer la tabla NO puede fallar por las filas que ya tiene.

    Es la trampa del orden: si el UNIQUE se quitara creando la tabla nueva con
    la restricción todavía puesta, o copiando antes de quitarla, una base con
    dos avisos del mismo día -que es exactamente la que se quiere arreglar-
    reventaría al migrar. La tabla nueva se hace SIN la restricción, así que
    cualquier cosa que hubiera dentro cabe.
    """
    vieja.con_unique("notifications", ("date", "kind"), "uq_notification_date_kind")
    with vieja.begin() as c:
        c.exec_driver_sql(
            "INSERT INTO notifications (date, kind, channel, status, body) "
            "VALUES ('2026-09-16', 'decision', 'telegram', 'sent', 'uno')"
        )

    ensure_schema(vieja)

    with vieja.begin() as c:
        c.exec_driver_sql(
            "INSERT INTO notifications (date, kind, channel, status, body) "
            "VALUES ('2026-09-16', 'decision', 'telegram', 'sent', 'dos')"
        )
        assert c.exec_driver_sql("SELECT count(*) FROM notifications").scalar() == 2


def test_una_base_al_dia_no_rehace_tablas_que_no_lo_necesitan(vieja):
    """Sin desfase, ni un cambio.

    Sin esto, la forma más fácil de poner verde todo lo de arriba es rehacer
    `notifications` en cada arranque: el UNIQUE se iría siempre, y con él se
    iría media tabla cada vez que la copia tuviera un fallo. Una migración que
    corre cuando no hace falta es una que nadie mira.
    """
    assert ensure_schema(vieja) == []


def test_tras_migrar_no_sobra_ni_un_unique_en_ninguna_tabla(vieja):
    """La comprobación de conjunto, en la dirección que faltaba entera.

    Un test por restricción se olvida de la que alguien quite mañana, que es
    precisamente lo que pasó con ésta: se quitó del modelo en un commit que
    explicaba muy bien por qué, y no había nada comparando el modelo con el
    disco que pudiera notarlo.
    """
    vieja.con_unique("notifications", ("date", "kind"), "uq_notification_date_kind")
    # `activities` va porque el modelo SÍ declara un UNIQUE suyo
    # -`garmin_activity_id`-, y una barrida que se lleve el sobrante y también
    # ése deja de detectarse mirando solo `notifications`.
    vieja.con_unique("activities", ("date",), "uq_activity_date")

    ensure_schema(vieja)

    for nombre, tabla in Base.metadata.tables.items():
        declarados = {
            tuple(sorted(c.name for c in r.columns))
            for r in tabla.constraints
            if isinstance(r, UniqueConstraint)
        } | {
            tuple(sorted(c.name for c in i.columns))
            for i in tabla.indexes
            if i.unique
        }
        sobran = _uniques_reales(vieja, nombre) - declarados
        assert not sobran, f"{nombre} arrastra el UNIQUE {sorted(sobran)}"
