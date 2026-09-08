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


def test_una_fecha_ilegible_descarta_la_actividad_en_vez_de_inventarla():
    assert ride_from_activity(actividad(startTimeLocal="ayer por la tarde")) is None


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


def test_sin_credenciales_no_se_construye_el_cliente():
    with pytest.raises(GarminError, match="GARMIN_EMAIL"):
        garmin.build_client(SettingsFalsos())


def test_el_cliente_sin_conectar_se_niega_a_leer():
    c = garmin.GarminClient(email="a@b.c", password="x", token_dir="/tmp")
    with pytest.raises(GarminError, match="no conectado"):
        c.day_metrics(date(2026, 9, 7))
    with pytest.raises(GarminError, match="no conectado"):
        c.rides(date(2026, 9, 1), date(2026, 9, 7))
