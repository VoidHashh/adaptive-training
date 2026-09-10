"""Smoke test de la progresión contra el config real."""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from app.config_loader import load_config
from app.engine.progression import apply_deload_volume, plan_progression
from app.engine.signals import Signals

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT / "config.yaml")
TODAY = date(2026, 9, 7)


def sig(lights: dict[int, str] | None = None,
        lumbar: dict[int, int] | None = None,
        **values) -> Signals:
    """`lights`/`lumbar` se indexan por días ATRÁS desde hoy.

    La lumbar de HOY va por defecto a 1 -sana- y se puede pisar pasando
    `lower_discomfort=`. No es cosmético: sin ese valor el freno
    `lumbar_bloquea_todo` no se puede evaluar y cierra la puerta, así que TODAS
    las secciones de abajo salían con "freno no evaluable" y ninguna enseñaba lo
    que su rótulo prometía. La de los techos decía "Techos y 0 kg" y lo que
    demostraba era la puerta cerrada.
    """
    hist: dict = {}
    if lights:
        hist["light"] = {TODAY - timedelta(days=k): v for k, v in lights.items()}
    if lumbar:
        hist["lower_discomfort"] = {TODAY - timedelta(days=k): v for k, v in lumbar.items()}
    values.setdefault("lower_discomfort", 1)
    return Signals(day=TODAY, values=values, history=hist)


ALL_CLEAN = {}  # se rellena por rutina


def run(title: str, routine: str, s: Signals, light: str,
        clean: int = 5, deload: bool = False, only: list[str] | None = None) -> None:
    routines = CFG.raw["routines"][routine]["exercises"]
    keys = [e["key"] for e in routines]
    plan = plan_progression(
        CFG, routine, s, light,
        compliance={k: True for k in keys},
        clean_sessions={k: clean for k in keys},
        deload_active=deload,
    )
    print(f"\n--- {title}")
    print(f"    puerta: {plan.gate_open} ({plan.gate_reason})")
    print(f"    series:  {plan.sets_allowed} ({plan.sets_reason})")
    print(f"    reps:    {plan.reps_allowed} ({plan.reps_reason})")
    for e in plan.exercises:
        if only and e.key not in only:
            continue
        if e.changed:
            print(f"      SUBE [{e.kind}] {e.text()}")
        elif only:
            print(f"      ---- {e.name}: {e.blocked_by}")
    if not plan.changes:
        print("      (ningún cambio)")
    # Se imprimen. Este bucle era `pass`: calculaba las líneas de los
    # ejercicios parados y las tiraba, igual que hacía el mensaje de la
    # mañana. Un diagnóstico que llama a la función y no enseña el resultado
    # confirma que la función existe, no que sirva de algo.
    for line in plan.stopped_lines():
        print(f"      PARADO  {line}")


print("=" * 78)
print("A. Todo verde, 5 sesiones limpias, semana limpia")
print("=" * 78)
run("dia_1", "dia_1", sig(), "green")
run("dia_2", "dia_2", sig(), "green")
run("dia_3", "dia_3", sig(), "green")

print()
print("=" * 78)
print("B. Detalle dia_2: cadena posterior en modo `sets`")
print("=" * 78)
run("dia_2 detalle", "dia_2", sig(), "green",
    only=["hip_thrust_barra", "curl_femoral_pie", "peso_muerto_smith",
          "jalon_al_pecho", "dead_bug", "plancha_lateral"])

print()
print("=" * 78)
print("C. Frenos del volumen")
print("=" * 78)
run("un ROJO hace 3 días", "dia_2", sig(lights={3: "red"}), "green",
    only=["hip_thrust_barra", "jalon_al_pecho"])
run("dos ÁMBAR", "dia_2", sig(lights={2: "amber", 5: "amber"}), "green",
    only=["hip_thrust_barra", "jalon_al_pecho"])
run("un solo ÁMBAR (no basta)", "dia_2", sig(lights={2: "amber"}), "green",
    only=["hip_thrust_barra"])
run("lumbar media 4", "dia_2",
    sig(lumbar={1: 4, 2: 4, 3: 4, 4: 4}), "green",
    only=["hip_thrust_barra"])
run("lumbar media 2 (pasa)", "dia_2",
    sig(lumbar={1: 2, 2: 2, 3: 2}), "green",
    only=["hip_thrust_barra"])

