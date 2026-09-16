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

Con `peso x reps` se cambia el par entero, que es lo que hace falta para
REORDENAR una rampa sin arrastrar las repeticiones detrás de los kilos:

    python scripts/fijar_carga.py --contenedor dia_1 gemelo_sentado 60x12,65x15,70x12 --aplicar

Lo que hay que corregir lo encuentra `scripts/auditar_cargas.py`, que solo lee.
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


def parsea_series(texto: str) -> list[tuple[float, int | None]]:
    """`60,65,70` o `60x12,65x15,70x12`. Devuelve (peso, reps o None).

    Las dos formas existen porque hacen cosas distintas y la diferencia importa:

      * Solo pesos: cambia la CARGA y deja cada serie con sus repeticiones donde
        estaban. Es el caso normal -subir de 55 a 60- y el que no hay que
        pensar.
      * Peso y reps: cambia el par entero. Hace falta para REORDENAR una rampa,
        que es el caso que motivó esto. `gemelo_sentado` tenía (70,12), (60,12),
        (65,15) por un dedazo en Hevy. Ordenarla solo por kilos -60, 65, 70-
        habría dejado las 15 repeticiones pegadas a la serie más pesada, porque
        las reps no se mueven: 12×60, 15×65, 12×70 se habría convertido en
        12×60, 12×65, 15×70. Un ejercicio más duro que nadie pidió, colado por
        la puerta de atrás de una corrección de orden.

    NO se admite mezclar las dos formas en la misma lista. `60,65x15,70` se
    leería «la de en medio lleva 15 y las otras las que tuvieran», y las otras
    dependen de un orden que es justo lo que se está cambiando. Es ambiguo
    exactamente cuando más da igual equivocarse, así que se rechaza.
    """
    trozos = [t.strip() for t in texto.split(",") if t.strip()]
    if not trozos:
        raise CargaInvalida("no se ha dado ningún peso.")
    # La `×` de teclado español y la `x` de siempre valen igual: quien copie la
    # rampa de un comentario del YAML traerá la primera.
    normalizados = [t.replace("×", "x").replace("X", "x") for t in trozos]
    con_reps = [t for t in normalizados if "x" in t]
    if con_reps and len(con_reps) != len(normalizados):
        raise CargaInvalida(
            "o todas las series llevan repeticiones o ninguna. Mezclar "
            f"«{', '.join(trozos)}» dejaría las reps de unas dependiendo del "
            "orden viejo, que es lo que se está cambiando."
        )
    salida: list[tuple[float, int | None]] = []
    for crudo, t in zip(trozos, normalizados, strict=True):
        if "x" in t:
            izq, _, der = t.partition("x")
            try:
                salida.append((float(izq.replace(",", ".")), int(der)))
            except ValueError:
                raise CargaInvalida(f"«{crudo}» no es «peso x reps».") from None
        else:
            try:
                salida.append((float(t.replace(",", ".")), None))
            except ValueError:
                raise CargaInvalida(f"«{crudo}» no es un peso.") from None
    return salida


def nuevas_series(
    vigentes: list[dict[str, Any]], pedidas: list[tuple[float, int | None]]
) -> list[dict[str, Any]]:
    """Las series de siempre con la carga nueva. Las reps solo cambian si se piden.

    Se exige que vengan tantas series como efectivas hay. Aceptar menos y
    rellenar, o aceptar más y recortar, sería cambiar el ESQUEMA del ejercicio
    -de tres series a dos- con la excusa de cambiarle el peso, y un cambio así
    tiene que pedirse a la cara y no colarse por el número de comas.
    """
    if not vigentes:
        raise CargaInvalida(
            "ese ejercicio no tiene series efectivas de las que partir."
        )
    if len(pedidas) != len(vigentes):
        raise CargaInvalida(
            f"tiene {len(vigentes)} series efectivas y se han dado {len(pedidas)}. "
            f"Esto cambia la carga, no el número de series."
        )
    if any(p < 0 for p, _ in pedidas):
        raise CargaInvalida("un peso negativo no es una carga.")
    if any(r is not None and r <= 0 for _, r in pedidas):
        raise CargaInvalida("una serie de cero repeticiones no es una serie.")
    salida = []
    for s, (p, reps) in zip(vigentes, pedidas, strict=True):
        nueva = dict(s)
        nueva["weight_kg"] = round(float(p), 3)
        if reps is not None:
            nueva["reps"] = reps
        salida.append(nueva)
    return salida


def huerfanas(cfg, filas: dict[tuple[str, str], Any]) -> list[tuple[str, str]]:
    """Las claves con fila en la base que el `config.yaml` ya no declara.

    Se comprueba el PAR (rutina, ejercicio) y no solo el ejercicio, que es la
    diferencia que importa: `plancha_lateral` existe en `dia_1`, `dia_2` y
    `dia_3`, y `remo_maquina` existe -pero en `hiit_dia_1`, no en `dia_1`. Mirar
    solo la clave del ejercicio daría por buena una fila que está colgada de la
    rutina equivocada, que es justo la clase de huérfana que apareció aquí.

    Una rutina entera que desaparece del YAML cae sola: ninguno de sus pares
    encuentra sitio.
    """
    declaradas = {
        (str(rk), str(ex.get("key")))
        for rk, rutina in (cfg.routines or {}).items()
        for ex in ((rutina or {}).get("exercises") or [])
    }
    return sorted(k for k in filas if k not in declaradas)


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


