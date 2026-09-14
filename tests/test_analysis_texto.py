"""Que el panel no vuelva a escribir "día(s)".

El test de abajo no es el de una función: es el de una regla de todo el
paquete. Los otros dos sitios donde ya se había concordado el plural a mano
-`signals.py` para el mensaje de Telegram, `portada.py` para el recuento de
fuerza- lo arreglaron uno a uno, y entre los dos quedaron once frases en
`auditoria.py`, tres en `stats.py`, dos en `impacto.py` y una en
`rendimiento.py` escribiendo paréntesis en la pantalla que el usuario mira
todas las mañanas. Arreglarlas también una a una garantiza que la duodécima
aparezca la semana que viene.

Así que aquí se mira el paquete entero, no las frases conocidas. Si alguien
escribe un `na` nuevo con "sesión(es)" dentro, esto pincha y nombra el fichero
y la línea.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from app.analysis.texto import cuantos, plural

# La marca habitual de un plural sin resolver: el sufijo entre paréntesis.
PARENTESIS = re.compile(r"\(s\)|\(es\)|\(a\)|\(as\)")

# La otra variante, la de "vez/veces": aparece cuando el plural no se puede
# hacer pegando un sufijo y hay que escribir las dos palabras enteras. Delata
# exactamente lo mismo, pero NO se puede buscar con `\w+/\w+`, porque esa
# forma es también la de cualquier ruta -`app/analysis/series.py` cayó en la
# primera pasada-. Lo que distingue un par singular/plural de una ruta es que
# las dos mitades son la misma palabra: comparten el arranque y la segunda es
# más larga.
BARRA = re.compile(r"\b([A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{2,})/([A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{2,})\b")

PAQUETE = pathlib.Path(__file__).resolve().parents[1] / "app" / "analysis"


def _plantillas(s: str) -> list[str]:
    """Los trozos de `s` que son un plural sin concordar, o lista vacía."""
    hallado = [m.group(0) for m in PARENTESIS.finditer(s)]
    for m in BARRA.finditer(s):
        uno, varios = m.group(1), m.group(2)
        if len(varios) > len(uno) and varios[:2].lower() == uno[:2].lower():
            hallado.append(m.group(0))
    return hallado


def test_concuerda_con_uno_con_cero_y_con_muchos():
    assert cuantos(1, "día", "días") == "1 día"
    assert cuantos(0, "día", "días") == "0 días"
    assert cuantos(2, "día", "días") == "2 días"
    assert cuantos(43, "día", "días") == "43 días"


def test_el_signo_no_cambia_la_concordancia():
    """Los desfases van de -3 a +3, y "se ADELANTA -1 días" es el mismo fallo.

    El signo lo pone la frase que rodea al número -"se adelanta", "va por
    detrás"-; la palabra solo mira la magnitud.
    """
    assert cuantos(-1, "día", "días") == "-1 día"
    assert cuantos(-3, "día", "días") == "-3 días"


def test_las_dos_formas_se_pasan_enteras_porque_el_plural_mueve_la_tilde():
    """`"sesión" + "es"` da `"sesiónes"`, y a ojo se lee cuatro veces sin verlo."""
    assert cuantos(1, "sesión", "sesiones") == "1 sesión"
    assert cuantos(2, "sesión", "sesiones") == "2 sesiones"
    assert cuantos(2, "vez", "veces") == "2 veces"


def test_plural_devuelve_la_palabra_sola():
    """Para concordar dos palabras con un solo recuento: "4 días prescritos"."""
    assert plural(1, "prescrito", "prescritos") == "prescrito"
    assert plural(4, "prescrito", "prescritos") == "prescritos"


def _literales_de_verdad(ruta: pathlib.Path) -> list[tuple[int, str]]:
    """Las cadenas que se pueden imprimir, sin los docstrings ni los comentarios.

    La distinción importa: un docstring PUEDE hablar de "día(s)" -este fichero
    lo hace en cada párrafo- porque está explicando justamente eso. Lo que no
    puede es salir por la pantalla. `ast` ya separa las dos cosas sin tener que
    adivinar con una expresión regular dónde acaba una comilla triple.
    """
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    docs: set[int] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            cuerpo = getattr(nodo, "body", None)
            if (
                cuerpo
                and isinstance(cuerpo[0], ast.Expr)
                and isinstance(cuerpo[0].value, ast.Constant)
                and isinstance(cuerpo[0].value.value, str)
            ):
                docs.add(id(cuerpo[0].value))
    return [
        (n.lineno, n.value)
        for n in ast.walk(arbol)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and id(n) not in docs
    ]


@pytest.mark.parametrize(
    "fichero", sorted(p.name for p in PAQUETE.glob("*.py") if p.name != "__pycache__")
)
def test_ninguna_frase_del_panel_lleva_un_plural_de_plantilla(fichero):
    """El paréntesis de plural es la firma de un texto generado.

    Y un texto que parece generado deja de leerse: el ojo lo salta igual que
    salta un identificador. Un dato que se salta con la vista es exactamente
    igual de útil que uno que no se calcula, así que esto no es cosmética.
    """
    ruta = PAQUETE / fichero
    culpables = [
        (n, s, _plantillas(s)) for n, s in _literales_de_verdad(ruta) if _plantillas(s)
    ]
    assert not culpables, (
        f"{fichero} escribe plurales de plantilla en cadenas que pueden acabar "
        f"en la pantalla; se concuerdan con `app.analysis.texto.cuantos`: "
        + "; ".join(f"línea {n}: {p} en {s!r}" for n, s, p in culpables)
    )
