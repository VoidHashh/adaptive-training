"""Que el motor no amanezca con la memoria en blanco.

Este sistema decide cada mañana apoyándose en lo que recuerda: cuántas sesiones
limpias lleva cada ejercicio, qué reglas especiales siguen vigentes, por dónde
va la rotación de fuerza, cuándo fue la última descarga. Todo eso vive en un
`EngineState` que existe en memoria mientras el proceso está vivo.

Un fallo aquí no se parece a un error: se parece a un sistema que funciona. La
decisión sale, el mensaje se envía, y lo único que pasa es que la racha de
sesiones limpias vuelve a cero cada noche y el hip thrust no sube nunca. O que
el peso muerto retirado catorce días reaparece al primer reinicio.

Por eso el test principal no es una lista de campos a mano -esa lista se queda
vieja el día que alguien añada uno-, sino un recorrido por
`dataclasses.fields(EngineState)`.
"""

from __future__ import annotations

import json
from dataclasses import fields as dataclass_fields
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.engine.decision import ActiveRule, EngineState, advance_state, decide
from app.engine.session_builder import SesionPedida
from app.models import (
    Activity,
    Base,
    Decision as DecisionRow,
    ExerciseTarget,
    LoadAdoption,
    Preview as PreviewRow,
    RuleState,
    WorkoutLog,
)
from app.repository import (
    CAMPOS_ACTIVIDAD,
    CAMPOS_PERSISTIDOS,
    COLUMNAS_ACTIVIDAD_APARTE,
    adopciones_sin_contar,
    campos_sin_persistir,
    columnas_actividad_sin_escribir,
    checkin_values,
    current_decision,
    enlazar_preview,
    enlazar_previews_pendientes,
    fusionar_metricas,
    get_checkin,
    guardar_adopciones,
    load_state,
    marcar_adopciones_contadas,
    metricas_guardadas,
    opciones_del_config,
    preguntas_del_config,
    previews_del_dia,
    save_decision,
    save_preview,
    save_state,
    serie_decisiones,
    sliders_del_config,
    state_as_dict,
    upsert_activities,
    upsert_checkin,
)

from tests.conftest import LUNES, sig_completa


