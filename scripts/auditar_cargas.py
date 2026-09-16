"""Comparar las cargas que MANDAN con los ejercicios que existen, y cantar lo que no cuadre.

POR QUÉ EXISTE
--------------
`config.yaml` describe los ejercicios; `exercise_targets` guarda la carga que de
verdad se entrena. En cuanto un ejercicio tiene fila, el YAML deja de mirarse
para los pesos (`app/engine/progression.py:con_carga_vigente`). Son dos sitios
que hablan del mismo ejercicio y solo uno decide, que es exactamente la forma
que tienen las cosas de desincronizarse sin dar ningún error.

Ya pasó dos veces seguidas con el mismo ejercicio:

  * `gemelo_sentado` quedó en la base como 70 → 60 → 65, con el 70 delante
    porque fue un dedazo en Hevy que la adopción se creyó. Se corrigió... en el
    YAML, que para un ejercicio con fila no cambia nada. Durante días el
    fichero decía una rampa y la sesión entrenaba otra.
  * Un comentario de `set_types.overrides` siguió describiendo ese mismo
    ejercicio como «30/40/50» mucho después de que dejara de serlo.

Ninguna de las dos cosas rompió nada. Ese es el problema: un semáforo que se
equivoca se nota, y una carga que se equivoca se levanta.

QUÉ MIRA
--------
Siete cosas, en dos grupos. Las que son un fallo casi seguro:

  HUÉRFANA        fila en la base de un ejercicio que el YAML ya no tiene.
                  No se entrena, pero resucita con su carga vieja el día que
                  la clave vuelva, y ese día nadie se acordará.
  RAMPA AL REVÉS  los pesos efectivos no van de menos a más. Los 22 ejercicios
                  con carga de este `config.yaml` suben o repiten, sin una sola
                  excepción; el único que bajaba era el dedazo. Ver abajo.
  REPS FUERA      alguna serie efectiva se sale del `rep_range` declarado. La
                  progresión decide mirando ese rango: con las reps fuera, la
                  puerta se abre o se cierra por un motivo que no es el suyo.
  POR DEBAJO      la carga de la base es MENOR que el punto de partida del
                  YAML. Se progresa hacia arriba; aparecer por debajo del
                  arranque significa que algo la sembró mal o la deshizo.

Y las que solo hay que saber, porque son normales:

  SIN FILA        ejercicio del YAML que aún no ha progresado. Arranca donde
                  diga el fichero: es el ÚNICO caso en que editar el YAML sirve
                  de algo, y por eso conviene tenerlo a la vista.
  SERIES          la base tiene otro número de series efectivas que el YAML.
                  Con `progression_type: sets` es lo que debe pasar.
  SUBIDA          la carga ha subido respecto al arranque. Es el sistema
                  funcionando; se cuenta para que el silencio signifique algo.

SOBRE «RAMPA AL REVÉS», QUE ES LA ÚNICA HEURÍSTICA DE AQUÍ
-----------------------------------------------------------
Las demás comprobaciones contrastan dos fuentes. Esta se inventa una regla:
«los pesos efectivos no bajan». No es una ley del entrenamiento -unas series
descendentes son perfectamente legítimas- sino un hecho de ESTE programa, que
se puede comprobar en el YAML y que el propio guion comprueba antes de usarla:
si algún día el fichero declara a propósito una rampa descendente, el aviso se
calla solo para ese ejercicio en vez de convertirse en ruido que se ignora.

Por eso no falla, AVISA. Un aviso que se equivoca y se puede desmentir de un
vistazo es barato; una comprobación que se desactiva por pesada no vale nada.

CÓMO SE USA
-----------
Solo lee. No escribe nunca, ni con banderas.

    python scripts/auditar_cargas.py --contenedor

Devuelve 1 si hay algo del primer grupo, para que pueda colgarse de un aviso
automático el día que haga falta. Lo que encuentre se arregla con
`scripts/fijar_carga.py`, que es el que sí escribe.
"""

from __future__ import annotations

import argparse
import json
import subprocess
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


