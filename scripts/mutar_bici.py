"""¿Sirven de algo los tests del punto de partida de la bici?

Se rompe el código a propósito y se comprueba que ALGÚN test se entera. Un test
que sobrevive a la mutación que dice vigilar no vigila nada.

POR QUÉ ESTA BATERÍA EN CONCRETO
--------------------------------
`_baseline_gaps` sustituyó un calendario fijo por un cálculo sobre la propia
distribución del usuario, y todo lo que este proyecto ha aprendido dice que ahí
es donde viven los fallos mudos: un percentil calculado sobre menos datos de
los que hay no falla, devuelve otro número. Las mutaciones de aquí abajo son
las averías concretas que se temen, escritas como código que compila:

  - que la banda de en medio desaparezca y no se recomiende «intensa» nunca más
  - que la salida de hoy entre en su propio consejo
  - que la ventana de 180 días se calcule sobre una caché de 90
  - que un día sin base se vuelva mudo en vez de decir que no tiene base
  - que el recuento de intensas se pierda del mensaje entero

Reglas de la casa: `newline=''` para no tocar los finales de línea, copia con
`shutil.copy2` y restauración en un `finally` (NUNCA `git checkout`: hay
trabajo sin commitear, y esa lección costó una recuperación desde el
transcript), y la aguja tiene que ser única en el fichero.

La falsación contra DATOS reales es otra cosa y vive aparte:
`scripts/falsear_bici.py`.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

FICHEROS = [
    "tests/test_bike_advisor.py",
    "tests/test_message.py",
    "tests/test_config_loader.py",
    "tests/test_activity_cache.py",
]

# (nombre, fichero, aguja, reemplazo, qué debería morir)
MUTACIONES = [
    # --- las tres bandas ---------------------------------------------------
    (
        "A. la banda baja se come a la de en medio: nunca más una intensa",
        "app/engine/bike_advisor.py",
        "    if dias < lo:",
        "    if dias < hi:",
        "bandas or fronteras",
    ),
    (
        "B. las dos bandas altas se cambian: volver de un parón pide series",
        "app/engine/bike_advisor.py",
        '        nivel, banda = niveles[2], f"por encima de p{p_high:g} ({hi:.1f})"',
        '        nivel, banda = niveles[1], f"por encima de p{p_high:g} ({hi:.1f})"',
        "bandas or paron",
    ),
    (
        "C. la frontera de abajo pasa de `<` a `<=`",
        "app/engine/bike_advisor.py",
        "    if dias < lo:",
        "    if dias <= lo:",
        "bandas or fronteras",
    ),
    (
        "D. la frontera de arriba pasa de `<` a `<=`",
        "app/engine/bike_advisor.py",
        "    elif dias < hi:",
        "    elif dias <= hi:",
        "bandas or fronteras",
    ),
    # --- las negativas, que son la mitad del diseño ------------------------
    (
        "E. percentiles iguales deja de ser motivo: la banda de 'intensa' no existe y calla",
        "app/engine/bike_advisor.py",
        "    if hi <= lo:",
        "    if False:",
        "percentiles_iguales",
    ),
    (
        "F. `min_gaps` deja de exigirse: dos puntos sueltos hacen de distribución",
        "app/engine/bike_advisor.py",
        "    if len(huecos) < min_gaps:",
        "    if False:",
        "min_gaps or huecos",
    ),
    (
        "G. falta el bloque en el config y se coge un defecto en vez de reventar",
        "app/engine/bike_advisor.py",
        "    if not isinstance(spec, dict):",
        "    if False and not isinstance(spec, dict):",
        "sin_el_bloque or defecto",
    ),
    # --- la fuga temporal --------------------------------------------------
    (
        "H. la salida de HOY entra en su propio consejo",
        "app/engine/bike_advisor.py",
        "r.date < hoy",
        "r.date <= hoy",
        "hoy or propio_consejo",
    ),
    (
        "I. la ventana deja de recortar y se mira el histórico entero",
        "app/engine/bike_advisor.py",
        "    desde = hoy - timedelta(days=window)",
        "    desde = date.min",
        "bike_advisor",
    ),
    # --- el día sin base tiene que VERSE -----------------------------------
    (
        "J. el día sin base vuelve a ser un bloque mudo",
        "app/engine/bike_advisor.py",
        "        out.skip_visible = True",
        "        out.skip_visible = False",
        "sin_base or se_dice",
    ),
    (
        "K. `se_muestra` ignora el día sin base",
        "app/engine/bike_advisor.py",
        "        return self.applies or self.skip_visible",
        "        return self.applies",
        "sin_base or se_dice",
    ),
    (
        "L. el mensaje deja de preguntar por `se_muestra`",
        "app/engine/message.py",
        "decision.bike.se_muestra",
        "decision.bike.applies",
        "sin_base or se_dice or bici",
    ),
    # --- el recuento, que se perdió una vez entero -------------------------
    (
        "M. el recuento de intensas desaparece del mensaje",
        "app/engine/message.py",
        "    conteo = decision.signals.intense_count",
        "    conteo = None",
        "recuento",
    ),
    # --- la caché que sostiene los 180 días --------------------------------
    (
        "N. `dias_adaptativos` vuelve a ignorar la ventana de la bici",
        "app/integrations/activity_cache.py",
        '        ventanas.append(int(gaps.get("window_days", 0)))',
        "        pass",
        "ventana_de_la_bici or backfill or minimo_de_cache",
    ),
    (
        "O. el defecto de `backfill_days` se queda corto otra vez",
        "app/integrations/activity_cache.py",
        'int(fetch.get("backfill_days", 210))',
        'int(fetch.get("backfill_days", 90))',
        "valores_declarados or backfill",
    ),
    (
        "P. el validador vuelve a saltarse el bloque cuando no está declarado",
        "app/config_loader.py",
        "    corta, larga = ventanas_declaradas(data)",
        "    corta, larga = (fetch.get('lookback_days'), fetch.get('backfill_days'))",
        "borrar_cycling_fetch",
    ),
    (
        "Q. el validador deja de comparar el backfill con lo que hace falta",
        "app/config_loader.py",
        "    necesarios = dias_adaptativos(data)",
        "    necesarios = 0",
        "backfill or ventana_de_la_bici",
    ),
]


def sustituir(texto: str, aguja: str, nuevo: str) -> str | None:
    """Sustituye una aguja que tiene que aparecer EXACTAMENTE una vez."""
    for a, n in ((aguja, nuevo), (aguja.replace("\n", "\r\n"), nuevo.replace("\n", "\r\n"))):
        if texto.count(a) == 1:
            return texto.replace(a, n)
    return None


def pytest(patron: str) -> tuple[bool, str]:
    r = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "pytest", *FICHEROS,
         "-k", patron, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=RAIZ, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    # Un patrón que no selecciona NINGÚN test sale con código 5 y no con 0. Aun
    # así se dice, porque una mutación sin tests que la miren no está muerta:
    # está sin vigilar, que es peor porque parece lo mismo desde fuera.
    if "no tests ran" in (r.stdout or ""):
        return True, (r.stdout or "") + "\n  (el patrón no selecciona ningún test)"
    return r.returncode == 0, (r.stdout or "") + (r.stderr or "")


def main() -> int:
    vivas = []
    for nombre, rel, aguja, nuevo, patron in MUTACIONES:
        ruta = RAIZ / rel
        original = io.open(ruta, encoding="utf-8", newline="").read()
        mutado = sustituir(original, aguja, nuevo)
        if mutado is None:
            print(f"[ ?? ] {nombre}\n       aguja ausente o repetida en {rel}")
            vivas.append(nombre)
            continue

        respaldo = ruta.with_suffix(ruta.suffix + ".bak_mut")
        shutil.copy2(ruta, respaldo)
        try:
            io.open(ruta, "w", encoding="utf-8", newline="").write(mutado)
            ok, salida = pytest(patron)
            if ok:
                print(f"[VIVA] {nombre}\n       ningún test de '{patron}' se enteró")
                vivas.append(nombre)
            else:
                fallos = [ln for ln in salida.splitlines() if "FAILED" in ln]
                print(f"[MUER] {nombre}")
                for f in fallos[:2]:
                    print(f"       {f.strip()}")
        finally:
            shutil.copy2(respaldo, ruta)
            respaldo.unlink()

    total = len(MUTACIONES)
    print(f"\n{total - len(vivas)}/{total} mutaciones muertas")
    for v in vivas:
        print(f"  SOBREVIVE: {v}")
    return 1 if vivas else 0


if __name__ == "__main__":
    raise SystemExit(main())
