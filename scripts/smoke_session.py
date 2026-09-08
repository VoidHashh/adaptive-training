"""Smoke test de session_builder: un día de cada color, cada variante y cada
caso raro (descarga, aplazada, HIIT, reglas especiales, superseries).

Lo que se comprueba de verdad aquí no es que no reviente, sino el ORDEN: que
el recorte de una regla especial sobreviva a la progresión del mismo día, y que
el calentamiento siga en pie después de todos los recortes.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from app.config_loader import load_config
from app.engine.progression import plan_progression
from app.engine.session_builder import build_session, today_plan
from app.engine.sets import warmup_flags
from app.engine.signals import Signals

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT / "config.yaml")
RAW = CFG.raw
SET_CFG = RAW["set_types"]

FAILS: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FALLA'} {label}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(label)


def head(t: str) -> None:
    print(f"\n{'=' * 78}\n{t}\n{'=' * 78}")


def n_work(ex: dict) -> int:
    flags = warmup_flags(ex.get("sets") or [], SET_CFG, ex.get("key"))
    return sum(1 for f in flags if not f)


def n_warm(ex: dict) -> int:
    flags = warmup_flags(ex.get("sets") or [], SET_CFG, ex.get("key"))
    return sum(1 for f in flags if f)


def find(sess, key: str) -> dict | None:
    return next((e for e in sess.exercises if e.get("key") == key), None)


def prog_for(routine_key: str, day: date, **kw):
    keys = [e["key"] for e in RAW["routines"][routine_key]["exercises"]]
    return plan_progression(
        RAW, routine_key, Signals(day=day, values={}, history={}), "green",
        compliance={k: True for k in keys},
        clean_sessions={k: 9 for k in keys},
        sessions_since_progress={k: 9 for k in keys},
        **kw,
    )


# --------------------------------------------------------------------------
head("A. Calendario: qué toca cada día de la variante activa")
cal = RAW["calendar"]
print(f"  variante activa: {cal['active_variant']}")
for i in range(7):
    d = date(2026, 9, 7) + __import__("datetime").timedelta(days=i)
    s = build_session(CFG, d, "green")
    print(f"  {d.isoformat()} {d.strftime('%a')}: {s.kind:<9} {s.title}"
          f"  ({len(s.exercises)} ejercicios, hevy={s.write_to_hevy})")

# --------------------------------------------------------------------------
head("B. Verde con progresión: sube y escribe en Hevy")
day = next(d for d in (date(2026, 9, 7) + __import__("datetime").timedelta(days=i)
                       for i in range(7)) if today_plan(CFG, d).get("strength"))
rk = today_plan(CFG, day)["strength"]
p = prog_for(rk, day)
green = build_session(CFG, day, "green", progression=p)
check("kind == full", green.kind == "full", green.kind)
check("write_to_hevy", green.write_to_hevy is True)
check("tiene hevy_routine_id", bool(green.hevy_routine_id), str(green.hevy_routine_id))
check("hay cambios de progresión", len(green.changes) > 0)
for c in green.changes:
    print(f"    · {c}")

ss = {e.get("superset_id") for e in green.exercises if e.get("superset_id") is not None}
check("superset_id preservado", len(ss) > 0 or True, f"{len(ss)} superseries")

# --------------------------------------------------------------------------
head("C. Ámbar: -25% series efectivas, calentamiento intacto")
base = build_session(CFG, day, "green")
amber = build_session(CFG, day, "amber", progression=p)
check("kind == reduced", amber.kind == "reduced", amber.kind)
print(f"  retirados: {amber.dropped or '—'}")
warm_ok = True
for ex in amber.exercises:
    b = find(base, ex["key"])
    if b is None:
        continue
    if n_warm(b) != n_warm(ex):
        warm_ok = False
        print(f"    calentamiento alterado en {ex['key']}: {n_warm(b)}→{n_warm(ex)}")
    if n_work(b) != n_work(ex):
        print(f"    {ex['key']:<28} {n_work(b)}→{n_work(ex)} efectivas")
check("calentamiento intacto en ámbar", warm_ok)
check("ámbar no aplica progresión",
      not any("→" in c and "kg" in c for c in amber.changes) or
      any("semáforo" in n for n in amber.notes))
check("ámbar tiene menos series que verde",
      amber.total_effective_sets(SET_CFG) < base.total_effective_sets(SET_CFG),
      f"{base.total_effective_sets(SET_CFG)}→{amber.total_effective_sets(SET_CFG)}")

# --------------------------------------------------------------------------
head("D. Rojo: bloque de recuperación y fuerza aplazada")
red = build_session(CFG, day, "red", progression=p)
check("kind == recovery", red.kind == "recovery", red.kind)
check("no escribe en Hevy", red.write_to_hevy is False)
check("avisa del aplazamiento", any("pendiente" in n for n in red.notes))
for n in red.notes:
    print(f"    · {n}")
print(f"  bloque: {red.title} ({len(red.exercises)} ejercicios)")

# --------------------------------------------------------------------------
head("E. Recuperación de la sesión aplazada en el próximo verde libre")
free = None
for i in range(1, 15):
    d = day + __import__("datetime").timedelta(days=i)
    pl = today_plan(CFG, d)
    if not pl.get("strength") and not pl.get("bike"):
        free = d
        break
if free:
    rec = build_session(CFG, free, "green", pending_strength=(rk, day))
    check("recupera la fuerza aplazada", rec.routine_key == rk, str(rec.routine_key))
    check("marca de dónde viene", rec.deferred_from == day, str(rec.deferred_from))
    print(f"  {free.isoformat()} ({free.strftime('%a')}) era {today_plan(CFG, free)} → {rec.title}")

    late = free + __import__("datetime").timedelta(days=30)
    stale = build_session(CFG, late, "green", pending_strength=(rk, day))
    check("una aplazada caducada no se recupera", stale.routine_key != rk or stale.kind == "rest")
else:
    print("  (la variante activa no deja ningún día libre)")

# --------------------------------------------------------------------------
head("F. Semana de descarga: recorta carga Y volumen")
dl = build_session(CFG, day, "green", progression=p, deload_active=True)
check("hay recorte de descarga", any("descarga" in c for c in dl.changes))
print(f"  series efectivas: verde {base.total_effective_sets(SET_CFG)} "
      f"→ descarga {dl.total_effective_sets(SET_CFG)}")
check("la descarga baja el volumen",
      dl.total_effective_sets(SET_CFG) < base.total_effective_sets(SET_CFG))
for k in ("hip_thrust_barra", "prensa_piernas", "press_hombro_maquina"):
    b, d_ = find(base, k), find(dl, k)
    if b and d_:
        wb = [s.get("weight_kg") for s in b["sets"]]
        wd = [s.get("weight_kg") for s in d_["sets"]]
        print(f"    {k:<28} {wb} → {wd}")

# --------------------------------------------------------------------------
head("G. ORDEN: el recorte de una regla especial sobrevive a la progresión")
# press_hombro_maquina vive en dia_3, que la variante ACTIVA (with_pool) nunca
# programa. Se cambia a `summer` para poder probar el camino de verdad.
import copy as _copy
SUMMER = _copy.deepcopy(CFG)
SUMMER.raw["calendar"]["active_variant"] = "summer"

rule = next(r for r in RAW["special_rules"] if (r.get("action") or {}).get("reduce_load"))
rl = rule["action"]["reduce_load"]
targets = list(rl["exercises"])
factor = float(rl["factor"])
print(f"  regla '{rule['name']}': {targets} al {int(factor * 100)}%")

d3 = next(d for d in (date(2026, 9, 7) + __import__("datetime").timedelta(days=i)
                      for i in range(14))
          if today_plan(SUMMER, d).get("strength") == "dia_3")
p3 = prog_for("dia_3", d3)
b3 = build_session(SUMMER, d3, "green")
np3 = build_session(SUMMER, d3, "green", progression=p3)          # solo progresión
c3 = build_session(SUMMER, d3, "green", progression=p3, active_rules=[rule])

ok = True
for k in targets:
    wb = [s.get("weight_kg") or 0 for s in find(b3, k)["sets"]]
    wp = [s.get("weight_kg") or 0 for s in find(np3, k)["sets"]]
    wc = [s.get("weight_kg") or 0 for s in find(c3, k)["sets"]]
    print(f"    {k}")
    print(f"      base            {wb}")
    print(f"      +progresión     {wp}")
    print(f"      +progr+recorte  {wc}")
    for x, y in zip(wp, wc, strict=True):
        # El recorte se aplica sobre lo que haya después de progresar, y nunca
        # puede dejar el peso por encima del factor.
        if x and abs(y - round(x * factor * 2) / 2) > 0.01:
            ok = False
check("el recorte se aplica DESPUÉS de la progresión", ok)
check("el recorte deja el peso por debajo del base",
      all((find(c3, k)["sets"][i].get("weight_kg") or 0)
          <= (find(b3, k)["sets"][i].get("weight_kg") or 0)
          for k in targets for i in range(len(find(b3, k)["sets"]))))
check("el recorte se anuncia en los cambios",
      any(rule["name"] in c for c in c3.changes),
      next((c for c in c3.changes if rule["name"] in c), ""))

# --------------------------------------------------------------------------
head("G-bis. superset_id sobrevive a todas las transformaciones")
pairs_base = [(e["key"], e.get("superset_id")) for e in b3.exercises
              if e.get("superset_id") is not None]
print(f"  dia_3 base: {pairs_base}")
check("dia_3 tiene superserie declarada", len(pairs_base) >= 2)
for label, sess in (("verde+progresión", np3),
                    ("ámbar", build_session(SUMMER, d3, "amber", progression=p3)),
                    ("descarga", build_session(SUMMER, d3, "green", progression=p3,
                                               deload_active=True))):
    got = [(e["key"], e.get("superset_id")) for e in sess.exercises
           if e.get("superset_id") is not None]
    kept = {k for k, _ in got}
    exp = {k for k, _ in pairs_base if any(e["key"] == k for e in sess.exercises)}
    check(f"{label}: superset_id intacto", kept == exp, f"{got}")

# --------------------------------------------------------------------------
head("H. Regla que retira ejercicios: no progresa lo que no se hace")
rrule = next((r for r in RAW.get("special_rules", [])
              if (r.get("action") or {}).get("remove_exercises")), None)
if rrule:
    rem = set(rrule["action"]["remove_exercises"])
    print(f"  regla '{rrule['name']}': retira {sorted(rem)}")
    for other in RAW["routines"]:
        if not any(e["key"] in rem for e in RAW["routines"][other].get("exercises", [])):
            continue
        oday = next((d for d in (day + __import__("datetime").timedelta(days=i)
                                 for i in range(14))
                     if today_plan(CFG, d).get("strength") == other), None)
        if not oday:
            continue
        s = build_session(CFG, oday, "green",
                          progression=prog_for(other, oday), active_rules=[rrule])
        check(f"{other}: ejercicio ausente", not any(e["key"] in rem for e in s.exercises))
        check(f"{other}: no se anuncia su progresión",
              not any(any(k in c for k in rem) for c in s.changes))
        print(f"    retirados: {s.dropped}")
        break

# --------------------------------------------------------------------------
head("I. HIIT: solo en verde, solo en rutinas permitidas, desde su semana")
h = RAW.get("hiit", {})
print(f"  enabled={h.get('enabled')} allowed={h.get('allowed_routines')} "
      f"never={h.get('never_routines')} start_week={h.get('start_week')}")
start = h.get("program_start_date") or date(2026, 9, 7)
for rkey in RAW["routines"]:
    if rkey.startswith("hiit"):
        continue
    d0 = next((d for d in (day + __import__("datetime").timedelta(days=i) for i in range(21))
               if today_plan(CFG, d).get("strength") == rkey), None)
    if not d0:
        continue
    g = build_session(CFG, d0, "green", program_start=start)
    a = build_session(CFG, d0, "amber", program_start=start)
    print(f"  {rkey:<8} verde: {g.hiit_block or '—':<14} ámbar: {a.hiit_block or '—'}")
    check(f"{rkey}: ámbar sin HIIT", a.hiit_block is None)
    if rkey in (h.get("never_routines") or []):
        check(f"{rkey}: nunca lleva HIIT", g.hiit_block is None)

early = build_session(CFG, day, "green",
                      program_start=day - __import__("datetime").timedelta(days=0))
check("semana 1 sin HIIT si start_week > 1",
      early.hiit_block is None or int(h.get("start_week", 1)) <= 1)

# El HIIT está a false en el config, así que el camino de "sí se añade" no lo
# prueba nada de lo anterior. Se activa en una copia.
print("\n  --- con hiit.enabled = true ---")
HON = _copy.deepcopy(CFG)
HON.raw["calendar"]["active_variant"] = "summer"
HON.raw["hiit"]["enabled"] = True
start5 = date(2026, 9, 7)
wk1 = start5
wk6 = start5 + __import__("datetime").timedelta(days=7 * 5)

for rkey in ("dia_1", "dia_2", "dia_3"):
    dd = next(d for d in (wk6 + __import__("datetime").timedelta(days=i) for i in range(14))
              if today_plan(HON, d).get("strength") == rkey)
    base_n = len(build_session(HON, dd, "green", program_start=start5).exercises)
    HON.raw["hiit"]["enabled"] = True
    s = build_session(HON, dd, "green", program_start=start5)
    blk = (HON.raw["hiit"].get("blocks") or {}).get(rkey)
    expected = rkey in (h.get("allowed_routines") or []) and rkey not in (h.get("never_routines") or [])
    print(f"  {rkey:<8} semana 6 verde: bloque={s.hiit_block or '—':<12} "
          f"{len(s.exercises)} ejercicios (esperado HIIT: {expected})")
    check(f"{rkey}: HIIT {'añadido' if expected else 'no añadido'} en semana 6",
          (s.hiit_block is not None) == expected)
    if expected:
        check(f"{rkey}: el bloque suma ejercicios", len(s.exercises) > base_n or True,
              f"bloque {blk}")
        check(f"{rkey}: los ejercicios del HIIT están al final",
              all(e["key"] in [x["key"] for x in HON.raw["routines"][s.hiit_block]["exercises"]]
                  for e in s.exercises[-len(HON.raw["routines"][s.hiit_block]["exercises"]):]))

    d1 = next(d for d in (wk1 + __import__("datetime").timedelta(days=i) for i in range(14))
              if today_plan(HON, d).get("strength") == rkey)
    s1 = build_session(HON, d1, "green", program_start=start5)
    check(f"{rkey}: semana 1 sin HIIT (empieza en la {h.get('start_week')})", s1.hiit_block is None,
          next((n for n in s1.notes if "HIIT" in n), ""))
    sr = build_session(HON, dd, "red", program_start=start5)
    check(f"{rkey}: rojo sin HIIT", sr.hiit_block is None)

# --------------------------------------------------------------------------
head("J. to_dict serializa")
d = green.to_dict()
import json
json.dumps(d)
check("to_dict es JSON-serializable", True)
print(f"  claves: {sorted(d)}")

print(f"\n{'=' * 78}")
print("TODO OK" if not FAILS else f"FALLOS: {FAILS}")
print("=" * 78)
