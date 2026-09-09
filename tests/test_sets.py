"""Qué series cuentan como calentamiento.

Es una decisión pequeña con consecuencias grandes: marcar una serie como
calentamiento la saca del cumplimiento, del recorte del ámbar y de la
progresión de volumen a la vez. Por eso el defecto no puede ser inventarse
calentamientos.
"""

from __future__ import annotations

from app.engine.sets import split_sets, warmup_flags


def series(n: int, tipo: str | None = None) -> list[dict]:
    return [{"reps": 10, "type": tipo} for _ in range(n)]


def test_sin_seccion_set_types_no_se_inventa_ningun_calentamiento():
    """El defecto documentado era conservador y el real era el contrario.

    `Config.set_types` prometía "sin heurística", pero los tres llamantes del
    motor leían `raw.get("set_types", {})` y se saltaban esa promesa: con la
    sección ausente, `_heuristic` arrancaba con `enabled=True` y convertía la
    primera serie de todo ejercicio de 4+ series en calentamiento.
    """
    assert warmup_flags(series(4), {}) == [False] * 4
    assert warmup_flags(series(5), {}) == [False] * 5


def test_sin_seccion_set_types_manda_el_marcado_de_hevy():
    """Ignorar la heurística no es ignorar lo que el usuario marcó a mano."""
    sets = series(3) + series(1, "warmup")
    assert warmup_flags(sets, {}) == [False, False, False, True]


def test_con_la_heuristica_activa_la_primera_de_cuatro_es_calentamiento(cfg):
    cfg_sets = cfg.raw["set_types"]
    assert warmup_flags(series(4), cfg_sets) == [True, False, False, False]


def test_con_menos_series_del_umbral_no_se_toca_nada(cfg):
    assert warmup_flags(series(3), cfg.raw["set_types"]) == [False] * 3


def test_un_marcado_explicito_de_hevy_gana_a_la_heuristica(cfg):
    """`api_then_heuristic`: si hay algo marcado, se cree entero."""
    sets = series(3) + series(1, "warmup")
    assert warmup_flags(sets, cfg.raw["set_types"]) == [False, False, False, True]


def test_split_sets_conserva_el_orden_y_no_pierde_series(cfg):
    sets = series(4)
    warm, work = split_sets(sets, cfg.raw["set_types"])
    assert len(warm) + len(work) == 4
    assert warm + work != [] and len(work) == 3


def test_una_lista_vacia_no_revienta(cfg):
    assert warmup_flags([], cfg.raw["set_types"]) == []
