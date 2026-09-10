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
]


def sustituir(texto: str, aguja: str, nuevo: str) -> str | None:
    """Sustituye una aguja que tiene que aparecer EXACTAMENTE una vez."""
    for a, n in ((aguja, nuevo), (aguja.replace("\n", "\r\n"), nuevo.replace("\n", "\r\n"))):
        if texto.count(a) == 1:
            return texto.replace(a, n)
    return None


def pytest(patron: str) -> tuple[bool, str]:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_api.py", "-k", patron, "-q",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=RAIZ, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
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
