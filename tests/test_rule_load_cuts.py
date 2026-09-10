"""Una regla que dispara y no recorta nada tiene que reventar.

`message.py` lista las reglas activas por su nombre, sin comprobar si han
cambiado una sola serie. Así que una regla que se anuncia y no recorta produce
exactamente el escenario peor de un sistema que decide solo: leer "descarga
lumbar activa" a las nueve de la mañana y entrenar sin ninguna descarga.

El agujero por el que se cuela es que `apply_load_factor` solo toca las series
que tienen `weight_kg`. Un ejercicio a 0 kg -porque la carga todavía no está
registrada en Hevy- multiplicado por 0,7 sigue siendo 0: no hay excepción, no
hay entrada en `changes`, no hay diferencia visible con un recorte que sí se
aplicó.
"""

from __future__ import annotations

import pytest

from app.engine.rules import RuleError
from app.engine.session_builder import apply_rule_load_cuts


def ej(key: str, kg: float | None, series: int = 3) -> dict:
    """Un ejercicio con `series` series, todas al mismo peso (o sin peso)."""
    return {
        "key": key,
        "name": key.replace("_", " "),
        "sets": [{"reps": 8, "weight_kg": kg} for _ in range(series)],
    }


def regla(nombre: str, factor: float, objetivos: list[str] | None = None) -> dict:
    accion: dict = {"factor": factor}
    if objetivos is not None:
        accion["exercises"] = objetivos
    return {"name": nombre, "action": {"reduce_load": accion}}


# --- la regla nombra ejercicios -------------------------------------------


def test_el_ejercicio_nombrado_esta_y_tiene_peso_se_recorta_sin_ruido():
    ejercicios = [ej("press_hombro_maquina", 40.0), ej("remo_t_apoyado", 60.0)]
    log = apply_rule_load_cuts(
        ejercicios, [regla("descarga_press_hombro", 0.7, ["press_hombro_maquina"])]
    )

    assert [s["weight_kg"] for s in ejercicios[0]["sets"]] == [28.0] * 3
    # El que no nombra no se toca.
    assert [s["weight_kg"] for s in ejercicios[1]["sets"]] == [60.0] * 3
    assert len(log) == 1
    assert "descarga_press_hombro" in log[0]


def test_el_ejercicio_nombrado_no_esta_hoy_calla():
    """Una regla sobre el peso muerto no hace nada un día de empuje.

    Este silencio es el correcto y tiene que seguir siéndolo: si fuese error,
    cualquier regla con `duration_days` reventaría en cuanto tocase un día
    cuya rutina no incluye el ejercicio, que es la mayoría de los días.
    """
    ejercicios = [ej("press_banca", 50.0)]
    assert apply_rule_load_cuts(
        ejercicios, [regla("descarga_lumbar", 0.7, ["peso_muerto_smith"])]
    ) == []
    assert [s["weight_kg"] for s in ejercicios[0]["sets"]] == [50.0] * 3


def test_el_ejercicio_nombrado_esta_y_no_tiene_peso_es_error_duro():
    """El caso que motivó todo esto.

    El ejercicio está en la sesión de hoy, la regla lo nombra, y el recorte no
    cambia nada porque no hay carga registrada. Antes: log vacío y el mensaje
    anunciando la descarga igual.
    """
    ejercicios = [ej("hip_thrust_barra", None), ej("press_banca", 50.0)]
    with pytest.raises(RuleError) as exc:
        apply_rule_load_cuts(
            ejercicios, [regla("descarga_lumbar", 0.7, ["hip_thrust_barra"])]
        )

    msg = str(exc.value)
    assert "descarga_lumbar" in msg
    assert "hip_thrust_barra" in msg
    assert "70" in msg


def test_basta_con_que_uno_de_los_nombrados_este_mudo():
    """Nombrar dos ejercicios es afirmar algo sobre los dos.

    Que el otro sí se recortara no compensa: la regla se anunciaría entera y
    solo la mitad sería verdad.
    """
    ejercicios = [ej("press_banca", 50.0), ej("plancha_lateral", None)]
    with pytest.raises(RuleError, match="plancha_lateral"):
        apply_rule_load_cuts(
            ejercicios,
            [regla("descarga_x", 0.8, ["press_banca", "plancha_lateral"])],
        )


def test_con_peso_en_una_sola_serie_ya_hay_recorte():
    """No se exige que TODAS las series tengan carga.

    Un ejercicio con la primera serie a 0 y el resto cargadas es un
    calentamiento sin peso, no un ejercicio sin registrar.
    """
    ejercicio = ej("press_banca", 50.0)
    ejercicio["sets"][0]["weight_kg"] = None
    log = apply_rule_load_cuts([ejercicio], [regla("r", 0.7, ["press_banca"])])
    assert len(log) == 1
    assert [s["weight_kg"] for s in ejercicio["sets"]] == [None, 35.0, 35.0]


# --- la regla recorta la sesión entera -------------------------------------


def test_el_recorte_global_no_revienta_por_los_ejercicios_de_peso_corporal():
    """Una plancha no se multiplica por 0,7 y eso es normal, no un fallo.

    Este es el motivo de que el recorte global se juzgue distinto al nombrado:
    exigirle que tocase todos los ejercicios haría imposible cualquier semana
    de descarga en una rutina con trabajo de estabilidad lumbar, que es
    justamente la que no se puede perder.
    """
    ejercicios = [ej("press_banca", 50.0), ej("plancha_lateral", None)]
    log = apply_rule_load_cuts(ejercicios, [regla("semana_de_descarga", 0.9)])

    assert [s["weight_kg"] for s in ejercicios[0]["sets"]] == [45.0] * 3
    assert [s["weight_kg"] for s in ejercicios[1]["sets"]] == [None] * 3
    assert len(log) == 1


def test_el_recorte_global_que_no_toca_absolutamente_nada_es_error_duro():
    ejercicios = [ej("plancha_lateral", None), ej("perro_de_caza", None)]
    with pytest.raises(RuleError) as exc:
        apply_rule_load_cuts(ejercicios, [regla("semana_de_descarga", 0.9)])
    assert "NADA" in str(exc.value)


# --- bordes ----------------------------------------------------------------


def test_sin_ejercicios_no_hay_nada_que_denunciar():
    """Día rojo o bloque de recuperación: la sesión viene vacía a propósito."""
    assert apply_rule_load_cuts([], [regla("semana_de_descarga", 0.9)]) == []
    assert apply_rule_load_cuts([], [regla("r", 0.7, ["press_banca"])]) == []


def test_una_regla_sin_reduce_load_se_ignora():
    ejercicios = [ej("plancha_lateral", None)]
    otras = [{"name": "retirada", "action": {"remove_exercises": ["x"]}}]
    assert apply_rule_load_cuts(ejercicios, otras) == []
