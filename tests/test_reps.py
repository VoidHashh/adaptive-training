"""Cómo suben las repeticiones.

Este fichero existe por una opción que decía lo contrario de lo que el código
hacía. `config.yaml` lleva desde el principio esto:

    progression:
      modes:
        double:
          rep_apply_to: lowest_first

y `progression.py` no leía esa clave en ningún sitio. Calculaba la subida sobre
la serie más BAJA y luego escribía el resultado en TODAS las series, porque
viajaba como valor absoluto (`new_reps`). El efecto medido sobre el esquema
real: 12/10/10 salía 11/11/11. O sea que el día que un ejercicio progresaba, la
serie superior perdía una repetición. Subir bajando el top set es lo contrario
de progresar, y no daba ningún error: Telegram anunciaba "+1 rep" y era verdad
para dos series de tres.

Es el mismo fallo que la carga ya tenía resuelto -viajar como delta y no como
absoluto- y la misma familia que todo lo demás de este proyecto: un interruptor
conectado a nada.

Las dos invariantes que fija este fichero:
  1. `lowest_first` sube UNA serie, la más baja, y si hay empate la primera.
  2. NINGÚN modo baja nunca una serie. Ni el que sube una ni el que sube todas.
"""

from __future__ import annotations

import pytest

from app.engine.progression import (
    ExerciseProgression,
    ProgressionPlan,
    plan_progression,
)
from app.engine.session_builder import apply_progression

from tests.conftest import LUNES, sig_completa


def ejercicio(reps: list[int], *, clave: str = "prensa") -> dict:
    """Un ejercicio con las reps dadas y sin calentamiento marcado."""
    return {
        "key": clave,
        "name": "Prensa",
        "sets": [{"reps": r, "weight_kg": 100, "type": "normal"} for r in reps],
    }


def plan_con(*cambios: ExerciseProgression) -> ProgressionPlan:
    return ProgressionPlan(
        routine_key="dia_1", gate_open=True, gate_reason="",
        sets_allowed=True, sets_reason="", reps_allowed=True, reps_reason="",
        exercises=list(cambios),
    )


def subir(reps: list[int], **kw) -> list[int]:
    """Aplica una subida de reps y devuelve el esquema resultante."""
    ex = ejercicio(reps)
    plan = plan_con(
        ExerciseProgression(
            key="prensa", name="Prensa", mode="double",
            changed=True, kind="volume", what="+1 rep",
            rep_delta=kw.get("delta", 1),
            rep_apply_to=kw.get("modo", "lowest_first"),
            rep_cap=kw.get("tope"),
        )
    )
    # `set_cfg` vacío = sin heurística de calentamiento: las tres series son
    # efectivas y la subida las ve todas.
    apply_progression([ex], plan, {})
    return [s["reps"] for s in ex["sets"]]


# ---------------------------------------------------------------------------
# lowest_first: el caso que la opción prometía
# ---------------------------------------------------------------------------


def test_la_rampa_sube_por_abajo_y_el_top_set_no_se_toca():
    """El ejemplo exacto del fallo: 12/10/10 tiene que salir 12/11/10.

    Antes salía 11/11/11. Los dos "suben una repetición" si se mira el total,
    pero uno conserva la rampa y el otro la aplana quitándole trabajo a la
    serie que más pesa.
    """
    assert subir([12, 10, 10], tope=12) == [12, 11, 10]


def test_la_rampa_se_llena_desde_abajo_escalon_a_escalon():
    """Cuatro subidas seguidas desde 12/10/10 hasta emparejar en 12/12/12.

    Ninguna de ellas baja nada. Esto es la progresión escalonada de toda la
    vida, y es lo que `lowest_first` significa.
    """
    esquema = [12, 10, 10]
    recorrido = [list(esquema)]
    for _ in range(4):
        esquema = subir(esquema, tope=12)
        recorrido.append(list(esquema))

    assert recorrido == [
        [12, 10, 10],
        [12, 11, 10],
        [12, 11, 11],
        [12, 12, 11],
        [12, 12, 12],
    ]


