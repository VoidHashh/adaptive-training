"""Vista 5: la percepción de la mañana contra lo que de verdad salió.

Esta es la vista que existe para ser un dato objetivo frente a una distorsión
que va siempre en la misma dirección, así que aquí los números no pueden salir
"aproximadamente bien". Cada expectativa de este archivo está calculada a mano
en el propio test y escrita al lado de la comprobación.

Lo que se vigila con más insistencia:

  - LA ESCALA ES 0-10. Los deslizadores del formulario van de cero a diez, no de
    uno a diez, y tomarlos por 1-10 metería un sesgo de un 11% en todos los
    índices de percepción sin dar un solo error. El contador entero se apoya en
    esto;
  - el cumplimiento se mide contra lo PRESCRITO ESE DÍA. Una semana de descarga
    bien hecha es un 100, no un fracaso;
  - repetir carga es un 50, no un cero. En una espalda con hernia L4-L5,
    sostener es el resultado bueno de la mayoría de las sesiones;
  - el desnivel se calcula, se guarda, se enseña y NO se promedia. Si entrara en
    el índice, rodar por el llano sería rendir peor;
  - la disociación pide las CUATRO condiciones a la vez, y la dirección contraria
    se registra igual aunque no se destaque: un marcador que apunta los aciertos
    y no los fallos no es un marcador;
  - la tabla es append-only DE VERDAD. Reevaluar una sesión devuelve la fila que
    ya había, sin tocarla, aunque el histórico de detrás haya cambiado.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.analysis.rendimiento import (
    ALINEADO,
    BASE_MINIMA,
    BICI,
    ESPERA_MAXIMA,
    FUERZA,
    HIIT,
    PERCEPCION,
    PERCEPCION_MEJOR,
    PERCEPCION_PEOR,
    SIN_DATO,
    _previas,
    _toca_evaluar,
    coste_cardiaco,
    cruzar,
    _frase_bici,
    cumplimiento,
    esfuerzo,
    evaluar_pendientes,
    evaluar_sesion,
    indice_percepcion,
    marcar_reportadas,
    mensaje_disociacion,
    pendientes_de_avisar,
    progresion,
    rendimiento_bici,
    rendimiento_fuerza,
    vista_percepcion,
    volumen_efectivo,
)
from app.models import (
    Activity,
    Base,
    Checkin,
    Decision,
    SessionPerformance,
    WorkoutLog,
)
from tests.dobles import doble_de
from app.config_loader import Config

HOY = date(2026, 9, 11)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@doble_de(Config)
class Cfg:
    """Un `config.yaml` de mentira con lo justo que mira esta vista.

    `set_types` en `api` para que ningún test dependa de la heurística de
    calentamiento: aquí se prueba el rendimiento, no el troceado de series, y
    eso ya tiene sus propias pruebas en el motor.
    """

    def __init__(self, set_types=None):
        self.raw = {"set_types": set_types or {"source": "api"}}


CFG = Cfg()


def manana(**sliders):
    """Los seis deslizadores, con cinco -el centro exacto- por defecto."""
    base = dict.fromkeys(PERCEPCION, 5.0)
    base.update(sliders)
    return base


def plan(*ejercicios, rutina="dia1"):
    return {"routine": rutina, "exercises": list(ejercicios)}


def ejercicio(clave, series, *, template=None):
    return {
        "key": clave,
        "template_id": template or f"T_{clave}",
        "name": clave,
        "sets": series,
    }


def entreno(*hechos):
    return {"exercises": list(hechos)}


def hecho(clave, series, *, template=None):
    return {
        "exercise_template_id": template or f"T_{clave}",
        "title": clave,
        "sets": [{"type": "normal", **s} for s in series],
    }


def serie(reps=8, kg=60.0):
    return {"reps": reps, "weight_kg": kg}


# ---------------------------------------------------------------------------
# La percepción: la escala y el sentido de cada deslizador
# ---------------------------------------------------------------------------


def test_la_escala_de_los_deslizadores_va_de_cero_a_diez():
    """El sesgo invisible que se evita aquí.

    Los deslizadores del formulario van de 0 a 10 (`static/app.js`: min="0"
    max="10"; la API valida `ge=0, le=10`). Si se normalizara sobre 1-10, un
    valor de 1 daría un 0 normalizado en vez del 10 que le toca, y TODOS los
    índices de percepción saldrían desplazados alrededor de un 11% sin producir
    un solo error. El contador de disociaciones -que es una resta de
    percentiles- heredaría ese desplazamiento entero.

    Un 1 sobre 0-10 es un 10. Sobre 1-10 sería un 0. Esa es toda la prueba.
    """
    piezas = indice_percepcion(manana(mood=1.0))["piezas"]
    assert piezas["mood"]["normalizado"] == 10.0

    # Y el otro extremo: un 0 es un 0, no un valor fuera de rango.
    piezas = indice_percepcion(manana(mood=0.0))["piezas"]
    assert piezas["mood"]["normalizado"] == 0.0


def test_la_manana_perfecta_da_cien_la_peor_da_cero_y_el_centro_da_cincuenta():
    """Los tres puntos de anclaje, calculados a mano.

    Perfecta: fatiga 0, ánimo 10, molestias 0 y 0, sueño 10, ganas 10. Los seis
    normalizados a 100, media 100.
    Peor: exactamente al revés, los seis a 0, media 0.
    Centro: los seis a 5, que sobre 0-10 es el 50% justo.
    """
    perfecta = manana(
        fatigue=0.0,
        mood=10.0,
        upper_discomfort=0.0,
        lower_discomfort=0.0,
        sleep_quality=10.0,
        training_desire=10.0,
    )
    assert indice_percepcion(perfecta)["indice"] == 100.0

    peor = manana(
        fatigue=10.0,
        mood=0.0,
        upper_discomfort=10.0,
        lower_discomfort=10.0,
        sleep_quality=0.0,
        training_desire=0.0,
    )
    assert indice_percepcion(peor)["indice"] == 0.0

    assert indice_percepcion(manana())["indice"] == 50.0


def test_los_deslizadores_de_alto_peor_se_dan_la_vuelta():
    """Un 8 de dolor y un 8 de ánimo no pueden sumar lo mismo.

    El sentido lo manda `series.DEFINICIONES`, la misma tabla que usan las
    vistas 1 a 3. Si aquí se duplicara, un cambio en el sentido del ánimo en un
    sitio y no en el otro no daría ningún error: daría un índice con el signo
    cambiado, que es el peor fallo posible en la vista que existe para ser un
    contrapeso objetivo.
    """
    piezas = indice_percepcion(manana(fatigue=8.0, mood=8.0))["piezas"]

    assert piezas["fatigue"]["sentido"] == "alto_peor"
    assert piezas["fatigue"]["normalizado"] == 20.0  # 100 - 80

    assert piezas["mood"]["sentido"] == "alto_mejor"
    assert piezas["mood"]["normalizado"] == 80.0


def test_con_menos_de_cuatro_deslizadores_no_hay_indice_y_se_dice_cuales_faltan():
    """Media de tres piezas es la media de tres piezas, no la mañana.

    Y lo que devuelve no es un cero -que se compararía con los demás días como
    si fuera una mañana malísima- sino None con el motivo escrito y la lista de
    los que faltan.
    """
    r = indice_percepcion({"fatigue": 5.0, "mood": 5.0, "sleep_quality": 5.0})

    assert r["indice"] is None
    assert r["n_sliders"] == 3
    assert "3 de los 6" in r["na"]
    for ausente in ("upper_discomfort", "lower_discomfort", "training_desire"):
        assert ausente in r["na"]


def test_con_cuatro_de_seis_hay_indice_y_se_calcula_solo_con_los_cuatro():
    """El mínimo justo, y la media se hace con los contestados, no con seis.

    Ánimo 10 (100), sueño 10 (100), fatiga 0 (100) y molestia lumbar 10 (0).
    Media de esos cuatro: 300/4 = 75. Si el denominador fuera seis saldría 50.
    """
    r = indice_percepcion(
        {
            "mood": 10.0,
            "sleep_quality": 10.0,
            "fatigue": 0.0,
            "lower_discomfort": 10.0,
        }
    )

    assert r["n_sliders"] == 4
    assert r["indice"] == 75.0
    assert r["na"] is None


def test_el_rpe_de_ayer_no_entra_en_la_percepcion_de_hoy():
    """Correlacionar una cosa consigo misma daría un contador vacío.

    `yesterday_rpe` habla del entreno de ayer, no del estado de hoy, y además es
    el ingrediente del componente de esfuerzo del OTRO lado de la comparación.
    Meterlo en la percepción sería poner el mismo número a los dos lados de la
    resta.
    """
    assert "yesterday_rpe" not in PERCEPCION

    con = indice_percepcion({**manana(), "yesterday_rpe": 10.0})
    sin = indice_percepcion(manana())
    assert con["indice"] == sin["indice"]
    assert "yesterday_rpe" not in con["piezas"]


# ---------------------------------------------------------------------------
# El cumplimiento: contra lo prescrito, y en fracciones
# ---------------------------------------------------------------------------


def test_una_semana_de_descarga_bien_hecha_es_un_cumplimiento_del_cien():
    """Lo prescrito ESE DÍA, nunca un volumen absoluto.

    El plan de descarga pide dos series de cinco a 40 kg y se hacen las dos. Eso
    es un 100. Medirlo contra el volumen de una semana normal -cuatro series de
    ocho a 60- convertiría el descanso programado en un fracaso, que es justo al
    revés de lo que esta vista tiene que decir.
    """
    descarga = plan(ejercicio("press", [serie(5, 40.0), serie(5, 40.0)]))
    hecho_tal_cual = entreno(hecho("press", [serie(5, 40.0), serie(5, 40.0)]))

    assert cumplimiento(hecho_tal_cual, descarga, CFG)["valor"] == 100.0

    # Y el mismo entreno, exactamente el mismo, medido contra la semana normal
    # -cuatro series de ocho a 60- se queda en cero de cuatro. Esa es la prueba
    # de que el punto de referencia es el plan del día y no un volumen fijo: si
    # lo fuera, los dos números tendrían que salir iguales.
    normal = plan(ejercicio("press", [serie() for _ in range(4)]))
    assert cumplimiento(hecho_tal_cual, normal, CFG)["valor"] == 0.0


def test_el_cumplimiento_es_una_fraccion_y_no_un_si_o_no():
    """Seis de siete series y cero de siete no pueden valer lo mismo.

    El motor contesta sí/no por ejercicio, porque lo suyo es abrir o no la
    puerta de la carga. Para un índice eso no sirve. Aquí: cuatro series
    prescritas, tres alcanzadas -la cuarta se queda en cinco reps de las ocho
    pedidas-, o sea 75.
    """
    pedido = plan(ejercicio("press", [serie() for _ in range(4)]))
    hizo = entreno(hecho("press", [serie(), serie(), serie(), serie(5, 60.0)]))

    r = cumplimiento(hizo, pedido, CFG)
    assert r["prescritas"] == 4
    assert r["logradas"] == 3
    assert r["valor"] == 75.0


def test_un_ejercicio_que_no_aparece_se_distingue_de_uno_que_aparece_y_falla():
    """"No está" y "está y no se completó" no son lo mismo.

    Las dos cuentan como cero series logradas, y en el índice pesan igual -es lo
    prudente: si no está, o no se hizo o no se registró-. Pero en el detalle se
    separan, porque llevan a mirar sitios distintos: una manda a revisar el
    entreno, la otra a revisar si se sincronizó.
    """
    pedido = plan(
        ejercicio("press", [serie(), serie()]),
        ejercicio("remo", [serie(), serie()]),
    )
    hizo = entreno(hecho("press", [serie(2, 60.0), serie(2, 60.0)]))

    d = cumplimiento(hizo, pedido, CFG)["detalle"]
    assert d["press"] == {"prescritas": 2, "logradas": 0, "registrado": True}
    assert d["remo"] == {"prescritas": 2, "logradas": 0, "registrado": False}


def test_sin_una_sola_serie_prescrita_el_cumplimiento_es_na_y_no_un_cero():
    """Sin nada que cumplir no hay cumplimiento que medir.

    Un cero aquí diría "esa sesión salió fatal" cuando lo que pasó es que no
    había sesión guardada. Es la diferencia entre un dato malo y ningún dato, y
    el contador se apoya en no confundirlos.
    """
    r = cumplimiento(entreno(), plan(), CFG)

    assert r["valor"] is None
    assert "sin nada que cumplir" in r["na"]


def test_pasarse_de_lo_pedido_cumple():
    """Doce repeticiones cuando se pedían diez es una sesión limpia.

    El criterio es "no se quedó corto", y es el del motor: se reutiliza
    `_alcanza` en vez de escribir un segundo criterio que acabaría discrepando.
    """
    pedido = plan(ejercicio("press", [serie(10, 60.0)]))
    hizo = entreno(hecho("press", [serie(12, 62.5)]))

    assert cumplimiento(hizo, pedido, CFG)["valor"] == 100.0


# ---------------------------------------------------------------------------
# La progresión: anclada en +-10%, y con el centro en repetir carga
# ---------------------------------------------------------------------------


def test_repetir_carga_es_un_cincuenta_y_no_un_cero():
    """El centro de la escala es deliberado, no un apaño para que quede bonito.

    En una espalda con hernia L4-L5, sostener la carga es el resultado normal y
    bueno de la mayoría de las sesiones. Una escala donde "igual que la semana
    pasada" puntuara cero convertiría el plan entero en una acusación semanal, y
    esta vista es exactamente la que no puede hacer eso.
    """
    r = progresion({"press": 60.0, "remo": 50.0}, {"press": 60.0, "remo": 50.0})

    assert r["valor"] == 50.0
    assert r["n_ejercicios"] == 2


def test_un_diez_por_ciento_arriba_es_cien_y_un_diez_por_ciento_abajo_es_cero():
    """Los dos anclajes de la escala, a mano.

    110/100 es +10% -> 50 + 0.10*500 = 100.
    90/100 es -10% -> 50 - 0.10*500 = 0.
    105/100 es +5% -> 50 + 0.05*500 = 75.
    """
    assert progresion({"press": 110.0}, {"press": 100.0})["valor"] == 100.0
    assert progresion({"press": 90.0}, {"press": 100.0})["valor"] == 0.0
    assert progresion({"press": 105.0}, {"press": 100.0})["valor"] == 75.0


def test_la_progresion_se_recorta_en_los_extremos_y_no_se_dispara():
    """Un cambio de ficha -de mancuerna a barra- no puede valer un 400.

    Doblar el peso es un +100%, que sin recorte daría 550. La escala es 0-100 y
    se queda ahí; el número crudo sigue en el detalle para quien quiera mirarlo.
    """
    r = progresion({"press": 200.0}, {"press": 100.0})

    assert r["valor"] == 100.0
    assert r["detalle"]["press"]["variacion_pct"] == 100.0


def test_la_progresion_solo_compara_los_ejercicios_que_estan_en_las_dos():
    """Comparar ejercicios distintos daría ruido con forma de número."""
    r = progresion(
        {"press": 110.0, "curl": 20.0},
        {"press": 100.0, "remo": 70.0},
    )

    assert r["n_ejercicios"] == 1
    assert set(r["detalle"]) == {"press"}
    assert r["valor"] == 100.0


def test_sin_ninguna_sesion_anterior_comparable_la_progresion_es_na():
    r = progresion({"press": 60.0}, {})

    assert r["valor"] is None
    assert r["n_ejercicios"] == 0
    assert "no hay contra qué comparar" in r["na"]


def test_un_peso_anterior_de_cero_no_se_usa_como_divisor():
    """Un ejercicio a peso corporal apuntado como 0 kg rompería la división.

    Y antes de romperla daría un infinito, que recortado en 100 diría "progresó
    muchísimo" de un ejercicio que no lleva carga.
    """
    r = progresion({"plancha": 0.0}, {"plancha": 0.0})

    assert r["valor"] is None
    assert r["n_ejercicios"] == 0


# ---------------------------------------------------------------------------
# El esfuerzo: el RPE PUESTO EN RELACIÓN con lo que se movió
# ---------------------------------------------------------------------------


def test_sin_el_rpe_de_la_manana_siguiente_el_esfuerzo_es_na_con_el_motivo():
    """Y el motivo dice DÓNDE se pregunta, que es lo que explica el retraso.

    Es la razón de fondo de que el aviso salga en el mensaje del día siguiente y
    no en el del día: antes de esa respuesta la sesión no se puede terminar de
    evaluar. No es una preferencia de formato.
    """
    r = esfuerzo(None, 5000.0, [1000.0, 2000.0])

    assert r["valor"] is None
    assert "mañana siguiente" in r["na"]
    assert r["volumen_kg"] == 5000.0  # el volumen sí se guarda, aunque falte el RPE


def test_el_esfuerzo_es_la_distancia_entre_lo_que_costo_y_lo_que_se_movio():
    """Un RPE de 7 no dice nada por sí solo. A mano, los dos extremos.

    Sesión buena: volumen 5000 por encima de las cuatro anteriores -percentil
    100- con un RPE de 5 -coste 50 sobre la escala 0-10-. Resultado:
    50 + (100 - 50)/2 = 75.

    Sesión mala: volumen 500 por debajo de todas -percentil 0- con un RPE de 9
    -coste 90-. Resultado: 50 + (0 - 90)/2 = 5.
    """
    previos = [1000.0, 2000.0, 3000.0, 4000.0]

    buena = esfuerzo(5.0, 5000.0, previos)
    assert buena["carga_pct"] == 100.0
    assert buena["coste_pct"] == 50.0
    assert buena["valor"] == 75.0

    mala = esfuerzo(9.0, 500.0, previos)
    assert mala["carga_pct"] == 0.0
    assert mala["coste_pct"] == 90.0
    assert mala["valor"] == 5.0


def test_sin_volumenes_previos_el_esfuerzo_no_se_puede_poner_en_relacion_con_nada():
    """Hay RPE, pero no hay con qué compararlo. Eso es N/A, no un 50."""
    r = esfuerzo(7.0, 5000.0, [])

    assert r["valor"] is None
    assert r["rpe"] == 7.0
    assert "no hay sesiones anteriores" in r["na"]


def test_el_volumen_efectivo_usa_el_mismo_criterio_de_calentamiento_que_el_motor():
    """Dos series de 10 a 50 kg son 1000 kg; la marcada como calentamiento no suma."""
    crudo = {
        "exercises": [
            {
                "exercise_template_id": "T_press",
                "sets": [
                    {"type": "warmup", "reps": 10, "weight_kg": 20.0},
                    {"type": "normal", "reps": 10, "weight_kg": 50.0},
                    {"type": "normal", "reps": 10, "weight_kg": 50.0},
                ],
            }
        ]
    }

    assert volumen_efectivo(crudo, CFG) == 1000.0


# ---------------------------------------------------------------------------
# La mezcla de fuerza
# ---------------------------------------------------------------------------


def test_sin_cumplimiento_no_hay_indice_aunque_las_otras_piezas_esten():
    """El cumplimiento es el esqueleto, y el motivo que sale es el SUYO.

    El eslabón roto de más abajo es el que manda. Devolver aquí el motivo de la
    mezcla -"no se pudo calcular el índice"- mandaría a mirar la media cuando lo
    que falta es la sesión prescrita de ese día.
    """
    r = rendimiento_fuerza(
        entreno(hecho("press", [serie()])),
        plan(),
        CFG,
        rpe=7.0,
        pesos_antes={"press": 60.0},
        volumenes_previos=[1000.0],
    )

    assert r["indice"] is None
    assert "sin nada que cumplir" in r["na"]


def test_el_indice_dice_con_cuantas_piezas_se_hizo():
    """Dos índices de 70 con distinto número de piezas no son el mismo 70.

    Aquí falta el RPE -sesión recién terminada-, así que el índice sale de dos
    piezas: cumplimiento 100 y progresión 50 (misma carga) -> 75. Y
    `componentes_usados` lo dice, en vez de dejar que quien lo lea lo suponga.
    """
    r = rendimiento_fuerza(
        entreno(hecho("press", [serie(), serie()])),
        plan(ejercicio("press", [serie(), serie()])),
        CFG,
        rpe=None,
        pesos_antes={"press": 60.0},
        volumenes_previos=[500.0],
    )

    assert r["componentes_usados"] == ["cumplimiento", "progresion"]
    assert r["componentes"]["cumplimiento"]["valor"] == 100.0
    assert r["componentes"]["progresion"]["valor"] == 50.0
    assert r["indice"] == 75.0
    assert r["componentes"]["esfuerzo"]["valor"] is None


def test_los_pesos_de_la_sesion_viajan_en_la_respuesta_para_la_siguiente():
    """Sin esto, la progresión de la sesión de la semana que viene no tendría base.

    Y tenerlos guardados es lo que evita volver a abrir el crudo de Hevy y
    reconstruir el plan de aquel día seis meses después.
    """
    r = rendimiento_fuerza(
        entreno(hecho("press", [serie(8, 60.0), serie(8, 62.5)])),
        plan(ejercicio("press", [serie(8, 60.0), serie(8, 60.0)])),
        CFG,
        rpe=None,
        pesos_antes={},
        volumenes_previos=[],
    )

    # El máximo, no la media ni la última: la serie top es la que define la carga.
    assert r["pesos"] == {"press": 62.5}


# ---------------------------------------------------------------------------
# La bici, que no tiene potenciómetro
# ---------------------------------------------------------------------------


def actividad(
    *,
    id_garmin=1,
    dia=None,
    metros=20000.0,
    segundos=3600.0,
    desnivel=200.0,
    zonas=(0.0, 0.0, 0.0, 0.0, 0.0),
    fc_media=None,
):
    return Activity(
        garmin_activity_id=id_garmin,
        date=dia or HOY,
        is_cycling=True,
        distance_m=metros,
        duration_s=segundos,
        moving_duration_s=segundos,
        elevation_gain_m=desnivel,
        hr_zone_1_s=zonas[0],
        hr_zone_2_s=zonas[1],
        hr_zone_3_s=zonas[2],
        hr_zone_4_s=zonas[3],
        hr_zone_5_s=zonas[4],
        avg_hr=fc_media,
    )


def test_el_coste_cardiaco_pondera_las_zonas_y_lo_dice():
    """Mitad del tiempo en zona 1 y mitad en zona 2: coste 1,5.

    Las zonas se las calcula Garmin con su máxima y su umbral, así que ya vienen
    normalizadas a él. Esto es literalmente lo que significa "frecuencia
    cardiaca relativa a las zonas".
    """
    r = coste_cardiaco(actividad(zonas=(600.0, 600.0, 0.0, 0.0, 0.0)))

    assert r["valor"] == 1.5  # (600*1 + 600*2) / 1200
    assert r["fuente"] == "zonas"
    assert r["reparto"] == [50.0, 50.0, 0.0, 0.0, 0.0]


def test_sin_zonas_se_cae_a_la_media_de_pulsaciones_y_se_dice_que_se_ha_caido():
    """Un número que unos días vale 1,5 y otros 140 y no avisa es peor que nada.

    `fuente` es lo que impide que ese 140 se compare con ese 1,5 dentro del
    mismo histórico sin que salte nadie.
    """
    r = coste_cardiaco(actividad(fc_media=140))

    assert r["valor"] == 140.0
    assert r["fuente"] == "fc_media"
    assert r["na"] is None


def test_sin_zonas_ni_pulsaciones_el_coste_cardiaco_es_na_con_el_motivo():
    r = coste_cardiaco(actividad())

    assert r["valor"] is None
    assert r["fuente"] is None
    assert "ni tiempo en zonas ni pulsaciones medias" in r["na"]


def test_la_velocidad_se_corrige_con_el_desnivel_ahi_es_donde_el_desnivel_cuenta():
    """20 km en una hora son 20 km/h; 200 m de desnivel en 20 km son 10 m/km.

    Corrección: 20 * (1 + 10/100) = 22,0. Tosca y a la vista, que es lo honesto
    sin un potenciómetro. Lo que no se hace es fingir precisión.
    """
    act = actividad(zonas=(1800.0, 1800.0, 0.0, 0.0, 0.0))
    historico = [
        {"velocidad_ajustada": 18.0, "eficiencia": 10.0, "desnivel_m_km": 5.0},
        {"velocidad_ajustada": 19.0, "eficiencia": 11.0, "desnivel_m_km": 6.0},
    ]

    r = rendimiento_bici(act, historico)
    assert r["metricas"]["velocidad_kmh"] == 20.0
    assert r["metricas"]["desnivel_m_km"] == 10.0
    assert r["metricas"]["velocidad_ajustada"] == 22.0


def test_el_desnivel_no_entra_en_el_indice_de_la_bici():
    """Si entrara, rodar por el llano sería rendir peor, y eso es falso.

    El desnivel mide lo duro que era el terreno, no lo bien que se rodó. Tres
    salidas llanas seguidas bajarían el índice sin que hubiera pasado nada.

    El caso está montado a propósito para que las dos lecturas den números
    DISTINTOS, porque si no el test no probaría nada:

      velocidad ajustada 22,0, por encima de las dos anteriores -> percentil 100;
      eficiencia 22/1,5 = 14,67, por encima de 10 y 11 -> percentil 100;
      desnivel 10 m/km, POR DEBAJO de los dos anteriores (20 y 30) -> percentil 0.

    Índice correcto: (100 + 100)/2 = 100. Si el desnivel se promediara saldría
    (100 + 100 + 0)/3 = 66,67, o sea: la misma salida, rodada más rápido que
    nunca, puntuaría un tercio menos por haber ido por terreno más llano.
    """
    act = actividad(zonas=(1800.0, 1800.0, 0.0, 0.0, 0.0))
    historico = [
        {"velocidad_ajustada": 18.0, "eficiencia": 10.0, "desnivel_m_km": 20.0},
        {"velocidad_ajustada": 19.0, "eficiencia": 11.0, "desnivel_m_km": 30.0},
    ]

    r = rendimiento_bici(act, historico)

    assert r["componentes_usados"] == ["velocidad", "corazon"]
    assert "desnivel" not in r["componentes_usados"]

    desnivel = r["componentes"]["desnivel"]
    assert desnivel["entra_en_el_indice"] is False
    assert desnivel["valor"] == 0.0  # se calcula y se enseña...
    assert desnivel["nota"]  # ...y lleva escrito por qué no se promedia

    assert r["indice"] == 100.0  # la media de los DOS
    assert r["indice"] != round((100.0 + 100.0 + 0.0) / 3, 2)  # no la de los tres


# ---------------------------------------------------------------------------
# Una salida sin desnivel: "llano" y "no lo sé" no son el mismo dato
# ---------------------------------------------------------------------------
#
# Pasa de verdad y no es raro: un rodillo de interior no da desnivel, y un
# dispositivo sin altímetro tampoco. Antes se leía con `or 0.0` y el hueco se
# convertía en cero metros, que es una afirmación: la velocidad ajustada se
# quedaba sin corregir pero seguía llamándose ajustada, la salida entraba en el
# histórico como la más llana de todas, y el mensaje de Telegram decía "con
# 0 m/km de desnivel" a alguien que acababa de subir un puerto.


def test_una_salida_sin_desnivel_no_se_lo_inventa_y_dice_por_que():
    """Sin desnivel no hay velocidad ajustada, y la cruda no sirve de sustituta.

    El histórico contra el que se compara está lleno de velocidades corregidas
    por el terreno: meter ahí una sin corregir es comparar dos cosas distintas
    y no decirlo. Es el mismo motivo por el que el coste cardiaco lleva
    `fuente`.
    """
    r = rendimiento_bici(actividad(desnivel=None), [])

    m = r["metricas"]
    assert m["desnivel_m_km"] is None
    assert m["velocidad_ajustada"] is None
    # Lo que SÍ se sabe se sigue diciendo: 20 km en una hora son 20 km/h,
    # y eso es cierto con altímetro y sin él.
    assert m["km"] == 20.0
    assert m["velocidad_kmh"] == 20.0
    assert "no trae desnivel acumulado" in m["na_desnivel"]


def test_sin_desnivel_la_salida_se_queda_sin_nota_y_con_el_motivo_escrito():
    """Una salida de rodillo no puntúa, y eso es lo correcto.

    Los tres componentes dependen del desnivel más de lo que parece: la
    velocidad porque se corrige con él, el desnivel porque es él, y el corazón
    porque la eficiencia es velocidad AJUSTADA por unidad de coste cardiaco.
    Sin altímetro se quedan los tres sin percentil.

    Escrito así a propósito, y no cayendo a la velocidad cruda: el histórico de
    eficiencias está hecho de ajustadas, y un percentil que unos días compara
    una cosa y otros otra es peor que no tener percentil. La salida se queda sin
    nota, con el motivo a la vista, y sus números crudos se siguen diciendo.
    """
    act = actividad(desnivel=None, zonas=(1800.0, 1800.0, 0.0, 0.0, 0.0))
    historico = [
        {"velocidad_ajustada": 18.0, "eficiencia": 10.0, "desnivel_m_km": 20.0},
        {"velocidad_ajustada": 19.0, "eficiencia": 11.0, "desnivel_m_km": 30.0},
    ]

    r = rendimiento_bici(act, historico)

    assert r["indice"] is None
    assert r["componentes_usados"] == []

    # Cada componente dice que le falta el desnivel, en vez de traer un
    # percentil calculado sobre un cero inventado.
    for nombre in ("velocidad", "corazon", "desnivel"):
        comp = r["componentes"][nombre]
        assert comp["valor"] is None, nombre
        assert "no trae desnivel acumulado" in comp["na"], nombre

    # Pero el coste cardiaco crudo sigue ahí: se midió y es cierto. Lo que no
    # se puede es situarlo dentro del histórico.
    assert r["componentes"]["corazon"]["coste_cardiaco"] == 1.5
    assert r["componentes"]["corazon"]["fuente_fc"] == "zonas"


def test_sin_distancia_o_duracion_no_hay_velocidad_que_calcular():
    r = rendimiento_bici(actividad(metros=0.0), [])

    assert r["indice"] is None
    assert "no trae distancia o duración" in r["na"]


def test_la_primera_salida_no_tiene_percentil_y_lo_dice():
    """El percentil de una muestra de una sola salida sería siempre el mismo número."""
    r = rendimiento_bici(actividad(zonas=(1800.0, 1800.0, 0.0, 0.0, 0.0)), [])

    assert r["indice"] is None
    assert r["componentes_usados"] == []
    assert "no hay salidas anteriores" in r["na"]
    # Pero las métricas crudas sí salen: son las que alimentarán el histórico.
    assert r["metricas"]["velocidad_ajustada"] == 22.0


# ---------------------------------------------------------------------------
# El cruce y la disociación
# ---------------------------------------------------------------------------


def test_la_disociacion_pide_las_cuatro_condiciones_a_la_vez():
    """Cada condición tapa un agujero distinto, así que se prueban una a una.

    Base holgada (20 sesiones) en todos los casos para aislar las otras tres.
    """
    # Las cuatro: mañana malísima (10), sesión normal o mejor (60), hueco 50.
    r = cruzar(10.0, 60.0, 20)
    assert (r["gap"], r["direccion"], r["disociacion"]) == (50.0, PERCEPCION_PEOR, True)

    # Hueco de sobra, pero la mañana solo fue mediocre (30 > 25). No cuenta: si
    # contara, cualquier día regular con una sesión buena dispararía el mensaje.
    r = cruzar(30.0, 80.0, 20)
    assert r["direccion"] == PERCEPCION_PEOR
    assert r["disociacion"] is False

    # Mañana malísima y sesión buena, pero el hueco es de 35: por debajo de los
    # 40 no es una contradicción, es ruido de medida.
    r = cruzar(10.0, 45.0, 20)
    assert (r["direccion"], r["disociacion"]) == (ALINEADO, False)

    # "Me sentí fatal y entrené fatal" NO es una distorsión: es una percepción
    # acertada, y contarla como acierto del cuerpo sería la clase de mentira
    # amable que aquí no se quiere.
    r = cruzar(5.0, 20.0, 20)
    assert (r["direccion"], r["disociacion"]) == (ALINEADO, False)


def test_la_direccion_contraria_se_registra_igual_aunque_no_se_destaque():
    """Un marcador que apunta los aciertos y no los fallos no es un marcador.

    El contador que se mira por las mañanas solo cuenta `perception_worse`, pero
    registrar solo esa dirección daría un número en el que no se puede creer.
    Que la otra esté ahí, guardada y sin destacar, es lo que lo hace honesto.
    """
    r = cruzar(60.0, 10.0, 20)

    assert r["gap"] == -50.0
    assert r["direccion"] == PERCEPCION_MEJOR
    assert r["disociacion"] is True


def test_con_pocas_sesiones_detras_hay_direccion_pero_no_disociacion():
    """La tabla es append-only, así que un falso positivo no se puede borrar.

    Un percentil sobre catorce sesiones es una ordenación de catorce cosas. La
    dirección sí se apunta -es una resta, no necesita muestra-, pero la marca
    que alimenta el contador no.
    """
    r = cruzar(10.0, 60.0, BASE_MINIMA - 1)

    assert r["direccion"] == PERCEPCION_PEOR
    assert r["disociacion"] is False
    assert f"solo hay {BASE_MINIMA - 1} sesiones anteriores" in r["na"]
    assert str(BASE_MINIMA) in r["na"]


def test_sin_uno_de_los_dos_percentiles_no_hay_cruce_y_se_dice_cual_falta():
    """Y se nombra el que falta, no "faltan datos"."""
    assert "la percepción" in cruzar(None, 60.0, 20)["na"]
    assert "el rendimiento" in cruzar(10.0, None, 20)["na"]

    de_los_dos = cruzar(None, None, 20)
    assert "la percepción ni el rendimiento" in de_los_dos["na"]
    assert de_los_dos["direccion"] == SIN_DATO
    assert de_los_dos["disociacion"] is False
    assert de_los_dos["gap"] is None


# ---------------------------------------------------------------------------
# La persistencia: append-only de verdad
# ---------------------------------------------------------------------------


def checkin(db, dia, *, rpe=None, **sliders):
    db.add(Checkin(date=dia, yesterday_rpe=rpe, **manana(**sliders)))
    db.commit()


def decision(db, dia, sesion, *, rutina="dia1", hiit=None):
    """`hiit` es la sesión ANIDADA del bloque, tal como la guarda el motor.

    Va dentro de `planned_session_json` y no en una columna aparte porque así
    es como está en producción: la decisión de un día guarda dos sesiones, la
    de fuerza en la raíz y el bloque colgando de `hiit`.
    """
    plan_json = {"routine": rutina, "exercises": sesion}
    if hiit is not None:
        plan_json["hiit"] = hiit
        plan_json["hiit_block"] = hiit["routine"]
    db.add(
        Decision(
            date=dia,
            light="green",
            fired_rules_json="[]",
            skipped_rules_json="[]",
            inputs_snapshot_json="{}",
            config_hash="h",
            source="checkin",
            is_current=True,
            planned_session_json=json.dumps(plan_json),
        )
    )
    db.commit()


def fila_previa(db, dia, *, kind=FUERZA, per=50.0, rend=50.0, clave=None):
    db.add(
        SessionPerformance(
            date=dia,
            kind=kind,
            source_key=clave or f"previa:{kind}:{dia}",
            perception_index=per,
            performance_index=rend,
            direction=ALINEADO,
            dissociation=False,
        )
    )
    db.commit()


def test_una_sesion_ya_evaluada_se_devuelve_sin_tocarla(db):
    """Append-only en el sentido fuerte: la fila guarda el juicio de SU día.

    Los percentiles no son verdades absolutas, son la posición dentro de una
    distribución que sigue creciendo. Aquí se evalúa una sesión, luego se mete
    una fila más en el histórico -que cambiaría el percentil si se recalculara-
    y se vuelve a evaluar la misma sesión: tiene que devolver lo mismo, y no
    puede haber una segunda fila.
    """
    dia = HOY - timedelta(days=2)
    checkin(db, dia)
    decision(db, dia, [ejercicio("press", [serie(), serie()])])

    primera = evaluar_sesion(
        db,
        CFG,
        dia=dia,
        kind=FUERZA,
        source_key="hevy:W1",
        entreno=entreno(hecho("press", [serie(), serie()])),
    )
    db.commit()
    antes = (primera.performance_index, primera.n_sessions_base, primera.gap_pct)

    fila_previa(db, dia - timedelta(days=1), rend=5.0)

    segunda = evaluar_sesion(
        db, CFG, dia=dia, kind=FUERZA, source_key="hevy:W1", entreno=entreno()
    )

    assert segunda.id == primera.id
    assert (segunda.performance_index, segunda.n_sessions_base, segunda.gap_pct) == antes
    assert len(list(db.scalars(select(SessionPerformance).where(
        SessionPerformance.source_key == "hevy:W1"
    )))) == 1


def test_la_percepcion_que_se_guarda_es_la_del_checkin_de_ese_dia(db):
    """La de antes de entrenar, que es la que hay que contrastar.

    Se guardan los seis deslizadores crudos además del índice: dentro de seis
    meses se tiene que poder rehacer la cuenta sin depender de que este archivo
    siga existiendo tal cual.
    """
    dia = HOY - timedelta(days=2)
    checkin(db, dia, fatigue=8.0, mood=2.0)
    decision(db, dia, [ejercicio("press", [serie()])])

    fila = evaluar_sesion(
        db,
        CFG,
        dia=dia,
        kind=FUERZA,
        source_key="hevy:W1",
        entreno=entreno(hecho("press", [serie()])),
    )

    assert fila.perceived_fatigue == 8.0
    assert fila.perceived_mood == 2.0
    # Fatiga 8 -> 20, ánimo 2 -> 20, los otros cuatro a 5 -> 50.
    # (20 + 20 + 50*4) / 6 = 40.
    assert fila.perception_index == 40.0


def test_sin_checkin_esa_manana_el_motivo_es_ese_y_no_el_del_cruce(db):
    """El eslabón roto de MÁS ABAJO es el que manda.

    Si no hay percepción, escribir "no se pudo situar la percepción en su
    histórico" -que también es cierto- mandaría a mirar el histórico cuando lo
    que falta es el formulario de esa mañana. Es el mismo fallo que ya apareció
    en las vistas 3 y 4, y por eso se prueba aquí explícitamente.
    """
    dia = HOY - timedelta(days=2)
    decision(db, dia, [ejercicio("press", [serie()])])

    fila = evaluar_sesion(
        db,
        CFG,
        dia=dia,
        kind=FUERZA,
        source_key="hevy:W1",
        entreno=entreno(hecho("press", [serie()])),
    )

    assert fila.perception_index is None
    assert "deslizadores" in fila.na_reason
    assert "histórico" not in fila.na_reason


def test_sin_sesion_prescrita_no_se_juzga_lo_que_se_hizo(db):
    """Sin saber lo que tocaba hacer, lo que se hizo no es ni mucho ni poco."""
    dia = HOY - timedelta(days=2)
    checkin(db, dia)

    fila = evaluar_sesion(
        db,
        CFG,
        dia=dia,
        kind=FUERZA,
        source_key="hevy:W1",
        entreno=entreno(hecho("press", [serie()])),
    )

    assert fila.performance_index is None
    assert "no hay sesión prescrita" in fila.na_reason


def test_en_la_muestra_solo_entran_las_sesiones_con_LOS_DOS_indices(db):
    """Situar la percepción entre treinta sesiones y el rendimiento entre doce
    sería restar dos números medidos con reglas distintas, y el hueco que sale
    de esa resta es justo el número del contador.
    """
    fila_previa(db, HOY - timedelta(days=5), clave="a")
    fila_previa(db, HOY - timedelta(days=4), rend=None, clave="b")
    fila_previa(db, HOY - timedelta(days=3), per=None, clave="c")

    assert [p.source_key for p in _previas(db, FUERZA, HOY)] == ["a"]


def test_una_sesion_no_entra_en_su_propia_muestra(db):
    """Una muestra de una sola sesión da siempre el percentil 50.

    Si la sesión que se evalúa entrara en su propio histórico, las primeras
    filas saldrían todas "normales" por construcción, que es exactamente la
    conclusión que esta vista existe para no dar por descontada.
    """
    dia = HOY - timedelta(days=2)
    fila_previa(db, dia, clave="la_de_hoy")
    fila_previa(db, dia - timedelta(days=1), clave="la_de_ayer")

    claves = [p.source_key for p in _previas(db, FUERZA, dia)]
    assert claves == ["la_de_ayer"]


def test_las_muestras_de_fuerza_y_de_bici_no_se_mezclan(db):
    """Un índice de fuerza y uno de bici no se construyen con las mismas piezas."""
    fila_previa(db, HOY - timedelta(days=3), kind=FUERZA, clave="f")
    fila_previa(db, HOY - timedelta(days=2), kind=BICI, clave="b")

    assert [p.source_key for p in _previas(db, FUERZA, HOY)] == ["f"]
    assert [p.source_key for p in _previas(db, BICI, HOY)] == ["b"]


# ---------------------------------------------------------------------------
# El barrido: cuándo se puede evaluar una sesión
# ---------------------------------------------------------------------------


def test_una_sesion_no_se_evalua_el_mismo_dia(db):
    """No es formato: el esfuerzo percibido se pregunta a la mañana siguiente.

    Evaluarla por la noche daría una fila sin ese componente y, como las filas
    no se reescriben, ese hueco sería para siempre.
    """
    assert _toca_evaluar(db, HOY, HOY) is False


def test_en_cuanto_llega_el_checkin_de_la_manana_siguiente_toca_evaluar(db):
    dia = HOY - timedelta(days=1)
    assert _toca_evaluar(db, dia, HOY) is False

    checkin(db, HOY, rpe=7.0)
    assert _toca_evaluar(db, dia, HOY) is True


def test_si_el_checkin_no_llega_nunca_se_evalua_igual_pasados_unos_dias(db):
    """Sin esta salida, el denominador dejaría de ser el total de sesiones.

    Y ese denominador es justo lo que hace creíble al contador: "tantas de
    tantas". Una sesión sin check-in detrás se quedaría fuera de la tabla para
    siempre y el número de arriba se calcularía sobre una selección.
    """
    assert _toca_evaluar(db, HOY - timedelta(days=ESPERA_MAXIMA - 1), HOY) is False
    assert _toca_evaluar(db, HOY - timedelta(days=ESPERA_MAXIMA), HOY) is True


def test_evaluar_pendientes_escribe_fuerza_y_bici_y_es_idempotente(db):
    """Dos pasadas seguidas no pueden dar dos filas de la misma sesión.

    El barrido corre cada mañana y puede correr dos veces el mismo día -un
    reinicio, un arranque manual-. Que la segunda pasada no escriba nada es lo
    que hace que el contador no se infle solo.
    """
    dia = HOY - timedelta(days=2)
    checkin(db, dia)
    checkin(db, dia + timedelta(days=1), rpe=7.0)
    decision(db, dia, [ejercicio("press", [serie(), serie()])])

    db.add(
        WorkoutLog(
            hevy_workout_id="W1",
            date=dia,
            routine_key="dia1",
            raw_json=json.dumps(entreno(hecho("press", [serie(), serie()]))),
        )
    )
    db.add(actividad(id_garmin=77, dia=dia, zonas=(1800.0, 1800.0, 0.0, 0.0, 0.0)))
    db.commit()

    primera = evaluar_pendientes(db, CFG, hasta=HOY, dias=30)
    db.commit()
    assert {f.source_key for f in primera} == {"hevy:W1", "garmin:77"}
    assert {f.kind for f in primera} == {FUERZA, BICI}

    segunda = evaluar_pendientes(db, CFG, hasta=HOY, dias=30)
    db.commit()
    assert {f.id for f in segunda} == {f.id for f in primera}
    assert db.scalar(select(SessionPerformance).where(
        SessionPerformance.source_key == "hevy:W1"
    )) is not None
    assert len(list(db.scalars(select(SessionPerformance)))) == 2


def test_el_hiit_es_un_tipo_propio_y_se_juzga_contra_su_propio_plan(db):
    """Un bloque de wall balls no es una sesión de fuerza corta.

    Todas las filas de `workout_log` entraban al barrido como `strength`, así
    que el entrenamiento del bloque HIIT -que en Hevy es un entrenamiento
    suelto- se comparaba con la lista de la prensa y el remo. Ninguno de sus
    ejercicios aparecía ahí, el cumplimiento salía a cero y la fila decía que
    la sesión había sido malísima cuando se había hecho entera.

    Y encima entraba en la distribución con la que se sitúan las sesiones de
    fuerza de verdad: otro volumen, otros pesos, otra duración. El percentil
    dejaba de significar "comparado con tus sesiones de fuerza".
    """
    cfg = Cfg()
    cfg.raw["hiit"] = {"blocks": {"dia1": "hiit_dia1"}}

    dia = HOY - timedelta(days=2)
    checkin(db, dia)
    checkin(db, dia + timedelta(days=1), rpe=7.0)
    decision(
        db,
        dia,
        [ejercicio("press", [serie(), serie()])],
        hiit={
            "routine": "hiit_dia1",
            "exercises": [ejercicio("wall_ball", [serie(20, 5.0)])],
        },
    )
    # Tres sesiones de fuerza detrás. Son las que el HIIT NO puede usar de
    # muestra: es la forma de ver que las dos distribuciones están separadas sin
    # tener que leer los percentiles.
    for i in (7, 14, 21):
        fila_previa(db, dia - timedelta(days=i))

    db.add(WorkoutLog(hevy_workout_id="W1", date=dia, routine_key="dia1",
                      raw_json=json.dumps(entreno(hecho("press", [serie(), serie()])))))
    db.add(WorkoutLog(hevy_workout_id="W2", date=dia, routine_key="hiit_dia1",
                      raw_json=json.dumps(entreno(hecho("wall_ball", [serie(20, 5.0)])))))
    db.commit()

    filas = {f.source_key: f for f in evaluar_pendientes(db, cfg, hasta=HOY, dias=30)}
    db.commit()

    assert filas["hevy:W1"].kind == FUERZA
    assert filas["hevy:W2"].kind == HIIT, (
        f"el bloque sigue entrando como fuerza: {filas['hevy:W2'].kind}"
    )
    assert filas["hevy:W2"].routine_key == "hiit_dia1", (
        "se ha juzgado contra el plan de fuerza: la rutina que ha quedado "
        f"apuntada es {filas['hevy:W2'].routine_key!r}"
    )
    assert filas["hevy:W2"].n_sessions_base == 0, (
        "el HIIT se está situando dentro de la muestra de las sesiones de "
        f"fuerza: {filas['hevy:W2'].n_sessions_base} previas"
    )
    assert filas["hevy:W1"].n_sessions_base == 3

    # Y lo que se hizo, se hizo: el bloque salió entero.
    comp = json.loads(filas["hevy:W2"].components_json or "{}")
    assert comp["rendimiento"]["componentes"]["cumplimiento"]["valor"] == 100.0, comp


def test_un_bloque_que_ya_no_esta_en_el_config_se_juzga_por_lo_que_era_su_dia(db):
    """El «Día 2 HIIT» del 22/09/2026, evaluado después de retirarse el bloque.

    Ese día la decisión guardó `hiit_dia_2` como bloque, y el 25 el bloque salió
    de `hiit.blocks` al fusionarse en el Día 2. El entreno seguía sin evaluar
    -el 23 y el 24 el sistema no corrió- y con solo el config de hoy entraba
    como FUERZA: juzgado contra la prensa y el remo, cumplimiento a cero, en
    una fila que no se reescribe.
    """
    cfg = Cfg()
    cfg.raw["hiit"] = {"blocks": {}}  # el bloque ya no existe en el config

    dia = HOY - timedelta(days=2)
    checkin(db, dia)
    checkin(db, dia + timedelta(days=1), rpe=7.0)
    decision(
        db,
        dia,
        [ejercicio("press", [serie(), serie()])],
        hiit={
            "routine": "hiit_dia2",
            "exercises": [ejercicio("wall_ball", [serie(20, 5.0)])],
        },
    )
    db.add(WorkoutLog(hevy_workout_id="W1", date=dia, routine_key="dia1",
                      raw_json=json.dumps(entreno(hecho("press", [serie(), serie()])))))
    db.add(WorkoutLog(hevy_workout_id="W2", date=dia, routine_key="hiit_dia2",
                      raw_json=json.dumps(entreno(hecho("wall_ball", [serie(20, 5.0)])))))
    db.commit()

    filas = {f.source_key: f for f in evaluar_pendientes(db, cfg, hasta=HOY, dias=30)}

    assert filas["hevy:W1"].kind == FUERZA
    assert filas["hevy:W2"].kind == HIIT, (
        "el bloque que ese día tocaba se ha evaluado como fuerza porque hoy ya "
        "no está en hiit.blocks"
    )
    comp = json.loads(filas["hevy:W2"].components_json or "{}")
    assert comp["rendimiento"]["componentes"]["cumplimiento"]["valor"] == 100.0, comp


def test_un_entreno_sin_rutina_no_pasa_por_hiit_un_dia_sin_bloque(db):
    """La otra cara: sin rutina de origen no hay nada que comparar con el
    bloque del día, y un día sin bloque tampoco. Una comparación de vacío con
    vacío lo convertiría en HIIT."""
    cfg = Cfg()
    cfg.raw["hiit"] = {"blocks": {}}
    dia = HOY - timedelta(days=2)
    checkin(db, dia)
    checkin(db, dia + timedelta(days=1), rpe=7.0)
    decision(db, dia, [ejercicio("press", [serie(), serie()])])
    db.add(WorkoutLog(hevy_workout_id="W1", date=dia, routine_key=None,
                      raw_json=json.dumps(entreno(hecho("press", [serie(), serie()])))))
    db.commit()

    (fila,) = evaluar_pendientes(db, cfg, hasta=HOY, dias=30)
    assert fila.kind == FUERZA


def test_la_sesion_de_hoy_se_queda_fuera_del_barrido(db):
    """Todavía no ha llegado la mañana en la que se pregunta el esfuerzo."""
    db.add(WorkoutLog(hevy_workout_id="W_hoy", date=HOY, raw_json=json.dumps(entreno())))
    db.commit()

    assert evaluar_pendientes(db, CFG, hasta=HOY, dias=30) == []


def test_el_rpe_que_se_usa_es_el_del_checkin_del_dia_siguiente(db):
    """Es la razón entera de que el aviso salga a la mañana siguiente.

    A mano: cumplimiento 100, progresión N/A (no hay sesión anterior de esta
    rutina), esfuerzo con RPE 5 -coste 50- y volumen por encima del único
    anterior -percentil 100-: 50 + (100 - 50)/2 = 75. Índice de dos piezas:
    (100 + 75)/2 = 87,5.
    """
    dia = HOY - timedelta(days=2)
    checkin(db, dia)
    checkin(db, dia + timedelta(days=1), rpe=5.0)
    decision(db, dia, [ejercicio("press", [serie(8, 60.0)])])

    # Una sesión anterior con volumen apuntado, para que el esfuerzo tenga con
    # qué compararse.
    db.add(
        SessionPerformance(
            date=dia - timedelta(days=7),
            kind=FUERZA,
            source_key="previa",
            routine_key="dia1",
            perception_index=50.0,
            performance_index=50.0,
            direction=ALINEADO,
            dissociation=False,
            components_json=json.dumps({"rendimiento": {"volumen_kg": 100.0}}),
        )
    )
    db.commit()

    fila = evaluar_sesion(
        db,
        CFG,
        dia=dia,
        kind=FUERZA,
        source_key="hevy:W1",
        entreno=entreno(hecho("press", [serie(8, 60.0)])),
    )

    assert fila.comp_compliance == 100.0
    assert fila.comp_rpe == 75.0
    assert fila.performance_index == 87.5


# ---------------------------------------------------------------------------
# El contador, que es para lo que existe todo lo anterior
# ---------------------------------------------------------------------------


def juicio(
    db,
    dia,
    *,
    kind=FUERZA,
    clave=None,
    per=50.0,
    per_pct=50.0,
    rend=50.0,
    rend_pct=50.0,
    gap=0.0,
    direccion=ALINEADO,
    disociacion=False,
    base=30,
    motivo=None,
    componentes=None,
    rutina="dia1",
    reportada=None,
    **piezas,
):
    """Una fila ya escrita. Los tests del contador NO pasan por `evaluar_sesion`.

    Es a propósito: el contador se prueba contra filas puestas a mano, con los
    números que hagan falta, para que un cambio en cómo se calcula un índice no
    rompa a la vez las pruebas del cálculo y las del recuento. Y porque así se
    puede escribir una fila incoherente -un gap que no cuadra con la resta- y
    comprobar que la vista la enseña tal cual en vez de rehacerla.
    """
    f = SessionPerformance(
        date=dia,
        kind=kind,
        source_key=clave or f"{kind}:{dia}",
        routine_key=rutina,
        perception_index=per,
        perception_pct=per_pct,
        performance_index=rend,
        performance_pct=rend_pct,
        gap_pct=gap,
        direction=direccion,
        dissociation=disociacion,
        n_sessions_base=base,
        na_reason=motivo,
        components_json=json.dumps(componentes, ensure_ascii=False)
        if componentes
        else None,
        reported_at=reportada,
        # `comp_compliance=...`, `comp_bike_elevation=...` y demás columnas de
        # pieza, para los tests que prueban las medias.
        **piezas,
    )
    db.add(f)
    db.commit()
    return f


def disociada(db, dia, **kw):
    """Una mañana malísima con una sesión que salió bien."""
    kw.setdefault("per", 10.0)
    kw.setdefault("per_pct", 8.0)
    kw.setdefault("rend", 71.0)
    kw.setdefault("rend_pct", 68.0)
    return juicio(
        db, dia, gap=60.0, direccion=PERCEPCION_PEOR, disociacion=True, **kw
    )


def contraria(db, dia, **kw):
    """La otra dirección: la mañana prometía más de lo que salió."""
    kw.setdefault("per", 80.0)
    kw.setdefault("per_pct", 90.0)
    kw.setdefault("rend", 20.0)
    kw.setdefault("rend_pct", 15.0)
    return juicio(
        db, dia, gap=-75.0, direccion=PERCEPCION_MEJOR, disociacion=True, **kw
    )


def test_el_contador_cuenta_solo_la_direccion_que_se_mira(db):
    """Nueve de ochenta y cuatro. El número que se mira una mañana mala.

    Dos disociaciones en la dirección que se destaca, una en la contraria y dos
    sesiones alineadas: el contador de arriba dice 2 de 5, no 3 de 5.
    """
    disociada(db, HOY - timedelta(days=1), clave="a")
    disociada(db, HOY - timedelta(days=3), clave="b")
    contraria(db, HOY - timedelta(days=5), clave="c")
    juicio(db, HOY - timedelta(days=7), clave="d")
    juicio(db, HOY - timedelta(days=9), clave="e")

    v = vista_percepcion(db, dias=30, hasta=HOY)

    assert v["contador"]["veces"] == 2
    assert v["contador"]["de"] == 5
    assert v["contador"]["pct"] == 40.0
    assert v["contraria"]["veces"] == 1
    assert v["alineadas"] == 2


def test_el_denominador_son_las_que_se_pudieron_juzgar_y_eso_se_dice(db):
    """"Nueve de ochenta y cuatro" solo significa algo si se sabe qué son las 84.

    Una sesión sin check-in esa mañana no es una sesión en la que la percepción
    acertara: es una sesión en la que no se preguntó. Meterla en el denominador
    haría bajar el contador cada vez que se olvidara un formulario, o sea,
    premiaría el olvido. Dejarla fuera y no decirlo sería peor. Salen las dos
    cifras y sale el reparto de motivos.
    """
    disociada(db, HOY - timedelta(days=1), clave="a")
    juicio(db, HOY - timedelta(days=2), clave="b")
    juicio(
        db,
        HOY - timedelta(days=3),
        clave="sin_manana",
        gap=None,
        direccion=SIN_DATO,
        base=30,
        motivo="solo 0 de los 6 deslizadores están contestados",
    )
    juicio(
        db,
        HOY - timedelta(days=4),
        clave="sin_manana_2",
        gap=None,
        direccion=SIN_DATO,
        base=30,
        motivo="solo 0 de los 6 deslizadores están contestados",
    )

    c = vista_percepcion(db, dias=30, hasta=HOY)["contador"]

    assert (c["veces"], c["de"], c["pct"]) == (1, 2, 50.0)
    assert c["total_sesiones"] == 4
    assert c["sin_juicio"] == 2
    assert c["motivos"] == {"solo 0 de los 6 deslizadores están contestados": 2}
    assert "no sobre todas las registradas" in c["nota"]


def test_una_sesion_con_pocas_sesiones_detras_no_entra_en_el_denominador(db):
    """El contador arrancaría lleno de falsos positivos que no se pueden borrar.

    La fila se guarda igual -con su dirección, que es una resta y no necesita
    muestra- pero ni cuenta arriba ni cuenta abajo, y el motivo sale escrito.
    """
    juicio(
        db,
        HOY - timedelta(days=1),
        clave="temprana",
        gap=60.0,
        direccion=PERCEPCION_PEOR,
        disociacion=True,  # aunque alguien la hubiera marcado
        base=BASE_MINIMA - 1,
    )

    c = vista_percepcion(db, dias=30, hasta=HOY)["contador"]

    assert c["veces"] == 0
    assert c["de"] == 0
    assert c["total_sesiones"] == 1
    assert c["sin_juicio"] == 1


def test_sin_ninguna_sesion_juzgable_el_contador_es_na_y_no_un_cero(db):
    """"Cero de cero" se lee como "nunca me ha pasado", y no es eso.

    Es "todavía no se puede contar", que es una frase distinta y que además
    pasa sola en cuanto haya material.
    """
    c = vista_percepcion(db, dias=30, hasta=HOY)["contador"]

    assert c["veces"] == 0
    assert c["de"] == 0
    assert c["pct"] is None
    assert "no es un cero" in c["na"]


def test_la_direccion_contraria_tambien_dice_por_que_no_puede_contar(db):
    """El motivo viaja pegado al número que explica, no al bloque de al lado.

    Los dos bloques comparten denominador, así que la tentación es dejar el
    motivo solo en el contador de arriba y dar por hecho que quien lea uno lee
    el otro. Pero se pintan por separado, y en cuanto la PWA saque este bloque
    en una pestaña propia el "0 de 0" se queda sin nada al lado que lo explique
    -y se lee como "nunca ha pasado", que es justo el error que toda la vista
    existe para no cometer-.

    Si alguien borra el `na` de `contraria` porque le parece redundante, este
    test se cae. Esa es toda su función.
    """
    v = vista_percepcion(db, dias=30, hasta=HOY)

    assert v["contraria"]["pct"] is None
    assert "no es un cero" in v["contraria"]["na"]

    # Y en cuanto hay material el motivo desaparece solo: un `na` que se quedara
    # pegado convertiría un porcentaje perfectamente calculable en sospechoso.
    disociada(db, HOY - timedelta(days=1), clave="a")
    contraria(db, HOY - timedelta(days=2), clave="b")

    v = vista_percepcion(db, dias=30, hasta=HOY)

    assert v["contraria"]["na"] is None
    assert v["contraria"]["pct"] == 50.0


def test_la_direccion_contraria_sale_contada_y_listada_pero_no_arriba(db):
    """Registrada y sin destacar. Es lo que hace creíble al número de arriba."""
    disociada(db, HOY - timedelta(days=1), clave="a")
    contraria(db, HOY - timedelta(days=2), clave="b")

    v = vista_percepcion(db, dias=30, hasta=HOY)

    assert v["contador"]["veces"] == 1
    assert v["contraria"]["veces"] == 1
    assert [d["fecha"] for d in v["disociaciones"]] == [
        (HOY - timedelta(days=1)).isoformat()
    ]
    assert [d["fecha"] for d in v["contrarias"]] == [
        (HOY - timedelta(days=2)).isoformat()
    ]
    assert v["contraria"]["que_es"]


def test_el_listado_va_del_mas_reciente_al_mas_viejo_y_trae_los_numeros(db):
    """El listado que se pidió: esos días, con sus números."""
    disociada(db, HOY - timedelta(days=10), clave="vieja")
    disociada(db, HOY - timedelta(days=2), clave="reciente")

    lista = vista_percepcion(db, dias=30, hasta=HOY)["disociaciones"]

    assert [d["fecha"] for d in lista] == [
        (HOY - timedelta(days=2)).isoformat(),
        (HOY - timedelta(days=10)).isoformat(),
    ]
    assert lista[0]["percepcion"] == 10.0
    assert lista[0]["rendimiento"] == 71.0
    assert lista[0]["gap"] == 60.0
    assert lista[0]["n_base"] == 30


def test_la_vista_ensena_lo_que_se_guardo_y_no_lo_recalcula(db):
    """La diferencia entre un histórico y una reconstrucción.

    Los percentiles de cada fila se calcularon con el histórico que había ESE
    día. Aquí se escribe a mano un gap que NO cuadra con la resta de los dos
    percentiles guardados: si la vista lo rehiciera, saldría 30 en vez del 99
    escrito, y el listado de "los días en que pasó" dejaría de coincidir con los
    días en que de verdad pasó.
    """
    juicio(
        db,
        HOY - timedelta(days=1),
        clave="rara",
        per_pct=20.0,
        rend_pct=50.0,
        gap=99.0,
        direccion=PERCEPCION_PEOR,
        disociacion=True,
    )

    fila = vista_percepcion(db, dias=30, hasta=HOY)["disociaciones"][0]
    assert fila["gap"] == 99.0


def test_los_componentes_salen_promediados_por_separado(db):
    """Un índice medio de 70 no dice de dónde sale. Esto sí.

    Cumplimiento 100 y 80 -> media 90. Progresión solo en una de las dos -> n=1
    y media 50. Y las piezas de bici, que aquí no existen, salen con su n a cero
    y el motivo escrito, no con un cero que se confundiría con "rindió cero".
    """
    juicio(
        db,
        HOY - timedelta(days=1),
        clave="a",
        comp_compliance=100.0,
        comp_progression=50.0,
    )
    juicio(db, HOY - timedelta(days=2), clave="b", comp_compliance=80.0)

    c = vista_percepcion(db, dias=30, hasta=HOY)["componentes"]

    assert c["cumplimiento"] == {
        "etiqueta": "Cumplimiento de lo prescrito",
        "corta": "cumplimiento",
        "media": 90.0,
        "n": 2,
        "na": None,
        "entra_en_el_indice": True,
    }
    assert c["progresion"]["media"] == 50.0
    assert c["progresion"]["n"] == 1
    assert c["corazon"]["media"] is None
    assert c["corazon"]["n"] == 0
    assert "ninguna sesión" in c["corazon"]["na"]


def test_cada_componente_viaja_con_su_nombre_escrito(db):
    """El nombre lo pone el backend, no la PWA.

    Sin esto, el móvil solo tiene la clave -`progresion`, `corazon`- y la pinta
    tal cual: seis palabras sin tilde con pinta de nombre de variable, justo en
    la vista que existe para leerse de un vistazo una mañana mala. La
    alternativa era un diccionario de nombres escrito a mano en JavaScript, que
    es otra copia de esta lista y se queda vieja en silencio el día que se añada
    una pieza. Este test es lo que sustituye a esa copia.

    Se comprueba también que la etiqueta no sea la clave disfrazada: si alguien
    añade un componente y lo etiqueta con su propio nombre, el test pasa pero la
    pantalla vuelve a enseñar la variable.
    """
    juicio(db, HOY - timedelta(days=1), clave="a", comp_compliance=100.0)

    c = vista_percepcion(db, dias=30, hasta=HOY)["componentes"]
    assert c, "la vista no devolvió ningún componente"

    for nombre, pieza in c.items():
        assert pieza.get("etiqueta"), f"`{nombre}` viaja sin etiqueta larga"
        assert pieza.get("corta"), f"`{nombre}` viaja sin etiqueta corta"
        assert pieza["etiqueta"] != nombre, (
            f"la etiqueta de `{nombre}` es la propia clave: en pantalla se lee "
            f"como un nombre de variable"
        )
        # La larga dice qué MIDE; la corta cabe en la tira de una línea de cada
        # sesión. Si fueran iguales, una de las dos sobra y la otra no hace su
        # trabajo.
        assert len(pieza["etiqueta"]) > len(pieza["corta"])


def test_el_desnivel_sigue_marcado_como_fuera_del_indice_en_las_medias(db):
    """Se promedia como contexto -por qué terreno se ha rodado- y nada más.

    La bandera viaja hasta el último sitio donde aparece el número, porque el
    sitio donde alguien lo sumaría por error es precisamente este: una tabla de
    medias donde todas las demás filas sí son rendimiento.
    """
    juicio(
        db,
        HOY - timedelta(days=1),
        kind=BICI,
        clave="a",
        comp_bike_elevation=30.0,
        comp_bike_speed=70.0,
    )

    c = vista_percepcion(db, dias=30, hasta=HOY)["componentes"]

    assert c["desnivel"]["media"] == 30.0
    assert c["desnivel"]["entra_en_el_indice"] is False
    assert c["velocidad"]["entra_en_el_indice"] is True


def test_por_tipo_separa_fuerza_y_bici(db):
    disociada(db, HOY - timedelta(days=1), kind=FUERZA, clave="a")
    juicio(db, HOY - timedelta(days=2), kind=BICI, clave="b")
    juicio(db, HOY - timedelta(days=3), kind=BICI, clave="c")

    t = vista_percepcion(db, dias=30, hasta=HOY)["por_tipo"]

    assert t[FUERZA] == {"sesiones": 1, "juzgadas": 1, "veces": 1}
    assert t[BICI] == {"sesiones": 2, "juzgadas": 2, "veces": 0}


def test_la_ventana_recorta_por_fecha_y_lo_dice(db):
    disociada(db, HOY - timedelta(days=2), clave="dentro")
    disociada(db, HOY - timedelta(days=40), clave="fuera")

    v = vista_percepcion(db, dias=30, hasta=HOY)

    assert v["contador"]["veces"] == 1
    assert v["ventana"] == {
        "desde": (HOY - timedelta(days=29)).isoformat(),
        "hasta": HOY.isoformat(),
        "dias": 30,
    }


# ---------------------------------------------------------------------------
# La frase
# ---------------------------------------------------------------------------

COMPONENTES_FUERZA = {
    "rendimiento": {
        "componentes": {
            "cumplimiento": {"valor": 100.0, "logradas": 12, "prescritas": 12},
            "progresion": {"valor": 75.0, "variacion_media_pct": 5.0},
            "esfuerzo": {"valor": 70.0, "rpe": 7.0, "carga_pct": 90.0},
        }
    }
}


def test_el_mensaje_lleva_los_numeros_concretos(db):
    """Concreto, verificable y aburrido. Ese es todo el valor que tiene."""
    f = disociada(
        db, date(2026, 9, 10), clave="a", componentes=COMPONENTES_FUERZA
    )

    texto = mensaje_disociacion(f, veces=9, de=84)

    assert "jueves 10 de septiembre" in texto.lower()
    assert "10 de 100" in texto  # la percepción cruda
    assert "percentil 8" in texto  # dónde cayó esa mañana
    assert "percentil 68" in texto  # dónde cayó la sesión
    assert "12 de 12 series" in texto
    assert "+5% de carga" in texto
    assert "RPE 7" in texto
    assert "Van 9 de 84" in texto


def test_el_mensaje_no_consuela_no_anima_y_no_propone_nada():
    """Una frase que consuela se aprende a descontar en dos semanas.

    "Ya está la app diciéndome que soy fuerte" y a partir de ahí no sirve de
    nada. Una cifra no se descuenta.

    Y no propone: no sugiere entrenar, no sugiere descansar, no dice qué hacer
    con el dato. La sesión de hoy la decide el motor con sus reglas, y mezclar
    las dos cosas convertiría el contador en un argumento.
    """
    f = SessionPerformance(
        date=date(2026, 9, 10),
        kind=FUERZA,
        source_key="a",
        perception_index=10.0,
        perception_pct=8.0,
        performance_index=71.0,
        performance_pct=68.0,
        direction=PERCEPCION_PEOR,
        dissociation=True,
        components_json=json.dumps(COMPONENTES_FUERZA),
    )

    texto = mensaje_disociacion(f, veces=9, de=84).lower()

    for palabra in (
        "capaz",
        "puedes",
        "enhorabuena",
        "bien hecho",
        "ánimo",
        "recuerda",
        "deberías",
        "intenta",
        "no te preocupes",
        "confía",
        "orgullo",
        "fuerte",
        "descansa",
        "entrena",
    ):
        assert palabra not in texto, f"el mensaje no puede decir '{palabra}'"


def test_el_mensaje_de_bici_habla_de_velocidad_y_zonas_no_de_series():
    """La bici no tiene potenciómetro, así que se cuenta lo que sí hay."""
    f = SessionPerformance(
        date=date(2026, 9, 10),
        kind=BICI,
        source_key="b",
        perception_index=10.0,
        perception_pct=8.0,
        performance_index=71.0,
        performance_pct=68.0,
        direction=PERCEPCION_PEOR,
        dissociation=True,
        components_json=json.dumps(
            {
                "rendimiento": {
                    "metricas": {
                        "km": 42.0,
                        "velocidad_kmh": 24.5,
                        "desnivel_m_km": 12.0,
                    },
                    "componentes": {
                        "corazon": {"fuente_fc": "zonas", "coste_cardiaco": 2.4}
                    },
                }
            }
        ),
    )

    texto = mensaje_disociacion(f, veces=3, de=40)

    assert "La salida en bici" in texto
    assert "42 km a 24,5 km/h" in texto
    assert "12 m/km de desnivel" in texto
    assert "zona media 2,4 de 5" in texto
    assert "series" not in texto


def test_el_mensaje_sale_aunque_falte_algun_componente():
    """Una sesión sin RPE todavía tiene mañana, sesión y contador que contar."""
    f = SessionPerformance(
        date=date(2026, 9, 10),
        kind=FUERZA,
        source_key="a",
        perception_index=10.0,
        perception_pct=8.0,
        performance_index=71.0,
        performance_pct=68.0,
        direction=PERCEPCION_PEOR,
        dissociation=True,
        components_json=None,
    )

    texto = mensaje_disociacion(f, veces=1, de=20)

    assert "percentil 8" in texto
    assert "Van 1 de 20" in texto
    assert "Los números:" not in texto  # no se inventa una línea vacía


# ---------------------------------------------------------------------------
# Que lo que escribe el evaluador sea lo que sabe leer el mensaje
# ---------------------------------------------------------------------------
#
# Todos los tests de la frase de aquí arriba le pasan a `mensaje_disociacion` un
# diccionario escrito a mano en este mismo fichero. Eso prueba el formato, y
# está bien que lo pruebe, pero no prueba el CONTRATO: nadie comprueba que la
# forma que `evaluar_sesion` guarda en `components_json` sea la forma que
# `_frase_fuerza` y `_frase_bici` van a buscar después.
#
# Y esa costura tiene renombres de verdad: el coste cardiaco se calcula como
# `{"valor", "fuente"}` y se guarda como `{"coste_cardiaco", "fuente_fc"}`. El
# día que alguien toque una de las dos puntas, los diccionarios a mano seguirán
# teniendo la forma vieja, los tests seguirán en verde, y el mensaje perderá en
# silencio su línea de números -que es la única razón por la que existe: ser
# concreto y verificable-. Los dos tests que siguen son los únicos en los que el
# diccionario lo escribe el evaluador.


def _sesion_de_fuerza_evaluada(db, *, kg_antes=60.0, kg_hoy=63.0):
    """Dos sesiones de la misma rutina, con el RPE de la mañana siguiente."""
    previo, dia = HOY - timedelta(days=9), HOY - timedelta(days=2)
    for d in (previo, previo + timedelta(days=1), dia, dia + timedelta(days=1)):
        checkin(db, d, rpe=7.0)
    for d, wid, kg in ((previo, "W0", kg_antes), (dia, "W1", kg_hoy)):
        decision(db, d, [ejercicio("press", [serie(), serie(), serie()])])
        db.add(
            WorkoutLog(
                hevy_workout_id=wid,
                date=d,
                routine_key="dia1",
                raw_json=json.dumps(entreno(hecho("press", [serie(kg=kg)] * 3))),
            )
        )
    db.commit()
    evaluar_pendientes(db, CFG, hasta=HOY, dias=30)
    db.commit()
    return db.scalar(
        select(SessionPerformance).where(SessionPerformance.source_key == "hevy:W1")
    )


def _numeros(texto, fila):
    """La línea de números, o un fallo que enseña el diccionario que la rompió."""
    linea = next(
        (ln for ln in texto.splitlines() if ln.startswith("Los números:")), None
    )
    assert linea is not None, (
        "el evaluador ha guardado unos componentes que el mensaje ya no sabe "
        f"leer, así que el aviso sale sin sus cifras: {fila.components_json}"
    )
    return linea


def test_los_numeros_de_fuerza_los_escribe_el_evaluador_y_los_lee_el_mensaje(db):
    """Las tres piezas de fuerza, de punta a punta y sin diccionarios a mano.

    Con carga distinta -60 a 63 kg- a propósito. Con la misma carga en los dos
    días la variación sale 0, y un 0 es indistinguible de la clave que falta:
    el test pasaría igual con la punta que escribe renombrada, que es justo lo
    que este test existe para no dejar pasar.
    """
    f = _sesion_de_fuerza_evaluada(db)

    linea = _numeros(mensaje_disociacion(f, veces=3, de=40), f)

    assert "3 de 3 series efectivas completadas" in linea
    assert "+5% de carga sobre la vez anterior" in linea
    assert "RPE 7 moviendo más volumen que el 100% de tus sesiones" in linea


def test_repetir_carga_se_dice_con_palabras_y_no_con_un_cero_por_ciento(db):
    """"La misma carga que la vez anterior", no "+0% de carga".

    Sostener es el resultado normal y bueno de la mayoría de las sesiones con
    una hernia L4-L5. Un "+0%" en un mensaje que existe para hacer de contrapeso
    se lee como un cero pelado, que es exactamente la lectura que sobra.
    """
    f = _sesion_de_fuerza_evaluada(db, kg_antes=60.0, kg_hoy=60.0)

    linea = _numeros(mensaje_disociacion(f, veces=3, de=40), f)

    assert "la misma carga que la vez anterior" in linea
    assert "%" not in linea.split(";")[1], "la variación no se dice en porcentaje"


def test_una_progresion_a_medias_revienta_en_vez_de_inventarse_la_frase(db):
    """Si la clave de la variación no está, no se dice "la misma carga".

    Es el único sitio de la frase donde una clave ausente podría convertirse en
    una afirmación concreta y falsa sobre el entreno en vez de en un hueco. El
    mensaje entero vale por ser verificable; preferimos que reviente y que
    mañana se reintente -`reported_at` solo se pone si el envío sale bien- a que
    salga diciendo con aplomo algo que no sabe.
    """
    f = SessionPerformance(
        date=date(2026, 9, 10),
        kind=FUERZA,
        source_key="a",
        perception_index=10.0,
        perception_pct=8.0,
        performance_index=71.0,
        performance_pct=68.0,
        direction=PERCEPCION_PEOR,
        dissociation=True,
        components_json=json.dumps(
            {"rendimiento": {"componentes": {"progresion": {"valor": 75.0}}}}
        ),
    )

    with pytest.raises(KeyError):
        mensaje_disociacion(f, veces=3, de=40)


def test_los_numeros_de_bici_los_escribe_el_evaluador_y_los_lee_el_mensaje(db):
    """Lo mismo para la bici, donde el renombre de las claves es real.

    `coste_cardiaco` y `fuente_fc` no se llaman así en el sitio donde se
    calculan. Este es el único test que recorre las dos puntas.
    """
    previo, dia = HOY - timedelta(days=9), HOY - timedelta(days=2)
    for d in (previo, previo + timedelta(days=1), dia, dia + timedelta(days=1)):
        checkin(db, d, rpe=7.0)
    db.add(actividad(id_garmin=10, dia=previo, zonas=(1800.0, 1800.0, 0.0, 0.0, 0.0)))
    db.add(
        actividad(
            id_garmin=11,
            dia=dia,
            metros=42000.0,
            segundos=6000.0,
            desnivel=500.0,
            zonas=(3000.0, 3000.0, 0.0, 0.0, 0.0),
        )
    )
    db.commit()
    evaluar_pendientes(db, CFG, hasta=HOY, dias=30)
    db.commit()
    f = db.scalar(
        select(SessionPerformance).where(SessionPerformance.source_key == "garmin:11")
    )

    linea = _numeros(mensaje_disociacion(f, veces=3, de=40), f)

    assert "42 km a 25,2 km/h con 11,9 m/km de desnivel" in linea
    assert "zona media 1,5 de 5" in linea
    assert "series" not in linea, "la bici no tiene series que contar"


def test_sin_zonas_el_mensaje_dice_pulsaciones_y_no_finge_una_zona(db):
    """Un 140 y un 1,5 no se pueden leer con la misma frase.

    Sin zonas el coste cardiaco se cae a la media de pulsaciones, que es un
    número de otra escala. Decir "zona media 140 de 5" sería falso; la frase
    tiene que cambiar con la fuente, igual que cambia el índice.
    """
    previo, dia = HOY - timedelta(days=9), HOY - timedelta(days=2)
    for d in (previo, previo + timedelta(days=1), dia, dia + timedelta(days=1)):
        checkin(db, d, rpe=7.0)
    db.add(actividad(id_garmin=20, dia=previo, fc_media=130.0))
    db.add(actividad(id_garmin=21, dia=dia, fc_media=142.0))
    db.commit()
    evaluar_pendientes(db, CFG, hasta=HOY, dias=30)
    db.commit()
    f = db.scalar(
        select(SessionPerformance).where(SessionPerformance.source_key == "garmin:21")
    )

    linea = _numeros(mensaje_disociacion(f, veces=3, de=40), f)

    assert "142 ppm de media" in linea
    assert "zona media" not in linea, "sin zonas no se puede hablar de zonas"


def test_sin_desnivel_la_frase_se_calla_esa_parte_en_vez_de_poner_una_raya():
    """Un rodillo no da desnivel, y `fmt_num(None)` sale como una raya.

    "con — m/km de desnivel" es ruido con pinta de dato. La frase se queda en
    los kilómetros y la velocidad, que son ciertos con altímetro y sin él.

    Este es el final del recorrido que empezó siendo `or 0.0`: antes esta misma
    salida decía "con 0 m/km de desnivel", que no es un hueco, es una
    afirmación, y encima falsa el día que el altímetro falle subiendo un puerto.

    Va contra `_frase_bici` y no de punta a punta como sus vecinos porque una
    salida sin desnivel no puntúa, luego nunca llega a ser una disociación y
    nunca genera mensaje. La costura entre `rendimiento_bici` y la frase la
    cubren los dos tests de arriba; lo que falta comprobar es solo el formato.
    """
    r = rendimiento_bici(actividad(desnivel=None, fc_media=142.0), [])

    linea = "; ".join(_frase_bici(r))

    assert "20 km a 20 km/h" in linea
    assert "142 ppm de media" in linea
    assert "desnivel" not in linea, "sin altímetro no se habla de desnivel"
    assert "—" not in linea, "una raya donde iba un número es ruido con pinta de dato"


# ---------------------------------------------------------------------------
# Lo que falta por decir
# ---------------------------------------------------------------------------


def test_pendientes_solo_trae_la_direccion_que_se_destaca(db):
    """La contraria se guarda y se cuenta en pantalla, pero no se manda.

    Avisar también de los días en que la mañana prometió de más convertiría el
    aviso en un comentario diario sobre el estado de ánimo, que no es lo que se
    pidió ni lo que hace falta.
    """
    disociada(db, HOY - timedelta(days=1), clave="a")
    contraria(db, HOY - timedelta(days=2), clave="b")
    juicio(db, HOY - timedelta(days=3), clave="c")

    assert [f.source_key for f in pendientes_de_avisar(db, hasta=HOY)] == ["a"]


def test_lo_ya_reportado_no_vuelve_a_salir(db):
    """Sin la marca, el mismo día saldría cada mañana hasta cambiar de ventana."""
    f = disociada(db, HOY - timedelta(days=1), clave="a")
    assert pendientes_de_avisar(db, hasta=HOY) == [f]

    marcar_reportadas(db, [f], cuando=datetime(2026, 9, 11, 7, 0))
    db.commit()

    assert pendientes_de_avisar(db, hasta=HOY) == []


def test_una_disociacion_vieja_no_se_avisa_tarde(db):
    """Mandarla con tres semanas de retraso solo enseñaría que el aviso no es de fiar.

    Ya no es una noticia de esta mañana. Se queda en el listado de la pantalla,
    que es donde le toca estar.
    """
    disociada(db, HOY - timedelta(days=40), clave="vieja")

    assert pendientes_de_avisar(db, hasta=HOY, dias=30) == []
    assert vista_percepcion(db, dias=60, hasta=HOY)["contador"]["veces"] == 1


def test_marcar_reportadas_no_toca_ningun_numero_del_juicio(db):
    """`reported_at` no es una excepción al append-only: es otra cosa.

    No entra en ningún índice, ni en el contador, ni en el listado. Es la marca
    de "esto ya se ha contado" y nada más, y por eso puede escribirse cuando
    todo lo demás no.
    """
    f = disociada(db, HOY - timedelta(days=1), clave="a")
    antes = (
        f.perception_index,
        f.performance_index,
        f.gap_pct,
        f.direction,
        f.dissociation,
        f.n_sessions_base,
    )

    marcar_reportadas(db, [f], cuando=datetime(2026, 9, 11, 7, 0))
    db.commit()
    db.expire_all()

    f = db.scalar(select(SessionPerformance).where(SessionPerformance.source_key == "a"))
    assert f.reported_at is not None
    assert (
        f.perception_index,
        f.performance_index,
        f.gap_pct,
        f.direction,
        f.dissociation,
        f.n_sessions_base,
    ) == antes
