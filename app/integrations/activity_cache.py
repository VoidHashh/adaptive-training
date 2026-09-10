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


def _id_actividad(act: dict[str, Any]) -> Any:
    for k in ("activityId", "activity_id", "id"):
        if act.get(k) is not None:
            return act[k]
    return None


def save_cache(path: Path | str, frescas: Sequence[dict[str, Any]]) -> int:
    """Mezcla actividades nuevas con las que ya había y las escribe. Devuelve el total.

    DOS COSAS QUE NO PUEDE HACER, Y LAS DOS ROMPERÍAN EN SILENCIO:

    1. **No sobrescribe: mezcla.** La caché son 180 días de histórico y el
       refresco diario trae una ventana mucho más corta. Escribir directamente
       lo recién leído tiraría todo lo anterior, y como `load_cached_rides` no
       se queja de una caché corta, los percentiles adaptativos se quedarían sin
       base sin que nadie viera un error: simplemente `load_3d_p90` pasaría a
       valer None y las reglas que lo usan dejarían de evaluarse.

    2. **No escribe en el sitio: escribe al lado y renombra.** Si el proceso se
       muere a mitad de un `json.dump`, el fichero queda truncado. Y un JSON
       truncado no es un fichero que falta -eso se detecta-: es una caché que
       existe, que no se puede parsear, y que deja el histórico en cero. El
       renombrado es atómico, así que o está la versión vieja o está la nueva.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    previas: list[dict[str, Any]] = []
    if path.is_file():
        try:
            with path.open(encoding="utf-8") as fh:
                cargado = json.load(fh)
            if isinstance(cargado, list):
                previas = cargado
        except (OSError, json.JSONDecodeError) as exc:
            # Aquí sí se avisa fuerte: se está a punto de reemplazar una caché
            # ilegible, y si no se dice, el histórico se habrá reiniciado sin
            # que conste en ninguna parte por qué.
            log.warning(
                "caché de actividades ilegible (%s); se reconstruye desde cero "
                "y se pierde el histórico que hubiera dentro", exc
            )

    fusion: dict[Any, dict[str, Any]] = {}
    sin_id: list[dict[str, Any]] = []
    for act in [*previas, *frescas]:  # las frescas después: ganan ellas
        aid = _id_actividad(act)
        if aid is None:
            sin_id.append(act)
        else:
            fusion[aid] = act

    total = [*fusion.values(), *sin_id]
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(total, fh, ensure_ascii=False, default=str)
    tmp.replace(path)
    return len(total)


def refresh_cache(
    path: Path | str,
    day: date,
    *,
    days: int = 190,
    fetch: Any = None,
) -> int:
    """Trae las actividades recientes de Garmin y las mezcla con la caché.

    `fetch` se inyecta para poder probar esto sin red; por defecto usa Garmin.
    """
    from datetime import timedelta

    if fetch is None:
        from app.integrations.garmin import build_client
        from app.settings import settings

        def fetch(desde: date, hasta: date) -> list[dict[str, Any]]:  # noqa: ANN202
            c = build_client(settings)
            c.connect()
            return c.raw_activities(desde, hasta)

    frescas = fetch(day - timedelta(days=days - 1), day) or []
    return save_cache(path, frescas)


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
