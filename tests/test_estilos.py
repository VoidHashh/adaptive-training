"""La hoja de estilos, atada a lo que la pantalla dice que es.

EL FALLO QUE LO MOTIVA
----------------------
`index.html` traía `<button type="button" id="previsualizar">Previsualizar</button>`
desde el commit `67962aa`, `app.js` lo enganchaba en la línea 1590, y dos tests
lo comprobaban: uno lee el HTML y exige `type="button"` y el orden respecto al
de enviar, y el otro PULSA el botón en el arnés y mira la petición que sale.
Los dos pasaban. El botón estaba en la imagen desplegada. Y desde el móvil no
existía.

Medido en el navegador contra el contenedor: 92 × 20 píxeles, fondo
`rgb(240,240,240)`, texto negro, letra de 13,3. Un `<button>` con la apariencia
que le pone el navegador cuando NADIE le ha escrito una regla, en una pantalla
donde todo lo demás es oscuro, ancho y de 17 píxeles. El de al lado, `#enviar`,
mide 544 × 57 y es azul. No estaba oculto: era invisible de otra manera, que es
peor, porque `hidden`, `display:none` y una clase que esconde se encuentran
todos mirando el DOM, y esto no se encuentra mirando nada.

Se buscó en el sitio equivocado -caché del navegador, service worker, la imagen
de Docker, el proxy- porque todas las comprobaciones que existían decían que el
botón estaba. Y estaba.

POR QUÉ NO LO COGÍA NINGÚN TEST
-------------------------------
Porque `static/styles.css` no lo abría ninguno. Treinta y siete kilobytes que
están en `ARMAZON`, que se versionan, a los que se les calcula la huella, que
obligan a subir `VERSION` cuando cambian... y cuyo contenido no leía una sola
línea de la batería. Era el único fichero del armazón con cobertura cero: la
suite sabía decir «el elemento existe, tiene el tipo correcto, está en el orden
correcto y su manejador se dispara», y no tenía forma de decir «y se parece a
un botón».

QUÉ COMPRUEBA ESTO, Y QUÉ NO PUEDE COMPROBAR
--------------------------------------------
No hay `jsdom` ni motor de CSS en este repositorio, y no los va a haber: la
batería corre con `node` a secas. Así que esto NO sabe si una regla calcula lo
que debería. Lo que sabe es si la regla EXISTE, y resulta que ese es justo el
modo de fallo que hubo: no una regla mal escrita, sino ninguna.

Lo que se le escapa, dicho para que nadie lo lea como más de lo que es:

  - Una regla que exista y esté mal. `padding: 1rem` donde tocaba `1px` pasa.
  - Una regla que case y no sirva sin otra. Es el caso de la tarjeta de
    previsualizar: llevaba `class="previsualizacion semaforo-green"`, y
    `.semaforo-green { border-color: var(--verde) }` SÍ la alcanzaba. Poner un
    color de borde a un elemento con el borde a cero no pinta nada, y su punto
    del semáforo -`<span class="punto">` vacío- se quedaba en `width: auto`,
    o sea de tamaño cero. La clase se aplicaba, la regla casaba, y el semáforo
    era literalmente invisible. Lo caza la comprobación POR CLASE de aquí
    abajo, que exige una regla que nombre `.previsualizacion`; no lo cazaría
    una que se conformara con «algo la alcanza».
  - El CSS que sobra. Una regla para una clase que ya no escribe nadie no
    rompe nada, y este fichero no la busca.
"""

from __future__ import annotations

import re

from app.settings import REPO_ROOT

ESTATICOS = REPO_ROOT / "static"

# LAS DOS LISTAS SALEN DEL DISCO, Y ESTABAN ESCRITAS A MANO (25/09/2026).
#
# Eran `["index.html", "metricas.html"]` y los cuatro scripts de entonces. El día
# que nacieron dos páginas nuevas -la de después de entrenar y la de avanzado-
# este fichero las certificó en verde SIN HABERLAS ABIERTO: una pantalla entera
# con decenas de clases sin una sola regla, y la guarda escrita precisamente
# para eso diciendo que no faltaba nada.
#
# Es el defecto que abre la cabecera de este fichero -el botón que nació sin
# CSS y nadie se enteró en dos semanas-, una planta más arriba: aquel se escapó
# por una clase; esta guarda iba a dejar escapar páginas enteras. Una lista de
# lo que hay que vigilar, escrita a mano, envejece en silencio igual que un
# comentario.
#
# `sw.js` se excluye porque no pinta: no tiene DOM, corre en otro hilo.
HTML = sorted(p.name for p in ESTATICOS.glob("*.html"))
JS = sorted(p.name for p in ESTATICOS.glob("*.js") if p.name != "sw.js")

