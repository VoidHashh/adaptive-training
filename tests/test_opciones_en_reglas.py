"""Umbrales escritos una sola vez: el operador `_option`.

Este fichero nace de la tercera opción muerta de la tanda. `cycling.weekend`
declaraba dos umbrales, con comentarios de calibración largos explicando por qué
valían lo que valían:

    total_hours_threshold: 2.5      # "ajustado a un fin de semana de 1-2,8 h"
    intense_rides_threshold: 1      # "con 2 la regla queda muerta"

y la regla que decide el lunes, `resaca_finde`, comparaba contra sus propios
literales: `{gte: 1}` y `{gt: 4.0}`. Ninguna línea de Python leía la sección.
El de las salidas coincidía por casualidad; el de las horas no: 4 h no se
alcanzan nunca con el volumen actual, así que esa mitad de la regla llevaba
semanas sin poder disparar. Medido con el `config.yaml` real: con el umbral
vivo, un fin de semana de 2,6 h ya deja el lunes en ámbar; con el literal de
4,0 hacían falta 4 horas largas que no aparecen en el histórico.

No es que el número estuviera mal puesto. Es que había dos números y solo uno
decidía, y el que decidía no era el documentado.

Las invariantes que fija este fichero:
  1. La regla lee el umbral de la sección. Cambiar la sección cambia el lunes.
  2. En la regla no queda ningún literal: si alguien vuelve a escribirlo, se
     nota aquí y no dentro de tres semanas.
  3. Una ruta que no existe es un ERROR, nunca un dato que falta. Un umbral
     adaptativo puede valer `None` por poco historial y entonces la regla se
     salta; una opción ausente significa que el YAML está mal, y saltarse la
     regla en silencio sería repetir el fallo con otro disfraz.
  4. Ese error salta al ARRANCAR, no a las 06:30 con el proceso decidiendo.
"""

from __future__ import annotations

import copy

import pytest

from app.config_loader import _validate
from app.engine.rules import (
    FIRED,
    NOT_FIRED,
    SKIPPED,
    RuleError,
    evaluate_light,
    evaluate_rule,
    resolve_option,
)

from tests.conftest import LUNES, sig, sig_completa

RUTA_HORAS = "cycling.weekend.total_hours_threshold"
RUTA_SALIDAS = "cycling.weekend.intense_rides_threshold"


def regla_finde(data) -> dict:
    return next(r for r in data["thresholds"]["amber"] if r["name"] == "resaca_finde")


def semaforo(cfg, **overrides) -> str:
    return evaluate_light(cfg, sig_completa(LUNES, **overrides)).light


# ---------------------------------------------------------------------------
# 1. El umbral de la sección es el que decide
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "horas,esperado",
    [(1.0, "green"), (2.4, "green"), (2.5, "green"), (2.6, "amber"), (3.5, "amber")],
)
def test_el_lunes_lo_decide_el_umbral_documentado_y_no_un_literal(cfg, horas, esperado):
    """2,5 h es el umbral escrito en `cycling.weekend`, y ahora es el que manda.

    El corte cae ENTRE 2,5 y 2,6 porque el operador es `gt` y no `gte`: eso no
    ha cambiado, se ha conservado tal cual estaba. Lo que ha cambiado es contra
    qué número compara. Con el literal de 4,0, las cinco filas de este test
    salían verdes y la regla no existía en la práctica.
    """
    assert semaforo(cfg, weekend_total_hours=horas) == esperado


@pytest.mark.parametrize("salidas,esperado", [(0, "green"), (1, "amber"), (2, "amber")])
def test_una_salida_intensa_basta_y_tambien_sale_de_la_seccion(cfg, salidas, esperado):
    """El umbral de salidas coincidía por casualidad con el literal.

    Coincidir no es estar conectado: el comentario de la sección explica que se
    bajó de 2 a 1 tras arreglar el clasificador, y si alguien lo vuelve a subir
    a 2 leyendo ese comentario, antes no habría pasado nada.
    """
    assert semaforo(cfg, weekend_intense_rides=salidas) == esperado


