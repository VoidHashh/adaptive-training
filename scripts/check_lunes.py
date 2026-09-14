"""Falsificación de la regla del lunes de `program.start`.

El defecto que esta regla arregla no era que la fecha estuviera vieja: era que
`_deload` no cuenta desde `program.start`, cuenta desde `week_start()` de
`program.start`. Con un martes escrito en el YAML el origen de verdad era el
lunes anterior, y el valor que se leía no era el valor que se usaba. Un
redondeo correcto que ocurre en silencio sigue siendo un fallo silencioso.

Lo que aquí se falsifica, entonces, no es un cálculo nuevo sino una GUARDA. Y
las guardas son el peor sitio para un test de adorno: una guarda rota no
revienta nada, simplemente deja pasar lo que tenía que parar, y el sistema
sigue funcionando con un origen que nadie escribió.

Se ejecuta con:  ./.venv/Scripts/python.exe scripts/check_lunes.py

Hay una entrada con `esperados=None`. Esa NO tiene que pinchar nada, y el
verificador falla si pincha algo. Es la contrapartida del resto: comprueba que
el redondeo de `week_start()` se ha vuelto inerte, que es justo lo que la regla
del lunes compra. Si algún día se pone en rojo, es que el origen ha vuelto a
caer a media semana por algún camino que la validación no cubre.
"""

from __future__ import annotations

import codecs
import os
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
PY = RAIZ / ".venv" / "Scripts" / "python.exe"

LOADER = "app/config_loader.py"
DECISION = "app/engine/decision.py"
CONFIG = "config.yaml"

# Los tres ficheros donde vive algo que dependa del origen del programa.
SUITE = [
    "tests/test_config_loader.py",
    "tests/test_decision.py",
    "tests/test_recalibracion.py",
]


# (nombre, fichero, viejo, nuevo, [tests que TIENEN que caer] | None si verde)
MUTACIONES: list[tuple[str, str, str, str, list[str] | None]] = [
    # --- la guarda existe ---------------------------------------------------
    (
        "la regla del lunes desaparece",
        LOADER,
        "    elif prog_start.weekday() != 0:",
        "    elif False:",
        [
            "test_program_start_invalido_impide_arrancar[martes]",
            "test_program_start_invalido_impide_arrancar[miercoles]",
            "test_program_start_invalido_impide_arrancar[jueves]",
            "test_program_start_invalido_impide_arrancar[viernes]",
            "test_program_start_invalido_impide_arrancar[sabado]",
            "test_program_start_invalido_impide_arrancar[domingo]",
            "test_el_error_del_lunes_dice_qué_día_es_y_cuál_sería_el_lunes",
        ],
    ),
    (
        "la regla del lunes, del revés: acepta todo menos el lunes",
        LOADER,
        "    elif prog_start.weekday() != 0:",
        "    elif prog_start.weekday() == 0:",
        # El control negativo es el que importa aquí. Sin él, invertir la regla
        # dejaría los seis casos de arriba en rojo -bien- pero nadie estaría
        # comprobando que el config REAL la pasa.
        [
            "test_el_program_start_del_config_real_es_lunes",
            "test_program_start_valida_es_accesible_como_date",
        ],
    ),
    (
        "solo se rechaza el domingo (la guarda se estrecha)",
        LOADER,
        "    elif prog_start.weekday() != 0:",
        "    elif prog_start.weekday() == 6:",
        [
            "test_program_start_invalido_impide_arrancar[martes]",
            "test_program_start_invalido_impide_arrancar[sabado]",
        ],
    ),
    # --- el mensaje sirve para arreglarlo -----------------------------------
    # Una guarda que para el arranque sin decir qué escribir obliga a mirar un
    # calendario, y quien lo mire puede contar mal igual que contó mal al
    # escribir la fecha.
    (
        "el error no dice qué día de la semana es",
        LOADER,
        "f\"program.start '{prog_start}' es {DIAS_ES[prog_start.weekday()]}, y \"",
        "f\"program.start '{prog_start}' es un mal día, y \"",
        ["test_el_error_del_lunes_dice_qué_día_es_y_cuál_sería_el_lunes"],
    ),
    (
        "el error no calcula el lunes: repite la fecha mala",
        LOADER,
        "        lunes_real = prog_start - timedelta(days=prog_start.weekday())",
        "        lunes_real = prog_start",
        ["test_el_error_del_lunes_dice_qué_día_es_y_cuál_sería_el_lunes"],
    ),
    (
        "DIAS_ES desalineado: el sábado pasa a llamarse domingo",
        LOADER,
        '    "sábado",\n    "domingo",',
        '    "domingo",\n    "sábado",',
        # El primer intento rotaba las dos PRIMERAS entradas, que no tocan el
        # índice 5 y dejaban la mutación sin efecto sobre el caso que el test
        # mira. La mutación estaba mal, no el test.
        ["test_el_error_del_lunes_dice_qué_día_es_y_cuál_sería_el_lunes"],
    ),
    # --- el techo de recalibrado_el -----------------------------------------
    (
        "el techo de recalibrado_el vuelve a ser hoy a secas",
        LOADER,
        "        techo = max(date.today(), prog_start)",
        "        techo = date.today()",
        # HOY esta mutación también tumba el config entero, porque el arranque
        # (2026-09-14) todavía es futuro y `recalibrado_el` vale lo mismo. A
        # partir del 15 ese efecto desaparece y el único que queda vigilando es
        # el test dedicado, que por eso se inventa su propio lunes futuro en vez
        # de fiarse del que hay en el YAML.
        ["test_el_techo_es_el_ultimo_entre_hoy_y_el_arranque"],
    ),
    # --- el config de verdad ------------------------------------------------
    (
        "program.start vuelve a caer a media semana en el config real",
        CONFIG,
        "  start: 2026-09-14",
        "  start: 2026-09-15",
        [
            "test_el_program_start_del_config_real_es_lunes",
            "test_program_start_valida_es_accesible_como_date",
            "test_el_config_del_repo_carga_desde_disco",
        ],
    ),
    # --- la contrapartida: esto TIENE que quedarse en verde -----------------
    (
        "`_deload` deja de redondear al lunes (inerte: el origen ya es lunes)",
        DECISION,
        "    origin = week_start(start_ref)",
        "    origin = start_ref",
        None,
    ),
]


