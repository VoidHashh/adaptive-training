"""Lectura de lo ejecutado en Hevy: la mitad que cierra el bucle.

El sistema escribe la rutina cada mañana y manda el mensaje. Sin esta parte
nunca se entera de si la sesión se hizo, y `advance_state` no recibe nunca un
`executed` de verdad: la racha de sesiones limpias se queda a cero para siempre
y la carga no sube nunca. Los tests de aquí vigilan sobre todo el sentido de los
fallos, porque en este módulo un fallo silencioso no da un error, da una racha
que avanza sin pruebas -y detrás de la racha va la subida de carga en una
espalda con hernia-.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.integrations.hevy import (
    SIN_RASTRO,
    HevyClient,
    HevyError,
    _alcanza,
    _fecha_workout,
    claves_hiit,
    motivos_incumplimiento,
    pesos_ejecutados,
    routine_key_de,
    workout_compliance,
)
from tests.conftest import FakeHTTP, FakeResponse
from tests.dobles import doble_de
from app.engine.session_builder import BuiltSession

DIA = date(2026, 9, 7)


# ---------------------------------------------------------------------------
# Dobles mínimos
# ---------------------------------------------------------------------------


@doble_de(BuiltSession)
class Plan:
    """Lo que el motor planificó. `workout_compliance` solo lee `.exercises`."""

    def __init__(self, *exercises: dict):
        self.exercises = list(exercises)


def ejercicio(key: str, *, template_id: str = None, name: str = None, sets=()):
    return {
        "key": key,
        "name": name or key,
        "template_id": template_id or f"T-{key}",
        "sets": list(sets),
    }


def serie(**kw) -> dict:
    return kw


def hecho(template_id: str, *sets: dict) -> dict:
    return {"exercise_template_id": template_id, "sets": list(sets)}


def workout(*exercises: dict, day: date = DIA, wid: str = "w1") -> dict:
    return {
        "id": wid,
        "start_time": f"{day.isoformat()}T07:30:00Z",
        "exercises": list(exercises),
    }


CFG_SETS = {"set_types": {"source": "api"}}


# ---------------------------------------------------------------------------
# _alcanza: ¿una serie cumple?
# ---------------------------------------------------------------------------


def test_hacer_de_mas_cumple():
    """Doce repeticiones cuando se pedían diez es una sesión limpia."""
    assert _alcanza({"reps": 12}, {"reps": 10}) is True


def test_quedarse_corto_no_cumple():
    assert _alcanza({"reps": 9}, {"reps": 10}) is False


def test_una_serie_sin_registrar_reps_no_cumple():
    """Sin número no hay prueba, y sin prueba no se da por hecho."""
    assert _alcanza({"reps": None}, {"reps": 10}) is False


def test_un_ejercicio_por_tiempo_no_necesita_reps():
    """La plancha lateral no tiene repeticiones y aun así se puede cumplir.

    Exigirle reps la dejaría en "no cumplida" para siempre, y un ejercicio que
    nunca cumple bloquea la racha entera de la sesión.
    """
    assert _alcanza({"duration_seconds": 45}, {"duration_s": 40}) is True
    assert _alcanza({"duration_seconds": 30}, {"duration_s": 40}) is False


def test_una_serie_del_plan_sin_magnitud_es_error_y_no_un_cumple():
    """El fallo que no puede pasar callando.

    Si el plan no pide ni reps ni tiempo no hay nada que comprobar, y devolver
    `True` significaría dar el ejercicio por limpio sin una sola prueba de que
    se hizo. Esa mentira alimenta la racha y la racha sube la carga.
    """
    with pytest.raises(HevyError, match="sin magnitud medible"):
        _alcanza({"reps": 10}, {"weight_kg": 60})


# ---------------------------------------------------------------------------
# _alcanza: el peso, que es la mitad que faltaba
# ---------------------------------------------------------------------------


def test_las_reps_completas_con_menos_peso_no_cumplen():
    """EL test del punto 13, y el que antes no existía.

    Diez repeticiones a 50 kg cuando el plan pedía diez a 60 no es una sesión
    limpia: es la misma sesión con menos carga. Mientras el peso quedó fuera de
    la comparación esto salía `True`, alimentaba la racha y pagaba la subida
    siguiente. El plan subía mientras la realidad bajaba.
    """
    assert _alcanza({"reps": 10, "weight_kg": 50}, {"reps": 10, "weight_kg": 60}) is False


def test_mas_peso_del_pedido_cumple():
    """El criterio sigue siendo "no se quedó corto", igual que con las reps."""
    assert _alcanza({"reps": 10, "weight_kg": 65}, {"reps": 10, "weight_kg": 60}) is True


def test_el_peso_exacto_cumple_aunque_venga_por_otro_camino():
    """62,5 escrito por el motor y leído desde JSON tienen que empatar.

    El epsilon existe para esto y solo para esto: es ruido de coma flotante, no
    una tolerancia de carga.
    """
    assert _alcanza({"reps": 10, "weight_kg": 62.5}, {"reps": 10, "weight_kg": 62.5}) is True
    assert _alcanza(
        {"reps": 10, "weight_kg": 62.5 - 1e-9}, {"reps": 10, "weight_kg": 62.5}
    ) is True


def test_medio_kilo_de_menos_sigue_siendo_de_menos():
    """El epsilon no puede convertirse en un margen de tolerancia por la puerta
    de atrás: si dejara pasar medio kilo, dejaría pasar la deriva entera."""
    assert _alcanza({"reps": 10, "weight_kg": 62.0}, {"reps": 10, "weight_kg": 62.5}) is False


def test_el_peso_sin_apuntar_no_cumple():
    """La ausencia de dato no es prueba de nada.

    Si valiera como cumplimiento, no apuntar el peso sería la manera de saltarse
    la comprobación entera.
    """
    assert _alcanza({"reps": 10}, {"reps": 10, "weight_kg": 60}) is False
    assert _alcanza({"reps": 10, "weight_kg": None}, {"reps": 10, "weight_kg": 60}) is False


def test_el_peso_corporal_sigue_cumpliendo():
    """Plancha y dominadas van a 0 kg o sin poner.

    Exigirles un peso registrado las dejaría en "no cumplido" para siempre, y un
    ejercicio que nunca cumple bloquea la racha de la sesión entera.
    """
    assert _alcanza({"reps": 12}, {"reps": 10, "weight_kg": 0}) is True
    assert _alcanza({"reps": 12}, {"reps": 10}) is True


def test_el_peso_por_si_solo_no_es_magnitud_medible():
    """Un peso sin reps ni segundos no dice si la serie se terminó.

    Por eso el peso no marca `comprobado` y el plan sigue teniendo que pedir
    reps o tiempo. Es el mismo error duro de antes, y comprobarlo aquí evita que
    añadir el peso lo haya desactivado sin querer.
    """
    with pytest.raises(HevyError, match="sin magnitud medible"):
        _alcanza({"reps": 10, "weight_kg": 60}, {"weight_kg": 60})


# ---------------------------------------------------------------------------
# workout_compliance
# ---------------------------------------------------------------------------


def test_sesion_completa_cumple_todos_los_ejercicios():
    plan = Plan(
        ejercicio("hip_thrust", sets=[serie(reps=10), serie(reps=10)]),
        ejercicio("remo", sets=[serie(reps=12)]),
    )
    w = workout(
        hecho("T-hip_thrust", {"reps": 10}, {"reps": 11}),
        hecho("T-remo", {"reps": 12}),
    )
    assert workout_compliance(w, plan, CFG_SETS) == {
        "hip_thrust": True,
        "remo": True,
    }


def test_una_serie_de_menos_no_es_limpio():
    """Tres series pedidas y dos hechas: el ejercicio no está completo."""
    plan = Plan(ejercicio("hip_thrust", sets=[serie(reps=10)] * 3))
    w = workout(hecho("T-hip_thrust", {"reps": 10}, {"reps": 10}))
    assert workout_compliance(w, plan, CFG_SETS) == {"hip_thrust": False}


def test_un_ejercicio_que_no_aparece_cuenta_como_no_hecho():
    """Lo prudente: si no está, o no se hizo o no se registró.

    En ninguno de los dos casos hay pruebas de que se completara, y esto es
    justo lo que decide si sube la carga.
    """
    plan = Plan(
        ejercicio("hip_thrust", sets=[serie(reps=10)]),
        ejercicio("peso_muerto", sets=[serie(reps=8)]),
    )
    w = workout(hecho("T-hip_thrust", {"reps": 10}))
    assert workout_compliance(w, plan, CFG_SETS) == {
        "hip_thrust": True,
        "peso_muerto": False,
    }


def test_el_calentamiento_no_cuenta_en_ninguno_de_los_dos_lados():
    """Ni las series de calentamiento del plan ni las del entrenamiento.

    Si contaran las del entrenamiento, la primera serie suave se emparejaría
    con la primera serie efectiva del plan y desplazaría todo el resto: un
    ejercicio bien hecho saldría corto por haber calentado.
    """
    plan = Plan(
        ejercicio(
            "hip_thrust",
            sets=[serie(reps=5, type="warmup"), serie(reps=10), serie(reps=10)],
        )
    )
    w = workout(
        hecho(
            "T-hip_thrust",
            {"reps": 5, "type": "warmup"},
            {"reps": 10},
            {"reps": 10},
        )
    )
    assert workout_compliance(w, plan, CFG_SETS) == {"hip_thrust": True}


def test_un_ejercicio_partido_en_dos_entradas_se_junta():
    """Hevy admite repetir el mismo ejercicio; las series suman, no compiten."""
    plan = Plan(ejercicio("hip_thrust", sets=[serie(reps=10)] * 3))
    w = workout(
        hecho("T-hip_thrust", {"reps": 10}),
        hecho("T-hip_thrust", {"reps": 10}, {"reps": 10}),
    )
    assert workout_compliance(w, plan, CFG_SETS) == {"hip_thrust": True}


def test_un_plan_todo_calentamiento_es_error_y_no_un_cumple():
    """`all([])` es `True`, y ahí no se ha mirado nada.

    Un ejercicio cuyas series del plan sean todas de calentamiento no tiene nada
    efectivo contra lo que comparar, así que saldría "completado" sin una sola
    prueba. Hoy el validador de `config.yaml` lo hace inalcanzable; el error está
    puesto para que el día que deje de serlo se entere alguien.
    """
    plan = Plan(
        ejercicio(
            "movilidad",
            sets=[serie(reps=10, type="warmup"), serie(reps=10, type="warmup")],
        )
    )
    w = workout(hecho("T-movilidad", {"reps": 10}, {"reps": 10}))
    with pytest.raises(HevyError, match="todas .*de calentamiento|calentamiento"):
        workout_compliance(w, plan, CFG_SETS)


# ---------------------------------------------------------------------------
# motivos_incumplimiento: POR QUÉ no cumplió
# ---------------------------------------------------------------------------
#
# El booleano de `workout_compliance` se traga la causa, y quien tenía que
# explicarla se la inventaba: el mensaje de la noche decía «no se completó a las
# reps objetivo» en los cinco casos posibles, también cuando las reps estaban
# clavadas y lo corto era el peso.


def test_lo_que_falló_fue_el_peso_y_eso_es_lo_que_se_dice():
    """El caso de la patada atrás: diez reps de diez, y 50 kg donde pedía 60."""
    plan = Plan(ejercicio("patada_atras", sets=[serie(reps=10, weight_kg=60)]))
    w = workout(hecho("T-patada_atras", {"reps": 10, "weight_kg": 50}))
    assert motivos_incumplimiento(w, plan, CFG_SETS) == {
        "patada_atras": "la serie 1 se hizo a 50 kg y pedía 60"
    }


def test_lo_que_falló_fueron_las_reps_y_se_dice_cuál_serie():
    plan = Plan(ejercicio("hip_thrust", sets=[serie(reps=10)] * 2))
    w = workout(hecho("T-hip_thrust", {"reps": 10}, {"reps": 4}))
    assert motivos_incumplimiento(w, plan, CFG_SETS) == {
        "hip_thrust": "la serie 2 se quedó en 4 de las 10 reps"
    }


def test_un_ejercicio_por_tiempo_habla_de_segundos_y_no_de_reps():
    """La plancha no tiene reps. Nombrarlas sería mandar a mirar algo que no existe."""
    plan = Plan(ejercicio("plancha", sets=[serie(duration_s=45)]))
    w = workout(hecho("T-plancha", {"duration_seconds": 30}))
    assert motivos_incumplimiento(w, plan, CFG_SETS) == {
        "plancha": "la serie 1 se quedó en 30 de los 45 segundos"
    }


def test_faltan_series_y_se_dice_cuántas():
    plan = Plan(ejercicio("hip_thrust", sets=[serie(reps=10)] * 3))
    w = workout(hecho("T-hip_thrust", {"reps": 10}, {"reps": 10}))
    assert motivos_incumplimiento(w, plan, CFG_SETS) == {
        "hip_thrust": "se apuntaron 2 de las 3 series"
    }


def test_lo_que_cumple_no_sale_en_los_motivos():
    plan = Plan(
        ejercicio("hip_thrust", sets=[serie(reps=10)]),
        ejercicio("remo", sets=[serie(reps=12)]),
    )
    w = workout(hecho("T-hip_thrust", {"reps": 10}), hecho("T-remo", {"reps": 3}))
    assert list(motivos_incumplimiento(w, plan, CFG_SETS)) == ["remo"]


def test_el_veredicto_y_el_motivo_no_pueden_contradecirse():
    """Cumplir es exactamente no tener motivo, y se saca de la misma cuenta.

    Dos cuentas en paralelo dejarían la puerta abierta a que el mensaje
    explicara un fallo que el estado no tiene, o a que el estado castigara un
    fallo que nadie sabe nombrar.
    """
    plan = Plan(
        ejercicio("hip_thrust", sets=[serie(reps=10, weight_kg=60)] * 2),
        ejercicio("remo", sets=[serie(reps=12)]),
        ejercicio("peso_muerto", sets=[serie(reps=8)]),
    )
    w = workout(
        hecho("T-hip_thrust", {"reps": 10, "weight_kg": 60}, {"reps": 10, "weight_kg": 55}),
        hecho("T-remo", {"reps": 12}),
    )
    cumple = workout_compliance(w, plan, CFG_SETS)
    porques = motivos_incumplimiento(w, plan, CFG_SETS)
    assert {k for k, v in cumple.items() if not v} == set(porques)


# --- «no lo hice» contra «lo hice y no lo apunté» --------------------------


def test_un_ejercicio_que_no_aparece_dice_que_no_se_puede_distinguir():
    """No hay rastro, y eso es lo único honesto que se puede escribir."""
    plan = Plan(ejercicio("suitcase_carry", sets=[serie(duration_s=40)]))
    w = workout(hecho("T-otro", {"reps": 10}))
    assert motivos_incumplimiento(w, plan, CFG_SETS) == {
        "suitcase_carry": SIN_RASTRO
    }
    assert "no se hizo" in SIN_RASTRO and "sin apuntarlo" in SIN_RASTRO


def test_un_ejercicio_abierto_y_dejado_en_blanco_no_es_lo_mismo_que_no_hacerlo():
    """`suitcase_carry` con la lista de series vacía SÍ estaba en la sesión.

    Fundirlo con "no aparece" contaba como no hecho algo de lo que sí hay
    rastro de haber estado ahí. Sigue sin cumplir -no hay prueba de que se
    completara- pero el motivo es otro y se puede leer.
    """
    plan = Plan(ejercicio("suitcase_carry", sets=[serie(duration_s=40)]))
    w = workout(hecho("T-suitcase_carry"))
    motivo = motivos_incumplimiento(w, plan, CFG_SETS)["suitcase_carry"]
    assert motivo != SIN_RASTRO
    assert "sin una sola serie efectiva apuntada" in motivo
    assert workout_compliance(w, plan, CFG_SETS) == {"suitcase_carry": False}


def test_un_ejercicio_solo_con_calentamiento_dice_lo_mismo():
    """Aparecer con calentamiento y nada más tampoco es prueba de haberlo hecho."""
    plan = Plan(ejercicio("suitcase_carry", sets=[serie(duration_s=40)]))
    w = workout(hecho("T-suitcase_carry", {"duration_seconds": 20, "type": "warmup"}))
    motivo = motivos_incumplimiento(w, plan, CFG_SETS)["suitcase_carry"]
    assert "sin una sola serie efectiva apuntada" in motivo


# ---------------------------------------------------------------------------
# pesos_ejecutados
# ---------------------------------------------------------------------------


def test_el_peso_ejecutado_es_el_de_la_serie_mas_pesada():
    """Una rampa 50/60/65 se resume por su serie top, que es la que manda.

    La media mezclaría el calentamiento efectivo con la serie de trabajo y
    daría un número que no se levantó nunca.
    """
    plan = Plan(ejercicio("hip_thrust", sets=[serie(reps=10, weight_kg=60)] * 3))
    w = workout(
        hecho(
            "T-hip_thrust",
            {"reps": 10, "weight_kg": 50},
            {"reps": 10, "weight_kg": 60},
            {"reps": 10, "weight_kg": 65},
        )
    )
    assert pesos_ejecutados(w, plan, CFG_SETS) == {"hip_thrust": 65.0}


def test_el_calentamiento_no_entra_en_el_peso_ejecutado():
    """Aquí da igual porque calentar es más ligero, pero no siempre: una serie
    de aproximación marcada como calentamiento puede ir por encima de la de
    trabajo en un esquema descendente."""
    plan = Plan(ejercicio("hip_thrust", sets=[serie(reps=10, weight_kg=60)]))
    w = workout(
        hecho(
            "T-hip_thrust",
            {"reps": 5, "weight_kg": 90, "type": "warmup"},
            {"reps": 10, "weight_kg": 60},
        )
    )
    assert pesos_ejecutados(w, plan, CFG_SETS) == {"hip_thrust": 60.0}


def test_sin_peso_apuntado_devuelve_none_y_no_cero():
    """`None` y `0.0` llevan a decisiones opuestas y no se pueden fundir.

    `None` es "no hay dato" y no toca nada; `0.0` sería "se hizo sin carga", que
    en tres sesiones arrastraría el objetivo al suelo. Un ejercicio sin registrar
    no puede acabar bajando la carga.
    """
    plan = Plan(
        ejercicio("hip_thrust", sets=[serie(reps=10, weight_kg=60)]),
        ejercicio("plancha", sets=[serie(duration_s=45)]),
    )
    w = workout(hecho("T-hip_thrust", {"reps": 10}))
    assert pesos_ejecutados(w, plan, CFG_SETS) == {
        "hip_thrust": None,
        "plancha": None,
    }


def test_el_peso_y_el_cumplimiento_ven_exactamente_lo_mismo():
    """La contradicción que el emparejamiento compartido hace imposible.

    Si las dos lecturas emparejaran por su cuenta, una podría decir "no aparece,
    no cumple" y la otra "se hizo a 70 kg", y de ahí saldría una adopción de
    carga apoyada en una sesión que el motor considera fallida.
    """
    plan = Plan(
        ejercicio("hip_thrust", template_id="T-1", sets=[serie(reps=10, weight_kg=60)]),
        ejercicio("remo", template_id="T-2", sets=[serie(reps=12, weight_kg=40)]),
    )
    w = workout(hecho("T-1", {"reps": 10, "weight_kg": 70}))

    cumple = workout_compliance(w, plan, CFG_SETS)
    pesos = pesos_ejecutados(w, plan, CFG_SETS)

    assert set(cumple) == set(pesos), "las dos lecturas no ven los mismos ejercicios"
    assert cumple == {"hip_thrust": True, "remo": False}
    assert pesos == {"hip_thrust": 70.0, "remo": None}


# ---------------------------------------------------------------------------
# routine_key_de: de qué rutina salió lo que se hizo
# ---------------------------------------------------------------------------

CFG_RUTINAS = {
    "routines": {
        "dia_1": {"hevy_routine_id": "ID-1"},
        "dia_3": {"hevy_routine_id": "ID-3"},
        "hiit_dia_1": {"hevy_routine_id": "ID-H1"},
    },
    "hiit": {"blocks": {"dia_1": "hiit_dia_1"}},
}


def test_la_rutina_se_lee_del_id_y_jamas_del_titulo():
    """El fallo que esto impide está medido, no supuesto.

    De los 14 entrenamientos reales de la cuenta el 2026-09-13, CUATRO llevan un
    título que nombra una rutina distinta de la que dice su `routine_id`: el
    título se congela al ejecutar y las rutinas se renombraron después. Clasificar
    por nombre habría errado el 29% del histórico, y en el sentido peor -contando
    como fuerza lo que fue HIIT y al revés-.
    """
    w = {"id": "x", "routine_id": "ID-1", "title": "Día 3"}
    assert routine_key_de(w, CFG_RUTINAS) == "dia_1"


def test_una_rutina_desconocida_no_se_confunde_con_otra():
    """`None` es un dato -«esto no sale del plan»-, no un fallo. Lo que no puede
    es acabar asignado a una rutina cualquiera."""
    assert routine_key_de({"id": "x", "routine_id": "ID-QUE-NO-ESTA"}, CFG_RUTINAS) is None
    assert routine_key_de({"id": "x"}, CFG_RUTINAS) is None


def test_las_claves_hiit_salen_del_config_y_no_de_una_lista_aparte():
    """Dos verdades sobre qué es HIIT acabarían discrepando el día que se añada
    un tercer bloque, y nadie lo notaría hasta que el presupuesto de intensas
    dejara de contarlo."""
    assert claves_hiit(CFG_RUTINAS) == {"hiit_dia_1"}
    assert claves_hiit({}) == set()


# ---------------------------------------------------------------------------
# _fecha_workout
# ---------------------------------------------------------------------------


def test_fecha_legible():
    assert _fecha_workout({"start_time": "2026-09-07T07:30:00Z"}) == DIA


def test_fecha_ilegible_se_descarta_avisando(caplog):
    """Aquí sí se descarta, al revés que en Garmin.

    Un entrenamiento sin fecha no se puede asignar a ningún día por definición.
    Lo que no puede es pasar callando.
    """
    assert _fecha_workout({"id": "x", "start_time": "ayer"}) is None
    assert _fecha_workout({"id": "x"}) is None
    assert "x" in caplog.text


# ---------------------------------------------------------------------------
# get_workouts
# ---------------------------------------------------------------------------


def cliente(http: FakeHTTP) -> HevyClient:
    c = HevyClient(api_key="k", base_url="https://api.hevyapp.com")
    c._client = lambda: http  # noqa: SLF001
    return c


def pagina(*workouts: dict) -> FakeResponse:
    return FakeResponse(200, {"workouts": list(workouts)})


def test_se_para_al_pasarse_de_la_ventana():
    """La API devuelve lo más reciente primero: en cuanto se pasa, ya está.

    No hace falta traerse el histórico entero cada mañana.
    """
    http = FakeHTTP(
        [
            pagina(
                workout(day=DIA, wid="hoy"),
                workout(day=DIA - timedelta(days=5), wid="viejo"),
            )
        ]
    )
    ws = cliente(http).get_workouts(since=DIA - timedelta(days=2))
    assert [w["id"] for w in ws] == ["hoy"]
    assert len(http.llamadas) == 1, "se pidió una página que ya no hacía falta"


def test_una_fecha_rota_no_corta_la_paginacion():
    """El fallo que se arregló: una fecha ilegible no termina la búsqueda.

    Antes `dia is None` y `dia < since` se trataban igual, así que un solo
    entrenamiento con el `start_time` raro cortaba la paginación entera y se
    perdían en silencio todos los anteriores. La sesión de ayer dejaba de
    contar porque la de hoy tenía la fecha mal.
    """
    roto = {"id": "roto", "start_time": "vete a saber", "exercises": []}
    http = FakeHTTP(
        [
            pagina(roto, workout(day=DIA, wid="bueno")),
            pagina(workout(day=DIA - timedelta(days=9), wid="viejo")),
        ]
    )
    ws = cliente(http).get_workouts(since=DIA - timedelta(days=2))
    assert [w["id"] for w in ws] == ["bueno"]


def test_quedarse_sin_paginas_es_error_y_no_una_lista_a_medias():
    """Una lista truncada tiene el mismo aspecto que la lista entera.

    Quien reconcilia daría por no hecha una sesión que sí está, solo que en la
    página siguiente, y eso rompe la racha sin motivo.
    """
    lote = [pagina(workout(day=DIA, wid=f"w{i}")) for i in range(3)]
    with pytest.raises(HevyError, match="páginas no bastan"):
        cliente(FakeHTTP(lote)).get_workouts(since=DIA - timedelta(days=30), max_pages=3)


def test_una_respuesta_vacia_termina_la_busqueda():
    """Sin más entrenamientos la lista está completa: no es un truncamiento."""
    http = FakeHTTP([pagina(workout(day=DIA, wid="hoy")), pagina()])
    ws = cliente(http).get_workouts(since=DIA - timedelta(days=2))
    assert [w["id"] for w in ws] == ["hoy"]


def test_la_ultima_pagina_de_la_cuenta_no_es_un_truncamiento():
    """Pedir más atrás de lo que existe no puede ser un error.

    La cuenta real tiene dos páginas. Con `since` a treinta días no se llega
    nunca a la ventana -no hay nada tan antiguo-, así que el guardián daba por
    truncada la lista y `get_workouts` reventaba; y si no reventaba ahí, la
    página siguiente devolvía 404 y reventaba igual. `POST /api/reconcile?
    dias=30` estaba muerto por esto, y el job de cada noche se salvaba solo
    porque con `dias_atras=3` no pasaba de la primera página.

    Leer el histórico entero es lo contrario de que falte algo: es que ya está
    todo.
    """
    http = FakeHTTP(
        [
            FakeResponse(200, {"workouts": [workout(day=DIA, wid="hoy")], "page_count": 2}),
            FakeResponse(
                200,
                {"workouts": [workout(day=DIA - timedelta(days=40), wid="viejo")],
                 "page_count": 2},
            ),
        ]
    )
    ws = cliente(http).get_workouts(since=DIA - timedelta(days=90))
    assert [w["id"] for w in ws] == ["hoy", "viejo"]
    assert len(http.llamadas) == 2, "se pidió una página que la propia API dice que no existe"


def test_sin_page_count_se_sigue_exigiendo_llegar_a_la_ventana():
    """No se adivina. Si la respuesta no dice cuántas páginas hay, el guardián
    sigue en pie: equivocarse aquí en el sentido optimista sería dar por
    completa una lista a medias, que es justo lo que se quiere impedir."""
    lote = [pagina(workout(day=DIA, wid=f"w{i}")) for i in range(3)]
    with pytest.raises(HevyError, match="páginas no bastan"):
        cliente(FakeHTTP(lote)).get_workouts(since=DIA - timedelta(days=30), max_pages=3)


def test_un_error_http_revienta_en_vez_de_devolver_lo_que_haya():
    http = FakeHTTP([FakeResponse(500, None, "boom")])
    with pytest.raises(HevyError, match="500"):
        cliente(http).get_workouts(since=DIA)


# ---------------------------------------------------------------------------
# `ignorar_peso`: una serie mas ligera no es una serie fallada
# ---------------------------------------------------------------------------
#
# Lo pidio el usuario el 25/09/2026 y el caso que lo destapo esta medido: la
# patada atras del 21/09 se hizo 30, 40 y 50 kg con las 24 reps completas en las
# tres, y el sistema se nego a adoptar los 50 porque la primera iba a 30 cuando
# el plan pedia 35. Estaba tirando la prueba MAS fuerte por culpa de la mas
# floja. Una rampa no es un fallo.
#
# La bandera existe SOLO para la rama de subir de `adoptar_cargas`. El veredicto
# estricto no cambia, y hay un test aqui abajo que lo sujeta: de el cuelga la
# racha de sesiones limpias, y ahi el peso tiene que seguir contando o una
# sesion hecha a 50 cuando el plan pedia 60 volveria a pagar la subida.


def test_para_subir_una_serie_mas_ligera_con_las_reps_completas_si_cumple():
    assert _alcanza(
        {"reps": 24, "weight_kg": 30}, {"reps": 24, "weight_kg": 35},
        ignorar_peso=True,
    ) is True


def test_ignorar_el_peso_no_perdona_las_reps():
    """70 kg a 4 reps cuando se pedian 10 sigue sin ser un objetivo nuevo."""
    assert _alcanza(
        {"reps": 4, "weight_kg": 70}, {"reps": 10, "weight_kg": 60},
        ignorar_peso=True,
    ) is False


def test_ignorar_el_peso_tampoco_perdona_los_segundos():
    assert _alcanza(
        {"duration_seconds": 12}, {"duration_s": 20}, ignorar_peso=True
    ) is False


def test_el_veredicto_estricto_no_se_entera_de_la_bandera():
    """La pareja del primero: los MISMOS datos, sin la bandera, no cumplen.

    Es lo que impide que alguien encienda `ignorar_peso` por defecto un dia y
    reabra el agujero de la racha sin que nada se ponga rojo.
    """
    assert _alcanza(
        {"reps": 24, "weight_kg": 30}, {"reps": 24, "weight_kg": 35}
    ) is False


def test_la_rampa_entera_de_la_patada_atras_del_21_de_septiembre():
    """El caso real, con el plan y las series tal cual estan en la base.

    Plan: calentamiento 20x20 y dos series de 24 a 35 kg.
    Hecho: calentamiento 20x20 y 30, 40 y 50 kg, las tres a 24 reps.
    """
    plan = Plan(ejercicio("patada_atras", sets=[
        serie(type="warmup", reps=20, weight_kg=20),
        serie(reps=24, weight_kg=35),
        serie(reps=24, weight_kg=35),
    ]))
    w = workout(hecho(
        "T-patada_atras",
        serie(type="warmup", reps=20, weight_kg=20),
        serie(type="normal", reps=24, weight_kg=30),
        serie(type="normal", reps=24, weight_kg=40),
        serie(type="normal", reps=24, weight_kg=50),
    ))
    estricto = workout_compliance(w, plan, CFG_SETS)
    arriba = workout_compliance(w, plan, CFG_SETS, ignorar_peso=True)
    assert estricto["patada_atras"] is False
    assert arriba["patada_atras"] is True
    # Y el motivo del estricto sigue nombrando la serie ligera, que es verdad.
    assert "se hizo a 30 kg y pedía 35" in motivos_incumplimiento(
        w, plan, CFG_SETS
    )["patada_atras"]
    # Para subir no hay nada que explicar.
    assert "patada_atras" not in motivos_incumplimiento(
        w, plan, CFG_SETS, ignorar_peso=True
    )


def test_los_dos_motivos_pueden_senalar_series_distintas():
    """Por que la rama de subir necesita SU motivo y no puede usar el estricto.

    Serie 1 ligera pero completa; serie 2 corta de reps. El motivo estricto se
    para en la primera -el peso- y el de subir llega a la segunda -las reps-.
    Reusar el estricto imprimiria «la serie 1 se hizo a 30 kg» junto a una carga
    que NO subio por culpa de la serie 2: una explicacion que no explica.
    """
    plan = Plan(ejercicio("x", sets=[
        serie(reps=10, weight_kg=35),
        serie(reps=10, weight_kg=35),
    ]))
    w = workout(hecho(
        "T-x",
        serie(type="normal", reps=10, weight_kg=30),
        serie(type="normal", reps=4, weight_kg=40),
    ))
    estricto = motivos_incumplimiento(w, plan, CFG_SETS)["x"]
    arriba = motivos_incumplimiento(w, plan, CFG_SETS, ignorar_peso=True)["x"]
    assert "serie 1" in estricto and "30 kg" in estricto
    assert "serie 2" in arriba and "reps" in arriba
    assert estricto != arriba
