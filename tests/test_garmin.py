"""Lectura de Garmin: normalización de actividades y política de 429.

Los dos puntos que importan:

1. La conversión actividad -> `Ride` vive fuera del cliente para que la caché
   en disco y la API produzcan objetos IDÉNTICOS. Si divergieran, los
   percentiles se calcularían sobre una distribución distinta de la real.

2. Un 429 persistente NO se traga. Un sistema que decide con datos a medias y
   no lo dice es peor que uno que falla. Y un 429 SUPERADO al reintentar
   tampoco desaparece: se anota, porque sigue siendo un aviso de que la IP
   está limitada.
"""

from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.integrations import garmin
from app.integrations.garmin import (
    HORA_TOPE_MANANA,
    MAX_RETRIES,
    GarminError,
    GarminRateLimited,
    _is_rate_limit,
    _retry,
    ride_from_activity,
    rides_from_activities,
)
from tests.dobles import doble_de
from app.settings import Settings
from garminconnect import Garmin


def actividad(**kwargs) -> dict:
    base = {
        "activityId": 111,
        "activityName": "Salida de tarde",
        "activityType": {"typeKey": "cycling"},
        "startTimeLocal": "2026-09-07 18:30:00",
        "duration": 3600.0,
        "distance": 30000.0,
        "activityTrainingLoad": 150.0,
        "aerobicTrainingEffect": 3.2,
        "anaerobicTrainingEffect": 0.4,
    }
    base.update(kwargs)
    return base


@pytest.fixture(autouse=True)
def sin_esperas(monkeypatch):
    """Los backoff son de hasta 32 s. En los tests no se espera de verdad."""
    monkeypatch.setattr(garmin.time, "sleep", lambda *_: None)


# ---------------------------------------------------------------------------
# Normalización
# ---------------------------------------------------------------------------


def test_una_salida_en_bici_se_convierte_entera():
    r = ride_from_activity(actividad())
    assert r is not None
    assert r.date == date(2026, 9, 7)
    assert r.duration_s == 3600.0
    assert r.distance_m == 30000.0
    assert r.training_load == 150.0
    assert r.activity_id == 111
    assert r.is_cycling


def test_el_desnivel_la_duracion_en_movimiento_y_la_media_de_pulso_se_leen():
    """Tres campos que estaban en el modelo y no los leía nadie.

    `elevation_gain_m` se calculaba, se guardaba, se comparaba contra el
    histórico y se contaba en el mensaje de Telegram... sobre una columna que
    ningún camino escribía. El desnivel es una de las tres piezas que se pidieron
    para juzgar la bici, que no tiene potenciómetro, así que no era un adorno.
    """
    r = ride_from_activity(
        actividad(elevationGain=540.0, movingDuration=3400.0, averageHR=138.0)
    )

    assert r is not None
    assert r.elevation_gain_m == 540.0
    assert r.moving_duration_s == 3400.0
    assert r.avg_hr == 138.0


def test_una_salida_sin_desnivel_lo_deja_en_none_y_no_en_cero():
    """Rodillo de interior, o un dispositivo sin altímetro.

    Cero metros y "no lo sé" son cosas distintas, y esta es la punta donde se
    decide cuál de las dos viaja. Un 0 aquí llegaría hasta el mensaje convertido
    en la afirmación "0 m/km de desnivel".
    """
    r = ride_from_activity(actividad())

    assert r is not None
    assert r.elevation_gain_m is None
    assert r.avg_hr is None


def test_los_diez_campos_nuevos_se_leen_con_el_nombre_que_manda_garmin():
    """Lo que ya venía en el crudo y se estaba tirando en cada salida.

    El inventario de las ochenta y nueve actividades de la caché dejó claro que
    no hay NADA de potencia -ni `avgPower`, ni normalizada, ni TSS-, así que
    juzgar la bici es pulso contra zonas, velocidad y desnivel y nada más. Estos
    diez son los que se aprobaron de esa lista:

      - `maxHR`, que estaba en las ochenta y nueve. Sin él, de una salida solo se
        sabe la media, y una media de 138 esconde tanto un tempo plano como una
        sucesión de repechos.
      - las dos velocidades y el desnivel NEGATIVO, que es la otra mitad del
        perfil: 540 m de subida con 40 de bajada y con 540 son dos salidas
        distintas y hasta ahora se contaban igual.
      - `calories`, en las ochenta y nueve.
      - las tres de respiración, en cincuenta y siete.
      - las dos de temperatura, para poder preguntarle al verano si tuvo algo
        que ver con julio -nueve salidas, todas suaves- en vez de suponerlo.

    La cadencia se quedó fuera a propósito: falta en dieciséis de cincuenta y
    ocho y no se sabe por qué. Un campo que aparece en dos de cada tres salidas
    sin explicación no es una señal, es una pregunta abierta.

    El nombre de cada uno se comprueba aquí porque un `act.get()` con la clave
    mal escrita no falla: devuelve None y la columna se queda vacía para
    siempre, que es la forma exacta en que estos diez llevaban meses perdiéndose.
    """
    r = ride_from_activity(actividad(
        maxHR=171.0,
        averageSpeed=6.94, maxSpeed=13.2,
        elevationLoss=505.0,
        calories=742.0,
        avgRespirationRate=21.0, maxRespirationRate=34.0, minRespirationRate=12.0,
        maxTemperature=34.0, minTemperature=21.0,
    ))

    assert r is not None
    assert r.max_hr == 171.0
    assert (r.avg_speed_mps, r.max_speed_mps) == (6.94, 13.2)
    assert r.elevation_loss_m == 505.0
    assert r.calories == 742.0
    assert (r.avg_respiration, r.max_respiration, r.min_respiration) == (21.0, 34.0, 12.0)
    assert (r.max_temp_c, r.min_temp_c) == (34.0, 21.0)


def test_los_diez_campos_nuevos_ausentes_se_quedan_en_none():
    """Un rodillo de interior no tiene temperatura, ni desnivel, ni velocidad.

    Y una salida sin cinta no tiene pulso. Que falten no es una avería, así que
    no hay nada que avisar; lo que no puede pasar es que se conviertan en ceros,
    porque un 0 en `max_temp_c` participaría en la media de julio como si esa
    salida hubiera sido a cero grados.
    """
    r = ride_from_activity(actividad())

    assert r is not None
    assert (r.max_hr, r.avg_speed_mps, r.max_speed_mps) == (None, None, None)
    assert (r.elevation_loss_m, r.calories) == (None, None)
    assert (r.avg_respiration, r.max_respiration, r.min_respiration) == (None,) * 3
    assert (r.max_temp_c, r.min_temp_c) == (None, None)


@pytest.mark.parametrize(
    "tipo",
    ["cycling", "road_biking", "gravel_cycling", "indoor_cycling", "virtual_ride"],
)
def test_todos_los_tipos_de_bici_cuentan(tipo):
    assert ride_from_activity(actividad(activityType={"typeKey": tipo})) is not None


