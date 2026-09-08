"""Smoke test del orquestador `decision.py` contra el config real.

Cubre lo que solo se puede romper en la juntura entre módulos: el orden de las
operaciones, la persistencia de las reglas especiales entre días, el ámbito
rutina+ejercicio del estado y el avance del estado tras ejecutar la sesión.

Todo lo que se afirma aquí se comprueba con `assert`. Un smoke test que solo
imprime es un test que pasa siempre.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

import copy

from app.config_loader import load_config
from app.engine.decision import EngineState, advance_state, decide
from app.engine.signals import Signals

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT / "config.yaml")

# Lunes. Con la variante `with_pool` activa: lunes=dia_1, jueves=dia_2.
MON = date(2026, 9, 7)
THU = MON + timedelta(days=3)

fallos: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"    [{'ok ' if cond else 'FALLO'}] {label}")
    if not cond:
        fallos.append(label)


def sig(day: date, hist: dict[str, dict[date, object]] | None = None, **values) -> Signals:
    return Signals(day=day, values=values, history=hist or {})


def summer_cfg():
    """Copia del config con la variante que sí entrena las tres rutinas."""
    c = copy.deepcopy(CFG)
    c.raw["calendar"]["active_variant"] = "summer"
    return c


def head(t: str) -> None:
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


# ---------------------------------------------------------------------------
head("A. Día verde con fuerza (lunes, dia_1)")
d = decide(CFG, MON, sig(MON), EngineState())
print(f"    semáforo={d.light}  sesión={d.session.kind}  rutina={d.session.routine_key}")
print(f"    calendar_routine={d.calendar_routine}")
print(f"    bici: {d.bike.level} — {d.bike.label}")
check(d.light == "green", "sin señales malas el semáforo es verde")
check(d.session.kind == "full", "sesión completa")
check(d.session.routine_key == "dia_1", "rutina dia_1")
check(d.calendar_routine == "dia_1", "calendar_routine registrado")
check(d.progression is not None, "hay plan de progresión")
check(d.bike is not None, "hay recomendación de bici")
check(not d.deload.active, "no es semana de descarga")

# ---------------------------------------------------------------------------
head("B. Día ámbar: sesión reducida y progresión cerrada")
d = decide(CFG, MON, sig(MON, upper_discomfort=6), EngineState())
print(f"    semáforo={d.light} ({d.trigger_rule})  sesión={d.session.kind}")
check(d.light == "amber", "molestia superior 6 -> ámbar")
check(d.session.kind == "reduced", "sesión reducida")
check(d.progression is not None and not d.progression.gate_open,
      "la puerta de progresión está cerrada en ámbar")

# ---------------------------------------------------------------------------
head("C. Día rojo: recuperación y fuerza aplazada")
d = decide(CFG, MON, sig(MON, lower_discomfort=7), EngineState())
print(f"    semáforo={d.light} ({d.trigger_rule})  sesión={d.session.kind}")
check(d.light == "red", "lumbar 7 -> rojo")
check(d.session.kind == "recovery", "bloque de recuperación")
st = advance_state(EngineState(), d, executed={})
print(f"    pending_strength={st.pending_strength}")
check(st.pending_strength == ("dia_1", MON), "dia_1 queda pendiente, no se pierde")

# ---------------------------------------------------------------------------
head("D. La sesión aplazada se recupera en el siguiente día libre y verde")
tue = MON + timedelta(days=1)  # martes = descanso
d2 = decide(CFG, tue, sig(tue), st)
print(f"    sesión={d2.session.kind}  rutina={d2.session.routine_key}")
print(f"    deferred_from={d2.session.deferred_from}")
check(d2.session.routine_key == "dia_1", "se recupera dia_1 en el martes de descanso")
check(d2.session.deferred_from == MON, "queda registrado de qué día venía")
st2 = advance_state(st, d2, executed={})
check(st2.pending_strength is None, "el pendiente se consume al recuperarlo")

# ---------------------------------------------------------------------------
head("E. Regla especial: retirada de peso muerto (lumbar >=5 dos días)")
hist = {"lower_discomfort": {THU: 5, THU - timedelta(days=1): 5}}
d = decide(CFG, THU, sig(THU, lower_discomfort=5, **{}), EngineState())
# el histórico hay que pasarlo para el `consecutive_days`
d = decide(CFG, THU, Signals(day=THU, values={"lower_discomfort": 5}, history=hist),
           EngineState())
nombres = [r.name for r in d.active_rules]
print(f"    reglas activas: {nombres}")
print(f"    semáforo={d.light} ({d.trigger_rule})")
keys = [e.get("key") for e in d.session.exercises]
check("retirada_peso_muerto" in nombres, "la regla dispara con dos días a 5")
check("peso_muerto_smith" not in keys, "el peso muerto sale de la sesión")
regla = next(r for r in d.active_rules if r.name == "retirada_peso_muerto")
print(f"    vigente de {regla.active_from} a {regla.active_until}")
check((regla.active_until - regla.active_from).days == 13, "14 días de vigencia")

# ---------------------------------------------------------------------------
head("F. La regla sobrevive a que hoy el dolor sea 0")
st_rule = advance_state(EngineState(), d, executed=None)
later = THU + timedelta(days=7)
d3 = decide(CFG, later, sig(later, lower_discomfort=0), st_rule)
nombres = [r.name for r in d3.active_rules]
print(f"    {later}: semáforo={d3.light}  reglas activas={nombres}")
check("retirada_peso_muerto" in nombres,
      "sigue vigente 7 días después aunque hoy no duela nada")

# ---------------------------------------------------------------------------
head("G. ...y caduca al día 15")
expired = THU + timedelta(days=14)
d4 = decide(CFG, expired, sig(expired, lower_discomfort=0), st_rule)
nombres = [r.name for r in d4.active_rules]
print(f"    {expired}: reglas activas={nombres}")
check("retirada_peso_muerto" not in nombres, "caducada al día 15")

# ---------------------------------------------------------------------------
head("H. Recorte de carga por regla especial (factor exacto)")
# OJO con lo que este bloque demuestra y lo que no. `upper_discomfort: 6`
# dispara a la vez el ámbar (`cervicales_hombros`) y la regla de hombro, así
# que aquí la progresión está cerrada de todos modos y este caso NO puede
# probar que el recorte va después de la progresión. Eso lo prueba
# `smoke_session.py` sección G, que sí construye el caso con progresión activa.
# Aquí lo que se comprueba es que la regla llega hasta la sesión y aplica su
# factor exacto.
cfg_s = summer_cfg()
fri = date(2026, 9, 11)  # viernes -> dia_3 en la variante summer
press_before = None
for ex in cfg_s.raw["routines"]["dia_3"]["exercises"]:
    if ex["key"] == "press_hombro_maquina":
        press_before = [s.get("weight_kg") for s in ex["sets"]]
d = decide(cfg_s, fri, sig(fri, upper_discomfort=6), EngineState())
nombres = [r.name for r in d.active_rules]
press_after = None
for ex in d.session.exercises:
    if ex.get("key") == "press_hombro_maquina":
        press_after = [s.get("weight_kg") for s in ex["sets"]]
print(f"    reglas activas: {nombres}")
print(f"    press antes:  {press_before}")
print(f"    press ahora:  {press_after}")
check("descarga_press_hombro" in nombres, "la regla de hombro dispara con 6")
check(press_after is not None and press_after[0] == 10 * 0.70,
      "la primera serie está exactamente al 70% (10 -> 7,0)")
check(d.light == "amber" and len(press_after) < len(press_before),
      "y además el ámbar ha recortado series (efecto acumulado, no alternativo)")

# ---------------------------------------------------------------------------
head("I. Semana de descarga: congela la progresión")
start = MON - timedelta(weeks=7)
st_dl = EngineState(program_start=start)
d = decide(CFG, MON, sig(MON), st_dl)
print(f"    descarga activa={d.deload.active}")
print(f"    motivo: {d.deload.reason}")
check(d.deload.active, "a las 7 semanas del inicio toca descarga")
check(d.progression is not None and not d.progression.gate_open,
      "la descarga congela la progresión")
check(not d.progression.changes, "y por tanto no cambia nada")

d_no = decide(CFG, MON - timedelta(weeks=1), sig(MON - timedelta(weeks=1)), st_dl)
print(f"    semana anterior: activa={d_no.deload.active} ({d_no.deload.reason})")
check(not d_no.deload.active, "la semana 6 no es de descarga")

# La descarga tiene que durar la semana entera, no solo el lunes.
st_run = advance_state(st_dl, d, executed=None)
print(f"    last_deload_start guardado = {st_run.last_deload_start}")
check(st_run.last_deload_start == MON, "se recuerda cuándo empezó")
dias = []
for i in range(1, 7):
    dd = decide(CFG, MON + timedelta(days=i), sig(MON + timedelta(days=i)), st_run)
    dias.append(dd.deload.active)
print(f"    resto de la semana activa: {dias}")
check(all(dias), "la descarga cubre los 7 días, no solo el día que dispara")

# ...y no se repite a la semana siguiente.
nxt = MON + timedelta(weeks=1)
d_nxt = decide(CFG, nxt, sig(nxt), st_run)
print(f"    semana siguiente: activa={d_nxt.deload.active} ({d_nxt.deload.reason})")
check(not d_nxt.deload.active, "no se encadena una segunda descarga")

# Jitter: si la semana que tocaba arranca en ROJO, se retrasa.
d_red = decide(CFG, MON, sig(MON, lower_discomfort=7), EngineState(program_start=start))
print(f"    lunes rojo: activa={d_red.deload.active} shifted={d_red.deload.shifted}")
check(d_red.light == "red", "el lunes es rojo")
check(not d_red.deload.active and d_red.deload.shifted,
      "la descarga se retrasa: descargar sobre una semana ya frenada no descarga")

# ---------------------------------------------------------------------------
head("J. Avance del estado: rachas por RUTINA+EJERCICIO")
cfg_s = summer_cfg()
st = EngineState()
mon = date(2026, 9, 7)   # dia_1
wed = date(2026, 9, 9)   # dia_2
d_mon = decide(cfg_s, mon, sig(mon), st)
st = advance_state(st, d_mon, executed={e["key"]: True for e in d_mon.session.exercises})
d_wed = decide(cfg_s, wed, sig(wed), st)
st = advance_state(st, d_wed, executed={e["key"]: True for e in d_wed.session.exercises})

planchas = {k: v for k, v in st.clean_sessions.items() if "plancha_lateral" in k[1]}
print(f"    rachas de plancha_lateral: {planchas}")
check(len(planchas) >= 2, "la plancha lateral tiene una racha por cada rutina")
check(all(isinstance(k, tuple) and len(k) == 2 for k in st.clean_sessions),
      "todas las claves son (rutina, ejercicio)")
print(f"    last_routine_light = {st.last_routine_light}")
check(st.last_routine_light.get("dia_1") == "green", "se recuerda el semáforo de dia_1")

# racha rota
d_mon2 = decide(cfg_s, mon + timedelta(days=7), sig(mon + timedelta(days=7)), st)
falla = next(e["key"] for e in d_mon2.session.exercises)
exe = {e["key"]: True for e in d_mon2.session.exercises}
exe[falla] = False
antes = st.clean_sessions.get(("dia_1", falla), 0)
st3 = advance_state(st, d_mon2, executed=exe)
print(f"    '{falla}': racha {antes} -> {st3.clean_sessions.get(('dia_1', falla))}")
check(st3.clean_sessions.get(("dia_1", falla)) == 0,
      "una serie sin completar resetea la racha entera, no la decrementa")

# ---------------------------------------------------------------------------
head("K. El estado no avanza si la sesión aún no se ha ejecutado")
st_pre = EngineState()
d_pre = decide(cfg_s, mon, sig(mon), st_pre)
st_post = advance_state(st_pre, d_pre, executed=None)
check(st_post.clean_sessions == {},
      "a las 7 de la mañana la decisión existe pero la racha no ha avanzado")
check(st_post.last_routine_light == {}, "ni el semáforo de la rutina")

# ---------------------------------------------------------------------------
head("L. to_dict serializable a JSON")
d = decide(CFG, MON, sig(MON), EngineState())
try:
    blob = json.dumps(d.to_dict(), ensure_ascii=False)
    print(f"    {len(blob)} bytes de JSON")
    check(True, "to_dict() es JSON-serializable")
    reloaded = json.loads(blob)
    check(reloaded["light"] == "green", "el JSON conserva el semáforo")
    check("inputs" in reloaded, "incluye la fotografía de señales")
except TypeError as exc:
    check(False, f"to_dict() no serializa: {exc}")

# ---------------------------------------------------------------------------
print()
print("=" * 78)
if fallos:
    print(f"{len(fallos)} FALLOS:")
    for f in fallos:
        print(f"  - {f}")
    sys.exit(1)
print("TODO OK")
