"""Cuántos sábados cambian de recomendación con el límite en 4 en vez de en 3.

Rejuega las 25 semanas reales de `data/cache/activities.json` contra el motor
de verdad -`recommend_bike`-, no contra una reconstrucción del razonamiento.
Para cada sábado con salidas en la semana:

  - se inyectan los 2 HIIT que el plan pide (lunes y jueves) como ejecutados,
  - se filtran las salidas a las ESTRICTAMENTE anteriores al sábado, porque la
    decisión se toma por la mañana y `_intense_rides_this_weekend` cuenta las
    salidas del fin de semana ya hechas: incluir la del propio sábado inventaría
    un recorte que el sistema no habría aplicado,
  - se pide la recomendación con el semáforo en verde, que es el caso en que el
    presupuesto es el único freno que puede actuar,
  - y se compara el nivel con `weekly_limit: 3` y con `weekly_limit: 4`.

Asumir los 2 HIIT hechos es el TECHO del efecto, no el caso medio:
`hiit.only_on_green` es True, así que las semanas con algún día en ámbar o rojo
habrían tenido menos HIIT y menos presupuesto gastado.
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
    Signals,
    StrengthSession,
    intensity_budget,
)
from app.integrations.activity_cache import load_cached_rides  # noqa: E402


def _semanas(rides):
    por_semana = defaultdict(list)
    for r in rides:
        lunes = r.date - timedelta(days=r.date.weekday())
        por_semana[lunes].append(r)
    return dict(sorted(por_semana.items()))


def _senales(cfg, day, rides_previas, sesiones):
    s = Signals(day=day)
    s.rides = list(rides_previas)
    s.budget = intensity_budget(rides_previas, sesiones, day, cfg.raw["cycling"])
    return s


def main() -> int:
    cfg = load_config("config.yaml")
    cache = load_cached_rides(Path("data/cache/activities.json"))
    if cache.error:
        print(f"cache ilegible: {cache.error}")
        return 1

    from app.engine.signals import classify_ride

    clasificadas = [classify_ride(r, cfg.raw["cycling"]) for r in cache.rides]
    print(f"{len(clasificadas)} salidas, {cache.first_day} -> {cache.last_day}")

    cfg3 = copy.deepcopy(cfg)
    cfg3.raw["cycling"]["recommendation"]["intensity_budget"]["weekly_limit"] = 3
    cfg4 = copy.deepcopy(cfg)
    cfg4.raw["cycling"]["recommendation"]["intensity_budget"]["weekly_limit"] = 4
    real = cfg.raw["cycling"]["recommendation"]["intensity_budget"]["weekly_limit"]
    print(f"weekly_limit real en config.yaml: {real}  (se simulan 3 y 4)")

    semanas = _semanas(clasificadas)
    cambios = []
    filas = []

    for lunes, _ in semanas.items():
        sabado = lunes + timedelta(days=5)
        # los 2 HIIT que el plan pide: lunes (dia_1) y jueves (dia_2)
        sesiones = [
            StrengthSession(date=lunes, routine_key="hiit_dia_1", is_hiit=True),
            StrengthSession(
                date=lunes + timedelta(days=3), routine_key="hiit_dia_2", is_hiit=True
            ),
        ]
        previas = [c for c in clasificadas if lunes <= c.ride.date < sabado]

        s3 = _senales(cfg3, sabado, previas, sesiones)
        s4 = _senales(cfg4, sabado, previas, sesiones)
        r3 = recommend_bike(cfg3, s3, "green")
        r4 = recommend_bike(cfg4, s4, "green")

        intensas_previas = sum(1 for c in previas if c.level == "intensa")
        filas.append(
            (
                sabado,
                intensas_previas,
                f"{s3.budget.used}/{s3.budget.limit}",
                r3.level,
                f"{s4.budget.used}/{s4.budget.limit}",
                r4.level,
            )
        )
        if r3.level != r4.level:
            cambios.append((sabado, intensas_previas, r3.level, r4.level))

    print()
    print(f"{'sabado':<12} {'int.L-V':>7}  {'lim3':>6} {'nivel3':<9} "
          f"{'lim4':>6} {'nivel4':<9}")
    for sab, ints, u3, n3, u4, n4 in filas:
        marca = "  <== CAMBIA" if n3 != n4 else ""
        print(f"{sab!s:<12} {ints:>7}  {u3:>6} {n3:<9} {u4:>6} {n4:<9}{marca}")

    print()
    print(f"semanas evaluadas: {len(filas)}")
    print(f"sabados que cambian de nivel al subir el limite: {len(cambios)}")
    for sab, ints, n3, n4 in cambios:
        print(f"  {sab} ({ints} intensa(s) L-V): {n3} -> {n4}")

    recortados3 = sum(1 for f in filas if f[3] != "intensa")
    recortados4 = sum(1 for f in filas if f[5] != "intensa")
    print()
    print(f"sabados SIN intensa con limite 3: {recortados3}/{len(filas)}")
    print(f"sabados SIN intensa con limite 4: {recortados4}/{len(filas)}")

    def _agotado(campo):
        usado, limite = campo.split("/")
        return int(usado) >= int(limite)

    print(f"semanas que agotan el limite 3: {sum(1 for f in filas if _agotado(f[2]))}")
    print(f"semanas que agotan el limite 4: {sum(1 for f in filas if _agotado(f[4]))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
