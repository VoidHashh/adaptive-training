"""La salida de emergencia de la adopción: fijar una carga a mano.

El tope de salto para la sesión sin adoptar y lo dice, que es lo correcto para
una errata al teclear en Hevy. Pero cuando el salto era REAL -60 kg de verdad en
una extensión cuyo objetivo eran 40- hacía falta una forma de cerrarlo, porque
el consejo que daba el mensaje («se sube a mano en config.yaml») apuntaba a una
pieza que ya no decide nada: en cuanto un ejercicio tiene fila en
`exercise_targets`, la carga sale de ahí y el YAML no se vuelve a mirar.
"""

from __future__ import annotations

import pytest

from app.config_loader import load_config
from scripts.fijar_carga import (
    CargaInvalida,
    efectivas_del_yaml,
    nuevas_series,
    parsea_series,
)


def series(*pesos: float) -> list[dict[str, object]]:
    return [{"type": "normal", "reps": 12, "weight_kg": p} for p in pesos]


def fijar(vigentes, texto):
    """El camino de verdad: lo que se teclea, parseado y aplicado."""
    return nuevas_series(vigentes, parsea_series(texto))


def test_cambia_los_pesos_y_deja_las_reps_donde_estaban():
    """Esto sube un peso. Tocar las reps de paso sería cambiar el entrenamiento
    por un camino que nadie ha pedido."""
    salida = fijar(series(30, 35, 40), "50,55,60")
    assert [s["weight_kg"] for s in salida] == [50, 55, 60]
    assert [s["reps"] for s in salida] == [12, 12, 12]


def test_no_se_puede_cambiar_el_numero_de_series_por_el_numero_de_comas():
    """Rellenar o recortar cambiaría el ESQUEMA del ejercicio -de tres series a
    dos- con la excusa de cambiarle el peso. Un cambio así se pide a la cara."""
    with pytest.raises(CargaInvalida, match="3 series efectivas"):
        fijar(series(30, 35, 40), "50,60")


def test_un_peso_negativo_no_es_una_carga():
    with pytest.raises(CargaInvalida, match="negativo"):
        fijar(series(30), "-5")


def test_sin_series_de_las_que_partir_se_niega():
    """Sin nada de lo que partir no hay reps que conservar, y el guión no se las
    puede inventar."""
    with pytest.raises(CargaInvalida, match="no tiene series efectivas"):
        fijar([], "50")


# ---------------------------------------------------------------------------
# Reordenar una rampa. Ver el docstring de `parsea_series`.
# ---------------------------------------------------------------------------


def gemelo() -> list[dict[str, object]]:
    """La rampa de `gemelo_sentado` tal y como estaba en la base: con el dedazo.

    El 70 delante era un error de tecleo en Hevy que la adopción se creyó, y las
    15 repeticiones iban con el 65.
    """
    return [
        {"type": "normal", "reps": 12, "weight_kg": 70},
        {"type": "normal", "reps": 12, "weight_kg": 60},
        {"type": "normal", "reps": 15, "weight_kg": 65},
    ]


def test_ordenar_solo_los_kilos_arrastra_las_reps_al_peso_mayor():
    """El fallo que motivó la sintaxis nueva, clavado como test.

    `60,65,70` parece la corrección obvia y no lo es: las reps se quedan donde
    estaban, así que las 15 -que iban con el 65- acaban en la serie de 70. Nadie
    pidió un ejercicio más duro; se colaría por la puerta de atrás de una
    corrección de orden. Esto NO es un fallo del guión, es lo que «cambiar los
    pesos» significa; por eso se comprueba, para que quede escrito cuál es la
    forma equivocada.
    """
    salida = fijar(gemelo(), "60,65,70")
    assert [s["weight_kg"] for s in salida] == [60, 65, 70]
    assert [s["reps"] for s in salida] == [12, 12, 15]
    assert salida[-1]["reps"] == 15, "las 15 reps han caído en el peso más alto"


def test_con_peso_y_reps_la_rampa_se_reordena_entera():
    """La forma correcta: los mismos tres pares, puestos de menos a más."""
    salida = fijar(gemelo(), "60x12,65x15,70x12")
    assert [(s["weight_kg"], s["reps"]) for s in salida] == [(60, 12), (65, 15), (70, 12)]

    antes = sorted((s["weight_kg"], s["reps"]) for s in gemelo())
    assert sorted((s["weight_kg"], s["reps"]) for s in salida) == antes, (
        "reordenar no puede inventar ni perder ninguna serie"
    )


def test_la_rampa_reordenada_es_la_que_declara_el_config(cfg):
    """Y coincide con el YAML, que es el punto de la corrección entera."""
    delyaml = efectivas_del_yaml(cfg, "dia_1", "gemelo_sentado")
    salida = fijar(gemelo(), "60x12,65x15,70x12")
    assert [(s["weight_kg"], s["reps"]) for s in salida] == [
        (s["weight_kg"], s["reps"]) for s in delyaml
    ]


def test_la_aspa_del_teclado_espanol_vale_igual():
    """Quien copie la rampa de un comentario del YAML traerá «×», no «x»."""
    assert parsea_series("60×12,65×15") == parsea_series("60x12,65x15")


def test_mezclar_las_dos_formas_se_rechaza():
    """`60,65x15,70` es ambiguo justo donde más da igual equivocarse: las reps de
    las otras dos dependerían del orden viejo, que es lo que se está cambiando."""
    with pytest.raises(CargaInvalida, match="o todas las series llevan repeticiones"):
        parsea_series("60,65x15,70")


def test_una_serie_de_cero_repeticiones_no_es_una_serie():
    with pytest.raises(CargaInvalida, match="cero repeticiones"):
        fijar(gemelo(), "60x12,65x0,70x12")


def test_el_punto_de_partida_sale_del_yaml_sin_el_calentamiento(cfg):
    """Para un ejercicio que todavía no ha progresado nunca, el YAML sí manda.

    Y el calentamiento no cuenta: sale del fichero en cada construcción y la
    progresión tampoco lo toca.
    """
    efectivas = efectivas_del_yaml(cfg, "dia_1", "extension_cuadriceps")
    assert efectivas, "el ejercicio está en esa rutina del config real"
    assert all(str(s.get("type") or "normal") != "warmup" for s in efectivas)


def test_un_ejercicio_que_no_esta_en_esa_rutina_no_inventa_series():
    c = load_config("config.yaml")
    assert efectivas_del_yaml(c, "dia_1", "ejercicio_fantasma") == []
    assert efectivas_del_yaml(c, "rutina_fantasma", "extension_cuadriceps") == []
