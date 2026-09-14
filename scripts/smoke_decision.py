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
CICLO = CFG.rotation_order()

# Dos fechas cualesquiera. Llevan nombre de día de la semana por costumbre, pero
# ya no significan nada: qué rutina toca no lo decide el calendario, lo decide
# cuál fue la última que apareció EJECUTADA en Hevy. Lo que gobierna estos
# apartados es el `EngineState` que se les pasa, no el día.
MON = date(2026, 9, 7)
THU = MON + timedelta(days=3)

fallos: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"    [{'ok ' if cond else 'FALLO'}] {label}")
    if not cond:
        fallos.append(label)


def sig(day: date, hist: dict[str, dict[date, object]] | None = None, **values) -> Signals:
    return Signals(day=day, values=values, history=hist or {})


def tras(routine_key: str, day: date = MON, **kw) -> EngineState:
    """El estado de quien acaba de ejecutar `routine_key`.

    Sustituye a un `summer_cfg()` que copiaba el config y le cambiaba
    `calendar.active_variant` a `"summer"` porque la variante activa
    (`with_pool`) no programaba `dia_3` ningún día de la semana. Ese apaño era
    la única forma que tenía este script de llegar al Día 3, y su existencia era
    el aviso -no leído- de que las sesiones reales de Día 3 no cuadraban con
    ningún plan. Ahora al ciclo se le dice por dónde va y ya está.
    """
    return EngineState(last_strength=(routine_key, day), **kw)


def anterior(routine_key: str) -> str:
    """La rutina que hay que haber ejecutado para que hoy toque `routine_key`."""
    i = CICLO.index(routine_key)
    return CICLO[i - 1]


def head(t: str) -> None:
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


# ---------------------------------------------------------------------------
head("A. Día verde con fuerza: sin nada leído de Hevy, el ciclo empieza por el principio")
d = decide(CFG, MON, sig(MON), EngineState())
print(f"    semáforo={d.light}  sesión={d.session.kind}  rutina={d.session.routine_key}")
print(f"    rotation_routine={d.rotation_routine}  last_strength={d.last_strength}")
print(f"    bici: {d.bike.level} — {d.bike.label}")
check(d.light == "green", "sin señales malas el semáforo es verde")
check(d.session.kind == "full", "sesión completa")
check(d.session.routine_key == CICLO[0], f"rutina {CICLO[0]}, la primera del ciclo")
check(d.rotation_routine == CICLO[0], "rotation_routine registrado en la decisión")
check(d.last_strength is None,
      "y se deja escrito que no había ninguna leída: el mensaje lo necesita "
      "para no inventarse un 'hace 0 días'")
check(d.progression is not None, "hay plan de progresión")

# El ciclo entero, pidiendo cada escalón por su estado anterior.
for rkey in CICLO:
    dd = decide(CFG, MON, sig(MON), tras(anterior(rkey)))
    check(dd.session.routine_key == rkey,
          f"tras {anterior(rkey)} toca {rkey} (salió {dd.session.routine_key})")
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
head("C. Día rojo: recuperación, y el puntero del ciclo no se mueve")
st_prev = tras("dia_1", MON - timedelta(days=3))   # venimos de haber hecho dia_1
d = decide(CFG, MON, sig(MON, lower_discomfort=7), st_prev)
print(f"    semáforo={d.light} ({d.trigger_rule})  sesión={d.session.kind}")
print(f"    rotation_routine={d.rotation_routine}")
check(d.light == "red", "lumbar 7 -> rojo")
check(d.session.kind == "recovery", "bloque de recuperación")
check(d.rotation_routine == "dia_2", "el ciclo sigue diciendo que tocaría dia_2")
st = advance_state(st_prev, d, executed={})
print(f"    last_strength={st.last_strength}")
check(st.last_strength == ("dia_1", MON - timedelta(days=3)),
      "el bloque de recuperación NO cuenta como sesión del ciclo: el puntero "
      "se queda en dia_1 y dia_2 sigue siendo el siguiente")

# ---------------------------------------------------------------------------
head("D. El aplazamiento no se ha quitado: se ha vuelto la conducta por defecto")
# Aquí había un apartado titulado "la sesión aplazada se recupera en el
# siguiente día libre y verde", que construía el día siguiente con el
# `pending_strength` que había dejado el rojo y comprobaba que se consumía. Ya
# no hay nada que consumir: el puntero solo avanza con una sesión EJECUTADA, así
# que no hacer nada lo deja donde estaba, indefinidamente y sin fecha de
# caducidad. Lo que antes era una máquina -guardar, recuperar, caducar- ahora es
# la ausencia de máquina, y eso se comprueba dejando pasar los días.
seguidos = []
for i in range(1, 15):
    dd = decide(CFG, MON + timedelta(days=i), sig(MON + timedelta(days=i)), st)
    seguidos.append(dd.rotation_routine)
print(f"    catorce días sin pisar el gimnasio: {sorted(set(seguidos))}")
check(set(seguidos) == {"dia_2"},
      "dos semanas después sigue tocando dia_2, sin estado que guardar")

# Y en cuanto se ejecuta de verdad, avanza.
d2 = decide(CFG, MON + timedelta(days=14), sig(MON + timedelta(days=14)), st)
st2 = advance_state(st, d2, executed={e["key"]: True for e in d2.session.exercises})
print(f"    tras ejecutarla: last_strength={st2.last_strength}")
check(st2.last_strength == ("dia_2", MON + timedelta(days=14)),
      "ejecutarla sí mueve el puntero")