def donde_lee(url: str) -> str:
    """La base, con ruta absoluta. Misma razón que en `fijar_carga.py`.

    Aquí no se escribe, pero una auditoría que dice «todo correcto» mirando el
    fichero vacío de al lado es peor que no auditar: deja la conciencia
    tranquila y el problema puesto.
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
    if "@" in url:
        esquema, _, resto = url.partition("://")
        return f"{esquema}://…@{resto.rpartition('@')[2]}"
    return url


def reejecutar_dentro(contenedor: str, argv: list[str]) -> int:
    """Copia este guion al contenedor y lo lanza allí. `scripts/` no va en la imagen."""
    yo = Path(__file__).resolve()
    destino = "/tmp/auditar_cargas.py"
    cp = subprocess.run(
        ["docker", "cp", str(yo), f"{contenedor}:{destino}"],
        capture_output=True,
        text=True,
    )
    if cp.returncode != 0:
        print(f"No he podido copiar el guion a «{contenedor}»:\n{cp.stderr.strip()}")
        print("\n¿Está corriendo? Míralo con:  docker ps")
        return 1
    # `flush` antes de soltar un hijo sobre el mismo descriptor: sin esto el
    # aviso sale después de lo que anuncia.
    print(f"(leyendo dentro de «{contenedor}», la base del volumen)\n", flush=True)
    return subprocess.run(["docker", "exec", contenedor, "python", destino, *argv]).returncode


def peso(s: dict[str, Any]) -> float:
    return float(s.get("weight_kg") or 0)


def efectivas(ex: dict[str, Any], set_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    sets = ex.get("sets") or []
    flags = warmup_flags(sets, set_cfg, ex.get("key"))
    return [s for s, f in zip(sets, flags, strict=True) if not f]


def pinta(series: list[dict[str, Any]]) -> str:
    return " · ".join(
        f"{s.get('reps', s.get('duration_s', '?'))}×{peso(s):g} kg" for s in series
    )


def no_baja(series: list[dict[str, Any]]) -> bool:
    """¿Los pesos van de menos a más (o iguales)?"""
    pesos = [peso(s) for s in series]
    return all(a <= b for a, b in zip(pesos, pesos[1:], strict=False))


def ejercicios_del_yaml(cfg) -> dict[tuple[str, str], dict[str, Any]]:
    salida: dict[tuple[str, str], dict[str, Any]] = {}
    for rk, rutina in (cfg.routines or {}).items():
        for ex in (rutina or {}).get("exercises") or []:
            salida[(str(rk), str(ex.get("key")))] = ex
    return salida


def auditar(cfg, filas: dict[tuple[str, str], Any]) -> tuple[list[str], list[str]]:
    """Devuelve (problemas, notas). Nada se escribe: esto solo mira."""
    problemas: list[str] = []
    notas: list[str] = []
    yaml = ejercicios_del_yaml(cfg)

    # La regla «no baja» se valida contra el propio fichero ANTES de usarla. Si
    # un ejercicio declara a propósito una rampa descendente, su aviso se calla
    # y se dice que se ha callado: una comprobación que no se puede desmentir
    # acaba desactivada entera.
    descendentes_a_proposito = {
        clave
        for clave, ex in yaml.items()
        if (efs := efectivas(ex, cfg.set_types)) and not no_baja(efs)
    }
    for clave in sorted(descendentes_a_proposito):
        notas.append(
            f"RAMPA        {clave[0]}/{clave[1]}: el YAML declara los pesos de más "
            f"a menos, así que no se le mira el orden a su fila."
        )

    for clave, fila in sorted(filas.items()):
        rk, ek = clave
        ex = yaml.get(clave)
        try:
            db_sets = json.loads(fila.current_sets_json or "[]")
        except (TypeError, ValueError):
            problemas.append(f"ILEGIBLE     {rk}/{ek}: `current_sets_json` no es JSON.")
            continue

        if ex is None:
            # ¿La misma clave de ejercicio vive en OTRA rutina? Entonces no es
            # un ejercicio retirado: es el mismo ejercicio apuntado a la rutina
            # que no era. Lo dice porque las dos averías se arreglan distinto y
            # desde la fila sola no se distinguen.
            otras = sorted(r for (r, e) in yaml if e == ek and r != rk)
            if otras:
                donde = " o ".join(otras)
                causa = (
                    f" `{ek}` sí existe, pero en {donde}. Una fila bajo «{rk}» "
                    f"sale de haber registrado las dos rutinas en un mismo "
                    f"entreno de Hevy: el ejercicio se apuntó a la rutina del "
                    f"título, no a la suya."
                )
            else:
                causa = " Esa clave no está en ninguna rutina del YAML."

            if db_sets:
                problemas.append(
                    f"HUÉRFANA     {rk}/{ek}: guarda {pinta(db_sets)} y el YAML no "
                    f"define ese par.{causa} Hoy no se entrena, pero vuelve con esa "
                    f"carga el día que la clave aparezca ahí."
                )
            else:
                problemas.append(
                    f"HUÉRFANA     {rk}/{ek}: fila sin carga para un par que el YAML "
                    f"no define.{causa} No cambia ninguna sesión: lo que ensucia es "
                    f"el cumplimiento, que se cuenta contra una rutina ajena."
                )
            continue

        if not db_sets:
            # Fila sin series: la construcción cae al YAML igual que si no
            # existiera. Se dice porque la fila SÍ existe y eso despista al
            # mirar la tabla a mano.
            notas.append(
                f"SIN CARGA    {rk}/{ek}: tiene fila pero sin series; arranca del YAML."
            )
            continue

        yaml_sets = efectivas(ex, cfg.set_types)
        con_peso = any(peso(s) > 0 for s in db_sets)

        if con_peso and clave not in descendentes_a_proposito and not no_baja(db_sets):
            problemas.append(
                f"RAMPA AL REVÉS  {rk}/{ek}: {pinta(db_sets)}. El YAML sube "
                f"({pinta(yaml_sets)}). Ordenada de menos a más sería "
                f"{pinta(sorted(db_sets, key=peso))}."
            )

        rr = ex.get("rep_range")
        if rr and len(rr) == 2:
            fuera = [
                s for s in db_sets if s.get("reps") and not (int(rr[0]) <= int(s["reps"]) <= int(rr[1]))
            ]
            if fuera:
                problemas.append(
                    f"REPS FUERA   {rk}/{ek}: {pinta(db_sets)} con rep_range "
                    f"{rr[0]}–{rr[1]}. La progresión decide mirando ese rango."
                )

        if con_peso and yaml_sets:
            tope_db = max(peso(s) for s in db_sets)
            tope_yaml = max(peso(s) for s in yaml_sets)
            if tope_yaml and tope_db < tope_yaml:
                problemas.append(
                    f"POR DEBAJO   {rk}/{ek}: la base va a {tope_db:g} kg y el YAML "
                    f"arranca en {tope_yaml:g}. Se progresa hacia arriba."
                )
            elif tope_db > tope_yaml:
                notas.append(
                    f"SUBIDA       {rk}/{ek}: {tope_yaml:g} → {tope_db:g} kg desde el arranque."
                )

        if yaml_sets and len(db_sets) != len(yaml_sets):
            notas.append(
                f"SERIES       {rk}/{ek}: {len(db_sets)} efectivas en la base y "
                f"{len(yaml_sets)} en el YAML"
                + (
                    f" (normal: `progression_type: {ex.get('progression_type')}`)."
                    if ex.get("progression_type") == "sets"
                    else ". El YAML no está en modo `sets`, así que esto no lo hizo la progresión."
                )
            )

    for clave, ex in sorted(yaml.items()):
        if clave in filas:
            continue
        efs = efectivas(ex, cfg.set_types)
        if any(peso(s) > 0 for s in efs):
            notas.append(
                f"SIN FILA     {clave[0]}/{clave[1]}: nunca ha progresado; entrena "
                f"lo del YAML ({pinta(efs)}). Aquí SÍ sirve editar el fichero."
            )

    return problemas, notas


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Comparar las cargas de la base con los ejercicios del YAML. Solo lee."
    )
    p.add_argument(
        "--contenedor",
        action="store_true",
        help="leer dentro del contenedor, la base que de verdad manda",
    )
    p.add_argument(
        "--nombre-contenedor",
        default=CONTENEDOR_POR_DEFECTO,
        help=f"cuál, si no es «{CONTENEDOR_POR_DEFECTO}»",
    )
    args = p.parse_args(argv)

    if args.contenedor:
        crudos = list(argv if argv is not None else sys.argv[1:])
        resto, saltar = [], False
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
    print(f"base de datos: {donde_lee(settings.database_url)}\n")

    with session_scope() as s:
        filas = {
            (f.routine_key, f.exercise_key): f
            for f in s.scalars(select(ExerciseTarget)).all()
        }
        problemas, notas = auditar(cfg, filas)

    total = len(ejercicios_del_yaml(cfg))
    print(f"{len(filas)} cargas guardadas · {total} ejercicios en el YAML\n")

    if problemas:
        print("LO QUE NO CUADRA\n" + "-" * 60)
        for x in problemas:
            print("  " + x)
        print()
    else:
        print("Nada que no cuadre.\n")

    if notas:
        print("LO NORMAL, PARA QUE EL SILENCIO SIGNIFIQUE ALGO\n" + "-" * 60)
        for x in notas:
            print("  " + x)
        print()

    return 1 if problemas else 0


if __name__ == "__main__":
    raise SystemExit(main())
