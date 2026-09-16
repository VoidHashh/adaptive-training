"""Señales de entrada: funciones puras, sin red y sin base de datos.

Este fichero cubre los tres sitios donde una decisión puede salir mal sin que
nadie lo note: la clasificación de una salida, el cálculo de la carga
acumulada y la resolución de los umbrales adaptativos.
"""

from __future__ import annotations

import copy
from datetime import date, timedelta

import pytest

from app.config_loader import load_config

from app.engine.signals import (
    CLAVE_APETECE,
    CLAVE_SESION_ELEGIDA,
    CLAVE_VOY_A_ENTRENAR,
    UNKNOWN,
    Checkin,
    DayMetrics,
    Ride,
    StrengthSession,
    build_signals,
    classify_all,
    classify_ride,
    intensity_count,
    last_ride_level,
    load_series,
    mean_excluding_outliers,
    percentile,
    resolve_adaptive_threshold,
    rolling_load,
    rutina_de_ayer,
    senales_producidas,
    week_start,
    zone_percentages,
)

from tests.conftest import LUNES, dias, ride

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
    # `weekend: {days: [...]}` ya no existe en `cycling`: el resumen del fin de
    # semana se borró con el recuento rodante y el config_loader rechaza la
    # clave por su nombre. Dejarla aquí haría que este diccionario describiera
    # una configuración que el sistema real no acepta.
    "recommendation": {
        "lookback_days": 1,
        "intensity_count": {
            "enabled": True,
            "window_days": 7,
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


# Aquí estaba `test_previous_weekday_es_estrictamente_anterior`. La función que
# comprobaba se ha borrado con `weekend_summary` y con la nota de fin de semana
# de la bici, que eran sus dos únicos llamantes. El test era correcto y por eso
# se va: un test verde sobre código que no ejecuta nadie es cobertura de adorno,
# y encima de las que peor envejecen, porque cuenta en el total.
#
# `week_start` sí sigue, con su test justo encima: lo usan el ciclo de descarga
# y `tendencia`, donde la semana natural es la unidad real.


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


# Estos tres miden la ARITMÉTICA de la ventana, y para eso hace falta que la
# ventana esté observada de punta a punta. Antes no hacía falta decirlo porque
# un día sin datos valía cero igual que un día de sofá; desde que `rolling_load`
# distingue las dos cosas, cada uno de estos casos lleva un ancla vieja y fuera
# de la ventana que se mide. El ancla no cambia ninguna suma: solo declara desde
# cuándo se ha mirado, que es justo lo que antes se daba por supuesto.
ANCLA = LUNES - timedelta(days=30)


def test_la_ventana_de_carga_incluye_el_dia_actual():
    rides = classify_all([ride(ANCLA, load=999), ride(LUNES, load=100)], CYCLING)
    assert rolling_load(rides, LUNES, 3) == 100.0
    assert rolling_load(rides, LUNES - timedelta(days=1), 3) == 0.0


def test_la_ventana_de_tres_dias_cubre_hoy_y_los_dos_anteriores():
    rides = classify_all(
        [ride(ANCLA, load=999)]
        + [ride(LUNES - timedelta(days=i), load=10) for i in range(5)],
        CYCLING,
    )
    assert rolling_load(rides, LUNES, 3) == 30.0
    assert rolling_load(rides, LUNES, 7) == 50.0


def test_load_series_devuelve_un_valor_por_dia():
    rides = classify_all([ride(ANCLA, load=999), ride(LUNES, load=100)], CYCLING)
    serie = load_series(rides, LUNES, days=10, window_days=3)
    assert len(serie) == 10
    assert serie[LUNES - timedelta(days=5)] == 0.0


def test_la_serie_no_mete_la_salida_del_propio_dia_en_su_casilla():
    """El día d vale [d-N, d-1]: la salida de d NO entra en `load_Nd` de d.

    ES EL TEST QUE FALTABA, Y POR ESO EL SESGO VIVIÓ SEIS MESES
    -----------------------------------------------------------
    `rolling_load` está bien y sus tests están bien: su ventana incluye el día y
    eso es lo que dicen. `resolve_adaptive_threshold` está bien y sus tests
    también: su ventana termina ayer y eso es lo que dice. El fallo no estaba en
    ninguna de las dos, estaba en que la serie que la primera construye es la
    misma que la segunda resume, y a las 07:00 el último elemento de esa serie
    está medido con una magnitud distinta de todos los demás -le falta la salida
    de hoy, que aún no existe-. Comparar ese elemento contra el percentil de los
    otros es comparar N-1 días contra N.

    Ningún test podía cazarlo mirando una función sola. Este mira la serie, que
    es donde vive la propiedad: TODOS los días valen lo mismo, incluido el
    último, y por eso el percentil significa algo.
    """
    rides = classify_all(
        [ride(ANCLA, load=999)]
        + [ride(LUNES - timedelta(days=i), load=10) for i in range(5)],
        CYCLING,
    )
    serie = load_series(rides, LUNES, days=10, window_days=3)

    # Tres días de a 10 terminando AYER: LUNES-3, LUNES-2 y LUNES-1. La salida de
    # hoy vale 10 y no está sumada. Con la ventana vieja aquí salía 30 contando a
    # LUNES y dejando fuera a LUNES-3, el mismo número por los días equivocados.
    assert serie[LUNES] == 30.0
    assert rolling_load(rides, LUNES, 3) == 30.0, "`rolling_load` no cambia"

    # Y la propiedad que hace comparable la serie: cada casilla es la suma de los
    # N días anteriores, la de hoy igual que las demás.
    for i in range(6):
        d = LUNES - timedelta(days=i)
        assert serie[d] == rolling_load(rides, d - timedelta(days=1), 3)


# ---------------------------------------------------------------------------
# Un día que no se miró no es un día de sofá
# ---------------------------------------------------------------------------
#
# EL FALLO: `sum([])` valía 0.0, así que un día anterior a la primera actividad
# conocida daba una carga de cero idéntica a la de un día real sin bici. Los dos
# ceros eran indistinguibles a partir de ahí.
#
# Lo que rompía no era el número, era la guarda de al lado. `min_days_required`
# existe para no calcular un percentil con cuatro datos, y esos ceros la
# cumplían de sobra: el 2026-03-15, sobre el histórico de verdad, la ventana de
# 60 días de `load_2d_p90` llevaba 52 ceros de días sin ningún dato y 8 días
# medidos. El percentil salía de los ceros y la primera salida real lo superaba
# sin despeinarse.
#
# Y no lo cazó nadie porque el síntoma era invisible: en el replay de seis meses
# `carga_acumulada` disparó 32 veces y se saltó CERO. Una regla que en 181 días
# nunca dice "no sé" no es una regla robusta, es una regla que no está mirando
# lo que cree. Tras el arreglo son 21 disparos y 24 saltadas.


def test_un_dia_anterior_a_la_primera_salida_conocida_no_vale_cero():
    """Cero significa 'miré y no hubo bici', y de ese día no se miró nada."""
    rides = classify_all([ride(LUNES, load=100)], CYCLING)
    # La ventana de 3 días que termina en LUNES empieza en LUNES-2, y de LUNES-2
    # no se sabe nada: la primera -y única- observación es la del propio LUNES.
    assert rolling_load(rides, LUNES, 3) is None
    # En cambio la ventana de 1 día está observada entera y sí vale.
    assert rolling_load(rides, LUNES, 1) == 100.0


def test_sin_ninguna_salida_no_hay_carga_que_dar():
    """Sin una sola observación no hay horizonte, y cero sería inventárselo."""
    assert rolling_load([], LUNES, 3) is None


def test_el_cero_de_un_dia_observado_sigue_siendo_un_cero():
    """El arreglo no puede comerse los ceros de verdad, que son la mayoría."""
    rides = classify_all([ride(ANCLA, load=50)], CYCLING)
    # Días muy posteriores al ancla: observados, sin bici, carga cero de verdad.
    assert rolling_load(rides, LUNES, 3) == 0.0
    assert rolling_load(rides, LUNES, 7) == 0.0


def test_los_dias_sin_mirar_no_le_cuentan_al_minimo_del_percentil():
    """La consecuencia real: `min_days_required` vuelve a ser un mínimo de datos.

    Es el test que habría cazado el fallo. Sin él, la serie llega llena de ceros
    fabricados, el mínimo se cumple y el percentil se calcula contra la nada.
    """
    rides = classify_all([ride(LUNES, load=100)], CYCLING)
    serie = load_series(rides, LUNES, days=90, window_days=3)
    medidos = [v for v in serie.values() if v is not None]
    # Ni uno. Con una sola observación no hay ninguna ventana de tres días
    # cubierta de punta a punta, ni siquiera la que termina en el propio LUNES:
    # empieza en LUNES-2 y de ese día no se sabe nada. Antes esta misma serie
    # llegaba con 90 ceros.
    assert medidos == [], "ninguna ventana de 3 días está observada entera"

    valor, motivo = resolve_adaptive_threshold(
        {"window_days": 60, "min_days_required": 30, "percentile": 90},
        serie,
        LUNES,
    )
    assert valor is None
    assert motivo, "y tiene que decir por qué, no callarse"


def test_la_serie_de_carga_cubre_la_ventana_que_pide_el_config():
    """`adaptive_thresholds.*.window_days` manda; aquí había un 90 escrito a mano.

    El 90 sobraba mientras la ventana más larga fuera de 60. Subirla a 120 o 180
    -que es lo que pide el criterio de calcular los umbrales contra la propia
    distribución- dejaba el percentil calculado sobre 90 días sin decir nada: ni
    error, ni nota, ni forma de verlo desde fuera. El valor que se lee en el YAML
    dejaba de ser el valor que se usa, otra vez.
    """
    from tests.conftest import REPO_ROOT

    cfg = load_config(REPO_ROOT / "config.yaml")
    rides = [ride(LUNES - timedelta(days=i), load=50) for i in range(0, 250, 3)]
    mets = dias(LUNES, 250, hrv=60, rhr=50, sleep_min=420)

    for ventana in (60, 120, 180):
        c = copy.deepcopy(cfg)
        # El umbral se declara aquí y no se coge del `config.yaml`: la sección
        # está vacía desde que se borró `carga_acumulada`, que era su único
        # lector. Lo que prueba este test es la maquinaria -pedir N días hace
        # que la serie tenga N días-, y esa sigue viva y sigue siendo la que
        # muerde el día que se declare un umbral nuevo.
        c.raw["adaptive_thresholds"]["load_2d_p90"] = {
            "metric": "load_2d",
            "window_days": ventana,
            "percentile": 90,
            "min_days_required": 30,
            "include_zero_days": True,
        }
        sig = build_signals(
            c, LUNES, metrics=mets, rides=rides,
            sessions=[], checkin_history=[], checkin=None,
        )
        serie = sig.history["load_2d"]
        assert len(serie) >= ventana + 1, (
            f"con window_days={ventana} la serie tiene {len(serie)} días: el "
            f"percentil se estaría calculando sobre menos ventana de la pedida"
        )


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
    `carga_acumulada` sin evaluar y sin saber por qué.

    LA SALIDA SIN CARGA ESTÁ AYER, NO HOY, Y ESO ES EL TEST
    -------------------------------------------------------
    Antes se ponía en el propio LUNES. Desde que la ventana termina la víspera,
    una salida de hoy NO entra en `load_Nd` de hoy: el valor seguía saliendo
    `None` -por falta de horizonte- y el test seguía verde acusando a un día que
    ya no tenía nada que ver. Habría certificado para siempre una nota que
    nombra el día equivocado.
    """
    from app.engine.signals import build_signals

    ayer = LUNES - timedelta(days=1)
    s = build_signals(
        cfg,
        LUNES,
        metrics=[],
        rides=[
            # Ancla vieja: declara desde cuándo se ha mirado, para que el `None`
            # no venga de la guarda de horizonte sino de la carga desconocida.
            ride(ANCLA, load=50),
            Ride(date=ayer, duration_s=7200, is_cycling=True),
        ],
        sessions=[],
        checkin_history=[],
        checkin=None,
    )
    assert s.values["load_7d"] is None
    nota = next((n for n in s.notes if n.startswith("load_7d")), None)
    assert nota is not None, f"ninguna nota explica el hueco: {s.notes}"
    assert ayer.isoformat() in nota, "hay que decir QUÉ día está sin clasificar"
    assert LUNES.isoformat() not in nota, (
        "hoy no entra en la ventana: acusarlo sería señalar al día equivocado"
    )


# ---------------------------------------------------------------------------
# Umbrales adaptativos
# ---------------------------------------------------------------------------
# El usuario los quiere calculados contra su propia distribución histórica, no
# contra constantes absolutas. Estos tests fijan las tres condiciones bajo las
# que el umbral NO existe: poco histórico, ventana degenerada, y el hecho de
# que la ventana termina ayer.


SPEC = {
    "metric": "load_2d",
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
# Recuento rodante de intensidad
# ---------------------------------------------------------------------------
#
# AQUÍ HABÍA DOS TESTS DE `weekend_summary` Y SE VAN CON ELLA
# ------------------------------------------------------------
# `test_una_salida_sin_clasificar_deja_el_fin_de_semana_en_no_se_sabe` y
# `test_una_intensa_confirmada_manda_aunque_falte_clasificar_otra`. Los dos
# comprobaban cómo se resolvía `intense_rides` del fin de semana cuando había
# salidas sin clasificar, y los dos justificaban su existencia diciendo "la
# regla del lunes se salta" — una regla (`resaca_finde`) que se borró hace
# tiempo. O sea que llevaban meses verdes defendiendo el comportamiento de algo
# que ya no se ejecutaba.
#
# La distinción que SÍ importaba -una salida sin clasificar no es un paseo- no
# se pierde: vive en `unknown` de `IntensityCount` y tiene su propio test más
# abajo, `test_una_salida_sin_clasificar_no_se_cuenta_como_paseo`.


def test_el_recuento_cuenta_lo_ejecutado_no_lo_programado():
    rides = classify_all([ride(LUNES, zones=(0, 0, 0, 900, 900))], CYCLING)
    hecho = [StrengthSession(date=LUNES, routine_key="dia_1", is_hiit=True)]
    c = intensity_count(rides, hecho, LUNES, CYCLING)
    assert c.used == 2

    c2 = intensity_count(rides, hecho + [
        StrengthSession(date=LUNES, routine_key="dia_2", is_hiit=True)
    ], LUNES, CYCLING)
    assert c2.used == 3


def test_el_recuento_no_tiene_techo_por_alto_que_suba():
    """La prueba de que esto ya no es un presupuesto.

    Siete días de bici intensa seguidos es exactamente el caso que el usuario
    puso: una semana de viaje. Antes eso era "presupuesto agotado" desde la
    cuarta salida y recorte en las tres siguientes. Ahora es un 7, y el 7 se
    enseña. Si esa semana deja al cuerpo hecho polvo, quien lo dirá es el
    semáforo por HRV, sueño, pulso de reposo y carga de Garmin, no una cuenta.
    """
    rides = classify_all(
        [
            ride(LUNES + timedelta(days=i), zones=(0, 0, 0, 900, 900), activity_id=i)
            for i in range(7)
        ],
        CYCLING,
    )
    c = intensity_count(rides, [], LUNES + timedelta(days=6), CYCLING)
    assert c.used == 7
    assert not hasattr(c, "limit")
    assert not hasattr(c, "exhausted")
    assert "llevas 7 sesiones intensas" in c.linea()


def test_la_linea_del_recuento_no_lleva_denominador():
    """Un "de 4" detrás convierte un dato en un aprobado o un suspenso.

    La frase la lee el usuario en el móvil cada mañana. Mientras diga "llevas
    3", es información. En cuanto diga "3 de 4", es un marcador, y un marcador
    prescribe aunque el código no recorte nada.
    """
    rides = classify_all([ride(LUNES, zones=(0, 0, 0, 900, 900))], CYCLING)
    linea = intensity_count(rides, [], LUNES, CYCLING).linea()
    assert "de 4" not in linea and "/" not in linea
    assert "llevas 1 sesion intensa en los ultimos 7 dias" in (
        linea.replace("ó", "o").replace("í", "i").replace("ú", "u")
    )


def test_sin_nada_hecho_la_linea_lo_dice_en_positivo():
    """Cero no es un hueco: es el dato de que no se ha apretado en una semana."""
    linea = intensity_count([], [], LUNES, CYCLING).linea()
    assert "ninguna" in linea
    # El "0" no puede salir como cifra, pero el periodo sí lleva un número: lo
    # que se comprueba es que no aparezca un cero, no que no aparezca ningún
    # dígito. Antes bastaba con `"0" not in linea` porque la frase no llevaba
    # cifras ningunas; ahora lleva el ancho de la ventana.
    assert "0" not in linea.replace("en los últimos 7 días", "")


def test_la_linea_dice_el_periodo_en_vez_de_darlo_por_supuesto():
    """«Esta semana» obliga a saber qué día es hoy; «7 días» no.

    Y se fue el «todavía». Decía «ninguna sesión intensa esta semana todavía», y
    ese «todavía» presuponía un periodo abierto por llenar, o sea una cuota
    implícita justo en la frase escrita para no tener cuota. En una ventana
    rodante no hay nada pendiente: los siete días de atrás ya pasaron enteros.
    """
    vacia = intensity_count([], [], LUNES, CYCLING).linea()
    assert "en los últimos 7 días" in vacia
    assert "todavía" not in vacia
    assert "semana" not in vacia

    rides = classify_all([ride(LUNES, zones=(0, 0, 0, 900, 900))], CYCLING)
    llena = intensity_count(rides, [], LUNES, CYCLING).linea()
    assert "en los últimos 7 días" in llena
    assert "semana" not in llena


def test_el_periodo_de_la_frase_es_el_que_dice_el_config():
    """Si `window_days` cambia, la frase cambia con él y no se queda en 7.

    Es la comprobación de que no hay ningún 7 escrito en el texto. Un literal
    ahí dejaría el mensaje diciendo «en los últimos 7 días» mientras la cuenta
    mira catorce, que es la forma más limpia que hay de publicar un número con
    las unidades equivocadas.
    """
    for ancho in (3, 7, 14, 21):
        cyc = copy.deepcopy(CYCLING)
        cyc["recommendation"]["intensity_count"]["window_days"] = ancho
        c = intensity_count([], [], LUNES, cyc)
        assert c.dias == ancho
        assert c.desde == LUNES - timedelta(days=ancho - 1)
        assert c.hasta == LUNES
        assert f"en los últimos {ancho} días" in c.linea()


def test_la_linea_concuerda_el_plural_en_vez_de_escribir_parentesis():
    """"sesion(es)" delata un texto de máquina, y esto lo lee una persona.

    No es cosmética suelta: el recuento existe solo para que alguien lo lea a
    las siete de la mañana. Un texto que parece generado se salta con la vista,
    y un dato que se salta con la vista es exactamente igual de útil que uno que
    no se calcula.
    """
    from app.engine.signals import IntensityCount

    def linea(used, unknown=0):
        return IntensityCount(
            used=used,
            detail=[],
            desde=LUNES - timedelta(days=6),
            hasta=LUNES,
            unknown=unknown,
        ).linea()

    assert "(s)" not in linea(1) and "(es)" not in linea(1)
    assert "1 sesión intensa en los últimos 7 días" in linea(1)
    assert "3 sesiones intensas en los últimos 7 días" in linea(3)
    assert "1 salida sin clasificar que pudo serlo" in linea(2, unknown=1)
    assert "2 salidas sin clasificar que pudieron serlo" in linea(2, unknown=2)
    for u in range(0, 6):
        for k in range(0, 3):
            assert "(s)" not in linea(u, k), (u, k)


def test_una_salida_sin_clasificar_no_se_cuenta_como_paseo():
    """El agujero: `used` solo sumaba `level == "intensa"`.

    Una salida que Garmin no pudo clasificar -sin zonas de FC y sin Training
    Effect- salía 'desconocida' y no se contaba. No es que se contara mal: se
    contaba como si se supiera, y no se sabía. Ignorarla es afirmar que fue un
    paseo, que está tan inventado como decir que fue intensa.

    Cuando esto era un presupuesto, el sesgo caía del lado de quitar el freno.
    Ahora no hay freno que quitar y el daño es otro, más pequeño y más tonto:
    el número que sale en el mensaje sería falso por abajo. Por eso `used` se
    declara como un MÍNIMO y `unknown` viaja al lado.
    """
    rides = classify_all(
        [
            ride(LUNES, zones=(0, 0, 0, 900, 900), activity_id=1),  # intensa
            Ride(date=LUNES, duration_s=3600, activity_id=2),  # desconocida
        ],
        CYCLING,
    )
    c = intensity_count(rides, [], LUNES, CYCLING)
    assert c.used == 1, "la desconocida no puede sumar como intensa"
    assert c.unknown == 1, "pero tampoco puede desaparecer"
    assert any("SIN CLASIFICAR" in d for d in c.detail)
    assert "sin clasificar" in c.linea()


def test_la_intensa_de_ayer_cuenta_aunque_fuera_otra_semana():
    """ESTE TEST DECÍA LO CONTRARIO, Y ERA EL DEFECTO ESCRITO EN VERDE.

    Se llamaba `test_la_intensidad_de_la_semana_pasada_no_cuenta` y afirmaba
    que una salida intensa el domingo NO cuenta el lunes. Pasaba, porque el
    código hacía eso. Y lo que describía era el sistema diciéndole al usuario
    «ninguna sesión intensa esta semana todavía» el lunes por la mañana, doce
    horas después de una salida en Z4-Z5.

    Medido sobre la caché real -58 salidas, 184 días-, eso pasó en 8 de 26
    lunes: el 31%. La cuenta natural y la rodante de 7 días discrepan en 39 de
    los 184 días, y el signo es siempre el mismo: la natural nunca cuenta de
    más. Con una hernia L4-L5 detrás, el error sistemático hacia «vas
    descansado» no es el lado en el que uno quiere equivocarse.

    Ahora la ventana rueda y el domingo entra, que es lo que el cuerpo sabía
    desde el principio.
    """
    ayer = LUNES - timedelta(days=1)  # domingo: otra semana natural
    rides = classify_all([ride(ayer, zones=(0, 0, 0, 900, 900))], CYCLING)
    c = intensity_count(rides, [], LUNES, CYCLING)
    assert c.used == 1
    assert c.desde == LUNES - timedelta(days=6)


def test_lo_que_cae_fuera_de_la_ventana_no_cuenta():
    """La ventana rueda, pero tiene borde: el día 7 hacia atrás ya no entra.

    Se comprueban los dos lados del corte con la misma salida movida un día,
    porque un `<=` por un `<` aquí no daría error: daría otro número, y un
    número plausible. Es el modo de fallo que este proyecto ya ha pagado dos
    veces con los percentiles.
    """
    dentro = LUNES - timedelta(days=6)
    fuera = LUNES - timedelta(days=7)
    assert intensity_count(
        classify_all([ride(dentro, zones=(0, 0, 0, 900, 900))], CYCLING),
        [], LUNES, CYCLING,
    ).used == 1
    assert intensity_count(
        classify_all([ride(fuera, zones=(0, 0, 0, 900, 900))], CYCLING),
        [], LUNES, CYCLING,
    ).used == 0


def test_el_recuento_no_depende_del_dia_de_la_semana():
    """La comprobación de que el calendario se ha ido de verdad.

    El mismo historial relativo -una intensa anteayer- mirado desde los siete
    días de la semana tiene que dar siete veces lo mismo. Con `week_starts_on`
    no lo daba: daba 1 de martes a domingo y 0 los lunes, porque el lunes el
    corte caía en medio.

    Está escrito con el historial RELATIVO a cada día y no con fechas fijas a
    propósito: si se fijaran las fechas, lo que se mediría sería otra cosa -qué
    días caen dentro de una ventana concreta- y volvería a depender del
    calendario por la puerta de atrás.
    """
    vistos = set()
    for i in range(7):
        hoy = LUNES + timedelta(days=i)
        rides = classify_all(
            [ride(hoy - timedelta(days=2), zones=(0, 0, 0, 900, 900))], CYCLING
        )
        c = intensity_count(rides, [], hoy, CYCLING)
        vistos.add((c.used, c.dias, c.linea()))
    assert len(vistos) == 1, f"el día de la semana cambia el recuento: {vistos}"


def test_sin_window_days_el_recuento_para_en_vez_de_suponer_siete():
    """El periodo sale escrito en el mensaje, así que no se puede inventar.

    Es la diferencia con `counts_as_intense`, que sí tiene defectos: si falta
    `hiit_executed`, el número cambia pero sigue queriendo decir lo que dice.
    Si falta `window_days` y el código supone un 7, la frase «en los últimos 7
    días» se convierte en una afirmación sobre un periodo que nadie eligió, y
    el que la lee no tiene forma de sospecharlo.
    """
    from app.engine.signals import IntensityCountConfigError

    cyc = copy.deepcopy(CYCLING)
    del cyc["recommendation"]["intensity_count"]["window_days"]
    with pytest.raises(IntensityCountConfigError, match="window_days"):
        intensity_count([], [], LUNES, cyc)

    # `window_days: yes` en YAML es `True`, y `isinstance(True, int)` es cierto
    # en Python: sin la guarda explícita del bool, esa errata contaría una
    # ventana de un día y el mensaje diría "hoy" tan tranquilo.
    for malo in (True, 0, -3, 7.0, "7", None):
        cyc["recommendation"]["intensity_count"]["window_days"] = malo
        with pytest.raises(IntensityCountConfigError):
            intensity_count([], [], LUNES, cyc)


def test_la_clave_de_la_semana_natural_no_puede_volver_en_silencio():
    """`week_starts_on` reaparecido tiene que doler, no ignorarse.

    Quien la escriba creerá estar eligiendo por dónde corta el contador. No
    corta por ningún sitio, así que no pasaría nada — que es exactamente el
    fallo que este proyecto lleva meses pagando, y en la dirección peor: la del
    silencio que se parece a que funciona.
    """
    from app.engine.signals import IntensityCountConfigError

    cyc = copy.deepcopy(CYCLING)
    cyc["recommendation"]["intensity_count"]["week_starts_on"] = "monday"
    with pytest.raises(IntensityCountConfigError, match="week_starts_on"):
        intensity_count([], [], LUNES, cyc)


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
# Qué rutina se entrenó ayer
#
# La pareja de `yesterday_rpe`. El freno `rpe_alto` lleva `blocks:
# last_session_only`, y `apply_progression` lo acota con esta señal. Como
# `build_signals` no la escribía, valía `None` siempre y el `in (None,
# routine_key)` del freno se cumplía para cualquier rutina: `last_session_only`
# bloqueaba tanto como `all`.
#
# `None` sigue queriendo decir «bloquea todo», así que estos tests son sobre
# cuándo se puede afirmar algo y cuándo hay que callarse.
# ---------------------------------------------------------------------------


def _ayer(*rutinas: str | None) -> list[StrengthSession]:
    ayer = LUNES - timedelta(days=1)
    return [StrengthSession(date=ayer, routine_key=r) for r in rutinas]


def test_la_rutina_de_ayer_es_la_que_se_entreno_ayer():
    assert rutina_de_ayer(_ayer("dia_2"), LUNES) == "dia_2"


def test_una_sesion_partida_en_dos_ratos_sigue_siendo_una_rutina():
    """Lo normal: dos entrenamientos de Hevy el mismo día, la misma rutina."""
    assert rutina_de_ayer(_ayer("dia_2", "dia_2"), LUNES) == "dia_2"


def test_dos_rutinas_distintas_ayer_no_se_eligen_a_cara_o_cruz():
    """El formulario tiene UN deslizador de RPE y no dice de cuál habla.

    Acotar el freno a una de las dos sería elegir por el usuario, y la mitad de
    las veces se elegiría la que no era.
    """
    assert rutina_de_ayer(_ayer("dia_1", "dia_2"), LUNES) is None


def test_un_entrenamiento_que_no_casa_con_ninguna_rutina_no_acota_nada():
    assert rutina_de_ayer(_ayer(None), LUNES) is None


def test_sin_entrenar_ayer_no_hay_rutina_a_la_que_acotar():
    assert rutina_de_ayer([], LUNES) is None


def test_lo_de_anteayer_no_es_lo_de_ayer():
    """El deslizador pregunta por AYER, no por la última vez que se entrenó."""
    anteayer = [StrengthSession(date=LUNES - timedelta(days=2), routine_key="dia_1")]
    assert rutina_de_ayer(anteayer, LUNES) is None


# ---------------------------------------------------------------------------
# build_signals contra el config real
# ---------------------------------------------------------------------------


def test_el_catalogo_de_senales_es_exactamente_lo_que_se_produce(cfg):
    """El test que sostiene el validador de `source`.

    `config_loader` rechaza el arranque si el YAML nombra una señal que no
    fabrica nadie. Eso solo se puede hacer con una lista, y una lista escrita a
    mano se queda atrás a la primera señal nueva: entonces el validador empieza
    a rechazar nombres BUENOS, que es peor que no validar, porque el sistema no
    arranca y el error señala al fichero de configuración, que está bien.

    Así que la lista no puede diferir de la producción ni en un nombre, en
    ninguna de las dos direcciones. Este test es esa igualdad.
    """
    s = build_signals(
        cfg,
        LUNES,
        metrics=[DayMetrics(date=LUNES, hrv=60.0, rhr=50.0)],
        rides=[],
        sessions=[],
        checkin_history=[],
    )
    assert set(s.values) == senales_producidas(cfg)


def test_el_recuento_apagado_no_entra_en_el_catalogo(cfg_copia):
    """Con el bloque apagado la señal no se escribe, así que no existe.

    Y si no existe, nombrarla desde el YAML tiene que doler igual que nombrar
    una que no se haya escrito nunca: el freno que la mirara se saltaría todos
    los días por falta de datos.
    """
    rec = cfg_copia.raw["cycling"]["recommendation"]["intensity_count"]
    assert any(n.startswith("intense_count_") for n in senales_producidas(cfg_copia))
    rec["enabled"] = False
    assert not any(n.startswith("intense_count_") for n in senales_producidas(cfg_copia))


def test_el_catalogo_lleva_la_ventana_que_dice_el_config(cfg_copia):
    """El nombre lleva la ventana dentro: cambiarla cambia la señal."""
    cfg_copia.raw["cycling"]["recommendation"]["intensity_count"]["window_days"] = 10
    nombres = senales_producidas(cfg_copia)
    assert "intense_count_10d" in nombres
    assert "intense_count_7d" not in nombres


def test_el_selector_no_es_una_senal(cfg):
    """No llega a `values` a propósito: es una cadena, no una medida."""
    assert CLAVE_SESION_ELEGIDA not in senales_producidas(cfg)


def test_la_rutina_de_ayer_llega_a_las_senales(cfg):
    """Sin esto la señal existe y el freno no la ve, que es donde estábamos."""
    s = build_signals(
        cfg,
        LUNES,
        metrics=[],
        rides=[],
        sessions=_ayer("dia_3"),
        checkin_history=[],
    )
    assert s.values["yesterday_routine"] == "dia_3"


def test_build_signals_no_inventa_lineas_base_sin_datos(cfg):
    s = build_signals(cfg, LUNES, metrics=[], rides=[], sessions=[], checkin_history=[])
    assert s.values["hrv_baseline"] is None
    assert s.values["hrv_ratio"] is None
    assert any("sin línea base" in n for n in s.notes)


def test_la_linea_base_excluye_el_dia_de_hoy(cfg):
    """Meter el valor de hoy en su propia media lo amortiguaría."""
    metrics = [DayMetrics(date=LUNES - timedelta(days=i), hrv=100.0) for i in range(1, 8)]
    metrics.append(DayMetrics(date=LUNES, hrv=50.0))  # hoy, muy bajo
    s = build_signals(cfg, LUNES, metrics=metrics, rides=[], sessions=[], checkin_history=[])
    assert s.values["hrv_baseline"] == pytest.approx(100.0)
    assert s.values["hrv_ratio"] == pytest.approx(0.5)


def test_sin_checkin_se_dice_en_las_notas(cfg):
    s = build_signals(cfg, LUNES, metrics=[], rides=[], sessions=[], checkin_history=[])
    assert any("sin check-in" in n for n in s.notes)
    for key in cfg.slider_keys():
        assert s.values[key] is None


def test_el_checkin_llega_a_las_señales(cfg):
    c = Checkin(date=LUNES, values={"lower_discomfort": 4})
    s = build_signals(cfg, LUNES, metrics=[], rides=[], sessions=[], checkin_history=[], checkin=c)
    assert s.values["lower_discomfort"] == 4
    assert s.history["lower_discomfort"][LUNES] == 4


def test_una_señal_a_none_cuenta_como_ausente(cfg):
    s = build_signals(cfg, LUNES, metrics=[], rides=[], sessions=[], checkin_history=[])
    assert not s.has("hrv")
    assert s.get("hrv") is None


def test_el_snapshot_es_serializable(cfg):
    import json

    s = build_signals(cfg, LUNES, metrics=[], rides=[], sessions=[], checkin_history=[])
    blob = json.dumps(s.snapshot(), ensure_ascii=False, default=str)
    assert json.loads(blob)["day"] == LUNES.isoformat()


def test_el_historico_largo_de_salidas_da_umbrales_adaptativos(cfg):
    """La razón de ser de la caché larga, vista desde el percentil.

    Con solo la última semana de salidas, la ventana de 60 días está casi toda
    a cero y el percentil 90 sale 0: el guardia lo anula y la regla que lo lea
    no se evalúa. Con el histórico largo el umbral existe.

    Los dos umbrales se declaran aquí porque el `config.yaml` ya no trae
    ninguno: `carga_acumulada` era su único lector y se borró. Lo que sigue
    vivo -y lo que este test protege- es que declarar uno funcione, incluyendo
    el orden en que `build_signals` construye las series y resuelve los
    percentiles: si el bloque se resolviera antes de existir la serie, el
    efecto sería una regla muda sin un solo error.
    """
    cfg = copy.deepcopy(cfg)
    for nombre, metrica in (("load_2d_p90", "load_2d"), ("load_7d_p90", "load_7d")):
        cfg.raw["adaptive_thresholds"][nombre] = {
            "metric": metrica,
            "window_days": 60,
            "percentile": 90,
            "min_days_required": 30,
            "include_zero_days": True,
        }

    pocas = [ride(LUNES - timedelta(days=i), load=100) for i in (0, 3)]
    muchas = [ride(LUNES - timedelta(days=i), load=100) for i in range(90)]

    s_pocas = build_signals(cfg, LUNES, metrics=[], rides=pocas, sessions=[], checkin_history=[])
    s_muchas = build_signals(cfg, LUNES, metrics=[], rides=muchas, sessions=[], checkin_history=[])

    assert s_pocas.adaptive.get("load_2d_p90") is None
    assert s_muchas.adaptive.get("load_2d_p90") is not None
    assert s_muchas.adaptive["load_2d_p90"] > 0
    assert s_muchas.adaptive.get("load_7d_p90") is not None


# ---------------------------------------------------------------------------
# El histórico de check-ins: la mina que estaba armada y dormida
# ---------------------------------------------------------------------------
#
# `checkin_history` tenía `= ()` y ningún caller de producción se lo pasaba,
# igual que `sessions`. La diferencia es que este todavía no había explotado:
# los dos únicos umbrales adaptativos del config miran carga derivada de las
# salidas, que se construye por otro camino, así que la serie de un punto no la
# leía nadie. Se arregla AHORA, estando dormida, porque la recalibración de
# octubre quiere umbrales adaptativos sobre la lumbar y el cansancio, y ese día
# el fallo sería un percentil calculado sobre un solo dato con cara de
# estadística sobre el histórico propio.


def test_sin_historico_la_serie_de_un_deslizador_tiene_un_punto(cfg):
    """El estado anterior, escrito para que se vea contra qué se compara."""
    c = Checkin(date=LUNES, values={"lower_discomfort": 4})
    s = build_signals(
        cfg, LUNES, metrics=[], rides=[], sessions=[], checkin_history=[], checkin=c
    )
    assert list(s.history["lower_discomfort"]) == [LUNES]


def test_el_historico_de_checkins_llega_a_la_serie(cfg):
    """Lo que hace que el parámetro deje de ser decorativo."""
    historial = [
        Checkin(date=LUNES - timedelta(days=i), values={"lower_discomfort": i})
        for i in range(1, 11)
    ]
    c = Checkin(date=LUNES, values={"lower_discomfort": 4})
    s = build_signals(
        cfg,
        LUNES,
        metrics=[],
        rides=[],
        sessions=[],
        checkin_history=historial,
        checkin=c,
    )
    serie = s.history["lower_discomfort"]
    assert len(serie) == 11, f"faltan días en la serie: {sorted(serie)}"
    assert serie[LUNES] == 4
    assert serie[LUNES - timedelta(days=10)] == 10


def test_un_umbral_adaptativo_sobre_un_deslizador_sale_de_la_serie_entera(
    cfg_copia,
):
    """El escenario de octubre, escrito hoy para que no llegue a existir.

    Se define el umbral que la recalibración va a querer -percentil sobre la
    lumbar- y se comprueba que sale del histórico y no del día de hoy. Sin
    `checkin_history`, `resolve_adaptive_threshold` recibía una serie de un
    punto: con `min_days_required` bajo habría devuelto ese punto como
    percentil -un umbral que se cumple siempre o nunca- y con él alto habría
    devuelto None y dejado la regla muda para siempre. Las dos formas de
    equivocarse son silenciosas.
    """
    cfg_copia.raw["adaptive_thresholds"]["lumbar_p90"] = {
        "metric": "lower_discomfort",
        "window_days": 60,
        "percentile": 90,
        "min_days_required": 10,
        "include_zero_days": True,
    }
    historial = [
        Checkin(date=LUNES - timedelta(days=i), values={"lower_discomfort": 2})
        for i in range(1, 31)
    ]

    sin = build_signals(
        cfg_copia, LUNES, metrics=[], rides=[], sessions=[], checkin_history=[]
    )
    assert sin.adaptive["lumbar_p90"] is None
    assert any("no hay serie" in n for n in sin.notes), (
        f"el umbral se anula sin decir por qué: {sin.notes}"
    )

    con = build_signals(
        cfg_copia, LUNES, metrics=[], rides=[], sessions=[], checkin_history=historial
    )
    assert con.adaptive["lumbar_p90"] == pytest.approx(2.0)


def test_el_dia_de_hoy_no_entra_en_su_propio_percentil(cfg_copia):
    """Un umbral que ya incluye el valor de hoy es más difícil de superar hoy.

    `resolve_adaptive_threshold` cierra la ventana AYER a propósito, y eso solo
    significa algo si `checkin_history` trae ayer. Con la serie de un punto la
    ventana quedaba vacía y la precaución no protegía de nada.
    """
    cfg_copia.raw["adaptive_thresholds"]["lumbar_p90"] = {
        "metric": "lower_discomfort",
        "window_days": 60,
        "percentile": 90,
        "min_days_required": 5,
        "include_zero_days": True,
    }
    historial = [
        Checkin(date=LUNES - timedelta(days=i), values={"lower_discomfort": 1})
        for i in range(1, 11)
    ]
    hoy = Checkin(date=LUNES, values={"lower_discomfort": 9})

    s = build_signals(
        cfg_copia,
        LUNES,
        metrics=[],
        rides=[],
        sessions=[],
        checkin_history=historial,
        checkin=hoy,
    )
    assert s.history["lower_discomfort"][LUNES] == 9, "hoy sí está en la serie"
    assert s.adaptive["lumbar_p90"] == pytest.approx(1.0), (
        "el 9 de hoy ha entrado en el percentil contra el que se compara el 9"
    )


def test_olvidarse_del_historico_es_un_error_y_no_una_serie_vacia(cfg):
    """La guarda entera. Un defecto aquí volvería a dormir la mina.

    Pasar `[]` a mano sigue valiendo -es una afirmación de quien llama, no un
    olvido- y por eso todos los tests de arriba lo hacen.
    """
    with pytest.raises(TypeError, match="checkin_history"):
        build_signals(cfg, LUNES, metrics=[], rides=[], sessions=[])


# AQUÍ ESTABA `test_los_umbrales_de_carga_siguen_saliendo_despues_de_bajar_el_bloque`.
#
# Comprobaba que mover el bloque de `adaptive_thresholds` al final del YAML no
# dejaba los dos umbrales sin serie. Leía los del `config.yaml` de verdad, y esa
# sección se quedó vacía al borrar `carga_acumulada`, su único lector.
#
# No se ha reescrito porque su propiedad -que declarar un umbral produzca un
# umbral, con el orden de construcción de `build_signals` de por medio- es
# exactamente la que comprueba
# `test_el_historico_largo_de_salidas_da_umbrales_adaptativos`, que ahora
# declara los dos a mano. Dos tests inyectando el mismo umbral para afirmar lo
# mismo no dan más cobertura: dan uno que nadie actualiza.


# ---------------------------------------------------------------------------
# Las claves de cuando esto recortaba tienen que reventar, no ignorarse
# ---------------------------------------------------------------------------
#
# `weekly_limit` se leía con `cfg.get("weekly_limit", 3)`, y ese 3 era el valor
# ANTERIOR del YAML: el que dejaba 5 de cada 25 sábados sin salida fuerte porque
# los dos HIIT del plan no cabían debajo del techo. Ese defecto es el que empezó
# toda esta revisión.
#
# Ahora el límite no existe, y aparece un modo de fallo peor que el defecto
# escondido: una clave BORRADA que se ignora en silencio. Quien escriba
# `weekly_limit: 2` en el bloque nuevo se va a quedar convencido de que se ha
# puesto un tope de dos sesiones, el fichero va a validar, y no va a pasar
# absolutamente nada. El silencio, esta vez, cae del lado de sentirse protegido
# sin estarlo, que es el peor sitio donde puede caer.


@pytest.mark.parametrize(
    "muerta",
    [
        "weekly_limit",
        "on_budget_exhausted",
        "max_intense_rides_per_weekend",
        "require_green_for_intense",
    ],
)
def test_las_claves_del_presupuesto_muerto_revientan(muerta):
    """El resto del bloque va completo A PROPÓSITO.

    Si aquí faltara `window_days`, la función reventaría igual pero por otro
    motivo, y `match=muerta` podría seguir pasando por casualidad mientras el
    test ya no comprueba lo que dice comprobar. Un bloque válido salvo por la
    clave muerta es la única forma de que el fallo que se observa sea el que se
    está buscando.
    """
    from app.engine.signals import IntensityCountConfigError

    cyc = dict(CYCLING)
    cyc["recommendation"] = {
        "lookback_days": 1,
        "intensity_count": {
            "enabled": True,
            "window_days": 7,
            muerta: 2,
        },
    }
    with pytest.raises(IntensityCountConfigError, match=muerta):
        intensity_count([], [], LUNES, cyc)


def test_el_error_de_clave_muerta_dice_por_donde_se_frena_de_verdad():
    """Un error que solo prohíbe deja a quien lo lee sin salida.

    Si alguien pone `weekly_limit` es porque quiere frenar por carga acumulada.
    Eso es una necesidad legítima y tiene sitio: el semáforo. El mensaje de
    error tiene que llevarle ahí, no limitarse a decirle que no.
    """
    from app.engine.signals import IntensityCountConfigError

    cyc = dict(CYCLING)
    cyc["recommendation"] = {
        "lookback_days": 1,
        "intensity_count": {"enabled": True, "window_days": 7, "weekly_limit": 2},
    }
    with pytest.raises(IntensityCountConfigError) as e:
        intensity_count([], [], LUNES, cyc)
    texto = str(e.value)
    assert "semáforo" in texto
    assert "carga de Garmin" in texto


def test_sin_bloque_de_recuento_no_se_cuenta_y_no_se_dice_nada():
    """No contar es una decisión legítima; contar cero es una afirmación.

    Que el bloque ESTÉ es cosa de `config_loader`, que lo exige en el YAML real.
    Aquí abajo, con un diccionario cualquiera, la función no revienta: es una
    función de cálculo, no la aduana del fichero.

    Lo que sí hace es devolver `None` y no un recuento a cero. La versión
    anterior devolvía `IntensityCount(used=0, ...)` SIN MIRAR las salidas, y ese
    cero acababa en el mensaje de la mañana como «ninguna sesión intensa hoy»:
    una frase falsa el día que sí hubo una, y sin ningún indicio de que lo
    fuera.

    Y el test que cubría esto pasaba `rides=[]`. Con la lista vacía, `used == 0`
    salía bien por los dos motivos a la vez -porque no se contó y porque no
    había nada que contar- así que no distinguía el correcto del defectuoso. Por
    eso aquí abajo hay una salida intensa de verdad: es lo único que separa una
    pregunta de una respuesta que se contesta sola.
    """
    rides = classify_all([ride(LUNES, zones=(0, 0, 0, 900, 900))], CYCLING)
    assert [r.level for r in rides] == ["intensa"], "el montaje del test"

    cyc = copy.deepcopy(CYCLING)
    del cyc["recommendation"]["intensity_count"]
    assert intensity_count(rides, [], LUNES, cyc) is None


def test_apagar_el_recuento_lo_apaga_de_verdad():
    """`enabled: false` no lo leía NADIE, y el validador juraba que sí.

    `config_loader` exige la clave, exige que sea booleana, y su mensaje de
    error dice por escrito que para no contar hay que poner `enabled: false`.
    Mientras tanto `intensity_count()` no la miraba en ningún sitio: el fichero
    validaba, el interruptor se dejaba apagar, y el número seguía saliendo en el
    mensaje todas las mañanas.

    Una opción muerta que además viene certificada como viva por el validador es
    peor que una opción muerta a secas: la primera no se nota nunca, porque
    quien la apaga se queda convencido de haberla apagado.

    Con una salida intensa dentro de la ventana, para que apagado y encendido no
    den lo mismo por casualidad.
    """
    rides = classify_all([ride(LUNES, zones=(0, 0, 0, 900, 900))], CYCLING)

    encendido = intensity_count(rides, [], LUNES, CYCLING)
    assert encendido is not None and encendido.used == 1, "el montaje del test"

    cyc = copy.deepcopy(CYCLING)
    cyc["recommendation"]["intensity_count"]["enabled"] = False
    assert intensity_count(rides, [], LUNES, cyc) is None


def test_sin_la_clave_enabled_se_cuenta_igual_y_no_se_calla():
    """El defecto de la clave ausente es contar, y hay que escribirlo aquí.

    En el YAML real la clave no puede faltar: `config_loader` la exige presente
    y booleana. Pero esta función la llaman además scripts y tests con
    diccionarios a mano, y para esos hay que elegir un defecto.

    Se elige contar, por dos motivos. Uno, que quien escribe el bloque con su
    `window_days` lo escribe para que cuente: si no, no lo escribiría. Y dos,
    porque el otro defecto -callar- es el que se rompe en silencio: un
    diccionario al que se le olvide la clave dejaría de contar sin un solo
    error, y el número simplemente no saldría en el mensaje. Los dos defectos se
    equivocan, pero solo uno de los dos se nota.

    Esto lo pinta una mutación que sobrevivió: cambiar el `True` por `False` no
    ponía rojo ni un test, porque todos los diccionarios del proyecto escriben
    `enabled` a mano. Un defecto que nadie fija es un defecto que el siguiente
    puede cambiar creyendo que da igual.
    """
    rides = classify_all([ride(LUNES, zones=(0, 0, 0, 900, 900))], CYCLING)

    cyc = copy.deepcopy(CYCLING)
    del cyc["recommendation"]["intensity_count"]["enabled"]

    c = intensity_count(rides, [], LUNES, cyc)
    assert c is not None, "sin la clave se cuenta: callar sería romperse en silencio"
    assert c.used == 1
    assert c.dias == 7


def test_con_el_recuento_apagado_la_senal_no_se_escribe_a_cero(cfg_copia):
    """Que falte la clave es incómodo de leer, y por eso es honesto.

    `sig.values` es lo que leen las reglas del YAML y lo que se guarda en la
    base para el panel y los replays. Un `intense_count_7d: 0` escrito por un
    bloque apagado es indistinguible, mirándolo, de un 0 que quiere decir «no
    has apretado esta semana», y se queda en el histórico para siempre. Un hueco
    se nota; un cero fabricado, no.

    Con una salida intensa de verdad, para que el contraste de arriba -la señal
    a 1 con el bloque encendido- no sea también un cero disfrazado.
    """
    from app.engine.signals import build_signals

    rides = [ride(LUNES, zones=(0, 0, 0, 900, 900))]

    encendida = build_signals(
        cfg_copia, LUNES, metrics=[], rides=rides, sessions=[], checkin_history=[]
    )
    assert encendida.values.get("intense_count_7d") == 1, "el montaje del test"

    cfg_copia.raw["cycling"]["recommendation"]["intensity_count"]["enabled"] = False
    apagada = build_signals(
        cfg_copia, LUNES, metrics=[], rides=rides, sessions=[], checkin_history=[]
    )

    assert apagada.intense_count is None
    claves = [k for k in apagada.values if k.startswith("intense_count")]
    assert claves == [], f"con el recuento apagado no debería haber señal: {claves}"


# ---------------------------------------------------------------------------
# Las dos preguntas de Sí/No
# ---------------------------------------------------------------------------


def test_las_preguntas_llegan_a_las_senales_y_al_historico(cfg):
    """Mismo bucle que los deslizadores: valor del día, serie y snapshot.

    Es el bucle compartido lo que cumple «se guardan y se cuentan en todas las
    métricas». Si las preguntas tuvieran su propio camino corto -asignar
    `sig.values` y ya- entrarían en el mensaje de hoy y faltarían en las
    correlaciones, en las tendencias y en el replay de un día pasado, y eso no se
    notaría hasta mirar una gráfica dentro de dos meses.
    """
    ayer = LUNES - timedelta(days=1)
    sig = build_signals(
        cfg,
        LUNES,
        metrics=[],
        rides=[],
        sessions=[],
        checkin=Checkin(
            date=LUNES, values={CLAVE_APETECE: False, CLAVE_VOY_A_ENTRENAR: True}
        ),
        checkin_history=[
            Checkin(date=ayer, values={CLAVE_APETECE: True, CLAVE_VOY_A_ENTRENAR: True})
        ],
    )

    assert sig.get(CLAVE_APETECE) is False
    assert sig.get(CLAVE_VOY_A_ENTRENAR) is True
    assert sig.series(CLAVE_APETECE) == {ayer: True, LUNES: False}
    assert sig.snapshot()["values"][CLAVE_VOY_A_ENTRENAR] is True


@pytest.mark.parametrize(
    "apetece, voy, esperada",
    [
        (True, True, False),
        (False, False, False),
        (True, False, True),
        (False, True, True),
    ],
)
def test_la_discordancia_son_las_dos_casillas_de_la_diagonal(cfg, apetece, voy, esperada):
    """Las cuatro celdas de la tabla 2x2, y la derivada que las resume.

    Las dos que discrepan no son la misma historia -«me apetecía y no fui» y «no
    me apetecía y fui» son casi opuestas- pero como binaria sirven para la
    pregunta que se quiere contestar: cuántas veces la intención y las ganas van
    por caminos distintos. El desglose de las cuatro celdas se guarda igual,
    porque las respuestas crudas siguen ahí.
    """
    sig = build_signals(
        cfg,
        LUNES,
        metrics=[],
        rides=[],
        sessions=[],
        checkin=Checkin(
            date=LUNES, values={CLAVE_APETECE: apetece, CLAVE_VOY_A_ENTRENAR: voy}
        ),
        checkin_history=[],
    )
    assert sig.get("discordancia") is esperada


@pytest.mark.parametrize(
    "valores",
    [
        {},
        {CLAVE_APETECE: True},
        {CLAVE_VOY_A_ENTRENAR: False},
    ],
)
def test_sin_las_dos_respuestas_no_hay_discordancia_ni_concordancia(cfg, valores):
    """Falta una: `None`, no `False`.

    Un `False` aquí querría decir «contestó lo mismo a las dos preguntas», y
    sobre un día en el que solo contestó una -o ninguna- eso es una afirmación
    inventada. Y no una cualquiera: es la que engorda la casilla de la
    concordancia con días vacíos, así que la correlación saldría más limpia
    cuantos menos datos hubiera. El p=0 que preocupaba sale precisamente de aquí.
    """
    sig = build_signals(
        cfg,
        LUNES,
        metrics=[],
        rides=[],
        sessions=[],
        checkin=Checkin(date=LUNES, values=valores),
        checkin_history=[],
    )
    assert sig.get("discordancia") is None


def test_ninguna_regla_del_semaforo_puede_leer_las_preguntas(cfg):
    """El guardia del config, comprobado sobre el config REAL.

    Los tests del validador prueban que RECHAZA una regla inventada. Este prueba
    lo otro: que hoy, en el fichero que de verdad se carga cada mañana, ninguna
    de las trece reglas las mira. Las dos afirmaciones hacen falta -un validador
    correcto con un config que ya lo incumpliera no serviría de nada- y esta es
    la que se romperá el día que alguien añada la regla, no el día que alguien
    toque el validador.
    """
    from app.engine.tendencia import senales_de_regla

    preguntas = set(cfg.pregunta_keys())
    for nivel in ("red", "amber"):
        for regla in cfg.raw["thresholds"].get(nivel) or []:
            usadas = senales_de_regla(regla) & preguntas
            assert not usadas, f"la regla '{regla.get('name')}' mira {usadas}"


# ---------------------------------------------------------------------------
# El selector de sesión
# ---------------------------------------------------------------------------


def _con_eleccion(cfg, eleccion):
    return build_signals(
        cfg,
        LUNES,
        metrics=[],
        rides=[],
        sessions=[],
        checkin=Checkin(date=LUNES, values={CLAVE_SESION_ELEGIDA: eleccion}),
        checkin_history=[],
    )


@pytest.mark.parametrize("eleccion", ["dia_1", "dia_3", "bici", "otro"])
def test_lo_elegido_llega_al_motor_por_su_campo(cfg, eleccion):
    assert _con_eleccion(cfg, eleccion).sesion_elegida == eleccion


def test_sin_checkin_no_hay_eleccion_y_eso_no_es_un_fallo(cfg):
    """La mayoría de las mañanas del sistema: decide a las 07:00 sin formulario."""
    sig = build_signals(
        cfg, LUNES, metrics=[], rides=[], sessions=[], checkin_history=[]
    )
    assert sig.sesion_elegida is None


def test_lo_elegido_NO_entra_en_el_espacio_de_nombres_de_las_reglas(cfg):
    """Es la mitad que de verdad protege algo, y va al revés que las preguntas.

    Las dos preguntas de Sí/No SÍ entran en `values` -comparten bucle con los
    deslizadores a propósito, para llegar gratis al histórico y a las series- y
    lo que las frena es el validador del config. Con el selector no vale ese
    arreglo: `values` se serializa en el snapshot y lo recorre el análisis
    haciendo cuentas, así que una cadena ahí dentro no es un permiso de más, es
    un tipo equivocado esperando a que alguien saque una media.

    Por eso este camino no está cerrado con una prohibición sino con una
    ausencia: el bucle copia `slider_keys + pregunta_keys`, y el selector no está
    en ninguna de las dos.
    """
    sig = _con_eleccion(cfg, "dia_2")

    assert CLAVE_SESION_ELEGIDA not in sig.values
    assert sig.get(CLAVE_SESION_ELEGIDA) is None
    assert CLAVE_SESION_ELEGIDA not in sig.snapshot()["values"]
    assert sig.series(CLAVE_SESION_ELEGIDA) == {}

    # Y lo que sí está en `values` sigue siendo todo numérico o booleano, que es
    # la propiedad que esto defiende.
    for clave, valor in sig.values.items():
        assert not isinstance(valor, str) or valor in (UNKNOWN,), (
            f"'{clave}' mete una cadena en el espacio de nombres evaluable"
        )


def test_elegir_una_rutina_no_toca_nada_de_lo_que_decide_el_color(cfg):
    """El selector informa; no negocia el semáforo.

    Dos mañanas idénticas salvo por lo que se eligió tienen que dar las mismas
    señales evaluables. Si no, elegir «bici» sería una forma de pedir verde.
    """
    a = _con_eleccion(cfg, "dia_1")
    b = _con_eleccion(cfg, "bici")
    assert a.values == b.values
    assert a.adaptive == b.adaptive
