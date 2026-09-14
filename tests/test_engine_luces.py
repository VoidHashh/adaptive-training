"""Que los tres colores del semáforo se llamen igual en todo el proyecto.

Esto no es un test de una función de siete líneas. Es el test de que no vuelva a
haber SEIS tablas con las mismas tres palabras, que es lo que había cuando se
escribió: `engine/message.py`, `engine/tendencia.py`, `engine/bike_advisor.py`,
`engine/progression.py`, `analysis/auditoria.py` y `analysis/portada.py`, más
una séptima escrita a mano en el JavaScript de la PWA.

Con seis copias no hay un sitio donde añadir un color: hay seis sitios donde
acordarse. Y lo que se cuela por ese hueco no hace ruido. La avería que destapó
todo esto fue una pantalla que ponía

    green: 0,0 % · amber: 100,0 % · red: 0,0 %

justo debajo de unas fichas que ya ponían «0 verde · 1 ámbar · 0 rojo». Ninguna
prueba podía verla: todas las claves existían y todos los valores eran cadenas
perfectamente válidas. Se encontró mirando la pantalla, y se encontró DESPUÉS de
que el repaso automático de `scripts/ver_panel.py` diera las seis vistas por
limpias.

Dos de las seis copias no se encontraron ni con eso. `bike_advisor._light_es`
tenía la tabla metida dentro de un `return`, y `progression.LIGHT_ES` se llamaba
de otra manera: ningún `grep` de `NOMBRE_LUZ` las veía. Aparecieron
preguntándole al AST por diccionarios cuyas claves son exactamente los tres
colores, que es lo que hace el último test de este fichero.

LAS QUE SIGUEN SIENDO TABLAS APARTE, Y POR QUÉ
----------------------------------------------
No todas se pueden derivar de la canónica, y forzarlo sería el error contrario:

  - `portada.NOMBRE_LUZ` guarda el PLURAL: «verde»→«verdes» pero «ámbar»→
    «ámbares». Pegar sufijos en castellano es el fallo que documenta entero
    `app/analysis/texto.py`.
  - `progression.LIGHT_ES` guarda el FEMENINO, porque su frase concuerda con
    «sesión»: «la última sesión fue roja». De las tres solo cambia una.
  - `message.EMOJI` no son palabras.

Las tres se quedan donde están. Lo que tienen que cumplir -y lo comprueba este
fichero- es hablar de los mismos tres colores que el motor, para que un color
nuevo no aparezca en cuatro sitios y falte en dos.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.analysis.auditoria import NOMBRE_LUZ as NOMBRE_AUDITORIA
from app.analysis.portada import NOMBRE_LUZ as PLURAL_PORTADA
from app.engine.luces import LUCES, NOMBRE_LUZ, nombre_luz
from app.engine.message import EMOJI
from app.engine.message import NOMBRE_LUZ as NOMBRE_TELEGRAM
from app.engine.progression import LIGHT_ES

APP = pathlib.Path(__file__).resolve().parents[1] / "app"


def test_el_orden_es_de_menos_a_mas_freno():
    """No es alfabético ni casual: es el orden en que se lee un semáforo.

    De ahí sale el orden de las fichas del reparto en la pantalla, así que
    cambiarlo mueve la pantalla.
    """
    assert LUCES == ("green", "amber", "red")


def test_la_forma_canonica_va_en_minuscula():
    """Es la única que se puede meter dentro de una frase y derivar hacia arriba.

    `VERDE` sale de `verde` con `.upper()`; al revés no, porque de `ÁMBAR` no
    se saca dónde va la tilde sin confiar en el `lower()` de una vocal
    acentuada. Esa desconfianza es la que hizo que Telegram llevara su propia
    tabla escrita a mano durante meses.
    """
    assert NOMBRE_LUZ == {"green": "verde", "amber": "ámbar", "red": "rojo"}


@pytest.mark.parametrize(
    "nombre, tabla",
    [
        ("auditoría", NOMBRE_AUDITORIA),
        ("telegram", NOMBRE_TELEGRAM),
        ("emoji", EMOJI),
        ("plural de la portada", PLURAL_PORTADA),
        ("femenino de progresión", LIGHT_ES),
    ],
)
def test_todas_las_tablas_hablan_de_los_mismos_colores(nombre, tabla):
    """El día que haya un cuarto color, que falle aquí y no en una pantalla."""
    assert set(tabla) == set(LUCES), (
        f"la tabla de {nombre} conoce {sorted(tabla)} y el motor {sorted(LUCES)}"
    )


def test_telegram_se_deriva_y_no_se_escribe_otra_vez():
    """Si alguien vuelve a escribirla a mano, esto seguirá pasando... y por eso
    no basta con este test: el que de verdad lo impide es el del AST."""
    assert NOMBRE_TELEGRAM == {luz: NOMBRE_LUZ[luz].upper() for luz in LUCES}


def test_el_nombre_de_una_luz_desconocida_es_la_propia_clave():
    """Y NO un hueco.

    Si un día llega un color que no está en la tabla, lo que hace falta ver en
    la pantalla es QUÉ color llegó. Un «—» ahí obliga a abrir la base para
    averiguar qué se está mirando, y es la clase de degradación silenciosa que
    este proyecto convierte en error en todas partes menos aquí, donde el dato
    crudo dice más que la excepción.
    """
    assert nombre_luz("morado") == "morado"


def test_sin_luz_se_dice_con_palabras():
    """`None` es «sin luz», no una cadena vacía: un hueco no se distingue de un
    fallo de pintado."""
    assert nombre_luz(None) == "sin luz"
    assert nombre_luz("") == "sin luz"


def test_la_puerta_de_progresion_nombra_el_color_y_no_la_clave():
    """El motivo de la puerta NO se queda en un log: se guarda.

    `gate_reason` va al JSON de la decisión del día y la vista de auditoría lo
    pinta tal cual, así que «el semáforo no está en verde (amber)» era una
    clave del motor escrita en la pantalla del móvil. Y por estar guardada, el
    arreglo no borra el pasado: los días ya decididos siguen enseñando la frase
    vieja mientras estén dentro de la ventana.
    """
    from app.engine.progression import evaluate_gate

    for luz, palabra in (("amber", "ámbar"), ("red", "rojo")):
        abierta, motivo = evaluate_gate(
            prog_cfg={"gate": {"require_green": True}},
            light=luz,
            signals=None,
            compliance_ok=True,
            routine_key="dia_1",
        )
        assert abierta is False
        assert palabra in motivo, f"{luz}: {motivo!r}"
        assert luz not in motivo, f"la clave del motor sigue en la pantalla: {motivo!r}"


def _dicts_de_colores(ruta: pathlib.Path) -> list[int]:
    """Las líneas de `ruta` donde hay un diccionario indexado por los tres colores.

    Por AST y no por `grep`, porque dos de las seis copias no tenían nombre que
    buscar: una vivía dentro de un `return` y la otra se llamaba `LIGHT_ES`.
    Preguntar por la FORMA -tres claves, estas tres- las encuentra igual se
    llamen como se llamen y estén donde estén.
    """
    # `utf-8-sig` y no `utf-8`: hay ficheros del proyecto con BOM, y `ast.parse`
    # de una cadena que empieza por U+FEFF no es un fallo de sintaxis del
    # fichero pero lo parece. Se descubrió al escribir este test.
    arbol = ast.parse(ruta.read_text(encoding="utf-8-sig"))
    lineas = []
    for n in ast.walk(arbol):
        if not isinstance(n, ast.Dict) or not n.keys:
            continue
        if not all(
            isinstance(k, ast.Constant) and isinstance(k.value, str) for k in n.keys
        ):
            continue
        if {k.value for k in n.keys} == set(LUCES):  # type: ignore[union-attr]
            lineas.append(n.lineno)
    return lineas


# Cada una con el motivo por el que sigue existiendo. La lista es corta a
# propósito: añadir una entrada aquí obliga a escribir por qué esa copia no se
# puede derivar de la canónica, que es la pregunta que nadie se hizo las seis
# veces anteriores.
PERMITIDAS = {
    "engine/luces.py": "la canónica; es la que las demás derivan o comprueban",
    "engine/message.py": "EMOJI, que no son palabras y no se derivan de ninguna",
    "engine/progression.py": "LIGHT_ES, el femenino que concuerda con «sesión»",
    "analysis/portada.py": "el plural, que en castellano no sale de un sufijo",
}


@pytest.mark.parametrize(
    "relativa",
    sorted(
        p.relative_to(APP).as_posix()
        for p in APP.rglob("*.py")
        if p.name != "__init__.py"
    ),
)
def test_ninguna_tabla_nueva_de_colores_fuera_de_las_declaradas(relativa):
    """La séptima copia falla aquí, se llame como se llame.

    Y falla nombrando el fichero y la línea, que es lo que no hizo ninguna de
    las seis veces anteriores: la sexta se encontró leyendo la pantalla del
    móvil un domingo.
    """
    lineas = _dicts_de_colores(APP / relativa)
    if not lineas:
        return
    assert relativa in PERMITIDAS, (
        f"{relativa} línea(s) {lineas}: hay una tabla indexada por los tres "
        f"colores del semáforo que no está declarada. Si es una palabra que no "
        f"se puede derivar de `app.engine.luces.NOMBRE_LUZ` -un plural, un "
        f"femenino, un emoji- decláralo en PERMITIDAS con el motivo. Si es la "
        f"misma palabra otra vez, impórtala: ya ha pasado seis veces."
    )