def _desescapa(nombre: str) -> str:
    """Devuelve los acentos a los identificadores de `parametrize`.

    pytest escapa a ASCII lo que va entre corchetes (`est\\xe1 vac\\xedo`) pero no
    el nombre de la función. Comparar los nombres tal cual daba por vivas
    mutaciones que sí estaban pinchando. Un verificador que se equivoca en verde
    es peor que no tenerlo.
    """
    if "[" not in nombre:
        return nombre
    base, _, ids = nombre.partition("[")
    try:
        ids = codecs.decode(ids.rstrip("]"), "unicode_escape")
    except Exception:  # noqa: BLE001
        return nombre
    return f"{base}[{ids}]"


def rojos() -> set[str]:
    """Los tests que fallan ahora mismo, por nombre completo con parámetros."""
    r = subprocess.run(
        [str(PY), "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", *SUITE],
        cwd=RAIZ, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    out = set()
    for linea in r.stdout.splitlines():
        if linea.startswith("FAILED ") or linea.startswith("ERROR "):
            ruta = linea.split(" ", 1)[1].split(" - ")[0].strip()
            out.add(_desescapa(ruta.split("::")[-1]))
    return out


def main() -> int:
    # Dos contadores, no uno. Ver el comentario largo en
    # `scripts/check_recalibracion.py::main`, donde este mismo fallo se dio de
    # verdad: una mutación cuyo fragmento ya no existía llevaba meses sumando
    # al total de «mutaciones sin test que las pille», y eso es lo contrario de
    # lo que pasaba. Un [SETUP] fallido no es un agujero en los tests: es que
    # de ese trozo de código esta batería no ha mirado nada.
    verdes = 0
    caducadas = 0
    print(f"{len(MUTACIONES)} mutaciones\n")
    for nombre, rel, viejo, nuevo, esperados in MUTACIONES:
        ruta = RAIZ / rel
        original = ruta.read_text(encoding="utf-8")
        if original.count(viejo) != 1:
            print(f"[SETUP] {nombre}: el fragmento aparece "
                  f"{original.count(viejo)} veces en {rel}, no 1")
            caducadas += 1
            continue
        try:
            ruta.write_text(original.replace(viejo, nuevo), encoding="utf-8")
            caidos = rojos()
        finally:
            ruta.write_text(original, encoding="utf-8")

        if esperados is None:
            if caidos:
                verdes += 1
                print(f"[ROJO INESPERADO] {nombre}")
                for t in sorted(caidos):
                    print(f"         ha pinchado: {t}")
            else:
                print(f"[ok] {nombre}  (verde, como tenía que ser)")
            continue

        faltan = [t for t in esperados if t not in caidos]
        if faltan:
            verdes += 1
            print(f"[VERDE] {nombre}")
            for t in faltan:
                print(f"         sigue pasando: {t}")
        else:
            print(f"[ok] {nombre}  ({len(caidos)} rojo/s)")

    print()
    if caducadas:
        print(f"{caducadas} mutación/es CADUCADAS: el código que mutaban ya no "
              f"está, así que no se ha probado nada de esa parte. No es que los "
              f"tests fallen: es que aquí no se ha mirado. Arréglalas o "
              f"bórralas.")
    if verdes:
        print(f"{verdes} mutación/es sin test que las pille.")
    if verdes or caducadas:
        return 1
    print("Todas las mutaciones pinchan. Los tests comprueban lo que dicen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
