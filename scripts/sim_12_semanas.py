"""Simulación de 12 semanas completas del motor, de principio a fin.

Tres rutinas alternando (variante `summer`, la única que programa las tres),
semáforos variados con una distribución realista, una semana de descarga, y el
estado de las rutinas persistiendo de una sesión a la siguiente.

QUÉ PERSISTE Y QUÉ NO
---------------------
Solo la PROGRESIÓN modifica el estado guardado. Los recortes del ámbar y de la
descarga son transitorios: se aplican a la sesión de ese día y se olvidan. Si
persistieran, un mal día dejaría la rutina recortada para siempre y bastarían
tres ámbares en un mes para desmontar el programa. Es la misma separación que
hará `decision.py`: el YAML es el plan, la sesión es lo que hoy toca hacer.

CUMPLIMIENTO
------------
No se asume que todo sale bien. Tras una subida de carga hay una probabilidad
de no completar las reps objetivo la siguiente vez, que es lo que en la vida
real cierra la puerta y retrasa la siguiente subida. Sin eso la simulación
mediría el techo teórico del motor, no su ritmo.
"""

from __future__ import annotations

import copy
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from app.config_loader import load_config
from app.engine.progression import plan_progression
from app.engine.session_builder import apply_progression, build_session, today_plan
from app.engine.sets import warmup_flags
from app.engine.signals import Signals

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT / "config.yaml")

WEEKS = 12
START = date(2026, 9, 7)          # lunes
DELOAD_WEEK = 8
VARIANT = "summer"
SEED = 20260907

# Distribución de semáforos. Calibrada a ojo sobre lo que sería un trimestre
# normal: la mayoría de los días se entrena, algunos se entrena menos y muy de
# vez en cuando no se entrena. PENDIENTE DE RECALIBRACIÓN con datos reales.
P_GREEN, P_AMBER = 0.72, 0.20     # el resto, rojo
P_FAIL_AFTER_LOAD = 0.30          # fallar las reps tras una subida de carga
P_FAIL_NORMAL = 0.05

# Perfiles alternativos para ver cuánto depende el resultado de esta suposición:
#   py scripts/sim_12_semanas.py tranquilo
PERFILES = {
    "normal":   (0.72, 0.20),
    "tranquilo": (0.85, 0.12),
    "duro":     (0.60, 0.27),
}
if len(sys.argv) > 1 and sys.argv[1] in PERFILES:
    P_GREEN, P_AMBER = PERFILES[sys.argv[1]]
    print(f"[perfil '{sys.argv[1]}': verde {P_GREEN:.0%} ámbar {P_AMBER:.0%} "
          f"rojo {1 - P_GREEN - P_AMBER:.0%}]")

rng = random.Random(SEED)

STATE = copy.deepcopy(CFG)
STATE.raw["calendar"]["active_variant"] = VARIANT
RAW = STATE.raw
SET_CFG = RAW["set_types"]
ROUTINES = ["dia_1", "dia_2", "dia_3"]

ALL_KEYS: dict[str, list[str]] = {
    r: [e["key"] for e in RAW["routines"][r]["exercises"]] for r in ROUTINES
}


def snapshot(routine: str) -> dict[str, tuple[int, list[float | None], list[int | None]]]:
    """Series efectivas, pesos y reps/segundos de cada ejercicio ahora mismo."""
    out = {}
    for ex in RAW["routines"][routine]["exercises"]:
        sets = ex["sets"]
        flags = warmup_flags(sets, SET_CFG, ex["key"])
        work = [s for s, f in zip(sets, flags, strict=True) if not f]
        out[ex["key"]] = (
            len(work),
            [s.get("weight_kg") for s in work],
            [s.get("reps") if s.get("reps") is not None else s.get("duration_s") for s in work],
        )
    return out


def fmt_w(ws: list[float | None]) -> str:
    if not any(ws):
        return "—"
    uniq = sorted({w for w in ws if w})
    if len(uniq) == 1:
        return f"{uniq[0]:g}".replace(".", ",")
    return "/".join(f"{w:g}".replace(".", ",") for w in ws if w)


def fmt_r(rs: list[int | None]) -> str:
    uniq = sorted({r for r in rs if r})
    if not uniq:
        return "—"
    if len(uniq) == 1:
        return str(uniq[0])
    return f"{uniq[0]}-{uniq[-1]}"


INITIAL = {r: snapshot(r) for r in ROUTINES}

