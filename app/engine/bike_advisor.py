"""Recomendación de bici para el fin de semana.

Cadena de recortes, en este orden y siempre el mismo:

  1. `baseline_by_weekday`          punto de partida (sábado intensa, domingo media)
  2. techo del semáforo             `actions.<luz>.bike_max`
  3. no dos intensas seguidas       si AYER se hizo una intensa
  4. presupuesto semanal            límite de sesiones intensas de la semana
  5. una sola intensa por finde     máximo del fin de semana
  6. solo intensa en verde          `require_green_for_intense`

Cada recorte se registra con su motivo. Eso es lo que permite que el mensaje de
Telegram diga "suave porque el sábado ya hiciste una intensa" en vez de un
"suave" a secas que no se puede discutir ni auditar.

Todos los recortes que miran al pasado usan lo que REALMENTE se hizo (las
actividades de Garmin ya clasificadas), no lo que se recomendó. Es una decisión
consciente: si el sábado se recomendó intensa y se salió a rodar suave, el
domingo no debe arrastrar un castigo por una intensidad que nunca ocurrió.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from app.engine.signals import (
    ClassifiedRide,
    IntensityBudget,
    Signals,
    previous_weekday,
)

DESCANSO = "descanso"


@dataclass
class BikeRecommendation:
    day_name: str
    level: str
    label: str
    detail: str
    duration_min: int
    duration_max: int
    baseline: str
    # Cada recorte aplicado: (nivel_antes, nivel_después, motivo).
    downgrades: list[tuple[str, str, str]] = field(default_factory=list)
    applies: bool = True
    skip_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "applies": self.applies,
            "skip_reason": self.skip_reason,
            "day": self.day_name,
            "level": self.level,
            "label": self.label,
            "detail": self.detail,
            "duration_min": self.duration_min,
            "duration_max": self.duration_max,
            "baseline": self.baseline,
            "downgrades": [
                {"from": a, "to": b, "why": why} for a, b, why in self.downgrades
            ],
        }

    def text(self) -> str:
        """Línea para el mensaje de Telegram."""
        if not self.applies:
            return ""
        rango = (
            f"{self.duration_min}-{self.duration_max} min"
            if self.duration_max > self.duration_min
            else f"{self.duration_max} min"
        )
        base = f"Bici: {self.label} ({rango}). {self.detail}"
        if self.downgrades:
            # Solo el motivo del último recorte: es el que manda. Los demás
            # quedan en el JSON de la decisión para quien quiera el detalle.
            base += f" [{self.downgrades[-1][2]}]"
        return base


def _cap(level: str, ceiling: str, order: list[str]) -> str:
    """Recorta `level` al techo `ceiling` según el orden de intensidad."""
    if ceiling not in order or level not in order:
        return level
    return level if order.index(level) <= order.index(ceiling) else ceiling


def recommend_bike(
    config: Any,
    signals: Signals,
    light: str,
) -> BikeRecommendation:
    raw = config.raw if hasattr(config, "raw") else config
    cycling = raw.get("cycling", {}) or {}
    rec = cycling.get("recommendation", {}) or {}
    actions = raw.get("actions", {}) or {}

    order: list[str] = list(rec.get("intensity_order") or [])
    types: dict[str, Any] = rec.get("types") or {}
    day_name = signals.weekday()

    def build(level: str, baseline: str, downs: list[tuple[str, str, str]]):
        t = types.get(level, {}) or {}
        return BikeRecommendation(
            day_name=day_name,
            level=level,
            label=str(t.get("label", level.capitalize())),
            detail=str(t.get("detail", "")),
            duration_min=int(t.get("duration_min", 0)),
            duration_max=int(t.get("duration_max", 0)),
            baseline=baseline,
            downgrades=downs,
        )

    if not rec.get("enabled", True):
        out = build(DESCANSO, DESCANSO, [])
        out.applies = False
        out.skip_reason = "la recomendación de bici está desactivada en el config"
        return out

    recommend_on = {str(d).lower() for d in (rec.get("recommend_on") or [])}
    if day_name not in recommend_on:
        out = build(DESCANSO, DESCANSO, [])
        out.applies = False
        out.skip_reason = f"hoy es {day_name}; solo se recomienda en {sorted(recommend_on)}"
        return out

    # --- 1. punto de partida ------------------------------------------------
    baseline = str((rec.get("baseline_by_weekday") or {}).get(day_name, "suave"))
    level = baseline
    downs: list[tuple[str, str, str]] = []

    def downgrade(new: str, why: str) -> None:
        nonlocal level
        if new != level and order.index(new) < order.index(level):
            downs.append((level, new, why))
            level = new

    # --- 2. techo del semáforo ---------------------------------------------
    ceiling = str((actions.get(light, {}) or {}).get("bike_max", "suave"))
    capped = _cap(level, ceiling, order)
    if capped != level:
        downgrade(capped, f"semáforo en {_light_es(light)}, techo {ceiling}")

    # --- 3. no dos intensas seguidas ---------------------------------------
    if rec.get("no_consecutive_intense", True):
        lookback = int(rec.get("lookback_days", 1))
        yesterday_level = signals.get("yesterday_ride_level")
        if yesterday_level == "intensa":
            target = str(rec.get("after_intense_downgrade_to", "suave"))
            when = "ayer" if lookback <= 1 else f"en los últimos {lookback} días"
            downgrade(target, f"{when} ya hiciste una salida intensa")

    # --- 4, 5 y 6: presupuesto de intensidad -------------------------------
    budget_cfg = rec.get("intensity_budget", {}) or {}
    if budget_cfg.get("enabled", True) and level == "intensa":
        budget: IntensityBudget | None = signals.budget

        if budget is not None and budget.exhausted:
            downgrade(
                str(budget_cfg.get("on_budget_exhausted", "suave")),
                f"presupuesto semanal agotado ({budget.used}/{budget.limit} sesiones intensas)",
            )

        max_weekend = int(budget_cfg.get("max_intense_rides_per_weekend", 1))
        done = _intense_rides_this_weekend(signals, cycling)
        if level == "intensa" and done >= max_weekend:
            downgrade(
                "suave",
                f"ya hay {done} salida(s) intensa(s) este fin de semana "
                f"(máximo {max_weekend})",
            )

        if level == "intensa" and budget_cfg.get("require_green_for_intense", True):
            if light != "green":
                downgrade("suave", f"una salida intensa exige semáforo verde y hoy está en {_light_es(light)}")

    return build(level, baseline, downs)


def _intense_rides_this_weekend(signals: Signals, cycling: dict[str, Any]) -> int:
    """Salidas intensas YA EJECUTADAS en el fin de semana en curso.

    Cuenta lo hecho, no lo recomendado: si el sábado se recomendó intensa y se
    acabó rodando suave, esa intensidad no se gastó y el domingo sigue
    disponible.
    """
    day_names = [
        str(d).lower() for d in ((cycling.get("weekend", {}) or {}).get("days") or [])
    ]
    if not day_names:
        return 0

    # Días del fin de semana EN CURSO (el que contiene hoy), no el anterior.
    days: list = []
    for name in day_names:
        if name == signals.weekday():
            days.append(signals.day)
        else:
            d = previous_weekday(signals.day, name)
            # Si ese día cae más de 6 atrás no es este fin de semana.
            if (signals.day - d) <= timedelta(days=6):
                days.append(d)

    rides: list[ClassifiedRide] = signals.rides or []
    # Estrictamente anteriores a hoy: la salida de hoy todavía no ha ocurrido
    # cuando se emite la recomendación por la mañana.
    return sum(
        1 for r in rides if r.date in days and r.date < signals.day and r.level == "intensa"
    )


def _light_es(light: str) -> str:
    return {"green": "verde", "amber": "ámbar", "red": "rojo"}.get(light, light)