def test_mover_el_umbral_de_horas_mueve_el_lunes(cfg_copia):
    """La prueba de que se lee de verdad: sube el techo y el ámbar desaparece.

    Un fin de semana de 3,5 h deja el lunes en ámbar con el umbral actual. Con
    el umbral en 6 h deja de hacerlo, sin tocar la regla.
    """
    assert semaforo(cfg_copia, weekend_total_hours=3.5) == "amber"
    cfg_copia.raw["cycling"]["weekend"]["total_hours_threshold"] = 6.0
    assert semaforo(cfg_copia, weekend_total_hours=3.5) == "green"


def test_mover_el_umbral_de_salidas_mueve_el_lunes(cfg_copia):
    assert semaforo(cfg_copia, weekend_intense_rides=1) == "amber"
    cfg_copia.raw["cycling"]["weekend"]["intense_rides_threshold"] = 2
    assert semaforo(cfg_copia, weekend_intense_rides=1) == "green"


# ---------------------------------------------------------------------------
# 2. En la regla no queda ningún literal
# ---------------------------------------------------------------------------


def test_la_regla_del_lunes_no_lleva_ningun_umbral_escrito_a_mano(cfg):
    """"Conecta la opción y borra el literal": esto vigila la segunda mitad.

    Mientras existan las dos copias, la que decide es la de la regla y la que
    se lee es la de la sección. Da igual que hoy coincidan: el fallo no es que
    los números difieran, es que puedan hacerlo sin que nada lo diga.
    """
    ramas = regla_finde(cfg.raw)["when"]["any"]
    operadores = [op for rama in ramas for cond in rama.values() for op in cond]
    assert operadores == ["gte_option", "gt_option"], (
        f"la regla `resaca_finde` compara con {operadores}: alguno es un umbral "
        f"escrito a mano dentro de la regla, y entonces el de `cycling.weekend` "
        f"vuelve a ser un número que no lee nadie."
    )


def test_las_dos_rutas_apuntan_a_la_seccion_del_fin_de_semana(cfg):
    ramas = regla_finde(cfg.raw)["when"]["any"]
    rutas = [operando for rama in ramas for cond in rama.values() for operando in cond.values()]
    assert rutas == [RUTA_SALIDAS, RUTA_HORAS]


# ---------------------------------------------------------------------------
# 3. Una opción que falta es un error, no un hueco en los datos
# ---------------------------------------------------------------------------


def test_una_ruta_inexistente_revienta_en_vez_de_saltarse_la_regla(cfg):
    """La diferencia con `_adaptive`, que es la razón de ser de esta rama.

    Si esto se saltara la regla, el lunes saldría verde con un `skipped` en el
    log y todo el mundo tan contento: exactamente el mismo final que tenía el
    literal de 4,0 h, con la misma cara de normalidad.
    """
    regla = {
        "name": "inventada",
        "when": {"weekend_total_hours": {"gt_option": "cycling.weekend.no_existe"}},
    }
    with pytest.raises(RuleError, match="no existe en config.yaml"):
        evaluate_rule(regla, sig_completa(LUNES), options=cfg.raw)


def test_un_umbral_adaptativo_sin_datos_si_se_salta(cfg):
    """El contraste. No es incoherencia: son dos cosas distintas.

    Sin historial suficiente, un percentil vale `None` y eso es un día normal
    de las primeras semanas. Una ruta que no existe no se arregla esperando.
    """
    regla = {
        "name": "inventada",
        "when": {"load_3d": {"gt_adaptive": "load_3d_p99"}},
    }
    res = evaluate_rule(regla, sig(LUNES, load_3d=100.0), options=cfg.raw)
    assert res.status == SKIPPED
    assert "load_3d_p99" in res.missing