@pytest.fixture
def db():
    """Base de datos en memoria, nueva para cada test."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def estado_lleno():
    """Un estado con TODOS los campos puestos, ninguno en su valor por defecto.

    Que no haya defectos es el punto: un campo que se guardase mal pero cuyo
    valor coincidiese con el que se construye al cargar pasaría inadvertido.
    """
    return EngineState(
        clean_sessions={("dia_1", "hip_thrust_barra"): 2, ("dia_2", "remo_t_apoyado"): 0},
        compliance={("dia_1", "hip_thrust_barra"): True, ("dia_2", "remo_t_apoyado"): False},
        current_sets={
            ("dia_1", "hip_thrust_barra"): [
                {"type": "normal", "reps": 10, "weight_kg": 62.5},
                {"type": "normal", "reps": 10, "weight_kg": 62.5},
            ],
            ("dia_2", "remo_t_apoyado"): [{"type": "normal", "reps": 12, "weight_kg": 40.0}],
        },
        # Distintos entre sí y distintos de cero: un cero se confundiría con
        # "nunca guardado" y con el defecto de la columna.
        sessions_since_progress={
            ("dia_1", "hip_thrust_barra"): 3,
            ("dia_2", "remo_t_apoyado"): 7,
        },
        # Solo `dia_1` se ha quedado corto. Que `dia_2` no aparezca en estos dos
        # diccionarios no es pereza: la ausencia es el valor normal -la inmensa
        # mayoría de ejercicios nunca se quedan por debajo- y la vuelta completa
        # tiene que devolverla como ausencia, no como un cero que luego se
        # confundiría con "lleva dos sesiones cortas y a la próxima baja".
        below_plan_streak={("dia_1", "hip_thrust_barra"): 2},
        below_plan_best_kg={("dia_1", "hip_thrust_barra"): 52.5},
        last_routine_light={"dia_1": "green", "dia_2": "amber"},
        active_rules=[
            ActiveRule(
                name="lumbar_retirada",
                action={"drop_exercises": ["peso_muerto_smith"]},
                active_from=LUNES,
                active_until=LUNES + timedelta(days=13),
                entity="peso_muerto_smith",
                reason="molestia lumbar 7/10",
                notify=True,
            )
        ],
        # Este NO lo escribe `save_state`: el puntero de la rotación sale de
        # `workout_log`, de sesiones ejecutadas. Está aquí porque el test que
        # exige que ningún campo se quede en su valor por defecto tiene razón en
        # exigirlo, y porque `state_as_dict` sí lo enseña.
        last_strength=("dia_3", LUNES - timedelta(days=1)),
        program_start=LUNES - timedelta(weeks=4),
        last_deload_start=LUNES - timedelta(weeks=2),
        # Distinto de `last_deload_start`: son dos fechas con el mismo tipo y
        # guardarlas cruzadas daría la vuelta completa por buena.
        deload_aplazada_desde=LUNES - timedelta(weeks=1),
    )


# ---------------------------------------------------------------------------
# La guarda: ningún campo del estado se queda sin guardar
# ---------------------------------------------------------------------------


def test_todos_los_campos_del_estado_estan_contemplados():
    """El test que evita el fallo que nadie ve.

    Si mañana `EngineState` gana un campo y `repository.py` no lo guarda, el
    sistema no dará ningún error: simplemente ese campo volverá a su valor por
    defecto cada arranque. Esto lo dice antes, y con el nombre del campo.
    """
    assert not campos_sin_persistir(), (
        f"campos de EngineState que nadie guarda: {sorted(campos_sin_persistir())}. "
        f"Añádelos a `repository.save_state`/`load_state` y a CAMPOS_PERSISTIDOS."
    )


def test_la_lista_de_campos_no_inventa_ninguno():
    """Al revés que el anterior: que CAMPOS_PERSISTIDOS no nombre campos que ya
    no existen, porque entonces protegería a un fantasma."""
    reales = {f.name for f in dataclass_fields(EngineState)}
    assert set(CAMPOS_PERSISTIDOS) <= reales, (
        f"CAMPOS_PERSISTIDOS nombra campos inexistentes: "
        f"{sorted(set(CAMPOS_PERSISTIDOS) - reales)}"
    )


# ---------------------------------------------------------------------------
# El mismo agujero, en la otra tabla
# ---------------------------------------------------------------------------
#
# `EngineState` tenía desde hace tiempo su red contra los campos que nadie
# guarda. `activities` no la tenía, y se notó: `elevation_gain_m`,
# `moving_duration_s` y `avg_hr` estaban declaradas en el modelo, se leían en
# `app/analysis/rendimiento.py`, se comparaban contra el histórico y salían en
# el mensaje de Telegram de la vista 5 -"42 km a 25,2 km/h con 0 m/km de
# desnivel"-, y no las escribía ninguna ruta del código. Las tres columnas
# estaban a NULL en todas las filas, siempre, y el `or 0.0` del análisis
# convertía ese hueco en un cero con pinta de medición.
#
# Nada falló. Ese es el problema: un feature entero -calculado, guardado,
# percentilado, probado y mencionado en un commit- colgando de una columna que
# no escribía nadie. Lo de abajo es para que la próxima vez falle.


def test_todas_las_columnas_de_actividades_las_escribe_alguien():
    """Que ninguna columna de `activities` se quede sin quien la rellene.

    El fallo que evita no da error en ninguna parte: la columna existe, se
    puede leer, y devuelve NULL. Quien la lee se encuentra un hueco donde
    esperaba un número, y si ese alguien tiene un valor por defecto a mano, el
    hueco se convierte en un dato falso sin que nadie se entere.
    """
    assert not columnas_actividad_sin_escribir(), (
        f"columnas de `activities` que no escribe nadie: "
        f"{sorted(columnas_actividad_sin_escribir())}. O las copia "
        f"`upsert_activities` -añádelas a CAMPOS_ACTIVIDAD- o se escriben en "
        f"otro sitio -entonces van a COLUMNAS_ACTIVIDAD_APARTE- o sobran en el "
        f"modelo. Lo que no puede ser es que se queden a NULL para siempre."
    )


def test_la_lista_de_columnas_de_actividades_no_inventa_ninguna():
    """Al revés: que las dos listas no nombren columnas que ya no existen.

    Una lista que protege a un fantasma da la misma tranquilidad que una que
    protege de verdad, y no es lo mismo.
    """
    reales = {c.name for c in Activity.__table__.columns}
    nombradas = set(CAMPOS_ACTIVIDAD) | set(COLUMNAS_ACTIVIDAD_APARTE)
    assert nombradas <= reales, (
        f"se nombran columnas inexistentes de `activities`: "
        f"{sorted(nombradas - reales)}"
    )


def test_una_salida_de_garmin_llega_entera_hasta_la_fila(db):
    """La cadena completa: diccionario crudo -> `Ride` -> fila en la BD.

    Se parte del crudo de Garmin y no de un `Ride` construido a mano porque el
    fallo estaba precisamente en las costuras: el dataclass no tenía los
    campos, el parser no los leía y el bucle de copia no los nombraba. Un
    `Ride` hecho a mano en el test se salta las dos primeras y solo habría
    pillado un tercio del problema.
    """
    from app.integrations.garmin import ride_from_activity

    ride = ride_from_activity(
        {
            "activityId": 4242,
            "activityName": "Puerto de la mañana",
            "activityType": {"typeKey": "cycling"},
            "startTimeLocal": "2026-09-07 08:30:00",
            "duration": 5400.0,
            "movingDuration": 5100.0,
            "distance": 42000.0,
            "elevationGain": 540.0,
            "averageHR": 142.0,
            "activityTrainingLoad": 210.0,
            "aerobicTrainingEffect": 3.8,
            "anaerobicTrainingEffect": 0.6,
        }
    )
    assert ride is not None

    assert upsert_activities(db, [ride]) == 1
    db.commit()

    fila = db.scalars(
        select(Activity).where(Activity.garmin_activity_id == 4242)
    ).one()
    assert fila.elevation_gain_m == 540.0
    assert fila.moving_duration_s == 5100.0
    assert fila.avg_hr == 142.0
    # Y los que ya funcionaban, para que se vea que el test mira la fila entera
    # y no solo los tres recién conectados.
    assert fila.distance_m == 42000.0
    assert fila.training_load == 210.0
    assert fila.is_cycling is True


def test_una_salida_sin_desnivel_deja_la_columna_a_nulo_y_no_a_cero(db):
    """Un rodillo de interior no da desnivel, y eso no es cero metros.

    La columna tiene que quedarse a NULL para que quien la lea sepa que no lo
    sabe. Guardar un 0.0 aquí sería mentir una sola vez y que la mentira se
    quedase en el histórico contra el que se comparan las demás salidas.
    """
    from app.integrations.garmin import ride_from_activity

    ride = ride_from_activity(
        {
            "activityId": 4343,
            "activityName": "Rodillo",
            "activityType": {"typeKey": "indoor_cycling"},
            "startTimeLocal": "2026-09-08 19:00:00",
            "duration": 3600.0,
            "distance": 30000.0,
        }
    )
    assert ride is not None

    upsert_activities(db, [ride])
    db.commit()

    fila = db.scalars(
        select(Activity).where(Activity.garmin_activity_id == 4343)
    ).one()
    assert fila.elevation_gain_m is None
    assert fila.avg_hr is None
    assert fila.moving_duration_s is None


def test_los_diez_campos_nuevos_llegan_hasta_la_fila(db):
    """La misma costura de tres piezas, otra vez y con diez campos.

    Se comprueba el camino entero -crudo de Garmin, `Ride`, columna- porque el
    fallo de los tres anteriores vivía justo entre las piezas y ninguna de las
    tres fallaba sola. Aquí hay una costura más: el nombre de cada campo del
    `Ride` tiene que coincidir con el de su columna, porque `upsert_activities`
    copia por nombre recorriendo `CAMPOS_ACTIVIDAD`. Renombrar uno en el
    dataclass y no en la lista deja la columna a NULL sin un solo error.
    """
    from app.integrations.garmin import ride_from_activity

    ride = ride_from_activity({
        "activityId": 4444,
        "activityName": "Puerto en agosto",
        "activityType": {"typeKey": "cycling"},
        "startTimeLocal": "2026-08-14 09:00:00",
        "duration": 7200.0,
        "distance": 50000.0,
        "elevationGain": 900.0,
        "elevationLoss": 880.0,
        "averageHR": 141.0,
        "maxHR": 171.0,
        "averageSpeed": 6.94,
        "maxSpeed": 15.5,
        "calories": 980.0,
        "avgRespirationRate": 22.0,
        "maxRespirationRate": 35.0,
        "minRespirationRate": 11.0,
        "maxTemperature": 36.0,
        "minTemperature": 22.0,
    })
    assert ride is not None

    assert upsert_activities(db, [ride]) == 1
    db.commit()

    f = db.scalars(
        select(Activity).where(Activity.garmin_activity_id == 4444)
    ).one()
    assert f.max_hr == 171.0
    assert (f.avg_speed_mps, f.max_speed_mps) == (6.94, 15.5)
    assert (f.elevation_gain_m, f.elevation_loss_m) == (900.0, 880.0)
    assert f.calories == 980.0
    assert (f.avg_respiration, f.max_respiration, f.min_respiration) == (22.0, 35.0, 11.0)
    assert (f.max_temp_c, f.min_temp_c) == (36.0, 22.0)


def test_el_estado_de_prueba_no_deja_ningun_campo_en_su_defecto(estado_lleno):
    """Guarda de `estado_lleno`: si un campo nuevo se quedase con su valor por
    defecto, la vuelta completa lo daría por bueno sin haberlo probado."""
    vacio = EngineState()
    iguales = [
        f.name
        for f in dataclass_fields(EngineState)
        if getattr(estado_lleno, f.name) == getattr(vacio, f.name)
    ]
    assert not iguales, (
        f"`estado_lleno` deja estos campos en su valor por defecto y por tanto "
        f"no los prueba: {iguales}"
    )


# ---------------------------------------------------------------------------
# La vuelta completa
# ---------------------------------------------------------------------------


def test_el_estado_sobrevive_a_la_ida_y_la_vuelta(db, estado_lleno):
    save_state(db, estado_lleno, day=LUNES)
    vuelto = load_state(db, program_start=estado_lleno.program_start)

    assert vuelto.clean_sessions == estado_lleno.clean_sessions
    assert vuelto.compliance == estado_lleno.compliance
    assert vuelto.current_sets == estado_lleno.current_sets, (
        "la carga vigente no ha sobrevivido: el ejercicio volvería al peso de "
        "partida de config.yaml"
    )
    assert vuelto.sessions_since_progress == estado_lleno.sessions_since_progress, (
        "el turno en la cola no ha sobrevivido: al reiniciar, todos los "
        "ejercicios volverían a empatar a cero y el cupo lo ganaría siempre el "
        "primero de la rutina"
    )
    assert vuelto.below_plan_streak == estado_lleno.below_plan_streak, (
        "la racha por debajo no ha sobrevivido: dos sesiones flojas seguidas se "
        "olvidarían al reiniciar y la carga no bajaría nunca aunque no se esté "
        "levantando"
    )
    assert vuelto.below_plan_best_kg == estado_lleno.below_plan_best_kg, (
        "el mejor peso de la racha no ha sobrevivido: al bajar se adoptaría el "
        "último en vez del mejor de la racha, que es más bajo"
    )
    assert vuelto.last_routine_light == estado_lleno.last_routine_light
    assert vuelto.program_start == estado_lleno.program_start
    assert vuelto.last_deload_start == estado_lleno.last_deload_start
    assert vuelto.deload_aplazada_desde == estado_lleno.deload_aplazada_desde, (
        "la descarga debida no ha sobrevivido al reinicio: se perdería sin ruido, "
        "porque lo que queda es justo lo que se veía antes de que la deuda "
        "existiera -ni descarga ni aviso-"
    )


def test_la_regla_especial_vuelve_entera_y_no_solo_su_nombre(db, estado_lleno):
    """Una regla sin su `action` está activa y no hace nada.

    Es el peor resultado posible de los tres: el mensaje sigue diciendo "peso
    muerto retirado (hasta el 20/09)" y el peso muerto está en la sesión.
    """
    save_state(db, estado_lleno, day=LUNES)
    regla = load_state(db).active_rules[0]
    original = estado_lleno.active_rules[0]

    assert regla.name == original.name
    assert regla.action == original.action, "la acción es lo que la regla HACE"
    assert regla.active_from == original.active_from
    assert regla.active_until == original.active_until
    assert regla.entity == original.entity
    assert regla.reason == original.reason
    assert regla.notify == original.notify


def test_guardar_dos_veces_no_duplica_ni_revienta(db, estado_lleno):
    """El segundo día. Las tablas tienen clave única, así que un insert ciego
    fallaría aquí y no el primer día, que es cuando se probaría a mano."""
    save_state(db, estado_lleno, day=LUNES)
    save_state(db, estado_lleno, day=LUNES + timedelta(days=1))

    assert len(db.query(ExerciseTarget).all()) == 2
    assert len(db.query(RuleState).all()) == 1
    assert load_state(db).clean_sessions == estado_lleno.clean_sessions


def test_una_racha_que_cambia_se_actualiza_en_vez_de_anadir_otra_fila(db, estado_lleno):
    save_state(db, estado_lleno, day=LUNES)
    estado_lleno.clean_sessions[("dia_1", "hip_thrust_barra")] = 3
    save_state(db, estado_lleno, day=LUNES + timedelta(days=1))

    assert load_state(db).clean_sessions[("dia_1", "hip_thrust_barra")] == 3
    assert len(db.query(ExerciseTarget).all()) == 2


# ---------------------------------------------------------------------------
# La racha por debajo: el caso en el que la AUSENCIA es el valor
# ---------------------------------------------------------------------------


def test_la_racha_por_debajo_que_se_anula_se_anula_tambien_en_la_tabla(db, estado_lleno):
    """El fallo que este patrón evita, y que no se parece a un fallo.

    Los demás campos se guardan "solo si están", porque escribir un cero por un
    ejercicio que no aparece sería inventar dato. Con la racha por debajo pasa lo
    contrario: anularla es BORRAR la clave del diccionario, así que con el patrón
    de "solo si está" la anulación no llegaría nunca a la tabla. El contador se
    quedaría clavado en 2 para siempre y la siguiente sesión floja, meses después,
    bajaría la carga como si fuera la tercera seguida.
    """
    save_state(db, estado_lleno, day=LUNES)
    assert load_state(db).below_plan_streak == {("dia_1", "hip_thrust_barra"): 2}

    # Una sesión buena rompe la racha: `adoptar_cargas` hace exactamente esto.
    estado_lleno.below_plan_streak.pop(("dia_1", "hip_thrust_barra"))
    estado_lleno.below_plan_best_kg.pop(("dia_1", "hip_thrust_barra"))
    save_state(db, estado_lleno, day=LUNES + timedelta(days=1))

    vuelto = load_state(db)
    assert vuelto.below_plan_streak == {}, (
        "la racha anulada ha sobrevivido en la tabla: la próxima sesión floja "
        "bajaría la carga creyendo que es la tercera seguida"
    )
    assert vuelto.below_plan_best_kg == {}


def test_un_cero_en_la_tabla_no_vuelve_como_racha_de_cero(db, estado_lleno):
    """"Sin racha" y "racha de cero" valen lo mismo para el motor, pero solo uno
    de los dos es lo que dice la tabla. Copiar la ausencia tal cual evita que un
    `save_state` posterior escriba filas de ceros para ejercicios que nunca se
    han quedado cortos."""
    save_state(db, estado_lleno, day=LUNES)
    vuelto = load_state(db)

    assert ("dia_2", "remo_t_apoyado") not in vuelto.below_plan_streak
    assert ("dia_2", "remo_t_apoyado") not in vuelto.below_plan_best_kg


def test_un_mejor_peso_de_cero_kilos_sobrevive(db, estado_lleno):
    """0 kg no es "no hay". Es un ejercicio hecho sin carga, y la columna es
    nullable justamente para poder distinguirlos: si `0.0` se guardara como NULL,
    la mejor sesión de la racha se perdería y al bajar se adoptaría otra cosa."""
    estado_lleno.below_plan_best_kg[("dia_1", "hip_thrust_barra")] = 0.0
    save_state(db, estado_lleno, day=LUNES)

    assert load_state(db).below_plan_best_kg == {("dia_1", "hip_thrust_barra"): 0.0}


# ---------------------------------------------------------------------------
# El libro de adopciones de carga
# ---------------------------------------------------------------------------


def adopcion(key: str = "hip_thrust_barra", *, aplicada: bool = True, **kw) -> dict:
    base = {
        "routine": "dia_1",
        "key": key,
        "direction": "up",
        "prescribed_kg": 60.0,
        "executed_kg": 65.0,
        "before_kg": 60.0,
        "after_kg": 65.0 if aplicada else None,
        "applied": aplicada,
        "reason": "se levantó eso de verdad",
    }
    base.update(kw)
    return base


def test_las_adopciones_se_guardan_con_los_cuatro_numeros(db):
    """La pregunta de dentro de tres meses -"¿por qué esto está en 65?"- se
    contesta con la fila entera o no se contesta."""
    assert guardar_adopciones(db, LUNES, [adopcion()]) == 1

    (f,) = db.query(LoadAdoption).all()
    assert (f.prescribed_kg, f.executed_kg, f.before_kg, f.after_kg) == (60, 65, 60, 65)
    assert f.direction == "up"
    assert f.applied is True
    assert f.date == LUNES
    assert f.reported_at is None, "recién guardada no la ha contado ningún mensaje"


def test_una_adopcion_rechazada_tambien_se_guarda(db):
    """Un tope que actúa sin dejar rastro es un tope que nadie puede corregir."""
    guardar_adopciones(db, LUNES, [adopcion(aplicada=False, executed_kg=600.0)])

    (f,) = db.query(LoadAdoption).all()
    assert f.applied is False
    assert f.after_kg is None
    assert f.executed_kg == 600


def test_las_adopciones_se_acumulan_en_vez_de_sustituirse(db):
    """Es un libro, no un estado. Dos noches distintas son dos filas: si la
    segunda pisara a la primera, el histórico de por qué la carga es la que es se
    perdería entero."""
    guardar_adopciones(db, LUNES, [adopcion()])
    guardar_adopciones(db, LUNES + timedelta(days=2), [adopcion()])

    assert len(db.query(LoadAdoption).all()) == 2


def test_solo_salen_las_que_no_ha_contado_nadie(db):
    guardar_adopciones(db, LUNES, [adopcion("a"), adopcion("b")])
    pendientes = adopciones_sin_contar(db)
    assert {p["key"] for p in pendientes} == {"a", "b"}

    marcar_adopciones_contadas(db, [p["id"] for p in pendientes])
    assert adopciones_sin_contar(db) == []


def test_las_viejas_no_caducan(db):
    """Si el PC estuvo tres días apagado, esas subidas siguen sin explicarse.

    Un límite de antigüedad dejaría un cambio de carga sin motivo visible, que es
    justo lo que esta tabla existe para impedir.
    """
    guardar_adopciones(db, LUNES - timedelta(days=40), [adopcion()])
    assert len(adopciones_sin_contar(db)) == 1


def test_si_telegram_falla_la_adopcion_se_cuenta_al_dia_siguiente(db):
    """Por esto leer y marcar están separados.

    `_mandar_telegram` se traga los fallos de envío para que un Telegram caído no
    tumbe la mañana, así que la transacción se confirma igual. Si marcar fuera
    parte de leer, la adopción quedaría sellada como contada por un mensaje que
    nunca llegó al móvil y el cambio de carga se quedaría sin explicar para
    siempre.
    """
    guardar_adopciones(db, LUNES, [adopcion()])

    # Mañana 1: se lee para el mensaje... y el envío falla, así que no se marca.
    assert len(adopciones_sin_contar(db)) == 1

    # Mañana 2: sigue pendiente y esta vez sí se cuenta.
    pendientes = adopciones_sin_contar(db)
    assert len(pendientes) == 1
    assert marcar_adopciones_contadas(db, [p["id"] for p in pendientes]) == 1
    assert adopciones_sin_contar(db) == []


def test_marcar_sin_ids_no_marca_nada(db):
    """Una lista vacía o llena de `None` no puede acabar sellando la tabla
    entera con un `IN ()` mal construido."""
    guardar_adopciones(db, LUNES, [adopcion()])

    assert marcar_adopciones_contadas(db, []) == 0
    assert marcar_adopciones_contadas(db, [None, None]) == 0
    assert len(adopciones_sin_contar(db)) == 1


def test_las_pendientes_salen_en_orden_cronologico(db):
    """El mensaje que explica tres días de golpe tiene que leerse en el orden en
    que pasaron las cosas."""
    guardar_adopciones(db, LUNES + timedelta(days=1), [adopcion("martes")])
    guardar_adopciones(db, LUNES, [adopcion("lunes")])

    assert [p["key"] for p in adopciones_sin_contar(db)] == ["lunes", "martes"]


def test_guardar_una_lista_vacia_no_toca_nada(db):
    assert guardar_adopciones(db, LUNES, []) == 0
    assert guardar_adopciones(db, LUNES, None) == 0
    assert db.query(LoadAdoption).all() == []


# ---------------------------------------------------------------------------
# Reglas caducadas
# ---------------------------------------------------------------------------


def test_una_regla_que_ya_no_esta_en_el_estado_desaparece_de_la_tabla(db, estado_lleno):
    """`advance_state` devuelve las que SIGUEN vigentes. Si al guardar solo se
    añadiera, la caducada seguiría en la tabla y volvería a cargarse cada
    mañana: retirada a perpetuidad y sin motivo a la vista."""
    save_state(db, estado_lleno, day=LUNES)
    estado_lleno.active_rules = []
    save_state(db, estado_lleno, day=LUNES + timedelta(days=20))

    assert db.query(RuleState).all() == []
    assert load_state(db).active_rules == []


# ---------------------------------------------------------------------------
# El puntero de la rotación
# ---------------------------------------------------------------------------
#
# Aquí vivían los tres tests de la sesión aplazada. Ya no hay nada que aplazar:
# el puntero no lo escribe nadie, se lee de las sesiones que se han HECHO. Lo
# que sigue prueba que lo lee de donde tiene que leerlo, porque el fallo que
# sustituye a "se perdió el aplazamiento" es más callado: un puntero que salga
# de lo PLANIFICADO avanza los días que no piso el gimnasio, y al tercer día
# libre me habría saltado el Día 2 sin que nada lo dijera.


def _log(db, dia: date, routine_key: str | None, *, unplanned: bool = False) -> None:
    """Una fila de `workout_log` tal y como la escribe la reconciliación.

    El título va a propósito con un texto que no nombra ninguna rutina: los
    títulos no son dato, y si algún día el puntero empezara a mirarlos, estos
    tests tendrían que caerse.
    """
    db.add(
        WorkoutLog(
            date=dia,
            hevy_workout_id=f"w-{dia.isoformat()}-{routine_key}",
            routine_key=routine_key,
            title="lo que diga Hevy",
            unplanned=unplanned,
        )
    )
    db.flush()


def test_el_puntero_sale_de_la_ultima_sesion_ejecutada(db):
    _log(db, LUNES - timedelta(days=6), "dia_1")
    _log(db, LUNES - timedelta(days=3), "dia_2")

    estado = load_state(db, rotation_order=["dia_1", "dia_2", "dia_3"])
    assert estado.last_strength == ("dia_2", LUNES - timedelta(days=3))


def test_sin_el_ciclo_delante_el_puntero_no_se_inventa(db):
    """`load_state` sin `rotation_order` no adivina.

    Es la llamada que hacen los tests y algún script suelto. Que devuelva None
    en vez de coger la última fila de `workout_log` sea cual sea importa: un
    HIIT o una sesión suelta no son escalones del ciclo, y tomarlos por el
    puntero adelantaría la rotación una posición por cada uno.
    """
    _log(db, LUNES - timedelta(days=1), "dia_2")
    assert load_state(db).last_strength is None


def test_lo_que_no_esta_en_el_ciclo_no_mueve_el_puntero(db):
    """Un HIIT, un bloque de recuperación o una sesión sin rutina reconocida
    quedan en `workout_log` igual que todo lo demás. Ninguno es un escalón."""
    _log(db, LUNES - timedelta(days=5), "dia_1")
    _log(db, LUNES - timedelta(days=2), "hiit_bici")
    _log(db, LUNES - timedelta(days=1), None, unplanned=True)

    estado = load_state(db, rotation_order=["dia_1", "dia_2", "dia_3"])
    assert estado.last_strength == ("dia_1", LUNES - timedelta(days=5)), (
        "el puntero ha saltado a algo que no es del ciclo: la próxima sesión "
        "de fuerza se habría saltado un día de la rotación"
    )


def test_una_sesion_del_ciclo_hecha_el_dia_que_no_tocaba_tambien_cuenta(db):
    """`unplanned` no descalifica.

    Este es el filtro que NO se puso, y a propósito: el Día 3 se estuvo
    registrando como suelto durante meses porque el calendario no lo nombraba.
    Si se hace el Día 3 un día cualquiera, se ha hecho el Día 3.
    """
    _log(db, LUNES - timedelta(days=4), "dia_2")
    _log(db, LUNES - timedelta(days=1), "dia_3", unplanned=True)

    estado = load_state(db, rotation_order=["dia_1", "dia_2", "dia_3"])
    assert estado.last_strength == ("dia_3", LUNES - timedelta(days=1))


def test_dos_sesiones_el_mismo_dia_desempatan_por_la_ultima_escrita(db):
    """Empate de fechas. Sin el desempate por `id`, SQLite devuelve lo que le
    apetezca y el puntero sería distinto en cada arranque con los mismos datos."""
    _log(db, LUNES, "dia_1")
    _log(db, LUNES, "dia_2")

    estado = load_state(db, rotation_order=["dia_1", "dia_2", "dia_3"])
    assert estado.last_strength == ("dia_2", LUNES)


def test_el_puntero_no_lo_escribe_save_state(db, estado_lleno):
    """La otra mitad de la regla: que no haya DOS sitios donde vive el puntero.

    Si `save_state` lo guardara además de leerlo de `workout_log`, volvería el
    patrón que ha costado todos los fallos del proyecto: dos valores para lo
    mismo y ninguna forma de saber cuál manda.
    """
    save_state(db, estado_lleno, day=LUNES)
    assert load_state(db, rotation_order=["dia_1", "dia_2", "dia_3"]).last_strength is None


# ---------------------------------------------------------------------------
# Base de datos vacía: el primer día
# ---------------------------------------------------------------------------


def test_una_base_vacia_da_un_estado_limpio_y_no_un_error(db):
    """El primer arranque. Nada que recuperar no es un fallo."""
    estado = load_state(db, program_start=LUNES)
    assert estado.clean_sessions == {}
    assert estado.active_rules == []
    assert estado.last_strength is None
    assert estado.last_deload_start is None
    assert estado.program_start == LUNES, "esto sí viene, pero del config"


def test_un_ejercicio_sin_estrenar_no_se_guarda_ni_como_si_ni_como_no(db):
    """La ausencia se conserva como ausencia hasta arriba.

    Guardar un False de relleno acusaría de un incumplimiento inventado;
    guardar un True abriría la progresión sobre una sesión que no ha existido.
    Lo correcto es que no haya fila y que el motor reciba `None`.
    """
    estado = EngineState(clean_sessions={("dia_1", "sentadilla"): 0})
    save_state(db, estado, day=LUNES)

    vuelto = load_state(db)
    assert ("dia_1", "sentadilla") not in vuelto.compliance
    comp, _ = vuelto.for_routine("dia_1", ["sentadilla"])
    assert comp["sentadilla"] is None, (
        "un True aquí es el fallo silencioso: convierte 'no tengo registro' "
        "en 'la última sesión fue perfecta' y abre la puerta de la carga"
    )


# ---------------------------------------------------------------------------
# `program_start` no se guarda a propósito
# ---------------------------------------------------------------------------


def test_el_inicio_del_programa_manda_el_config_y_no_la_base_de_datos(db, estado_lleno):
    """Si se guardara, el día que el usuario corrija la fecha en el YAML la
    base seguiría con la vieja y no habría forma de saber cuál manda."""
    save_state(db, estado_lleno, day=LUNES)
    otra = date(2025, 1, 6)
    assert load_state(db, program_start=otra).program_start == otra


# ---------------------------------------------------------------------------
# Representación legible
# ---------------------------------------------------------------------------


def test_el_estado_legible_no_pierde_las_claves_compuestas(estado_lleno):
    d = state_as_dict(estado_lleno)
    assert d["clean_sessions"]["dia_1/hip_thrust_barra"] == 2
    assert d["last_strength"]["routine"] == "dia_3"
    assert d["last_deload_start"] == estado_lleno.last_deload_start.isoformat()
    assert (
        d["deload_aplazada_desde"]
        == estado_lleno.deload_aplazada_desde.isoformat()
    )


# ---------------------------------------------------------------------------
# El bucle de verdad, pasando por disco cada día
# ---------------------------------------------------------------------------


def _simula(db, cfg, dias: int, arranque=LUNES):
    """Corre `dias` días haciendo `load_state` y `save_state` en CADA uno.

    Releer el estado del disco todos los días es el punto: imita el proceso que
    se reinicia, que en un Umbrel pasa cada vez que se actualiza el contenedor.
    Una vuelta completa que funciona en memoria puede seguir perdiéndolo todo
    aquí si el bucle se olvida de guardar.
    """
    decisiones = []
    for i in range(dias):
        dia = arranque + timedelta(days=i)
        estado = load_state(db, program_start=cfg.program_start)
        d = decide(cfg, dia, sig_completa(dia), estado)
        # Sesión ejecutada y limpia: es lo que alimenta la racha.
        ejecutado = {ex["key"]: True for ex in d.session.exercises if ex.get("key")}
        save_state(db, advance_state(estado, d, executed=ejecutado), day=dia)
        decisiones.append(d)
    return decisiones


def test_la_racha_se_acumula_entre_reinicios_y_el_programa_progresa(db, cfg):
    """La prueba de que esto sirve para algo.

    Si el estado no sobreviviera al disco, la racha volvería a cero cada día,
    nunca llegaría a `clean_sessions_required` y NADA subiría jamás. El sistema
    seguiría mandando su mensaje cada mañana, con los mismos pesos para
    siempre, sin un solo error en el log.
    """
    decisiones = _simula(db, cfg, dias=21)

    # Se mira la subida de CARGA, no cualquier cambio. El volumen sube también
    # sin memoria -no necesita racha-, así que un `assert subidas` a secas
    # pasaría con la base de datos desconectada y no probaría nada. La carga es
    # lo único que exige `clean_sessions_required` sesiones limpias seguidas, y
    # por tanto lo único que demuestra que el estado sobrevive al disco.
    cargas = [
        e
        for d in decisiones
        for e in (d.progression.changes if d.progression else [])
        if e.kind == "load"
    ]
    assert cargas, "en tres semanas no ha subido ninguna carga: el estado no se recuerda"

    rachas = load_state(db).clean_sessions
    assert any(v > 0 for v in rachas.values()), f"todas las rachas a cero: {rachas}"


def test_sin_guardar_el_estado_no_se_mueve_absolutamente_nada(db, cfg):
    """El contraste que le da valor al test de arriba.

    Mismo bucle, tirando el estado cada día.

    Este test comprobaba antes algo más flojo: que sin memoria no subiera la
    CARGA, dando por bueno que el volumen sí subiera. Y subía: 12 anuncios de
    subida en tres semanas, repitiendo los mismos -`gemelo_sentado` 12→13 el
    día 7 y otra vez el día 14-, que es la firma exacta de la amnesia. Anunciar
    para siempre, avanzar nunca.

    El motivo era `for_routine`, que convertía "no tengo ni un registro de este
    ejercicio" en "la última sesión fue perfecta". La puerta general se abría
    con eso, y la puerta gobierna también el volumen. Un contenedor que
    perdiera la base de datos habría seguido mandando su mensaje cada mañana,
    con subidas inventadas, sin un solo error en el log.

    Ahora la ausencia de registro cierra la puerta: sin memoria no se mueve
    nada.
    """
    decisiones = []
    for i in range(21):
        dia = LUNES + timedelta(days=i)
        decisiones.append(
            decide(cfg, dia, sig_completa(dia), EngineState(program_start=cfg.program_start))
        )

    subidas = [
        e for d in decisiones for e in (d.progression.changes if d.progression else [])
    ]
    assert not subidas, f"sin memoria no debería moverse nada, y se movió: {subidas}"

    # Y que no se mueva por el motivo correcto, no porque el escenario se haya
    # quedado por el camino sin una sola sesión de fuerza. Sin esta parte el
    # test de arriba pasaría aunque `decide` devolviera siempre None.
    con_rutina = [d.progression for d in decisiones if d.progression]
    assert con_rutina, "el escenario ya no tiene ni una sesión de fuerza; rehazlo"
    assert not any(p.gate_open for p in con_rutina)
    motivos = {p.gate_reason for p in con_rutina}
    assert any("primera vez que el sistema ve" in m for m in motivos), motivos
    assert all(p.estreno for p in con_rutina), (
        "sin estado guardado toda rutina se estrena todos los días, que es "
        "precisamente la amnesia que este test vigila"
    )


# ---------------------------------------------------------------------------
# Check-in
# ---------------------------------------------------------------------------


def test_el_checkin_se_guarda_y_se_lee(db, cfg):
    upsert_checkin(db, LUNES, {"fatigue": 4, "lower_discomfort": 2}, config=cfg)
    assert checkin_values(get_checkin(db, LUNES)) == {
        "fatigue": 4,
        "lower_discomfort": 2,
    }


def test_reenviar_el_formulario_corrige_en_vez_de_duplicar(db, cfg):
    """Uno se equivoca de deslizador y lo vuelve a mandar. Vale el último."""
    upsert_checkin(db, LUNES, {"fatigue": 9}, config=cfg)
    upsert_checkin(db, LUNES, {"fatigue": 3}, config=cfg)
    assert checkin_values(get_checkin(db, LUNES))["fatigue"] == 3


def test_un_deslizador_sin_contestar_no_existe_en_vez_de_valer_cero(db, cfg):
    """Para las reglas no es lo mismo "molestia 0" que "no lo he contestado":
    lo primero es un dato bueno, lo segundo hace que la regla no se evalúe."""
    upsert_checkin(db, LUNES, {"fatigue": 4}, config=cfg)
    valores = checkin_values(get_checkin(db, LUNES))
    assert "lower_discomfort" not in valores
    assert valores["fatigue"] == 4


def test_un_cero_de_verdad_si_se_guarda(db, cfg):
    """La otra cara: un 0 contestado es un dato y tiene que llegar."""
    upsert_checkin(db, LUNES, {"lower_discomfort": 0}, config=cfg)
    assert checkin_values(get_checkin(db, LUNES))["lower_discomfort"] == 0


# --- el histórico, que hasta ahora no leía nadie ---------------------------
#
# `build_signals` acepta `checkin_history=` desde el primer día y ningún caller
# de producción se lo pasaba, igual que pasó con `sessions`. La diferencia es
# que este no había explotado todavía: los dos umbrales adaptativos del config
# miran carga derivada de las salidas. En cuanto haya uno sobre la lumbar o el
# cansancio -octubre-, sin esta función la serie sería de un punto y el
# percentil se calcularía contra sí mismo.


def test_el_historial_de_checkins_sale_en_orden_y_como_checkin_del_motor(db, cfg):
    from app.repository import historial_checkins

    for i in range(5):
        upsert_checkin(
            db, LUNES - timedelta(days=i), {"fatigue": i + 1}, config=cfg
        )

    hist = historial_checkins(db, desde=LUNES - timedelta(days=4), hasta=LUNES)
    assert [c.date for c in hist] == [
        LUNES - timedelta(days=i) for i in (4, 3, 2, 1, 0)
    ]
    assert hist[0].values["fatigue"] == 5
    assert hist[-1].values["fatigue"] == 1


def test_la_ventana_del_historial_no_se_pasa_por_los_extremos(db, cfg):
    """Un percentil sobre días de fuera de la ventana no es el de la ventana."""
    from app.repository import historial_checkins

    for i in range(10):
        upsert_checkin(db, LUNES - timedelta(days=i), {"fatigue": 3}, config=cfg)

    hist = historial_checkins(
        db, desde=LUNES - timedelta(days=3), hasta=LUNES - timedelta(days=1)
    )
    assert len(hist) == 3
    assert LUNES not in {c.date for c in hist}


def test_un_deslizador_sin_contestar_no_entra_en_la_serie_como_cero(db, cfg):
    """La diferencia entre «no contesté» y «contesté el mínimo».

    Para un percentil las dos cosas se leerían igual y una de ellas es falsa. Lo
    quita `checkin_values`, y esto lo fija para que siga siendo así cuando
    alguien decida «rellenar los huecos» de la serie.
    """
    from app.repository import historial_checkins

    upsert_checkin(db, LUNES, {"fatigue": 4}, config=cfg)
    (c,) = historial_checkins(db, desde=LUNES, hasta=LUNES)
    assert c.values["fatigue"] == 4
    assert "lower_discomfort" not in c.values


def test_sin_checkins_el_historial_es_una_lista_vacia_y_no_un_fallo(db, cfg):
    """Los primeros días del sistema son exactamente este caso."""
    from app.repository import historial_checkins

    assert historial_checkins(db, desde=LUNES - timedelta(days=30), hasta=LUNES) == []


def test_un_deslizador_mal_escrito_es_un_error_y_no_un_campo_ignorado(db, cfg):
    """`fatiga` por `fatigue` desde la PWA se guardaría en ninguna parte y el
    sistema decidiría sin ese dato creyendo el check-in completo."""
    with pytest.raises(ValueError, match="fatiga"):
        upsert_checkin(db, LUNES, {"fatiga": 4}, config=cfg)


def test_una_columna_real_que_no_es_deslizador_tambien_se_rechaza(db, cfg):
    """`comments` es columna, pero no un deslizador: va por su parámetro."""
    with pytest.raises(ValueError, match="checkin_sliders"):
        upsert_checkin(db, LUNES, {"comments": "hola"}, config=cfg)


def test_los_deslizadores_validos_salen_del_yaml(cfg):
    """Los siete del config, incluido el `yesterday_rpe` que se añadió después
    del diseño inicial. Una lista a mano se habría quedado en seis."""
    claves = sliders_del_config(cfg)
    assert "yesterday_rpe" in claves
    assert len(claves) == 7


# ---------------------------------------------------------------------------
# El selector de sesión
# ---------------------------------------------------------------------------
#
# La tercera categoría de respuesta. Se guarda por el mismo sitio que las otras
# dos -es una columna de `checkins` como ellas- y se diferencia en dos cosas:
# es una cadena, y a las cadenas sí hay que mirarles el valor.


def test_el_selector_se_guarda_como_cualquier_otra_respuesta(db, cfg):
    upsert_checkin(db, LUNES, {"fatigue": 4, "chosen_session": "dia_2"}, config=cfg)
    fila = get_checkin(db, LUNES)
    assert fila.chosen_session == "dia_2"


def test_bici_y_otro_valen_igual_que_una_rutina_del_ciclo(db, cfg):
    """Las dos existen para que «hice algo» no se cuente como «no contesté»."""
    for eleccion in ("bici", "otro"):
        upsert_checkin(db, LUNES, {"chosen_session": eleccion}, config=cfg)
        assert get_checkin(db, LUNES).chosen_session == eleccion


def test_una_eleccion_que_no_existe_es_un_error_y_no_una_fila_muda(db, cfg):
    """El `fatiga` por `fatigue` un nivel más abajo: la clave está bien y miente
    el contenido.

    Y es peor que el de la clave, porque este no se ve. Un `dia_4` guardado en un
    ciclo de tres no coincide con ninguna rutina, con ninguna propuesta y con
    ningún `workout_log`: el día queda escrito y se comporta exactamente igual
    que si no hubieras abierto el formulario.
    """
    with pytest.raises(ValueError, match="dia_4"):
        upsert_checkin(db, LUNES, {"chosen_session": "dia_4"}, config=cfg)


def test_la_eleccion_distingue_mayusculas(db, cfg):
    """«Bici» no es `bici`, y dejarlo pasar sería inventar un cuarto estado."""
    with pytest.raises(ValueError, match="Bici"):
        upsert_checkin(db, LUNES, {"chosen_session": "Bici"}, config=cfg)


def test_no_contestar_el_selector_sigue_siendo_valido(db, cfg):
    """El tercer estado de siempre: la mayoría de los días no se toca."""
    upsert_checkin(db, LUNES, {"fatigue": 4}, config=cfg)
    assert get_checkin(db, LUNES).chosen_session is None


def test_las_opciones_del_selector_salen_del_ciclo_y_no_de_una_lista_aparte(cfg):
    """Si se enumeraran a mano, un `dia_4` nuevo no se podría elegir.

    Es el fallo que ya se pagó una vez con el calendario fijo: el fichero estaba
    impecable y sencillamente no nombraba `dia_3`.
    """
    assert opciones_del_config(cfg) == set(cfg.rotation_order()) | {"bici", "otro"}


def test_el_ciclo_manda_tambien_al_guardar_y_no_solo_al_dibujar(db, cfg_copia):
    """El de arriba no basta, y la diferencia no es académica.

    Con el config real el ciclo es `dia_1, dia_2, dia_3` y cualquier lista
    escrita a mano diría exactamente lo mismo: el test anterior la dejaría
    pasar. Este mueve el ciclo -añade un día y quita otro- sin tocar la sección
    del selector, que es la situación en la que las dos versiones se separan.

    Lo que protege es que la lista que VALIDA sea la lista que OFRECE. Si se
    separan, el formulario enseña un día que el guardado rechaza, o al revés; y
    del segundo caso no se entera nadie hasta que un check-in real se guarda
    apuntando a una rutina que ya no está en la rotación.
    """
    cfg_copia.raw["rotation"]["order"].append("dia_4")
    assert "dia_4" in opciones_del_config(cfg_copia)
    upsert_checkin(db, LUNES, {"chosen_session": "dia_4"}, config=cfg_copia)
    assert get_checkin(db, LUNES).chosen_session == "dia_4"

    # Y al revés: lo que sale del ciclo deja de poderse elegir el mismo día.
    cfg_copia.raw["rotation"]["order"].remove("dia_3")
    with pytest.raises(ValueError, match="dia_3"):
        upsert_checkin(db, LUNES, {"chosen_session": "dia_3"}, config=cfg_copia)


def test_el_selector_no_es_un_deslizador_ni_una_pregunta(cfg):
    """Y por eso no llega a `signals.values`, que es lo que lo hace seguro.

    `checkin_keys()` es la lista que `build_signals` vuelca en el espacio de
    nombres de las reglas y la que recorre el análisis sacando series. Todo lo
    que hay ahí es número o booleano. Meter aquí una cadena no daría un color
    raro: daría un `float('dia_2')` la primera vez que alguien promedie el
    histórico entero.
    """
    assert "chosen_session" not in cfg.checkin_keys()
    assert "chosen_session" not in sliders_del_config(cfg)
    assert "chosen_session" not in preguntas_del_config(cfg)


def test_lo_elegido_viaja_en_los_valores_del_checkin(db, cfg):
    """`checkin_values` vuelca la fila entera, y de ahí lo recoge `build_signals`.

    Aquí sí tiene que estar: es el camino por el que llega al motor. Lo que no
    puede es seguir desde `values` del check-in hasta `values` de las señales, y
    de eso se encarga `build_signals` copiando solo las claves que conoce.
    """
    upsert_checkin(db, LUNES, {"chosen_session": "dia_3"}, config=cfg)
    assert checkin_values(get_checkin(db, LUNES))["chosen_session"] == "dia_3"


def test_los_comentarios_se_guardan_aparte(db, cfg):
    upsert_checkin(db, LUNES, {"fatigue": 4}, config=cfg, comments="lumbar rara")
    fila = get_checkin(db, LUNES)
    assert fila.comments == "lumbar rara"
    assert "comments" not in checkin_values(fila), "no es una señal para las reglas"


# ---------------------------------------------------------------------------
# Decisiones
# ---------------------------------------------------------------------------


def test_la_decision_se_guarda_con_lo_que_hace_falta_para_reproducirla(db, cfg):
    d = decide(cfg, LUNES, sig_completa(LUNES), EngineState())
    fila = save_decision(db, d)

    assert fila.light == d.light
    assert fila.is_current is True
    assert fila.config_hash == d.config_hash, "sin esto no se puede reproducir"
    assert fila.inputs_snapshot_json, "ni sin la foto de las señales"


def test_decidir_dos_veces_el_mismo_dia_deja_rastro_de_la_primera(db, cfg):
    """A las 07:00 sin check-in y a las 09:40 con él. La primera no se pisa:
    se marca. Es lo que hace depurable un semáforo que cambió a media mañana."""
    primera = decide(cfg, LUNES, sig_completa(LUNES), EngineState())
    save_decision(db, primera)

    segunda = decide(cfg, LUNES, sig_completa(LUNES, lower_discomfort=7), EngineState())
    save_decision(db, segunda)

    todas = db.query(DecisionRow).filter(DecisionRow.date == LUNES).all()
    assert len(todas) == 2, "append-only: la primera no se borra"
    assert sum(1 for f in todas if f.is_current) == 1
    assert current_decision(db, LUNES).light == segunda.light
    assert segunda.light == "red", "el escenario ya no cambia el semáforo; rehacer"


def test_dos_decisiones_que_solo_se_diferencian_en_lo_elegido_se_distinguen(db, cfg):
    """El caso real de arriba, pero con la entrada que no viajaba en la foto.

    A las 07:00 no hay formulario y se planifica la propuesta del ciclo. A las
    09:40 llega el check-in diciendo `dia_3` y se planifica otra rutina. Las dos
    filas quedan guardadas, que es lo que promete `append-only`, pero hasta
    ahora quedaban con el MISMO `inputs_snapshot_json`: mismas señales, misma
    configuración, distinta rutina planificada y nada que lo explicase.

    `checkins.chosen_session` no tapa el hueco. Guarda una fila por día con la
    respuesta final, así que leída después dice `dia_3` para las dos
    decisiones, incluida la que se tomó cuando todavía no se había contestado.

    El test compara las fotos ENTERAS, no solo la clave nueva: lo que hay que
    poder afirmar es que las dos filas son distinguibles, no que existe un
    campo.
    """
    a_las_siete = decide(cfg, LUNES, sig_completa(LUNES), EngineState())
    fila_siete = save_decision(db, a_las_siete)

    con_formulario = sig_completa(LUNES)
    con_formulario.sesion_elegida = "dia_3"
    a_las_nueve = decide(cfg, LUNES, con_formulario, EngineState())
    fila_nueve = save_decision(db, a_las_nueve)

    assert a_las_siete.rotation_routine != "dia_3", (
        "el escenario no discrimina: la propuesta del ciclo ya era la elegida"
    )
    assert a_las_nueve.rotation_routine == "dia_3", "la elección manda para hoy"

    assert fila_siete.inputs_snapshot_json != fila_nueve.inputs_snapshot_json, (
        "dos decisiones con entradas distintas no pueden guardar la misma foto"
    )
    assert json.loads(fila_siete.inputs_snapshot_json)["sesion_elegida"] is None
    assert json.loads(fila_nueve.inputs_snapshot_json)["sesion_elegida"] == "dia_3"


# ---------------------------------------------------------------------------
# Previsualizaciones
# ---------------------------------------------------------------------------


RESPUESTAS = {"fatigue": 4, "lower_discomfort": 2, "chosen_session": "dia_1"}


def _previsualizar(db, cfg, dia=LUNES, **kwargs):
    """Una previsualización de un día tranquilo, sin nada anulado."""
    d = decide(cfg, dia, sig_completa(dia), EngineState())
    return save_preview(db, d, answers=dict(RESPUESTAS), **kwargs)


def test_previsualizar_no_escribe_en_checkins(db, cfg):
    """La razón de que exista la tabla, y lo primero que hay que poder afirmar.

    Si las respuestas tentativas llegaran a `checkins`, entrarían en el
    histórico del que salen los percentiles de los umbrales adaptativos. Cuatro
    previsualizaciones moviendo el deslizador de fatiga serían cuatro lecturas
    de fatiga en la ventana de 60 días, y el sistema calibraría sus umbrales
    sobre respuestas que nunca se dieron.
    """
    _previsualizar(db, cfg)
    _previsualizar(db, cfg)

    assert get_checkin(db, LUNES) is None, "una previsualización no es una respuesta"
    assert db.query(DecisionRow).count() == 0, "ni una decisión guardada"


def test_la_segunda_previsualizacion_no_tapa_a_la_primera(db, cfg):
    """«No quiero que la segunda tape a la primera: la diferencia es el dato.»"""
    primera = _previsualizar(db, cfg)
    segunda = _previsualizar(db, cfg)

    filas = previews_del_dia(db, LUNES)
    assert len(filas) == 2
    assert [f.seq for f in filas] == [1, 2], "y en el orden en que se pidieron"
    assert primera.id != segunda.id
    assert json.loads(filas[0].answers_json) == RESPUESTAS, (
        "cada una con las respuestas que la generaron, no con las últimas"
    )


def test_el_contador_de_revisiones_es_por_dia(db, cfg):
    """Empieza de nuevo cada mañana: `seq` dice "la segunda de HOY"."""
    _previsualizar(db, cfg, dia=LUNES)
    _previsualizar(db, cfg, dia=LUNES)
    manana = _previsualizar(db, cfg, dia=LUNES + timedelta(days=1))

    assert manana.seq == 1, "un día nuevo no hereda las revisiones del anterior"


def test_el_desacuerdo_tiene_tres_estados_y_el_tercero_es_no_haber_dicho(db, cfg):
    """`NULL` no es `False`.

    Casi todas las previsualizaciones se van a mirar sin opinar. Si eso contara
    como acuerdo explícito, la medida de "cuántas veces discrepo" se dividiría
    entre un denominador lleno de conformidades que nadie dio.
    """
    callado = _previsualizar(db, cfg)
    conforme = _previsualizar(db, cfg, disagreed=False)
    discrepa = _previsualizar(db, cfg, disagreed=True, disagreement_reason="me sobra")

    assert callado.disagreed is None
    assert conforme.disagreed is False
    assert discrepa.disagreed is True
    assert discrepa.disagreement_reason == "me sobra"


def test_discrepar_no_toca_las_respuestas(db, cfg):
    """«Sin tocar respuestas» es literal: es otra columna, no otra previsualización.

    Es la diferencia entre las dos formas de no estar de acuerdo que el sistema
    tiene que poder distinguir. Cambiar una respuesta y volver a previsualizar
    deja DOS filas con respuestas distintas. Decir "no lo comparto" deja UNA
    fila con las mismas respuestas y una marca. Si discrepar reescribiera las
    respuestas, las dos cosas quedarían idénticas en la tabla y la medida de
    "¿estoy calibrando o estoy forzando el resultado?" se quedaría sin poder
    contestarse.
    """
    fila = _previsualizar(db, cfg, disagreed=True, disagreement_reason="hoy puedo más")

    assert json.loads(fila.answers_json) == RESPUESTAS
    assert len(previews_del_dia(db, LUNES)) == 1


def test_la_anulacion_se_saca_de_la_decision_y_no_se_pide_aparte(db, cfg):
    """Un solo sitio donde mirar qué se anuló.

    La anulación ya viaja dentro de la sesión construida. Pedírsela además al
    que llama abriría la puerta a que la fila dijera una cosa y la decisión
    guardada otra.
    """
    d = decide(
        cfg,
        LUNES,
        sig_completa(LUNES, lower_discomfort=7),
        EngineState(),
        sesion_pedida=SesionPedida(tipo="full", confirmada=True, motivo="hoy sí"),
    )
    assert d.light == "red", "el escenario ya no es un día rojo; rehacer"

    fila = save_preview(db, d, answers=dict(RESPUESTAS))

    assert fila.override_session_type == "full"
    assert fila.forced_on_red is True, "subir en rojo se marca aparte"
    assert fila.light == "red"
    assert fila.session_type == "full", "lo que saldría, no lo que se proponía"


def test_una_anulacion_que_no_sube_no_se_marca_como_forzada(db, cfg):
    """Anular y forzar no son lo mismo, y la columna tiene que separarlos.

    Con el semáforo en verde, pedir recuperación es la anulación más prudente
    que existe: se baja de dureza por voluntad propia. Hay anulación -y tiene
    que constar- pero no hay nada forzado.

    Sin este caso, `forced_on_red` podría estar copiando "¿hay anulación?" en
    vez de "¿se subió en rojo?" y los dos tests de al lado seguirían pasando:
    uno no tiene anulación y el otro la tiene forzada. El que discrimina es
    este.
    """
    d = decide(
        cfg,
        LUNES,
        sig_completa(LUNES),
        EngineState(),
        sesion_pedida=SesionPedida(tipo="recovery", motivo="lumbar rara"),
    )
    assert d.light == "green", "el escenario ya no es un día verde; rehacer"

    fila = save_preview(db, d, answers=dict(RESPUESTAS))

    assert fila.override_session_type == "recovery", "la anulación consta"
    assert fila.forced_on_red is False, "pero no se forzó nada"


def test_enlazar_una_preview_que_no_existe_revienta(db, cfg):
    """Un enlace perdido no se nota al guardar: se nota al medir, meses después.

    La medida de "¿cuántas anulaciones acabé ejecutando?" sale de este enlace.
    Si `enlazar_preview` se tragara un id que no existe, el número saldría bajo
    -pareciendo que se anula mucho y se ejecuta poco- sin que nada indicase que
    lo que falla es el registro y no la conducta.
    """
    with pytest.raises(ValueError, match="no hay previsualización"):
        enlazar_preview(db, 9999, 1)


def test_un_dia_sin_anular_nada_no_se_marca_como_forzado(db, cfg):
    fila = _previsualizar(db, cfg)

    assert fila.override_session_type is None
    assert fila.override_routine is None
    assert fila.forced_on_red is False


def test_previsualizar_sin_enviar_queda_sin_decision(db, cfg):
    """Mirar qué saldría y no enviar es un final legítimo, no un registro a medias."""
    fila = _previsualizar(db, cfg)
    assert fila.decision_id is None


def test_enviar_despues_de_previsualizar_las_enlaza(db, cfg):
    """Así se contesta "¿se ejecutó la anulación?" con un JOIN.

    Y no con un booleano en la previsualización que habría que mantener al día
    y que podría acabar diciendo que sí mientras `decisions` dice que no.
    """
    d = decide(cfg, LUNES, sig_completa(LUNES), EngineState())
    fila = save_preview(db, d, answers=dict(RESPUESTAS))
    decidida = save_decision(db, d)

    enlazar_preview(db, fila.id, decidida.id)

    assert previews_del_dia(db, LUNES)[0].decision_id == decidida.id


def test_la_segunda_decision_del_dia_no_se_lleva_las_previews_de_la_primera(db, cfg):
    """Un día con dos envíos: cada previsualización se queda con la SUYA.

    Pasa de verdad -el envío de la mañana y el de la tarde rehaciéndolo- y sin
    la condición de "solo las pendientes" el segundo se llevaría por delante el
    enlace del primero. Nada fallaría: la fila seguiría enlazada, solo que a una
    decisión que no fue la que ejecutó lo que se miró por la mañana.

    Y ahí se rompe justo la medida que la tabla existe para dar: "lo que miré,
    ¿se llegó a hacer?" se contestaría comparando la anulación de la mañana con
    la sesión de la tarde, que es otra pregunta y con otra respuesta.
    """
    def decidido():
        return decide(cfg, LUNES, sig_completa(LUNES), EngineState())

    manana = save_preview(db, decidido(), answers=dict(RESPUESTAS))
    primera = save_decision(db, decidido())
    assert enlazar_previews_pendientes(db, LUNES, primera.id) == 1

    tarde = save_preview(db, decidido(), answers=dict(RESPUESTAS))
    segunda = save_decision(db, decidido())
    assert enlazar_previews_pendientes(db, LUNES, segunda.id) == 1, (
        "ha enlazado más de una: se ha llevado también la de la mañana"
    )

    assert primera.id != segunda.id, "el escenario no tiene dos decisiones; rehacer"
    db.refresh(manana)
    db.refresh(tarde)
    assert manana.decision_id == primera.id
    assert tarde.decision_id == segunda.id


def test_enviar_sin_haber_previsualizado_no_es_un_error(db, cfg):
    """El camino de siempre sigue siendo un camino, y no devuelve cero por fallo.

    Enviar el formulario sin mirar antes es lo normal. Si esto reventara -o si
    devolviera algo que quien llama tuviera que tratar como excepción- el botón
    de enviar acabaría dependiendo de haber pulsado antes el de previsualizar.
    """
    decidida = save_decision(db, decide(cfg, LUNES, sig_completa(LUNES), EngineState()))

    assert enlazar_previews_pendientes(db, LUNES, decidida.id) == 0


def test_ninguna_columna_de_previews_se_queda_sin_escribir(db, cfg):
    """El guardián de la columna decorativa, aplicado a la tabla nueva.

    Una columna declarada que nadie escribe nunca es peor que no tenerla:
    aparece en un `SELECT *` y parece un dato que se está guardando. Este test
    ejerce el camino completo -anulación, desacuerdo y envío- y exige que
    después no quede ni una columna a `NULL` por falta de escritor.

    Lo que se excluye va nombrado y con motivo, no por comodidad.
    """
    d = decide(
        cfg,
        LUNES,
        sig_completa(LUNES, lower_discomfort=7),
        EngineState(),
        sesion_pedida=SesionPedida(tipo="full", confirmada=True, motivo="hoy sí"),
    )
    fila = save_preview(
        db, d, answers=dict(RESPUESTAS), disagreed=True, disagreement_reason="puedo más"
    )
    enlazar_preview(db, fila.id, save_decision(db, d).id)
    db.flush()
    db.refresh(fila)

    # `override_routine` solo se escribe cuando la rutina elegida se desvía de
    # la que tocaba en el ciclo, y este día no se desvía: no es una columna sin
    # escritor, es una columna sin caso. Tiene la suya en el test de arriba.
    vacias = {
        c.name
        for c in PreviewRow.__table__.columns
        if getattr(fila, c.name) is None and c.name != "override_routine"
    }
    assert not vacias, f"columnas declaradas y nunca escritas: {sorted(vacias)}"


def test_la_rutina_anulada_se_guarda_cuando_de_verdad_se_desvia(db, cfg):
    """La otra mitad de la anulación: no la dureza, sino qué rutina del ciclo."""
    senales = sig_completa(LUNES)
    senales.sesion_elegida = "dia_3"
    d = decide(cfg, LUNES, senales, EngineState())
    assert d.propuesta != "dia_3", "el escenario no discrimina; rehacer"

    fila = save_preview(db, d, answers=dict(RESPUESTAS))

    assert fila.override_routine == "dia_3"


# ---------------------------------------------------------------------------
# La carga progresada se acumula
# ---------------------------------------------------------------------------


def _tope_vigente(session, rutina: str, ejercicio: str) -> float | None:
    """El peso de la serie efectiva más pesada, leído de la base de datos."""
    estado = load_state(session)
    series = estado.current_sets.get((rutina, ejercicio))
    if not series:
        return None
    pesos = [s.get("weight_kg") or 0 for s in series]
    return max(pesos) if any(pesos) else None


def test_la_carga_progresada_se_acumula_en_vez_de_reiniciarse(db, cfg):
    """Tres progresiones seguidas del mismo ejercicio tienen que SUMAR.

    Este es el test del fallo más caro que ha tenido el proyecto, y no daba
    ningún error. La sesión se construía siempre desde `config.yaml` y el
    incremento se sumaba encima, así que la carga oscilaba entre el peso de
    partida y ese peso más un escalón, para siempre. Telegram anunciaba
    "100→105 kg" cada pocas semanas, Hevy mostraba 105 ese día, y la siguiente
    vez que tocaba esa rutina volvía a 100. Visto desde fuera era un sistema
    que progresa; medido, era uno que no se mueve.

    Se simulan varios meses entrenando todo lo que se manda, y se mira la carga
    VIGENTE que queda guardada, no la que se escribe hoy: una semana de descarga
    escribe menos a propósito y eso no es un retroceso.

    El horizonte era de 140 días y ahora es de 200. No es que la garantía haya
    cambiado -la carga sigue acumulando y sigue sin retroceder-, es que la
    CADENCIA se ha movido: con `rep_apply_to: lowest_first` la doble progresión
    sube una repetición por sesión en una sola serie, así que llegar al tope de
    reps -que es lo que dispara el escalón de peso- cuesta 6 sesiones en vez de
    2. Medido en este mismo `config.yaml`: los escalones caen en los días 7,
    161 y 245 en vez de 7, 63 y 126. Si este test vuelve a quedarse corto, mirar
    primero si alguien ha tocado `rep_apply_to` antes de sospechar de la
    persistencia.

    Ese 2,4x asusta más de lo que cuesta, y conviene dejar dicho por qué. Aquí
    se entrena TODO lo que se manda y no se cierra ninguna puerta nunca, así que
    la cadencia de reps es lo único que frena. En condiciones reales no lo es:
    `scripts/sim_12_semanas.py`, con el mismo semáforo y la misma semilla, da 12
    escalones de carga en 12 semanas con `lowest_first` y 13 con `all_sets`. Uno
    de diferencia. Lo que manda de verdad son las puertas de volumen -abiertas
    el 65-71% de las sesiones- y los cupos por sesión; conservar la rampa sale
    casi gratis. Este test mide el peor caso, no el caso.
    """
    raw = cfg.raw
    rutina = "dia_1"
    ejercicio = next(
        e for e in raw["routines"][rutina]["exercises"]
        if any((s.get("weight_kg") or 0) > 0 for s in e.get("sets") or [])
    )
    clave = ejercicio["key"]
    partida = max((s.get("weight_kg") or 0) for s in ejercicio["sets"])
    incremento = float(
        ejercicio.get("increment_kg",
                      (raw.get("progression") or {}).get("default_increment_kg", 2.5))
    )

    vistos: list[float] = [partida]
    for n in range(200):
        dia = LUNES + timedelta(days=n)
        estado = load_state(db, program_start=raw.get("program", {}).get("start_date"))
        decision = decide(cfg, dia, sig_completa(dia), estado)
        sesion = decision.session
        if sesion.routine_key == rutina and sesion.kind in {"full", "reduced"}:
            # Se entrena todo lo mandado: es la única forma de acumular racha.
            ejecutado = {e["key"]: True for e in sesion.exercises if e.get("key")}
        else:
            ejecutado = None
        nuevo = advance_state(estado, decision, executed=ejecutado)
        save_state(db, nuevo, day=dia)

        tope = _tope_vigente(db, rutina, clave)
        if tope is not None and tope != vistos[-1]:
            vistos.append(tope)
        if len(vistos) >= 4:
            break

    esperado = [partida, partida + incremento, partida + 2 * incremento]
    assert vistos[:3] == esperado, (
        f"'{clave}' no acumula carga: se ha visto {vistos[:3]} y tenía que ser "
        f"{esperado}. Si se repite el mismo peso, la progresión se está "
        f"calculando otra vez sobre el punto de partida de config.yaml."
    )
    assert vistos == sorted(vistos), f"la carga ha retrocedido: {vistos}"


def _simular(db, cfg, dias: int, rutina: str) -> dict[str, list[list[dict]]]:
    """Entrena todo lo que se manda y devuelve, por ejercicio, la traza de
    series vigentes DISTINTAS que han ido quedando guardadas en la base."""
    raw = cfg.raw
    traza: dict[str, list[list[dict]]] = {}
    for n in range(dias):
        dia = LUNES + timedelta(days=n)
        estado = load_state(db, program_start=raw.get("program", {}).get("start_date"))
        decision = decide(cfg, dia, sig_completa(dia), estado)
        sesion = decision.session
        if sesion.routine_key == rutina and sesion.kind in {"full", "reduced"}:
            ejecutado = {e["key"]: True for e in sesion.exercises if e.get("key")}
        else:
            ejecutado = None
        save_state(db, advance_state(estado, decision, executed=ejecutado), day=dia)

        for (rk, ek), series in load_state(db).current_sets.items():
            if rk != rutina:
                continue
            t = traza.setdefault(ek, [])
            if not t or t[-1] != series:
                t.append(series)
    return traza


def _claves_por_modo(cfg, rutina: str) -> dict[str, list[str]]:
    modos: dict[str, list[str]] = {}
    for e in cfg.raw["routines"][rutina]["exercises"]:
        modos.setdefault(str(e.get("progression_type")), []).append(e["key"])
    return modos


def test_las_reps_del_modo_volume_se_acumulan(db, cfg):
    """El volumen tiene el mismo fallo que la carga si no se persiste.

    En `volume` no hay peso que mirar: lo que sube son reps o segundos, y viven
    dentro de las mismas series. Si el estado no vuelve, el plan de mañana se
    calcula otra vez sobre las reps de `config.yaml` y el ejercicio se queda
    clavado en 12→14 para siempre, que es justo lo que no se ve en Telegram
    porque el mensaje sí anuncia la subida cada vez.
    """
    modos = _claves_por_modo(cfg, "dia_1")
    traza = _simular(db, cfg, 140, "dia_1")

    con_reps = [
        k for k in modos.get("volume", [])
        if traza.get(k) and any(s.get("reps") for s in traza[k][0])
    ]
    assert con_reps, "config.yaml ya no tiene ningún ejercicio volume por reps"

    for clave in con_reps:
        serie_reps = [min(int(s["reps"]) for s in v if s.get("reps"))
                      for v in traza[clave]]
        assert len(serie_reps) >= 3, (
            f"'{clave}' solo ha cambiado {len(serie_reps)} vez/veces en 140 días: "
            f"{serie_reps}. El volumen no está acumulando."
        )
        assert serie_reps == sorted(serie_reps), (
            f"'{clave}' ha RETROCEDIDO en reps: {serie_reps}"
        )
        assert serie_reps[2] > serie_reps[0], (
            f"'{clave}' no suma: {serie_reps[:3]}. Si las reps oscilan entre dos "
            f"valores, se están recalculando sobre el punto de partida del YAML."
        )


def test_los_segundos_del_modo_volume_se_acumulan(db, cfg):
    """Lo mismo que las reps, pero para los isométricos (plancha y compañía),
    que progresan en `duration_s` y no tienen ni peso ni repeticiones."""
    modos = _claves_por_modo(cfg, "dia_1")
    traza = _simular(db, cfg, 140, "dia_1")

    con_segundos = [
        k for k in modos.get("volume", [])
        if traza.get(k) and any(s.get("duration_s") for s in traza[k][0])
    ]
    assert con_segundos, "config.yaml ya no tiene ningún ejercicio volume por tiempo"

    for clave in con_segundos:
        segs = [min(int(s["duration_s"]) for s in v if s.get("duration_s"))
                for v in traza[clave]]
        assert len(segs) >= 3, f"'{clave}' apenas se mueve en 140 días: {segs}"
        assert segs == sorted(segs), f"'{clave}' ha RETROCEDIDO en segundos: {segs}"
        assert segs[2] > segs[0], (
            f"'{clave}' no suma segundos: {segs[:3]}. Se están recalculando sobre "
            f"el punto de partida del YAML."
        )


def test_las_series_anadidas_en_modo_sets_se_acumulan(db, cfg):
    """El modo `sets` es el que más importa con una hernia L4-L5.

    Es la vía por la que el sistema añade volumen ANTES que carga -una serie más
    de hip thrust antes que 5 kg más-, así que si la cuenta de series no vuelve
    de la base, el ejercicio se queda en las series del YAML y el sistema pasa a
    subir peso mucho antes de lo que debería. El fallo silencioso aquí no es
    "progresa menos": es "progresa por donde no toca".
    """
    modos = _claves_por_modo(cfg, "dia_1")
    traza = _simular(db, cfg, 140, "dia_1")
    claves = [k for k in modos.get("sets", []) if traza.get(k)]
    assert claves, "config.yaml ya no tiene ningún ejercicio en modo sets"

    for clave in claves:
        cuentas = [len(v) for v in traza[clave]]
        assert cuentas == sorted(cuentas), (
            f"'{clave}' ha PERDIDO series por el camino: {cuentas}"
        )
        assert max(cuentas) > cuentas[0], (
            f"'{clave}' nunca añade una serie: {cuentas}. Con el estado sin "
            f"persistir, cada día vuelve a las series de config.yaml y la serie "
            f"añadida ayer desaparece."
        )


def test_ningun_ejercicio_se_queda_sin_progresar_por_perder_siempre_el_cupo(db, cfg):
    """La cola de los cupos tiene que rotar. Nadie pasa hambre.

    Hay como mucho `max_volume_increases_per_session` subidas de volumen por
    sesión, y en Día 1 hay más candidatos que cupo casi todas las semanas. El
    desempate es `queue_policy: waiting_longest`, pero `waiting` lo alimentaba
    `sessions_since_progress`, que en producción no lo rellenaba NADIE: solo lo
    pasaban los scripts de simulación. Con el contador siempre a cero el único
    criterio que quedaba era el orden de la rutina, y los ejercicios del final
    perdían el cupo todas las veces.

    El daño no era "progresa más despacio". En 140 días simulados el perro de
    caza no subía ni una sola repetición y la plancha lateral subía una vez,
    mientras la prensa sumaba carga: los dos que se quedaban parados son los de
    estabilidad lumbar, que con una hernia L4-L5 son justo los que deben ganar
    volumen antes de que nada gane peso. Y no había forma de notarlo, porque el
    mensaje diario era correcto cada mañana: solo decía lo que subía hoy, nunca
    lo que llevaba medio año sin subir.
    """
    modos = _claves_por_modo(cfg, "dia_1")
    traza = _simular(db, cfg, 140, "dia_1")

    candidatos = [k for k in modos.get("volume", []) if traza.get(k)]
    assert candidatos, "config.yaml ya no tiene ejercicios en modo volume en dia_1"

    parados = [k for k in candidatos if len(traza[k]) < 2]
    assert not parados, (
        f"en 140 días estos ejercicios no progresaron NUNCA: {parados}. "
        f"Pierden el cupo de volumen en todas las sesiones, así que la cola no "
        f"está rotando: comprueba que `sessions_since_progress` llega de verdad "
        f"a `plan_progression` y que avanza en `apply_execution`."
    )


# ---------------------------------------------------------------------------
# La serie que alimenta la capa de tendencia
# ---------------------------------------------------------------------------


def test_la_serie_devuelve_un_dia_por_fila(db):
    for i in range(5, 0, -1):
        db.add(DecisionRow(date=LUNES - timedelta(days=i), light="amber",
                        trigger_rule="sueno_corto"))
    db.commit()
    serie = serie_decisiones(db, hasta=LUNES)
    assert [d.day for d in serie] == [LUNES - timedelta(days=i) for i in range(5, 0, -1)]
    assert all(d.light == "amber" and d.trigger_rule == "sueno_corto" for d in serie)


def test_la_serie_viene_ordenada(db):
    """La racha se camina hacia atrás y el motivo agrupa por semana ISO.

    Las dos cosas dan igual con la lista desordenada, pero el replay compara la
    salida de la capa contra la del día anterior y un orden inestable haría
    aparecer y desaparecer avisos sin que cambiara ningún dato.
    """
    for i in (3, 1, 5, 2, 4):
        db.add(DecisionRow(date=LUNES - timedelta(days=i), light="green"))
    db.commit()
    dias_serie = [d.day for d in serie_decisiones(db, hasta=LUNES)]
    assert dias_serie == sorted(dias_serie)


def test_la_serie_solo_trae_la_decision_vigente(db):
    """Un recálculo de las 09:40 no puede contar como un segundo día.

    `decisions` es append-only: el mismo día puede tener la decisión de las
    07:00 y la que la sustituyó al llegar el check-in. Si las dos entraran en la
    serie, la capa vería un día repetido -que es error duro- y la mañana
    reventaría por un histórico perfectamente normal.
    """
    db.add(DecisionRow(date=LUNES, light="green", is_current=False))
    db.add(DecisionRow(date=LUNES, light="amber", trigger_rule="sueno_corto",
                    is_current=True))
    db.commit()
    serie = serie_decisiones(db, hasta=LUNES)
    assert len(serie) == 1
    assert serie[0].light == "amber"


def test_la_serie_no_mira_hacia_adelante(db):
    """En el recálculo de las 09:40 puede haber filas posteriores al día pedido."""
    db.add(DecisionRow(date=LUNES, light="green"))
    db.add(DecisionRow(date=LUNES + timedelta(days=1), light="amber"))
    db.commit()
    assert [d.day for d in serie_decisiones(db, hasta=LUNES)] == [LUNES]


def test_la_serie_no_recorta_el_historico(db):
    """A propósito no tiene ventana.

    Cortarla por 90 días cortaría la racha justo cuando empieza a importar: una
    mala racha de cuatro meses se leería como una de tres. El coste es un SELECT
    que crece, y crece a razón de una fila al día.
    """
    for i in range(400, 0, -1):
        db.add(DecisionRow(date=LUNES - timedelta(days=i), light="green"))
    db.commit()
    assert len(serie_decisiones(db, hasta=LUNES)) == 400


# ---------------------------------------------------------------------------
# El wellness guardado, que hasta ahora tampoco leía nadie
#
# `upsert_daily_metrics` escribía seis meses de wellness y la mañana decidía con
# los ocho días que acababa de pedirle a Garmin. Lo guardado estaba a un SELECT
# de distancia y no se abría nunca.
# ---------------------------------------------------------------------------


def _fila_wellness(dia, **kw):
    from app.models import DailyMetrics

    return DailyMetrics(date=dia, **kw)


def test_las_metricas_guardadas_salen_ordenadas_y_acotadas(db):
    for i in range(5):
        db.add(_fila_wellness(LUNES - timedelta(days=i), hrv=60 + i, rhr=50))
    db.commit()
    leidas = metricas_guardadas(db, desde=LUNES - timedelta(days=3), hasta=LUNES)
    assert [m.date for m in leidas] == [LUNES - timedelta(days=i) for i in (3, 2, 1, 0)]
    assert leidas[-1].hrv == 60


def test_las_metricas_guardadas_conservan_los_huecos(db):
    """Un `None` guardado tiene que llegar como `None`.

    Rellenarlo con la media, o con el último valor conocido, convertiría «esa
    noche no dormí con el reloj» en «dormí y salió normal», que es la mentira
    que vuelve inútil una línea base.
    """
    db.add(_fila_wellness(LUNES, hrv=None, rhr=48))
    db.commit()
    (m,) = metricas_guardadas(db, desde=LUNES, hasta=LUNES)
    assert m.hrv is None
    assert m.rhr == 48


def test_lo_fresco_manda_sobre_lo_guardado():
    """Garmin corrige hacia atrás: el sueño de anoche se reescribe por la mañana."""
    from app.engine.signals import DayMetrics

    fusion = fusionar_metricas(
        [DayMetrics(date=LUNES, sleep_min=400, hrv=60)],
        [DayMetrics(date=LUNES, sleep_min=455)],
    )
    assert [m.sleep_min for m in fusion] == [455]


def test_un_hueco_de_hoy_no_borra_un_dato_de_la_base():
    """La avería que hace cara la fusión perezosa.

    Si esta mañana falla la llamada de sueño y las otras contestan, la fila
    fresca trae `sleep_min=None`. Sustituyendo la fila entera, el sueño de esa
    noche desaparecería de la decisión aunque lleve semanas guardado.
    """
    from app.engine.signals import DayMetrics

    fusion = fusionar_metricas(
        [DayMetrics(date=LUNES, hrv=60, sleep_min=430)],
        [DayMetrics(date=LUNES, hrv=58, sleep_min=None)],
    )
    assert fusion[0].hrv == 58
    assert fusion[0].sleep_min == 430


def test_la_fusion_trae_los_dias_que_solo_estan_en_un_lado():
    """Hoy solo está en lo fresco -se archiva al final de la mañana- y los
    noventa de atrás solo están en la base."""
    from app.engine.signals import DayMetrics

    fusion = fusionar_metricas(
        [DayMetrics(date=LUNES - timedelta(days=i), hrv=60) for i in (3, 2, 1)],
        [DayMetrics(date=LUNES, hrv=61)],
    )
    assert [m.date for m in fusion] == [
        LUNES - timedelta(days=i) for i in (3, 2, 1, 0)
    ]


def test_la_fusion_sale_ordenada_por_fecha():
    """`_baseline_for` busca por fecha en un dict, pero la serie de sueño se
    lee por tramos y un orden inestable movería la media sin que cambie un dato."""
    from app.engine.signals import DayMetrics

    fusion = fusionar_metricas(
        [DayMetrics(date=LUNES - timedelta(days=i)) for i in (1, 5, 3)],
        [DayMetrics(date=LUNES - timedelta(days=i)) for i in (0, 4, 2)],
    )
    assert [m.date for m in fusion] == sorted(m.date for m in fusion)