# --- estado que arrastra el motor entre sesiones ---------------------------
# Indexado por (rutina, ejercicio), que es lo que dice `state_scope`. La
# plancha lateral del Día 1 y la del Día 3 son dosis distintas (20 s y 30 s) y
# no comparten racha: si la compartieran, acumularía sesiones limpias de tres
# rutinas y progresaría al triple de velocidad de la que se entrena.
clean: dict[tuple[str, str], int] = {}
waiting: dict[tuple[str, str], int] = {}
compliance: dict[tuple[str, str], bool] = {}
just_loaded: set[tuple[str, str]] = set()
for r in ROUTINES:
    for k in ALL_KEYS[r]:
        clean[(r, k)] = 0
        waiting[(r, k)] = 0
        compliance[(r, k)] = True

# El semáforo de la última vez que se hizo CADA rutina, que es lo que ahora
# gobierna las dos puertas de volumen.
last_light: dict[str, str | None] = dict.fromkeys(ROUTINES, None)

light_hist: dict[date, str] = {}
disc_hist: dict[date, int] = {}

n_load_per_session: list[int] = []
n_vol_per_session: list[int] = []
progressed: dict[tuple[str, str], int] = dict.fromkeys(clean, 0)
ceiling_hits: dict[tuple[str, str], int] = {}
missing: dict[tuple[str, str], int] = {}
gate_log: list[tuple[str, str, bool, str, bool, str]] = []
counts = {"full": 0, "reduced": 0, "recovery": 0, "rest": 0, "bike": 0, "pool": 0}
lights_count = {"green": 0, "amber": 0, "red": 0}
pending: tuple[str, date] | None = None
deferred_recovered = 0
blocked_reasons: dict[str, int] = {}

print("=" * 100)
print(f"SIMULACIÓN DE {WEEKS} SEMANAS · variante '{VARIANT}' · descarga en la semana "
      f"{DELOAD_WEEK} · semilla {SEED}")
print("=" * 100)

for wk in range(1, WEEKS + 1):
    deload = wk == DELOAD_WEEK
    monday = START + timedelta(days=7 * (wk - 1))
    header = f"\n── SEMANA {wk:>2}  ({monday.isoformat()})" + ("   ·· DESCARGA ··" if deload else "")
    print(header)

    for off in range(7):
        day = monday + timedelta(days=off)

        # --- semáforo del día ---
        roll = rng.random()
        light = "green" if roll < P_GREEN else ("amber" if roll < P_GREEN + P_AMBER else "red")
        # La lumbar acompaña al semáforo: en rojo duele más. Es lo que dispara
        # tanto el freno general (>=4) como el freno de volumen de 7 días.
        disc = {"green": rng.choice([0, 0, 1, 1, 2]),
                "amber": rng.choice([2, 3, 3, 4]),
                "red": rng.choice([4, 5, 5, 6])}[light]
        light_hist[day] = light
        disc_hist[day] = disc
        lights_count[light] += 1

        sig = Signals(
            day=day,
            values={"lower_discomfort": disc},
            history={"light": dict(light_hist), "lower_discomfort": dict(disc_hist)},
        )

        plan_day = today_plan(STATE, day)
        rk = plan_day.get("strength")
        is_recovered = False
        if rk is None and pending and light == "green" and not plan_day.get("bike"):
            if (day - pending[1]).days <= 7:
                rk, is_recovered = pending[0], True

        prog = None
        if rk:
            prog = plan_progression(
                RAW, rk, sig, light,
                compliance={k: compliance[(rk, k)] for k in ALL_KEYS[rk]},
                clean_sessions={k: clean[(rk, k)] for k in ALL_KEYS[rk]},
                deload_active=deload,
                sessions_since_progress={k: waiting[(rk, k)] for k in ALL_KEYS[rk]},
                last_routine_light=last_light[rk],
            )
            gate_log.append((rk, light, prog.sets_allowed, prog.sets_reason,
                             prog.reps_allowed, prog.reps_reason))

        sess = build_session(
            STATE, day, light,
            progression=prog,
            deload_active=deload,
            pending_strength=pending,
        )
        counts[sess.kind] = counts.get(sess.kind, 0) + 1

        if sess.kind == "recovery" and sess.routine_key:
            pending = (rk, day) if rk else pending
        elif sess.routine_key and sess.kind in ("full", "reduced"):
            if is_recovered or (pending and sess.deferred_from):
                deferred_recovered += 1
                pending = None

        # --- persistir SOLO la progresión -----------------------------------
        applied: set[str] = set()
        if rk and prog and sess.kind == "full" and not deload:
            live = RAW["routines"][rk]["exercises"]
            # Un ejercicio retirado hoy por una regla no debe progresar en el
            # estado guardado: no se ha hecho.
            present = {e["key"] for e in sess.exercises}
            keep = copy.copy(prog)
            keep.exercises = [e for e in prog.exercises if e.key in present]
            apply_progression(live, keep, SET_CFG)
            applied = {c.key for c in keep.changes}
            n_load_per_session.append(sum(1 for c in keep.changes if c.kind == "load"))
            n_vol_per_session.append(sum(1 for c in keep.changes if c.kind == "volume"))
            for c in keep.changes:
                progressed[(rk, c.key)] += 1
            for e in prog.exercises:
                if e.at_ceiling:
                    ceiling_hits[(rk, e.key)] = ceiling_hits.get((rk, e.key), 0) + 1
                if e.needs_data:
                    missing[(rk, e.key)] = missing.get((rk, e.key), 0) + 1
            if not keep.changes and prog.gate_reason != "puerta abierta":
                blocked_reasons[prog.gate_reason] = blocked_reasons.get(prog.gate_reason, 0) + 1

        # --- actualizar el estado que arrastra el motor ----------------------
        if rk and sess.kind in ("full", "reduced"):
            for k in ALL_KEYS[rk]:
                if k not in {e["key"] for e in sess.exercises}:
                    continue
                p_fail = P_FAIL_AFTER_LOAD if (rk, k) in just_loaded else P_FAIL_NORMAL
                ok = rng.random() > p_fail
                compliance[(rk, k)] = ok
                clean[(rk, k)] = clean[(rk, k)] + 1 if ok else 0
                waiting[(rk, k)] = 0 if k in applied else waiting[(rk, k)] + 1
            just_loaded = {(rk, c.key) for c in (prog.changes if prog else [])
                           if c.kind == "load"}
            last_light[rk] = light
        elif rk and sess.kind == "recovery":
            # Un rojo con la fuerza aplazada también es información sobre esa
            # rutina: la próxima vez que toque, la puerta estricta la ve.
            last_light[rk] = light

        # --- línea del día ---------------------------------------------------
        mark = {"green": "V", "amber": "A", "red": "R"}[light]
        what = ", ".join(f"{c.name.split('(')[0].strip()} {c.what}"
                         for c in (prog.changes if prog and applied else [])
                         if c.key in applied)
        tail = ""
        if sess.kind in ("full", "reduced", "recovery"):
            tail = f"{sess.title:<16}"
            if sess.deferred_from:
                tail += "[recuperada] "
            if sess.kind == "reduced":
                tail += f"[-{len(sess.dropped)} ej, {sess.total_effective_sets(SET_CFG)} series] "
            elif deload and sess.kind == "full":
                tail += f"[descarga, {sess.total_effective_sets(SET_CFG)} series] "
            tail += what or ("—" if sess.kind == "full" else "")
        else:
            tail = sess.title
        print(f"   {day.strftime('%a')} {day.day:>2}  {mark}  d{disc}  {tail}")

