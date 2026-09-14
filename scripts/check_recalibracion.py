"""Falsificación de `tests/test_recalibracion.py`: ¿pincha algo cada mutación?

La práctica que pidió el usuario y que aquí importa más que en ningún otro
sitio: un recordatorio roto no revienta, se calla, y un test verde sobre un
recordatorio mudo es exactamente la figura que este repositorio lleva meses
persiguiendo -la pieza que tenía que avisar del hueco certificando que no lo
hay-.

Cada entrada de abajo rompe UNA cosa del recordatorio y anota qué tests tienen
que ponerse rojos. Si alguno de esos tests pasa con la mutación puesta, ese test
no estaba comprobando lo que su nombre dice.

Se ejecuta con:  ./.venv/Scripts/python.exe scripts/check_recalibracion.py

Los ficheros se restauran desde una copia en memoria y en un `finally`, no con
`git checkout --`: dos de estos ficheros no están en git todavía la primera vez
que esto se ejecuta, y un `checkout` que falla en silencio deja la mutación
puesta.
"""

from __future__ import annotations

import codecs
import os
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
PY = RAIZ / ".venv" / "Scripts" / "python.exe"

RECAL = "app/engine/recalibracion.py"
REPO = "app/repository.py"
LOADER = "app/config_loader.py"
RUNNER = "app/runner.py"
MENSAJE = "app/engine/message.py"