def _borrar_huerfanas(s, cfg, filas: dict[tuple[str, str], Any], *, aplicar: bool) -> int:
    """Quitar de la base las filas que el YAML ya no reconoce.

    `auditar_cargas.py` sabe encontrarlas desde el principio y remata diciendo
    que se arreglan con este guion, que hasta hoy solo sabía CAMBIAR una carga.
    Una huérfana no se arregla cambiándole el peso: sobra entera. El consejo
    mandaba a una herramienta que no podía hacer lo que anunciaba.

    ANTES DE BORRAR SE ENSEÑA LO QUE HAY DENTRO, y no es cortesía. Una fila de
    `exercise_targets` puede llevar la única copia de una carga que costó meses
    de progresión: `config.yaml` guarda el punto de PARTIDA y la columna
    `current_sets_json` guarda dónde se ha llegado. Borrar eso porque el
    ejercicio cambió de sitio en el YAML sería tirar el histórico para arreglar
    una etiqueta. Con la carga a la vista, la diferencia entre «sobra» y «esto
    hay que moverlo, no borrarlo» se decide mirando, no confiando.

    No mueve nada, a propósito. Mover una fila de `dia_1/plancha_frontal` a
    `hiit_dia_1/plancha_frontal` es una decisión sobre el PROGRAMA -si ese
    ejercicio sigue siendo el mismo ejercicio en su sitio nuevo- y no sobre los
    datos. Se hace a mano, con `fijar_carga.py <rutina> <ejercicio> <pesos>`
    sobre la clave nueva y borrando la vieja después.
    """
    sobran = huerfanas(cfg, filas)
    if not sobran:
        print("No hay ninguna fila huérfana: todas las cargas guardadas "
              "corresponden a un ejercicio que el config.yaml declara.")
        return 0

    print(f"{len(sobran)} fila(s) que el config.yaml ya no declara:\n")
    con_carga = []
    for rk, ek in sobran:
        f = filas[(rk, ek)]
        try:
            series = json.loads(f.current_sets_json or "[]")
        except ValueError:
            series = []
            print(f"  {rk:>10} / {ek:<28} JSON ilegible")
            continue
        pintada = _pinta(series) if series else "sin carga guardada"
        if series:
            con_carga.append(f"{rk}/{ek}")
        print(
            f"  {rk:>10} / {ek:<28} {pintada}"
            f"   (racha {f.clean_streak}, últ. {f.updated_at})"
        )

    if con_carga:
        # El aviso va aquí y no al final porque al final ya se ha decidido.
        print(
            f"\n  OJO: {len(con_carga)} de ellas SÍ llevan carga guardada "
            f"({', '.join(con_carga)}).\n"
            f"  Esa carga no está en ningún otro sitio. Si el ejercicio sigue "
            f"en el programa\n  bajo otra clave, primero ponla allí y borra "
            f"después."
        )

    if not aplicar:
        print("\n(simulación: no se ha borrado nada. Repite con --aplicar)")
        return 0

    if len(sobran) == len(filas):
        # Todas huérfanas significa que el YAML y la base no se están mirando:
        # un `config.yaml` que no cargó, o la base de otra instalación. Borrarlo
        # todo en ese estado es el peor resultado posible de una limpieza.
        print(
            "\nME NIEGO: sobran TODAS las filas de la base. Eso no es un resto "
            "de\n  ejercicios retirados, es un config.yaml que no corresponde a "
            "esta base."
        )
        return 1

    for clave in sobran:
        s.delete(filas[clave])
    print(f"\nBorradas {len(sobran)} fila(s).")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Fijar a mano la carga de un ejercicio.")
    p.add_argument("rutina", nargs="?", help="clave de la rutina, p. ej. dia_1")
    p.add_argument("ejercicio", nargs="?", help="clave del ejercicio")
    p.add_argument(
        "pesos", nargs="?",
        help="carga de las series efectivas, separada por comas: «50,55,60» "
             "cambia solo el peso; «60x12,65x15,70x12» cambia también las reps, "
             "que es lo que hace falta para reordenar una rampa",
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
    p.add_argument(
        "--borrar-huerfanas", action="store_true",
        help="borrar las filas de ejercicios que el config.yaml ya no declara "
             "(las que `auditar_cargas.py` llama HUÉRFANA)",
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

        if args.borrar_huerfanas:
            return _borrar_huerfanas(s, cfg, filas, aplicar=args.aplicar)

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
            propuestas = nuevas_series(vigentes, parsea_series(args.pesos))
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