# Un nombre de CSS válido. Sirve para tirar la basura que deja leer JavaScript
# con expresiones regulares: `?`, `===`, `||` y demás trozos de expresión que
# aparecen dentro de un `class="${...}"`.
NOMBRE = re.compile(r"^-?[A-Za-z_][\w-]*$")

# Lo interpolado se sustituye por esto antes de partir por espacios, para poder
# distinguir tres cosas que si no se confunden: un nombre entero (`aviso`), un
# nombre que se COMPONE en tiempo de ejecución (`semaforo-${d.light}`) y un
# hueco que es entero una expresión (`${a.clase}`).
HUECO = "\x00"

SALTO = "\n"


# ---------------------------------------------------------------------------
# Lo que la pantalla escribe
# ---------------------------------------------------------------------------


def _sin_interpolar(texto: str) -> str:
    """Cambia cada `${...}` por un hueco, contando llaves anidadas.

    Con una expresión regular no vale: `${o.key === sel.propuesta ? "a" : ""}`
    lleva llaves y comillas dentro, y cortar por la primera `}` parte el
    atributo por la mitad y deja medio JavaScript haciéndose pasar por el
    nombre de una clase.
    """
    fuera: list[str] = []
    prof = 0
    i = 0
    while i < len(texto):
        if texto.startswith("${", i):
            if prof == 0:
                fuera.append(HUECO)
            prof += 1
            i += 2
            continue
        if prof:
            if texto[i] == "{":
                prof += 1
            elif texto[i] == "}":
                prof -= 1
            i += 1
            continue
        fuera.append(texto[i])
        i += 1
    return "".join(fuera)


def _trozos(valor: str) -> tuple[list[str], list[str]]:
    """Parte un `class="..."` en nombres enteros y prefijos que se completan."""
    enteros, prefijos = [], []
    for tr in _sin_interpolar(valor).split():
        if HUECO not in tr:
            if NOMBRE.match(tr):
                enteros.append(tr)
            continue
        cabeza = tr.split(HUECO)[0]
        # `semaforo-` de `semaforo-${d.light}`. Un trozo que es SOLO el hueco no
        # deja nada que comprobar y no se cuenta: comprobarlo sería inventarse
        # un nombre.
        if cabeza and NOMBRE.match(cabeza):
            prefijos.append(cabeza)
    return enteros, prefijos


def _elementos() -> list[dict]:
    """Cada etiqueta con `id` o `class`, la escriba el HTML o una plantilla JS.

    Se leen las etiquetas y no solo los atributos sueltos porque hace falta
    saber QUÉ NOMBRES LLEVA UN MISMO ELEMENTO: un `id` sin regla es un fallo si
    el elemento no tiene nada más, y es un asidero de JavaScript perfectamente
    normal si además lleva una clase que sí está peinada.
    """
    fuera = []
    for f in HTML + JS:
        texto = (ESTATICOS / f).read_text(encoding="utf-8")
        for m in re.finditer(
            r"""<([A-Za-z][\w-]*)((?:[^<>"']|"[^"]*"|'[^']*')*?)>""", texto
        ):
            atrs = m.group(2)
            mid = re.search(r"""\bid\s*=\s*["']([^"']*)["']""", atrs)
            mcl = re.search(r"""\bclass\s*=\s*["']([^"']*)["']""", atrs)
            if not mid and not mcl:
                continue
            crudo = mid.group(1).strip() if mid else ""
            # Un `id="sl-${key}"` no es un nombre: es una familia de nombres, y
            # ninguno de ellos está escrito en ninguna parte para comparar.
            ident = crudo if (crudo and "${" not in crudo and NOMBRE.match(crudo)) else ""
            enteros, prefijos = _trozos(mcl.group(1)) if mcl else ([], [])
            if not ident and not enteros and not prefijos:
                continue
            fuera.append({
                "donde": f"{f}:{texto.count(SALTO, 0, m.start()) + 1}",
                "etiqueta": m.group(1),
                "id": ident,
                "clases": enteros,
                "prefijos": prefijos,
            })
    return fuera


