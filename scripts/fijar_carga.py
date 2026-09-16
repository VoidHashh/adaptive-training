"""Poner a mano la carga vigente de un ejercicio, en el sitio que de verdad manda.

POR QUÉ EXISTE
--------------
La adopción no se traga cualquier salto: si un día aparece en Hevy un peso muy
por encima del objetivo vigente, se para y lo dice, porque un 600 tecleado en
vez de un 60 no puede convertirse en la rutina de mañana. Pero cuando el salto
es REAL -se levantaron de verdad los 60 en una extensión cuyo objetivo era 40-
el sistema se quedaba sin salida: rechazaba el mismo salto sesión tras sesión y
el mensaje aconsejaba «se sube a mano en config.yaml», que ya no era verdad.

No lo era porque `config.yaml` es el punto de PARTIDA de un ejercicio y nada
más. En cuanto ese ejercicio tiene una fila en `exercise_targets`, la carga
vigente sale de la columna `current_sets_json` y el YAML no se vuelve a mirar
(`app/engine/progression.py:con_carga_vigente`). Editar el fichero no habría
cambiado nada, y encima no habría dado ningún error: el consejo mandaba a tocar
una pieza que ya no decide.

Esto escribe en la columna que sí decide.

QUÉ TOCA Y QUÉ NO
-----------------
Toca las series EFECTIVAS. Los calentamientos salen del YAML en cada
construcción y la progresión tampoco los mueve nunca.

Borra la racha de sesiones por debajo (`below_plan_streak`, `below_plan_best_kg`)
porque esa racha se contaba contra el objetivo ANTERIOR: dejarla puesta
significaría que una bajada futura se decidiera sobre sesiones que ya no se
comparan con nada. No toca `clean_streak` ni `last_compliant`: el cumplimiento
de la última sesión pasó como pasó, y ponerlo a cero cerraría la puerta de la
progresión acusando de un fallo que nadie cometió.

Por defecto SIMULA. Escribe solo con `--aplicar`.

    python scripts/fijar_carga.py
    python scripts/fijar_carga.py dia_1 extension_cuadriceps
    python scripts/fijar_carga.py dia_1 extension_cuadriceps 50,55,60
    python scripts/fijar_carga.py dia_1 extension_cuadriceps 50,55,60 --aplicar
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import select

from app.config_loader import load_config
from app.db import init_db, session_scope
from app.engine.sets import warmup_flags
from app.models import ExerciseTarget
from app.settings import settings


class CargaInvalida(ValueError):
    """Lo que se ha pedido escribir no es una carga que se pueda entrenar."""


def nuevas_series(
    vigentes: list[dict[str, Any]], pesos: list[float]
) -> list[dict[str, Any]]:
    """Las series de siempre con los pesos nuevos. Las reps NO se tocan.

    Se exige que vengan tantos pesos como series efectivas hay. Aceptar menos y
    rellenar, o aceptar más y recortar, sería cambiar el ESQUEMA del ejercicio
    -de tres series a dos- con la excusa de cambiarle el peso, y un cambio así
    tiene que pedirse a la cara y no colarse por el número de comas.
    """
    if not vigentes:
        raise CargaInvalida(
            "ese ejercicio no tiene series efectivas de las que partir."
        )
    if len(pesos) != len(vigentes):
        raise CargaInvalida(
            f"tiene {len(vigentes)} series efectivas y se han dado {len(pesos)} "
            f"pesos. Esto cambia el peso, no el número de series."
        )
    if any(p < 0 for p in pesos):
        raise CargaInvalida("un peso negativo no es una carga.")
    salida = []
    for s, p in zip(vigentes, pesos, strict=True):
        nueva = dict(s)
        nueva["weight_kg"] = round(float(p), 3)
        salida.append(nueva)
    return salida


def efectivas_del_yaml(cfg, routine_key: str, exercise_key: str) -> list[dict[str, Any]]:
    """El punto de partida del fichero, para un ejercicio que aún no tiene fila."""
    rutina = (cfg.routines or {}).get(routine_key) or {}
    for ex in rutina.get("exercises") or []:
        if str(ex.get("key")) != exercise_key:
            continue
        sets = ex.get("sets") or []
        flags = warmup_flags(sets, cfg.set_types, exercise_key)
        return [s for s, f in zip(sets, flags, strict=True) if not f]
    return []


def _pinta(series: list[dict[str, Any]]) -> str:
    return " · ".join(
        f"{s.get('reps', '?')}×{(s.get('weight_kg') or 0):g} kg" for s in series
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Fijar a mano la carga de un ejercicio.")
    p.add_argument("rutina", nargs="?", help="clave de la rutina, p. ej. dia_1")
    p.add_argument("ejercicio", nargs="?", help="clave del ejercicio")
    p.add_argument(
        "pesos", nargs="?",
        help="pesos de las series efectivas separados por comas, p. ej. 50,55,60",
    )
    p.add_argument(
        "--aplicar", action="store_true",
        help="escribir de verdad (por defecto solo simula)",
    )
    args = p.parse_args(argv)

    cfg = load_config(settings.config_path)
    init_db()

    with session_scope() as s:
        filas = {
            (f.routine_key, f.exercise_key): f
            for f in s.scalars(select(ExerciseTarget)).all()
        }

        if not args.rutina:
            if not filas:
                print("No hay ninguna carga guardada todavía.")
                return 0
            print("Cargas vigentes en la base (lo que de verdad manda):\n")
            for (rk, ek), f in sorted(filas.items()):
                try:
                    series = json.loads(f.current_sets_json or "[]")
                except ValueError:
                    print(f"  {rk:>10} / {ek:<28} JSON ilegible")
                    continue
                print(f"  {rk:>10} / {ek:<28} {_pinta(series) or '—'}")
            return 0

        if not args.ejercicio:
            p.error("hace falta la clave del ejercicio.")

        clave = (args.rutina, args.ejercicio)
        fila = filas.get(clave)
        if fila is not None and fila.current_sets_json:
            vigentes = json.loads(fila.current_sets_json)
            origen = "base de datos"
        else:
            vigentes = efectivas_del_yaml(cfg, args.rutina, args.ejercicio)
            origen = "config.yaml (este ejercicio todavía no ha progresado nunca)"

        if not vigentes:
            print(
                f"No encuentro {args.rutina}/{args.ejercicio}: ni tiene fila con "
                f"carga ni aparece en esa rutina del config."
            )
            return 1

        print(f"{args.rutina} / {args.ejercicio}")
        print(f"  ahora   {_pinta(vigentes)}   (según {origen})")

        if not args.pesos:
            print("\n(no se ha pedido ningún cambio)")
            return 0

        try:
            pesos = [float(t.replace(",", ".")) for t in args.pesos.split(",")]
            propuestas = nuevas_series(vigentes, pesos)
        except (ValueError, CargaInvalida) as exc:
            print(f"\nNo se puede: {exc}")
            return 1

        print(f"  queda   {_pinta(propuestas)}")

        if not args.aplicar:
            print("\n(simulación: no se ha escrito nada. Repite con --aplicar)")
            return 0

        if fila is None:
            fila = ExerciseTarget(
                routine_key=args.rutina, exercise_key=args.ejercicio
            )
            s.add(fila)
        fila.current_sets_json = json.dumps(propuestas, ensure_ascii=False)
        tope = [x.get("weight_kg") or 0 for x in propuestas]
        fila.current_target_kg = max(tope) if any(tope) else None
        fila.below_plan_streak = 0
        fila.below_plan_best_kg = None

    print("\nEscrito. La próxima sesión se construye ya con esa carga.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
