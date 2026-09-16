"""Frenos de la progresión.

Este fichero existe por un fallo silencioso concreto. `evaluate_gate` hacía
esto con un freno cuya señal no estaba disponible:

    value = signals.get(str(brake.get("source")))
    if value is None:
        continue        # <- el freno desaparecía sin decir nada

El freno que más importa aquí es `lumbar_bloquea_todo`, que con una hernia
L4-L5 es el que manda. Un día sin check-in es justo el día del que menos se
sabe, y era exactamente el día en el que la puerta se quedaba abierta y se
subía peso.

La norma que fija este fichero: un freno que no se puede evaluar NO es un
freno que no salta. Cierra la puerta y lo dice. La excepción (`on_missing:
skip`) existe para señales legítimamente opcionales y hay que declararla a
mano en el YAML.
"""

from __future__ import annotations

import copy

import pytest

from app.engine.progression import evaluate_gate, plan_progression
from app.engine.rules import RuleError

from tests.conftest import LUNES, sig


def gate(cfg, señales, *, light="green", compliance=True, routine="dia_1"):
    return evaluate_gate(
        cfg.raw["progression"], light, señales, compliance, routine, False
    )


# ---------------------------------------------------------------------------
# Freno sin datos
# ---------------------------------------------------------------------------


def test_sin_check_in_el_freno_lumbar_cierra_la_puerta(cfg):
    """El caso que motivó todo esto: día verde, sesión anterior completa, y
    ningún dato de lumbar porque no se rellenó el check-in."""
    abierta, motivo = gate(cfg, sig(LUNES))
    assert not abierta
    assert "lumbar_bloquea_todo" in motivo
    assert "lower_discomfort" in motivo, "hay que decir QUÉ señal falta"


def test_el_motivo_de_cierre_es_legible_no_un_codigo(cfg):
    _, motivo = gate(cfg, sig(LUNES))
    assert "no evaluable" in motivo
    assert "no se sube carga" in motivo


def test_con_la_lumbar_sana_la_puerta_se_abre(cfg):
    abierta, motivo = gate(cfg, sig(LUNES, lower_discomfort=1))
    assert abierta
    assert motivo == "puerta abierta"


def test_con_la_lumbar_cargada_el_freno_salta(cfg):
    abierta, motivo = gate(cfg, sig(LUNES, lower_discomfort=5))
    assert not abierta
    assert "lumbar_bloquea_todo" in motivo
    assert "= 5" in motivo, "hay que decir con qué valor saltó"


def test_el_rpe_de_ayer_ausente_no_bloquea_porque_es_opcional(cfg):
    """`on_missing: skip` en `rpe_alto`.

    Si el RPE de ayer bloqueara al faltar, la progresión no avanzaría nunca:
    es nulo siempre que ayer no se entrenó, o sea la mitad de los días.
    """
    abierta, _ = gate(cfg, sig(LUNES, lower_discomfort=2))
    assert abierta


def test_el_freno_lumbar_no_lleva_on_missing_skip(cfg):
    """Si alguien se lo pone algún día, que falle este test y lo piense.

    `skip` en la lumbar es exactamente el fallo que este fichero documenta.
    """
    frenos = {b["name"]: b for b in cfg.raw["progression"]["brakes"]}
    assert frenos["lumbar_bloquea_todo"].get("on_missing", "block") == "block"


def test_el_defecto_de_on_missing_es_bloquear(cfg_copia):
    """Quitar `on_missing` de un freno tiene que dejarlo en el lado seguro."""
    frenos = cfg_copia.raw["progression"]["brakes"]
    for b in frenos:
        b.pop("on_missing", None)
    # Sin RPE de ayer y sin lumbar: ahora AMBOS frenos cierran.
    abierta, motivo = gate(cfg_copia, sig(LUNES))
    assert not abierta
    assert "no evaluable" in motivo


def test_on_missing_skip_no_desactiva_el_freno_cuando_si_hay_dato(cfg):
    """`skip` cubre la ausencia, no el valor: con RPE 9 el freno salta."""
    señales = sig(LUNES, lower_discomfort=1, yesterday_rpe=9, yesterday_routine="dia_1")
    abierta, motivo = gate(cfg, señales)
    assert not abierta
    assert "rpe_alto" in motivo


# ---------------------------------------------------------------------------
# `blocks: last_session_only`
#
# Un RPE de 9 en la sesión de empuje no dice nada sobre la de pierna, y por eso
# el freno del RPE está escrito para acotarse a la rutina de la que habla. Solo
# que no se acotaba: `yesterday_routine` no la producía nadie, valía `None`
# siempre, y el `in (None, routine_key)` del motor se cumplía para cualquier
# rutina. `last_session_only` bloqueaba exactamente igual que `all`.
# ---------------------------------------------------------------------------


def test_el_rpe_alto_de_ayer_no_bloquea_la_rutina_de_hoy_si_es_otra(cfg):
    señales = sig(LUNES, lower_discomfort=1, yesterday_rpe=9, yesterday_routine="dia_2")
    abierta, motivo = gate(cfg, señales, routine="dia_1")
    assert abierta, motivo


def test_el_rpe_alto_bloquea_la_rutina_de_la_que_habla(cfg):
    señales = sig(LUNES, lower_discomfort=1, yesterday_rpe=9, yesterday_routine="dia_2")
    abierta, motivo = gate(cfg, señales, routine="dia_2")
    assert not abierta
    assert "rpe_alto" in motivo