check(decide(CFG, MON + timedelta(days=15), sig(MON + timedelta(days=15)),
             st2).rotation_routine == "dia_3",
      "y al día siguiente ya toca dia_3")

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
fri = date(2026, 9, 11)
press_before = None
for ex in CFG.raw["routines"]["dia_3"]["exercises"]:
    if ex["key"] == "press_hombro_maquina":
        press_before = [s.get("weight_kg") for s in ex["sets"]]
# Se llega al Día 3 diciendo que la última ejecutada fue la anterior del ciclo.
d = decide(CFG, fri, sig(fri, upper_discomfort=6), tras(anterior("dia_3"), fri - timedelta(days=2)))
check(d.session.routine_key == "dia_3", f"la sesión es dia_3 ({d.session.routine_key})")
nombres = [r.name for r in d.active_rules]
press_after = None
for ex in d.session.exercises:
    if ex.get("key") == "press_hombro_maquina":
        press_after = [s.get("weight_kg") for s in ex["sets"]]
print(f"    reglas activas: {nombres}")
print(f"    press antes:  {press_before}")
print(f"    press ahora:  {press_after}")
check("descarga_press_hombro" in nombres, "la regla de hombro dispara con 6")
# El factor y el peso de partida salen del config, no de una constante. Aquí
# ponía `press_after[0] == 10 * 0.70`, con el 10 copiado a mano de cuando la
# primera serie del press pesaba 10 kg. Ahora pesa 12,5 y el check marcaba FALLO
# por un peso que había subido -o sea, por la única cosa que se quiere que pase-.
_factor = float(next(r for r in CFG.raw["special_rules"]
                     if r["name"] == "descarga_press_hombro")["action"]["reduce_load"]["factor"])
_esperado = round(press_before[0] * _factor * 2) / 2
check(press_after is not None and press_after[0] == _esperado,
      f"la primera serie está exactamente al {int(_factor * 100)}% "
      f"({press_before[0]} -> {_esperado})")
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
# Aquí había otra `summer_cfg()` y dos fechas elegidas para caer en dia_1 y
# dia_2. No hace falta ninguna de las dos: se entrena dos veces seguidas y el
# ciclo avanza solo, que es justo lo que se quiere demostrar de paso.
st = EngineState()
mon = date(2026, 9, 7)
wed = date(2026, 9, 9)
d_mon = decide(CFG, mon, sig(mon), st)
st = advance_state(st, d_mon, executed={e["key"]: True for e in d_mon.session.exercises})
d_wed = decide(CFG, wed, sig(wed), st)
st = advance_state(st, d_wed, executed={e["key"]: True for e in d_wed.session.exercises})
print(f"    dos sesiones seguidas: {d_mon.session.routine_key} -> {d_wed.session.routine_key}")
check([d_mon.session.routine_key, d_wed.session.routine_key] == CICLO[:2],
      "entrenar dos veces avanza el ciclo sin que nadie le diga la fecha")

planchas = {k: v for k, v in st.clean_sessions.items() if "plancha_lateral" in k[1]}
print(f"    rachas de plancha_lateral: {planchas}")
check(len(planchas) >= 2, "la plancha lateral tiene una racha por cada rutina")
check(all(isinstance(k, tuple) and len(k) == 2 for k in st.clean_sessions),
      "todas las claves son (rutina, ejercicio)")
print(f"    last_routine_light = {st.last_routine_light}")
check(st.last_routine_light.get(d_mon.session.routine_key) == "green",
      f"se recuerda el semáforo de {d_mon.session.routine_key}")

# Racha rota. Hay que dar la vuelta entera al ciclo antes de poder romper nada:
# una racha solo existe donde ya se ha entrenado, y tras dos sesiones la
# siguiente es una rutina virgen con la racha a 0. Con el `"dia_1"` escrito a
# mano que había aquí, este apartado acababa comprobando que 0 seguía siendo 0.
d_3 = decide(CFG, mon + timedelta(days=4), sig(mon + timedelta(days=4)), st)
st = advance_state(st, d_3, executed={e["key"]: True for e in d_3.session.exercises})

d_mon2 = decide(CFG, mon + timedelta(days=7), sig(mon + timedelta(days=7)), st)
rk2 = d_mon2.session.routine_key
falla = next(e["key"] for e in d_mon2.session.exercises)
exe = {e["key"]: True for e in d_mon2.session.exercises}
exe[falla] = False
antes = st.clean_sessions.get((rk2, falla), 0)
st3 = advance_state(st, d_mon2, executed=exe)
print(f"    {rk2}/'{falla}': racha {antes} -> {st3.clean_sessions.get((rk2, falla))}")
check(antes > 0, f"hay una racha que romper en {rk2}/{falla} (vale {antes})")
check(st3.clean_sessions.get((rk2, falla)) == 0,
      "una serie sin completar resetea la racha entera, no la decrementa")

# ---------------------------------------------------------------------------
head("K. El estado no avanza si la sesión aún no se ha ejecutado")
st_pre = EngineState()
d_pre = decide(CFG, mon, sig(mon), st_pre)
st_post = advance_state(st_pre, d_pre, executed=None)
check(st_post.clean_sessions == {},
      "a las 7 de la mañana la decisión existe pero la racha no ha avanzado")
check(st_post.last_routine_light == {}, "ni el semáforo de la rutina")
check(st_post.last_strength is None,
      "ni el puntero del ciclo: planificar no es entrenar, y si avanzara aquí "
      "un día sin gimnasio se saltaría una rutina entera")

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