print()
print("=" * 78)
print("D. Frenos generales")
print("=" * 78)
run("semáforo ámbar", "dia_2", sig(), "amber")
run("lumbar hoy = 4 (bloquea todo)", "dia_2", sig(lower_discomfort=4), "green")
run("RPE de ayer = 8", "dia_2", sig(yesterday_rpe=8, yesterday_routine="dia_2"), "green")
run("semana de descarga", "dia_2", sig(), "green", deload=True)
run("0 sesiones limpias", "dia_2", sig(), "green", clean=0,
    only=["hip_thrust_barra", "jalon_al_pecho"])

print()
print("=" * 78)
print("E. Techos y 0 kg")
print("=" * 78)
ex = {"key": "t", "name": "Plancha al tope", "progression_type": "volume",
      "max_seconds": 30, "sets": [{"duration_s": 30}, {"duration_s": 30}]}
cfg2 = {**CFG.raw, "routines": {**CFG.raw["routines"], "tmp": {"exercises": [ex]}}}
plan = plan_progression(cfg2, "tmp", sig(), "green",
                        compliance={"t": True}, clean_sessions={"t": 5})
print(f"    techo: {plan.exercises[0].blocked_by} | ceilings={plan.ceilings}")
print(f"    parados: {plan.stopped_lines()}")

ex0 = {"key": "z", "name": "Sin peso", "progression_type": "double",
       "rep_range": [12, 15],
       "sets": [{"reps": 15, "weight_kg": 0}, {"reps": 15, "weight_kg": 0}]}
cfg3 = {**CFG.raw, "routines": {**CFG.raw["routines"], "tmp": {"exercises": [ex0]}}}
plan = plan_progression(cfg3, "tmp", sig(), "green",
                        compliance={"z": True}, clean_sessions={"z": 5})
print(f"    0 kg al tope: {plan.exercises[0].blocked_by}")

print()
print("=" * 78)
print("F. sets -> then (ya en el techo de series)")
print("=" * 78)
exs = {"key": "h", "name": "Hip thrust", "progression_type": "sets",
       "max_sets": 3, "then": "double", "rep_range": [10, 12], "increment_kg": 5,
       "sets": [{"type": "warmup", "reps": 10, "weight_kg": 40},
                {"reps": 12, "weight_kg": 60},
                {"reps": 12, "weight_kg": 60},
                {"reps": 12, "weight_kg": 60}]}
cfg4 = {**CFG.raw, "routines": {**CFG.raw["routines"], "tmp": {"exercises": [exs]}}}
plan = plan_progression(cfg4, "tmp", sig(), "green",
                        compliance={"h": True}, clean_sessions={"h": 5})
e = plan.exercises[0]
print(f"    modo={e.mode} changed={e.changed} kind={e.kind}")
print(f"    telegram: {e.text()}")

print()
print("=" * 78)
print("H. Bloque de progresión del día (dia_2, todo verde)")
print("=" * 78)
# El rótulo decía "Mensaje de Telegram completo" y era falso: el mensaje de
# verdad lo arma `render_telegram`, y de estas líneas solo pintaba las subidas.
# Las de los ejercicios parados se veían AQUÍ y en ningún otro sitio, así que
# este script era la prueba de que el aviso funcionaba y a la vez el motivo de
# que nadie mirase si llegaba. Ahora sí llega; para ver el mensaje entero,
# `scripts/smoke_decision.py`.
keys2 = [e["key"] for e in CFG.raw["routines"]["dia_2"]["exercises"]]
p = plan_progression(CFG, "dia_2", sig(), "green",
                     compliance={k: True for k in keys2},
                     clean_sessions={k: 5 for k in keys2})
for line in p.text_lines():
    print(f"    · {line}")

print()
print("=" * 78)
print("G. Recorte de volumen en descarga")
print("=" * 78)
src = CFG.raw["routines"]["dia_2"]["exercises"][0]
out = apply_deload_volume(src, CFG.raw["progression"], CFG.raw["set_types"])
print(f"    antes: {src['sets']}")
print(f"    ahora: {out['sets']}")
src2 = CFG.raw["routines"]["dia_2"]["exercises"][-1]
out2 = apply_deload_volume(src2, CFG.raw["progression"], CFG.raw["set_types"])
print(f"    {src2['key']} antes: {src2['sets']}")
print(f"    {src2['key']} ahora: {out2['sets']}")
