"""Caché en disco de actividades de Garmin, para el histórico largo.

POR QUÉ EXISTE
--------------
Los umbrales adaptativos (`load_3d_p90`, `load_7d_p90`) son percentiles sobre
la propia distribución histórica del usuario: exigen 30 días con dato dentro de
una ventana de 60. El wellness se consulta día a día —una petición por métrica y
por día—, así que ampliar esa ventana a 60 días serían cientos de peticiones y
un 429 seguro. Las actividades, en cambio, se piden por rango en una sola
llamada, y además ya estaban descargadas en `data/cache/activities.json` de un
análisis anterior: 180 días que el motor puede usar desde el primer arranque en
vez de esperar un mes a acumularlos.

DOS REGLAS QUE NO SE NEGOCIAN
-----------------------------
1. **La caché nunca pisa a un dato fresco.** Al fusionar, si una actividad está
   en las dos fuentes gana la de Garmin. El desempate es por `activity_id`, no
   por fecha: dos salidas el mismo día son dos salidas, y colapsarlas
   falsearía la carga del día.

2. **La caché se declara.** `load_cached_rides` devuelve, junto a las salidas,
   de cuándo es el fichero y qué rango cubre. Quien la use tiene que poder
   decirlo en voz alta: un percentil calculado sobre datos de hace tres meses
   sigue siendo válido, pero el usuario tiene derecho a saber que lo es.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence

from app.engine.signals import Ride
from app.integrations.garmin import rides_from_activities

log = logging.getLogger(__name__)


@dataclass
class CachedActivities:
    """Salidas leídas de disco, con su procedencia."""

    rides: list[Ride] = field(default_factory=list)
    path: Path | None = None
    file_mtime: datetime | None = None
    total_activities: int = 0
    first_day: date | None = None
    last_day: date | None = None
    error: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.rides)

    def describe(self) -> str:
        """Una línea para la cabecera del informe."""
        if self.error:
            return f"caché de actividades no utilizable: {self.error}"
        if not self.rides:
            return "caché de actividades vacía"
        span = ""
        if self.first_day and self.last_day:
            dias = (self.last_day - self.first_day).days + 1
            span = f", {self.first_day} → {self.last_day} ({dias} días)"
        return (
            f"{len(self.rides)} salidas en bici de {self.total_activities} "
            f"actividades cacheadas{span}"
        )


def load_cached_rides(path: Path | str) -> CachedActivities:
    """Lee el volcado de actividades. No lanza: devuelve el fallo en `error`.

    Que no lance es deliberado. Esta caché es una mejora del histórico, no un
    requisito para decidir: si el fichero no está o está corrupto, el sistema
    tiene que seguir funcionando con lo que Garmin le dé hoy, y limitarse a
    decir que los umbrales adaptativos se quedan sin base.
    """
    path = Path(path)
    if not path.is_file():
        return CachedActivities(path=path, error=f"no existe {path}")

    try:
        with path.open(encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return CachedActivities(path=path, error=f"no se pudo leer {path}: {exc}")

    if not isinstance(raw, list):
        return CachedActivities(
            path=path, error=f"{path} no contiene una lista de actividades"
        )

    rides = rides_from_activities(raw)
    dias = sorted(r.date for r in rides)
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        mtime = None

    return CachedActivities(
        rides=rides,
        path=path,
        file_mtime=mtime,
        total_activities=len(raw),
        first_day=dias[0] if dias else None,
        last_day=dias[-1] if dias else None,
    )


def merge_rides(cached: Sequence[Ride], fresh: Sequence[Ride]) -> list[Ride]:
    """Une caché y datos frescos. Ante el mismo `activity_id`, gana el fresco.

    Las salidas sin `activity_id` (las sintéticas de las pruebas) no se
    deduplican por id —no lo tienen— sino por la terna (fecha, duración,
    distancia). Es suficiente para no duplicar y no tan agresivo como para
    fundir dos salidas distintas del mismo día.
    """

    def clave(r: Ride) -> Any:
        if r.activity_id is not None:
            return ("id", r.activity_id)
        return ("shape", r.date, r.duration_s, r.distance_m)

    fusion: dict[Any, Ride] = {clave(r): r for r in cached}
    for r in fresh:  # después, para que sobrescriban
        fusion[clave(r)] = r
    return sorted(fusion.values(), key=lambda r: (r.date, r.activity_id or 0))
