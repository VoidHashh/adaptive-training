"""Un verde decidido sin datos no es un verde.

QUÉ PASABA
----------
El 15 de septiembre de 2026 el contenedor levantó a las 06:22, el check-in
llegó a las 06:23 y la decisión se escribió a las 06:23:33. Garmin todavía no
tenía la noche, así que `hrv`, `sleep_min` y compañía llegaron vacíos y las
reglas que dependían de ellos se SALTARON. No dispararon. Y como el semáforo es
«si no dispara nada, verde», el día salió verde.

Ese verde decía «estás bien» cuando lo único que se podía decir era «no lo sé».
Son dos cosas distintas y se pintaban del mismo color, que es la avería de fondo
de este proyecto -un valor que se lee y que no es el valor que se usa- en su
versión más cara: aquí el dato leído gobierna la sesión que se levanta.

QUÉ SE ATA AQUÍ
---------------
Que ese día sale ÁMBAR, que el ámbar se identifica como tal -`ambar_sin_datos`,
no una regla del cuerpo-, que el mensaje lo explica con esas palabras y sin
depender de `include_reasoning`, y que cuando el dato llega y el día se
recalcula a verde, el cambio se anuncia como anulación exactamente igual que ya
se anunciaba al revés.

Y, con el mismo cuidado, lo que NO se toca: un rojo, un ámbar de verdad, un día
completo, y un verde al que solo le falta el check-in.
"""

from __future__ import annotations

import pytest

from app.config_loader import ConfigError, load_config
from app.engine.decision import EngineState, decide
from app.engine.message import render_telegram
from app.engine.rules import (
    MEDIDAS_DEL_RELOJ,
    REGLA_SIN_DATOS,
    evaluate_light,
)
from tests.conftest import LUNES, sig, sig_completa


# Las señales del formulario, sin nada del reloj. Es la mañana del 15 de
# septiembre: el usuario contestó, Garmin no.
SOLO_CHECKIN = {
    "lower_discomfort": 1,
    "upper_discomfort": 1,
    "fatigue": 3,
    "training_desire": 8,
}


def _luz(cfg, **valores):
    return evaluate_light(cfg, sig(LUNES, **valores))


# ---------------------------------------------------------------------------
# El caso
# ---------------------------------------------------------------------------


def test_la_manana_a_ciegas_sale_ambar_y_no_verde(cfg):
    """El 2026-09-15, clavado."""
    d = _luz(cfg, **SOLO_CHECKIN)

    assert d.light == "amber", (
        "no dispara ninguna regla porque no se han podido evaluar, que no es "
        "lo mismo que porque el cuerpo esté bien"
    )
    assert d.trigger_rule == REGLA_SIN_DATOS


def test_el_disparo_dice_qué_medidas_faltaban(cfg):
    """`missing` es lo que el mensaje va a nombrar, así que tiene que ser cierto."""
    d = _luz(cfg, **SOLO_CHECKIN)
    aviso = next(r for r in d.fired if r.name == REGLA_SIN_DATOS)

    assert aviso.missing, "un ámbar por precaución sin decir por qué no vale"
    assert set(aviso.missing) <= MEDIDAS_DEL_RELOJ, (
        f"solo se nombran medidas del reloj, y aquí hay otras: {aviso.missing}"
    )
    assert "hrv" in aviso.missing
    assert "sleep_min" in aviso.missing


def test_el_disparo_dice_qué_reglas_se_quedaron_mudas(cfg):
    """Las medidas son para el usuario; las reglas, para depurar en noviembre."""
    d = _luz(cfg, **SOLO_CHECKIN)
    aviso = next(r for r in d.fired if r.name == REGLA_SIN_DATOS)

    detalle = " ".join(aviso.detail)
    assert "hrv_baja_1d" in detalle
    assert "sueno_muy_corto" in detalle


def test_el_ambar_por_precaucion_no_contamina_las_reglas_saltadas(cfg):
    """`skipped` es de donde el scheduler deduce si vale la pena repreguntar.

    Si el aviso sintético cayera ahí dentro, `_medidas_que_faltaban` lo leería
    como una regla más y el conjunto de medidas a rescatar seguiría siendo el
    mismo por pura casualidad. Pero el día que el aviso cambie de forma, la
    recomputación se rompería en un sitio que nadie relaciona con esto.
    """
    d = _luz(cfg, **SOLO_CHECKIN)
    assert REGLA_SIN_DATOS not in [r.name for r in d.skipped]


# ---------------------------------------------------------------------------
# Lo que NO se toca
# ---------------------------------------------------------------------------


