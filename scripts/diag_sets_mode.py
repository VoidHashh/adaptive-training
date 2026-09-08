"""¿El modo `sets` funciona, o solo está bloqueado por la puerta de volumen?

Se desactivan los frenos de volumen y se dan 15 sesiones de dia_2 perfectas.
Si hip thrust / curl femoral / peso muerto siguen sin subir series, el problema
está en el código. Si suben, el problema está en el config.
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
from app.engine.session_builder import apply_progression
from app.engine.sets import warmup_flags
from app.engine.signals import Signals

CFG = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
RAW = copy.deepcopy(CFG.raw)
RAW["progression"]["volume_safety"]["enabled"] = False   # frenos fuera
SET_CFG = RAW["set_types"]
RK = "dia_2"
KEYS = [e["key"] for e in RAW["routines"][RK]["exercises"]]
POST = ["hip_thrust_barra", "curl_femoral_pie", "peso_muerto_smith"]

for k in POST:
    ex = next(e for e in RAW["routines"][RK]["exercises"] if e["key"] == k)
    print(f"  {k:<22} mode={ex.get('progression_type')} "
          f"clean_req={ex.get('clean_sessions_required', '(def)')} "
          f"max_sets={ex.get('max_sets')} sets={len(ex['sets'])}")

clean = dict.fromkeys(KEYS, 0)
waiting = dict.fromkeys(KEYS, 0)
print("\n15 sesiones de dia_2, todas verdes y todas limpias, sin frenos de volumen:")
for n in range(1, 16):
    day = date(2026, 9, 9) + timedelta(days=7 * n)
    plan = plan_progression(
        RAW, RK, Signals(day=day, values={"lower_discomfort": 0}, history={}), "green",
        compliance=dict.fromkeys(KEYS, True),
        clean_sessions=dict(clean),
        sessions_since_progress=dict(waiting),
    )
    ch = {c.key for c in plan.changes}
    desc = ", ".join(f"{c.key} {c.what}" for c in plan.changes) or "—"
    print(f"  s{n:>2}: {desc}")
    apply_progression(RAW["routines"][RK]["exercises"], plan, SET_CFG)
    for k in KEYS:
        clean[k] += 1
        waiting[k] = 0 if k in ch else waiting[k] + 1

print("\nestado final de la cadena posterior:")
for k in POST:
    ex = next(e for e in RAW["routines"][RK]["exercises"] if e["key"] == k)
    flags = warmup_flags(ex["sets"], SET_CFG, k)
    work = [s for s, f in zip(ex["sets"], flags, strict=True) if not f]
    print(f"  {k:<22} {len(work)} series efectivas  {[s.get('weight_kg') for s in work]}  "
          f"reps {[s.get('reps') for s in work]}")
