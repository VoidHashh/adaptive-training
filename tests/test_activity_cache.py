"""Caché en disco de actividades.

Regla de fondo: la caché es una MEJORA del histórico, no un requisito para
decidir. Por eso `load_cached_rides` nunca lanza —devuelve el fallo en `error`
y el sistema sigue con lo que Garmin le dé hoy— y por eso la caché nunca pisa
un dato fresco.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from app.engine.signals import Ride
from app.integrations.activity_cache import (
    CachedActivities,
    load_cached_rides,
    merge_rides,
)

from tests.conftest import LUNES


def actividad(dia: date, act_id: int, tipo: str = "cycling") -> dict:
    return {
        "activityId": act_id,
        "activityName": f"Salida {act_id}",
        "activityType": {"typeKey": tipo},
        "startTimeLocal": f"{dia.isoformat()} 18:00:00",
        "duration": 3600.0,
        "distance": 30000.0,
        "activityTrainingLoad": 100.0,
    }


# ---------------------------------------------------------------------------
# Lectura
# ---------------------------------------------------------------------------


def test_un_fichero_que_no_existe_no_revienta(tmp_path):
    c = load_cached_rides(tmp_path / "no_esta.json")
    assert not c.available
    assert "no existe" in c.error
    assert "no utilizable" in c.describe()


def test_un_json_corrupto_no_revienta(tmp_path):
    f = tmp_path / "activities.json"
    f.write_text("{esto no es json", encoding="utf-8")
    c = load_cached_rides(f)
    assert not c.available
    assert "no se pudo leer" in c.error


def test_un_json_que_no_es_una_lista_se_rechaza(tmp_path):
    f = tmp_path / "activities.json"
    f.write_text(json.dumps({"actividades": []}), encoding="utf-8")
    c = load_cached_rides(f)
    assert not c.available
    assert "no contiene una lista" in c.error


def test_se_leen_las_salidas_y_se_declara_el_rango(tmp_path):
    f = tmp_path / "activities.json"
    crudas = [actividad(LUNES - timedelta(days=i), i) for i in range(10)]
    crudas.append(actividad(LUNES, 99, tipo="running"))
    f.write_text(json.dumps(crudas), encoding="utf-8")

    c = load_cached_rides(f)
    assert c.available
    assert len(c.rides) == 10, "la carrera a pie no cuenta como salida"
    assert c.total_activities == 11, "pero sí cuenta en el total leído del fichero"
    assert c.first_day == LUNES - timedelta(days=9)
    assert c.last_day == LUNES

    texto = c.describe()
    assert "10 salidas en bici de 11 actividades" in texto
    assert "10 días" in texto


def test_una_cache_vacia_lo_dice(tmp_path):
    f = tmp_path / "activities.json"
    f.write_text("[]", encoding="utf-8")
    assert load_cached_rides(f).describe() == "caché de actividades vacía"


def test_la_cache_del_repositorio_si_existe_es_legible():
    """No falla si el fichero no está: no todos los clones lo tendrán."""
    from tests.conftest import REPO_ROOT

    ruta = REPO_ROOT / "data" / "cache" / "activities.json"
    if not ruta.is_file():
        return
    c = load_cached_rides(ruta)
    assert c.error is None
    assert c.available
    assert c.first_day is not None and c.last_day is not None
    assert c.last_day >= c.first_day


# ---------------------------------------------------------------------------
# Fusión
# ---------------------------------------------------------------------------


def r(dia: date, act_id: int | None = None, load: float = 100.0) -> Ride:
    return Ride(
        date=dia,
        duration_s=3600.0,
        distance_m=30000.0,
        training_load=load,
        activity_id=act_id,
    )


def test_el_dato_fresco_gana_al_cacheado():
    cache = [r(LUNES, 1, load=100)]
    fresco = [r(LUNES, 1, load=222)]
    fusion = merge_rides(cache, fresco)
    assert len(fusion) == 1
    assert fusion[0].training_load == 222


def test_dos_salidas_el_mismo_dia_son_dos_salidas():
    """Colapsarlas por fecha falsearía la carga del día."""
    fusion = merge_rides([r(LUNES, 1)], [r(LUNES, 2)])
    assert len(fusion) == 2


def test_las_salidas_sin_id_se_deduplican_por_forma():
    """Las sintéticas de las pruebas no tienen id, pero no deben duplicarse."""
    fusion = merge_rides([r(LUNES)], [r(LUNES)])
    assert len(fusion) == 1


def test_dos_salidas_sin_id_pero_distintas_no_se_funden():
    a = Ride(date=LUNES, duration_s=3600.0, distance_m=30000.0)
    b = Ride(date=LUNES, duration_s=7200.0, distance_m=60000.0)
    assert len(merge_rides([a], [b])) == 2


def test_la_fusion_sale_ordenada_por_fecha():
    cache = [r(LUNES - timedelta(days=i), i + 1) for i in range(5)]
    fresco = [r(LUNES + timedelta(days=1), 99)]
    fusion = merge_rides(cache, fresco)
    assert [x.date for x in fusion] == sorted(x.date for x in fusion)


def test_fusionar_con_nada_devuelve_lo_que_habia():
    cache = [r(LUNES, 1)]
    assert merge_rides(cache, []) == cache
    assert merge_rides([], cache) == cache
    assert merge_rides([], []) == []


def test_cached_activities_vacio_no_esta_disponible():
    assert not CachedActivities().available
