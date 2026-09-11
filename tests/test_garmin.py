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

from datetime import date

import pytest

from app.integrations import garmin
from app.integrations.garmin import (
    MAX_RETRIES,
    GarminError,
    GarminRateLimited,
    _is_rate_limit,
    _retry,
    ride_from_activity,
    rides_from_activities,
)


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
    se puede leer. Una salida real que desaparece resta carga de `load_3d/7d`
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


class SettingsFalsos:
    garmin_email = ""
    garmin_password = ""
    garmin_token_dir = "/tmp/tokens"


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

    def get_training_readiness(self, *_):
        raise RuntimeError("504")


class ApiSana:
    """El mismo API cuando todo va bien. Es la base de casi todos los tests."""

    def get_hrv_data(self, *_):
        return {"hrvSummary": {"lastNightAvg": 60}}

    def get_stats(self, *_):
        return {"restingHeartRate": 52}

    def get_sleep_data(self, *_):
        return {"dailySleepDTO": {"sleepTimeSeconds": 25200,
                                  "sleepScores": {"overall": {"value": 80}}}}

    def get_body_battery(self, *_):
        return [{"bodyBatteryValuesArray": [[0, 70]]}]

    def get_training_readiness(self, *_):
        return [{"score": 74, "level": "HIGH"}]


def cliente(api, *, readiness: bool = False) -> "garmin.GarminClient":
    """El cliente por defecto es el de PRODUCCIÓN, o sea sin readiness.

    Podría haberse dejado encendido aquí para no tocar los tests que contaban
    cuatro o cinco apuntes, y habría sido la decisión equivocada: el ajuste que
    de verdad corre todos los días habría quedado sin cubrir, y los que sí
    corren serían los del camino que no se toma. `readiness=True` lo piden
    explícitamente los pocos tests que van sobre esa llamada.
    """
    c = garmin.GarminClient(
        email="a@b.c", password="x", token_dir="/tmp", fetch_readiness=readiness
    )
    c._api = api
    return c


def cliente_con_api_rota() -> "garmin.GarminClient":
    return cliente(ApiQueFalla(), readiness=True)


def test_un_fallo_de_lectura_no_tumba_el_dia_pero_queda_anotado():
    c = cliente_con_api_rota()
    m = c.day_metrics(date(2026, 9, 7))

    # Lo que sí llegó, llega.
    assert m.rhr == 52.0
    # Lo que no, se queda en None como siempre...
    assert m.hrv is None and m.sleep_min is None and m.body_battery is None
    # ...pero ahora hay constancia de POR QUÉ está en None.
    assert len(c.fetch_errors) == 4


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
    assert len(c.fetch_errors) == 28


def test_un_dia_limpio_no_deja_apuntes():
    c = cliente(ApiSana())
    m = c.day_metrics(date(2026, 9, 7))
    assert m.hrv == 60.0 and m.sleep_min == 420 and m.body_battery == 70
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


def sin_clave(base: type, metodo: str, valor, *, readiness: bool = False):
    """Un API sano al que se le cambia UNA respuesta."""
    api = base()
    setattr(api, metodo, lambda *_: valor)
    return cliente(api, readiness=readiness)


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
# readiness: el interruptor con cable, y la toma sin corriente
# ---------------------------------------------------------------------------
#
# Historia en tres actos, porque los tres dejan tests distintos.
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
# 3. Se apaga la llamada (`wellness.fetch_readiness: false`) y se le pone voz al
#    vacío. Apagada no se pide y no cuenta como hueco; encendida, una respuesta
#    vacía lo dice en el informe.
#
# La diferencia entre el acto 1 y el 2 no se ve desde dentro del código: en los
# dos la columna acaba a NULL. Solo se ve preguntándole a Garmin de verdad. Por
# eso `scripts/sondeo_wellness.py` se queda en el repositorio.


def test_con_readiness_encendido_se_lee_y_llega_a_DayMetrics():
    m = cliente(ApiSana(), readiness=True).day_metrics(date(2026, 9, 7))
    assert m.readiness == 74
    assert m.not_requested == ()


def test_apagado_no_se_pide_siquiera():
    """Que no se llame es el punto: es una petición al día contra un límite."""
    api = ApiSana()
    api.get_training_readiness = lambda *_: pytest.fail(
        "se ha pedido readiness con el interruptor apagado"
    )
    m = cliente(api).day_metrics(date(2026, 9, 7))
    assert m.readiness is None
    assert m.hrv == 60.0, "apagar la quinta llamada no puede tocar las otras"


def test_apagado_lo_dice_para_que_no_cuente_como_hueco():
    """Sin esto, TODAS las filas quedarían `partial` y la marca no diría nada.

    Es la diferencia entre "no se pidió" y "se pidió y no vino". La segunda es
    una avería que se puede reintentar; la primera es una decisión.
    """
    m = cliente(ApiSana()).day_metrics(date(2026, 9, 7))
    assert m.not_requested == ("readiness",)
    assert "readiness" not in (m.raw or {}), "no se pidió: no puede haber crudo"


def test_encendido_una_respuesta_vacía_ya_no_se_traga_en_silencio():
    """El fallo del acto 2, y el que costó meses de columna vacía.

    `[]` es una respuesta correcta de Garmin, así que ningún `try` se entera; y
    con el `if tr:` delante tampoco se enteraba nadie más. Un `readiness=None`
    por lista vacía era idéntico a un `readiness=None` porque no existía la
    llamada.
    """
    c = sin_clave(ApiSana, "get_training_readiness", [], readiness=True)
    assert c.day_metrics(date(2026, 9, 7)).readiness is None
    assert len(c.fetch_errors) == 1
    assert "wellness.fetch_readiness" in c.fetch_errors[0], (
        "el apunte tiene que decir qué hacer, no solo que algo vino vacío"
    )


def test_un_readiness_sin_score_avisa_como_los_demás():
    c = sin_clave(ApiSana, "get_training_readiness", [{"level": "HIGH"}], readiness=True)
    m = c.day_metrics(date(2026, 9, 7))
    assert m.readiness is None
    assert any("score" in e for e in c.fetch_errors)


def test_que_falle_el_readiness_no_se_lleva_por_delante_el_resto_del_día():
    """Es la métrica que menos decide, y no puede tumbar la lectura de HRV."""
    api = ApiSana()
    api.get_training_readiness = lambda *_: (_ for _ in ()).throw(RuntimeError("404"))
    c = cliente(api, readiness=True)
    m = c.day_metrics(date(2026, 9, 7))
    assert m.hrv == 60.0 and m.rhr == 52.0
    assert m.readiness is None
    assert len(c.fetch_errors) == 1 and "readiness" in c.fetch_errors[0]


def test_un_429_sigue_propagandose_y_no_se_queda_en_un_apunte():
    """`fetch_errors` es para los fallos que se pueden absorber. Un 429 no lo
    es: si se anotara aquí, el día se decidiría con datos a medias."""
    class ApiLimitada:
        def get_hrv_data(self, *_):
            raise RuntimeError("429 Too Many Requests")

    c = garmin.GarminClient(email="a@b.c", password="x", token_dir="/tmp")
    c._api = ApiLimitada()
    with pytest.raises(GarminRateLimited):
        c.day_metrics(date(2026, 9, 7))