def _clases_puestas_por_javascript() -> dict[str, set[str]]:
    """`className = "..."` y `classList.add("...")`: clases sin etiqueta delante.

    La tarjeta de previsualizar entra por aquí y por ningún otro sitio: su
    `<section id="previsualizacion" hidden>` nace sin clases en el HTML y
    `app.js` le pone `previsualizacion semaforo-…` al pintarla. Mirar solo las
    etiquetas del HTML dejaría fuera justo el elemento que provocó esto.
    """
    fuera: dict[str, set[str]] = {}
    for f in JS:
        texto = (ESTATICOS / f).read_text(encoding="utf-8")
        trozos = [
            (m.start(), m.group(2))
            for m in re.finditer(r"""className\s*\+?=\s*(["'`])(.*?)\1""", texto, re.S)
        ]
        for m in re.finditer(r"""classList\.(?:add|remove|toggle)\(([^)]*)\)""", texto):
            for lit in re.findall(r"""["'`]([^"'`]*)["'`]""", m.group(1)):
                trozos.append((m.start(), lit))
        for pos, valor in trozos:
            donde = f"{f}:{texto.count(SALTO, 0, pos) + 1}"
            enteros, prefijos = _trozos(valor)
            for n in enteros + [p + HUECO for p in prefijos]:
                fuera.setdefault(n, set()).add(donde)
    return fuera


# ---------------------------------------------------------------------------
# Lo que la hoja de estilos nombra
# ---------------------------------------------------------------------------


def _nombres_del_css() -> tuple[set[str], set[str]]:
    """Las clases y los `id` que aparecen en algún selector.

    Se quitan los comentarios antes de mirar, y no es un detalle: este
    repositorio comenta mucho y en los comentarios se citan nombres de clase.
    Sin quitarlos, `styles.css` "nombraría" cualquier clase de la que alguien
    hubiera hablado, que es la forma más tonta de que este test apruebe en
    falso.
    """
    css = re.sub(
        r"/\*.*?\*/", " ",
        (ESTATICOS / "styles.css").read_text(encoding="utf-8"),
        flags=re.S,
    )
    clases: set[str] = set()
    ids: set[str] = set()
    for bloque in re.finditer(r"([^{}]+)\{", css):
        sel = bloque.group(1).strip()
        if not sel or sel.startswith("@"):
            continue
        clases.update(re.findall(r"\.([A-Za-z_][\w-]*)", sel))
        ids.update(re.findall(r"#([A-Za-z_][\w-]*)", sel))
    return clases, ids


# ---------------------------------------------------------------------------
# Las dos listas de excepciones
# ---------------------------------------------------------------------------

# CLASES que se escriben a propósito sin una regla propia. Cada línea dice de
# dónde saca su aspecto, porque una excepción sin motivo escrito es la forma
# educada de apagar un test.
SIN_REGLA_A_PROPOSITO = {
    "g-calendario": (
        "el calendario de ciento ochenta días va SIEMPRE dentro de un "
        '`<div class="desliza">` (metricas.js), y de ahí saca su '
        "`display: block`. No entra en la regla de `svg.g-barra` y compañía "
        "porque aquella lleva `max-width: 100%`, y aquí eso es exactamente lo "
        "que no puede pasar: el dibujo es MÁS ANCHO que el móvil a propósito y "
        "se desliza en horizontal. Encogerlo hasta que quepa deja los cuadros "
        "de los días indistinguibles, que es el motivo escrito en `.desliza`"
    ),
    "g-semanas": (
        "igual que `g-calendario`: dentro de `.desliza`, y además con un "
        '`width="..."` en la etiqueta porque el ancho lo calcula el propio '
        "dibujante a partir de cuántas semanas hay"
    ),
}