@pytest.mark.parametrize("tipo", ["running", "strength_training", "swimming", ""])
def test_lo_que_no_es_bici_se_descarta(tipo):
    assert ride_from_activity(actividad(activityType={"typeKey": tipo})) is None


def test_el_tipo_se_compara_en_minusculas():
    assert ride_from_activity(actividad(activityType={"typeKey": "CYCLING"})) is not None


def test_una_fecha_ilegible_en_una_salida_de_bici_revienta():
    """Este test decía lo contrario y estaba mal.

    Descartar la actividad "en vez de inventarla" suena prudente, pero la
    alternativa a inventarse la fecha no era tirar la salida: era decir que no
    se puede leer. Una salida real que desaparece resta carga de `load_2d/7d`
    y deja el día en verde cuando tocaba ámbar, sin que nada lo cuente.

    Además no es un caso raro y aislado. Garmin manda `startTimeLocal` en ISO
    siempre; si deja de poder leerse es que cambió el formato, y entonces no
    se pierde una salida, se pierden todas.
    """
    act = actividad(startTimeLocal="ayer por la tarde", startTimeGMT=None)
    with pytest.raises(GarminError, match="fecha ilegible"):
        ride_from_activity(act)


def test_el_error_de_fecha_dice_qué_actividad_y_qué_fecha():
    """Sin el id no hay forma de ir a mirarla en Garmin."""
    act = actividad(startTimeLocal="ayer por la tarde", startTimeGMT=None)
    with pytest.raises(GarminError) as exc:
        ride_from_activity(act)
    assert "111" in str(exc.value)
    assert "ayer por la tarde" in str(exc.value)


def test_una_fecha_ilegible_en_algo_que_no_es_bici_sigue_sin_molestar():
    """El filtro de tipo va primero: no se revienta por una carrera rara."""
    act = actividad(
        activityType={"typeKey": "running"},
        startTimeLocal="ayer por la tarde",
        startTimeGMT=None,
    )
    assert ride_from_activity(act) is None


def test_si_falta_la_hora_local_se_usa_la_gmt():
    act = actividad(startTimeLocal=None, startTimeGMT="2026-09-06T16:30:00Z")
    r = ride_from_activity(act)
    assert r is not None and r.date == date(2026, 9, 6)


def test_las_zonas_se_leen_de_los_cinco_campos():
    act = actividad(**{f"hrTimeInZone_{i}": i * 100 for i in range(1, 6)})
    r = ride_from_activity(act)
    assert r is not None
    assert r.zones == (100.0, 200.0, 300.0, 400.0, 500.0)


def test_sin_ninguna_zona_el_campo_queda_a_none_no_a_ceros():
    """Cero segundos en todas las zonas y 'no hay dato de zonas' no son lo mismo."""
    r = ride_from_activity(actividad())
    assert r is not None and r.zones is None


def test_una_zona_parcial_rellena_las_demas_a_cero():
    r = ride_from_activity(actividad(hrTimeInZone_4=600))
    assert r is not None and r.zones == (0.0, 0.0, 0.0, 600.0, 0.0)


def test_rides_from_activities_filtra_y_conserva_el_orden():
    crudas = [
        actividad(activityId=1),
        actividad(activityId=2, activityType={"typeKey": "running"}),
        actividad(activityId=3, startTimeLocal="2026-09-08 07:00:00"),
    ]
    rides = rides_from_activities(crudas)
    assert [r.activity_id for r in rides] == [1, 3]


def test_rides_from_activities_tolera_una_lista_vacia_o_nula():
    assert rides_from_activities([]) == []
    assert rides_from_activities(None) == []


# ---------------------------------------------------------------------------
# Política de 429
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mensaje",
    ["HTTP 429", "Too Many Requests", "rate limit exceeded"],
)
def test_se_reconoce_un_429_venga_como_venga(mensaje):
    assert _is_rate_limit(RuntimeError(mensaje))


def test_un_error_normal_no_se_confunde_con_un_429():
    assert not _is_rate_limit(RuntimeError("401 Unauthorized"))


def test_un_error_que_no_es_429_no_se_reintenta():
    intentos = {"n": 0}

    def falla():
        intentos["n"] += 1
        raise RuntimeError("401 Unauthorized")

    with pytest.raises(RuntimeError, match="401"):
        _retry(falla, what="prueba")
    assert intentos["n"] == 1, "un 401 no mejora por insistir"


def test_un_429_superado_al_reintentar_se_anota_igualmente():
    """Sobrevivir a un 429 sigue siendo un aviso de que la IP está limitada.

    El informe tiene que poder contarlo en vez de presentar el resultado como
    si la lectura hubiera ido fina.
    """
    intentos = {"n": 0}
    avisos: list[str] = []

    def a_la_tercera():
        intentos["n"] += 1
        if intentos["n"] < 3:
            raise RuntimeError("HTTP 429 Too Many Requests")
        return "datos"

    assert _retry(a_la_tercera, what="hrv", sink=avisos) == "datos"
    assert len(avisos) == 2
    assert all("429 en 'hrv'" in a for a in avisos)


def test_un_429_persistente_se_propaga_como_su_propio_error():
    """No se decide con datos a medias: mejor fallar y reintentar más tarde."""
    avisos: list[str] = []

    def siempre_429():
        raise RuntimeError("429")

    with pytest.raises(GarminRateLimited) as exc:
        _retry(siempre_429, what="login", sink=avisos)

    assert "login" in str(exc.value)
    assert len(avisos) == MAX_RETRIES
    assert isinstance(exc.value, GarminError), "el llamante genérico también debe verlo"


def test_una_llamada_correcta_no_ensucia_el_registro_de_429():
    avisos: list[str] = []
    assert _retry(lambda: 7, what="stats", sink=avisos) == 7
    assert avisos == []


# ---------------------------------------------------------------------------
# build_client
# ---------------------------------------------------------------------------


@doble_de(Settings)
class SettingsFalsos:
    garmin_email = ""
    garmin_password = ""
    garmin_token_dir = "/tmp/tokens"


@doble_de(Settings)
class SettingsConCredenciales(SettingsFalsos):
    garmin_email = "a@b.c"
    garmin_password = "x"


def test_sin_credenciales_no_se_construye_el_cliente():
    with pytest.raises(GarminError, match="GARMIN_EMAIL"):
        garmin.build_client(SettingsFalsos())


# ---------------------------------------------------------------------------
# La política de reintentos sale del config, no de dos constantes
# ---------------------------------------------------------------------------
#
# `schedule.garmin_retry` llevaba declarado desde el principio -3 intentos,
# esperas de 60/300/900 s- y no lo leía nadie: el código reintentaba 5 veces con
# esperas de 2, 4, 8, 16 y 32. No era una función que faltara, era una
# CONTRADICCIÓN, y la que perdía era la del YAML, que es la que se lee cuando
# alguien quiere entender qué hace el sistema.


def test_la_politica_sale_del_config():
    cfg = {"schedule": {"garmin_retry": {
        "attempts": 3, "backoff_seconds": [60, 300, 900]
    }}}
    p = garmin.retry_policy_from_config(cfg)
    assert p.attempts == 3
    assert p.espera(1) == 60.0
    assert p.espera(2) == 300.0


