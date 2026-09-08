"""Smoke test de bike_advisor contra el config real.

Recorre sábado y domingo x semáforo x escenarios de historial, e imprime el
nivel resultante y la cadena de recortes. No es el test definitivo: es la
comprobación de que la cadena se comporta como dice el docstring.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from app.config_loader import load_config
from app.engine.bike_advisor import recommend_bike
from app.engine.signals import ClassifiedRide, IntensityBudget, Ride, Signals

CFG = load_config(Path(__file__).resolve().parents[1] / "config.yaml")

SAT = date(2026, 9, 5)  # sábado
SUN = date(2026, 9, 6)  # domingo


def make_signals(
    day: date,
    rides: list[ClassifiedRide] | None = None,
    yesterday_level: str | None = None,
    budget: IntensityBudget | None = None,
) -> Signals:
    return Signals(
        day=day,
        values={"yesterday_ride_level": yesterday_level},
        history={},
        adaptive={},
        notes=[],
        rides=rides or [],
        weekend=None,
        budget=budget,
    )


def show(title: str, sig: Signals, light: str) -> None:
    r = recommend_bike(CFG, sig, light)
    chain = " -> ".join(f"{a}>{b} ({why})" for a, b, why in r.downgrades) or "sin recortes"
    print(f"\n{title}")
    print(f"  luz={light:5s} baseline={r.baseline:9s} final={r.level}")
    print(f"  cadena: {chain}")
    print(f"  texto : {r.text() or '(no aplica: ' + str(r.skip_reason) + ')'}")


def ride(d: date, level: str) -> ClassifiedRide:
    return ClassifiedRide(
        ride=Ride(date=d, duration_s=5400),
        level=level,
        source="test",
        load=100.0,
        load_estimated=False,
    )


print("=" * 78)
print("A. Sin historial: baseline puro recortado solo por el semáforo")
print("=" * 78)
for day, name in ((SAT, "sábado"), (SUN, "domingo")):
    for light in ("green", "amber", "red"):
        show(f"{name} limpio", make_signals(day), light)

print()
print("=" * 78)
print("B. Ayer se hizo una intensa (no dos seguidas)")
print("=" * 78)
show(
    "domingo, sábado intenso",
    make_signals(SUN, rides=[ride(SAT, "intensa")], yesterday_level="intensa"),
    "green",
)

print()
print("=" * 78)
print("C. Presupuesto semanal agotado")
print("=" * 78)
show(
    "sábado, presupuesto 2/2",
    make_signals(SAT, budget=IntensityBudget(limit=2, used=2, detail=["test"], week_start=SAT - timedelta(days=5))),
    "green",
)
show(
    "sábado, presupuesto 1/2",
    make_signals(SAT, budget=IntensityBudget(limit=2, used=1, detail=["test"], week_start=SAT - timedelta(days=5))),
    "green",
)

print()
print("=" * 78)
print("D. Ya hay una intensa este fin de semana (ejecutada, no recomendada)")
print("=" * 78)
show(
    "domingo, sábado intenso pero yesterday_ride_level ausente",
    make_signals(SUN, rides=[ride(SAT, "intensa")]),
    "green",
)
show(
    "domingo, sábado SUAVE (la recomendación del sábado no cuenta)",
    make_signals(SUN, rides=[ride(SAT, "suave")]),
    "green",
)

print()
print("=" * 78)
print("E. Intensa exige verde")
print("=" * 78)
show("sábado ámbar", make_signals(SAT), "amber")

print()
print("=" * 78)
print("F. Día que no es de finde")
print("=" * 78)
show("miércoles", make_signals(date(2026, 9, 2)), "green")

print()
print("=" * 78)
print("G. El finde ANTERIOR no debe contaminar (ride de hace 8 días)")
print("=" * 78)
show(
    "sábado, intensa hace 8 días",
    make_signals(SAT, rides=[ride(SAT - timedelta(days=8), "intensa")]),
    "green",
)