# IDS cuyo elemento no lleva ninguna clase y aun así no pinta nada de su
# cosecha: existen para que `getElementById` tenga a qué agarrarse. La
# diferencia con `#previsualizar` es la que importa, y por eso están uno a uno
# y no por un patrón: `#previsualizar` ES el botón.
SOLO_SON_ASIDEROS = {
    "salud": 'contenedor vacío; lo que se ve son los `<p class="aviso ...">` de dentro',
    "formulario": "el `<form>`. No pinta: coloca. Sus hijos sí están peinados",
    "deslizadores": 'contenedor; cada hijo sale con `class="slider"`, que sí tiene regla',
    "comentarios": (
        "un `<textarea>`, y `textarea { ... }` a secas lo peina entero: fondo, "
        "borde, color y tipo de letra. El `id` está para leerle el valor"
    ),
    "titulo": (
        "un `<h1>` dentro de `<header class=\"cab\">`, o sea `.cab h1`. El `id` "
        "está porque cada vista de métricas le escribe su propio título"
    ),
    "etiqueta-comentarios": (
        'un `<span>` dentro de `<label class="comentarios">`: hereda de ahí, y '
        "el `id` está para escribirle el texto que mande el servidor"
    ),
    "dias": "un `<select>` alcanzado por `.barra-ventana select` (metricas.html)",
    "resp": "un `<select>` alcanzado por `.filtro select` (metricas.js)",
}


# ---------------------------------------------------------------------------
# Las comprobaciones
# ---------------------------------------------------------------------------


def test_toda_clase_que_se_pinta_tiene_una_regla_que_la_nombra():
    """Una clase es intención de pintar. Aquí no se usan como asideros.

    Para agarrar algo desde JavaScript este proyecto usa `id` -`$("resultado")`-
    y para pintarlo usa clases. Así que una clase que ninguna regla nombra es
    siempre lo mismo: alguien escribió el nombre de un aspecto que no existe.

    Se exige que la regla la NOMBRE, y no que "algo la alcance". La diferencia
    la enseñó la tarjeta de previsualizar: la alcanzaba `.semaforo-green` y no
    le servía de nada, porque esa regla solo pone un color de borde y el borde
    lo daba `.resultado`, que la tarjeta no lleva.
    """
    css_clases, _ = _nombres_del_css()

    escritas: dict[str, set[str]] = {}
    prefijos: dict[str, set[str]] = {}
    for el in _elementos():
        for c in el["clases"]:
            escritas.setdefault(c, set()).add(el["donde"])
        for p in el["prefijos"]:
            prefijos.setdefault(p, set()).add(el["donde"])
    for n, donde in _clases_puestas_por_javascript().items():
        if n.endswith(HUECO):
            prefijos.setdefault(n[:-1], set()).update(donde)
        else:
            escritas.setdefault(n, set()).update(donde)

    huerfanas = {
        c: sitios for c, sitios in sorted(escritas.items())
        if c not in css_clases and c not in SIN_REGLA_A_PROPOSITO
    }
    assert not huerfanas, (
        "clases que la pantalla escribe y que `styles.css` no nombra en ningún "
        "selector. El elemento sale con la pinta que le ponga el navegador, que "
        "en esta aplicación quiere decir claro sobre oscuro y pequeño:\n"
        + SALTO.join(f"  .{c:<22} {', '.join(sorted(s))}" for c, s in huerfanas.items())
    )

    # Los nombres que se componen -`semaforo-${d.light}`- no se pueden buscar
    # enteros, pero su principio sí: si no hay NI UNA clase que empiece por ahí,
    # es que la familia entera está sin peinar.
    sin_familia = {
        p: sitios for p, sitios in sorted(prefijos.items())
        if not any(c.startswith(p) for c in css_clases)
    }
    assert not sin_familia, (
        "familias de clases que se componen en tiempo de ejecución y de las que "
        "`styles.css` no tiene ni una:\n"
        + SALTO.join(f"  .{p}…  {', '.join(sorted(s))}" for p, s in sin_familia.items())
    )


