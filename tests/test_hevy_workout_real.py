"""La lectura de un entrenamiento de Hevy, contra un entrenamiento DE VERDAD.

POR QUÉ ESTE FICHERO. El 2026-09-12 se descubrió que la escritura en Hevy
estaba rota desde siempre y tenía seis tests en verde: el doble aceptaba
cualquier cuerpo, así que probaba que el código hacía lo que hacía y no que la
API lo aceptara. Al buscar más sitios con la misma forma apareció este, que es
la otra mitad del mismo agujero y la más silenciosa de las dos.

Todo lo que `hevy.py` sabe leer de un entrenamiento -`exercise_template_id`,
`title`, `sets[].type`, `weight_kg`, `reps`- estaba pinchado ÚNICAMENTE contra
diccionarios escritos a mano en los tests. Nunca había pasado por aquí la
respuesta real de `GET /v1/workouts`. Y el modo de fallo no avisa: si una clave
no casa, `_emparejar` devuelve `reales=None`, el ejercicio cuenta como no hecho,
la racha de sesiones limpias no avanza, la puerta de la progresión no se abre y
la carga se queda quieta PARA SIEMPRE sin que nadie lance nada ni escriba nada.
Un sistema que decide solo, decidiendo no subir nunca, por un nombre de campo.

Así que el fixture es la respuesta literal de la API, sin tocar: el «Día 3» del
2026-09-10, once ejercicios, cuarenta series, con calentamiento y con trabajo,
con peso y sin peso. Si Hevy renombra un campo, estos tests se caen; que es
justo lo que no pasaba.

Comprobado además a mano contra la API el mismo día, sobre los tres días de
fuerza reales: `dia_3` 11/11, `dia_2` 9/9, `dia_1` 9/9. O sea que hoy el
contrato se cumple. Esto es para que siga cumpliéndose.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.integrations.hevy import (
    _emparejar,
    pesos_ejecutados,
    workout_compliance,
)
from tests.dobles import doble_de
from app.engine.session_builder import BuiltSession

FIXTURE = Path(__file__).parent / "fixtures" / "hevy_workout_dia_3.json"
RUTINA = "dia_3"


@pytest.fixture(scope="module")
def entreno() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@doble_de(BuiltSession)
class _Plan:
    """`_emparejar` pide un objeto con `.exercises`; el YAML da una lista."""

    def __init__(self, exercises):
        self.exercises = exercises


@pytest.fixture
def plan(cfg):
    ejercicios = ((cfg.raw.get("routines") or {}).get(RUTINA) or {}).get("exercises")
    assert ejercicios, f"la rutina '{RUTINA}' ya no está en el config"
    return _Plan(ejercicios)


# ---------------------------------------------------------------------------
# La forma de la respuesta
# ---------------------------------------------------------------------------


def test_el_entreno_real_trae_los_campos_que_el_codigo_lee(entreno):
    """Si Hevy renombra uno de estos, aquí se ve; en producción, no.

    Es la lista exacta de campos que toca `hevy.py`. No es documentación: es la
    guarda de que el día que cambie el contrato, se entere un test y no la
    progresión callándose durante semanas.
    """
    assert {"id", "title", "start_time", "exercises"} <= set(entreno)

    ejercicios = entreno["exercises"]
    assert ejercicios, "el fixture se ha quedado sin ejercicios"
    for ex in ejercicios:
        assert "exercise_template_id" in ex, "la clave del emparejamiento"
        assert "title" in ex, "el respaldo del emparejamiento"
        for s in ex.get("sets") or []:
            assert "type" in s, "sin esto no se distingue calentamiento de trabajo"
            assert "weight_kg" in s and "reps" in s


def test_el_fixture_tiene_calentamiento_y_trabajo(entreno):
    """Si no, el filtro de calentamiento no se estaría probando contra nada."""
    tipos = {s.get("type") for ex in entreno["exercises"] for s in ex.get("sets") or []}
    assert "warmup" in tipos and "normal" in tipos


# ---------------------------------------------------------------------------
# El emparejamiento, que es lo que falla en silencio
# ---------------------------------------------------------------------------


def test_todos_los_ejercicios_del_plan_casan_con_el_entreno_real(entreno, plan):
    """EL TEST. Ninguna clave del plan puede quedarse sin emparejar.

    Un `reales=None` no lanza nada: el ejercicio cuenta como no hecho, la racha
    no avanza y la carga se queda quieta. El fallo no se ve por ningún sitio, ni
    en el log ni en el mensaje de la mañana. Por eso se comprueba que casan
    TODOS y no que casa alguno.
    """
    parejas = _emparejar(entreno, plan, None)
    sin_casar = sorted(k for k, (_obj, reales) in parejas.items() if not reales)
    assert not sin_casar, (
        f"{len(sin_casar)} ejercicio(s) del plan no encuentran sus series en el "
        f"entreno real: {sin_casar}. La progresión de esos se para sin avisar."
    )


def test_el_emparejamiento_va_por_template_id_y_no_por_el_nombre(entreno, plan):
    """El nombre es del YAML y lo escribe una persona; el id lo da Hevy.

    Cambiar un nombre en `config.yaml` -tildes, mayúsculas, un paréntesis- no
    puede parar la progresión de ese ejercicio.
    """
    tocado = json.loads(json.dumps(entreno))
    for ex in tocado["exercises"]:
        ex["title"] = "OTRO NOMBRE QUE NO COINCIDE CON NADA"

    parejas = _emparejar(tocado, plan, None)
    sin_casar = sorted(k for k, (_o, r) in parejas.items() if not r)
    assert not sin_casar, f"se estaba emparejando por el nombre: {sin_casar}"


def test_si_el_id_cambia_de_nombre_de_campo_se_nota(entreno, plan):
    """La deriva de contrato que este fichero existe para pillar.

    Renombrando el campo, TODO deja de emparejar. Hoy eso no lanza nada: la
    sesión entera cuenta como no hecha y la carga se congela. El test es la
    única alarma que hay.
    """
    tocado = json.loads(json.dumps(entreno))
    for ex in tocado["exercises"]:
        ex["template_id"] = ex.pop("exercise_template_id")
        ex["title"] = "tampoco por aquí"

    parejas = _emparejar(tocado, plan, None)
    assert all(not r for _o, r in parejas.values()), (
        "el escenario ya no reproduce la deriva; hay que rehacer el test"
    )


# ---------------------------------------------------------------------------
# Lo que se lee de las series
# ---------------------------------------------------------------------------


def test_el_calentamiento_no_cuenta_como_trabajo(entreno, plan):
    """40 kg de calentamiento no pueden adoptarse como la carga del día."""
    parejas = _emparejar(entreno, plan, None)
    for key, (_objetivo, reales) in parejas.items():
        for s in reales or []:
            assert str(s.get("type", "normal")).lower() not in {"warmup", "warm_up"}, (
                f"'{key}': una serie de calentamiento se ha colado como trabajo"
            )


def test_los_pesos_que_se_leen_son_los_que_estan_en_el_entreno(entreno, plan):
    """Y el que se lee es el MÁS PESADO de las series efectivas.

    Los números están escritos aquí a mano a propósito: son los que hay en el
    fixture, comprobados leyéndolo. Un test que recalcule el valor con la misma
    fórmula que el código no comprueba nada.
    """
    pesos = pesos_ejecutados(entreno, plan, None)
    esperado = {
        "abduccion_cadera": 55.0,
        "aduccion_cadera": 50.0,
        "press_hombro_maquina": 17.5,
        "contractora_pecho": 30.0,
        "vuelos_posteriores": 3.0,
        "curl_predicador": 18.75,
        "extension_triceps_polea": 17.5,
    }
    for key, kg in esperado.items():
        assert pesos.get(key) == kg, f"'{key}': se esperaba {kg}, se leyó {pesos.get(key)}"


def test_el_cumplimiento_se_calcula_sobre_el_entreno_real(entreno, plan):
    """No se afirma qué días se cumplió: se afirma que la pregunta se contesta.

    Lo que no puede pasar es que devuelva un diccionario vacío o con menos
    entradas que ejercicios tiene el plan, porque entonces `apply_execution`
    daría la sesión por no hecha sin haber mirado nada.
    """
    cumplido = workout_compliance(entreno, plan, None)
    assert set(cumplido) == {str(ex["key"]) for ex in plan.exercises}
    assert all(isinstance(v, bool) for v in cumplido.values())
