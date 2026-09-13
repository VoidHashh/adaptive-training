"""Umbrales escritos una sola vez: el operador `_option`.

Este fichero nació de la tercera opción muerta de la tanda. `cycling.weekend`
declaraba dos umbrales, con comentarios de calibración largos explicando por qué
valían lo que valían:

    total_hours_threshold: 2.5      # "ajustado a un fin de semana de 1-2,8 h"
    intense_rides_threshold: 1      # "con 2 la regla queda muerta"

y la regla que decidía el lunes, `resaca_finde`, comparaba contra sus propios
literales: `{gte: 1}` y `{gt: 4.0}`. Ninguna línea de Python leía la sección. El
de las salidas coincidía por casualidad; el de las horas no: 4 h no se alcanzan
nunca con el volumen actual, así que esa mitad de la regla llevaba semanas sin
poder disparar.

QUÉ HA CAMBIADO, Y POR QUÉ ESTE FICHERO SIGUE AQUÍ
--------------------------------------------------
`resaca_finde` se ha borrado entera, y con ella sus dos umbrales. Frenaba por
horas de bici -una cuenta, no una señal del cuerpo- y era el único freno por
cuenta que llegaba hasta la sesión de fuerza. Sobre 180 días disparó dos veces
como única causa del ámbar con la HRV en 1,62 y la carga de tres días a un
cuarto del propio p90.

Eso deja al operador `_option` sin ningún usuario en el `config.yaml` real, y la
tentación sería borrarlo también. No se borra, y conviene dejar dicho por qué:
`_option` no es una opción muerta, es la CURA de las opciones muertas. El día que
vuelva a hacer falta un umbral que viva en su sección documentada en vez de como
literal dentro de una regla, o está este operador o se escribe el número dos
veces, que es exactamente de donde viene todo esto.

Un operador sin usuarios y sin tests sí sería código muerto. Por eso los tests de
aquí abajo dejaron de colgar de `resaca_finde` -que ya no existe- y trabajan
contra reglas sintéticas: prueban el mecanismo, que es lo que se conserva.

Las invariantes que fija este fichero:
  1. Una regla que declara `_option` lee el número de la sección, y cambiar la
     sección cambia lo que decide la regla.
  2. Una ruta que no existe es un ERROR, nunca un dato que falta. Un umbral
     adaptativo puede valer `None` por poco historial y entonces la regla se
     salta; una opción ausente significa que el YAML está mal, y saltarse la
     regla en silencio sería repetir el fallo con otro disfraz.
  3. Ese error salta al ARRANCAR, no a las 06:30 con el proceso decidiendo.
  4. `resaca_finde` y sus dos umbrales no pueden volver a entrar en silencio.
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

# Una ruta que de verdad apunta a un número en el config real. Antes aquí había
# `cycling.weekend.total_hours_threshold`; se fue con la regla que lo leía, y
# apuntar a un sitio que ya no existe convertiría a estos tests en verdes por el
# motivo contrario al que los hizo nacer.
#
# Se ha elegido una ruta HONDA a propósito -cuatro tramos- y con unidades que
# pegan con la señal contra la que se compara: así el caminante de rutas se
# prueba de verdad y la regla de laboratorio se puede leer sin tener que
# recordar que los números no significan nada.
RUTA_NUMERO = "cycling.load.fallback_estimate.load_per_hour.intensa"


def regla_sintetica(ruta: str = RUTA_NUMERO, op: str = "gt_option") -> dict:
    """Una regla de laboratorio, no una del YAML.

    Colgar estos tests de una regla real fue lo que hizo que el fichero entero
    se quedara sin sentido al borrarla. Lo que se prueba aquí es la gramática, y
    la gramática no depende de que hoy exista una regla que la use.
    """
    return {
        "name": "sintetica",
        "description": "regla de laboratorio para el operador _option",
        "when": {"load_3d": {op: ruta}},
    }


# ---------------------------------------------------------------------------
# 1. El umbral de la sección es el que decide
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "valor,esperado",
    [(100.0, NOT_FIRED), (160.0, NOT_FIRED), (161.0, FIRED), (400.0, FIRED)],
)
def test_la_regla_compara_contra_el_numero_de_la_seccion(cfg, valor, esperado):
    """El corte cae exactamente donde dice la sección, y ni un punto antes.

    El 160 no se afirma a mano: se lee de la misma ruta que lee la regla, y la
    primera línea comprueba que el caso de prueba sigue siendo el que se quería
    probar. Un test que repitiera el número sería la tercera copia del mismo
    valor, y este fichero existe precisamente porque dos ya eran demasiadas.
    """
    umbral = resolve_option(RUTA_NUMERO, cfg.raw)
    assert (valor > umbral) == (esperado == FIRED), "el caso de prueba está mal elegido"
    res = evaluate_rule(regla_sintetica(), sig(LUNES, load_3d=valor), options=cfg.raw)
    assert res.status == esperado


def test_mover_el_umbral_en_la_seccion_mueve_lo_que_decide_la_regla(cfg_copia):
    """La prueba de que se lee de verdad: sube el techo y el disparo desaparece.

    Sin esto, un `_option` podría estar resolviendo contra una copia congelada al
    arrancar y nadie lo notaría: el número saldría bien en los dos sitios y
    seguiría decidiendo el viejo.
    """
    senales = sig(LUNES, load_3d=200.0)
    assert evaluate_rule(regla_sintetica(), senales, options=cfg_copia.raw).status == FIRED
    cfg_copia.raw["cycling"]["load"]["fallback_estimate"]["load_per_hour"]["intensa"] = 300
    assert evaluate_rule(regla_sintetica(), senales, options=cfg_copia.raw).status == NOT_FIRED


def test_el_detalle_dice_el_numero_y_de_donde_sale(cfg):
    """El log tiene que poder explicar un ámbar tres semanas después, y para eso
    no basta con el valor: hace falta saber contra qué se comparó y por qué ese
    número era ese. La ruta es lo que lleva al comentario que lo justifica."""
    res = evaluate_rule(
        regla_sintetica(), sig(LUNES, load_3d=400.0), level="amber", options=cfg.raw
    )
    assert res.status == FIRED
    assert any("400 gt 160" in d and RUTA_NUMERO in d for d in res.detail), res.detail


# ---------------------------------------------------------------------------
# 2. Una opción que falta es un error, no un hueco en los datos
# ---------------------------------------------------------------------------


def test_una_ruta_inexistente_revienta_en_vez_de_saltarse_la_regla(cfg):
    """La diferencia con `_adaptive`, que es la razón de ser de esta rama.

    Si esto se saltara la regla, el día saldría verde con un `skipped` en el log
    y todo el mundo tan contento: exactamente el mismo final que tenía el
    literal de 4,0 h de `resaca_finde`, con la misma cara de normalidad.
    """
    with pytest.raises(RuleError, match="no existe en config.yaml"):
        evaluate_rule(
            regla_sintetica("cycling.fetch.no_existe"),
            sig_completa(LUNES),
            options=cfg.raw,
        )


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
    y la regla dispara todos los días por un motivo inventado. Los otros tres
    fallarían solos con un TypeError; este no, y por eso está excluido a mano.
    """
    with pytest.raises(RuleError, match="tiene que ser un número"):
        resolve_option(valor, cfg.raw)