def test_con_empate_sube_la_primera_y_solo_una():
    """Tres series iguales: sube una, no tres. Si subieran las tres, el
    ejercicio saltaría al tope en una sola sesión."""
    assert subir([10, 10, 10], tope=12) == [11, 10, 10]


def test_el_tope_frena_la_subida_de_la_serie_mas_baja():
    """Con todo en el tope no queda recorrido y el esquema no se mueve. Lo que
    NO puede pasar es que se pase del tope."""
    assert subir([12, 12, 12], tope=12) == [12, 12, 12]
    assert subir([11, 11, 11], delta=3, tope=12) == [12, 11, 11]


# ---------------------------------------------------------------------------
# all_sets: el otro modo, que también tiene que respetar la invariante
# ---------------------------------------------------------------------------


def test_all_sets_sube_todas_las_series_a_la_vez():
    assert subir([12, 10, 10], modo="all_sets", tope=14) == [13, 11, 11]


def test_all_sets_topa_cada_serie_por_su_cuenta_sin_nivelar_hacia_abajo():
    """El tope frena a la que llega, no recorta a la que ya estaba por encima.

    Este es el detalle que convierte un tope en un recorte silencioso si se
    escribe mal: con `min(valor + delta, tope)` a secas, una serie de 14 con
    tope 12 saldría 12. El tope existe para frenar la subida, no para nivelar.
    """
    assert subir([14, 10, 10], modo="all_sets", tope=12) == [14, 11, 11]


# ---------------------------------------------------------------------------
# La invariante que vale para los dos modos
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("modo", ["lowest_first", "all_sets"])
@pytest.mark.parametrize(
    "esquema",
    [[12, 10, 10], [10, 10, 10], [12, 12, 12], [14, 10, 10], [8], [15, 12, 10, 8]],
)
def test_ninguna_serie_baja_nunca(modo, esquema):
    """La invariante de la que salió todo este fichero, en los dos modos y en
    todos los esquemas que aparecen en `config.yaml`.

    `zip` TRUNCA POR EL LADO CORTO, así que una salida más corta que la entrada
    -o vacía- no compara las series que sobran y la invariante aprueba sobre
    las que sí quedan. Perder una serie al progresar es una forma de "bajar"
    peor que bajar repeticiones, y es la única que el `all(...)` no ve.
    """
    salida = subir(esquema, modo=modo, tope=12)
    assert len(salida) == len(esquema), (
        f"con rep_apply_to={modo}, {esquema} ha salido {salida}: el número de "
        f"series ha cambiado, y `zip` se comería las que faltan sin mirarlas"
    )
    assert all(d >= a for a, d in zip(esquema, salida, strict=True)), (
        f"con rep_apply_to={modo}, {esquema} ha salido {salida}: alguna serie "
        f"ha PERDIDO repeticiones al progresar."
    )


@pytest.mark.parametrize("modo", ["lowest_first", "all_sets"])
def test_sin_delta_no_se_toca_nada(modo):
    """Un plan que no sube reps no puede reescribirlas de rebote."""
    assert subir([12, 10, 10], delta=0, modo=modo, tope=12) == [12, 10, 10]


def test_un_modo_desconocido_no_aplana_la_rampa():
    """Si alguien escribe `rep_apply_to: todas` en el YAML, el cargador lo caza
    antes de llegar aquí. Pero si llegara, el peor comportamiento posible sería
    volver al absoluto: esto fija que el camino por defecto es el conservador,
    que sube una sola serie."""
    assert subir([12, 10, 10], modo="cualquier_cosa", tope=12) == [12, 11, 10]


# ---------------------------------------------------------------------------
# El absoluto que SÍ es correcto
# ---------------------------------------------------------------------------


