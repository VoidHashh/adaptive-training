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


# ---------------------------------------------------------------------------
# El recuento CUENTA. No recorta. Nunca.
# ---------------------------------------------------------------------------
#
# Estos tests son la frontera entre el sistema de antes y el de ahora, y están
# escritos al revés que los que sustituyen: antes se comprobaba que el freno
# saltara, y ahora se comprueba que NO salte por mucho que se le cargue la
# semana. Los cuatro frenos por recuento que había -`weekly_limit`,
# `on_budget_exhausted`, `max_intense_rides_per_weekend` y
# `require_green_for_intense`- ya no existen.
#
# Lo que queda es un único recorte: el techo del semáforo. Ese sí puede bajar el
# nivel, porque sale de lo que mide el cuerpo -HRV, sueño, pulso de reposo,
# carga de Garmin- y no de una hoja de cálculo.


def _con_recuento(day: date, used: int, unknown: int = 0) -> Signals:
    from app.engine.signals import IntensityCount

    s = Signals(day=day)
    s.intense_count = IntensityCount(
        used=used, detail=[], week_start=day, unknown=unknown
    )
    return s


@pytest.mark.parametrize("hechas", [0, 1, 3, 4, 7, 12])
def test_el_recuento_no_baja_el_nivel_por_alto_que_sea(cfg, hechas):
    """La semana de viaje: siete días de bici seguidos y el sábado sigue libre.

    Antes, de la cuarta sesión fuerte en adelante el sábado salía 'suave' con el
    motivo "presupuesto agotado". Eso es el sistema decidiendo qué se puede
    hacer, y es justo lo que se ha quitado: el sistema registra y se adapta.
    """
    rec = recommend_bike(cfg, _con_recuento(SABADO, used=hechas), "green")
    assert rec.level == "intensa", [d[2] for d in rec.downgrades]
    assert not rec.downgrades


def test_el_recuento_sale_como_nota_y_no_como_recorte(cfg):
    """La distinción tiene que sobrevivir hasta la pantalla del móvil.

    `downgrades` cambia el nivel recomendado; `notas` no. Si el recuento se
    colara en `downgrades`, el mensaje diría "intensa, recortada por..." sin
    haber recortado nada, y a partir de ahí da igual lo que haga el código:
    quien lo lee entiende que le han frenado.
    """
    rec = recommend_bike(cfg, _con_recuento(SABADO, used=5), "green")
    assert not rec.downgrades
    notas = rec.texto_notas()
    assert any("5" in n for n in notas), notas
    assert rec.level == "intensa"


def test_la_nota_del_recuento_no_lleva_tono_de_reprimenda(cfg):
    """"El sistema informa y se adapta, no juzga." Dicho literal del usuario.

    Esto no es cosmética. Una nota que regaña convierte el recuento en una
    prescripción por la puerta de atrás: no frena el código, frena el que lo
    lee. Y la semana de más carga es precisamente la semana en que menos falta
    hace que nadie te riña.
    """
    rec = recommend_bike(cfg, _con_recuento(SABADO, used=9), "green")
    junto = " ".join(rec.texto_notas()).lower()
    for palabra in (
        "demasiad", "exceso", "excedid", "deberías", "cuidado", "ojo",
        "agotado", "límite", "te pasas", "de más",
    ):
        assert palabra not in junto, f"tono de reproche: '{palabra}' en {junto!r}"


def test_una_salida_sin_clasificar_sale_en_la_nota_y_no_recorta(cfg):
    """El sesgo cambió de sitio al quitar el freno, pero no desapareció.

    Cuando esto era un presupuesto, ignorar una salida sin clasificar abría la
    puerta a una sesión fuerte de más. Ahora no hay puerta: lo único que se
    estropea es el número que sale escrito, que quedaría corto. Se dice, y ya.
    """
    rec = recommend_bike(cfg, _con_recuento(SABADO, used=2, unknown=1), "green")
    assert rec.level == "intensa"
    assert not rec.downgrades
    assert any("sin clasificar" in n for n in rec.texto_notas())