def test_un_elemento_que_solo_se_llama_por_su_id_tiene_una_regla():
    """El caso de `#previsualizar`: se nombra a sí mismo y nadie le contesta.

    Un `id` sin regla no es un fallo por sí solo -la mayoría son asideros para
    `getElementById`, y el elemento se pinta por su clase-. Lo es cuando el
    elemento NO TIENE NADA MÁS: entonces el `id` es lo único que dice qué es
    esa cosa, y si el CSS no lo menciona, no lo pinta nadie.

    También cuenta la clase que JavaScript le ponga luego al mismo elemento:
    `<section id="resultado" hidden>` nace pelado y recibe
    `className = "resultado ..."`, y `.resultado` sí tiene regla. Sin mirar
    eso, este test pediría una regla `#resultado` que sobraría.
    """
    css_clases, css_ids = _nombres_del_css()
    puestas_por_js = set(_clases_puestas_por_javascript())

    desnudos = []
    for el in _elementos():
        if not el["id"] or el["clases"] or el["prefijos"]:
            continue
        if el["id"] in css_ids or el["id"] in SOLO_SON_ASIDEROS:
            continue
        # `#resultado` -> `.resultado`, puesta por `app.js` al vuelo.
        if el["id"] in css_clases and el["id"] in puestas_por_js:
            continue
        desnudos.append(el)

    assert not desnudos, (
        "elementos cuyo único nombre es su `id`, sin ninguna clase y sin "
        "ninguna regla que los nombre. Se pintan con lo que traiga el "
        "navegador:\n"
        + SALTO.join(
            f"  <{el['etiqueta']}> #{el['id']:<22} {el['donde']}" for el in desnudos
        )
        + "\n\nSi de verdad no tiene que pintar nada de su cosecha, va a "
        "`SOLO_SON_ASIDEROS` con el motivo escrito."
    )


def test_las_dos_listas_de_excepciones_no_se_quedan_hablando_de_fantasmas():
    """Una excepción para algo que ya no existe es una excepción que tapa.

    Es la avería de siempre de este repositorio, aplicada a las dos listas de
    arriba: el día que una clase se renombre o que el botón de previsualizar
    desaparezca, la entrada correspondiente se queda, y lo que queda no es una
    línea de más sino un permiso abierto con el nombre de nadie. Peor todavía:
    si el nombre vuelve más adelante para otra cosa, nace ya perdonado.
    """
    escritas = set()
    for el in _elementos():
        escritas.update(el["clases"])
        if el["id"]:
            escritas.add(el["id"])
    escritas.update(_clases_puestas_por_javascript())

    for lista, como in ((SIN_REGLA_A_PROPOSITO, "clase"), (SOLO_SON_ASIDEROS, "id")):
        sobran = sorted(n for n in lista if n not in escritas)
        assert not sobran, (
            f"la lista de excepciones perdona {como}s que ya no escribe nadie: "
            f"{sobran}. Quítalas: mientras estén, el nombre que vuelva a "
            f"aparecer nace perdonado."
        )


def test_no_se_perdona_a_quien_ya_tiene_regla():
    """El fantasma del otro sentido, y es el que casi se cuela.

    `g-hrv` estuvo en la lista de excepciones con su motivo escrito -se
    estiraba por un `width="100%"` en la etiqueta, le faltaba solo el
    `display: block`-. Al añadirle la regla, la excepción se quedaba ahí
    perdonando a alguien que ya no lo necesita, y la comprobación de arriba no
    la veía: `g-hrv` SE SIGUE ESCRIBIENDO, así que no era un nombre de nadie.

    Una excepción así no rompe nada hoy y hace daño mañana: dice que ese
    elemento se pinta por otros medios cuando ya no es verdad, así que el día
    que alguien borre la regla -limpiando CSS que "no usa nadie", por ejemplo-
    el test no se va a quejar. El permiso sigue firmado.
    """
    css_clases, css_ids = _nombres_del_css()

    ya_peinadas = sorted(n for n in SIN_REGLA_A_PROPOSITO if n in css_clases)
    assert not ya_peinadas, (
        f"estas clases están en `SIN_REGLA_A_PROPOSITO` y `styles.css` ya las "
        f"nombra: {ya_peinadas}. La excepción sobra, y mientras esté cubre el "
        f"día que alguien borre esa regla."
    )

    ya_peinados = sorted(n for n in SOLO_SON_ASIDEROS if n in css_ids)
    assert not ya_peinados, (
        f"estos `id` están en `SOLO_SON_ASIDEROS` -o sea declarados como "
        f"asideros que no pintan- y `styles.css` les escribe una regla: "
        f"{ya_peinados}. Una de las dos cosas es mentira."
    )


