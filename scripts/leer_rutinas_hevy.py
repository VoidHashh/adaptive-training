"""Relee las rutinas de fuerza de Hevy por API y las compara con config.yaml.

Solo LEE (GET). No escribe en Hevy ni en el config: imprime lo que hay allí y
lo que hay aquí, marca cada ejercicio con `=` o `≠` y señala las series sin
peso o sin reps.

POR QUÉ ESTO ES UNA HERRAMIENTA Y NO UN APAÑO DE UN DÍA: el PUT de rutinas de
Hevy REEMPLAZA la rutina entera, y el punto de partida de lo que se escribe es
este config. Si el config se queda atrás respecto a lo que hay en la app -por
ejemplo porque apuntaste a mano las cargas reales de unos ejercicios que
estaban a 0 kg- el primer día que `integrations.hevy.write_enabled` se ponga en
true esos pesos se van por el desagüe sin que nadie diga nada. La deriva entre
los dos lados es silenciosa por construcción; esto es lo que la hace visible.

Conviene pasarlo siempre que se toquen las rutinas en la app, y desde luego
antes de encender la escritura.

    ./.venv/Scripts/python.exe scripts/leer_rutinas_hevy.py

Lo que salga marcado `≠` hay que llevarlo al config a mano: este script NO
escribe en el YAML a propósito, porque esos bloques llevan comentarios que
explican cada decisión y un regenerador automático se los llevaría por delante.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config_loader import load_config  # noqa: E402
from app.integrations.hevy import HevyClient  # noqa: E402

FUERZA = ("dia_1", "dia_2", "dia_3")


def env(clave: str) -> str:
    for linea in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if linea.strip().startswith(f"{clave}="):
            return linea.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get(clave, "")


def main() -> None:
    cfg = load_config(ROOT / "config.yaml")
    routines = cfg.raw.get("routines", {})
    cli = HevyClient(api_key=env("HEVY_API_KEY"), data_root=ROOT / "data")

    for clave in FUERZA:
        definicion = routines.get(clave, {})
        rid = definicion.get("hevy_routine_id")
        print("=" * 78)
        print(f"{clave}  —  {definicion.get('title')}  ({rid})")
        print("=" * 78)
        try:
            remota = cli.get_routine(rid)
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR AL LEER: {exc}")
            continue

        locales = {e["key"]: e for e in definicion.get("exercises", [])}
        por_template: dict[str, dict] = {}
        for e in locales.values():
            por_template[str(e.get("template_id"))] = e

        for ej in remota.get("exercises", []):
            tid = str(ej.get("exercise_template_id"))
            nombre = ej.get("title") or tid
            sets = ej.get("sets", []) or []
            piezas = []
            for s in sets:
                w = s.get("weight_kg")
                r = s.get("reps")
                t = s.get("type", "normal")
                marca = "w" if t == "warmup" else ""
                piezas.append(f"{marca}{r if r is not None else '·'}x{w if w is not None else '·'}")
            loc = por_template.get(tid)
            k = loc.get("key") if loc else "NO ESTÁ EN CONFIG"
            prog = loc.get("progression_type") if loc else "-"
            limpio = loc.get("clean_sessions_required") if loc else None
            sin_peso = [s for s in sets if not s.get("weight_kg")]
            sin_reps = [s for s in sets if not s.get("reps")]
            bandera = ""
            if sin_peso:
                bandera += f"  << {len(sin_peso)}/{len(sets)} SIN PESO"
            if sin_reps:
                bandera += f"  << {len(sin_reps)}/{len(sets)} SIN REPS"
            print(f"  {nombre[:38]:<38} [{k}]")
            print(f"      remoto : {' '.join(piezas)}{bandera}")
            if loc:
                lp = []
                for s in loc.get("sets", []):
                    marca = "w" if s.get("type") == "warmup" else ""
                    lp.append(f"{marca}{s.get('reps', '·')}x{s.get('weight_kg', '·')}")
                igual = "=" if lp == piezas else "≠"
                print(f"      config : {' '.join(lp)}   [{igual}] prog={prog}"
                      + (f" clean={limpio}" if limpio else ""))
        print()


if __name__ == "__main__":
    main()
