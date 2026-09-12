"""La documentación, tratada como código que puede estar roto.

`docs/analisis.md` se escribió como especificación de una sección pendiente, se
implementó la sección, y el documento se quedó diciendo "no implementada
todavía" -con cinco vistas en producción- y mandando los endpoints a
`/api/analysis/...`, que no existe ni ha existido nunca. Nadie se enteró porque
un documento no tiene forma de fallar: se lee igual de bien siendo falso.

Es exactamente el fallo que este proyecto lleva meses cazando en el código. El
validador que certificaba una sección muerta. La fila de `workout_log` que
decía "Sí" cuando no se escribía ni una de sus cuatro columnas. El `.env.example`
que era la única lista de ajustes y le faltaban cuatro. En los tres casos la
pieza que tenía que avisar del hueco era justo la que decía que no lo había.

Así que el remedio es el mismo que ya funciona para `.env.example` en
`test_settings.py`: atar el documento a la cosa que describe, de forma que
cambiar una sin la otra rompa la suite. No prueba que el texto sea BUENO -eso no
lo puede probar nada-, pero sí que no nombre rutas inventadas ni se deje ninguna
fuera, que es como se murió la versión anterior.
"""

from __future__ import annotations

import re

from app.api import app
from app.settings import REPO_ROOT

DOC = REPO_ROOT / "docs" / "analisis.md"

PREFIJO = "/api/metrics/"


def _rutas_del_codigo() -> set[str]:
    """Las rutas que FastAPI sirve de verdad, preguntándoselo a FastAPI.

    Leídas del router y no del fichero con una expresión regular: un `grep` de
    `@app.get` daría por buena una ruta dentro de un comentario o de un bloque
    de código muerto, que es justo el tipo de cosa que este test busca.
    """
    rutas = set()
    for r in app.routes:
        camino = getattr(r, "path", "")
        if camino.startswith(PREFIJO):
            rutas.add(camino)
    return rutas


def _rutas_del_documento() -> set[str]:
    """Toda ruta `/api/...` que el documento nombre, sea del prefijo que sea.

    Mirar solo `/api/metrics/` sería inútil precisamente contra el fallo que
    hubo: el documento viejo no nombraba ni una ruta de métricas -las mandaba
    todas a `/api/analysis/...`- así que un test limitado a ese prefijo lo
    habría dado por bueno. Se comprobó, y en efecto pasaba.
    """
    texto = DOC.read_text(encoding="utf-8")
    return set(re.findall(r"/api/[a-z0-9][a-z0-9/_-]*", texto))


def test_el_documento_no_nombra_endpoints_que_no_existen():
    """El fallo concreto que tuvo: `/api/analysis/...` nunca existió.

    Quien leyera el documento y fuera a probarlo se encontraba un 404 y tenía
    dos explicaciones igual de plausibles: que el documento mintiera o que su
    despliegue estuviera roto. Ninguna de las dos es una buena mañana.

    Un prefijo suelto -"todo bajo `/api/metrics/`"- no es una ruta inventada, y
    se acepta si es principio de alguna que exista. Nombrar el barrio no es
    nombrar una casa que no está.
    """
    reales = {r.path for r in app.routes if getattr(r, "path", "").startswith("/api/")}
    sobran = {
        ruta for ruta in _rutas_del_documento()
        if ruta not in reales
        and not any(r.startswith(ruta.rstrip("/") + "/") for r in reales)
    }
    assert not sobran, f"docs/analisis.md nombra rutas inexistentes: {sorted(sobran)}"


def test_el_documento_nombra_todas_las_vistas_que_existen():
    """Al revés: una vista sin documentar es una vista que nadie sabe que está.

    Es el mismo argumento que con `DATABASE_URL` en `.env.example`: lo que
    existe y no está escrito no es una función, es un secreto.
    """
    faltan = _rutas_del_codigo() - _rutas_del_documento()
    assert not faltan, f"docs/analisis.md no menciona: {sorted(faltan)}"


def test_el_documento_declara_su_estado_y_es_el_de_verdad():
    """El fallo no fue un matiz de redacción: el título decía "(pendiente)" y el
    cuerpo abría con "**No implementada todavía.**" mientras la sección llevaba
    meses sirviendo datos.

    Se comprueba en POSITIVO -que el estado esté declarado y sea "implementada"-
    y no buscando la frase vieja, por dos motivos. El práctico: este mismo
    documento CITA la frase vieja al explicar cómo se rompió, así que una lista
    negra se cazaría a sí misma. El de fondo: prohibir una redacción concreta
    solo obliga a escribir la mentira con otras palabras. Exigir que la verdad
    esté dicha no tiene esa salida.
    """
    texto = DOC.read_text(encoding="utf-8")
    titulo = texto.splitlines()[0]
    assert "pendiente" not in titulo.lower(), (
        f"el título sigue marcando la sección como pendiente: {titulo!r}"
    )
    estado = texto.split("## Estado", 1)
    assert len(estado) == 2, "el documento tiene que declarar su estado"
    assert estado[1].lstrip().lower().startswith("implementada"), (
        "cinco vistas y seis endpoints sirviendo: el estado es implementada"
    )