def test_el_cierre_de_ciclo_si_reescribe_todas_las_series():
    """`new_reps` sigue existiendo y sigue siendo absoluto, porque hay un caso
    en el que serlo es lo correcto: cuando la doble progresión llega al tope
    del rango, sube el peso y devuelve las reps al mínimo. Ahí el recorte está
    declarado y anunciado, y afecta a todas las series a propósito."""
    ex = ejercicio([12, 12, 12])
    plan = plan_con(
        ExerciseProgression(
            key="prensa", name="Prensa", mode="double",
            changed=True, kind="load", what="100→105 kg",
            new_reps=10, weight_delta_kg=5.0, apply_to="all_sets",
        )
    )
    apply_progression([ex], plan, {})
    assert [s["reps"] for s in ex["sets"]] == [10, 10, 10]
    assert [s["weight_kg"] for s in ex["sets"]] == [105, 105, 105]


# ---------------------------------------------------------------------------
# El camino entero: planificador -> constructor
# ---------------------------------------------------------------------------
#
# Todo lo de arriba construye el `ExerciseProgression` a mano, así que prueba
# el constructor pero NO el planificador. Eso deja pasar el fallo original tal
# cual: basta con que `_plan_double` vuelva a poner `ex.new_reps = baja + inc`
# en vez del delta para que 12/10/10 salga otra vez 11/11/11, y ninguno de los
# tests anteriores se entera. Se comprobó mutando el fichero: 593 pasando.
# Estos dos recorren el camino de verdad.


def _doble_con_esquema(cfg, esquema: list[int]):
    """Planifica `prensa_horizontal` partiendo del esquema de reps dado.

    El esquema entra por `current_sets` -la carga vigente guardada- y no
    tocando `config.yaml`, porque es exactamente por donde entra en producción:
    el YAML es el punto de partida y a partir de la primera progresión manda la
    base de datos.
    """
    series = [{"reps": r, "weight_kg": 100.0, "type": "normal"} for r in esquema]
    plan = plan_progression(
        cfg, "dia_1", sig_completa(LUNES), "green",
        compliance={e["key"]: True for e in cfg.raw["routines"]["dia_1"]["exercises"]},
        clean_sessions={e["key"]: 5 for e in cfg.raw["routines"]["dia_1"]["exercises"]},
        last_routine_light="green",
        current_sets={("dia_1", "prensa_horizontal"): series},
    )
    cambio = next(
        (e for e in plan.exercises if e.key == "prensa_horizontal"), None
    )
    assert cambio is not None, "la prensa ha desaparecido del plan"
    return cambio, series


def test_el_planificador_manda_un_incremento_y_no_unas_reps_absolutas(cfg):
    """La garantía en el sitio donde se rompió: lo que sale del planificador
    para una subida de reps tiene que ser un DELTA.

    Si vuelve a salir `new_reps`, el constructor lo escribirá en todas las
    series y volveremos a tener el 11/11/11, sin que nadie proteste.
    """
    cambio, _ = _doble_con_esquema(cfg, [12, 10, 10])
    assert cambio.changed and cambio.kind == "volume"
    assert cambio.rep_delta == 1, (
        f"la prensa no sube por delta: rep_delta={cambio.rep_delta}"
    )
    assert cambio.new_reps is None, (
        "el planificador ha mandado unas reps ABSOLUTAS para una subida de "
        "reps. Eso se escribe en todas las series y le quita repeticiones a la "
        "serie superior: es el fallo que este fichero existe para impedir."
    )
    assert cambio.rep_apply_to == "lowest_first"
    assert cambio.rep_cap == 12


def test_del_planificador_al_esquema_final_el_top_set_sigue_intacto(cfg):
    """Planificador y constructor juntos, sobre el ejemplo de siempre."""
    cambio, series = _doble_con_esquema(cfg, [12, 10, 10])
    ex = {"key": "prensa_horizontal", "name": "Prensa", "sets": series}
    apply_progression([ex], plan_con(cambio), {})
    assert [s["reps"] for s in ex["sets"]] == [12, 11, 10]