def test_sin_seccion_se_mantiene_el_exponencial_de_siempre():
    """Un config sin la sección no puede quedarse SIN reintentos.

    El defecto de un ajuste que falta tiene que ser el comportamiento anterior,
    no el vacío: si no, añadir validación al YAML apagaría la resiliencia de
    quien no se haya enterado.
    """
    p = garmin.retry_policy_from_config({})
    assert p.attempts == MAX_RETRIES
    assert [p.espera(i) for i in (1, 2, 3)] == [2.0, 4.0, 8.0]


def test_el_cliente_construido_lleva_la_politica_del_config():
    """El eslabón que faltaba: leerla y no usarla no arregla nada."""
    cfg = {"schedule": {"garmin_retry": {
        "attempts": 2, "backoff_seconds": [45]
    }}}
    c = garmin.build_client(SettingsConCredenciales(), cfg)
    assert c.retry.attempts == 2
    assert c.retry.espera(1) == 45.0


def test_reintentar_respeta_los_intentos_y_las_esperas_de_la_politica(monkeypatch):
    """Se comprueban las ESPERAS, no solo el número de intentos.

    Contar intentos deja pasar el fallo que motivó todo esto: cinco reintentos
    con esperas de dos segundos caben enteros dentro del mismo bloqueo de
    Garmin, así que el sistema insiste cinco veces a una puerta cerrada y se
    rinde en un minuto. El margen real es la suma de las esperas, y es lo único
    que decide si el 429 se sobrevive.
    """
    dormido: list[float] = []
    monkeypatch.setattr(garmin.time, "sleep", lambda s: dormido.append(s))

    pol = garmin.RetryPolicy(attempts=3, backoffs=(60.0, 300.0, 900.0))
    intentos = {"n": 0}

    def siempre_429():
        intentos["n"] += 1
        raise RuntimeError("429")

    with pytest.raises(GarminRateLimited):
        _retry(siempre_429, what="hrv", policy=pol)

    assert intentos["n"] == 3, "se intenta exactamente lo que dice el config"
    assert dormido == [60.0, 300.0], "entre 3 intentos hay 2 esperas"
    assert sum(dormido) == 360.0, (
        "seis minutos de margen; con el exponencial corto eran 6 segundos"
    )


def test_menos_intentos_que_el_defecto_se_respetan(monkeypatch):
    """Con `attempts` por DEBAJO de `MAX_RETRIES`, para poder notar la diferencia.

    Si el bucle se guiara por la constante en vez de por la política, un config
    que pide 2 intentos haría 5. Contra una API que limita por IP eso no es un
    detalle: son tres llamadas de más justo cuando ya te está diciendo que
    pares.
    """
    monkeypatch.setattr(garmin.time, "sleep", lambda *_: None)
    intentos = {"n": 0}

    def siempre_429():
        intentos["n"] += 1
        raise RuntimeError("429")

    with pytest.raises(GarminRateLimited):
        _retry(siempre_429, what="hrv", policy=garmin.RetryPolicy(attempts=2))

    assert intentos["n"] == 2, f"{MAX_RETRIES=} no manda sobre el config"


def test_no_se_espera_despues_del_ultimo_intento(monkeypatch):
    """Dormir para luego rendirse solo retrasa el aviso.

    Con las esperas del config real serían quince minutos de más antes de que
    el mensaje de error llegue al móvil.
    """
    dormido: list[float] = []
    monkeypatch.setattr(garmin.time, "sleep", lambda s: dormido.append(s))

    def siempre_429():
        raise RuntimeError("429")

    with pytest.raises(GarminRateLimited):
        _retry(
            siempre_429, what="hrv",
            policy=garmin.RetryPolicy(attempts=3, backoffs=(60.0, 300.0, 900.0)),
        )

    assert len(dormido) == 2, "3 intentos, 2 esperas: la tercera no se duerme"
    assert 900.0 not in dormido


def test_la_politica_del_config_de_verdad_da_mas_margen_que_el_defecto():
    """La comprobación que da sentido al cambio, medida sobre el config real."""
    from app.config_loader import load_config
    from app.settings import REPO_ROOT

    real = garmin.retry_policy_from_config(load_config(REPO_ROOT / "config.yaml"))
    defecto = garmin.RetryPolicy()

    margen = sum(real.espera(i) for i in range(1, real.attempts))
    antes = sum(defecto.espera(i) for i in range(1, defecto.attempts))

    assert margen > antes, (
        f"el config da {margen:.0f} s de margen y el defecto {antes:.0f} s: "
        f"si esto se invierte, un 429 real vuelve a no sobrevivirse"
    )
    assert margen >= 300, "un 429 de Garmin dura minutos, no segundos"


def test_el_cliente_sin_conectar_se_niega_a_leer():
    c = garmin.GarminClient(email="a@b.c", password="x", token_dir="/tmp")
    with pytest.raises(GarminError, match="no conectado"):
        c.day_metrics(date(2026, 9, 7))
    with pytest.raises(GarminError, match="no conectado"):
        c.rides(date(2026, 9, 1), date(2026, 9, 7))


# ---------------------------------------------------------------------------
# Un hueco por error de lectura no es un hueco de verdad
# ---------------------------------------------------------------------------
#
# Las cuatro lecturas de wellness atrapaban cualquier excepción y la mandaban a
# `log.debug`. Como el nivel por defecto no imprime los debug, "esa noche no
# hubo HRV" y "no se pudo leer el HRV de esa noche" acababan siendo el mismo
# `None` y ni el log ni el informe distinguían uno de otro.


# ---------------------------------------------------------------------------
# Actividades en crudo
# ---------------------------------------------------------------------------


@doble_de(Garmin)
class ApiConActividades:
    def __init__(self, crudas):
        self.crudas = crudas
        self.pedido = None

    def get_activities_by_date(self, desde, hasta, *a, **kw):  # noqa: ANN001
        self.pedido = (desde, hasta)
        return self.crudas


def test_las_actividades_en_crudo_salen_sin_tocar():
    """La caché guarda ESTO y no los `Ride` ya parseados.

    El día que `ride_from_activity` aprenda a leer un campo nuevo, un histórico
    de objetos normalizados no lo tendría: habría que volver a bajar 180 días a
    una API que limita por IP. Guardando el diccionario tal cual, ese campo ya
    está en disco esperando. Por eso este test comprueba identidad y no forma.
    """
    crudas = [
        actividad(activityId=1),
        actividad(activityId=2, activityType={"typeKey": "running"}),
    ]
    c = garmin.GarminClient(email="a@b.c", password="x", token_dir="/tmp")
    c._api = ApiConActividades(crudas)

    salida = c.raw_activities(date(2026, 9, 1), date(2026, 9, 7))

    assert salida == crudas, "se ha normalizado o filtrado algo por el camino"
    assert any(a["activityType"]["typeKey"] == "running" for a in salida), (
        "la caché tiene que guardar TODO, no solo lo que hoy sabemos leer"
    )
    # Garmin quiere las fechas en ISO, no objetos `date`.
    assert c._api.pedido == ("2026-09-01", "2026-09-07")


