"""Smoke test de session_builder: un día de cada color, cada escalón del ciclo
y cada caso raro (descarga, HIIT, reglas especiales, superseries).

Lo que se comprueba de verdad aquí no es que no reviente, sino el ORDEN: que
el recorte de una regla especial sobreviva a la progresión del mismo día, y que
el calentamiento siga en pie después de todos los recortes.

Antes había que buscar el día de la semana en que el calendario programaba cada
rutina -y para `dia_3` había que cambiarse de variante, porque la activa no lo
programaba ningún día-. Ahora la rutina se pide y ya está: `build_session`
recibe `rotation_routine` y el día de la semana no pinta nada.
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
from app.engine.session_builder import build_session, siguiente_en_rotacion
from app.engine.sets import warmup_flags
from app.engine.signals import Signals

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT / "config.yaml")
RAW = CFG.raw
SET_CFG = RAW["set_types"]
CICLO = CFG.rotation_order()

# Una fecha cualquiera. Es lunes, pero da exactamente igual: desde que la fuerza
# va por rotación, el día de la semana no entra en `build_session` para nada más
# que el cálculo de la semana de programa (HIIT y descarga). Si algún día
# volviera a importar, los apartados de abajo empezarían a dar resultados
# distintos según cuándo se ejecute el script, que es justo lo que se quiere ver.
HOY = date(2026, 9, 14)

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


# Un día tranquilo con TODO lo que piden los frenos de `progression.brakes`.
#
# Aquí había un `Signals(day=day, values={}, history={})`, o sea un día en el
# que no se sabía nada. Cuando el freno lumbar pasó a cerrar la puerta si no
# puede evaluarse -"un freno que no se puede evaluar NO es un freno que no
# salta"-, este script se quedó progresando cero sin enterarse: el apartado B
# marcaba FALLA por "no hay cambios" y el G, que existe para comprobar que el
# recorte de una regla especial se aplica DESPUÉS de progresar, imprimía la
# misma lista de pesos en las tres líneas y daba OK. Un OK sobre dos columnas
# idénticas porque ninguna de las dos había progresado.
#
# Los valores son los mismos de `tests/conftest.py::SENALES_COMPLETAS`. No se
# importan de ahí a propósito: un script de diagnóstico que dependiera del
# andamiaje de los tests dejaría de poder ejecutarse solo.
DIA_TRANQUILO = {
    "lower_discomfort": 1,
    "upper_discomfort": 1,
    "fatigue": 3,
    "training_desire": 8,
    "hrv": 60.0,
    "hrv_baseline": 60.0,
    "hrv_ratio": 1.0,
    "rhr": 50.0,
    "rhr_baseline": 50.0,
    "rhr_delta": 0.0,
    "sleep_min": 450.0,
    "load_2d": 100.0,
    # `weekend_intense_rides` y `weekend_total_hours` se han ido de aquí igual
    # que de `SENALES_COMPLETAS`: ninguna regla las lee y `build_signals` ya no
    # las escribe. Copiar a mano los valores de otro fichero tiene este precio
    # -hay que acordarse de los dos sitios- y se asume a sabiendas: un guión de
    # diagnóstico que dependiera del andamiaje de los tests dejaría de poder
    # ejecutarse solo, que es peor.
}


def senales(day: date) -> Signals:
    valores = dict(DIA_TRANQUILO)
    s = Signals(
        day=day,
        values=valores,
        history={k: {day - timedelta(days=i): v for i in range(7)}
                 for k, v in valores.items()},
    )
    s.adaptive.update({"load_2d_p90": 200.0, "load_7d_p90": 400.0})
    return s


def prog_for(routine_key: str, day: date, **kw):
    keys = [e["key"] for e in RAW["routines"][routine_key]["exercises"]]
    return plan_progression(
        RAW, routine_key, senales(day), "green",
        compliance={k: True for k in keys},
        clean_sessions={k: 9 for k in keys},
        sessions_since_progress={k: 9 for k in keys},
        **kw,
    )


# --------------------------------------------------------------------------
head("A. Rotación: el ciclo entero, y que da la vuelta")
print(f"  ciclo declarado: {CICLO}")
for rkey in CICLO:
    s = build_session(CFG, HOY, "green", rotation_routine=rkey)
    print(f"  {rkey:<8}: {s.kind:<9} {s.title}"
          f"  ({len(s.exercises)} ejercicios, hevy={s.write_to_hevy})")

# La vuelta completa, empezando por "todavía no hay ninguna leída de Hevy".
vuelta, ultima = [], None
for _ in range(len(CICLO) + 1):
    ultima = siguiente_en_rotacion(CFG, ultima)
    vuelta.append(ultima)
print(f"  vuelta desde cero: {vuelta}")
check("el ciclo da la vuelta y vuelve al principio", vuelta == CICLO + CICLO[:1],
      str(vuelta))
check("dia_3 está en el ciclo", "dia_3" in CICLO,
      "el Día 3 se hace siempre; la variante de calendario que lo ignoraba "
      "es justo lo que se ha borrado")

# --------------------------------------------------------------------------
head("B. Verde con progresión: sube y escribe en Hevy")
day = HOY
rk = CICLO[0]
p = prog_for(rk, day)
# Primero la puerta, y con su motivo delante. Si mañana aparece un freno nuevo
# que este script no sabe alimentar, lo dirá por su nombre en vez de dejar que
# todo lo de abajo se ejecute sobre una progresión vacía.
check("la puerta de progresión está abierta", p.gate_open, p.gate_reason)
green = build_session(CFG, day, "green", rotation_routine=rk, progression=p)
check("kind == full", green.kind == "full", green.kind)
check("write_to_hevy", green.write_to_hevy is True)
check("tiene hevy_routine_id", bool(green.hevy_routine_id), str(green.hevy_routine_id))
check("hay cambios de progresión", len(green.changes) > 0)
for c in green.changes:
    print(f"    · {c}")

# Aquí había un `check("superset_id preservado", len(ss) > 0 or True, ...)`. El
# `or True` lo hacía imposible de fallar, y encima sobre dia_1, que no declara
# ninguna superserie: decía OK y "0 superseries" en la misma línea. Se queda
# como dato impreso; quien prueba las superseries de verdad es el apartado G-bis,
# sobre dia_3, que sí las tiene.
ss = {e.get("superset_id") for e in green.exercises if e.get("superset_id") is not None}
print(f"  superseries en {rk}: {len(ss)}")

# --------------------------------------------------------------------------
head("C. Ámbar: -25% series efectivas, calentamiento intacto")
base = build_session(CFG, day, "green", rotation_routine=rk)
amber = build_session(CFG, day, "amber", rotation_routine=rk, progression=p)
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
head("D. Rojo: bloque de recuperación, y la rotación se queda donde estaba")
red = build_session(CFG, day, "red", rotation_routine=rk, progression=p)
check("kind == recovery", red.kind == "recovery", red.kind)
check("no escribe en Hevy", red.write_to_hevy is False)
check("dice que la rotación no se mueve",
      any("la rotación no se mueve" in n for n in red.notes))
check("y dice cuál sigue tocando", any(rk in n for n in red.notes), rk)
for n in red.notes:
    print(f"    · {n}")
print(f"  bloque: {red.title} ({len(red.exercises)} ejercicios)")

# AQUÍ ESTABA EL APARTADO E, "recuperación de la sesión aplazada en el próximo
# verde libre", y no se ha sustituido por otro porque ya no hay nada que probar.
# Aquel apartado buscaba un día que el calendario dejara libre, construía la
# sesión con `pending_strength=(rk, day)` y comprobaba que la fuerza aplazada se
# recuperaba ahí y que una aplazada de hace un mes caducaba. Las tres piezas
# -el hueco libre, el pendiente y la caducidad- eran del calendario fijo.
#
# Con rotación el aplazamiento no se ha quitado: se ha vuelto la conducta por
# defecto. El puntero solo avanza con una sesión EJECUTADA, así que un día rojo,
# un día de bici o diez días sin pisar el gimnasio dejan `rk` exactamente donde
# estaba, sin estado que guardar ni fecha que caducar. Eso es lo que comprueba
# el apartado D de arriba, y `tests/test_decision.py` lo prueba en serio con
# semanas enteras sin entrenar.

# --------------------------------------------------------------------------
head("F. Semana de descarga: recorta carga Y volumen")
dl = build_session(CFG, day, "green", rotation_routine=rk, progression=p,
                   deload_active=True)
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
# `press_hombro_maquina` vive en dia_3. Aquí había una copia del config con
# `calendar.active_variant = "summer"`, porque la variante activa (`with_pool`)
# no programaba dia_3 ningún día de la semana y sin cambiarse de variante este
# apartado no tenía forma de llegar a la rutina. Ese apaño era el síntoma del
# fallo: todas las sesiones reales de Día 3 se registraban como entrenos
# sueltos. Ahora la rutina se pide por su nombre.
rule = next(r for r in RAW["special_rules"] if (r.get("action") or {}).get("reduce_load"))
rl = rule["action"]["reduce_load"]
targets = list(rl["exercises"])
factor = float(rl["factor"])
print(f"  regla '{rule['name']}': {targets} al {int(factor * 100)}%")

d3 = HOY
p3 = prog_for("dia_3", d3)
b3 = build_session(CFG, d3, "green", rotation_routine="dia_3")
np3 = build_session(CFG, d3, "green", rotation_routine="dia_3",
                   progression=p3)                               # solo progresión
c3 = build_session(CFG, d3, "green", rotation_routine="dia_3",
                   progression=p3, active_rules=[rule])

ok = True
movio = False
for k in targets:
    wb = [s.get("weight_kg") or 0 for s in find(b3, k)["sets"]]
    wp = [s.get("weight_kg") or 0 for s in find(np3, k)["sets"]]
    wc = [s.get("weight_kg") or 0 for s in find(c3, k)["sets"]]
    print(f"    {k}")
    print(f"      base            {wb}")
    print(f"      +progresión     {wp}")
    print(f"      +progr+recorte  {wc}")
    if wp != wb:
        movio = True
    for x, y in zip(wp, wc, strict=True):
        # El recorte se aplica sobre lo que haya después de progresar, y nunca
        # puede dejar el peso por encima del factor.
        if x and abs(y - round(x * factor * 2) / 2) > 0.01:
            ok = False
# Antes de dar por bueno el orden, que haya un orden que comprobar. Si la
# progresión no mueve el peso del ejercicio recortado, las líneas "base" y
# "+progresión" salen idénticas y el apartado entero da OK sin haber probado
# nada: exactamente lo que pasaba mientras los frenos cerraban la puerta.
check("la progresión mueve el peso del ejercicio recortado", movio,
      f"gate={p3.gate_open}: {p3.gate_reason}")
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
                    ("ámbar", build_session(CFG, d3, "amber",
                                            rotation_routine="dia_3", progression=p3)),
                    ("descarga", build_session(CFG, d3, "green",
                                               rotation_routine="dia_3", progression=p3,
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
    # Se recorre el ciclo entero, no se corta en la primera rutina que valga.
    # Antes esto llevaba un `break` porque encontrar el día de la semana en que
    # el calendario programaba cada rutina era caro y podía no existir; ahora la
    # rutina se pide y no hay excusa para probar solo una.
    tocadas = 0
    for other in CICLO:
        if not any(e["key"] in rem for e in RAW["routines"][other].get("exercises", [])):
            continue
        tocadas += 1
        s = build_session(CFG, day, "green", rotation_routine=other,
                          progression=prog_for(other, day), active_rules=[rrule])
        check(f"{other}: ejercicio ausente", not any(e["key"] in rem for e in s.exercises))
        check(f"{other}: no se anuncia su progresión",
              not any(any(k in c for k in rem) for c in s.changes))
        print(f"    {other}: retirados {s.dropped}")
    check("la regla toca alguna rutina del ciclo", tocadas > 0,
          f"{tocadas} rutinas")

# --------------------------------------------------------------------------
head("I. HIIT: solo en verde, solo en rutinas permitidas, desde su semana")
h = RAW.get("hiit", {})
print(f"  enabled={h.get('enabled')} allowed={h.get('allowed_routines')} "
      f"never={h.get('never_routines')} start_week={h.get('start_week')}")
start = h.get("program_start_date") or date(2026, 9, 7)
for rkey in CICLO:
    g = build_session(CFG, day, "green", rotation_routine=rkey, program_start=start)
    a = build_session(CFG, day, "amber", rotation_routine=rkey, program_start=start)
    print(f"  {rkey:<8} verde: {g.hiit_block or '—':<14} ámbar: {a.hiit_block or '—'}")
    check(f"{rkey}: ámbar sin HIIT", a.hiit_block is None)
    if rkey in (h.get("never_routines") or []):
        check(f"{rkey}: nunca lleva HIIT", g.hiit_block is None)

# Aquí había un `check("semana 1 sin HIIT si start_week > 1", ... or start_week
# <= 1)`. Con `start_week: 1` en el config esa condición es cierta siempre, mire
# lo que mire: un OK que no puede fallar. Se sustituye por el interruptor, que
# sí se puede comprobar en los dos sentidos.
OFF = copy.deepcopy(CFG)
OFF.raw["hiit"]["enabled"] = False
check("con hiit.enabled = false no se añade bloque a ninguna rutina del ciclo",
      all(build_session(OFF, day, "green", rotation_routine=r,
                        program_start=start).hiit_block is None for r in CICLO))

# El camino de "sí se añade" se prueba sobre una copia con el interruptor puesto
# a mano, para que este apartado siga probando algo el día que se apague en el
# config.
#
# Esa copia llevaba también `calendar.active_variant = "summer"`, por lo mismo
# que el apartado G: sin cambiarse de variante no había ningún día en el que
# pedirle a `build_session` el dia_3, y el caso más interesante del HIIT es
# justo ese, la rutina que está en `never_routines`.
print("\n  --- con hiit.enabled = true ---")
HON = copy.deepcopy(CFG)
HON.raw["hiit"]["enabled"] = True
start5 = date(2026, 9, 7)
wk1 = start5
wk6 = start5 + timedelta(days=7 * 5)

for rkey in CICLO:
    # `base_n` sale de la copia CON EL HIIT APAGADO. Antes salía de `HON` con la
    # misma llamada que `s`, así que `len(s.exercises) > base_n` comparaba un
    # número consigo mismo y era siempre falso; de ahí el `or True` que llevaba
    # pegado el check y que lo dejaba pasando pasara lo que pasara.
    base_n = len(build_session(OFF, wk6, "green", rotation_routine=rkey,
                               program_start=start5).exercises)
    s = build_session(HON, wk6, "green", rotation_routine=rkey, program_start=start5)
    blk = (HON.raw["hiit"].get("blocks") or {}).get(rkey)
    expected = rkey in (h.get("allowed_routines") or []) and rkey not in (h.get("never_routines") or [])
    print(f"  {rkey:<8} semana 6 verde: bloque={s.hiit_block or '—':<12} "
          f"{len(s.exercises)} ejercicios (esperado HIIT: {expected})")
    check(f"{rkey}: HIIT {'añadido' if expected else 'no añadido'} en semana 6",
          (s.hiit_block is not None) == expected)
    if expected:
        check(f"{rkey}: el bloque suma ejercicios", len(s.exercises) > base_n,
              f"bloque {blk}: {base_n}→{len(s.exercises)}")
        check(f"{rkey}: los ejercicios del HIIT están al final",
              all(e["key"] in [x["key"] for x in HON.raw["routines"][s.hiit_block]["exercises"]]
                  for e in s.exercises[-len(HON.raw["routines"][s.hiit_block]["exercises"]):]))

    # La semana 1 lleva HIIT o no según lo que diga `start_week`, y no según lo
    # que dijera cuando se escribió esto. Aquí había un `s1.hiit_block is None`
    # a secas, de cuando `start_week` valía 5; con el HIIT ya activado y
    # empezando en la 1 el script marcaba FALLA por una regla que el config ya
    # no tiene. Un umbral copiado a mano es un umbral que caduca solo.
    sw = int(h.get("start_week", 1))
    s1 = build_session(HON, wk1, "green", rotation_routine=rkey, program_start=start5)
    espera_wk1 = expected and sw <= 1
    check(f"{rkey}: semana 1 {'con' if espera_wk1 else 'sin'} HIIT "
          f"(start_week={sw})",
          (s1.hiit_block is not None) == espera_wk1,
          next((n for n in s1.notes if "HIIT" in n), ""))
    sr = build_session(HON, wk6, "red", rotation_routine=rkey, program_start=start5)
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

# El `sys.exit` no estaba, y sin él este guión acumulaba fallos en `FAILS`, los
# imprimía y salía con 0: quien lo llamara desde una tubería o un cron veía un
# éxito. Contar los fallos y luego tirarlos es peor que no contarlos, porque
# desde fuera se parece exactamente a no tener ninguno.
sys.exit(1 if FAILS else 0)
