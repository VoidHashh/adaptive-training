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

import inspect
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
    # El recuento se le pregunta al router y no se escribe aquí. Decía "cinco
    # vistas y seis endpoints" y para cuando se leyó ya eran nueve: un mensaje de
    # fallo que envejece es la misma avería que este fichero entero persigue, solo
    # que escondida en el único sitio que nadie mira mientras la suite está verde.
    assert estado[1].lstrip().lower().startswith("implementada"), (
        f"{len(_rutas_del_codigo())} endpoints sirviendo bajo {PREFIJO}: "
        f"el estado es implementada"
    )


# ---------------------------------------------------------------------------
# Las líneas de trabajo abiertas
# ---------------------------------------------------------------------------
#
# Una línea de trabajo es una promesa a futuro escrita sobre el código de hoy, y
# eso la hace MÁS frágil que el resto del documento, no menos: describe algo que
# por definición no existe todavía, así que nada la ejecuta, y se apoya en
# nombres de columnas y de funciones que sí existen y que se pueden renombrar
# mañana. El día que pase, la nota no queda incompleta: queda falsa, y se lee
# con la misma confianza que el resto. Es exactamente cómo murió la versión
# anterior de este documento entero.


def _linea_de_trabajo() -> str:
    texto = DOC.read_text(encoding="utf-8")
    partes = texto.split("## Líneas de trabajo abiertas", 1)
    assert len(partes) == 2, (
        "el documento ya no tiene sección de líneas de trabajo. Si se ha "
        "cerrado la que había, este test sobra; si se ha renombrado, lo que "
        "sobra es el título nuevo"
    )
    return partes[1].split("\n## ", 1)[0]


def test_la_linea_de_la_carga_de_fuerza_nombra_cosas_que_existen():
    """Las columnas y funciones en las que se apoya, comprobadas de verdad.

    La línea dice que el tonelaje "está hecho, no hay que construirlo" y lo
    justifica nombrando cuatro piezas concretas. Si alguna se renombra, lo que
    queda escrito es que el trabajo ya está resuelto en un sitio que no existe,
    y la próxima persona que abra la línea empieza buscando un fantasma.

    Se comprueban contra el modelo y el módulo, no contra un `grep` del código:
    una columna citada dentro de un comentario o de una migración vieja pasaría
    un `grep` y no serviría para nada.
    """
    from app.integrations import hevy
    from app.models import WorkoutLog

    seccion = _linea_de_trabajo()
    columnas = set(WorkoutLog.__table__.columns.keys())

    for col in ("total_sets", "total_volume_kg", "raw_json"):
        if f"`{col}`" in seccion or f"`workout_log.{col}`" in seccion:
            assert col in columnas, (
                f"la línea de trabajo nombra `workout_log.{col}` y esa columna "
                f"ya no está. Hay: {sorted(columnas)}"
            )

    for fn in ("workout_totals", "get_workouts"):
        assert f"`{fn}`" in seccion, (
            f"la línea de trabajo ya no nombra `{fn}`; si la pieza ha cambiado, "
            f"la nota hay que reescribirla, no dejarla a medias"
        )
        assert hasattr(hevy, fn) or hasattr(hevy.HevyClient, fn), (
            f"la línea de trabajo se apoya en `{fn}`, que no está en "
            f"`integrations/hevy.py`"
        )


def test_la_ventana_de_reconciliacion_que_cita_es_la_de_verdad():
    """Los dos números de la ventana, contados como lo que son hoy.

    La línea ya no dice que la reconciliación mire tres días: dice que tres es
    el SUELO y cuarenta y cinco el techo, y sobre esa distinción se apoya su
    diagnóstico de por qué faltaba el histórico. Si alguno de los dos cambia en
    `job_reconcile` sin tocar el documento, lo que queda escrito no envejece
    hacia incompleto sino hacia falso: un lector sacaría la conclusión contraria
    -que un hueco largo se recupera solo, o que no se recupera nunca- con la
    misma confianza.
    """
    from app.scheduler import job_reconcile

    seccion = _linea_de_trabajo()
    firma = inspect.signature(job_reconcile).parameters

    for nombre in ("dias_atras", "tope_dias"):
        citado = re.search(rf"`{nombre}=(\d+)`", seccion)
        assert citado is not None, (
            f"la línea de trabajo ya no cita `{nombre}`, que es parte de donde "
            f"sale su diagnóstico sobre el histórico que faltaba"
        )
        assert int(citado.group(1)) == firma[nombre].default, (
            f"la línea dice {nombre}={citado.group(1)} y el trabajo nocturno "
            f"usa {firma[nombre].default}"
        )