def test_sin_actividades_se_devuelve_una_lista_y_no_None():
    """Un `None` colándose hasta `save_cache` vaciaría la caché entera."""
    c = garmin.GarminClient(email="a@b.c", password="x", token_dir="/tmp")
    c._api = ApiConActividades(None)
    assert c.raw_activities(date(2026, 9, 1), date(2026, 9, 7)) == []


@doble_de(Garmin)
class ApiQueFalla:
    """Un API de Garmin en el que todo revienta menos las pulsaciones."""

    def get_hrv_data(self, *_):
        raise RuntimeError("500 del servidor")

    def get_stats(self, *_):
        return {"restingHeartRate": 52}

    def get_sleep_data(self, *_):
        raise RuntimeError("respuesta vacía")

    def get_body_battery(self, *_):
        raise RuntimeError("timeout")


BB_FIXTURE = Path(__file__).parent / "fixtures" / "garmin_body_battery.json"

#: Tres respuestas REALES de `get_body_battery`, tal cual las devolvió Garmin,
#: guardadas junto al valor que el código viejo sacaba de cada una. Están aquí
#: por lo que costó descubrir: el doble que había antes en `ApiSana` devolvía
#: `[{"bodyBatteryValuesArray": [[0, 70]]}]`, un solo punto y sin los sellos de
#: tiempo que trae la respuesta de verdad. Contra ese doble, leer el primer
#: punto de la serie y leer el pico de la mañana dan lo mismo -70-, así que el
#: fallo que tenía la columna seis meses era INVISIBLE para esta suite. Un doble
#: más simple que el original no es un doble: es otro sistema.
BB_DIAS: dict = json.loads(BB_FIXTURE.read_text(encoding="utf-8"))

#: El día normal: te acuestas con 47, recargas hasta 91 a las 5:24 y bajas.
BB_DIA_NORMAL = "2026-09-15"
#: El día de la siesta: el máximo del día (49) cae a las 18:07, la mañana es 33.
BB_DIA_SIESTA = "2026-05-22"
#: Uno de los 48 días en que el reloj no midió: seis puntos, los seis a `null`.
BB_DIA_SIN_MEDIR = "2026-03-15"


def bb_respuesta(iso: str) -> list:
    """La respuesta real de ese día, copiada para que nadie la mute."""
    return copy.deepcopy(BB_DIAS[iso]["respuesta"])


@doble_de(Garmin)
class ApiSana:
    """El mismo API cuando todo va bien. Es la base de casi todos los tests.

    El body battery que devuelve es la respuesta REAL del 2026-09-15, con sus
    seis puntos y sus dos sellos de tiempo, no una maqueta. Por eso el valor
    limpio de este API es 91 -el pico de la recarga nocturna- y no un número
    redondo elegido a mano.
    """

    def get_hrv_data(self, *_):
        return {"hrvSummary": {"lastNightAvg": 60}}

    def get_stats(self, *_):
        return {"restingHeartRate": 52}

    def get_sleep_data(self, *_):
        return {"dailySleepDTO": {"sleepTimeSeconds": 25200,
                                  "sleepScores": {"overall": {"value": 80}}}}

    def get_body_battery(self, *_):
        return bb_respuesta(BB_DIA_NORMAL)


def cliente(api) -> "garmin.GarminClient":
    c = garmin.GarminClient(email="a@b.c", password="x", token_dir="/tmp")
    c._api = api
    return c


def cliente_con_api_rota() -> "garmin.GarminClient":
    return cliente(ApiQueFalla())


def test_un_fallo_de_lectura_no_tumba_el_dia_pero_queda_anotado():
    c = cliente_con_api_rota()
    m = c.day_metrics(date(2026, 9, 7))

    # Lo que sí llegó, llega.
    assert m.rhr == 52.0
    # Lo que no, se queda en None como siempre...
    assert m.hrv is None and m.sleep_min is None and m.body_battery is None
    # ...pero ahora hay constancia de POR QUÉ está en None.
    assert len(c.fetch_errors) == 3


def test_el_apunte_dice_qué_día_y_qué_métrica():
    c = cliente_con_api_rota()
    c.day_metrics(date(2026, 9, 7))
    texto = " | ".join(c.fetch_errors)
    assert "2026-09-07" in texto
    assert "hrv" in texto
    assert "500 del servidor" in texto, "el error original hay que conservarlo"


def test_los_apuntes_se_acumulan_entre_dias():
    """El informe cuenta lecturas fallidas de toda la ventana, no de una."""
    c = cliente_con_api_rota()
    for i in range(1, 8):
        c.day_metrics(date(2026, 9, i))
    assert len(c.fetch_errors) == 21


def test_un_dia_limpio_no_deja_apuntes():
    c = cliente(ApiSana())
    m = c.day_metrics(date(2026, 9, 7))
    assert m.hrv == 60.0 and m.sleep_min == 420 and m.body_battery == 91
    assert c.fetch_errors == []


# ---------------------------------------------------------------------------
# Un 200 al que le falta el campo
# ---------------------------------------------------------------------------
#
# El agujero era más fino que el anterior y bastante peor. Los cuatro `try`
# solo se enteran de lo que lanza una excepción, y una respuesta correcta a la
# que le falta el campo que buscamos NO lanza nada: `.get()` devuelve None y el
# día sigue como si esa noche no se hubiera medido.
#
# Lo que hace grave a este caso es su forma. Un 500 falla un día; un campo
# renombrado falla TODOS los días a partir de ese, y sin síntoma: la línea base
# se queda sin puntos suficientes, `adaptive_thresholds` deja de existir y el
# sistema decide con las constantes de reserva durante semanas.


def sin_clave(base: type, metodo: str, valor):
    """Un API sano al que se le cambia UNA respuesta."""
    api = base()
    setattr(api, metodo, lambda *_: valor)
    return cliente(api)


def test_un_hrv_sin_lastNightAvg_no_pasa_por_una_noche_sin_medir():
    c = sin_clave(ApiSana, "get_hrv_data", {"hrvSummary": {"lastNightAverage": 60}})
    m = c.day_metrics(date(2026, 9, 7))
    assert m.hrv is None
    assert len(c.fetch_errors) == 1
    assert "lastNightAvg" in c.fetch_errors[0]


def test_una_noche_sin_HRV_de_verdad_sigue_sin_dejar_apunte():
    """La otra mitad, y sin ella el aviso no valdría nada.

    Un reloj en la mesilla es un caso normalísimo y tiene que seguir siendo
    `hrv=None` en silencio. Si cada noche sin medir dejara un apunte, la lista
    se llenaría de ruido y dejaría de mirarse justo el día que dijera algo.
    """
    for vacio in ({}, None, {"hrvSummary": {}}, {"hrvSummary": None}):
        c = sin_clave(ApiSana, "get_hrv_data", vacio)
        assert c.day_metrics(date(2026, 9, 7)).hrv is None
        assert c.fetch_errors == [], f"con {vacio!r} no hay nada que avisar"


