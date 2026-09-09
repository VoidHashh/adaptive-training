"""Series de calentamiento frente a series efectivas.

Un único sitio decide qué serie es calentamiento, porque la respuesta la
necesitan tres cálculos distintos que deben coincidir siempre: el cumplimiento
que abre la progresión, el volumen, y el recorte del -25% de los días ámbar.
Si cada uno lo resolviera por su cuenta, tarde o temprano discreparían.

Precedencia (`set_types.source`):

  api                 el campo `type` de Hevy manda y punto
  heuristic           se ignora la API, se aplica siempre la regla
  api_then_heuristic  manda la API; si NO marca ninguna serie del ejercicio
                      como calentamiento, se aplica la regla

Detalle importante de la rama `api_then_heuristic`: la ausencia de marcas no se
puede distinguir de "todas son efectivas de verdad" mirando solo el dato. Se
resuelve por ejercicio, no por rutina: si un ejercicio tiene alguna serie
marcada, se cree su marcado entero; si no tiene ninguna, se aplica la
heurística a ese ejercicio. Así, marcar un solo ejercicio en Hevy no cambia el
comportamiento de los demás.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

WARMUP = "warmup"
NORMAL = "normal"

SOURCE_API = "api"
SOURCE_HEURISTIC = "heuristic"
SOURCE_API_THEN_HEURISTIC = "api_then_heuristic"
VALID_SOURCES = {SOURCE_API, SOURCE_HEURISTIC, SOURCE_API_THEN_HEURISTIC}


def warmup_flags(
    sets: Sequence[dict[str, Any]],
    cfg: dict[str, Any],
    exercise_key: str | None = None,
) -> list[bool]:
    """Devuelve, para cada serie, si es de calentamiento.

    `cfg` es la sección `set_types` del YAML.
    """
    n = len(sets)
    if n == 0:
        return []

    if not cfg:
        # Sin sección `set_types` NO se aplica la heurística. El defecto
        # contrario -que es el que había- convertía en calentamiento la
        # primera serie de todo ejercicio de 4+ series sin que nadie lo
        # hubiera pedido, y eso mueve el cumplimiento, el recorte del ámbar
        # y la progresión de volumen a la vez.
        #
        # El validador además exige la sección, así que esto solo actúa si
        # alguien llama al motor con un dict a medias. Manda el marcado de
        # Hevy y punto.
        return [str(s.get("type") or NORMAL).lower() == WARMUP for s in sets]

    source = str(cfg.get("source", SOURCE_API_THEN_HEURISTIC))
    overrides = cfg.get("overrides") or {}

    # Un override desactiva la heurística para ese ejercicio, pero NO ignora un
    # marcado explícito: si la serie viene marcada en Hevy, se respeta.
    heuristic_allowed = overrides.get(exercise_key, True) if exercise_key else True

    from_api = [str(s.get("type") or NORMAL).lower() == WARMUP for s in sets]

    if source == SOURCE_API:
        return from_api
    if source == SOURCE_HEURISTIC:
        return _heuristic(n, cfg) if heuristic_allowed else [False] * n

    # api_then_heuristic
    if any(from_api):
        return from_api
    return _heuristic(n, cfg) if heuristic_allowed else [False] * n


def _heuristic(n: int, cfg: dict[str, Any]) -> list[bool]:
    h = cfg.get("heuristic") or {}
    if not h.get("enabled", True):
        return [False] * n
    threshold = int(h.get("sets_gte", 4))
    count = int(h.get("count", 1))
    if n < threshold:
        return [False] * n
    return [i < count for i in range(n)]


def split_sets(
    sets: Sequence[dict[str, Any]],
    cfg: dict[str, Any],
    exercise_key: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(calentamiento, efectivas), conservando el orden original."""
    flags = warmup_flags(sets, cfg, exercise_key)
    warm = [s for s, f in zip(sets, flags, strict=True) if f]
    work = [s for s, f in zip(sets, flags, strict=True) if not f]
    return warm, work


def effective_sets(
    sets: Sequence[dict[str, Any]],
    cfg: dict[str, Any],
    exercise_key: str | None = None,
) -> list[dict[str, Any]]:
    return split_sets(sets, cfg, exercise_key)[1]


def excludes(cfg: dict[str, Any], what: str) -> bool:
    """¿Se excluye el calentamiento de este cálculo?"""
    return bool((cfg.get("exclude_from") or {}).get(what, True))


def volume_kg(
    sets: Sequence[dict[str, Any]],
    cfg: dict[str, Any],
    exercise_key: str | None = None,
) -> float:
    """Volumen en kg (reps x peso), excluyendo el calentamiento si toca."""
    chosen = (
        effective_sets(sets, cfg, exercise_key)
        if excludes(cfg, "volume")
        else list(sets)
    )
    total = 0.0
    for s in chosen:
        reps = s.get("reps")
        kg = s.get("weight_kg")
        if reps and kg:
            total += float(reps) * float(kg)
    return total