def test_el_semaforo_sigue_mandando_por_encima_del_recuento(cfg):
    """Quitar los frenos de cuenta no puede haber tocado el freno del cuerpo.

    Es el reverso exacto del cambio: el recuento no recorta NUNCA, y el ámbar
    recorta SIEMPRE. Si al quitar los cuatro frenos se hubiera llevado por
    delante el techo del semáforo, el sistema habría pasado de frenar de más a
    no frenar nada, que es bastante peor.
    """
    rec = recommend_bike(cfg, _con_recuento(SABADO, used=9), "amber")
    assert rec.level == "suave"
    assert rec.downgrades, "el ámbar tiene que seguir explicando por qué recorta"
    assert "ámbar" in rec.downgrades[0][2].lower()


def test_con_el_recuento_ausente_no_hay_nota_ni_hueco(cfg):
    """Un día sin recuento -cualquier ruta que no pase por `build_signals`- no
    puede sacar una línea vacía ni un "None" en el mensaje."""
    rec = recommend_bike(cfg, _senales(SABADO), "green")
    assert rec.texto_notas() == []
    assert rec.level == "intensa"


# ---------------------------------------------------------------------------
# La ventana del fin de semana contaba el domingo de la semana pasada
# ---------------------------------------------------------------------------
#
# El filtro era `<= 6 días`, y un sábado el domingo anterior cae exactamente a
# 6. El número que se enseña tiene que ser verdad aunque no decida nada; si
# acaso más, porque un dato que no decide es un dato que nadie va a ir a
# comprobar.


def _con_salidas(day: date, fechas: list[date]) -> Signals:
    from app.engine.signals import ClassifiedRide, Ride

    s = Signals(day=day)
    s.rides = [
        ClassifiedRide(
            ride=Ride(date=d, duration_s=7200),
            level="intensa",
            source="test",
            load=100.0,
            load_estimated=False,
        )
        for d in fechas
    ]
    return s


def test_el_domingo_pasado_no_es_este_fin_de_semana(cfg):
    from app.engine.bike_advisor import _intense_rides_this_weekend

    domingo_pasado = SABADO - timedelta(days=6)
    assert domingo_pasado.weekday() == 6
    sig = _con_salidas(SABADO, [domingo_pasado])
    assert _intense_rides_this_weekend(sig, cfg.raw["cycling"]) == 0


def test_el_sabado_de_ayer_si_es_este_fin_de_semana(cfg):
    from app.engine.bike_advisor import _intense_rides_this_weekend

    sig = _con_salidas(DOMINGO, [SABADO])
    assert _intense_rides_this_weekend(sig, cfg.raw["cycling"]) == 1


def test_la_salida_del_fin_de_semana_se_cuenta_pero_no_recorta(cfg):
    """El freno que había aquí, `max_intense_rides_per_weekend`, ya no está.

    Y aparte de sobrar, contaba mal: solo era alcanzable los sábados -el punto
    de partida del domingo es 'media' y el bloque entero se saltaba- y en los
    sábados miraba al domingo de la semana anterior.
    """
    sig = _con_salidas(DOMINGO, [SABADO])
    rec = recommend_bike(cfg, sig, "green")
    assert rec.level == "media", [d[2] for d in rec.downgrades]
    assert not rec.downgrades
    nota = next(n for n in rec.texto_notas() if "fin de semana" in n)
    assert nota == "llevas 1 salida intensa este fin de semana", nota
    assert "(s)" not in nota, "esto lo lee una persona, no un log"


def test_la_intensa_de_ayer_avisa_pero_ya_no_baja_el_nivel(cfg):
    """`no_consecutive_intense` era el único de los tres que sí disparaba.

    108 veces en la rejilla de 648 combinaciones. Por eso es el que más se nota
    al quitarlo, y por eso tiene que dejar dicho lo que sabe: dos intensas
    seguidas es un dato que importa, y quien decide si hoy toca o no es el que
    pedalea.
    """
    sig = _senales(SABADO)
    sig.values["yesterday_ride_level"] = "intensa"
    rec = recommend_bike(cfg, sig, "green")
    assert rec.level == "intensa"
    assert not rec.downgrades
    assert any("intensa" in n for n in rec.texto_notas())


