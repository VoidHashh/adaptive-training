"""Simula N sesiones seguidas de dia_2 y dia_3, todas verdes y limpias.

Comprueba lo que un test de una sola sesión no puede ver: que ningún ejercicio
se queda sin progresar para siempre, y en cuántas sesiones el volumen agota sus
techos y le devuelve el turno a la carga.
"""

from __future__ import annotations

import copy
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from app.config_loader import load_config
from app.engine.progression import plan_progression
from app.engine.sets import warmup_flags
from app.engine.signals import Signals

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT / "config.yaml")
SET_CFG = CFG.raw["set_types"]


def apply(exercise: dict, e) -> None:
    """Aplica la mutación decidida sobre el bloque de series."""
    sets = exercise["sets"]
    flags = warmup_flags(sets, SET_CFG, exercise.get("key"))
    work = [s for s, f in zip(sets, flags, strict=True) if not f]

    if e.add_sets and work:
        for _ in range(e.add_sets):
            work.append(copy.deepcopy(work[-1]))
    if e.new_reps is not None:
        for s in work:
            if s.get("reps"):
                s["reps"] = e.new_reps
    if e.new_duration_s is not None:
        for s in work:
            if s.get("duration_s"):
                s["duration_s"] = e.new_duration_s
    if e.weight_delta_kg is not None:
        if e.apply_to == "all_sets":
            for s in work:
                s["weight_kg"] = (s.get("weight_kg") or 0) + e.weight_delta_kg
        else:
            top = max(work, key=lambda s: s.get("weight_kg") or 0)
            top["weight_kg"] = (top.get("weight_kg") or 0) + e.weight_delta_kg

    warm = [s for s, f in zip(sets, flags, strict=True) if f]
    exercise["sets"] = warm + work


def simulate(routine_key: str, sessions: int = 30) -> None:
    raw = copy.deepcopy(CFG.raw)
    keys = [e["key"] for e in raw["routines"][routine_key]["exercises"]]
    clean = {k: 0 for k in keys}
    waiting = {k: 0 for k in keys}
    progressed: dict[str, int] = {k: 0 for k in keys}
    first_load_after_volume: int | None = None
    seen_volume = False

    print(f"\n{'=' * 78}\n{routine_key}: {sessions} sesiones verdes y limpias\n{'=' * 78}")

    for n in range(1, sessions + 1):
        day = date(2026, 9, 7) + timedelta(days=7 * n)
        s = Signals(day=day, values={}, history={})
        plan = plan_progression(
            raw, routine_key, s, "green",
            compliance={k: True for k in keys},
            clean_sessions=dict(clean),
            sessions_since_progress=dict(waiting),
        )
        kinds = {e.kind for e in plan.changes}
        if KIND := ("volume" in kinds):
            seen_volume = True
        if seen_volume and "load" in kinds and first_load_after_volume is None:
            first_load_after_volume = n

        changed_keys = {e.key for e in plan.changes}
        for e in plan.changes:
            progressed[e.key] += 1
            ex = next(x for x in raw["routines"][routine_key]["exercises"]
                      if x["key"] == e.key)
            apply(ex, e)

        for k in keys:
            clean[k] += 1
            waiting[k] = 0 if k in changed_keys else waiting[k] + 1

        if n <= 12 or n % 6 == 0:
            desc = ", ".join(f"{e.name.split('(')[0].strip()} {e.what}"
                             for e in plan.changes) or "—"
            print(f"  s{n:>2}: {desc}")

    print(f"\n  turno devuelto a la carga en la sesión: {first_load_after_volume}")
    never = [k for k, v in progressed.items() if v == 0]
    print(f"  ejercicios que NUNCA progresaron: {never or 'ninguno'}")
    for k in keys:
        ex = next(x for x in raw["routines"][routine_key]["exercises"] if x["key"] == k)
        print(f"    {progressed[k]:>2}x {k:<30} -> {ex['sets']}")


simulate("dia_2", 30)
simulate("dia_3", 30)