def test_una_clave_presente_con_valor_nulo_es_un_dato_y_no_un_fallo():
    """Garmin manda `"restingHeartRate": null` los días que no lo mide.

    Eso es una respuesta, no un silencio: la clave está donde tiene que estar.
    """
    c = sin_clave(ApiSana, "get_stats", {"restingHeartRate": None})
    assert c.day_metrics(date(2026, 9, 7)).rhr is None
    assert c.fetch_errors == []


def test_unos_stats_sin_la_clave_de_pulsaciones_sí_avisan():
    c = sin_clave(ApiSana, "get_stats", {"totalSteps": 8000})
    m = c.day_metrics(date(2026, 9, 7))
    assert m.rhr is None
    assert len(c.fetch_errors) == 1
    assert "restingHeartRate" in c.fetch_errors[0]


def test_un_sueño_sin_sleepTimeSeconds_avisa():
    c = sin_clave(ApiSana, "get_sleep_data", {"dailySleepDTO": {"sleepStartTimestampGMT": 1}})
    m = c.day_metrics(date(2026, 9, 7))
    assert m.sleep_min is None
    assert any("sleepTimeSeconds" in e for e in c.fetch_errors)


def test_una_noche_sin_puntuación_no_avisa_porque_pasa_de_verdad():
    """`sleepScores` falta en las siestas y en las noches que Garmin no puntúa.

    Es el único nivel que se lee con `.get` a propósito: avisar aquí sería
    generar ruido diario por un caso normal.
    """
    c = sin_clave(
        ApiSana, "get_sleep_data", {"dailySleepDTO": {"sleepTimeSeconds": 25200}}
    )
    m = c.day_metrics(date(2026, 9, 7))
    assert m.sleep_min == 420 and m.sleep_score is None
    assert c.fetch_errors == []


def test_una_serie_de_body_battery_que_ya_no_son_pares_avisa():
    """Antes, un cambio de formato aquí era un None más entre los normales."""
    c = sin_clave(ApiSana, "get_body_battery", [{"bodyBatteryValuesArray": [[70]]}])
    m = c.day_metrics(date(2026, 9, 7))
    assert m.body_battery is None
    assert any("pares" in e for e in c.fetch_errors)


def test_un_día_sin_body_battery_no_avisa():
    for vacio in ([], None, [{}]):
        c = sin_clave(ApiSana, "get_body_battery", vacio)
        assert c.day_metrics(date(2026, 9, 7)).body_battery is None
        assert c.fetch_errors == [], f"con {vacio!r} no hay nada que avisar"


def test_el_apunte_del_campo_que_falta_explica_que_no_falla_solo_hoy():
    """El texto es la mitad del arreglo.

    Un "no se pudo leer hrv" se lee como un día malo y se ignora. Lo que hay
    que poder entender de un vistazo es que a partir de hoy no se va a leer
    NINGUNO, que es lo que convierte un aviso en algo que se atiende.
    """
    c = sin_clave(ApiSana, "get_hrv_data", {"hrvSummary": {"otroNombre": 60}})
    c.day_metrics(date(2026, 9, 7))
    aviso = c.fetch_errors[0]
    assert "todos los días" in aviso
    assert "hrvSummary" in aviso, "sin la ruta no se sabe dónde mirar"


# ---------------------------------------------------------------------------
# readiness: el interruptor con cable, la toma sin corriente, y el final
# ---------------------------------------------------------------------------
#
# Historia en cuatro actos. De los tres primeros quedaron tests; del cuarto
# queda uno solo, el de abajo, y merece la pena contar el camino porque lo que
# enseña no es sobre readiness: es sobre cuándo dejar de arreglar algo.
#
# 1. `DayMetrics.readiness`, `daily_metrics.readiness` y
#    `sig.values["readiness"]` estaban declarados y no los llenaba nadie.
#    Interruptor sin cable: la columna a NULL todos los días y nadie enterándose.
# 2. Se conectó el cable. Y entonces se midió, que es lo que no se había hecho
#    nunca: `get_training_readiness` devuelve `[]` para esta cuenta TODOS los
#    días, incluido ayer. Sondeados -1, -2, -3 y de -3 a -175: nueve de nueve.
#    La calcula el reloj, no el servidor, y este reloj no la calcula. O sea que
#    el cable llevaba a una toma sin corriente, y el `if tr:` se tragaba la
#    lista vacía exactamente igual de callado que el acto 1.
# 3. Se apagó la llamada con un interruptor (`wellness.fetch_readiness: false`)
#    y se le puso voz al vacío. Aquí es donde se paró demasiado pronto: quedó un
#    ajuste cuya única posición útil era "apagado", una columna que no iba a
#    tener un dato nunca, y `not_requested`, que existía solo para que su
#    ausencia no marcara `partial` todas las filas.
# 4. Se borró entero. La llamada, el campo, la columna, el interruptor y
#    `not_requested`. Cuatro piezas sosteniendo a una quinta que no medía nada.
#
# La diferencia entre el acto 1 y el 2 no se ve desde dentro del código: en los
# dos la columna acaba a NULL. Solo se ve preguntándole a Garmin de verdad. Por
# eso `scripts/sondeo_wellness.py` se queda en el repositorio.


def test_no_hay_quinta_llamada():
    """Son cuatro peticiones por día, y que sean cuatro es el test.

    Esto vigila las dos formas de que vuelva la quinta: que alguien la llame
    otra vez -el `pytest.fail` salta- y que alguien vuelva a inventarse un hueco
    que no cuenta como hueco. Un día completo son los cinco campos que hay y
    cero apuntes; en cuanto haya una sexta métrica sin dato detrás, vuelven las
    filas `partial` permanentes que, al no reintentarse, se quedan calladas.

    En el backfill importaba el doble: cuatro llamadas por día y ciento setenta
    y nueve días son setecientas dieciséis peticiones contra un servicio que
    corta por IP. La quinta habría sido casi doscientas más para traer `[]`.
    """
    api = ApiSana()
    api.get_training_readiness = lambda *_: pytest.fail(
        "se ha vuelto a pedir training readiness: este reloj no la calcula"
    )
    m = cliente(api).day_metrics(date(2026, 9, 7))

    assert (m.hrv, m.rhr, m.sleep_min, m.sleep_score, m.body_battery) == (
        60.0, 52.0, 420, 80, 91
    )
    assert set(m.raw) == {"hrv", "stats", "sleep", "body_battery"}
    assert not hasattr(m, "readiness"), "la columna se fue: el campo también"
    assert not hasattr(m, "not_requested"), (
        "`not_requested` era el parche que sostenía a readiness y se fue con ella"
    )


def test_un_429_sigue_propagandose_y_no_se_queda_en_un_apunte():
    """`fetch_errors` es para los fallos que se pueden absorber. Un 429 no lo
    es: si se anotara aquí, el día se decidiría con datos a medias."""
    @doble_de(Garmin)
    class ApiLimitada:
        def get_hrv_data(self, *_):
            raise RuntimeError("429 Too Many Requests")

    c = garmin.GarminClient(email="a@b.c", password="x", token_dir="/tmp")
    c._api = ApiLimitada()
    with pytest.raises(GarminRateLimited):
        c.day_metrics(date(2026, 9, 7))


