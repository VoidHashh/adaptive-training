"""¿Sirven de algo los tests de la carga ejecutada?

Se rompe el código a propósito y se comprueba que ALGÚN test se entera. Un test
que sobrevive a la mutación que dice vigilar no vigila nada, y aquí eso se paga
caro: todas las mutaciones de esta lista producen un sistema que ARRANCA, decide,
escribe la rutina y manda el mensaje. Ninguna da un error. Lo único que cambia es
el peso que acaba en la barra.

Las de abajo no son hipótesis: son el comportamiento REAL del sistema antes del
punto 13 o un paso en falso plausible al escribirlo. Dos estuvieron en
producción: la A -el peso fuera del cumplimiento- y la I, que es la forma que
tenía hasta el 25/09/2026 el fallo de las rampas (una aducción hecha a 50/60/80
guardada como 55/70/80).

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
        # Desde el 25/09/2026 la condición lleva delante `not ignorar_peso`, y
        # hasta que esto se corrigió esa misma tarde la aguja vieja no aparecía:
        # la mutación del fallo que SÍ estuvo en producción no se probaba.
        "if not ignorar_peso and objetivo_kg is not None and float(objetivo_kg) > 0:",
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
        "return max(pesos) if pesos else None",
        "return max(pesos) if pesos else 0.0",
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
        # Desde el 25/09/2026 subir se decide con `limpio_arriba`, el veredicto
        # que no mira el peso de cada serie (ver `app/engine/adoption.py`), y
        # ese nombre ya es único: la aguja no necesita la línea de encima.
        "if not limpio_arriba.get(key, False):",
        "if False:",
        ["tests/test_adoption.py"],
    ),
    (
        "E2. un ejercicio sin un solo peso apuntado se congela en silencio",
        "app/engine/adoption.py",
        # Aquí la aguja lleva la línea de DEBAJO, que es lo que distingue esta
        # rama de la de arriba: la otra sigue con un comentario.
        "if not limpio.get(key, False):\n                salida.append(",
        "if False:\n                salida.append(",
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
        "if previa is None or hecho > (tope_apuntado(previa) or 0.0):",
        "if True:",
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
        # Hasta el 25/09/2026 esta era «se adopta el peso absoluto y la rampa se
        # aplana», contra `_desplazar`. Ese desplazamiento ERA el fallo: una
        # rampa hecha sobre un objetivo plano quedaba con todas las series al
        # tope. La mutación sigue siendo la misma idea contra el código nuevo.
        "I. se adopta el tope en todas las series y 30/40/50 se aplana a 50/50/50",
        "app/engine/adoption.py",
        'nueva["weight_kg"] = round(float(hecha["weight_kg"]), 3)',
        'nueva["weight_kg"] = round(max(float(h["weight_kg"]) for h in elegidas), 3)',
        ["tests/test_adoption.py", "tests/test_runner.py"],
    ),
    (
        "I2. la serie de más, que el plan del día no contaba, se adopta sin mirar"
        " sus reps",
        "app/engine/adoption.py",
        'if falla is not None:\n            return f"la serie de',
        'if False:\n            return f"la serie de',
        ["tests/test_adoption.py"],
    ),
    (
        "I3. en una sesión partida se adoptan las series del último rato y no"
        " las del más pesado",
        "app/runner.py",
        "if previo is None or kg > previo:",
        "if True:",
        ["tests/test_runner.py"],
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
        # Con la línea de debajo: desde el 25/09/2026 `_aplicar` también tiene
        # un `if hecho is None:`, la guarda de «sin una sola serie con peso».
        "if hecho is None:\n            # Ni se hizo",
        "if hecho is None and _reset_por_debajo(state, clave) is None:\n"
        "            # Ni se hizo",
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
        'if not a.get("applied") and a.get("executed_kg") is not None',
        "if False",
        ["tests/test_message.py"],
    ),
    (
        "M2. las incidencias sin peso no llegan al mensaje y el ejercicio se queda quieto sin decirlo",
        "app/engine/message.py",
        'if not a.get("applied") and a.get("executed_kg") is None',
        "if False",
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