def test_sin_config_delante_tampoco_se_lo_inventa():
    with pytest.raises(RuleError, match="no se ha pasado el config"):
        resolve_option(RUTA_NUMERO, None)


def test_el_camino_bueno_devuelve_el_numero(cfg):
    esperado = cfg.raw["cycling"]["load"]["fallback_estimate"]["load_per_hour"]["intensa"]
    assert resolve_option(RUTA_NUMERO, cfg.raw) == esperado


# ---------------------------------------------------------------------------
# 3. Y el error salta al arrancar, no por la mañana
# ---------------------------------------------------------------------------


def _con_regla(cfg_copia, when: dict) -> dict:
    """Mete una regla sintética en `thresholds.amber` del config copiado.

    Antes esto reescribía el `when` de `resaca_finde`. Ahora se añade una regla
    nueva, que además prueba algo que aquella versión no podía probar: que la
    validación pasa por TODAS las reglas y no solo por las que ya conocía.
    """
    data = cfg_copia.raw
    data["thresholds"]["amber"].append({"name": "sintetica", "when": when})
    return data


def test_una_ruta_rota_impide_arrancar(cfg_copia):
    data = _con_regla(cfg_copia, {"load_3d": {"gt_option": "cycling.fetch.no_existe"}})
    assert "no existe en config.yaml" in "\n".join(_validate(data))


def test_una_ruta_que_apunta_a_algo_que_no_es_numero_impide_arrancar(cfg_copia):
    data = _con_regla(cfg_copia, {"load_3d": {"gt_option": "cycling.weekend.days"}})
    assert "tiene que ser un número" in "\n".join(_validate(data))


def test_un_operador_mal_escrito_en_una_regla_impide_arrancar(cfg_copia):
    """Antes esto no lo miraba nadie: `check_ops` solo pasaba por los frenos y
    los disparadores de las reglas especiales, nunca por las del semáforo. Un
    `gte_optionn` levantaba `RuleError` a las 06:30, con el proceso ya en
    marcha, que es el peor momento posible para enterarse."""
    data = _con_regla(cfg_copia, {"load_3d": {"gte_optionn": RUTA_NUMERO}})
    assert "operador desconocido" in "\n".join(_validate(data))


