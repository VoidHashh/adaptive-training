"""Inserta `progression_type` y sus parámetros en cada ejercicio del config.

Reescritura a nivel de TEXTO, anclada en las líneas `- key:` / `sets:`, no un
round-trip de YAML: un round-trip destruiría los comentarios, que son medio
entregable.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

CFG = Path(__file__).resolve().parents[1] / "config.yaml"

# (rutina, key) -> líneas a insertar justo antes de `sets:`
SPEC: dict[tuple[str, str], list[str]] = {}


def d(routine, key, *lines):
    SPEC[(routine, key)] = list(lines)


# --- dia_1 -----------------------------------------------------------------
d("dia_1", "prensa_horizontal",
  "progression_type: double",
  "rep_range: [10, 12]")
d("dia_1", "extension_cuadriceps",
  "progression_type: double",
  "rep_range: [10, 12]")
d("dia_1", "patada_atras",
  "# Cadena posterior: primero series, después carga. NO estaba en la lista",
  "# de tres que diste (hip thrust, peso muerto, curl femoral), pero es",
  "# cadena posterior igual. Si prefieres que aquí mande la carga, cambia",
  "# esta línea a `double` y no hay que tocar nada más.",
  "progression_type: sets",
  "max_sets: 4",
  "then: double",
  "rep_range: [20, 24]")
d("dia_1", "gemelo_sentado",
  "progression_type: double",
  "rep_range: [12, 15]")
d("dia_1", "press_triceps_sentado",
  "# Las cuatro series a 0 kg. En `double` eso significa que subirá reps",
  "# hasta 15 y ahí se quedará: el motor NO inventa una carga inicial",
  "# partiendo de cero (ver `_can_raise_load`). En cuanto registres el peso",
  "# real en Hevy, la progresión de carga se activa sola.",
  "progression_type: double",
  "rep_range: [12, 15]")
d("dia_1", "elevacion_cadera_unilateral",
  "progression_type: volume",
  "max_reps: 30")
d("dia_1", "plancha_lateral",
  "progression_type: volume",
  "max_seconds: 45")
d("dia_1", "perro_de_caza",
  "progression_type: volume",
  "max_reps: 30")
d("dia_1", "bosu_propiocepcion",
  "# Progresa por tiempo, no por el lastre: en propiocepción aguantar más",
  "# es el objetivo, y más peso solo cambia el ejercicio.",
  "progression_type: volume",
  "max_seconds: 45")

# --- dia_2 -----------------------------------------------------------------
d("dia_2", "hip_thrust_barra",
  "progression_type: sets",
  "max_sets: 5",
  "then: double",
  "rep_range: [10, 12]")
d("dia_2", "curl_femoral_pie",
  "progression_type: sets",
  "max_sets: 5",
  "then: double",
  "rep_range: [10, 12]")
d("dia_2", "peso_muerto_smith",
  "# Techo de series más bajo que el resto de la cadena posterior: es el",
  "# ejercicio que vigila `retirada_peso_muerto`, y aquí el volumen tampoco",
  "# es gratis.",
  "progression_type: sets",
  "max_sets: 4",
  "then: double",
  "rep_range: [10, 12]")
d("dia_2", "jalon_al_pecho",
  "progression_type: double",
  "rep_range: [10, 12]")
d("dia_2", "remo_t_apoyado",
  "progression_type: double",
  "rep_range: [10, 12]")
d("dia_2", "face_pull",
  "progression_type: double",
  "rep_range: [12, 15]")
d("dia_2", "dead_bug",
  "progression_type: volume",
  "max_reps: 30")
d("dia_2", "pallof_press",
  "progression_type: volume",
  "max_reps: 30")
d("dia_2", "plancha_lateral",
  "progression_type: volume",
  "max_seconds: 60")

# --- dia_3 -----------------------------------------------------------------
for k in ("abduccion_cadera", "aduccion_cadera", "press_hombro_maquina",
          "contractora_pecho", "vuelos_posteriores", "curl_predicador",
          "extension_triceps_polea"):
    d("dia_3", k, "progression_type: double", "rep_range: [10, 12]")
d("dia_3", "perro_de_caza", "progression_type: volume", "max_reps: 30")
d("dia_3", "dead_bug", "progression_type: volume", "max_reps: 30")
d("dia_3", "plancha_lateral", "progression_type: volume", "max_seconds: 45")
d("dia_3", "plancha", "progression_type: volume", "max_seconds: 60")

# --- HIIT ------------------------------------------------------------------
# Los intervalos del HIIT son estructura, no dosis: 30-30 y 40-20 están así
# porque el bloque dura lo que dura. Alargarlos deja de ser HIIT. Solo la
# plancha progresa, que es lo que pediste.
d("hiit_dia_1", "plancha_frontal", "progression_type: volume", "max_seconds: 60")
for r, k in (("hiit_dia_1", "remo_maquina"), ("hiit_dia_1", "suitcase_carry"),
             ("hiit_dia_1", "air_bike"), ("hiit_dia_1", "wall_ball"),
             ("hiit_dia_2", "battle_ropes"), ("hiit_dia_2", "comba"),
             ("hiit_dia_2", "ski_erg"), ("hiit_dia_2", "step_up")):
    d(r, k, "progression_type: none")


def main() -> int:
    lines = CFG.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    routine: str | None = None
    key: str | None = None
    applied: set[tuple[str, str]] = set()

    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        # Cabecera de rutina: dos niveles de indentación dentro de `routines:`.
        if indent == 2 and stripped.endswith(":") and not stripped.startswith("-"):
            routine = stripped[:-1]
        if stripped.startswith("- key:"):
            key = stripped.split("- key:", 1)[1].strip()

        if stripped in ("sets:",) or stripped.startswith("sets: ["):
            spec = SPEC.get((routine or "", key or ""))
            if spec and (routine, key) not in applied:
                pad = " " * indent
                out.extend(pad + s for s in spec)
                applied.add((routine, key))  # type: ignore[arg-type]
        out.append(line)

    missing = set(SPEC) - applied
    if missing:
        print("NO APLICADOS:", sorted(missing))
        return 1

    CFG.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"OK: {len(applied)} ejercicios anotados")
    return 0


raise SystemExit(main())
