"""El techo del semáforo sobre la recomendación de bici.

De los seis recortes encadenados de `bike_advisor`, este es el único que
responde al estado de HOY: los demás miran al calendario o al histórico. Es
también el que decide si un domingo en rojo se sale a rodar.

Hasta ahora todo el módulo tenía un solo test, y era `d.bike is not None`.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.engine.bike_advisor import BikeConfigError, _cap, recommend_bike
from app.engine.signals import Signals

from tests.conftest import LUNES

ORDEN = ["descanso", "suave", "media", "intensa"]
SABADO = LUNES + timedelta(days=5)
DOMINGO = LUNES + timedelta(days=6)


# --- la guarda de `_cap` ---------------------------------------------------


def test_cap_recorta_al_techo():
    assert _cap("intensa", "suave", ORDEN) == "suave"


def test_cap_no_sube_un_nivel_que_ya_esta_por_debajo():
    """El techo es un máximo, no un objetivo."""
    assert _cap("suave", "intensa", ORDEN) == "suave"


@pytest.mark.parametrize(
    "nivel,techo",
    [("intensa", "moderada"), ("brutal", "suave")],
)
def test_un_nivel_desconocido_revienta_en_vez_de_no_recortar(nivel, techo):
    """Devolver el nivel sin tocar era quitarle el techo al semáforo callando.

    Una función cuyo trabajo es poner un techo no puede tener una rama que
    consiste en no ponerlo: el modo de fallo tiene que ser 'para', no 'sigue'.
    """
    with pytest.raises(BikeConfigError, match="intensity_order"):
        _cap(nivel, techo, ORDEN)


# --- la cadena completa, que es lo que se lee en el mensaje ----------------


def _senales(day: date) -> Signals:
    return Signals(day=day)


def test_en_rojo_el_sabado_no_sale_la_intensa(cfg):
    """`baseline_by_weekday` pone el sábado en 'intensa' y el rojo en
    'descanso'. Sin techo, un rojo se leería como un sábado cualquiera."""
    rec = recommend_bike(cfg, _senales(SABADO), "red")
    assert rec.level == "descanso"


def test_el_recorte_del_semaforo_se_explica_y_no_solo_se_aplica(cfg):
    """Un 'descanso' a secas no se puede discutir ni auditar.

    El motivo es lo que separa una recomendación de una orden, y lo que
    permite mirar atrás dentro de un mes y entender por qué ese domingo no se
    salió.
    """
    rec = recommend_bike(cfg, _senales(SABADO), "red")
    assert rec.downgrades, "un recorte sin motivo no se puede auditar"
    desde, hasta, motivo = rec.downgrades[0]
    assert (desde, hasta) == ("intensa", "descanso")
    assert "rojo" in motivo.lower()
    assert "descanso" in rec.text().lower()


def test_en_ambar_el_sabado_baja_a_suave_pero_no_a_descanso(cfg):
    """El ámbar recorta, no cancela: `actions.amber.bike_max` es 'suave'."""
    rec = recommend_bike(cfg, _senales(SABADO), "amber")
    assert rec.level == "suave"


def test_en_verde_el_sabado_se_queda_como_estaba(cfg):
    """El techo verde es 'intensa', o sea que no hay techo efectivo.

    Importa comprobarlo: si el verde recortara, el sistema estaría siempre
    frenando y la progresión de bici no llegaría nunca arriba.
    """
    rec = recommend_bike(cfg, _senales(SABADO), "green")
    assert rec.level == "intensa"
    assert not rec.downgrades


def test_el_domingo_parte_de_media_y_el_rojo_tambien_lo_baja(cfg):
    rec = recommend_bike(cfg, _senales(DOMINGO), "red")
    assert rec.baseline == "media"
    assert rec.level == "descanso"


def test_entre_semana_no_se_recomienda_bici_y_se_dice_por_que(cfg):
    rec = recommend_bike(cfg, _senales(LUNES), "green")
    assert not rec.applies
    assert rec.skip_reason
    assert rec.text() == "", "si no aplica, no puede ocupar una línea del mensaje"
