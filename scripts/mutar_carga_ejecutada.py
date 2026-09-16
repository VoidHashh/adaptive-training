"""¿Sirven de algo los tests de la carga ejecutada?

Se rompe el código a propósito y se comprueba que ALGÚN test se entera. Un test
que sobrevive a la mutación que dice vigilar no vigila nada, y aquí eso se paga
caro: todas las mutaciones de esta lista producen un sistema que ARRANCA, decide,
escribe la rutina y manda el mensaje. Ninguna da un error. Lo único que cambia es
el peso que acaba en la barra.

Las trece de abajo no son hipótesis: doce son el comportamiento REAL del sistema
antes del punto 13 o un paso en falso plausible al escribirlo, y la número uno
-el peso fuera del cumplimiento- estuvo en producción.

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

TODOS = [
    "tests/test_hevy_reconcile.py",
    "tests/test_adoption.py",
    "tests/test_repository.py",
    "tests/test_message.py",
    "tests/test_runner.py",
    "tests/test_config_loader.py",
]

# (nombre, fichero, aguja, reemplazo, ficheros de test donde debería morir)
MUTACIONES = [
    (
        "A. el peso vuelve a quedarse fuera del cumplimiento (el fallo real)",
        "app/integrations/hevy.py",
        "if objetivo_kg is not None and float(objetivo_kg) > 0:",
        "if False:",
        ["tests/test_hevy_reconcile.py", "tests/test_runner.py"],
    ),
    (
        "B. el epsilon de coma flotante se convierte en una tolerancia de 5 kg",
        "app/integrations/hevy.py",
        "if float(hecho_kg) < float(objetivo_kg) - 1e-6:",
        "if float(hecho_kg) < float(objetivo_kg) - 5.0:",
        ["tests/test_hevy_reconcile.py"],
    ),
    (
        "C. 'sin peso apuntado' pasa a valer 0 kg en vez de 'no hay dato'",
        "app/integrations/hevy.py",
        "salida[key] = max(pesos) if pesos else None",
        "salida[key] = max(pesos) if pesos else 0.0",
        ["tests/test_hevy_reconcile.py", "tests/test_adoption.py"],
    ),
    (
        "D. un plan todo calentamiento vuelve a salir 'completado' sin mirar nada",
        "app/integrations/hevy.py",
        "if plan_sets and not objetivo:",
        "if False:",
        ["tests/test_hevy_reconcile.py"],
    ),
    (
        "E. se adopta hacia arriba aunque la sesión no se completara",
        "app/engine/adoption.py",
        "if not limpio.get(key, False):",
        "if False:",
        ["tests/test_adoption.py"],
    ),
    (
        "F. se baja la carga a la primera sesión floja",
        "app/engine/adoption.py",
        "if racha < necesarias:",
        "if racha < 1:",
        ["tests/test_adoption.py"],
    ),
    (
        "G. al bajar se adopta la última sesión y no la mejor de la racha",
        "app/engine/adoption.py",
        "mejor = max(float(state.below_plan_best_kg.get(clave) or 0), hecho)",
        "mejor = hecho",
        ["tests/test_adoption.py"],
    ),
    (
        "H. desaparece el tope de salto: un 600 por un 60 entra tal cual",
        "app/engine/adoption.py",
        "if objetivo > 0 and abs(delta) > margen:",
        "if False:",
        ["tests/test_adoption.py"],
    ),
    (
        "I. se adopta el peso absoluto y la rampa 50/60/65 se aplana a 65/65/65",
        "app/engine/adoption.py",
        'nueva["weight_kg"] = round(max(0.0, actual + delta), 3)',
        'nueva["weight_kg"] = round(max(0.0, max(\n'
        '            float(x.get("weight_kg") or 0) for x in series\n'
        "        ) + delta), 3)",
        ["tests/test_adoption.py"],
    ),
    (
        "J. quedarse corto se mide contra el objetivo y no contra el plan del día:"
        " una descarga bien hecha baja la carga",
        "app/engine/adoption.py",
        "if hecho >= prescrito:",
        "if hecho >= objetivo:",
        ["tests/test_adoption.py"],
    ),
    (
        "K. un día sin peso apuntado rompe la racha por debajo",
        "app/engine/adoption.py",
        "if hecho is None:",
        "if hecho is None and _reset_por_debajo(state, clave) is None:",
        ["tests/test_adoption.py"],
    ),
    (
        "L. la racha por debajo anulada no se borra de la tabla y se queda clavada",
        "app/repository.py",
        "fila.below_plan_streak = int(state.below_plan_streak.get((rutina, ejercicio), 0))",
        "fila.below_plan_streak = int(\n"
        "            state.below_plan_streak.get((rutina, ejercicio), fila.below_plan_streak or 0)\n"
        "        )",
        ["tests/test_repository.py"],
    ),
    (
        "M. las adopciones rechazadas dejan de contarse: el tope actúa en silencio",
        "app/engine/message.py",
        'rechazadas = [a for a in adopciones if not a.get("applied")]',
        "rechazadas = []",
        ["tests/test_message.py"],
    ),
    (
        "N. las adopciones se sellan aunque Telegram falle y nadie las lea nunca",
        "app/runner.py",
        'if res.telegram_status in {"sent", "dry_run"}:',
        "if True:",
        ["tests/test_runner.py", "tests/test_api.py"],
    ),
    (
        "O. la tabla de adopciones no llega nunca a una base que ya existia",
        "app/db.py",
        "Base.metadata.create_all(engine)\n    ensure_schema(engine)",
        "ensure_schema(engine)",
        ["tests/test_db_schema.py"],
    ),
    (
        "P. la racha por debajo se migra sin su defecto y entra a NULL",
        "app/db.py",
        "    return f\"{'' if col.nullable else ' NOT NULL'} DEFAULT {literal}\"",
        '    return ""',
        ["tests/test_db_schema.py"],
    ),
]


def sustituir(texto: str, aguja: str, nuevo: str) -> str | None:
    """Sustituye una aguja que tiene que aparecer EXACTAMENTE una vez."""
    for a, n in ((aguja, nuevo), (aguja.replace("\n", "\r\n"), nuevo.replace("\n", "\r\n"))):
        if texto.count(a) == 1:
            return texto.replace(a, n)
    return None


def pytest(ficheros: list[str]) -> tuple[bool, str]:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", *ficheros, "-q", "--no-header",
         "-x", "-p", "no:cacheprovider"],
        cwd=RAIZ, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return r.returncode == 0, (r.stdout or "") + (r.stderr or "")


def main() -> int:
    solo = sys.argv[1] if len(sys.argv) > 1 else ""
    # Dos listas, no una: ver el comentario largo en `scripts/mutar_bici.py`.
    # Una aguja que ya no está en el fichero no es un agujero en los tests, es
    # una mutación que no se ha llegado a hacer.
    vivas = []
    caducadas = []
    for nombre, rel, aguja, nuevo, ficheros in MUTACIONES:
        if solo and not nombre.startswith(solo):
            continue
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
            ok, salida = pytest(ficheros)
            if ok:
                print(f"[VIVA] {nombre}\n       ningún test de {ficheros} se enteró")
                vivas.append(nombre)
            else:
                fallos = [ln for ln in salida.splitlines() if "FAILED" in ln or "failed" in ln]
                print(f"[MUER] {nombre}")
                for f in fallos[:2]:
                    print(f"       {f.strip()}")
        finally:
            shutil.copy2(respaldo, ruta)
            respaldo.unlink()

    total = len([m for m in MUTACIONES if not solo or m[0].startswith(solo)])
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