def test_un_dia_completo_sigue_siendo_verde(cfg):
    """El contraste. Sin esto, el arreglo podría ser «todo ámbar siempre» y los
    demás tests pasarían igual."""
    d = evaluate_light(cfg, sig_completa(LUNES))
    assert d.light == "green"
    assert d.trigger_rule is None
    assert REGLA_SIN_DATOS not in [r.name for r in d.fired]


def test_un_rojo_sigue_siendo_rojo_aunque_falte_medio_reloj(cfg):
    """La promoción solo corrige el verde. Un rojo ya dice lo que hay que decir,
    y cambiarle el disparo a `ambar_sin_datos` sería, literalmente, rebajarlo."""
    d = _luz(cfg, lower_discomfort=7)
    assert d.light == "red"
    assert d.trigger_rule == "lumbar_alto"


def test_un_ambar_de_verdad_conserva_su_propia_regla(cfg):
    """Y este es el que importa de los dos.

    Si la precaución pisara el `trigger_rule` de un ámbar que ya había
    disparado, el histórico diría «ámbar por falta de datos» los días en que en
    realidad fue la cervical. El recuento de reglas del panel y la tendencia
    salen de ahí.
    """
    d = _luz(cfg, upper_discomfort=6)
    assert d.light == "amber"
    assert d.trigger_rule == "cervicales_hombros"
    assert REGLA_SIN_DATOS not in [r.name for r in d.fired]


def test_un_verde_sin_checkin_pero_con_reloj_sigue_verde(cfg):
    """La mitad del problema que esto NO resuelve, escrita para que se vea.

    Sin check-in, `lumbar_medio`, `cervicales_hombros` y `cansancio_alto` se
    saltan las tres, y por el mismo principio ese verde también es ciego. No se
    promueve a propósito: no hay quien rescate un check-in que no se rellenó, o
    sea que ese ámbar no tendría camino de vuelta a verde y se quedaría puesto
    para siempre. Un ámbar del que no se sale no avisa, castiga.

    Si algún día se decide promoverlo también, este test es el que hay que
    cambiar, y su docstring dice contra qué argumento.
    """
    d = evaluate_light(
        cfg,
        sig_completa(
            LUNES,
            lower_discomfort=None, upper_discomfort=None,
            fatigue=None, training_desire=None,
        ),
    )
    assert d.skipped, "la premisa: sin check-in hay reglas que no se evalúan"
    assert d.light == "green"


def test_una_base_que_falta_no_es_un_reloj_que_falta(cfg):
    """`hrv_baseline` ausente con `hrv` presente NO promueve.

    Es el mismo criterio que ya aplica `_medidas_que_faltaban`: una base que
    falta le faltan días de historia, y eso no lo arregla que llegue la noche de
    hoy. Si contara, las dos primeras semanas de una instalación nueva serían
    ámbar todas, sin que llegar a la séptima mañana cambiara nada.
    """
    s = sig_completa(LUNES)
    for clave in ("hrv_baseline", "hrv_ratio"):
        s.values[clave] = None
        s.history.pop(clave, None)

    d = evaluate_light(cfg, s)
    assert any(r.name == "hrv_baja_1d" for r in d.skipped), "la premisa"
    assert d.light == "green"


# ---------------------------------------------------------------------------
# El interruptor
# ---------------------------------------------------------------------------


def test_apagarlo_devuelve_el_comportamiento_de_antes(cfg_copia):
    cfg_copia.raw["thresholds"]["ambar_sin_datos"] = False
    d = evaluate_light(cfg_copia, sig(LUNES, **SOLO_CHECKIN))
    assert d.light == "green"


def test_encendido_es_el_defecto_aunque_no_esté_la_clave(cfg_copia):
    """Un `config.yaml` viejo -o el de otra instalación- no se queda sin la
    precaución por omisión. Lo que hay que escribir para no tenerla es
    `false`, que deja constancia."""
    cfg_copia.raw["thresholds"].pop("ambar_sin_datos", None)
    d = evaluate_light(cfg_copia, sig(LUNES, **SOLO_CHECKIN))
    assert d.light == "amber"


def test_el_config_real_lo_tiene_encendido(cfg):
    """Porque es una decisión del usuario, no un defecto del código.

    Sin esto, el defecto del motor bastaría para que todo lo demás pasara, y el
    día que alguien escribiera `false` en el fichero no se caería ni un test:
    solo dejarían de llegar los ámbares.
    """
    assert cfg.raw["thresholds"]["ambar_sin_datos"] is True