print("\n" + "=" * 100)
print("EVOLUCIÓN POR EJERCICIO")
print("=" * 100)

for r in ROUTINES:
    print(f"\n{RAW['routines'][r]['title']} ({r})")
    print(f"  {'ejercicio':<30} {'modo':<7} {'series':>9}  {'carga':>18}  {'reps/s':>11}   subidas")
    print(f"  {'-' * 30} {'-' * 7} {'-' * 9}  {'-' * 18}  {'-' * 11}   -------")
    now = snapshot(r)
    for ex in RAW["routines"][r]["exercises"]:
        k = ex["key"]
        s0, w0, r0 = INITIAL[r][k]
        s1, w1, r1 = now[k]
        mode = ex.get("progression_type", RAW["progression"]["default_progression_type"])
        sets_s = f"{s0}→{s1}" if s0 != s1 else f"{s0}"
        w_s = f"{fmt_w(w0)}→{fmt_w(w1)}" if fmt_w(w0) != fmt_w(w1) else fmt_w(w0)
        r_s = f"{fmt_r(r0)}→{fmt_r(r1)}" if fmt_r(r0) != fmt_r(r1) else fmt_r(r0)
        flag = ""
        if (r, k) in ceiling_hits:
            flag = " ·techo"
        if (r, k) in missing:
            flag = " ·sin dato"
        print(f"  {ex['name'][:30]:<30} {mode:<7} {sets_s:>9}  {w_s:>18}  {r_s:>11}   "
              f"{progressed[(r, k)]:>2}x{flag}")

print("\n" + "=" * 100)
print("RESUMEN")
print("=" * 100)
tot = sum(lights_count.values())
print(f"  días simulados      {tot}  ·  verde {lights_count['green']} "
      f"({lights_count['green'] / tot:.0%})  ámbar {lights_count['amber']} "
      f"({lights_count['amber'] / tot:.0%})  rojo {lights_count['red']} "
      f"({lights_count['red'] / tot:.0%})")