# ---------------------------------------------------------------------------
# La sesión: reanudada, nueva, o no se sabe
# ---------------------------------------------------------------------------
#
# POR QUÉ ESTO TIENE BATERÍA PROPIA
# ---------------------------------------------------------------------------
# `session_resumed` es el sitio donde se mira para decidir si repetir la llamada
# es gratis o cuesta un intento de login. Durante meses dijo `True` -"sesión
# reanudada, no gasta intentos"- mientras por debajo la librería se comía dos
# 429 seguidos, porque lo único que comprobaba era que `login()` no reventara.
# `Garmin.login(tokenstore)` vuelve normalmente tanto si reanuda como si hace
# login entero con credenciales, así que "no ha reventado" y "no ha hecho login"
# eran la misma frase para dos cosas distintas. El de siempre: el valor que se
# lee no es el valor que se usa.


@doble_de(Garmin)
class _InternoFalso:
    """El cliente de dentro de `Garmin`. Su `login` es el que cuesta dinero."""

    def __init__(self):
        self.veces = 0

    def login(self, *a, **k):
        self.veces += 1
        return (None, None)


@doble_de(
    Garmin,
    # `dump` no está en `Garmin`: está en `Garmin.garth`, que es otro objeto,
    # de la librería `garth`. Este doble se apunta a sí mismo en `self.garth`
    # para no tener que fabricar dos clases, así que necesita el método encima
    # para que `api.garth.dump(ruta)` -la llamada que hace `garmin.py`- funcione.
    salvo=("dump",),
)
class _GarminFalso:
    """Imita las tres salidas reales de `Garmin.login(tokenstore)`.

    `hace_login` decide si el camino simulado es el de reanudar (no toca el
    login interno) o el de credenciales (lo llama). Los dos vuelven sin
    excepción, que es justo lo que hacía indistinguibles a los dos casos.
    """

    def __init__(self, *, hace_login: bool, escribe=None):
        self.client = _InternoFalso()
        self._hace_login = hace_login
        self._escribe = escribe
        self.garth = self

    def login(self, tokenstore=None):
        if self._hace_login:
            self.client.login("u", "p")
            if self._escribe is not None:
                self._escribe()
        return (None, None)

    def dump(self, ruta):  # api.garth.dump
        if self._escribe is not None:
            self._escribe()


def _con_garmin_falso(monkeypatch, falso):
    """`connect()` hace `from garminconnect import Garmin` por dentro."""
    import sys
    import types

    modulo = types.ModuleType("garminconnect")
    modulo.Garmin = lambda email, password: falso
    monkeypatch.setitem(sys.modules, "garminconnect", modulo)


def test_si_no_se_llama_al_login_interno_la_sesion_se_reanudo(monkeypatch, tmp_path):
    falso = _GarminFalso(hace_login=False)
    _con_garmin_falso(monkeypatch, falso)
    c = garmin.GarminClient(email="a@b.c", password="x", token_dir=str(tmp_path))
    c.connect()

    assert c.session_resumed is True
    assert falso.client.veces == 0
    # No hubo login, así que no había nada que guardar: afirmar sobre los tokens
    # aquí sería inventarse una comprobación que no se ha hecho.
    assert c.tokens_guardados is None


def test_un_login_con_credenciales_no_se_disfraza_de_sesion_reanudada(
    monkeypatch, tmp_path
):
    """El fallo de 2026-09-13, clavado.

    La librería carga los tokens, la API los rechaza, y hace login entero por
    dentro. `login()` vuelve normalmente. Antes eso se leía como "reanudada".
    """
    falso = _GarminFalso(
        hace_login=True,
        escribe=lambda: (tmp_path / "garmin_tokens.json").write_text("{}"),
    )
    _con_garmin_falso(monkeypatch, falso)
    c = garmin.GarminClient(email="a@b.c", password="x", token_dir=str(tmp_path))
    c.connect()

    assert c.session_resumed is False, (
        "hubo login con credenciales y se ha dicho que se reanudó la sesión: "
        "es exactamente la mentira que invita a repetir la llamada hasta el 429"
    )
    assert falso.client.veces == 1


def test_un_login_que_no_deja_tokens_escritos_lo_dice(monkeypatch, tmp_path):
    """La avería cara: funciona siempre porque no guarda nunca.

    `Garmin.login()` guarda los tokens dentro de un `contextlib.suppress`, así
    que un directorio sin permiso de escritura devuelve un login perfectamente
    correcto y vacío. Cada mañana repite el login entero y nadie se entera,
    porque cada ejecución por separado sale bien.
    """
    falso = _GarminFalso(hace_login=True, escribe=None)  # no escribe nada
    _con_garmin_falso(monkeypatch, falso)
    c = garmin.GarminClient(email="a@b.c", password="x", token_dir=str(tmp_path))
    c.connect()

    assert c.session_resumed is False
    assert c.tokens_guardados is False, (
        "el directorio quedó vacío después de un login y no se ha dicho"
    )


def test_los_tokens_se_cuentan_por_lo_que_haya_y_no_por_nombre_esperado(
    monkeypatch, tmp_path
):
    """Buscar `oauth1_token.json` habría dado un falso negativo.

    La versión instalada guarda UN solo fichero, `garmin_tokens.json`. Dar por
    hecha la pareja de ficheros de otra versión es el mismo error que dar por
    hecha la forma de una respuesta: se comprueba que haya algo, no que se
    llame como uno se imagina.
    """
    falso = _GarminFalso(
        hace_login=True,
        escribe=lambda: (tmp_path / "garmin_tokens.json").write_text("{}"),
    )
    _con_garmin_falso(monkeypatch, falso)
    c = garmin.GarminClient(email="a@b.c", password="x", token_dir=str(tmp_path))
    c.connect()

    assert c.tokens_guardados is True


def test_los_ficheros_ocultos_no_cuentan_como_tokens(monkeypatch, tmp_path):
    """Un `.gitkeep` no es una sesión guardada."""
    falso = _GarminFalso(
        hace_login=True,
        escribe=lambda: (tmp_path / ".gitkeep").write_text(""),
    )
    _con_garmin_falso(monkeypatch, falso)
    c = garmin.GarminClient(email="a@b.c", password="x", token_dir=str(tmp_path))
    c.connect()

    assert c.tokens_guardados is False


def test_un_directorio_de_tokens_ilegible_se_anota_pero_no_tumba_la_conexion(
    monkeypatch, tmp_path
):
    """No poder guardar la sesión encarece mañana; no impide leer hoy.

    Convertirlo en excepción cambiaría una degradación por una avería total y
    dejaría al motor sin wellness por un problema de permisos.
    """
    falso = _GarminFalso(hace_login=True, escribe=None)
    _con_garmin_falso(monkeypatch, falso)
    c = garmin.GarminClient(
        email="a@b.c", password="x", token_dir=str(tmp_path / "no-existe")
    )
    c.connect()  # no debe reventar

    assert c.tokens_guardados is False
    assert c._api is falso, "la conexión tenía que quedar utilizable igualmente"