# (nombre, fichero, viejo, nuevo, [tests que TIENEN que caer])
MUTACIONES: list[tuple[str, str, str, str, list[str]]] = [
    # --- la cuenta ---------------------------------------------------------
    (
        "el aviso llega un día tarde (>= por >)",
        RECAL,
        "        return self.dias >= self.cada",
        "        return self.dias > self.cada",
        ["test_no_avisa_hasta_que_la_muestra_esta_completa", "test_de_la_base_de_datos_al_telegram"],
    ),
    (
        "el aviso sale todos los días desde el principio",
        RECAL,
        "        if not self.toca:\n            return []",
        "        if False:\n            return []",
        [
            "test_no_avisa_hasta_que_la_muestra_esta_completa",
            "test_la_cuenta_atras_no_se_enseña_mientras_no_toca",
            "test_mientras_no_toca_el_mensaje_no_lo_menciona",
            "test_un_dia_antes_el_telegram_se_calla",
            "test_la_cuenta_viaja_en_el_json_de_la_decision",
            "test_decidir_un_dia_anterior_a_la_ultima_revision_no_revienta",
        ],
    ),
    (
        "restantes se va a negativo",
        RECAL,
        "        return max(0, self.cada - self.dias)",
        "        return self.cada - self.dias",
        ["test_cuando_toca_no_quedan_dias_negativos"],
    ),
    (
        "una cuenta negativa pasa callando",
        RECAL,
        "    if dias_con_decision < 0:",
        "    if False:",
        ["test_una_cuenta_negativa_es_error_duro"],
    ),
    # --- lo que dice -------------------------------------------------------
    (
        "el aviso no dice cómo callarlo",
        RECAL,
        '            "Para que deje de salir, pon la fecha de hoy en "\n            "program.recalibrado_el. Si de mirarlo sale que no cambias nada, "\n            "cámbiala igual: eso también es haber recalibrado.",',
        '            "Toca revisar.",',
        [
            "test_las_rutas_que_nombra_el_aviso_existen_en_el_config_real[program.recalibrado_el]",
            "test_el_aviso_explica_que_no_cambiar_nada_tambien_cuenta",
            "test_cuando_toca_el_mensaje_de_la_mañana_lo_dice",
        ],
    ),
    # EL FRAGMENTO QUE BUSCABA ESTA MUTACIÓN YA NO EXISTÍA
    # -----------------------------------------------------
    # Nombraba `cycling.weekend.total_hours_threshold` junto a
    # `cycling.classification`, y ese umbral se borró con `resaca_finde`. El
    # `old` dejó de aparecer en `recalibracion.py` y esta entrada llevaba desde
    # entonces dando `[SETUP] el fragmento aparece 0 veces`, que el guión suma
    # al total de «mutaciones sin test que las pille».
    #
    # Es el fallo de esta casa en su versión de andamiaje: una comprobación que
    # dejó de comprobar y siguió contándose. Un [SETUP] fallido no es lo mismo
    # que una mutación que se escapa -uno dice «no he podido probarlo» y el
    # otro «lo he probado y pasa»- y mezclarlos en el mismo contador hace que
    # arreglarlo parezca opcional.
    (
        "el aviso no dice qué umbrales mirar",
        RECAL,
        '            "Toca mirar con /api/export el único umbral que sigue calibrado a "\n            "mano sobre una muestra corta: la clasificación de las salidas de "\n            "bici (cycling.classification). Los demás se recalibran solos "\n            "contra tu histórico (adaptive_thresholds); comprueba de paso que "\n            "sus ventanas siguen teniendo días suficientes.",',
        '            "Toca mirar los umbrales con /api/export.",',
        [
            "test_las_rutas_que_nombra_el_aviso_existen_en_el_config_real[cycling.classification]",
        ],
    ),
    (
        "el aviso no dice los números de la cuenta",
        RECAL,
        '            f"Llevas {self.dias} días con decisión desde la última revisión de "\n            f"umbrales ({self.desde.isoformat()}), y el aviso estaba puesto en "\n            f"{self.cada}.",',
        '            "Toca revisar los umbrales.",',
        ["test_el_aviso_dice_los_dos_numeros_de_la_cuenta"],
    ),
    # --- el contador -------------------------------------------------------
    (
        "cuenta filas en vez de días",
        REPO,
        "            select(func.count(distinct(DecisionRow.date))).where(",
        "            select(func.count(DecisionRow.id)).where(",
        ["test_se_cuentan_dias_y_no_filas"],
    ),
    (
        "el rango invertido devuelve cero callando",
        REPO,
        "    if desde > hasta:",
        "    if False:",
        ["test_un_rango_invertido_es_error_duro"],
    ),
    (
        "el primer día de la ventana no cuenta",
        REPO,
        "                DecisionRow.date >= desde,",
        "                DecisionRow.date > desde,",
        [
            "test_solo_se_cuentan_los_dias_desde_la_ultima_revision",
            "test_los_dias_sin_decision_no_se_inventan",
            "test_se_cuentan_dias_y_no_filas",
            "test_cambiar_otra_cosa_del_config_no_reinicia_la_cuenta",
            "test_la_mañana_cuenta_el_dia_de_hoy",
            "test_de_la_base_de_datos_al_telegram",
        ],
    ),
    (
        "la cuenta se reinicia con el config_hash (el diseño que se descartó)",
        REPO,
        "                DecisionRow.date <= hasta,\n            )",
        "                DecisionRow.date <= hasta,\n                DecisionRow.config_hash == 'despues',\n            )",
        ["test_cambiar_otra_cosa_del_config_no_reinicia_la_cuenta"],
    ),
    # --- el YAML -----------------------------------------------------------
    (
        "se puede borrar recalibrar_cada_dias y arrancar igual",
        LOADER,
        "    cada = (data.get(\"program\") or {}).get(\"recalibrar_cada_dias\")\n    if cada is None:",
        "    cada = (data.get(\"program\") or {}).get(\"recalibrar_cada_dias\")\n    if False:",
        [
            "test_borrar_una_de_las_dos_claves_impide_arrancar[recalibrar_cada_dias-program.recalibrar_cada_dias está vacío]",
            "test_una_errata_en_el_nombre_de_la_clave_impide_arrancar",
        ],
    ),
    (
        "se puede borrar recalibrado_el y arrancar igual",
        LOADER,
        "    recal = (data.get(\"program\") or {}).get(\"recalibrado_el\")\n    if recal is None:",
        "    recal = (data.get(\"program\") or {}).get(\"recalibrado_el\")\n    if False:",
        ["test_borrar_una_de_las_dos_claves_impide_arrancar[recalibrado_el-program.recalibrado_el está vacío]"],
    ),
    (
        "program sin lista blanca: una errata pasa",
        LOADER,
        '        {"start", "recalibrar_cada_dias", "recalibrado_el"},\n        "program",',
        '        {"start", "recalibrar_cada_dias", "recalibrado_el", "recalibrar_cada"},\n        "program",',
        ["test_una_errata_en_el_nombre_de_la_clave_impide_arrancar"],
    ),
    (
        "cada_dias admite cualquier cosa",
        LOADER,
        "    elif not _es_num(cada) or int(cada) != cada or int(cada) < 1:",
        "    elif False:",
        [
            "test_un_cada_dias_que_no_es_un_entero_de_dias_impide_arrancar[cero]",
            "test_un_cada_dias_que_no_es_un_entero_de_dias_impide_arrancar[negativo]",
            "test_un_cada_dias_que_no_es_un_entero_de_dias_impide_arrancar[con_decimales]",
            "test_un_cada_dias_que_no_es_un_entero_de_dias_impide_arrancar[texto]",
            "test_un_cada_dias_que_no_es_un_entero_de_dias_impide_arrancar[booleano]",
        ],
    ),
    # Las dos formas de escribir mal la fecha se detectan en dos ramas
    # distintas del `elif`, así que van en dos mutaciones. Juntarlas fue el
    # primer error de este mismo fichero: una sola mutación dejaba viva la otra
    # rama y dos de los tres casos seguían en verde con razón.
    (
        "recalibrado_el con hora pasa",
        LOADER,
        "    elif isinstance(recal, datetime):",
        "    elif False:",
        ["test_un_recalibrado_el_mal_escrito_impide_arrancar[con_hora]"],
    ),
    (
        "recalibrado_el como texto pasa",
        LOADER,
        "    elif not isinstance(recal, date):\n        try:\n            datetime.strptime(str(recal), \"%Y-%m-%d\")",
        "    elif False:\n        try:\n            datetime.strptime(str(recal), \"%Y-%m-%d\")",
        [
            "test_un_recalibrado_el_mal_escrito_impide_arrancar[entrecomillada]",
            "test_un_recalibrado_el_mal_escrito_impide_arrancar[texto_libre]",
        ],
    ),
    (
        "recalibrado_el anterior al arranque pasa",
        LOADER,
        "        if recal < prog_start:",
        "        if False:",
        ["test_recalibrado_el_antes_del_arranque_impide_arrancar"],
    ),
    (
        "recalibrado_el en el futuro pasa",
        LOADER,
        "        elif recal > techo:",
        "        elif False:",
        ["test_recalibrado_el_en_el_futuro_impide_arrancar"],
    ),
    (
        # El techo dejó de ser `hoy` a secas cuando `program.start` se movió al
        # lunes de arranque real, que el día del cambio todavía era futuro: el
        # mínimo pedía >= start y el máximo <= hoy, y no quedaba ningún valor
        # entre los dos. Aflojar el mínimo en vez del máximo habría sido la
        # tentación fácil, y habría dejado la cuenta arrancando en días que el
        # programa no vivió. Esta mutación vigila que no se haga.
        "el suelo de recalibrado_el desaparece",
        LOADER,
        "        if recal < prog_start:",
        "        if False:",
        ["test_recalibrado_el_antes_del_arranque_impide_arrancar"],
    ),
    (
        "el accesor se inventa un 28 por defecto",
        LOADER,
        '        return int(self._data["program"]["recalibrar_cada_dias"])',
        '        return int(self._data["program"].get("recalibrar_cada_dias", 28))',
        ["test_el_accesor_no_se_inventa_un_valor_por_defecto"],
    ),
    # --- la mañana ---------------------------------------------------------
    (
        "la cuenta se hace antes de guardar: hoy no cuenta",
        RUNNER,
        "    fila = repo.save_decision(session, decision)\n",
        "    fila = None\n",
        [
            "test_la_mañana_cuenta_el_dia_de_hoy",
            "test_de_la_base_de_datos_al_telegram",
        ],
    ),
    (
        "la mañana no cuelga la cuenta en la decisión",
        RUNNER,
        "    decision.recalibracion = evaluar_recalibracion(cfg, dias)",
        "    pass",
        [
            "test_la_mañana_cuenta_el_dia_de_hoy",
            "test_decidir_un_dia_anterior_a_la_ultima_revision_no_revienta",
            "test_de_la_base_de_datos_al_telegram",
            "test_un_dia_antes_el_telegram_se_calla",
        ],
    ),
    # --- el mensaje --------------------------------------------------------
    (
        "el aviso no llega al mensaje",
        MENSAJE,
        "    if lineas_recal:",
        "    if False:",
        [
            "test_cuando_toca_el_mensaje_de_la_mañana_lo_dice",
            "test_el_aviso_sale_aunque_el_razonamiento_este_apagado",
            "test_de_la_base_de_datos_al_telegram",
        ],
    ),
    (
        "el aviso se esconde con el razonamiento apagado",
        MENSAJE,
        "    recalibracion = getattr(decision, \"recalibracion\", None)",
        "    recalibracion = getattr(decision, \"recalibracion\", None) if incluir_motivo else None",
        ["test_el_aviso_sale_aunque_el_razonamiento_este_apagado"],
    ),
]