print(f"  sesiones            " + "  ".join(f"{k} {v}" for k, v in counts.items() if v))
print(f"  aplazadas recuperadas  {deferred_recovered}")

cap_l = RAW["progression"]["volume_safety"]["max_load_increases_per_session"]
cap_v = RAW["progression"]["volume_safety"]["max_volume_increases_per_session"]
if n_load_per_session:
    print(f"\n  subidas de CARGA por sesión   máx {max(n_load_per_session)} (cupo {cap_l})  "
          f"media {sum(n_load_per_session) / len(n_load_per_session):.1f}")
    print(f"  subidas de VOLUMEN por sesión máx {max(n_vol_per_session)} (cupo {cap_v})  "
          f"media {sum(n_vol_per_session) / len(n_vol_per_session):.1f}")
    assert max(n_load_per_session) <= cap_l, "CUPO DE CARGA SUPERADO"
    assert max(n_vol_per_session) <= cap_v, "CUPO DE VOLUMEN SUPERADO"
    print("  cupos respetados: sí")

never = [f"{r}/{k}" for (r, k), v in progressed.items() if v == 0]
print(f"\n  ejercicios que NUNCA progresaron: {never or 'ninguno'}")
if ceiling_hits:
    print(f"  en su techo: {', '.join(f'{r}/{k}' for r, k in sorted(ceiling_hits))}")
if missing:
    print(f"  sin carga registrada en Hevy: "
          f"{', '.join(f'{r}/{k}' for r, k in sorted(missing))}")
if blocked_reasons:
    print("\n  sesiones verdes sin progresión, por motivo:")
    for reason, n in sorted(blocked_reasons.items(), key=lambda x: -x[1]):
        print(f"    {n:>2}x  {reason}")

# --- las dos puertas -------------------------------------------------------
print("\n" + "-" * 100)
print("LAS DOS PUERTAS DE VOLUMEN (solo sesiones en verde)")
print("-" * 100)
verdes = [g for g in gate_log if g[1] == "green"]
if verdes:
    ns = sum(1 for g in verdes if g[2])
    nr = sum(1 for g in verdes if g[4])
    print(f"  sesiones verdes con rutina        {len(verdes)}")
    print(f"  puerta de SERIES abierta          {ns:>3}  ({ns / len(verdes):.0%})")
    print(f"  puerta de REPS   abierta          {nr:>3}  ({nr / len(verdes):.0%})")
    from collections import Counter
    for lbl, idx_ok, idx_why in (("SERIES", 2, 3), ("REPS", 4, 5)):
        cerr = Counter(g[idx_why].split(" (")[0] for g in verdes if not g[idx_ok])
        if cerr:
            print(f"\n  motivos de cierre de {lbl}:")
            for why, n in cerr.most_common():
                print(f"    {n:>3}x  {why}")

# --- ¿alguien va demasiado rápido? -----------------------------------------
print("\n" + "-" * 100)
print("EL OTRO EXTREMO: ¿alguien progresa demasiado rápido?")
print("-" * 100)
print("  Referencia: 12 semanas son ~12 sesiones por rutina. Más de ~6 subidas es")
print("  más de una cada dos sesiones, y en carga eso es mucho.")
filas = []
for r in ROUTINES:
    now = snapshot(r)
    for ex in RAW["routines"][r]["exercises"]:
        k = ex["key"]
        s0, w0, r0 = INITIAL[r][k]
        s1, w1, r1 = now[k]
        top0 = max([w for w in w0 if w] or [0])
        top1 = max([w for w in w1 if w] or [0])
        pct = ((top1 / top0 - 1) * 100) if top0 else 0.0
        filas.append((progressed[(r, k)], pct, r, ex["name"][:28], s0, s1, top0, top1))
filas.sort(key=lambda x: (-x[1], -x[0]))
print(f"\n  {'ejercicio':<30} {'rutina':<7} {'subidas':>7} {'series':>7} "
      f"{'carga tope':>16} {'Δ carga':>9}")
for n, pct, r, name, s0, s1, t0, t1 in filas[:8]:
    ss = f"{s0}→{s1}" if s0 != s1 else str(s0)
    tt = f"{t0:g}→{t1:g}" if t0 != t1 else (f"{t0:g}" if t0 else "—")
    print(f"  {name:<30} {r:<7} {n:>6}x {ss:>7} {tt:>16} "
          f"{(f'+{pct:.0f}%' if pct else '—'):>9}")
peor = filas[0] if filas else None
if peor and peor[1] > 40:
    print(f"\n  AVISO: {peor[3]} sube un {peor[1]:.0f}% de carga en 12 semanas.")
elif peor:
    print(f"\n  Nadie se dispara: la mayor subida de carga es +{peor[1]:.0f}% en 12 semanas.")