@pytest.mark.parametrize(
    "valor,motivo",
    [
        ("cycling.weekend.days", "es una lista"),
        ("cycling.load.source", "es un texto"),
        ("cycling", "es una sección entera"),
        ("set_types.write_warmup_type_to_hevy", "es un booleano"),
    ],
)
def test_una_ruta_que_no_apunta_a_un_numero_revienta(cfg, valor, motivo):
    """El booleano es el peligroso de los cuatro.

    `True` es un `int` en Python, así que `2.5 > True` no da error: da `True`,
    y la regla dispara todos los lunes por un motivo inventado. Los otros tres
    fallarían solos con un TypeError; este no, y por eso está excluido a mano.
    """
    with pytest.raises(RuleError, match="tiene que ser un número"):
        resolve_option(valor, cfg.raw)


def test_sin_config_delante_tampoco_se_lo_inventa():
    with pytest.raises(RuleError, match="no se ha pasado el config"):
        resolve_option(RUTA_HORAS, None)


def test_el_camino_bueno_devuelve_el_numero(cfg):
    assert resolve_option(RUTA_HORAS, cfg.raw) == 2.5
    assert resolve_option(RUTA_SALIDAS, cfg.raw) == 1


def test_la_rama_que_no_dispara_no_dice_nada_pero_tampoco_falla(cfg):
    """Un fin de semana tranquilo: la regla se evalúa entera y sale que no."""
    res = evaluate_rule(
        regla_finde(cfg.raw), sig_completa(LUNES), level="amber", options=cfg.raw
    )
    assert res.status == NOT_FIRED
    assert res.missing == []


def test_cuando_dispara_el_detalle_dice_el_numero_y_de_donde_sale(cfg):
    """El log tiene que poder explicar el ámbar tres semanas después, y para eso
    no basta con el valor: hace falta saber contra qué se comparó."""
    res = evaluate_rule(
        regla_finde(cfg.raw),
        sig_completa(LUNES, weekend_total_hours=3.5),
        level="amber",
        options=cfg.raw,
    )
    assert res.status == FIRED
    assert any("3.5 gt 2.5" in d and RUTA_HORAS in d for d in res.detail), res.detail


# ---------------------------------------------------------------------------
# 4. Y el error salta al arrancar, no por la mañana
# ---------------------------------------------------------------------------


def _con_when(cfg_copia, when: dict) -> dict:
    data = cfg_copia.raw
    regla_finde(data)["when"] = when
    return data


def test_una_ruta_rota_impide_arrancar(cfg_copia):
    data = _con_when(
        cfg_copia, {"weekend_total_hours": {"gt_option": "cycling.weekend.total_hours"}}
    )
    assert "no existe en config.yaml" in "\n".join(_validate(data))


def test_una_ruta_que_apunta_a_algo_que_no_es_numero_impide_arrancar(cfg_copia):
    data = _con_when(
        cfg_copia, {"weekend_total_hours": {"gt_option": "cycling.weekend.days"}}
    )
    assert "tiene que ser un número" in "\n".join(_validate(data))


def test_un_operador_mal_escrito_en_una_regla_impide_arrancar(cfg_copia):
    """Antes esto no lo miraba nadie: `check_ops` solo pasaba por los frenos y
    los disparadores de las reglas especiales, nunca por las del semáforo. Un
    `gte_optionn` levantaba `RuleError` a las 06:30, con el proceso ya en
    marcha, que es el peor momento posible para enterarse."""
    data = _con_when(cfg_copia, {"weekend_total_hours": {"gte_optionn": RUTA_HORAS}})
    assert "operador desconocido" in "\n".join(_validate(data))


def test_un_percentil_inexistente_impide_arrancar_con_cualquier_operador(cfg_copia):
    """El validador viejo solo comprobaba `gt_adaptive`, literal.

    `lt_adaptive: load_3d_p99` pasaba la validación tan campante y se saltaba la
    regla todos los días. Es el mismo agujero de siempre en su versión más
    tonta: la comprobación estaba escrita para un operador de los cuatro.
    """
    for op in ("gt_adaptive", "gte_adaptive", "lt_adaptive", "lte_adaptive"):
        data = _con_when(copy.deepcopy(cfg_copia), {"load_3d": {op: "no_existe_p99"}})
        assert "adaptive_thresholds" in "\n".join(_validate(data)), op


def test_el_config_real_sigue_siendo_valido(cfg):
    assert _validate(cfg.raw) == []