def test_un_percentil_inexistente_impide_arrancar_con_cualquier_operador(cfg_copia):
    """El validador viejo solo comprobaba `gt_adaptive`, literal.

    `lt_adaptive: load_3d_p99` pasaba la validación tan campante y se saltaba la
    regla todos los días. Es el mismo agujero de siempre en su versión más
    tonta: la comprobación estaba escrita para un operador de los cuatro.
    """
    for op in ("gt_adaptive", "gte_adaptive", "lt_adaptive", "lte_adaptive"):
        data = _con_regla(copy.deepcopy(cfg_copia), {"load_3d": {op: "no_existe_p99"}})
        assert "adaptive_thresholds" in "\n".join(_validate(data)), op


# ---------------------------------------------------------------------------
# 4. `resaca_finde` no vuelve
# ---------------------------------------------------------------------------
#
# Se borra una regla que llevaba meses decidiendo lunes, y el riesgo no es el
# borrado: es que dentro de tres meses alguien -yo- lea el comentario de
# `cycling.weekend` y la reponga a medias. Con los dos umbrales pero sin la
# regla, o con la regla pero contra literales otra vez. Las dos mitades por
# separado son silenciosas: la primera no hace nada y parece que sí, la segunda
# hace algo distinto de lo que está escrito.


def test_la_regla_del_lunes_ya_no_esta_en_el_config_real(cfg):
    nombres = [
        r["name"] for luz in ("red", "amber") for r in cfg.raw["thresholds"][luz]
    ]
    assert "resaca_finde" not in nombres


def test_reponer_la_regla_impide_arrancar(cfg_copia):
    """Y con un motivo que se pueda leer sin abrir el git log.

    El mensaje lleva los números que la mataron y adónde ir si de verdad hace
    falta frenar por carga. Un "clave desconocida" a secas invitaría a insistir.
    """
    cfg_copia.raw["thresholds"]["amber"].append(
        {
            "name": "resaca_finde",
            "only_on_weekday": ["monday"],
            "when": {"weekend_total_hours": {"gt": 2.5}},
        }
    )
    errores = "\n".join(_validate(cfg_copia.raw))
    assert "resaca_finde ya no existe" in errores
    assert "carga_acumulada" in errores, "hay que decir por dónde se frena de verdad"


@pytest.mark.parametrize(
    "muerto,valor", [("total_hours_threshold", 2.5), ("intense_rides_threshold", 1)]
)
def test_reponer_solo_los_umbrales_tampoco_pasa(cfg_copia, muerto, valor):
    """Esta es la mitad peligrosa de las dos.

    Escribir el umbral sin la regla no frena nada, pero deja en el fichero un
    número con pinta de decidir el lunes. Se saldría a rodar el domingo contando
    con un freno que no existe, que es peor que no tenerlo.
    """
    cfg_copia.raw["cycling"]["weekend"][muerto] = valor
    errores = "\n".join(_validate(cfg_copia.raw))
    assert f"cycling.weekend.{muerto} ya no lo lee nadie" in errores


def test_el_fin_de_semana_solo_dice_ya_que_dias_son(cfg):
    """Lo que queda de `cycling.weekend` es una lista de días, y nada más.

    Sigue habiendo quien la lee -el resumen del fin de semana y la ventana del
    `bike_advisor`-, así que la sección no se borra. Lo que no puede tener es un
    umbral suelto esperando a que alguien lo conecte.
    """
    assert set(cfg.raw["cycling"]["weekend"]) == {"days"}


def test_el_lunes_ya_no_sale_ambar_por_haber_rodado_el_fin_de_semana(cfg):
    """El caso concreto que se ha quitado, escrito como caso.

    Nueve horas de bici el fin de semana y una salida intensa: con
    `resaca_finde` esto era ámbar el lunes -menos series, sin progresión y sin
    HIIT- con la HRV alta y el pulso de reposo por debajo de la media. Ahora es
    verde, y si el cuerpo estuviera fundido lo diría `carga_acumulada`.
    """
    luz = evaluate_light(
        cfg,
        sig_completa(LUNES, weekend_total_hours=9.06, weekend_intense_rides=1),
    )
    assert luz.light == "green", luz.fired_names()


def test_las_horas_del_fin_de_semana_se_siguen_calculando(cfg):
    """Quitar el freno no es dejar de mirar.

    La señal sigue existiendo y sigue guardándose en la decisión; lo único que
    ha cambiado es que ya no la lee ninguna regla. El día que se quiera saber
    qué se hizo ese fin de semana, el dato está.
    """
    s = sig_completa(LUNES, weekend_total_hours=9.06, weekend_intense_rides=1)
    assert s.get("weekend_total_hours") == 9.06
    assert s.get("weekend_intense_rides") == 1


def test_el_config_real_sigue_siendo_valido(cfg):
    assert _validate(cfg.raw) == []