def _desescapa(nombre: str) -> str:
    """Devuelve los acentos a los identificadores de `parametrize`.

    pytest escapa a ASCII lo que va entre corchetes (`est\\xe1 vac\\xedo`) pero no
    el nombre de la función, así que comparar los nombres tal cual daba por
    vivas tres mutaciones que en realidad estaban pinchando: el fallo estaba en
    este comparador, no en los tests. Un verificador que se equivoca en verde es
    peor que no tenerlo.
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
        [str(PY), "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
         "tests/test_recalibracion.py"],
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
    # DOS CONTADORES, NO UNO, Y NO ES UN DETALLE
    # -------------------------------------------
    # Antes esto era un `fallos` solo, y al final imprimía «N mutación/es sin
    # test que las pille». Eso mezcla dos cosas que no se parecen en nada:
    #
    #   [VERDE] = la mutación se aplicó, los tests corrieron, y pasaron. Hay un
    #             agujero de verdad en la batería de tests.
    #   [SETUP] = el fragmento a mutar ya no está en el fichero, así que la
    #             mutación NI SIQUIERA SE PROBÓ. No se sabe nada de ese trozo
    #             de código; lo que hay es una comprobación caducada.
    #
    # Contarlas juntas hace que una comprobación caducada se lea como un
    # agujero en los tests, y -peor- que arreglarla parezca cuestión de gusto.
    # Es exactamente el defecto que este guión existe para cazar, cometido por
    # el propio guión: un `[SETUP]` sobrevivió aquí meses porque su línea se
    # perdía entre los `[ok]` y el resumen final no distinguía.
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
