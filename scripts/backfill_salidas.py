"""Archivar en la base las salidas en bici que llevaban meses solo en disco.

EL AGUJERO QUE TAPA
-------------------
`scripts/backfill_wellness.py` reconstruyó ciento setenta y nueve días de
bienestar sin un hueco. Durante ese mismo tiempo la tabla `activities` tuvo cero
filas, y no porque fallara nada: `upsert_activities` solo lo llamaba el trabajo
diario, que archiva las salidas del día que está decidiendo y de ningún otro.

Así que el histórico largo cubría una de las dos fuentes. Por fuera las dos se
llaman "el backfill de seis meses"; por dentro una no existía. Y el material sí
estaba -`data/cache/activities.json` guarda las actividades desde antes de que
el proyecto tuviera base de datos-: el motor las lee cada mañana para los
percentiles adaptativos, pero el análisis mira la tabla, y la tabla estaba
vacía. De ahí que "impacto de las salidas sobre el HRV de los días siguientes" y
"clasificación histórica de las salidas" salieran N/A con seis meses de datos en
el disco.

NO REESCRIBE NADA
-----------------
Solo se archivan las salidas que no tienen fila. Las que ya la tienen se cuentan
y se dejan como están, porque en `activities` se guarda la CLASIFICACIÓN y esa
depende de los umbrales del `config.yaml` del día en que se hizo: una salida
etiquetada de `intensa` en abril tiene que seguir siéndolo aunque el YAML haya
cambiado dos veces. Reclasificar en cada ejecución haría que "el lumbar sube
después de una salida intensa" se midiera contra unas intensas que en su momento
no lo fueron.

Las que sí se archivan no las clasificó nadie nunca, así que se clasifican con
el config de hoy. Eso se dice arriba del todo en cada ejecución, y no en letra
pequeña: es una propiedad del histórico que se está creando.

Esto no toca la red y no tarda. Se puede repetir las veces que haga falta: la
segunda vez no escribe nada y lo dice.

    python scripts/backfill_salidas.py --simular
    python scripts/backfill_salidas.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from app.backfill import SinCacheDeSalidas, rellenar_salidas
from app.config_loader import load_config
from app.db import init_db, session_scope
from app.integrations.activity_cache import RUTA_CACHE_SALIDAS, load_cached_rides
from app.settings import settings

# El orden en que se imprimen los niveles. A mano, de menos a más, porque
# ordenados por frecuencia el reparto se lee peor: lo que interesa de "18 suaves,
# 24 medias, 16 intensas" es la forma de la distribución, no el ranking.
NIVELES = ("suave", "media", "intensa", "desconocida")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--cache", type=Path, default=RUTA_CACHE_SALIDAS,
        help=f"volcado de actividades (por defecto, {RUTA_CACHE_SALIDAS})",
    )
    p.add_argument(
        "--simular", action="store_true",
        help="dice qué se archivaría, sin escribir en la base",
    )
    args = p.parse_args()

    cfg = load_config(settings.config_path)
    init_db()

    cache = load_cached_rides(args.cache)
    print(f"caché     {args.cache}")
    print(f"          {cache.describe()}")
    if cache.file_mtime:
        print(f"          escrita el {cache.file_mtime:%Y-%m-%d %H:%M}")

    try:
        with session_scope() as s:
            res = rellenar_salidas(s, cfg, args.cache, simular=args.simular)
    except SinCacheDeSalidas as exc:
        print(f"\nNo hay nada que archivar: {exc}")
        return 1

    print()
    print("=" * 74)
    print("RESULTADO")
    print("=" * 74)
    print(" ", res.resumen())

    if res.escritas:
        print(f"  rango     {res.primera} → {res.ultima}")
        print("  niveles   ", end="")
        # Los niveles que no estén en NIVELES se imprimen igual, detrás. Un
        # `level` nuevo en el config que aquí no apareciera sería una salida
        # archivada que no se cuenta en ninguna columna del informe.
        orden = [n for n in NIVELES if n in res.niveles]
        orden += [n for n in sorted(res.niveles) if n not in NIVELES]
        print(", ".join(f"{res.niveles[n]} {n}" for n in orden))

    if res.sin_id:
        print(
            f"\n  {res.sin_id} salida(s) sin activity_id NO se han guardado. Sin id "
            f"no hay con qué distinguirlas, y dos del mismo día se fundirían en una."
        )
    if res.repetidas:
        print(
            f"\n  {res.repetidas} activity_id repetido(s) en el fichero. No debería "
            f"pasar -la caché fusiona por id al escribir-; se ha usado el último."
        )

    if res.simulado:
        print("\n(simulación: no se ha escrito nada)")
        return 0

    if res.escritas:
        print(
            "\nLas salidas archivadas ahora se han clasificado con el config de HOY,\n"
            "porque nadie las clasificó en su momento. Las que ya tenían fila\n"
            "conservan la clasificación del día en que se hicieron."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
