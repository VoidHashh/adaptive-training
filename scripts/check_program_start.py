"""Comprueba dos cosas sobre `program.start`.

1. Que el cargador se NIEGA A ARRANCAR si falta, está en null, lleva hora o
   está entrecomillado. No que avise: que falle.
2. Que con la fecha puesta la semana de descarga se programa de verdad, cada
   `every_n_weeks` a partir de ese origen, y que el jitter la retrasa como
   mucho una semana.

Se ejecuta contra el config.yaml real, no contra un fixture: lo que interesa es
que la configuración de verdad produzca descargas de verdad.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import copy

import yaml

from app.config_loader import Config, ConfigError, _validate, compute_hash, load_config
from app.engine.decision import EngineState, decide
from app.engine.signals import Signals

RAW = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))


def espera_fallo(mutacion, etiqueta: str) -> None:
    data = copy.deepcopy(RAW)
    mutacion(data)
    problemas = [p for p in _validate(data) if "program.start" in p]
    assert problemas, f"{etiqueta}: el validador NO se quejó, y debería"
    print(f"  [ok] {etiqueta}")
    print(f"       -> {problemas[0][:110]}")


print("=" * 74)
print("  1. EL VALIDADOR SE NIEGA A ARRANCAR SIN program.start")
print("=" * 74)

espera_fallo(lambda d: d.__setitem__("program", {"start": None}), "start: null")
espera_fallo(lambda d: d.__setitem__("program", {}), "sección program vacía")
espera_fallo(lambda d: d.__setitem__("program", {"start": "2026-09-08"}),
             "fecha entrecomillada (llega como texto)")
espera_fallo(lambda d: d.__setitem__("program", {"start": "no soy una fecha"}),
             "texto que no es fecha")

# Y que la ausencia de la sección entera también para el arranque.
sin_seccion = copy.deepcopy(RAW)
del sin_seccion["program"]
problemas = _validate(sin_seccion)
assert any("program" in p for p in problemas), "falta program: no se detectó"
print("  [ok] sección 'program' ausente -> falta la sección obligatoria")

# El camino bueno.
cfg = load_config(Path("config.yaml"))
assert cfg.program_start == date(2026, 9, 8), cfg.program_start
print(f"\n  [ok] config.yaml real carga con program.start = {cfg.program_start}")


print()
print("=" * 74)
print("  2. LA DESCARGA SE PROGRAMA DE VERDAD")
print("=" * 74)

regla = next(r for r in cfg.raw["special_rules"] if r["name"] == "semana_de_descarga")
every = regla["trigger"]["every_n_weeks"]
jitter = regla["trigger"]["jitter_weeks"]
dur = regla["action"]["duration_days"]
print(f"  every_n_weeks={every}  jitter_weeks={jitter}  duration_days={dur}")
print(f"  -> ventana esperada entre descargas: {every} a {every + jitter} semanas")

# Simulación día a día durante un año, todo en verde (el caso menos favorable:
# si algo tuviera que disparar la descarga por síntomas, aquí no lo hará).
state = EngineState(program_start=cfg.program_start)
inicio = cfg.program_start
arranques: list[date] = []
dias_en_descarga = 0

for i in range(370):
    day = inicio + timedelta(days=i)
    sig = Signals(day=day)  # sin señales: nada dispara, solo el calendario
    d = decide(cfg, day, sig, state, source="check_program_start")
    if d.deload.active:
        dias_en_descarga += 1
        if d.deload.start == day:
            arranques.append(day)
    state = __import__(
        "app.engine.decision", fromlist=["advance_state"]
    ).advance_state(state, d)

print(f"\n  descargas en 370 días: {len(arranques)}")
for a in arranques:
    print(f"    · {a} ({['lun','mar','mié','jue','vie','sáb','dom'][a.weekday()]})"
          f" -> hasta {a + timedelta(days=dur - 1)}")

assert arranques, (
    "NINGUNA descarga en un año. Con program.start puesto esto no puede pasar."
)

huecos = [(b - a).days / 7 for a, b in zip(arranques, arranques[1:])]
print(f"\n  separación entre descargas (semanas): {[round(h, 1) for h in huecos]}")
for h in huecos:
    assert every <= h <= every + jitter + 0.01, (
        f"separación de {h} semanas fuera de la ventana [{every}, {every + jitter}]"
    )

# La cuenta se hace entre INICIOS DE SEMANA, no desde la fecha cruda. El origen
# es martes 2026-09-08 y las descargas siempre empiezan en lunes, así que medir
# desde el martes daría 6,86 semanas y parecería que la primera se adelanta.
# No se adelanta: cae en el lunes de la semana 7.
lunes_origen = inicio - timedelta(days=inicio.weekday())
primera = (arranques[0] - lunes_origen).days / 7
print(f"  origen {inicio} ({['lun','mar','mié','jue','vie','sáb','dom'][inicio.weekday()]})"
      f" -> lunes de esa semana: {lunes_origen}")
print(f"  primera descarga a las {primera:.0f} semanas de ese lunes")
assert every <= primera <= every + jitter + 0.01, primera
assert all(a.weekday() == 0 for a in arranques), (
    "alguna descarga no empieza en lunes: una descarga a mitad de semana parte "
    "en dos el microciclo y no descarga ninguna de las dos mitades del todo"
)

esperados = len(arranques) * dur
assert dias_en_descarga == esperados, (
    f"días en descarga {dias_en_descarga} != {esperados}: alguna descarga no "
    f"duró los {dur} días completos"
)
print(f"  [ok] {dias_en_descarga} días en descarga = {len(arranques)} × {dur}")

print()
print("=" * 74)
print("  TODO CORRECTO")
print("=" * 74)