def test_la_guarda_mira_todas_las_paginas_y_todos_los_scripts():
    """La guarda de la guarda.

    El 25/09/2026 las dos listas de arriba estaban escritas a mano y dos páginas
    nuevas pasaron en verde sin que nadie las abriera. Ahora salen del disco;
    esto es lo que impide que vuelvan a escribirse a mano sin que se note.
    """
    en_disco_html = {p.name for p in ESTATICOS.glob("*.html")}
    en_disco_js = {p.name for p in ESTATICOS.glob("*.js")} - {"sw.js"}
    assert set(HTML) == en_disco_html, sorted(en_disco_html - set(HTML))
    assert set(JS) == en_disco_js, sorted(en_disco_js - set(JS))


# ---------------------------------------------------------------------------
# `hidden` que no esconde
# ---------------------------------------------------------------------------


def _reglas_sin_comentarios() -> list[tuple[list[str], str]]:
    css = re.sub(r"/\*.*?\*/", "", (ESTATICOS / "styles.css").read_text(encoding="utf-8"), flags=re.S)
    return [
        ([s.strip() for s in m.group(1).split(",")], m.group(2))
        for m in re.finditer(r"([^{}]+)\{([^}]*)\}", css)
    ]


def test_todo_lo_que_se_esconde_con_hidden_se_esconde_de_verdad():
    """Un `display` del autor le gana al `[hidden]` del navegador.

    El atributo `hidden` solo esconde porque el navegador lo traduce a
    `display: none`, y esa traducción es una regla MÁS BAJA que cualquiera que
    escriba esta hoja. Si la clase de un elemento dice `display: flex`, poner
    `el.hidden = true` deja el atributo en el DOM y el elemento en pantalla: el
    código cree que lo ha escondido, el test que mira el atributo también, y el
    usuario lo sigue viendo.

    Se encontró el 25/09/2026 con la barra del selector de ventana, antes de que
    llegara a ningún móvil: `.barra-ventana` lleva `display: flex` y la portada
    iba a esconderla con `hidden`. Se barrió entonces toda la aplicación y era
    el único caso; esto es lo que impide que el siguiente pase igual.

    Sigue los `$("id").hidden = ...` y las variables que guardan un `$("id")`.
    No sigue elementos buscados por clase, que es donde esta comprobación se
    queda corta -dicho para que no se lea como más de lo que es-.
    """
    reglas = _reglas_sin_comentarios()

    def display_de(sel: str) -> str | None:
        for sels, cuerpo in reglas:
            if sel in sels:
                d = re.search(r"display\s*:\s*([\w-]+)", cuerpo)
                if d and d.group(1) != "none":
                    return d.group(1)
        return None

    def cubierto(sel: str) -> bool:
        return any(f"{sel}[hidden]" in s or s == "[hidden]" for sels, _ in reglas for s in sels)

    escondidos: dict[str, set[str]] = {}
    for f in JS:
        texto = re.sub(r"/\*.*?\*/", "", (ESTATICOS / f).read_text(encoding="utf-8"), flags=re.S)
        for i in re.findall(r'\$\("([\w-]+)"\)\.hidden\s*=', texto):
            escondidos.setdefault(i, set()).add(f)
        for var, i in re.findall(r'(?:const|let)\s+(\w+)\s*=\s*\$\("([\w-]+)"\)', texto):
            if re.search(rf"\b{var}\.hidden\s*=", texto):
                escondidos.setdefault(i, set()).add(f)
    assert escondidos, "no encuentro ningún `.hidden = ...`: el patrón de búsqueda ha dejado de valer"

    clases_de: dict[str, list[str]] = {}
    for h in HTML:
        for m in re.finditer(r'<[a-z]+[^>]*\bid="([\w-]+)"[^>]*>', (ESTATICOS / h).read_text(encoding="utf-8")):
            c = re.search(r'class="([^"]+)"', m.group(0))
            clases_de[m.group(1)] = c.group(1).split() if c else []

    visibles = []
    for i, donde in sorted(escondidos.items()):
        for sel in ["#" + i] + ["." + c for c in clases_de.get(i, [])]:
            d = display_de(sel)
            if d and not cubierto(sel):
                visibles.append(f"#{i} ({', '.join(sorted(donde))}): {sel} lleva display:{d}")
    assert not visibles, (
        "estos elementos se esconden con `hidden` y su regla les pone un "
        "`display`, así que el atributo se pone y el elemento sigue en pantalla. "
        "Añade `SELECTOR[hidden] { display: none; }`:\n  " + "\n  ".join(visibles)
    )