def test_si_no_se_puede_mirar_el_login_se_dice_que_no_se_sabe(monkeypatch, tmp_path):
    """Tercer estado. La librería cambió por dentro y no hay dónde engancharse.

    Suponer lo cómodo aquí es elegir entre dos mentiras. `None` dice la verdad:
    no se sabe.
    """
    @doble_de(
        Garmin,
        # `client` y `garth` no están en la CLASE `Garmin`: los crea su
        # `__init__`, así que `hasattr(Garmin, "client")` es False aunque un
        # `Garmin` de verdad los tenga siempre. Aquí se ponen a `None` a
        # propósito, porque el caso que este test simula es justamente el de
        # una sesión de la que no se puede mirar si hubo login.
        salvo=("client", "garth"),
    )
    class SinCliente:
        client = None
        garth = None

        def login(self, tokenstore=None):
            return (None, None)

    falso = SinCliente()
    _con_garmin_falso(monkeypatch, falso)
    c = garmin.GarminClient(email="a@b.c", password="x", token_dir=str(tmp_path))
    c.connect()

    assert c.session_resumed is None


def test_el_no_se_sabe_es_falsy_para_que_lo_de_siempre_degrade_al_lado_seguro():
    """`None` tiene que caer del lado prudente.

    Hay código -`app/cli.py`, el informe- que pregunta `if client.session_resumed`
    sin distinguir los tres estados. Con `None` falsy, ese código deja de
    afirmar que no hubo login, que es el lado que no hace daño. Si `None` fuera
    truthy, "no se sabe" se leería como "reanudada" y volveríamos al 429.
    """
    assert not None
    c = garmin.GarminClient(email="a@b.c", password="x", token_dir="/tmp")
    c.session_resumed = None
    assert not c.session_resumed


# ---------------------------------------------------------------------------
# Body battery: qué punto de la serie es "el de la mañana"
# ---------------------------------------------------------------------------
#
# La columna `body_battery` llevaba seis meses guardando el PRIMER punto de la
# serie con un comentario encima que decía que se guardaba el de la mañana. El
# primer punto es la medianoche: con cuánta batería te acuestas, no con cuánta
# te levantas. Media guardada 35,0 sobre una media real de 82,7 en los 138 días
# medidos, y los 138 coincidiendo con el primer punto y NINGUNO con el pico.
#
# Lo que hizo el fallo invisible durante seis meses no fue la falta de tests:
# era que el doble de `get_body_battery` devolvía UN punto sin sellos de tiempo,
# y con un solo punto el primero y el máximo son el mismo número. Estos tests
# van contra respuestas reales guardadas en `tests/fixtures/`, y cada uno se ha
# comprobado volviendo a poner `niveles[0][1]` para ver que falla.


def _leer(respuesta) -> tuple[int | None, list[str]]:
    """El valor que acaba en la fila, por el camino de producción entero."""
    c = sin_clave(ApiSana, "get_body_battery", respuesta)
    return c.day_metrics(date(2026, 9, 7)).body_battery, c.fetch_errors


def _con_huso(respuesta: list, horas: int) -> list:
    """La misma respuesta declarando otro huso, sin tocar la serie.

    Solo se mueve `startTimestampLocal`, que es lo único que el código mira
    para saber qué hora local es cada instante.
    """
    bloque = respuesta[0]
    gmt = datetime.strptime(bloque["startTimestampGMT"][:19], "%Y-%m-%dT%H:%M:%S")
    bloque["startTimestampLocal"] = (gmt + timedelta(hours=horas)).strftime(
        "%Y-%m-%dT%H:%M:%S.0"
    )
    return respuesta


def test_el_fixture_es_la_respuesta_real_y_trae_lo_que_el_codigo_lee():
    """Sin esto, media docena de tests de aquí abajo pasarían en vacío.

    Todos recorren `BB_DIAS`. Si el fichero se quedara sin días, o si una
    respuesta perdiera la serie, los bucles darían cero vueltas y el fichero
    entero seguiría en verde diciendo que la columna está bien.
    """
    assert set(BB_DIAS) == {BB_DIA_NORMAL, BB_DIA_SIESTA, BB_DIA_SIN_MEDIR}
    for iso, dia in BB_DIAS.items():
        bloque = dia["respuesta"][0]
        assert bloque["bodyBatteryValuesArray"], f"{iso} sin serie"
        assert bloque["startTimestampGMT"] and bloque["startTimestampLocal"], (
            f"{iso} sin los dos sellos de tiempo: son de donde sale el huso"
        )
        assert "guardado_por_el_codigo_viejo" in dia, (
            f"{iso} sin el valor que guardaba el código viejo, que es la mitad "
            "de lo que este fixture demuestra"
        )


def test_la_serie_real_viene_en_seis_puntos_y_no_por_minutos():
    """Es el porqué de leer el máximo en vez de "la muestra tras despertar".

    Si Garmin devolviera la serie por minutos, lo natural sería coger el valor
    del instante en que acaba el sueño. Devuelve SEIS puntos en veinticuatro
    horas, así que "la muestra siguiente al despertar" cae de media dos horas
    después, con la batería ya gastada: sale una media de 72,6 en vez de 82,7,
    y el error es siempre hacia abajo. El día que esto cambie -que la serie
    venga fina- este test se pondrá rojo y habrá que rehacer la decisión.
    """
    for iso, dia in BB_DIAS.items():
        serie = dia["respuesta"][0]["bodyBatteryValuesArray"]
        assert len(serie) == 6, f"{iso}: {len(serie)} puntos, ya no son seis"
    serie = BB_DIAS[BB_DIA_NORMAL]["respuesta"][0]["bodyBatteryValuesArray"]
    horas = (serie[-1][0] - serie[0][0]) / 3_600_000
    assert horas > 8, (
        f"seis puntos repartidos en {horas:.1f} h: entre dos muestras cabe "
        "media mañana, que es justo por lo que no se lee 'la de después'"
    )


def test_se_lee_el_pico_de_la_noche_y_no_el_valor_de_la_medianoche():
    """El test que no existía. Es el fallo entero en dos números.

    47 es con lo que se acostó y 91 con lo que se levantó. La respuesta es la
    misma; lo que cambia es qué punto se lee. Contra el doble viejo -un punto
    suelto, sin sellos- las dos lecturas daban 70 y este test no era posible.
    """
    valor, errores = _leer(bb_respuesta(BB_DIA_NORMAL))
    assert valor == 91, "el pico de la recarga nocturna, a las 05:24 locales"
    assert BB_DIAS[BB_DIA_NORMAL]["guardado_por_el_codigo_viejo"] == 47, (
        "lo que la columna guardó ese día: el primer punto, la medianoche"
    )
    assert errores == [], "un día normal y completo no tiene nada que avisar"


