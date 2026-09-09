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
    HevyClient,
    HevyError,
    _alcanza,
    _fecha_workout,
    workout_compliance,
)
from tests.conftest import FakeHTTP, FakeResponse

DIA = date(2026, 9, 7)


# ---------------------------------------------------------------------------
# Dobles mínimos
# ---------------------------------------------------------------------------


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


def test_un_error_http_revienta_en_vez_de_devolver_lo_que_haya():
    http = FakeHTTP([FakeResponse(500, None, "boom")])
    with pytest.raises(HevyError, match="500"):
        cliente(http).get_workouts(since=DIA)
