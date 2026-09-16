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
from scripts.fijar_carga import CargaInvalida, efectivas_del_yaml, nuevas_series


def series(*pesos: float) -> list[dict[str, object]]:
    return [{"type": "normal", "reps": 12, "weight_kg": p} for p in pesos]


def test_cambia_los_pesos_y_deja_las_reps_donde_estaban():
    """Esto sube un peso. Tocar las reps de paso sería cambiar el entrenamiento
    por un camino que nadie ha pedido."""
    salida = nuevas_series(series(30, 35, 40), [50, 55, 60])
    assert [s["weight_kg"] for s in salida] == [50, 55, 60]
    assert [s["reps"] for s in salida] == [12, 12, 12]


def test_no_se_puede_cambiar_el_numero_de_series_por_el_numero_de_comas():
    """Rellenar o recortar cambiaría el ESQUEMA del ejercicio -de tres series a
    dos- con la excusa de cambiarle el peso. Un cambio así se pide a la cara."""
    with pytest.raises(CargaInvalida, match="3 series efectivas"):
        nuevas_series(series(30, 35, 40), [50, 60])


def test_un_peso_negativo_no_es_una_carga():
    with pytest.raises(CargaInvalida, match="negativo"):
        nuevas_series(series(30), [-5])


def test_sin_series_de_las_que_partir_se_niega():
    """Sin nada de lo que partir no hay reps que conservar, y el guión no se las
    puede inventar."""
    with pytest.raises(CargaInvalida, match="no tiene series efectivas"):
        nuevas_series([], [50])


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
