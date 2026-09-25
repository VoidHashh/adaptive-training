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

from datetime import timedelta

from app.engine.progression import (
    evaluate_gate,
    evaluate_volume_gates,
    plan_progression,
)
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


def _por_rutina(cfg) -> dict:
    """`progression` con `compliance_scope: routine`, el ámbito de antes del
    25/09/2026. Sigue siendo una opción del YAML, y sus tests la vigilan."""
    prog = copy.deepcopy(cfg.raw["progression"])
    prog["gate"]["compliance_scope"] = "routine"
    return prog


def test_el_motivo_nombra_a_los_ejercicios_sin_registro(cfg):
    """Un aviso que nombra su causa se arregla desde el móvil.

    Con una rutina en marcha y un ejercicio nuevo, "no hay registro" a secas
    es un callejón sin salida: nueve ejercicios y ninguna pista de cuál. Es del
    ámbito `routine`: por ejercicio, la puerta ni se cierra.
    """
    abierta, motivo = evaluate_gate(
        _por_rutina(cfg), "green", sig(LUNES, lower_discomfort=1),
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


def test_por_ejercicio_uno_sin_registro_no_cierra_la_rutina(cfg):
    """El mismo caso con `compliance_scope: exercise`: la puerta sigue abierta,
    y el que no tiene registro se queda sin subir él solo (`plan_progression`)."""
    abierta, motivo = evaluate_gate(
        cfg.raw["progression"], "green", sig(LUNES, lower_discomfort=1),
        None, "dia_1", False,
        compliance_por_ejercicio={
            "prensa_horizontal": True,
            "gemelo_sentado": False,
            "hip_thrust_barra": None,
        },
    )
    assert abierta, motivo


def test_por_ejercicio_sin_registro_de_ninguno_se_cierra_igual(cfg):
    """El estreno de una rutina sigue cerrando la puerta entera: no hay
    registro de NINGUNO, y eso sí es de la rutina."""
    abierta, motivo = evaluate_gate(
        cfg.raw["progression"], "green", sig(LUNES, lower_discomfort=1),
        None, "dia_1", False,
        compliance_por_ejercicio={"prensa_horizontal": None, "gemelo_sentado": None},
    )
    assert not abierta
    assert "no hay registro" in motivo


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


# ---------------------------------------------------------------------------
# Las puertas de VOLUMEN
#
# Son otra cosa que la puerta general de arriba y hasta hoy no tenían un solo
# test que las llamara: se comprobaban de refilón, a través de `plan_progression`
# y siempre con la puerta general ya cerrada, que es justo el caso en el que su
# motivo no se llega a leer (`decision.py` escribe `gate_reason` en su lugar).
#
# La media lumbar que miran NO es el freno `lumbar_bloquea_todo`. Aquel lee el
# valor de HOY y cierra si falta; esta lee la media de los 7 días ANTERIORES a
# hoy. Se puede tener lo primero y no lo segundo -check-in de hoy relleno, la
# semana pasada en blanco- y entonces esta ventana se queda vacía.
# ---------------------------------------------------------------------------


def lumbar(day, valores: dict[int, float]):
    """Señales con un histórico de molestia lumbar: `{días atrás: valor}`."""
    return sig(day, history={
        "lower_discomfort": {day - timedelta(days=i): v for i, v in valores.items()}
    })


def volumen(cfg, señales, *, luz="green", day=LUNES):
    return evaluate_volume_gates(cfg.raw["progression"], señales, day, luz)


def test_sin_partes_de_lumbar_la_puerta_de_volumen_se_cierra(cfg):
    """Sin partes no se sube volumen. No saber no es estar bien.

    Esto tuvo dos vidas anteriores y las dos estaban mal. Primero abría
    devolviendo "sin señales que desaconsejen", que es literalmente la misma
    frase que cuando sí hay partes y salen bajos: el registro que se lee en
    Telegram y en la auditoría afirmaba haber comprobado la lumbar de alguien
    con una hernia L4-L5 en una semana en la que no había nada que comprobar.
    Después abría diciendo la verdad, que era mejor pero seguía subiendo
    volumen a ciegas.

    Ahora cierra, que es lo que el bucle de `brakes` hace noventa líneas más
    arriba con cualquier señal que falte. La puerta estricta decide si se añade
    una serie efectiva a la cadena posterior; el coste de equivocarse no es
    simétrico.
    """
    for abierta, motivo in volumen(cfg, sig(LUNES)):
        assert not abierta, motivo
        assert "sin saber cómo está la lumbar" in motivo
        assert "sin señales que desaconsejen" not in motivo, (
            "esa frase es la de haber mirado y salir bien; aquí no se ha mirado"
        )


def test_el_motivo_dice_que_el_checkin_lo_desbloquea(cfg):
    """Una puerta que se cierra sin explicar la llave es una puerta rota.

    Este bloqueo no se levanta entrenando mejor ni esperando: se levanta
    rellenando el check-in, que son treinta segundos. Si el motivo no lo dice,
    el volumen se queda parado indefinidamente y desde fuera parece que el
    sistema ha decidido que no toca subir.
    """
    for _, motivo in volumen(cfg, sig(LUNES)):
        assert "check-in" in motivo, motivo


def test_el_parte_de_hoy_no_llena_la_ventana_de_la_semana(cfg):
    """La distinción entre los dos controles lumbares, escrita.

    Un check-in de hoy hace que `lumbar_bloquea_todo` sí pueda evaluarse, y es
    fácil dar por hecho que entonces la puerta de volumen también mira algo. No:
    su ventana son los días 1..7 ANTERIORES, y sigue vacía.
    """
    for abierta, motivo in volumen(cfg, lumbar(LUNES, {0: 1})):
        assert not abierta, motivo
        assert "sin saber cómo está la lumbar" in motivo


def test_con_la_semana_rellena_y_sana_el_motivo_es_el_de_siempre(cfg):
    """CONTRAGUARDA: si no, los dos tests de arriba estarían siempre verdes.

    Bastaría con que el motivo nuevo se devolviera SIEMPRE -por ejemplo si
    alguien lo sacara fuera del `if`- para que ambos pasaran sin que la puerta
    comprobara nada. Aquí hay siete partes buenos y la frase tiene que ser la
    otra.
    """
    señales = lumbar(LUNES, {i: 1 for i in range(1, 8)})
    (serie_ok, serie_por_que), (reps_ok, reps_por_que) = volumen(cfg, señales)
    assert serie_ok and reps_ok
    assert serie_por_que == "sin señales que desaconsejen añadir una serie"
    assert reps_por_que == "sin señales que desaconsejen subir repeticiones"


def test_la_lumbar_sostenida_para_antes_la_serie_que_las_reps(cfg):
    """Los dos umbrales son distintos a propósito y salen del config.

    Una media de 4 no es lo mismo para una serie efectiva nueva que para dos
    repeticiones. Si alguien igualara los dos límites, este test lo dice.
    """
    (serie_ok, serie_por_que), (reps_ok, _) = volumen(
        cfg, lumbar(LUNES, {i: 4 for i in range(1, 8)})
    )
    assert not serie_ok
    assert "media 4.0" in serie_por_que
    assert reps_ok, "el límite de las reps es 5, no 4"

    (serie_ok, _), (reps_ok, reps_por_que) = volumen(
        cfg, lumbar(LUNES, {i: 5 for i in range(1, 8)})
    )
    assert not serie_ok
    assert not reps_ok
    assert "media 5.0" in reps_por_que


def test_un_solo_parte_malo_en_la_semana_no_es_la_media(cfg):
    """Se promedia lo que hay, no se rellena lo que falta.

    Un único día de 6 con el resto en blanco da media 6 y bloquea; el mismo 6
    entre seis días buenos da 1,7 y no. Es la diferencia entre "la semana viene
    cargada" y "hubo un mal día", y la ventana la mantiene sola.
    """
    (serie_ok, _), _ = volumen(cfg, lumbar(LUNES, {3: 6}))
    assert not serie_ok

    (serie_ok, _), _ = volumen(
        cfg, lumbar(LUNES, {**{i: 1 for i in range(1, 8)}, 3: 6})
    )
    assert serie_ok


def test_sin_ambito_de_cumplimiento_el_motor_revienta_en_vez_de_suponer(cfg):
    """Tuvo defecto -`routine`- y no lo leía nadie: el banco de mutaciones lo
    cambió a `exercise` y la suite siguió verde. Lo que decide si un ejercicio
    incompleto frena a toda la rutina no se supone."""
    prog = copy.deepcopy(cfg.raw["progression"])
    prog["gate"].pop("compliance_scope")
    with pytest.raises(RuleError, match="compliance_scope"):
        evaluate_gate(prog, "green", sig(LUNES, lower_discomfort=1), True, "dia_1", False)
