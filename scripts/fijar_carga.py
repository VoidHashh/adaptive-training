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

EN QUÉ BASE ESCRIBE, QUE ES LA PARTE QUE SALIÓ MAL
--------------------------------------------------
Esto se ejecuta desde el PC de pruebas, donde `./data/app.db` existe, está
vacío y no lo lee nadie: la base que manda vive dentro del contenedor, en el
volumen. Lanzado a pelo, el guion abría el fichero de al lado, decía «este
ejercicio todavía no ha progresado nunca» -que era verdad EN ESE FICHERO- y se
ofrecía a escribir ahí. Ni una palabra sobre cuál de las dos bases estaba
mirando. Pasó dos veces.

Por eso ahora:

  * Lo PRIMERO que imprime es la base que va a tocar, con su ruta absoluta y
    cuántas cargas tiene dentro. Antes de nada, se mire lo que se mire.
  * Si le piden ESCRIBIR en una base sin una sola fila de cargas, se niega. Esa
    es la firma del fichero fósil, y ningún ejercicio real se fija sobre una
    base recién nacida. Se puede forzar con `--aunque-este-vacia` para el caso
    legítimo -una instalación nueva de verdad-, que existe pero no es este.
  * `--contenedor` hace el trabajo de apuntar bien: se copia dentro y se
    reejecuta allí. El comando correcto tenía que EXISTIR, porque un aviso en
    un README no paró esto las dos veces anteriores.

Por defecto SIMULA. Escribe solo con `--aplicar`.

    python scripts/fijar_carga.py --contenedor
    python scripts/fijar_carga.py --contenedor dia_1 extension_cuadriceps
    python scripts/fijar_carga.py --contenedor dia_1 extension_cuadriceps 40,60,60 --aplicar
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


CONTENEDOR_POR_DEFECTO = "adaptive-training"


class CargaInvalida(ValueError):
    """Lo que se ha pedido escribir no es una carga que se pueda entrenar."""


class BaseEquivocada(RuntimeError):
    """Se ha pedido escribir en una base que no parece la que manda."""


def donde_escribe(url: str) -> str:
    """La base, en una línea y con ruta absoluta. Para poder desmentirla de un vistazo.

    Un `sqlite:///data/app.db` no dice nada: la gracia es ver el disco entero,
    porque el error que esto previene es exactamente confundir dos ficheros que
    se llaman igual.
    """
    if url.startswith("sqlite:///"):
        ruta = Path(url[len("sqlite:///") :])
        try:
            absoluta = ruta.resolve()
        except OSError:
            absoluta = ruta
        if not absoluta.exists():
            return f"{absoluta}  (NO EXISTE todavía)"
        return f"{absoluta}  ({absoluta.stat().st_size / 1024:.0f} KB)"
    # Cualquier otro motor: se enseña la URL sin la contraseña.
    if "@" in url:
        esquema, _, resto = url.partition("://")
        return f"{esquema}://…@{resto.rpartition('@')[2]}"
    return url


def reejecutar_dentro(contenedor: str, argv: list[str]) -> int:
    """Copia este mismo guion al contenedor y lo lanza allí.

    `scripts/` no viaja en la imagen -el `Dockerfile` copia `app/`, `static/` y
    el YAML, y nada más- así que no basta con `docker exec`: hay que meterlo.
    Va a `/tmp`, que no es el volumen: no deja rastro en los datos y desaparece
    al reiniciar, que es lo que se quiere de una herramienta de paso.
    """
    import subprocess

    yo = Path(__file__).resolve()
    destino = "/tmp/fijar_carga.py"
    cp = subprocess.run(
        ["docker", "cp", str(yo), f"{contenedor}:{destino}"],
        capture_output=True, text=True,
    )
    if cp.returncode != 0:
        print(f"No he podido copiar el guion a «{contenedor}»:\n{cp.stderr.strip()}")
        print("\n¿Está corriendo? Míralo con:  docker ps")
        return 1
    # `flush` porque lo siguiente es un proceso hijo que escribe en el mismo
    # descriptor: sin esto, el aviso de dónde se está ejecutando salía DESPUÉS
    # de la salida que anunciaba.
    print(f"(ejecutando dentro de «{contenedor}», sobre la base del volumen)\n",
          flush=True)
    return subprocess.run(
        ["docker", "exec", contenedor, "python", destino, *argv]
    ).returncode


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
    # Bandera SIN valor, y el nombre aparte. Con `nargs="?"` argparse se comía
    # el primer posicional -`--contenedor dia_1 ...` intentaba copiar a un
    # contenedor llamado `dia_1`- y fallaba con un mensaje sobre Docker que no
    # tenía nada que ver con lo que estaba mal.
    p.add_argument(
        "--contenedor", action="store_true",
        help="ejecutar dentro del contenedor, contra la base que de verdad manda",
    )
    p.add_argument(
        "--nombre-contenedor", default=CONTENEDOR_POR_DEFECTO,
        help=f"cuál, si no es «{CONTENEDOR_POR_DEFECTO}»",
    )
    p.add_argument(
        "--aunque-este-vacia", action="store_true",
        help="permitir escribir en una base sin ninguna carga (instalación nueva)",
    )
    args = p.parse_args(argv)

    if args.contenedor:
        crudos = list(argv if argv is not None else sys.argv[1:])
        resto = []
        saltar = False
        for a in crudos:
            if saltar:
                saltar = False
                continue
            if a == "--contenedor":
                continue
            if a == "--nombre-contenedor":
                saltar = True
                continue
            if a.startswith("--nombre-contenedor="):
                continue
            resto.append(a)
        return reejecutar_dentro(args.nombre_contenedor, resto)

    cfg = load_config(settings.config_path)
    init_db()

    # LO PRIMERO, SIEMPRE. Ver el docstring: el fallo que esto previene no fue
    # escribir mal, fue escribir bien en el fichero de al lado sin decirlo.
    print(f"base de datos: {donde_escribe(settings.database_url)}\n")

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

        # Una base sin UNA SOLA carga no es la que manda: es el fósil de `./data`
        # o una instalación recién nacida. Fijar a mano la carga de un ejercicio
        # presupone un histórico que ahí no existe.
        if not filas and not args.aunque_este_vacia:
            print(
                "\nME NIEGO: esta base no tiene ni una carga guardada.\n"
                "  Eso es la firma del fichero vacío de `./data`, no de la base que\n"
                "  decide. La que manda vive en el volumen del contenedor.\n\n"
                f"  Prueba:  python scripts/fijar_carga.py --contenedor "
                f"{args.rutina} {args.ejercicio} {args.pesos} --aplicar\n\n"
                "  Si de verdad es una instalación nueva y vacía, "
                "repite con --aunque-este-vacia."
            )
            return 1

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