def test_una_siesta_por_la_tarde_no_es_el_valor_de_la_mañana():
    """Por esto el corte está al mediodía y no se coge el máximo del día.

    El 2026-05-22 la noche fue mala -se sube de 5 a 33- y por la tarde hay una
    recarga que llega a 49 a las 18:06. El máximo del día diría 49, que es más
    alto que la mañana y no describe ninguna mañana. Es un día de 138, pero es
    la diferencia entre una definición y una casualidad.
    """
    respuesta = bb_respuesta(BB_DIA_SIESTA)
    valores = [p[1] for p in respuesta[0]["bodyBatteryValuesArray"] if p[1] is not None]
    assert max(valores) == 49, "el máximo del día entero, que es de la tarde"

    valor, errores = _leer(respuesta)
    assert valor == 33, "el máximo hasta el mediodía"
    assert errores == []


def test_el_corte_de_la_mañana_no_es_un_numero_suelto_en_el_codigo():
    """`HORA_TOPE_MANANA` tiene que ser lo que de verdad recorta.

    Si alguien lo cambia a 24 "para simplificar", el día de la siesta empieza a
    valer 49 y nadie se entera. El nombre existe para que se pueda mover a
    propósito, no para decorar.
    """
    assert HORA_TOPE_MANANA == 12
    subidos = garmin.HORA_TOPE_MANANA
    try:
        garmin.HORA_TOPE_MANANA = 24
        assert _leer(bb_respuesta(BB_DIA_SIESTA))[0] == 49, (
            "con el tope en 24 tiene que entrar la siesta: si no, el corte no "
            "lo hace esta constante y este test no vigila nada"
        )
    finally:
        garmin.HORA_TOPE_MANANA = subidos
    assert _leer(bb_respuesta(BB_DIA_SIESTA))[0] == 33


def test_un_dia_que_el_reloj_no_midio_sigue_siendo_none_y_no_un_cero():
    """48 días del histórico traen los seis puntos con los seis valores a null.

    Es una noche sin reloj, y tiene que seguir siendo "no se sabe". Un 0 sería
    una batería vacía, que es un dato clínico y falso; y no hay nada que avisar,
    porque dormir sin reloj no es un fallo de lectura.
    """
    respuesta = bb_respuesta(BB_DIA_SIN_MEDIR)
    assert all(p[1] is None for p in respuesta[0]["bodyBatteryValuesArray"])

    valor, errores = _leer(respuesta)
    assert valor is None
    assert errores == []


def test_el_huso_sale_de_la_respuesta_y_no_de_una_constante():
    """Y se nota, porque el día de la siesta cambia de valor si se falla.

    En UTC, la muestra de las 13:54 locales cae a las 11:54 y se cuela en la
    mañana: 42 en vez de 33. Ese es el test: no que el huso se lea, sino que
    leerlo mal dé otro número. En el histórico hay 14 días a +1 y 171 a +2, así
    que un `+2` escrito a mano estaría mal dos semanas al año.
    """
    assert _leer(_con_huso(bb_respuesta(BB_DIA_SIESTA), 0))[0] == 42, (
        "si esto ya daba 33, el huso no se estaría aplicando a nada"
    )
    assert _leer(bb_respuesta(BB_DIA_SIESTA))[0] == 33


def test_en_horario_de_invierno_el_pico_se_sigue_leyendo_bien():
    """Día INVENTADO, y hay que decirlo: no hay ninguno real que sirva.

    Los 14 días a +1 del histórico son exactamente los 14 primeros de la racha
    de 48 sin medir, así que ningún día de invierno tiene pico que leer. En vez
    de fingir que este fixture es real, se coge el día normal y se le declara
    huso de invierno: el pico se mueve de las 05:24 a las 04:24 y tiene que
    seguir siendo 91.
    """
    invierno = BB_DIAS[BB_DIA_SIN_MEDIR]["respuesta"][0]
    assert all(
        p[1] is None for p in invierno["bodyBatteryValuesArray"]
    ), "el día real a +1 del fixture tiene datos: entonces úsalo y quita este apaño"

    valor, errores = _leer(_con_huso(bb_respuesta(BB_DIA_NORMAL), 1))
    assert valor == 91
    assert errores == []


def test_sin_los_sellos_de_tiempo_no_se_adivina_la_hora_y_se_anota():
    """Sin huso no se sabe cuál de los seis puntos es de la mañana.

    Coger uno a ojo sería volver a lo de antes con otra cara. Se devuelve None
    -que ya significa "no se sabe"- y se deja apunte, porque si estos dos
    campos desaparecen es que la respuesta ha cambiado de forma y eso se arregla
    una vez, no se sufre seis meses.
    """
    for quitar in ("startTimestampLocal", "startTimestampGMT"):
        respuesta = bb_respuesta(BB_DIA_NORMAL)
        respuesta[0].pop(quitar)
        valor, errores = _leer(respuesta)
        assert valor is None, f"sin {quitar} no hay forma honrada de dar un número"
        assert len(errores) == 1 and "hora local" in errores[0], (
            f"sin {quitar} el aviso tiene que decir qué falta: {errores}"
        )


def test_un_sello_de_tiempo_ilegible_tampoco_se_interpreta():
    """Si el formato cambia, `strptime` revienta, y eso no puede tumbar el día.

    El resto de la fila -HRV, pulsaciones, sueño- es buena. Se pierde una
    columna y se anota; no se pierde el día.
    """
    respuesta = bb_respuesta(BB_DIA_NORMAL)
    respuesta[0]["startTimestampLocal"] = "15/09/2026 00:00"
    c = sin_clave(ApiSana, "get_body_battery", respuesta)
    m = c.day_metrics(date(2026, 9, 7))

    assert m.body_battery is None
    assert m.hrv == 60.0 and m.rhr == 52.0, "el resto de la fila se salva"
    assert len(c.fetch_errors) == 1


def test_un_dia_que_solo_tiene_muestras_de_tarde_no_inventa_una_mañana():
    """Un reloj que se pone a mediodía. No hay mañana que leer: es None.

    Y NO es un aviso: la respuesta vino bien y con la forma de siempre, solo que
    no cubre la franja que nos interesa. Avisar aquí sería ruido diario por un
    caso normal, que es la misma línea que se sigue con `sleepScores`.
    """
    respuesta = bb_respuesta(BB_DIA_SIESTA)
    serie = respuesta[0]["bodyBatteryValuesArray"]
    respuesta[0]["bodyBatteryValuesArray"] = serie[3:]
    assert respuesta[0]["bodyBatteryValuesArray"], "si se queda vacía esto no prueba nada"

    valor, errores = _leer(respuesta)
    assert valor is None
    assert errores == []


def test_un_punto_suelto_sin_instante_no_arrastra_al_resto_del_dia():
    """Garmin manda algún `[null, 60]` suelto. Se salta ese punto, no el día."""
    respuesta = bb_respuesta(BB_DIA_NORMAL)
    respuesta[0]["bodyBatteryValuesArray"].insert(0, [None, 12])

    valor, errores = _leer(respuesta)
    assert valor == 91
    assert errores == []
