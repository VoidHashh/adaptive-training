"""Caché en disco de actividades.

Regla de fondo: la caché es una MEJORA del histórico, no un requisito para
decidir. Por eso `load_cached_rides` nunca lanza —devuelve el fallo en `error`
y el sistema sigue con lo que Garmin le dé hoy— y por eso la caché nunca pisa
un dato fresco.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from app.engine.signals import Ride
from app.integrations.activity_cache import (
    CachedActivities,
    load_cached_rides,
    merge_rides,
    refresh_cache,
    save_cache,
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


# ---------------------------------------------------------------------------
# Escritura
# ---------------------------------------------------------------------------
#
# El fallo que se vigila aquí no da error: el refresco diario trae una ventana
# corta, y si escribiera encima en vez de mezclar, el histórico largo
# desaparecería sin que nadie viera nada. `load_cached_rides` no se queja de una
# caché corta, así que `load_3d_p90` pasaría a valer None y las reglas que lo
# usan dejarían de evaluarse en silencio.


def test_guardar_mezcla_en_vez_de_sobrescribir(tmp_path):
    f = tmp_path / "activities.json"
    viejas = [actividad(LUNES - timedelta(days=i), 1000 + i) for i in range(180)]
    save_cache(f, viejas)

    # El refresco diario: una ventana de tres días.
    frescas = [actividad(LUNES - timedelta(days=i), 1000 + i) for i in range(3)]
    total = save_cache(f, frescas)

    assert total == 180, "la ventana corta se ha llevado por delante el histórico"
    c = load_cached_rides(f)
    assert c.first_day == LUNES - timedelta(days=179)


def test_una_actividad_repetida_no_se_duplica_y_gana_la_fresca(tmp_path):
    f = tmp_path / "activities.json"
    vieja = actividad(LUNES, 7)
    vieja["activityTrainingLoad"] = 100.0
    save_cache(f, [vieja])

    nueva = actividad(LUNES, 7)
    nueva["activityTrainingLoad"] = 250.0
    assert save_cache(f, [nueva]) == 1

    rides = load_cached_rides(f).rides
    assert len(rides) == 1
    assert rides[0].training_load == 250.0


def test_las_actividades_sin_id_no_se_pierden(tmp_path):
    """Sin id no se pueden deduplicar, pero descartarlas sería peor."""
    f = tmp_path / "activities.json"
    anonima = actividad(LUNES, 1)
    del anonima["activityId"]
    assert save_cache(f, [anonima, actividad(LUNES, 2)]) == 2


def test_una_cache_ilegible_se_reconstruye_avisando(tmp_path, caplog):
    """Se pierde el histórico, sí. Pero que conste por qué.

    Callarse aquí dejaría un sistema que un día empieza a calcular percentiles
    sobre tres días de datos sin que exista ni una línea que lo explique.
    """
    f = tmp_path / "activities.json"
    f.write_text("[{truncado", encoding="utf-8")

    with caplog.at_level("WARNING"):
        assert save_cache(f, [actividad(LUNES, 1)]) == 1
    assert "ilegible" in caplog.text
    assert load_cached_rides(f).available


def test_un_volcado_interrumpido_no_corrompe_la_cache_buena(tmp_path, monkeypatch):
    """Un `json.dump` a medias deja un fichero truncado.

    Y un JSON truncado no es un fichero que falta -eso se detecta y se dice-: es
    una caché que existe, que no parsea, y que deja el histórico en cero. Por eso
    se escribe al lado y se renombra: o está la versión vieja o está la nueva.

    Se simula la muerte del proceso a mitad del volcado escribiendo unos bytes y
    reventando después, que es exactamente la forma del fallo real.
    """
    import app.integrations.activity_cache as mod

    f = tmp_path / "activities.json"
    save_cache(f, [actividad(LUNES - timedelta(days=i), i) for i in range(50)])
    original = f.read_text(encoding="utf-8")

    def dump_a_medias(obj, fh, **kw):
        fh.write('[{"activityId": 1, "act')
        raise OSError("se acabó el disco a mitad de escribir")

    monkeypatch.setattr(mod.json, "dump", dump_a_medias)
    with pytest.raises(OSError):
        save_cache(f, [actividad(LUNES, 999)])

    assert f.read_text(encoding="utf-8") == original, (
        "la caché buena se ha corrompido a mitad de escritura"
    )
    monkeypatch.undo()
    assert len(load_cached_rides(f).rides) == 50


def test_refrescar_pide_la_ventana_larga_y_la_mezcla(tmp_path):
    f = tmp_path / "activities.json"
    save_cache(f, [actividad(LUNES - timedelta(days=300), 1)])

    pedido: list[tuple[date, date]] = []

    def fetch(desde: date, hasta: date) -> list[dict]:
        pedido.append((desde, hasta))
        return [actividad(LUNES, 2)]

    total = refresh_cache(f, LUNES, days=190, fetch=fetch)

    assert pedido == [(LUNES - timedelta(days=189), LUNES)]
    assert total == 2, "el refresco ha tirado la actividad de hace 300 días"


def test_refrescar_sin_novedades_no_borra_la_cache(tmp_path):
    """Garmin devolviendo vacío no es motivo para quedarse sin histórico."""
    f = tmp_path / "activities.json"
    save_cache(f, [actividad(LUNES - timedelta(days=i), i) for i in range(50)])
    assert refresh_cache(f, LUNES, fetch=lambda desde, hasta: None) == 50
