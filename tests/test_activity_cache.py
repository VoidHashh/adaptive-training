"""Caché en disco de actividades.

Regla de fondo: la caché es una MEJORA del histórico, no un requisito para
decidir. Por eso `load_cached_rides` nunca lanza —devuelve el fallo en `error`
y el sistema sigue con lo que Garmin le dé hoy— y por eso la caché nunca pisa
un dato fresco.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from app.engine.signals import Ride
from app.integrations.activity_cache import (
    CachedActivities,
    dias_adaptativos,
    load_cached_rides,
    merge_rides,
    refresh_cache,
    save_cache,
    ventana_de_salidas,
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
# caché corta, así que `load_2d_p90` pasaría a valer None y las reglas que lo
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


# ---------------------------------------------------------------------------
# Cuánto se le pide a Garmin cada mañana
# ---------------------------------------------------------------------------
#
# Esta sección existe por una opción que llevaba desde el principio declarada
# en `config.yaml` y que no leía nadie:
#
#     cycling:
#       fetch:
#         lookback_days: 10
#         backfill_days: 90
#
# El código pedía 190 días a pelo, con la constante repetida en `app/cli.py` y
# en `app/scheduler.py`. Y como `save_cache` FUSIONA y no poda nunca, a partir
# del segundo día esos 190 días eran una petición enorme para añadir, como
# mucho, la salida de ayer. Garmin limita por IP: un 429 en el trabajo de
# madrugada deja al de las 06:30 sin histórico y la mañana se decide a ciegas.
#
# Al conectar la opción aparece la otra mitad del problema, la que la
# constante tapaba: la ventana corta SOLO vale si la caché puede sostenerla.
# Las tres situaciones en las que no puede se prueban una a una, porque
# `load_cached_rides` no se queja de una caché corta —lo dice `save_cache` en
# su propio docstring— y ese silencio dejaría `load_2d_p90` en None y las dos
# reglas que lo usan sin evaluar, para siempre y sin un solo error.


CFG_FETCH = {
    "cycling": {"fetch": {"lookback_days": 10, "backfill_days": 90}},
    "adaptive_thresholds": {
        "load_2d_p90": {"window_days": 60, "percentile": 90},
        "load_7d_p90": {"window_days": 60, "percentile": 90},
    },
}


def cache_de(dias_cubiertos: int, *, escrita_hace: int = 0, day: date = LUNES):
    primero = day - timedelta(days=dias_cubiertos - 1)
    return CachedActivities(
        rides=[Ride(date=primero, duration_s=3600), Ride(date=day, duration_s=3600)],
        file_mtime=datetime.combine(day - timedelta(days=escrita_hace), datetime.min.time()),
        total_activities=2,
        first_day=primero,
        last_day=day,
    )


def test_con_la_cache_sana_solo_se_relee_la_ventana_corta():
    dias, motivo = ventana_de_salidas(CFG_FETCH, LUNES, cache_de(180))
    assert dias == 10
    assert "10" in motivo


def test_sin_cache_se_pide_el_historico_entero():
    """Primer arranque, y también el día que el fichero se pierda o se corrompa."""
    dias, motivo = ventana_de_salidas(CFG_FETCH, LUNES, CachedActivities(error="no existe"))
    assert dias == 90
    assert "no utilizable" in motivo or "no existe" in motivo


def test_sin_objeto_de_cache_tampoco_se_asume_que_hay_historico():
    dias, _ = ventana_de_salidas(CFG_FETCH, LUNES, None)
    assert dias == 90


def test_una_cache_mas_corta_que_el_percentil_dispara_el_backfill():
    """El fallo silencioso que la constante tapaba. Una caché de 20 días deja
    `load_2d_p90` en None: la regla no dispara, no falla, y el mensaje sale
    igual de bonito con una señal menos."""
    dias, motivo = ventana_de_salidas(CFG_FETCH, LUNES, cache_de(20))
    assert dias == 90
    assert "20" in motivo and "60" in motivo, "hay que decir cuánto falta y para qué"


def test_una_cache_justo_en_el_limite_vale():
    assert ventana_de_salidas(CFG_FETCH, LUNES, cache_de(60))[0] == 10


def test_una_cache_sin_refrescar_mas_dias_que_la_ventana_deja_un_agujero():
    """El contenedor ha estado parado tres semanas. La caché es larga y buena,
    pero entre lo último que tiene y hoy hay días que una ventana de 10 no
    alcanza, y `save_cache` fusiona: el hueco no se llenaría nunca solo."""
    dias, motivo = ventana_de_salidas(CFG_FETCH, LUNES, cache_de(180, escrita_hace=21))
    assert dias == 90
    assert "21" in motivo


def test_un_hueco_que_la_ventana_corta_si_alcanza_no_dispara_el_backfill():
    """El borde: escrita hace 9 días, y la ventana `[day-9, day]` llega."""
    assert ventana_de_salidas(CFG_FETCH, LUNES, cache_de(180, escrita_hace=9))[0] == 10


def test_no_salir_en_bici_no_cuenta_como_cache_desactualizada():
    """La frescura se mide por cuándo se ESCRIBIÓ la caché, no por la fecha de
    la última salida. Quien no monta en un mes tiene la caché al día; medirlo
    por `last_day` le haría descargar 90 días cada mañana sin que falte nada."""
    c = cache_de(180)
    c.last_day = LUNES - timedelta(days=30)
    assert ventana_de_salidas(CFG_FETCH, LUNES, c)[0] == 10


def test_las_ventanas_salen_del_config_y_no_de_una_constante():
    """La prueba de que la opción manda: se cambian las dos y el código las
    sigue. Con `RIDE_HISTORY_DAYS` esto devolvía 190 en los dos casos."""
    otro = {**CFG_FETCH, "cycling": {"fetch": {"lookback_days": 3, "backfill_days": 365}}}
    assert ventana_de_salidas(otro, LUNES, cache_de(180))[0] == 3
    assert ventana_de_salidas(otro, LUNES, None)[0] == 365


def test_los_umbrales_adaptativos_marcan_el_minimo_de_cache():
    """Subir `window_days` en el YAML tiene que mover esto. Si no, ampliar la
    ventana del percentil lo apagaría en vez de mejorarlo."""
    ancho = {**CFG_FETCH,
             "adaptive_thresholds": {"load_2d_p90": {"window_days": 120}}}
    assert dias_adaptativos(ancho) == 120
    assert ventana_de_salidas(ancho, LUNES, cache_de(90))[0] == 90, "90 < 120: backfill"
    assert ventana_de_salidas(CFG_FETCH, LUNES, cache_de(90))[0] == 10, "90 > 60: basta"


def test_la_ventana_de_la_bici_tambien_marca_el_minimo_de_cache():
    """`dias_adaptativos` solo miraba `adaptive_thresholds` mientras esa era la
    única sección que se calibraba contra el histórico. El punto de partida de
    la bici es la segunda, y su ventana de huecos entre intensas es más larga
    que cualquier percentil de carga.

    Lo que hace falta entender de este fallo es que NO daba error. Con la caché
    quedándose en los 60 días del percentil, `_baseline_gaps` habría encontrado
    los huecos que caben en 60 días en vez de los de 180, habría sacado unos
    percentiles más estrechos y habría recomendado con ellos tan tranquilo.
    Menos datos de los que existen no devuelve un fallo: devuelve otro número.
    """
    bici = {**CFG_FETCH,
            "cycling": {**CFG_FETCH.get("cycling", {}),
                        "recommendation": {"baseline_from_gaps": {"window_days": 180}}}}
    assert dias_adaptativos(bici) == 180
    assert ventana_de_salidas(bici, LUNES, cache_de(120))[0] != 10, (
        "una caché de 120 días no puede sostener una ventana de 180"
    )


def test_sin_la_seccion_en_el_yaml_se_usan_los_valores_declarados(cfg):
    """Los defectos del código son los mismos números que el YAML trae escritos.
    Un defecto distinto sería otra vez código y config diciendo cosas
    diferentes, con el agravante de que aquí no se vería.

    Y no se compara contra dos números copiados aquí a mano, porque así es como
    esto se quedó afirmando `90` mientras el `config.yaml` subía a 210 para
    poder sostener los 180 días de huecos de la bici: la prueba pasaba, y lo
    que decía su propio docstring había dejado de ser verdad. Se compara
    contra el YAML de verdad.
    """
    real = cfg.raw["cycling"]["fetch"]
    assert ventana_de_salidas({}, LUNES, cache_de(180))[0] == real["lookback_days"]
    assert ventana_de_salidas({}, LUNES, None)[0] == real["backfill_days"]


def test_el_borde_del_agujero_esta_en_la_ventana_justa():
    """Escrita hace exactamente `lookback_days` días. La ventana
    `[day-9, day]` NO incluye el día 10, así que ahí ya falta algo. Un `>` en
    vez de un `>=` dejaría ese día fuera para siempre, porque `save_cache`
    fusiona y nadie vuelve a mirar atrás."""
    assert ventana_de_salidas(CFG_FETCH, LUNES, cache_de(180, escrita_hace=10))[0] == 90
    assert ventana_de_salidas(CFG_FETCH, LUNES, cache_de(180, escrita_hace=9))[0] == 10