def test_un_interruptor_que_no_es_booleano_no_arranca(cfg_copia, tmp_path):
    """`ambar_sin_datos: "no"` se comportaría como encendido.

    La lectura es `.get(clave, True)`, así que cualquier cosa que no sea `False`
    deja la precaución puesta. Escribirlo como cadena -que es lo que sale solo
    si uno va rápido- la dejaría encendida creyendo haberla apagado, y la única
    señal sería un ámbar inexplicable la primera mañana que el reloj llegue
    tarde.
    """
    import yaml

    cfg_copia.raw["thresholds"]["ambar_sin_datos"] = "no"
    ruta = tmp_path / "config.yaml"
    ruta.write_text(
        yaml.safe_dump(cfg_copia.raw, allow_unicode=True), encoding="utf-8"
    )
    with pytest.raises(ConfigError) as e:
        load_config(ruta)
    assert "ambar_sin_datos" in str(e.value)


def test_el_nombre_esta_reservado(cfg_copia, tmp_path):
    """Una regla del YAML llamada igual haría indistinguibles en el histórico un
    ámbar por precaución y un ámbar por regla."""
    import yaml

    cfg_copia.raw["thresholds"]["amber"].append(
        {"name": REGLA_SIN_DATOS, "requires": ["fatigue"],
         "when": {"fatigue": {"gte": 99}}}
    )
    ruta = tmp_path / "config.yaml"
    ruta.write_text(
        yaml.safe_dump(cfg_copia.raw, allow_unicode=True), encoding="utf-8"
    )
    with pytest.raises(ConfigError) as e:
        load_config(ruta)
    assert "reservado" in str(e.value)


def test_la_lista_de_medidas_del_reloj_es_una_sola(cfg):
    """El motor promueve y el scheduler rescata. Si cada uno tuviera su propia
    lista, el día que se separasen el motor pintaría un ámbar provisional que la
    recomputación no sabría que hay que deshacer: quedaría puesto para siempre.
    """
    from app.scheduler import MEDIDAS_DE_GARMIN

    assert MEDIDAS_DE_GARMIN is MEDIDAS_DEL_RELOJ


# ---------------------------------------------------------------------------
# El mensaje
# ---------------------------------------------------------------------------


def _mensaje(cfg, **valores):
    d = decide(cfg, LUNES, sig(LUNES, **valores), EngineState())
    return d, render_telegram(d, cfg)


def test_el_mensaje_lo_dice_con_esas_palabras(cfg):
    d, txt = _mensaje(cfg, **SOLO_CHECKIN)
    assert d.light == "amber"
    assert "Ámbar por precaución" in txt
    assert "no se han podido evaluar" in txt
    assert "la variabilidad" in txt, "hay que nombrar las medidas, no «el reloj»"
    assert "no es que estés peor" in txt.lower()


def test_el_mensaje_sale_aunque_el_razonamiento_esté_apagado(cfg_copia):
    """El sitio natural para «qué regla ha disparado» es el bloque «Por qué», y
    ese bloque se apaga con `include_reasoning: false`. Puesto ahí, el único
    ámbar del sistema que no significa «estás peor» se quedaría sin explicación
    justo con el ajuste que existe para acortar el mensaje.
    """
    cfg_copia.raw.setdefault("notifications", {}).setdefault("telegram", {})[
        "include_reasoning"
    ] = False
    d = decide(cfg_copia, LUNES, sig(LUNES, **SOLO_CHECKIN), EngineState())
    txt = render_telegram(d, cfg_copia)

    assert "Por qué" not in txt, "la premisa del test"
    assert "Ámbar por precaución" in txt


def test_el_mensaje_concuerda_cuando_falta_una_sola_medida(cfg):
    """«la nota de sueño no se han podido evaluar» es una falta pequeña en la
    única línea que explica el color del día."""
    s = sig_completa(LUNES)
    for clave in ("sleep_min",):
        s.values[clave] = None
        s.history.pop(clave, None)

    d = decide(cfg, LUNES, s, EngineState())
    txt = render_telegram(d, cfg)

    assert d.light == "amber"
    assert "no se ha podido evaluar lo que duermes" in txt
    assert "no se han podido" not in txt


def test_un_dia_verde_no_lleva_el_aviso(cfg):
    _, txt = _mensaje(cfg, **{**SOLO_CHECKIN, "hrv": 60.0})
    d = decide(cfg, LUNES, sig_completa(LUNES), EngineState())
    txt = render_telegram(d, cfg)
    assert "Ámbar por precaución" not in txt
