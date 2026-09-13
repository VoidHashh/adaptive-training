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


# --- el presupuesto que no se sabe si está agotado --------------------------


def _con_presupuesto(day: date, used: int, unknown: int, limit: int = 3) -> Signals:
    from app.engine.signals import IntensityBudget

    s = Signals(day=day)
    s.budget = IntensityBudget(
        limit=limit, used=used, detail=[], week_start=day, unknown=unknown
    )
    return s


def test_una_salida_sin_clasificar_no_deja_pasar_la_intensa(cfg):
    """El agujero, visto desde donde se nota.

    Dos intensas confirmadas y una salida sin clasificar, con el límite en 3.
    Antes: las desconocidas no se contaban, `exhausted` era False y el sábado
    salía 'intensa'. Un presupuesto que se salta solo cuando faltan datos no es
    un presupuesto: la semana en que Garmin no clasifica bien es justamente la
    que acaba con una salida fuerte de más.
    """
    rec = recommend_bike(cfg, _con_presupuesto(SABADO, used=2, unknown=1), "green")
    assert rec.level == "suave"


def test_el_motivo_dice_que_no_se_sabe_y_no_que_esta_agotado(cfg):
    """Frenar por falta de datos y frenar por un hecho no son lo mismo.

    El motivo va escrito en el mensaje precisamente para que el sábado por la
    mañana se pueda decidir a mano con la información buena.
    """
    rec = recommend_bike(cfg, _con_presupuesto(SABADO, used=2, unknown=1), "green")
    motivo = rec.downgrades[-1][2]
    assert "puede que" in motivo
    assert "sin clasificar" in motivo
    assert "agotado (" not in motivo, "no se puede afirmar lo que no se sabe"


def test_con_el_presupuesto_agotado_de_verdad_el_motivo_lo_afirma(cfg):
    rec = recommend_bike(cfg, _con_presupuesto(SABADO, used=3, unknown=0), "green")
    assert rec.level == "suave"
    assert "agotado" in rec.downgrades[-1][2]


def test_sin_salidas_sin_clasificar_la_intensa_del_sabado_sigue_saliendo(cfg):
    """Que el freno nuevo no se coma todos los sábados.

    Un presupuesto que recorta siempre no es un presupuesto, y este test es lo
    único que separa las dos cosas.
    """
    rec = recommend_bike(cfg, _con_presupuesto(SABADO, used=1, unknown=0), "green")
    assert rec.level == "intensa"
    assert not rec.downgrades


# ---------------------------------------------------------------------------
# El reparto que describe la regla tiene que caber en el número
# ---------------------------------------------------------------------------
#
# Los tests de arriba fijan el MECANISMO y se fabrican su propio `limit=3`, así
# que ninguno mira el `weekly_limit` de verdad. El número del YAML se quedaba
# sin nadie que lo defendiera, y no es un número cualquiera: describe un reparto
# concreto -hasta 2 HIIT entre semana, 1 intensa suelta y 1 salida fuerte el fin
# de semana- que con el límite en 3 no cabía. Los dos HIIT caen lunes y jueves,
# o sea ANTES del fin de semana, así que el sábado llegaba a 2/3 y cualquier
# intensidad de entre semana lo dejaba sin salida. Medido sobre 25 semanas
# reales, uno de cada cinco fines de semana.
#
# Estos tests leen el config del repositorio a propósito. Volver a 3 los rompe,
# que es justo lo que tienen que hacer.


def _presupuesto_real(
    cfg, day, *, hiit: int, intensas_entre_semana: int, sabado_intenso: bool = False
):
    """Gasta el presupuesto con el `weekly_limit` DE VERDAD, no con uno de test."""
    from datetime import timedelta as _td

    from app.engine.signals import (
        ClassifiedRide,
        Ride,
        StrengthSession,
        intensity_budget,
    )

    lunes = day - _td(days=day.weekday())

    def _salida(d):
        return ClassifiedRide(
            ride=Ride(date=d, duration_s=7200),
            level="intensa",
            source="test",
            load=100.0,
            load_estimated=False,
        )

    sesiones = [
        StrengthSession(date=lunes + _td(days=d), routine_key=k, is_hiit=True)
        for d, k in list(enumerate(("hiit_dia_1", "hiit_dia_2")))[:hiit]
    ]
    rides = [_salida(lunes + _td(days=1 + i)) for i in range(intensas_entre_semana)]
    if sabado_intenso:
        rides.append(_salida(lunes + _td(days=5)))
    s = Signals(day=day)
    s.rides = rides
    s.budget = intensity_budget(rides, sesiones, day, cfg.raw["cycling"])
    return s


def test_los_dos_hiit_de_la_semana_no_se_comen_la_salida_del_sabado(cfg):
    """Lo que motivó subir el límite: hacer el plan entero no puede castigarte.

    Dos HIIT hechos -exactamente lo que el plan pide- más una salida intensa
    suelta entre semana. Con el límite en 3 esto daba 3/3 y el sábado salía
    'suave': el sistema recortaba la salida por haber cumplido.
    """
    sig = _presupuesto_real(cfg, SABADO, hiit=2, intensas_entre_semana=1)
    assert not sig.budget.exhausted, (
        f"{sig.budget.used}/{sig.budget.limit}: el reparto que describe la "
        "regla no cabe en el presupuesto"
    )
    rec = recommend_bike(cfg, sig, "green")
    assert rec.level == "intensa", [d[2] for d in rec.downgrades]


def test_el_presupuesto_sigue_frenando_cuando_de_verdad_hay_de_mas(cfg):
    """Subir el límite no puede equivaler a quitar el freno.

    Dos HIIT y DOS intensas entre semana ya son cuatro sesiones fuertes antes
    del sábado. Ahí el presupuesto tiene que cortar, o no es un presupuesto.
    """
    sig = _presupuesto_real(cfg, SABADO, hiit=2, intensas_entre_semana=2)
    assert sig.budget.exhausted, f"{sig.budget.used}/{sig.budget.limit}"
    rec = recommend_bike(cfg, sig, "green")
    assert rec.level == "suave"
    assert "agotado" in rec.downgrades[-1][2]


def test_el_domingo_no_depende_del_presupuesto_para_frenar(cfg):
    """El freno del domingo no puede ser el número que acabamos de subir.

    Con el límite en 3 el domingo posterior a un sábado intenso salía agotado y
    eso tapaba que ya había otros dos frenos puestos. Al subirlo a 4 el
    presupuesto deja de cortar ahí -3/4- y queda a la vista quién sostiene de
    verdad el domingo. Con una hernia L4-L5 eso no puede quedar sin comprobar.

    El sábado intenso tiene que estar en las salidas y no solo en
    `yesterday_ride_level`: sin él el presupuesto se queda en 2/4, nunca llega a
    estar cerca de agotarse y el test aprobaría sin haber probado nada.
    """
    sig = _presupuesto_real(
        cfg, DOMINGO, hiit=2, intensas_entre_semana=0, sabado_intenso=True
    )
    sig.values["yesterday_ride_level"] = "intensa"
    assert not sig.budget.exhausted, (
        f"{sig.budget.used}/{sig.budget.limit}: si el presupuesto ya corta "
        "aquí, este test no comprueba que los otros frenos existan"
    )

    rec = recommend_bike(cfg, sig, "green")
    assert rec.level == "suave"
    assert "intensa" in rec.downgrades[-1][2]
