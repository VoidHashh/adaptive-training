"""La maqueta aprobada del panel sigue viva, versionada y cumpliendo sus reglas.

POR QUÉ EXISTE ESTE ARCHIVO
---------------------------
No para probar la maqueta: no se sirve a nadie, no la usa el contenedor y si se
rompiera no se caería nada. Existe porque la maqueta se PERDIÓ, y perderla costó
dos días de trabajo en dirección contraria.

Se escribió el 2026-09-15 contestando a cinco reglas muy concretas, se enseñó, y
se quedó en `out/`, que está en `.gitignore`. Semanas después el encargo fue
«lleva la pantalla del umbral al código real y aplica el mismo criterio al resto
de vistas», y «el mismo criterio» no aparecía en `git log`, ni en el árbol
versionado, ni en ningún commit: solo en un directorio que el repositorio no
mira. Se entendió por otro criterio distinto.

Así que lo que se prueba aquí no es que la maqueta funcione. Es que ESTÉ, que el
repositorio la vea, y que siga diciendo lo que se aprobó. Las tres cosas que
fallaron.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
MAQUETA = RAIZ / "docs" / "maquetas" / "panel_umbral.py"
PAYLOAD = RAIZ / "docs" / "maquetas" / "umbral-payload.json"

# Regla 2, literal: «Nada de p corregida, correlación, percentiles ni mitad
# central en la vista principal. Todo eso detrás de "ver detalle"».
#
# La lista es la del encargo y no una interpretación: cada entrada es una de las
# cuatro cosas que se nombraron, más las formas en que se escriben de verdad en
# este proyecto. `\bp\s*=` caza «p = 0,03», que es p a secas aunque no diga
# «corregida», y es exactamente lo que se pidió no ver al abrir la pantalla.
PROHIBIDO_EN_LA_PRINCIPAL = [
    ("p corregida", re.compile(r"p\s+corregida", re.I)),
    ("percentil", re.compile(r"percentil", re.I)),
    ("mitad central", re.compile(r"mitad\s+central", re.I)),
    ("correlación", re.compile(r"correlaci[oó]n", re.I)),
    ("de rangos / Spearman", re.compile(r"spearman|de\s+rangos", re.I)),
    ("p = …", re.compile(r"\bp\s*=\s*[\d0-9]", re.I)),
    ("intervalo de confianza", re.compile(r"IC\s*95|intervalo\s+de\s+confianza", re.I)),
]


def sin_detalles(html: str) -> str:
    """El HTML que se ve al abrir: todo menos lo que hay dentro de `<details>`.

    Se recorta por `<details>` entero y no solo por su `<summary>`: lo que la
    regla permite esconder es el bloque plegado, y comprobar contra el HTML
    completo daría por incumplida una pantalla que cumple, que es la forma de
    que una guarda acabe desactivada por molesta.
    """
    return re.sub(r"<details.*?</details>", " ", html, flags=re.S | re.I)


def texto_visible(html: str) -> str:
    sin_svg = re.sub(r"<svg.*?</svg>", " ", html, flags=re.S | re.I)
    sin_estilo = re.sub(r"<style.*?</style>", " ", sin_svg, flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", sin_estilo))


@pytest.fixture(scope="module")
def html() -> str:
    """La maqueta, ejecutada de verdad.

    Se ejecuta en vez de leer un HTML guardado porque lo que se quiere saber es
    si SIGUE corriendo. Un fichero guardado seguiría pasando el test años
    después de que el script dejara de funcionar.
    """
    r = subprocess.run(
        [sys.executable, "-X", "utf8", str(MAQUETA)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=RAIZ,
    )
    assert r.returncode == 0, (
        f"la maqueta aprobada ya no se ejecuta:\n{r.stdout}\n{r.stderr}"
    )
    salida = RAIZ / "out" / "panel" / "umbral.html"
    assert salida.exists(), "la maqueta corrió y no escribió el HTML"
    return salida.read_text(encoding="utf-8")


def test_la_maqueta_esta_versionada():
    """Que el repositorio la VEA. Es el fallo original, y el único que importó.

    La maqueta funcionaba perfectamente en `out/`. Lo que no hacía era existir
    para quien viniera a buscarla: ni `git log`, ni `git grep`, ni un `git show`
    de ningún commit la encontraban. Una decisión de diseño aprobada que solo
    vive en un directorio ignorado se va a perder, y al perderse no deja hueco:
    deja sitio para que alguien invente otro criterio y crea que es el que había.
    """
    if not shutil.which("git"):
        pytest.skip("sin git no se puede comprobar")

    for f in (MAQUETA, PAYLOAD):
        rel = f.relative_to(RAIZ).as_posix()
        visto = subprocess.run(
            ["git", "ls-files", "--error-unmatch", rel],
            capture_output=True,
            text=True,
            cwd=RAIZ,
        )
        assert visto.returncode == 0, (
            f"`{rel}` no está en el índice de git. Si está en `out/` o en otro "
            f"directorio ignorado, vuelve a pasar lo de 2026-09-15: la maqueta "
            f"aprobada existe, funciona, y nadie que venga a buscar «el criterio "
            f"que aprobamos» la va a encontrar."
        )


def test_la_maqueta_sigue_teniendo_los_cuatro_graficos(html):
    """Cuatro dibujos y cuatro plegables: el reparto que se aprobó.

    Los títulos van comprobados uno a uno y no por su número. «Cuatro SVG»
    seguiría pasando si alguien cambiara el quesito por otra barra, y el quesito
    estaba pedido por su nombre en el encargo -«barras, líneas, quesitos»-.
    """
    assert html.count("<svg") == 4, f"la maqueta pintaba 4 gráficos y ahora {html.count('<svg')}"
    assert html.count("<details") == 4, "cada gráfico llevaba su «ver detalle»"

    titulos = [re.sub(r"\s+", " ", t).strip() for t in re.findall(r"<h2>(.*?)</h2>", html, re.S)]
    assert titulos == [
        "Cuánto te baja la HRV según la salida",
        "Cuánto tarda en volver",
        "Tu HRV desde marzo",
        "Cómo son tus salidas",
    ], f"los cuatro bloques aprobados han cambiado: {titulos}"

    # El quesito, por su forma. Un anillo son `path` con `stroke-width` gordo y
    # sin relleno; buscarlo por la palabra «quesito» no probaría que se dibuja.
    assert re.search(r'<path d="M[^"]+A\d', html), "el quesito ya no dibuja su anillo"


def test_cada_grafico_lleva_UNA_frase_y_no_un_parrafo(html):
    """Regla 1 y regla 4, que son la misma medida por los dos lados.

    «El texto, debajo y en una frase» y «si hace falta un párrafo para explicar
    el gráfico, el gráfico está mal». Se cuentan los puntos: una frase tiene uno
    al final. Dos puntos seguidos ya son un párrafo camuflado.

    El tope está en 240 caracteres y no en «una oración» a rajatabla porque la
    frase aprobada más larga -«Solo las salidas de más de 176 de carga se notan:
    la HRV baja 7,0 ms a la mañana siguiente.»- lleva dos puntos y medio y es
    exactamente lo que se quería. Lo que se persigue es el párrafo, no la coma.
    """
    frases = re.findall(r'<p class="frase">(.*?)</p>', html, re.S)
    assert len(frases) == 4, f"cada bloque llevaba su frase; hay {len(frases)}"
    for f in frases:
        limpia = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", f)).strip()
        assert len(limpia) <= 240, f"esto ya es un párrafo, no una frase: {limpia!r}"
        assert limpia.count(".") <= 2, f"tres frases donde se pidió una: {limpia!r}"


def test_lo_estadistico_esta_detras_de_ver_detalle(html):
    """Regla 2, sobre la propia maqueta. Es la referencia, tiene que cumplirla.

    Se mira el texto que queda AL QUITAR los `<details>`, que es literalmente lo
    que se ve al abrir la pantalla en el móvil sin tocar nada.

    Y se comprueba además que los términos SIGUEN EXISTIENDO dentro de los
    plegables. La regla era esconderlos, no tirarlos: «Si lo quieres conservar,
    que esté escondido detrás de un "ver detalle"». Una maqueta que los hubiera
    borrado pasaría la primera mitad de este test y habría perdido el dato.
    """
    principal = texto_visible(sin_detalles(html))
    encontrados = [n for n, r in PROHIBIDO_EN_LA_PRINCIPAL if r.search(principal)]
    assert not encontrados, (
        f"la vista principal de la maqueta enseña {encontrados}, que es justo lo "
        f"que la regla 2 manda esconder detrás de «ver detalle»."
    )

    completo = texto_visible(html)
    assert any(r.search(completo) for _, r in PROHIBIDO_EN_LA_PRINCIPAL), (
        "no queda ni un número estadístico en toda la maqueta. La regla era "
        "ESCONDERLOS detrás de «ver detalle», no borrarlos: el detalle es lo "
        "que se abre cuando la frase de arriba no basta."
    )


def test_los_numeros_se_leen_en_espanol(html):
    """Regla 3. Coma decimal y el menos de verdad, no el guion del teclado.

    Se mira solo el texto: dentro del SVG hay coordenadas con punto decimal, que
    son píxeles y no los lee nadie.
    """
    texto = texto_visible(html)
    assert not re.search(r"\d\.\d", texto), (
        "hay un número con punto decimal en el texto: 48.4 en vez de 48,4"
    )
    assert "−" in html, "el menos tipográfico (−) se ve a las 6 de la mañana y el guion no"


def test_es_legible_en_vertical_sin_zoom(html):
    """Regla 5. El `viewBox` manda y ningún gráfico se sale del ancho.

    340 es el ancho de trabajo de la maqueta y cabe en el móvil más estrecho que
    se usa hoy. Lo que se comprueba no es ese número sino que TODOS midan igual:
    un gráfico más ancho que los demás es el que obliga a deslizar en horizontal.
    """
    assert 'name="viewport"' in html and "width=device-width" in html
    anchos = {int(w) for w in re.findall(r'viewBox="0 0 (\d+) \d+"', html)}
    assert anchos, "ningún SVG declara `viewBox`, así que no escala"
    assert max(anchos) <= 360, f"hay un gráfico de {max(anchos)} px: obliga a deslizar"
