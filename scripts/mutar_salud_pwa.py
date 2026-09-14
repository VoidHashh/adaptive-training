"""¿Sirven de algo los tests del panel de salud?

Se rompe el código a propósito y se comprueba que ALGÚN test se entera. Un test
que sobrevive a la mutación que dice vigilar no vigila nada.

Reglas de la casa: `newline=''` para no tocar los finales de línea, copia con
`shutil.copy2` y restauración en un `finally` (nunca `git checkout`: hay trabajo
sin commitear), y la aguja tiene que ser única en el fichero.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

# (nombre, fichero, aguja, reemplazo, qué debería morir)
MUTACIONES = [
    (
        "A. la API renombra secrets_missing y la PWA se queda con el nombre viejo",
        "app/api.py",
        '"secrets_missing": settings.missing_secrets(),',
        '"secretos_que_faltan": settings.missing_secrets(),',
        "health_sigue_diciendo",
    ),
    (
        "B. se borra la llamada y la función se queda ahí, sin que nadie la use",
        "static/app.js",
        "\ncomprobarSalud();",
        "\n// comprobarSalud();",
        "un_health_que_falla",
    ),
    (
        "C. la PWA deja de mirar los secretos que faltan",
        "static/app.js",
        "const faltan = s.secrets_missing || [];",
        "const faltan = [];",
        "lee_cada_clave",
    ),
    (
        "D. la PWA deja de mirar el planificador",
        "static/app.js",
        "const plan = s.scheduler || {};",
        "const plan = {running: true, jobs: {x: 1}};",
        "lee_cada_clave",
    ),
    (
        "E. desaparece el aviso de modo en seco",
        "static/app.js",
        '      titulo: "Modo en seco.",',
        '      titulo: "Todo correcto.",',
        "modo_en_seco",
    ),
    (
        "F. el panel se encadena al arranque y un health lento retrasa el formulario",
        "static/app.js",
        "\ncomprobarSalud();",
        "\nawait comprobarSalud();",
        "un_health_que_falla",
    ),
    (
        "G. el planificador deja de decir si está en marcha",
        "app/api.py",
        '            "running": False,',
        '            "en_marcha": False,',
        "health_sigue_diciendo",
    ),
    (
        "H. la PWA deja de mirar el reloj",
        "static/app.js",
        "const reloj = s.clock || {};",
        "const reloj = {matches: true};",
        "lee_cada_clave",
    ),
    (
        "I. el aviso del reloj vuelve a ser incapaz de fallar",
        "app/api.py",
        '        "matches": del_reloj == de_las_reglas,',
        '        "matches": True,',
        "aviso_del_reloj",
    ),
    (
        "J. una zona mal escrita tumba /api/health en vez de contarlo",
        "app/api.py",
        "    except Exception as e:  # zona escrita mal en el `config.yaml`",
        "    except ZeroDivisionError as e:  # ya no atrapa lo que pasa de verdad",
        "zona_mal_escrita",
    ),
    # -----------------------------------------------------------------------
    # La rutina huérfana: lo que quedó en Hevy de una decisión anulada.
    # -----------------------------------------------------------------------
    (
        "K. se mira cualquier día y la huérfana de ayer avisa hoy",
        "app/api.py",
        "            .where(HevyWrite.date == hoy)\n",
        "\n",
        "huerfana",
    ),
    (
        "L. se mira la PRIMERA escritura del día en vez de la última",
        "app/api.py",
        ".order_by(HevyWrite.id.desc())",
        ".order_by(HevyWrite.id.asc())",
        "huerfana",
    ),
    (
        "M. cualquier escritura del día cuenta como huérfana",
        "app/api.py",
        'if fila is None or fila.status != "stale":',
        "if fila is None:",
        "huerfana",
    ),
    (
        "N. una fila `stale` anterior a la columna `reason` se calla",
        "app/api.py",
        'return (fila.reason or\n            "en Hevy quedó una rutina de una decisión anulada y no se ha "\n            "podido deshacer"), None',
        "return fila.reason, None",
        "huerfana",
    ),
    (
        "O. no poder mirar la huérfana se traga y sale como que no la hay",
        "app/api.py",
        'return None, f"no se ha podido mirar si hoy quedó una rutina huérfana: {e}"',
        "return None, None",
        "huerfana",
    ),
    (
        "P. la huérfana se calcula y no llega al veredicto",
        "app/api.py",
        '    if esc.get("stale_write"):',
        "    if False:",
        "huerfana or pantalla_avisa",
    ),
    (
        "Q. no haber podido mirarla no llega al veredicto",
        "app/api.py",
        '    if esc.get("stale_error"):',
        "    if False:",
        "huerfana or pantalla_avisa",
    ),
    (
        "R. la marca de escritura ilegible no llega al veredicto",
        "app/api.py",
        '    if esc.get("pending_error"):',
        "    if False:",
        "marca_de_escritura or pantalla_avisa",
    ),
    # Estas dos dejan el bloque del aviso ESCRITO y le matan la condición. Es
    # la forma que tiene este proyecto de comprobar que el cruce por texto de
    # `test_la_pantalla_avisa_de_todo...` no se conforma con que la palabra
    # aparezca en el fichero: las dos sobrevivieron a la batería entera hasta
    # que se añadió el arnés de Node que PINTA los avisos de verdad.
    (
        "S. el aviso de la huérfana está escrito y su condición es imposible",
        "static/app.js",
        "if (esc.stale_write) {",
        "if (false) {",
        "pantalla_pinta",
    ),
    (
        "T. el aviso de la marca ilegible está escrito y no se pinta nunca",
        "static/app.js",
        "if (esc.pending_error) {",
        "if (false) {",
        "pantalla_pinta",
    ),
]


def sustituir(texto: str, aguja: str, nuevo: str) -> str | None:
    """Sustituye una aguja que tiene que aparecer EXACTAMENTE una vez."""
    for a, n in ((aguja, nuevo), (aguja.replace("\n", "\r\n"), nuevo.replace("\n", "\r\n"))):
        if texto.count(a) == 1:
            return texto.replace(a, n)
    return None


def pytest(patron: str) -> tuple[bool, str]:
    """Los dos ficheros, porque los guardianes viven repartidos.

    Empezó mirando sólo `test_api.py`, que es donde están los tests del
    endpoint. Los que cruzan el servidor con la pantalla -el que exige que cada
    problema tenga su aviso, y el que ejecuta el JavaScript en Node para
    comprobar que el aviso SE PINTA- están en `test_pwa.py`, y sin ellos las
    mutaciones de `static/app.js` no tenían quien las matara.
    """
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_api.py", "tests/test_pwa.py",
         "-k", patron, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=RAIZ, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    # Un patrón que no selecciona NINGÚN test sale con código 5 y no con 0, o
    # sea que no se confunde con "los tests pasaron". Aun así se dice, porque
    # una mutación sin tests que la miren no es una mutación muerta.
    if "no tests ran" in (r.stdout or ""):
        return True, (r.stdout or "") + "\n  (el patrón no selecciona ningún test)"
    return r.returncode == 0, (r.stdout or "") + (r.stderr or "")


def main() -> int:
    # Dos listas, no una: ver el comentario largo en `scripts/mutar_bici.py`.
    # Una aguja que ya no está en el fichero no es un agujero en los tests, es
    # una mutación que no se ha llegado a hacer.
    vivas = []
    caducadas = []
    for nombre, rel, aguja, nuevo, patron in MUTACIONES:
        ruta = RAIZ / rel
        original = io.open(ruta, encoding="utf-8", newline="").read()
        mutado = sustituir(original, aguja, nuevo)
        if mutado is None:
            print(f"[CADU] {nombre}\n       aguja ausente o repetida en {rel}")
            caducadas.append(nombre)
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
    print(f"\n{total - len(vivas) - len(caducadas)}/{total} mutaciones muertas")
    for v in vivas:
        print(f"  SOBREVIVE: {v}")
    for c in caducadas:
        print(f"  CADUCADA (aquí no se ha probado nada): {c}")
    if caducadas:
        print("\nLas caducadas no son un agujero en los tests: es que el código "
              "que mutaban ya no está escrito así. Arregla la aguja o borra la "
              "mutación, pero no la dejes contando como cobertura.")
    return 1 if (vivas or caducadas) else 0


if __name__ == "__main__":
    raise SystemExit(main())
