"""Señales de entrada: funciones puras, sin red y sin base de datos.

Este fichero cubre los tres sitios donde una decisión puede salir mal sin que
nadie lo note: la clasificación de una salida, el cálculo de la carga
acumulada y la resolución de los umbrales adaptativos.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.engine.signals import (
    UNKNOWN,
    Checkin,
    DayMetrics,
    Ride,
    StrengthSession,
    build_signals,
    classify_all,
    classify_ride,
    intensity_budget,
    last_ride_level,
    load_series,
    mean_excluding_outliers,
    percentile,
    previous_weekday,
    resolve_adaptive_threshold,
    rolling_load,
    week_start,
    weekend_summary,
    zone_percentages,
)

from tests.conftest import LUNES, ride

CYCLING = {
    "activity_types": ["cycling"],
    "classification": [
        {"level": "intensa", "zones": [4, 5], "min_time_pct": 30},
        {"level": "media", "zones": [3, 4, 5], "min_time_pct": 40},
        {"level": "suave", "always": True},
    ],
    "classification_fallback": {
        "use": "anaerobic_training_effect",
        "intensa_if_gte": 2.0,
        "media_if_gte": 1.0,
        "on_no_data": UNKNOWN,
    },
    "load": {
        "fallback_estimate": {
            "enabled": True,
            "load_per_hour": {"suave": 50, "media": 90, "intensa": 150},
        }
    },
    "weekend": {"days": ["saturday", "sunday"]},
    "recommendation": {
        "lookback_days": 1,
        "intensity_budget": {
            "weekly_limit": 3,
            "week_starts_on": "monday",
            "counts_as_intense": {"ride_intensa": True, "hiit_executed": True},
        },
    },
}


# ---------------------------------------------------------------------------
# Utilidades numéricas
# ---------------------------------------------------------------------------


def test_percentil_interpola_igual_que_numpy():
    # numpy.percentile([1,2,3,4], 90) == 3.7
    assert percentile([1, 2, 3, 4], 90) == pytest.approx(3.7)


def test_percentil_de_un_solo_valor_es_ese_valor():
    assert percentile([42.0], 90) == 42.0


def test_percentil_de_lista_vacia_es_none():
    assert percentile([], 90) is None
    assert percentile([None, None], 90) is None


def test_la_media_recortada_necesita_al_menos_cinco_valores():
    """Con 4 datos, quitar máximo y mínimo deja una media de 2 puntos.

    Eso no es más robusto: es otra cifra con la mitad de información. Por eso
    el recorte solo se aplica a partir de 5.
    """
    cuatro = [10, 20, 30, 100]
    assert mean_excluding_outliers(cuatro, True) == mean_excluding_outliers(cuatro, False)

    cinco = [10, 20, 30, 40, 1000]
    assert mean_excluding_outliers(cinco, True) == pytest.approx(30.0)
    assert mean_excluding_outliers(cinco, False) == pytest.approx(220.0)


def test_semana_empieza_el_lunes():
    assert LUNES.weekday() == 0
    for i in range(7):
        assert week_start(LUNES + timedelta(days=i)) == LUNES


def test_previous_weekday_es_estrictamente_anterior():
    """Si hoy es lunes, 'el lunes anterior' es hace 7 días, no hoy."""
    assert previous_weekday(LUNES, "monday") == LUNES - timedelta(days=7)
    assert previous_weekday(LUNES, "sunday") == LUNES - timedelta(days=1)
    assert previous_weekday(LUNES, "saturday") == LUNES - timedelta(days=2)


# ---------------------------------------------------------------------------
# Clasificación de salidas
# ---------------------------------------------------------------------------


def test_los_porcentajes_de_zona_se_calculan_sobre_el_tiempo_en_zonas():
    """Las paradas no deben diluir el porcentaje de Z4-Z5."""
    pcts = zone_percentages((0, 0, 0, 600, 600))
    assert pcts[4] == pytest.approx(50.0)
    assert pcts[5] == pytest.approx(50.0)
    assert sum(pcts.values()) == pytest.approx(100.0)


def test_sin_zonas_no_hay_porcentajes():
    assert zone_percentages(None) == {}
    assert zone_percentages((0, 0, 0, 0, 0)) == {}


def test_una_salida_con_un_tercio_en_z4_z5_es_intensa():
    r = ride(LUNES, zones=(0, 0, 1000, 300, 300))
    assert classify_ride(r, CYCLING).level == "intensa"


def test_el_umbral_de_clasificacion_es_estricto():
    """'más del 30%' es > 30, no >= 30. Justo en el borde NO dispara."""
    # 30% exacto en Z4+Z5.
    r = ride(LUNES, zones=(0, 0, 700, 150, 150))
    assert classify_ride(r, CYCLING).level != "intensa"


def test_sin_zonas_se_recurre_al_efecto_anaerobico():
    r = Ride(date=LUNES, duration_s=3600, anaerobic_te=2.5)
    c = classify_ride(r, CYCLING)
    assert c.level == "intensa"
    assert c.source == "fallback_te"


def test_sin_zonas_ni_te_la_salida_queda_desconocida_no_suave():
    """Nunca se asume 'suave': esa es la lectura optimista."""
    r = Ride(date=LUNES, duration_s=3600)
    c = classify_ride(r, CYCLING)
    assert c.level == UNKNOWN
    assert c.source == "none"


def test_la_carga_real_de_garmin_gana_a_la_estimada():
    r = ride(LUNES, load=222.0, zones=(0, 0, 0, 600, 600))
    c = classify_ride(r, CYCLING)
    assert c.load == 222.0
    assert not c.load_estimated


def test_sin_carga_de_garmin_se_estima_por_duracion_y_se_declara():
    r = Ride(date=LUNES, duration_s=7200, zones=(0, 0, 0, 600, 600))  # 2 h intensas
    c = classify_ride(r, CYCLING)
    assert c.load == pytest.approx(2 * 150)
    assert c.load_estimated is True


def test_una_salida_desconocida_no_recibe_carga_inventada():
    """Sin factor para ese nivel no se inventa un número."""
    r = Ride(date=LUNES, duration_s=7200)
    c = classify_ride(r, CYCLING)
    assert c.level == UNKNOWN
    assert c.load == 0.0
    assert not c.load_estimated
    # ...pero el 0 va marcado: es relleno, no una lectura.
    assert c.load_known is False


def test_una_carga_real_y_una_estimada_si_se_saben():
    real = classify_ride(ride(LUNES, load=222.0, zones=(0, 0, 0, 600, 600)), CYCLING)
    est = classify_ride(Ride(date=LUNES, duration_s=7200, zones=(0, 0, 0, 600, 600)), CYCLING)
    assert real.load_known is True
    assert est.load_known is True


def test_las_actividades_que_no_son_bici_se_descartan():
    rides = [ride(LUNES, cycling=True), ride(LUNES, cycling=False)]
    assert len(classify_all(rides, CYCLING)) == 1


# ---------------------------------------------------------------------------
# Carga acumulada
# ---------------------------------------------------------------------------


def test_la_ventana_de_carga_incluye_el_dia_actual():
    rides = classify_all([ride(LUNES, load=100)], CYCLING)
    assert rolling_load(rides, LUNES, 3) == 100.0
    assert rolling_load(rides, LUNES - timedelta(days=1), 3) == 0.0


def test_la_ventana_de_tres_dias_cubre_hoy_y_los_dos_anteriores():
    rides = classify_all(
        [ride(LUNES - timedelta(days=i), load=10) for i in range(5)], CYCLING
    )
    assert rolling_load(rides, LUNES, 3) == 30.0
    assert rolling_load(rides, LUNES, 7) == 50.0


def test_load_series_devuelve_un_valor_por_dia():
    rides = classify_all([ride(LUNES, load=100)], CYCLING)
    serie = load_series(rides, LUNES, days=10, window_days=3)
    assert len(serie) == 10
    assert serie[LUNES] == 100.0
    assert serie[LUNES - timedelta(days=5)] == 0.0


# ---------------------------------------------------------------------------
# Una salida con carga desconocida no se suma como si fuera un cero
# ---------------------------------------------------------------------------
#
# El fallo: una salida sin `training_load` y sin forma de estimarla entraba en
# `load_3d/7d` valiendo 0.0, exactamente igual que un día de sofá. La carga
# salía por debajo de la real, `carga_acumulada` no llegaba a su umbral y el
# día salía verde. Y como el umbral es un percentil de la propia serie, ese
# cero falso además rebajaba el listón para los días siguientes.


def test_una_salida_sin_carga_conocida_deja_la_ventana_sin_dato():
    rides = classify_all(
        [ride(LUNES, load=100), Ride(date=LUNES, duration_s=7200, is_cycling=True)],
        CYCLING,
    )
    assert rolling_load(rides, LUNES, 3) is None, (
        "sumar 100 + 0 daría 100, que es menos carga de la que hubo"
    )


def test_la_ventana_vuelve_a_dar_numero_en_cuanto_la_salida_sale_de_ella():
    """No es un veneno permanente: caduca con la ventana."""
    rides = classify_all(
        [
            ride(LUNES - timedelta(days=5), load=100),
            Ride(date=LUNES - timedelta(days=5), duration_s=7200, is_cycling=True),
        ],
        CYCLING,
    )
    assert rolling_load(rides, LUNES, 3) == 0.0
    assert rolling_load(rides, LUNES, 7) is None


def test_un_dia_de_descanso_sigue_siendo_cero_no_desconocido():
    """La distinción tiene que ir en los dos sentidos."""
    rides = classify_all([ride(LUNES - timedelta(days=10), load=100)], CYCLING)
    assert rolling_load(rides, LUNES, 7) == 0.0


def test_el_percentil_adaptativo_descarta_los_dias_sin_dato():
    """`resolve_adaptive_threshold` ya filtra los `None`: lo que se comprueba
    aquí es que un día contaminado no entra en la muestra como un cero."""
    from app.engine.signals import resolve_adaptive_threshold

    serie = {LUNES - timedelta(days=i): 100.0 for i in range(1, 11)}
    limpio, _ = resolve_adaptive_threshold(
        {"window_days": 30, "min_days_required": 5, "percentile": 90}, serie, LUNES
    )
    serie[LUNES - timedelta(days=3)] = None
    con_hueco, _ = resolve_adaptive_threshold(
        {"window_days": 30, "min_days_required": 5, "percentile": 90}, serie, LUNES
    )
    assert limpio == con_hueco == 100.0


def test_se_dice_en_las_notas_que_falta_la_carga(cfg):
    """Si no se dice, es otro fallo silencioso: el usuario vería
    `carga_acumulada` sin evaluar y sin saber por qué."""
    from app.engine.signals import build_signals

    s = build_signals(
        cfg,
        LUNES,
        metrics=[],
        rides=[Ride(date=LUNES, duration_s=7200, is_cycling=True)],
        sessions=[],
        checkin=None,
    )
    assert s.values["load_7d"] is None
    nota = next((n for n in s.notes if n.startswith("load_7d")), None)
    assert nota is not None, f"ninguna nota explica el hueco: {s.notes}"
    assert LUNES.isoformat() in nota, "hay que decir QUÉ día está sin clasificar"


# ---------------------------------------------------------------------------
# Umbrales adaptativos
# ---------------------------------------------------------------------------
# El usuario los quiere calculados contra su propia distribución histórica, no
# contra constantes absolutas. Estos tests fijan las tres condiciones bajo las
# que el umbral NO existe: poco histórico, ventana degenerada, y el hecho de
# que la ventana termina ayer.


SPEC = {
    "metric": "load_3d",
    "window_days": 60,
    "percentile": 90,
    "min_days_required": 30,
    "include_zero_days": True,
}


def serie_constante(valor: float, dias: int, fin: date) -> dict[date, float]:
    return {fin - timedelta(days=i): valor for i in range(dias)}


def test_sin_historico_suficiente_no_hay_umbral():
    serie = serie_constante(100.0, 10, LUNES)
    valor, motivo = resolve_adaptive_threshold(SPEC, serie, LUNES)
    assert valor is None
    assert "histórico insuficiente" in motivo


def test_con_historico_suficiente_sale_el_percentil():
    serie = serie_constante(100.0, 70, LUNES)
    valor, motivo = resolve_adaptive_threshold(SPEC, serie, LUNES)
    assert valor == pytest.approx(100.0)
    assert motivo is None


def test_un_percentil_cero_no_es_un_umbral():
    """Tras un parón largo hay 60 días 'con dato' cuyo p90 es 0.

    Un umbral de 0 significa 'dispara con cualquier cosa por encima de nada' y
    convertiría la regla en un ámbar permanente justo al volver de una lesión,
    que es cuando menos falta hace. Sin umbral utilizable, la regla se salta.
    """
    serie = serie_constante(0.0, 70, LUNES)
    valor, motivo = resolve_adaptive_threshold(SPEC, serie, LUNES)
    assert valor is None
    assert "percentil sale 0" in motivo


def test_la_ventana_termina_ayer_no_hoy():
    """El valor de hoy no debe entrar en la distribución con la que se compara."""
    serie = serie_constante(100.0, 70, LUNES)
    serie[LUNES] = 100_000.0  # un pico enorme hoy
    valor, _ = resolve_adaptive_threshold(SPEC, serie, LUNES)
    assert valor == pytest.approx(100.0), "el pico de hoy se ha colado en su propio umbral"


def test_excluir_los_ceros_cambia_el_recuento_de_dias_validos():
    spec = {**SPEC, "include_zero_days": False, "min_days_required": 30}
    serie = serie_constante(0.0, 40, LUNES)
    for i in range(1, 11):  # solo 10 días con carga
        serie[LUNES - timedelta(days=i)] = 100.0
    valor, motivo = resolve_adaptive_threshold(spec, serie, LUNES)
    assert valor is None
    assert "10 días con dato" in motivo


# ---------------------------------------------------------------------------
# Fin de semana y presupuesto de intensidad
# ---------------------------------------------------------------------------


def test_una_salida_sin_clasificar_deja_el_fin_de_semana_en_no_se_sabe():
    """Ante la duda, la regla del lunes se salta; no se asume que fue suave."""
    sabado = previous_weekday(LUNES, "saturday")
    rides = classify_all([Ride(date=sabado, duration_s=3600)], CYCLING)
    res = weekend_summary(rides, LUNES, CYCLING)
    assert res.unknown_rides == 1
    assert res.intense_rides is None


def test_una_intensa_confirmada_manda_aunque_falte_clasificar_otra():
    """El dato que falta ya no cambia la conclusión, así que la regla sí evalúa."""
    sabado = previous_weekday(LUNES, "saturday")
    domingo = previous_weekday(LUNES, "sunday")
    rides = classify_all(
        [
            Ride(date=sabado, duration_s=3600),  # desconocida
            ride(domingo, zones=(0, 0, 0, 900, 900)),  # intensa
        ],
        CYCLING,
    )
    res = weekend_summary(rides, LUNES, CYCLING)
    assert res.unknown_rides == 1
    assert res.intense_rides == 1


def test_el_presupuesto_cuenta_lo_ejecutado_no_lo_programado():
    rides = classify_all([ride(LUNES, zones=(0, 0, 0, 900, 900))], CYCLING)
    hecho = [StrengthSession(date=LUNES, routine_key="dia_1", is_hiit=True)]
    b = intensity_budget(rides, hecho, LUNES, CYCLING)
    assert b.used == 2
    assert b.remaining == 1
    assert not b.exhausted

    b2 = intensity_budget(rides, hecho + [
        StrengthSession(date=LUNES, routine_key="dia_2", is_hiit=True)
    ], LUNES, CYCLING)
    assert b2.exhausted


def test_una_salida_sin_clasificar_no_se_cuenta_como_paseo():
    """El agujero: `used` solo sumaba `level == "intensa"`.

    Una salida que Garmin no pudo clasificar -sin zonas de FC y sin Training
    Effect- salía 'desconocida' y no gastaba presupuesto. No es que se contara
    mal: se contaba como si se supiera, y no se sabía. Ignorarla es afirmar que
    fue un paseo, que está tan inventado como decir que fue intensa y además
    cae del lado que quita el freno.
    """
    rides = classify_all(
        [
            ride(LUNES, zones=(0, 0, 0, 900, 900), activity_id=1),  # intensa
            Ride(date=LUNES, duration_s=3600, activity_id=2),  # desconocida
        ],
        CYCLING,
    )
    b = intensity_budget(rides, [], LUNES, CYCLING)
    assert b.used == 1, "la desconocida no puede sumar como intensa"
    assert b.unknown == 1, "pero tampoco puede desaparecer"
    assert any("SIN CLASIFICAR" in d for d in b.detail)


def test_el_presupuesto_que_puede_estar_agotado_se_distingue_del_que_lo_esta():
    """Con `weekly_limit: 3`: dos confirmadas y una sin clasificar.

    El sistema informaba "2/3, te queda una" y dejaba pasar la cuarta salida
    fuerte de la semana. El presupuesto existe justamente para que esa cuarta
    no ocurra, y era la salida sin datos la que abría la puerta.

    `exhausted` sigue siendo un hecho y `indeterminate` una falta de datos: no
    se mezclan, porque quien decide tiene que poder distinguirlas.
    """
    rides = classify_all(
        [
            ride(LUNES, zones=(0, 0, 0, 900, 900), activity_id=1),
            ride(LUNES, zones=(0, 0, 0, 900, 900), activity_id=2),
            Ride(date=LUNES, duration_s=3600, activity_id=3),
        ],
        CYCLING,
    )
    b = intensity_budget(rides, [], LUNES, CYCLING)
    assert b.used == 2 and b.unknown == 1
    assert not b.exhausted, "dos de tres confirmadas no agotan nada"
    assert b.indeterminate, "pero la tercera podría haberlo agotado"


def test_si_lo_confirmado_ya_agota_el_presupuesto_lo_desconocido_no_cambia_nada():
    """Mismo criterio que `weekend_summary`.

    Si el dato que falta no puede cambiar la conclusión, no hay indecisión que
    declarar: el presupuesto está agotado y punto.
    """
    rides = classify_all(
        [ride(LUNES, zones=(0, 0, 0, 900, 900), activity_id=i) for i in (1, 2, 3)]
        + [Ride(date=LUNES, duration_s=3600, activity_id=4)],
        CYCLING,
    )
    b = intensity_budget(rides, [], LUNES, CYCLING)
    assert b.exhausted
    assert not b.indeterminate


def test_sin_salidas_sin_clasificar_no_hay_indecision():
    """La guarda de que esto no frena por defecto.

    Un presupuesto que se declarase indeterminado sin motivo recortaría todas
    las semanas y dejaría de ser un presupuesto.
    """
    rides = classify_all([ride(LUNES, zones=(0, 0, 0, 900, 900))], CYCLING)
    b = intensity_budget(rides, [], LUNES, CYCLING)
    assert b.unknown == 0
    assert not b.indeterminate


def test_la_intensidad_de_la_semana_pasada_no_cuenta():
    anterior = LUNES - timedelta(days=1)  # domingo
    rides = classify_all([ride(anterior, zones=(0, 0, 0, 900, 900))], CYCLING)
    assert intensity_budget(rides, [], LUNES, CYCLING).used == 0


def test_manda_la_salida_mas_intensa_del_dia_anterior():
    ayer = LUNES - timedelta(days=1)
    rides = classify_all(
        [
            ride(ayer, zones=(1800, 0, 0, 0, 0), activity_id=1),  # suave
            ride(ayer, zones=(0, 0, 0, 900, 900), activity_id=2),  # intensa
        ],
        CYCLING,
    )
    assert last_ride_level(rides, LUNES, 1) == "intensa"


def test_sin_salidas_ayer_no_hay_nivel():
    assert last_ride_level([], LUNES, 1) is None


# ---------------------------------------------------------------------------
# build_signals contra el config real
# ---------------------------------------------------------------------------


def test_build_signals_no_inventa_lineas_base_sin_datos(cfg):
    s = build_signals(cfg, LUNES, metrics=[], rides=[], sessions=[])
    assert s.values["hrv_baseline"] is None
    assert s.values["hrv_ratio"] is None
    assert any("sin línea base" in n for n in s.notes)


def test_la_linea_base_excluye_el_dia_de_hoy(cfg):
    """Meter el valor de hoy en su propia media lo amortiguaría."""
    metrics = [DayMetrics(date=LUNES - timedelta(days=i), hrv=100.0) for i in range(1, 8)]
    metrics.append(DayMetrics(date=LUNES, hrv=50.0))  # hoy, muy bajo
    s = build_signals(cfg, LUNES, metrics=metrics, rides=[], sessions=[])
    assert s.values["hrv_baseline"] == pytest.approx(100.0)
    assert s.values["hrv_ratio"] == pytest.approx(0.5)


def test_sin_checkin_se_dice_en_las_notas(cfg):
    s = build_signals(cfg, LUNES, metrics=[], rides=[], sessions=[])
    assert any("sin check-in" in n for n in s.notes)
    for key in cfg.slider_keys():
        assert s.values[key] is None


def test_el_checkin_llega_a_las_señales(cfg):
    c = Checkin(date=LUNES, values={"lower_discomfort": 4})
    s = build_signals(cfg, LUNES, metrics=[], rides=[], sessions=[], checkin=c)
    assert s.values["lower_discomfort"] == 4
    assert s.history["lower_discomfort"][LUNES] == 4


def test_una_señal_a_none_cuenta_como_ausente(cfg):
    s = build_signals(cfg, LUNES, metrics=[], rides=[], sessions=[])
    assert not s.has("hrv")
    assert s.get("hrv") is None


def test_el_snapshot_es_serializable(cfg):
    import json

    s = build_signals(cfg, LUNES, metrics=[], rides=[], sessions=[])
    blob = json.dumps(s.snapshot(), ensure_ascii=False, default=str)
    assert json.loads(blob)["day"] == LUNES.isoformat()


def test_el_historico_largo_de_salidas_da_umbrales_adaptativos(cfg):
    """La razón de ser de la caché de 180 días.

    Con solo la última semana de salidas, la ventana de 60 días está casi toda
    a cero y el percentil 90 sale 0: el guardia lo anula y `carga_acumulada`
    no se evalúa. Con el histórico largo el umbral existe. Este test es el que
    impide que alguien "simplifique" la ventana y deje esa regla muda para
    siempre sin enterarse.
    """
    pocas = [ride(LUNES - timedelta(days=i), load=100) for i in (0, 3)]
    muchas = [ride(LUNES - timedelta(days=i), load=100) for i in range(90)]

    s_pocas = build_signals(cfg, LUNES, metrics=[], rides=pocas, sessions=[])
    s_muchas = build_signals(cfg, LUNES, metrics=[], rides=muchas, sessions=[])

    assert s_pocas.adaptive.get("load_3d_p90") is None
    assert s_muchas.adaptive.get("load_3d_p90") is not None
    assert s_muchas.adaptive["load_3d_p90"] > 0
