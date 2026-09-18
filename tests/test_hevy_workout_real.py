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
    assert all(e.get("key") for e in ejercicios), (
        f"hay ejercicios de '{RUTINA}' sin `key`: {[e for e in ejercicios if not e.get('key')]}. "
        "`_emparejar` los salta en silencio y saldrían del emparejado sin estar."
    )
    return _Plan(ejercicios)


def _parejas(entreno: dict, plan) -> dict:
    """`_emparejar` con la comprobación de que ha emparejado ALGO.

    UN DICCIONARIO VACÍO PASA CUATRO DE LOS TESTS DE ESTE FICHERO SIN MIRAR
    NADA, Y ES EL RESULTADO MÁS FÁCIL DE PRODUCIR QUE HAY.
    `_emparejar` construye su salida recorriendo `planned.exercises` y hace
    `continue` en cuanto un ejercicio no trae `key`. O sea que el día que la
    clave de ejercicio se llame de otra forma en el `config.yaml` -o que
    `_emparejar` decida no incluir los ejercicios que no aparecen en el
    entreno-, esto devuelve `{}`. Y entonces:

      - `assert not sin_casar` es `assert not []`: cierto;
      - `assert all(not r for ... in parejas.values())` es `all([])`: cierto;
      - el bucle del calentamiento no da ni una vuelta, así que sus aserciones
        no se ejecutan.

    Los cuatro aprueban, y lo que este fichero entero existe para vigilar -que
    el contrato con Hevy no se haya movido- deja de vigilarse sin que la cuenta
    de tests baje ni uno. Es exactamente el modo de fallo que describe el
    docstring del módulo, una capa por encima: el código sí avisaría, si alguien
    lo estuviera mirando.

    Se exige una entrada POR EJERCICIO del plan y no solo que no esté vacío:
    emparejar tres de once y callar los otros ocho es la misma avería en
    pequeño, y es la que más se parece a un cambio real.
    """
    parejas = _emparejar(entreno, plan, None)
    assert set(parejas) == {str(e["key"]) for e in plan.exercises}, (
        "`_emparejar` no ha devuelto una entrada por ejercicio del plan: "
        f"faltan {sorted({str(e['key']) for e in plan.exercises} - set(parejas))}. "
        "Sin ellas, las comprobaciones de este fichero pasan en vacío."
    )
    return parejas


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


def _los_que_estaban(entreno: dict, plan) -> tuple[set[str], set[str]]:
    """Parte el plan en los que EXISTÍAN cuando se grabó y los de después.

    EL FIXTURE ES UNA FOTO Y EL PLAN ESTÁ VIVO. La grabación es el Día 3 del
    2026-09-10 con once ejercicios; el 18-09-2026 la rutina pasó a quince, y
    esos cuatro no pueden estar en un entreno de ocho días antes. Comparar la
    foto contra el plan de hoy y exigir que casen todos convierte este fichero
    en algo que se pone rojo cada vez que se toca la rutina, que es una razón
    excelente para que alguien acabe borrándolo.

    La salida NO es relajar la exigencia a «los que casen, que casen». Eso sí
    mataría el test: un fallo de contrato haría que no casara ninguno, el
    conjunto de «los que estaban» saldría vacío y todo aprobaría en vacío, que
    es exactamente el agujero contra el que avisa `_parejas`.

    Lo que se hace es separar por una razón COMPROBABLE y no por una lista
    escrita a mano: un ejercicio del plan «estaba» si su `template_id` aparece
    en la grabación. Con eso, los dos modos de fallo siguen cazándose:

      - si el contrato se mueve -Hevy renombra `exercise_template_id`- ningún
        id casa, `estaban` sale vacío y salta la guarda de abajo;
      - si el emparejado se rompe para un ejercicio cuyo id SÍ está en la
        grabación, ese cae en `estaban` y su test falla como antes.

    Lo único que deja de ser un fallo es lo que nunca lo fue: que un ejercicio
    añadido después no aparezca en un entreno anterior.
    """
    ids_grabados = {
        str(ex.get("exercise_template_id") or "").upper()
        for ex in entreno.get("exercises") or []
    }
    estaban, posteriores = set(), set()
    for e in plan.exercises:
        destino = estaban if str(e.get("template_id") or "").upper() in ids_grabados else posteriores
        destino.add(str(e["key"]))

    assert len(estaban) >= len(ids_grabados), (
        f"solo {len(estaban)} ejercicio(s) del plan tienen un `template_id` que "
        f"esté en la grabación, y la grabación trae {len(ids_grabados)}. O se han "
        f"quitado ejercicios de la rutina, o el campo del id ha cambiado de "
        f"nombre y este fichero estaba a punto de aprobar en vacío."
    )
    return estaban, posteriores


def test_todos_los_ejercicios_del_plan_casan_con_el_entreno_real(entreno, plan):
    """EL TEST. Ninguna clave del plan puede quedarse sin emparejar.

    Un `reales=None` no lanza nada: el ejercicio cuenta como no hecho, la racha
    no avanza y la carga se queda quieta. El fallo no se ve por ningún sitio, ni
    en el log ni en el mensaje de la mañana. Por eso se comprueba que casan
    TODOS los que pudieron hacerse, y no que casa alguno.
    """
    estaban, posteriores = _los_que_estaban(entreno, plan)
    parejas = _parejas(entreno, plan)

    sin_casar = sorted(k for k, (_obj, reales) in parejas.items() if not reales)
    assert not (set(sin_casar) & estaban), (
        f"ejercicio(s) del plan que SÍ están en el entreno real y no encuentran "
        f"sus series: {sorted(set(sin_casar) & estaban)}. La progresión de esos "
        f"se para sin avisar."
    )
    # Y al revés: lo que no casa tiene que ser exactamente lo añadido después.
    # Sin esto, un ejercicio que dejara de casar por cualquier otro motivo se
    # colaría con la excusa de ser nuevo.
    assert set(sin_casar) <= posteriores, (
        f"no casan {sorted(set(sin_casar) - posteriores)} y no son de los "
        f"añadidos después de la grabación"
    )


def test_el_emparejamiento_va_por_template_id_y_no_por_el_nombre(entreno, plan):
    """El nombre es del YAML y lo escribe una persona; el id lo da Hevy.

    Cambiar un nombre en `config.yaml` -tildes, mayúsculas, un paréntesis- no
    puede parar la progresión de ese ejercicio.
    """
    estaban, _ = _los_que_estaban(entreno, plan)
    tocado = json.loads(json.dumps(entreno))
    for ex in tocado["exercises"]:
        ex["title"] = "OTRO NOMBRE QUE NO COINCIDE CON NADA"

    parejas = _parejas(tocado, plan)
    sin_casar = {k for k, (_o, r) in parejas.items() if not r}
    assert not (sin_casar & estaban), (
        f"se estaba emparejando por el nombre: {sorted(sin_casar & estaban)}"
    )


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

    parejas = _parejas(tocado, plan)
    assert all(not r for _o, r in parejas.values()), (
        "el escenario ya no reproduce la deriva; hay que rehacer el test"
    )


# ---------------------------------------------------------------------------
# Lo que se lee de las series
# ---------------------------------------------------------------------------


def test_el_calentamiento_no_cuenta_como_trabajo(entreno, plan):
    """40 kg de calentamiento no pueden adoptarse como la carga del día."""
    parejas = _parejas(entreno, plan)
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
