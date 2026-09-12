"""Herramienta de operador para las rutinas de Hevy: mirar, y deshacer.

POR QUÉ EXISTE ESTE FICHERO
---------------------------
`HevyClient.restore()` estaba escrito, comentado y con seis tests. Y no lo
llamaba nadie. Ni un comando, ni un endpoint, ni un script: fuera de
`tests/test_hevy.py` la función era inalcanzable.

Eso la convertía en la peor clase de opción muerta, porque no era una opción
cualquiera: era **la red de seguridad** con la que se justificaba abrir el
interruptor de escritura. «Hay revertido probado» era verdad sobre el código y
falso sobre el sistema. Un test que pasa no es un comando que se pueda teclear a
las siete de la mañana con la rutina del día ya destrozada.

Lo mismo con `read_pending()`: la marca de «escritura en curso» se escribía
antes de cada PUT para sobrevivir a que el proceso muriera a mitad, y no la leía
nadie. Una señal que se emite y nadie escucha es exactamente igual de útil que
no emitirla, con el agravante de que parece que sí.

QUÉ HACE Y QUÉ NO
-----------------
Hace cuatro cosas, todas de operador y ninguna automática:

    estado      ¿hay alguna escritura sin confirmar? ¿qué rutinas hay?
    copias      las copias guardadas de una rutina, de la más nueva a la vieja
    ver         qué se escribió el último día, y qué había justo antes
    revertir    devolver una rutina al estado de una copia

`revertir` es la única que toca Hevy, pide confirmación escrita y NO mira
`integrations.hevy.write_enabled`. Eso último es deliberado y viene de
`restore()`: si el interruptor bloqueara la reversión, el modo seguro impediría
deshacer justo el desastre que se causó mientras estaba abierto.

Se ejecuta:

    python -m app.rutina estado
    python -m app.rutina copias --rutina dia_1
    python -m app.rutina ver --rutina dia_1
    python -m app.rutina revertir --rutina dia_1 --si
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config_loader import load_config
from app.settings import settings

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Resolver qué rutina es "dia_1"
# ---------------------------------------------------------------------------


def routine_map(cfg: Any) -> dict[str, str]:
    """`{clave del config: id de Hevy}` de las rutinas que tienen id.

    Las que no lo tienen se quedan fuera a propósito: una rutina sin
    `hevy_routine_id` no se escribe nunca, así que tampoco se puede revertir, y
    ofrecerla en la lista sería ofrecer algo que va a fallar más tarde.
    """
    salida: dict[str, str] = {}
    for clave, datos in (cfg.routines or {}).items():
        rid = (datos or {}).get("hevy_routine_id")
        if rid:
            salida[clave] = str(rid)
    return salida


def resolver(cfg: Any, quien: str) -> tuple[str, str]:
    """De `dia_1` -o del id crudo- a `(clave, id)`.

    Acepta las dos formas porque las dos aparecen: la clave es lo que uno
    recuerda, y el id es lo que sale en los mensajes de error y en los nombres
    de las carpetas de copias. Obligar a traducir a mano entre ellas, con la
    rutina rota y con prisa, es pedir un error de más.
    """
    mapa = routine_map(cfg)
    if quien in mapa:
        return quien, mapa[quien]
    for clave, rid in mapa.items():
        if rid == quien:
            return clave, rid
    raise SystemExit(
        f"no conozco la rutina {quien!r}. Las que hay: "
        f"{', '.join(sorted(mapa)) or '(ninguna con hevy_routine_id)'}"
    )


# ---------------------------------------------------------------------------
# estado
# ---------------------------------------------------------------------------


def cmd_estado(cfg: Any, args: argparse.Namespace) -> int:
    from app.integrations.hevy import backup_dir, latest_backup, read_pending

    raiz = data_root()
    print(f"copias en: {raiz / 'hevy_backups'}\n")

    pendiente = read_pending(raiz)
    if pendiente is not None:
        print("!! HAY UNA ESCRITURA SIN CONFIRMAR")
        print("   Una rutina quedó en estado desconocido: puede ser la vieja, la")
        print("   nueva, o una mezcla. Míralo en Hevy antes de nada.")
        for k, v in pendiente.items():
            print(f"   {k}: {v}")
        rid = pendiente.get("routine_id")
        if rid and rid != "?":
            print(f"\n   Para deshacerlo:  python -m app.rutina revertir --rutina {rid}")
        print()
    else:
        print("no hay escrituras a medias\n")

    mapa = routine_map(cfg)
    if not mapa:
        print("ninguna rutina del config tiene hevy_routine_id")
        return 0

    print(f"{'rutina':<14} {'copias':>6}  última copia")
    print("-" * 62)
    for clave, rid in sorted(mapa.items()):
        carpeta = backup_dir(raiz, rid)
        n = len(list(carpeta.glob("*.json"))) if carpeta.is_dir() else 0
        ultima = latest_backup(raiz, rid) if n else None
        cuando = (
            f"{ultima.taken_at:%Y-%m-%d %H:%M:%S}" if ultima else "— nunca escrita —"
        )
        print(f"{clave:<14} {n:>6}  {cuando}")
    return 0


# ---------------------------------------------------------------------------
# copias
# ---------------------------------------------------------------------------


def cmd_copias(cfg: Any, args: argparse.Namespace) -> int:
    from app.integrations.hevy import backup_dir, leer_backup

    clave, rid = resolver(cfg, args.rutina)
    carpeta = backup_dir(data_root(), rid)
    if not carpeta.is_dir():
        print(f"{clave} ({rid}): no hay ninguna copia todavía.")
        print("Se escribe una copia por cada escritura, antes del PUT.")
        return 0

    ficheros = sorted(carpeta.glob("*.json"), reverse=True)
    print(f"{clave} ({rid}) — {len(ficheros)} copia(s), de la más nueva a la vieja\n")
    for i, f in enumerate(ficheros):
        marca = "  <- la que usaría `revertir` sin --copia" if i == 0 else ""
        copia = leer_backup(f, rid)
        if copia is None:
            # Una copia ilegible se DICE, y se dice AQUÍ. Callarla la dejaría en
            # la lista como una opción válida hasta el momento de usarla, que es
            # siempre el peor momento para descubrirlo. `leer_backup` ya lo ha
            # anotado en el log; esto es para quien está mirando la pantalla.
            print(f"  {f.name}   !! ILEGIBLE — no sirve para revertir")
            continue
        n = len(copia.payload.get("exercises") or [])
        print(f"  {f.name}   {n} ejercicios{marca}")
    return 0


# ---------------------------------------------------------------------------
# ver
# ---------------------------------------------------------------------------


def cmd_ver(cfg: Any, args: argparse.Namespace) -> int:
    """Qué se escribió, y qué había justo antes.

    Las dos mitades vienen de sitios distintos a propósito: lo escrito está en
    la tabla `hevy_writes` -que registra también los intentos fallidos- y lo
    anterior está en el fichero de copia. Juntarlas en una sola fuente habría
    significado guardar el estado previo en la base, y entonces una base
    corrupta se llevaría por delante las dos cosas a la vez.
    """
    from app.integrations.hevy import latest_backup, payload_diff
    from app.models import HevyWrite

    clave, rid = resolver(cfg, args.rutina)

    from sqlalchemy import select

    from app.db import session_scope

    with session_scope() as s:
        filas = list(
            s.execute(
                select(HevyWrite)
                .where(HevyWrite.hevy_routine_id == rid)
                .order_by(HevyWrite.id.desc())
                .limit(args.n)
            ).scalars()
        )
        # Se sacan los campos AQUÍ, dentro de la sesión. Devolver las instancias
        # y leerlas fuera da `DetachedInstanceError` el día que alguien mueva
        # una línea, y el fallo aparece lejos de la causa.
        escrituras = [
            {
                "date": f.date,
                "status": f.status,
                "http": f.http_status,
                "error": f.error,
                "payload": json.loads(f.payload_json) if f.payload_json else None,
                "written_at": f.written_at,
            }
            for f in filas
        ]

    if not escrituras:
        print(f"{clave} ({rid}): no hay ninguna escritura registrada.")
        return 0

    print(f"{clave} ({rid}) — últimas {len(escrituras)} escritura(s)\n")
    for e in escrituras:
        print(f"  {e['date']}  [{e['status']}]"
              f"{'  HTTP ' + str(e['http']) if e['http'] else ''}"
              f"   {e['written_at']:%H:%M:%S}")
        if e["error"]:
            print(f"      error: {e['error']}")

    ultima = escrituras[0]
    copia = latest_backup(data_root(), rid)
    print()
    if copia is None:
        print("No hay copia guardada, así que no hay con qué comparar.")
        return 0

    print(f"ANTES  copia {copia.describe()}")
    print(f"AHORA  lo escrito el {ultima['date']} [{ultima['status']}]\n")

    if ultima["payload"] is None:
        print("  (esa escritura no guardó payload)")
        return 0

    diff = payload_diff(copia.payload, ultima["payload"])
    if not diff:
        print("  sin diferencias: se escribió lo mismo que ya había")
    else:
        for linea in diff:
            print(f"  {linea}")
    return 0


# ---------------------------------------------------------------------------
# revertir
# ---------------------------------------------------------------------------


def cmd_revertir(cfg: Any, args: argparse.Namespace) -> int:
    from app.integrations.hevy import (
        HevyError,
        build_client,
        latest_backup,
        leer_backup,
    )

    clave, rid = resolver(cfg, args.rutina)
    raiz = data_root()

    if args.copia:
        fichero = Path(args.copia)
        if not fichero.is_file():
            print(f"no existe el fichero de copia {fichero}")
            return 1
        copia = leer_backup(fichero, rid)
        faltaba = f"la copia {fichero} no se puede leer"
    else:
        copia = latest_backup(raiz, rid)
        faltaba = f"no hay ninguna copia legible de {clave} ({rid})"

    if copia is None:
        # Las dos formas de no tener copia -no existe, o existe y está rota-
        # acaban aquí, y el mensaje distingue cuál es. Decir «no hay copia»
        # cuando lo que pasa es que hay una corrupta manda a buscar donde no es.
        print(f"{faltaba}.")
        print("Sin copia no hay a dónde volver: no se ha tocado nada.")
        return 1

    print(f"rutina : {clave} ({rid})")
    print(f"copia  : {copia.describe()}")
    print(f"         {copia.path}")
    print()
    print("Esto hace un PUT en Hevy y REEMPLAZA la rutina entera por el")
    print("contenido de esa copia. Lo que haya ahora se pierde.")
    print()
    print("El interruptor `write_enabled` NO se consulta: revertir tiene que")
    print("funcionar aunque el de escritura esté apagado.")
    print()

    if not args.si:
        try:
            # `input` y no un `--si` a secas: quien llega aquí suele llegar con
            # prisa, y una tecla de más es barata comparada con revertir la
            # rutina equivocada.
            respuesta = input("Escribe REVERTIR para continuar: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\ncancelado. No se ha tocado nada.")
            return 1
        if respuesta != "REVERTIR":
            print("cancelado. No se ha tocado nada.")
            return 1

    try:
        cliente = build_client(settings, cfg)
        r = cliente.restore(rid, copia)
    except HevyError as exc:
        print(f"\nNO se ha revertido: {exc}")
        return 1

    print(f"\nREVERTIDA. {r.reason}")
    print(f"Compruébalo en Hevy: la rutina «{clave}» tiene que verse como el")
    print(f"{copia.taken_at:%d-%m-%Y a las %H:%M}.")
    return 0


# ---------------------------------------------------------------------------


def data_root() -> Path:
    """La carpeta de datos, deducida igual que en `hevy.build_client`.

    Se repite la deducción en vez de importarla porque `build_client` necesita
    la API key y aquí hay comandos -`estado`, `copias`- que tienen que funcionar
    sin ella: mirar qué copias existen no debería exigir credenciales.
    """
    url = str(settings.database_url)
    return Path(url.split("///")[-1]).parent if "///" in url else Path("data")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m app.rutina",
        description="Mirar y deshacer las escrituras de rutinas en Hevy",
    )
    p.add_argument("--config", default=None, help="ruta a config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("estado", help="escrituras a medias y copias por rutina")

    c = sub.add_parser("copias", help="las copias guardadas de una rutina")
    c.add_argument("--rutina", required=True, help="clave del config (dia_1) o id")

    v = sub.add_parser("ver", help="qué se escribió y qué había antes")
    v.add_argument("--rutina", required=True, help="clave del config (dia_1) o id")
    v.add_argument("-n", type=int, default=5, help="cuántas escrituras listar (5)")

    r = sub.add_parser("revertir", help="devolver una rutina a una copia")
    r.add_argument("--rutina", required=True, help="clave del config (dia_1) o id")
    r.add_argument("--copia", default=None,
                   help="fichero de copia concreto. Por defecto, el más reciente")
    r.add_argument("--si", action="store_true",
                   help="no preguntar (para guiones; a mano, mejor sin esto)")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    cfg = load_config(Path(args.config) if args.config else settings.config_path)

    return {
        "estado": cmd_estado,
        "copias": cmd_copias,
        "ver": cmd_ver,
        "revertir": cmd_revertir,
    }[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