def test_sin_saber_de_que_rutina_habla_el_rpe_alto_bloquea_todas(cfg):
    """`None` es el lado caro y reversible: no subir hoy se arregla mañana.

    Pasa de verdad -ayer sin entrenar, o dos rutinas distintas el mismo día- y
    entonces el deslizador no dice de cuál de ellas habla.
    """
    señales = sig(LUNES, lower_discomfort=1, yesterday_rpe=9)
    for rutina in ("dia_1", "dia_2", "dia_3"):
        abierta, _ = gate(cfg, señales, routine=rutina)
        assert not abierta, rutina


# ---------------------------------------------------------------------------
# Erratas en el operador
# ---------------------------------------------------------------------------


def test_un_operador_desconocido_en_un_freno_revienta_en_vez_de_callar(cfg_copia):
    """Antes: ninguna rama coincidía, `hit` se quedaba en False y el freno no
    saltaba jamás. Un freno de seguridad muerto por una letra."""
    cfg_copia.raw["progression"]["brakes"][1]["when"] = {"gtee": 4}
    with pytest.raises(RuleError, match="operador desconocido"):
        gate(cfg_copia, sig(LUNES, lower_discomfort=9))


def test_el_error_de_operador_dice_cuales_valen(cfg_copia):
    cfg_copia.raw["progression"]["brakes"][1]["when"] = {"mayor_que": 4}
    with pytest.raises(RuleError) as exc:
        gate(cfg_copia, sig(LUNES, lower_discomfort=9))
    assert "gte" in str(exc.value)


# ---------------------------------------------------------------------------
# El resto de la puerta sigue funcionando
# ---------------------------------------------------------------------------


def test_el_ambar_cierra_la_puerta_antes_de_mirar_los_frenos(cfg):
    abierta, motivo = gate(cfg, sig(LUNES, lower_discomfort=1), light="amber")
    assert not abierta
    assert "semáforo" in motivo


def test_sin_registro_de_la_ultima_sesion_no_se_progresa(cfg):
    abierta, motivo = gate(cfg, sig(LUNES, lower_discomfort=1), compliance=None)
    assert not abierta
    assert "no hay registro" in motivo


def test_el_motivo_nombra_a_los_ejercicios_sin_registro(cfg):
    """Un aviso que nombra su causa se arregla desde el móvil.

    Con una rutina en marcha y un ejercicio nuevo, "no hay registro" a secas
    es un callejón sin salida: nueve ejercicios y ninguna pista de cuál.
    """
    abierta, motivo = evaluate_gate(
        cfg.raw["progression"], "green", sig(LUNES, lower_discomfort=1),
        None, "dia_1", False,
        compliance_por_ejercicio={
            "prensa_horizontal": True,
            "gemelo_sentado": True,
            "hip_thrust_barra": None,
        },
    )
    assert not abierta
    assert "hip_thrust_barra" in motivo
    assert "prensa_horizontal" not in motivo, "solo los que faltan, no la lista entera"


def test_con_la_rutina_entera_sin_estrenar_no_se_listan_los_nueve(cfg):
    """El primer arranque. Nombrarlos a todos es ruido, no información: son
    todos, y el mensaje de Telegram no es un volcado del config."""
    _, motivo = evaluate_gate(
        cfg.raw["progression"], "green", sig(LUNES, lower_discomfort=1),
        None, "dia_1", False,
        compliance_por_ejercicio={"prensa_horizontal": None, "gemelo_sentado": None},
    )
    assert motivo == "no hay registro de la última sesión con el que comparar"


def test_sin_detalle_el_motivo_sigue_siendo_el_de_siempre(cfg):
    """`compliance_por_ejercicio` es opcional: quien no lo pase no se rompe."""
    _, motivo = gate(cfg, sig(LUNES, lower_discomfort=1), compliance=None)
    assert motivo == "no hay registro de la última sesión con el que comparar"


def test_una_rutina_sin_ejercicios_no_abre_la_puerta_por_vacuidad(cfg):
    """`all([])` es True, y ahí está la trampa.

    Si el cumplimiento global se resolviera con `all()` a secas, "no hay nada
    que comprobar" se convertiría en "todo comprobado y correcto". Es la misma
    confusión entre vacío y conforme que este arreglo persigue.

    El config ya NO deja arrancar con una rutina vacía -`_validate` la rechaza
    desde que se cerró la lista blanca de claves de rutina-, así que este caso
    no llega por el YAML. Se mantiene igualmente: aquí se entra con el `raw`
    en la mano desde el planificador y desde los tests, y la defensa que
    importa es la de la función, no la de la puerta de entrada.
    """
    raw = copy.deepcopy(cfg.raw)
    raw["routines"]["dia_1"]["exercises"] = []
    plan = plan_progression(
        raw, "dia_1", sig(LUNES, lower_discomfort=1), "green", compliance={},
    )
    assert not plan.gate_open
    assert "no hay registro" in plan.gate_reason


def test_en_semana_de_descarga_la_progresion_esta_congelada(cfg):
    abierta, motivo = evaluate_gate(
        cfg.raw["progression"], "green", sig(LUNES, lower_discomfort=1),
        True, "dia_1", True,
    )
    assert not abierta
    assert "descarga" in motivo
