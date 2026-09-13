"""¿Cuántas salidas sin clasificar hacen falta ahora para recortar el sábado?

Y la pregunta del otro lado, que es la importante: con el límite en 4, ¿sigue
el presupuesto siendo un freno o se ha vuelto decorativo?

Tres medidas:

  1. El umbral de la duda. `indeterminate` se dispara cuando
     `used + unknown >= limit` sin que `used >= limit`. Con el suelo de 2 HIIT,
     eso se mide pidiéndole el recorte al motor de verdad, no a la aritmética.
  2. La altura a la que queda el techo. Sobre las 25 semanas reales, cuántas
     habrían llegado a 4/4 contando TODAS las salidas de la semana (incluida la
     del fin de semana, que es la que importa para el domingo).
  3. El domingo. El sábado se decide con el presupuesto a medias; el domingo se
     decide con la semana casi entera, y es donde el techo de 4 tiene alguna
     posibilidad de actuar.
"""

from __future__ import annotations

import copy
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config_loader import load_config  # noqa: E402
from app.engine.bike_advisor import recommend_bike  # noqa: E402
from app.engine.signals import (  # noqa: E402
    ClassifiedRide,
    Ride,
    Signals,
    StrengthSession,
    classify_ride,
    intensity_budget,
)
from app.integrations.activity_cache import load_cached_rides  # noqa: E402

RUTA = "cycling", "recommendation", "intensity_budget", "weekly_limit"


def _con_limite(cfg, limite):
    c = copy.deepcopy(cfg)
    c.raw["cycling"]["recommendation"]["intensity_budget"]["weekly_limit"] = limite
    return c


def _hiit(lunes, cuantos=2):
    dias = [(0, "hiit_dia_1"), (3, "hiit_dia_2")][:cuantos]
    return [
        StrengthSession(date=lunes + timedelta(days=d), routine_key=k, is_hiit=True)
        for d, k in dias
    ]


def _salida(day, level):
    return ClassifiedRide(
        ride=Ride(date=day, duration_s=5400),
        level=level,
        source="sim",
        load=90.0,
        load_estimated=False,
    )


def _senales(cfg, day, rides, sesiones):
    s = Signals(day=day)
    s.rides = list(rides)
    s.budget = intensity_budget(rides, sesiones, day, cfg.raw["cycling"])
    return s


def umbral_de_la_duda(cfg):
    from app.engine.signals import UNKNOWN

    print("=" * 72)
    print("1. UMBRAL DE LA DUDA: sabado en verde, 2 HIIT hechos")
    print("=" * 72)
    lunes = (
        __import__("datetime").date(2026, 9, 14)
    )
    sabado = lunes + timedelta(days=5)
    for limite in (3, 4):
        c = _con_limite(cfg, limite)
        print(f"\n  limite {limite}:")
        for intensas in (0, 1):
            for desconocidas in (0, 1, 2, 3):
                rides = [
                    _salida(lunes + timedelta(days=1 + i), "intensa")
                    for i in range(intensas)
                ] + [
                    _salida(lunes + timedelta(days=1 + intensas + i), UNKNOWN)
                    for i in range(desconocidas)
                ]
                s = _senales(c, sabado, rides, _hiit(lunes))
                r = recommend_bike(c, s, "green")
                b = s.budget
                estado = (
                    "AGOTADO"
                    if b.exhausted
                    else ("DUDA" if b.indeterminate else "margen")
                )
                marca = "  <-- recorta" if r.level != "intensa" else ""
                print(
                    f"    {intensas} intensa(s) + {desconocidas} sin clasificar:"
                    f"  {b.used}/{b.limit} (+{b.unknown}?) {estado:<8}"
                    f" -> {r.level}{marca}"
                )


def altura_del_techo(cfg, clasificadas):
    print()
    print("=" * 72)
    print("2. ALTURA DEL TECHO sobre las 25 semanas reales (semana completa)")
    print("=" * 72)
    por_semana = defaultdict(list)
    for c in clasificadas:
        por_semana[c.ride.date - timedelta(days=c.ride.date.weekday())].append(c)

    reparto = defaultdict(int)
    for lunes, rides in sorted(por_semana.items()):
        intensas = sum(1 for c in rides if c.level == "intensa")
        reparto[intensas] += 1

    total = sum(reparto.values())
    print(f"\n  salidas intensas por semana (n={total}):")
    for n in sorted(reparto):
        gasto = 2 + n  # los 2 HIIT que el plan pide
        print(
            f"    {n} intensa(s): {reparto[n]:>2} semanas"
            f"   -> gasto con 2 HIIT = {gasto}"
            f"   {'AGOTA 3' if gasto >= 3 else '':<8}"
            f"{'AGOTA 4' if gasto >= 4 else ''}"
        )
    agota3 = sum(v for k, v in reparto.items() if 2 + k >= 3)
    agota4 = sum(v for k, v in reparto.items() if 2 + k >= 4)
    print(f"\n  semanas que agotarian el limite 3: {agota3}/{total}")
    print(f"  semanas que agotarian el limite 4: {agota4}/{total}")


def el_domingo(cfg, clasificadas):
    print()
    print("=" * 72)
    print("3. EL DOMINGO: la semana casi entera ya gastada")
    print("=" * 72)
    por_semana = defaultdict(list)
    for c in clasificadas:
        por_semana[c.ride.date - timedelta(days=c.ride.date.weekday())].append(c)

    cambios = 0
    frenados3 = frenados4 = 0
    evaluadas = 0
    for lunes, _ in sorted(por_semana.items()):
        domingo = lunes + timedelta(days=6)
        previas = [c for c in clasificadas if lunes <= c.ride.date < domingo]
        evaluadas += 1
        niveles = {}
        for limite in (3, 4):
            c = _con_limite(cfg, limite)
            s = _senales(c, domingo, previas, _hiit(lunes))
            # el domingo hereda lo de ayer, que es lo que de verdad lo frena
            ayer = [p for p in previas if p.ride.date == lunes + timedelta(days=5)]
            if ayer:
                s.values["yesterday_ride_level"] = ayer[0].level
            niveles[limite] = (recommend_bike(c, s, "green").level, s.budget)
        n3, b3 = niveles[3]
        n4, b4 = niveles[4]
        if n3 != n4:
            cambios += 1
            print(
                f"  {domingo}: {b3.used}/{b3.limit} {n3}"
                f"  ->  {b4.used}/{b4.limit} {n4}   CAMBIA"
            )
        if n3 != "intensa":
            frenados3 += 1
        if n4 != "intensa":
            frenados4 += 1
    print(f"\n  domingos evaluados: {evaluadas}")
    print(f"  domingos que cambian de nivel: {cambios}")
    print(f"  domingos SIN intensa con limite 3: {frenados3}/{evaluadas}")
    print(f"  domingos SIN intensa con limite 4: {frenados4}/{evaluadas}")


def main() -> int:
    cfg = load_config("config.yaml")
    cache = load_cached_rides(Path("data/cache/activities.json"))
    clasificadas = [classify_ride(r, cfg.raw["cycling"]) for r in cache.rides]

    umbral_de_la_duda(cfg)
    altura_del_techo(cfg, clasificadas)
    el_domingo(cfg, clasificadas)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
