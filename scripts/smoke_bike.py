"""Smoke test de bike_advisor contra el config real.

Recorre sábado y domingo x semáforo x escenarios de historial, e imprime el
nivel resultante, la cadena de recortes y las notas. No es el test definitivo:
es la comprobación de que la cadena se comporta como dice el docstring.

LO QUE ESTE GUIÓN TIENE QUE ENSEÑAR AHORA
-----------------------------------------
Antes servía para ver saltar los frenos. Ahora sirve para ver que NO saltan: la
columna de recortes tiene que estar vacía en todo lo que no sea el techo del
semáforo, y la de notas tiene que llevar el dato que antes justificaba el
recorte. Por eso se imprimen las dos por separado y con etiquetas distintas: si
alguna vez un hecho de contexto vuelve a aparecer en `recortes`, se ve de un
vistazo y sin leer una línea de código.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from app.config_loader import load_config
from app.engine.bike_advisor import recommend_bike
from app.engine.signals import ClassifiedRide, IntensityCount, Ride, Signals

CFG = load_config(Path(__file__).resolve().parents[1] / "config.yaml")

SAT = date(2026, 9, 5)  # sábado
SUN = date(2026, 9, 6)  # domingo


def make_signals(
    day: date,
    rides: list[ClassifiedRide] | None = None,
    yesterday_level: str | None = None,
    conteo: IntensityCount | None = None,
) -> Signals:
    return Signals(
        day=day,
        values={"yesterday_ride_level": yesterday_level},
        history={},
        adaptive={},
        notes=[],
        rides=rides or [],
        weekend=None,
        intense_count=conteo,
    )


def cuenta(used: int, unknown: int = 0) -> IntensityCount:
    return IntensityCount(
        used=used,
        detail=["test"],
        week_start=SAT - timedelta(days=5),
        unknown=unknown,
    )


def show(title: str, sig: Signals, light: str) -> None:
    r = recommend_bike(CFG, sig, light)
    chain = " -> ".join(f"{a}>{b} ({why})" for a, b, why in r.downgrades) or "NINGUNO"
    print(f"\n{title}")
    print(f"  luz={light:5s} baseline={r.baseline:9s} final={r.level}")
    print(f"  recortes (bajan el nivel): {chain}")
    for n in r.texto_notas():
        print(f"  nota     (no baja nada) : {n}")
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
print("B. Ayer se hizo una intensa: se dice, no se recorta")
print("=" * 78)
show(
    "domingo, sábado intenso",
    make_signals(SUN, rides=[ride(SAT, "intensa")], yesterday_level="intensa"),
    "green",
)

print()
print("=" * 78)
print("C. Recuento semanal alto: sigue saliendo 'intensa'")
print("=" * 78)
print("   (aquí antes había un 2/2 que bajaba el sábado a 'suave')")
for n in (0, 2, 4, 7, 12):
    show(f"sábado, {n} sesiones intensas esta semana", make_signals(SAT, conteo=cuenta(n)), "green")
show(
    "sábado, 2 contadas y 1 sin clasificar",
    make_signals(SAT, conteo=cuenta(2, unknown=1)),
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
print("E. El techo del semáforo, que es el único recorte que queda")
print("=" * 78)
show("sábado ámbar", make_signals(SAT), "amber")
show("sábado ámbar con 9 intensas esta semana", make_signals(SAT, conteo=cuenta(9)), "amber")

print()
print("=" * 78)
print("F. Día que no es de finde")
print("=" * 78)
show("miércoles", make_signals(date(2026, 9, 2)), "green")

print()
print("=" * 78)
print("G. El finde ANTERIOR no debe contaminar el recuento del finde")
print("=" * 78)
show(
    "sábado, intensa hace 8 días",
    make_signals(SAT, rides=[ride(SAT - timedelta(days=8), "intensa")]),
    "green",
)
show(
    "sábado, intensa el DOMINGO PASADO (hace 6 días: el caso del fallo)",
    make_signals(SAT, rides=[ride(SAT - timedelta(days=6), "intensa")]),
    "green",
)
