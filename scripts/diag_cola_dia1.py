"""¿Bosu y gemelo no progresan por un techo, o porque nunca les llega el turno?"""

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
from app.engine.signals import Signals

RAW = copy.deepcopy(load_config(Path(__file__).resolve().parents[1] / "config.yaml").raw)
RAW["progression"]["volume_safety"]["enabled"] = False
SC = RAW["set_types"]
RK = "dia_1"
KEYS = [e["key"] for e in RAW["routines"][RK]["exercises"]]

clean = dict.fromkeys(KEYS, 0)
wait = dict.fromkeys(KEYS, 0)
print("dia_1, 12 sesiones verdes y limpias, SIN frenos de volumen:")
for n in range(1, 13):
    d = date(2026, 9, 7) + timedelta(days=7 * n)
    p = plan_progression(
        RAW, RK, Signals(day=d, values={"lower_discomfort": 0}, history={}), "green",
        compliance=dict.fromkeys(KEYS, True),
        clean_sessions=dict(clean),
        sessions_since_progress=dict(wait),
    )
    ch = {c.key for c in p.changes}
    desc = ", ".join(f"{c.key} {c.what}" for c in p.changes) or "—"
    print(f"  s{n:>2}: {desc}")
    apply_progression(RAW["routines"][RK]["exercises"], p, SC)
    for k in KEYS:
        clean[k] += 1
        wait[k] = 0 if k in ch else wait[k] + 1

print("\nestado final y motivo del último plan:")
last = {e.key: e for e in p.exercises}
for e in RAW["routines"][RK]["exercises"]:
    k = e["key"]
    st = [(s.get("weight_kg"), s.get("reps") or s.get("duration_s")) for s in e["sets"]]
    lp = last.get(k)
    why = ""
    if lp:
        why = lp.blocked_by or ("techo" if lp.at_ceiling else "")
        if lp.needs_data:
            why = "sin carga registrada"
    print(f"  {k:<30} {str(st):<46} {why}")
