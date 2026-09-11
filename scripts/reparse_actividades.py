"""Rellenar columnas de `activities` que no existían cuando se archivó la fila.

QUÉ PROBLEMA RESUELVE
---------------------
Una columna nueva en `models.py` nace vacía para todo el histórico. `activities`
tiene cincuenta y ocho salidas archivadas entre marzo y septiembre, y las diez
columnas que se acaban de añadir -pulso máximo, las dos velocidades, el desnivel
negativo, las calorías, las tres de respiración y las dos de temperatura- están a
NULL en las cincuenta y ocho, no porque Garmin no las mandara sino porque cuando
se guardaron no había dónde ponerlas.

El dato no se ha perdido: `data/cache/activities.json` guarda el resumen ENTERO
de cada actividad tal como llega, se fusiona en cada refresco y no se poda nunca.
Esa decisión -archivar el crudo y no el `Ride` ya parseado- es justo la que hace
que añadir una columna cueste un reparseo local de dos segundos en vez de volver
a bajar seis meses contra un servidor que corta por 429.

LO QUE NO HACE, QUE IMPORTA IGUAL
---------------------------------
Solo escribe donde hay un NULL: una celda con valor no se pisa nunca. Y no toca
la clasificación -`intensity_level`, `classification_source`, `training_load`,
`training_load_estimated`-, que depende de los umbrales del `config.yaml` del día
en que se hizo. Reescribirla con el YAML de hoy convertiría el histórico en una
foto del presente, y entonces "el lumbar sube después de una salida intensa" se
mediría contra unas intensas que en su momento no lo fueron.

Las salidas que están en la caché pero NO tienen fila no se archivan aquí; eso es
`scripts/backfill_salidas.py`. Se cuentan y se dicen, porque son la diferencia
entre "no había columna" y "no había salida".

    python scripts/reparse_actividades.py --simular
    python scripts/reparse_actividades.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from app.backfill import SinCacheDeSalidas, reparsear_actividades
from app.db import init_db, session_scope
from app.integrations.activity_cache import RUTA_CACHE_SALIDAS, load_cached_rides


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--cache", type=Path, default=RUTA_CACHE_SALIDAS,
        help=f"volcado de actividades (por defecto, {RUTA_CACHE_SALIDAS})",
    )
    p.add_argument(
        "--campo", action="append", dest="campos", metavar="COLUMNA",
        help="limitar a esta columna; se puede repetir. Por defecto, todas las "
             "que `upsert_activities` copia del Ride menos la carga.",
    )
    p.add_argument(
        "--simular", action="store_true",
        help="dice qué se rellenaría, sin escribir en la base",
    )
    args = p.parse_args()

    init_db()

    cache = load_cached_rides(args.cache)
    print(f"caché     {args.cache}")
    print(f"          {cache.describe()}")
    if cache.file_mtime:
        print(f"          escrita el {cache.file_mtime:%Y-%m-%d %H:%M}")

    try:
        with session_scope() as s:
            res = reparsear_actividades(
                s, args.cache, campos=args.campos, simular=args.simular
            )
    except SinCacheDeSalidas as exc:
        print(f"\nNo hay nada que reparsear: {exc}")
        return 1

    print()
    print("=" * 74)
    print("RESULTADO")
    print("=" * 74)
    print(" ", res.resumen())

    # Se listan TODAS las columnas miradas, también las que no han rellenado
    # nada. Una columna que sale con 0/0/58 está diciendo algo -que el crudo no
    # trae ese campo en ninguna salida- y ese algo se pierde si solo se imprimen
    # las que han cambiado.
    columnas = sorted(
        set(res.rellenadas) | set(res.sin_dato) | set(res.ya_estaban)
    )
    if columnas:
        print()
        print(f"  {'columna':<24} {'rellenadas':>10} {'sin dato':>9} {'ya estaban':>11}")
        print(f"  {'-' * 24} {'-' * 10:>10} {'-' * 9:>9} {'-' * 11:>11}")
        for c in columnas:
            print(
                f"  {c:<24} {res.rellenadas.get(c, 0):>10} "
                f"{res.sin_dato.get(c, 0):>9} {res.ya_estaban.get(c, 0):>11}"
            )
        print(
            "\n  'sin dato' no es un fallo: un rodillo de interior no tiene "
            "temperatura\n  ni desnivel, y una salida sin cinta no tiene pulso. "
            "Se cuenta aparte para\n  poder distinguir 'no se guardó' de 'Garmin "
            "no lo mandó'."
        )

    if res.sin_fila:
        print(
            f"\n  {res.sin_fila} salida(s) de la caché no tienen fila en la base y "
            f"NO se han\n  archivado: eso lo hace scripts/backfill_salidas.py, que "
            f"crea historia.\n  Esto solo completa la que ya hay."
        )

    if res.simulado:
        print("\n(simulación: no se ha escrito nada)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
