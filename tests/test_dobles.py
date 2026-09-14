"""La comprobación que impide que un doble se separe de su original.

Lo que hace que esto valga algo no es la comparación por introspección, que es
la parte fácil: es que NO SE PUEDE ESQUIVAR. Una clase nueva en `tests/` sin
declarar qué es pone rojo el primer test de este módulo, y una clase declarada
como doble queda comparada con su original para siempre.

La alternativa -acordarse de mirar si el doble sigue pareciéndose- ya se probó,
y produjo cinco defectos de la misma forma en tres días. Ver `tests/dobles.py`.
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import pkgutil

import pytest

from tests.dobles import (
    MARCA,
    declaracion_de,
    divergencias,
    estado_mutable_de_clase,
    no_es_doble,
)

RAIZ = pathlib.Path(__file__).resolve().parent

# Las clases que pytest recoge como agrupadores. No representan nada de `app/`
# y obligarlas a declararlo sería ruido: su nombre ya lo dice, y es pytest quien
# fija esa convención, no nosotros.
def _es_agrupador_de_pytest(nombre: str) -> bool:
    return nombre.startswith("Test")


# `dobles.py` es la maquinaria de la comprobación, no un test. Sus clases no
# representan nada de `app/` y no puede declararlo con sus propios decoradores
# sin morderse la cola.
_NO_SON_TESTS = {"__init__.py", "dobles.py"}


def _ficheros() -> list[pathlib.Path]:
    return sorted(p for p in RAIZ.glob("*.py") if p.name not in _NO_SON_TESTS)


def _clases_declaradas(arbol: ast.AST) -> list[tuple[ast.ClassDef, bool]]:
    """Todas las `class` del fichero, con si llevan decorador de declaración.

    Se lee el árbol y no el módulo importado porque una clase definida DENTRO
    de una función o de un fixture no existe hasta que esa función corre, y
    `test_cli.py` tenía justo eso: un doble escondido en un fixture. Leer el
    árbol las ve todas.
    """
    fuera = []
    for n in ast.walk(arbol):
        if not isinstance(n, ast.ClassDef):
            continue
        marcada = False
        for d in n.decorator_list:
            f = d.func if isinstance(d, ast.Call) else d
            nombre = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if nombre in {"doble_de", "no_es_doble"}:
                marcada = True
        fuera.append((n, marcada))
    return fuera


def test_toda_clase_de_los_tests_declara_lo_que_es():
    """La capa que hace que la de abajo no se pueda esquivar.

    Sin esto, la comparación solo miraría los dobles que alguien se acordó de
    registrar, que es la misma disciplina que ya falló. Con esto, escribir un
    doble nuevo y no declararlo es un test rojo.
    """
    sin_declarar: list[str] = []
    for p in _ficheros():
        arbol = ast.parse(p.read_text(encoding="utf-8-sig"))
        for nodo, marcada in _clases_declaradas(arbol):
            if marcada or _es_agrupador_de_pytest(nodo.name):
                continue
            sin_declarar.append(f"{p.name}:{nodo.lineno} {nodo.name}")

    assert not sin_declarar, (
        "estas clases de los tests no dicen qué son:\n  "
        + "\n  ".join(sin_declarar)
        + "\n\nPonles `@doble_de(LaClaseDeApp)` si hacen de algo de `app/`, o "
        "`@no_es_doble(\"por qué no\")` si son un ayudante. Las dos salen de "
        "`tests/dobles.py`. Esto existe porque un doble que se separa de su "
        "original deja el test en verde certificando una conducta imposible, y "
        "eso pasó cinco veces en tres días."
    )


def _modulos():
    for m in pkgutil.iter_modules([str(RAIZ)]):
        if m.name == "__init__":
            continue
        yield importlib.import_module(f"tests.{m.name}")


def _clases_importadas():
    """Clases declaradas que existen a nivel de módulo, ya importadas.

    Las anidadas dentro de una función no aparecen aquí y no pueden: no existen
    hasta que la función corre. A ésas las cubre `doble_de`, que compara en el
    momento de decorar, y la capa del árbol sintáctico, que sí las ve y obliga a
    que lleven la declaración puesta.

    Se devuelve cada clase UNA vez aunque la importen cinco módulos. `HevyFalso`
    y `TelegramFalso` viven en `test_runner.py` y los reexportan cuatro ficheros
    más; sin esto, un fallo suyo salía repetido cinco veces y el listado parecía
    cinco problemas distintos.
    """
    vistas: set[int] = set()
    for mod in _modulos():
        for nombre, obj in vars(mod).items():
            if not isinstance(obj, type) or declaracion_de(obj) is None:
                continue
            if id(obj) in vistas:
                continue
            vistas.add(id(obj))
            yield obj.__module__, nombre, obj


def test_ningun_doble_se_ha_separado_de_su_original():
    problemas: list[str] = []
    mirados = 0
    for modulo, nombre, cls in _clases_importadas():
        dec = declaracion_de(cls)
        if dec.original is None:
            continue
        mirados += 1
        for d in divergencias(cls, dec.original):
            problemas.append(f"{modulo}.{nombre}: {d}")

    assert mirados, "no se ha comparado ni un solo doble; la recolección falla"
    assert not problemas, "dobles que ya no se parecen a su original:\n  " + "\n  ".join(
        problemas
    )


def test_ninguna_clase_de_los_tests_comparte_estado_entre_instancias():
    """Un `list` de clase es una lista para todas las instancias.

    En un doble que acumula llamadas eso significa que el segundo test ve lo
    que hizo el primero. `test_scheduler.py` tenía un `fetch_errors: list[str] =
    []` de clase cuyo original lo declara con `field(default_factory=list)`.
    """
    problemas = [
        f"{modulo}.{nombre}: {d}"
        for modulo, nombre, cls in _clases_importadas()
        for d in estado_mutable_de_clase(cls)
    ]
    assert not problemas, "estado mutable compartido entre instancias:\n  " + "\n  ".join(
        problemas
    )


def test_la_marca_no_se_hereda_y_una_hija_tambien_tiene_que_declararse():
    """`declaracion_de` mira `__dict__` y no `getattr`, y esto lo demuestra.

    Con `getattr`, una clase que heredara de un doble declarado saldría por
    declarada sin haber dicho nada, y se compararía contra el original de su
    madre en vez de contra el suyo. Es la clase de degradación silenciosa que
    este módulo existe para no tener.
    """

    # Las dos se fabrican con `type()` y no con `class`, y no es por gusto: la
    # hija de este test tiene que NO llevar declaración, que es justo lo que la
    # otra capa -la del árbol sintáctico- prohíbe escribir. Con `class` habría
    # que ponerle el decorador para que la capa de arriba no la cazara, y con el
    # decorador puesto ya no se podría comprobar que no hereda nada. `type()` no
    # deja rastro en el árbol, así que las dos capas conviven sin estorbarse.
    Madre = type("Madre", (), {})
    setattr(Madre, MARCA, object())
    Hija = type("Hija", (Madre,), {})

    assert declaracion_de(Madre) is not None
    assert getattr(Hija, MARCA, None) is not None, (
        "la cobaya no hereda el atributo; el test no estaría probando nada"
    )
    assert declaracion_de(Hija) is None, (
        "la marca se está heredando: una subclase pasaría por declarada"
    )


def test_un_motivo_vacio_no_cuela_como_declaracion():
    """`no_es_doble("")` sería declarar sin decir nada."""
    for malo in ("", "   ", None):
        with pytest.raises(ValueError):
            no_es_doble(malo)
