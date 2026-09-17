"""La PWA, vigilada desde Python.

No se prueba aquí que la pantalla quede bonita. Se prueba lo único que un test
de Python puede saber de un archivo de JavaScript y que además importa: que lo
que la PWA cree del backend siga siendo verdad.

Los tres fallos que motivan este archivo pasaron los tres en silencio:

- `pintarCobertura` leía `v.length === 2` sobre el `{desde, hasta}` que manda
  `Cobertura.como_dict`. Un objeto no tiene `length`, así que la comprobación
  daba `false` siempre y las cinco vistas abrían diciendo "Sin ningún dato en
  esta ventana de: check-ins, datos de Garmin, salidas de bici, entrenos de
  fuerza" encima de ciento setenta y nueve días de Garmin. Ni un error, ni un
  `undefined`: la rama contraria y una frase bien escrita afirmando lo
  contrario de lo que pasaba.
- `ARMAZON` en el service worker se quedó nombrando solo el check-in cuando se
  añadieron las métricas. Online no se nota -el `fetch` cachea al vuelo lo que
  se va pidiendo-, así que el fallo espera al primer arranque sin cobertura de
  una pantalla que nunca se visitó con red.
- `icon-maskable.svg` estaba en el manifest y no en `ARMAZON`.

El patrón es siempre el mismo: dos sitios que tienen que decir lo mismo, y
nada que los ate. Lo que hacen estos tests es atarlos.

Se lee el JavaScript como TEXTO, con expresiones regulares. Es feo y se sabe.
La alternativa -no comprobar nada, o montar un intérprete- es peor, y cada
extracción se rompe ruidosamente si el archivo cambia de forma: si la regex no
encuentra nada, el test falla en vez de pasar en vacío. Eso es lo que hace que
sirva de algo.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.analysis.series import Cobertura
from app.api import app, get_config
from app.db import get_session
from app.models import (
    Activity,
    Base,
    Checkin,
    DailyMetrics,
    Decision,
    RuleState,
    SessionPerformance,
    WorkoutLog,
)

RAIZ = Path(__file__).resolve().parents[1]
ESTATICOS = RAIZ / "static"

SW = (ESTATICOS / "sw.js").read_text(encoding="utf-8")
COMUN = (ESTATICOS / "comun.js").read_text(encoding="utf-8")
METRICAS = (ESTATICOS / "metricas.js").read_text(encoding="utf-8")
GRAFICOS = (ESTATICOS / "graficos.js").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Sacar listas de un archivo de JavaScript
# ---------------------------------------------------------------------------


def _lista(fuente: str, nombre: str) -> list[str]:
    """Los literales de cadena de `const NOMBRE = [...]`.

    Se corta en el primer `];` que aparece a principio de línea, que es como
    están escritos todos. Si no se encuentra el bloque se levanta: un test que
    no encuentra lo que iba a comprobar tiene que romperse, no aprobar.
    """
    m = re.search(rf"const {nombre} = \[(.*?)^\];", fuente, re.S | re.M)
    if m is None:
        raise AssertionError(f"no encuentro `const {nombre} = [...]` en el archivo")
    return re.findall(r'"([^"]*)"', m.group(1))


def _sin_comentarios(fuente: str) -> str:
    """El código sin los comentarios.

    Hace falta en los dos sitios donde se busca algo por su nombre. Los
    comentarios de estos archivos son prosa larga que habla justo de lo que el
    test persigue -el fallo del `v.length`, la frontera del cálculo-, así que
    sin quitarlos el test se dispara con su propia documentación.
    """
    fuente = re.sub(r"/\*.*?\*/", "", fuente, flags=re.S)
    return re.sub(r"^\s*//.*$", "", fuente, flags=re.M)


def _claves_de_objeto(fuente: str, nombre: str) -> list[str]:
    """Las claves de primer nivel de `const NOMBRE = { ... };`."""
    m = re.search(rf"const {nombre} = \{{(.*?)^\}};", fuente, re.S | re.M)
    if m is None:
        raise AssertionError(f"no encuentro `const {nombre} = {{...}}` en el archivo")
    cuerpo = m.group(1)
    # Solo lo que está a un nivel de indentación: las claves de dentro de un
    # valor anidado no son claves del objeto.
    return re.findall(r"^  ([A-Za-z_][A-Za-z0-9_]*):", cuerpo, re.M)


# ---------------------------------------------------------------------------
# El armazón del service worker
# ---------------------------------------------------------------------------

# Lo que se queda fuera del caché A PROPÓSITO, escrito a mano y con el motivo.
# La lista tiene que ser explícita: si fuera un patrón -"los .js no"- el
# olvido que este test persigue volvería a colarse por el patrón.
FUERA_DEL_ARMAZON = {
    # Un service worker no se cachea a sí mismo. Lo gestiona el navegador
    # aparte, y meterlo en su propio caché es la forma de que un `sw.js` roto
    # se vuelva imposible de sustituir.
    "sw.js",
}


def _archivos_de_static() -> set[str]:
    return {
        p.relative_to(ESTATICOS).as_posix()
        for p in ESTATICOS.rglob("*")
        if p.is_file()
    }


def test_el_armazon_del_service_worker_nombra_todo_lo_que_hay_en_static():
    """Cada archivo de `static/` está cacheado o excluido a mano.

    El fallo que evita no se ve nunca desarrollando: con red, el `fetch` del
    service worker guarda al vuelo todo lo que se pide, así que la aplicación
    va perfecta aunque `ARMAZON` nombre la mitad. Lo que se rompe es abrir sin
    cobertura una pantalla que nunca se abrió con ella, y eso pasa justo el día
    que hace falta.
    """
    armazon = set(_lista(SW, "ARMAZON"))
    # "/" es el alias de "/index.html", no un archivo.
    cacheados = {r.lstrip("/") for r in armazon if r != "/"}

    faltan = _archivos_de_static() - cacheados - FUERA_DEL_ARMAZON
    assert not faltan, (
        f"estos archivos de static/ no están en ARMAZON ni excluidos a mano: "
        f"{sorted(faltan)}. O se cachean, o se añaden a FUERA_DEL_ARMAZON con "
        f"el motivo escrito."
    )


def test_el_armazon_no_nombra_ficheros_que_no_existen():
    """Un nombre mal escrito en `ARMAZON` deja la instalación sin caché ENTERA.

    `cache.addAll` es todo o nada: si una sola de las URL da 404, la promesa se
    rompe, el `install` falla y no se cachea NINGÚN archivo. La aplicación
    sigue yendo online, así que el estropicio solo aparece sin conexión, y
    entonces no hay nada, no falta uno.
    """
    for ruta in _lista(SW, "ARMAZON"):
        if ruta == "/":
            continue
        assert (ESTATICOS / ruta.lstrip("/")).is_file(), (
            f"ARMAZON nombra `{ruta}`, que no existe en static/. `addAll` es "
            f"todo o nada: con esto, el service worker no cachea nada."
        )


def _version_del_sw() -> str:
    """El `const VERSION = "vN"` del service worker, o se levanta."""
    m = re.search(r'const VERSION = "([^"]+)";', SW)
    if m is None:
        raise AssertionError('no encuentro `const VERSION = "..."` en sw.js')
    return m.group(1)


def _huella_del_armazon() -> str:
    """Una huella del CONTENIDO de todo lo que el service worker cachea.

    En bytes y no en texto, porque en `ARMAZON` hay cinco PNG. Ordenada por
    ruta, y con la ruta dentro del hash: si no, renombrar un archivo por otro
    del mismo tamaño y contenido no movería la huella.
    """
    h = hashlib.sha256()
    for ruta in sorted(_lista(SW, "ARMAZON")):
        if ruta == "/":
            continue  # alias de /index.html, que ya está en la lista
        p = ESTATICOS / ruta.lstrip("/")
        h.update(ruta.encode("utf-8"))
        h.update(p.read_bytes())
    return h.hexdigest()


# La huella del armazón en cada versión. Se añade una línea al subir `VERSION`.
#
# Las viejas se quedan a propósito: son la prueba de que el número se movió de
# verdad cada vez, que es justo lo que no pasó entre la v5 y esta. Que la lista
# crezca es la señal de que la regla se está cumpliendo.
HUELLAS_DEL_ARMAZON = {
    "v6": "6f2fbb1d5b8582bacc197674486762dafc8a004db939ec4cf97244eebd648c5a",
    # v7: las dos preguntas de Sí/No en el formulario. Toca los tres archivos
    # del armazón que se abren todas las mañanas -`index.html`, `app.js` y
    # `styles.css`-, así que es exactamente el caso para el que está la regla:
    # un móvil que estuvo sin cobertura se quedaría con el formulario de antes,
    # sin las preguntas, y no habría nada que lo dijera.
    "v7": "7c82f876e4881b9470a7719e87999eea6e486bec71659c7067d58d994be3fbcd",
    # v8: la otra mitad de las dos preguntas. La v7 las metió en el formulario
    # -que es donde se contestan- y esta las saca en la ficha: la tabla de las
    # cuatro casillas, en `metricas.js` y `styles.css`. Aquí el móvil viejo no
    # se rompe, que es peor: la API ya manda `tabla` y el JavaScript de antes la
    # tira a la basura sin decir nada, así que la pantalla se ve entera y le
    # falta justo lo que se pidió. Sin subir el número, eso dura hasta que al
    # teléfono le dé por revalidar solo.
    "v8": "cd6bc73ac83d1225403ac52439b6eb8e87f5963bcbdace02ccf4dc494f94f6ef",
    # v9: el selector de qué se va a hacer hoy. Toca otra vez los tres archivos
    # que se abren todas las mañanas -`index.html`, `app.js` y `styles.css`-, y
    # aquí el móvil viejo falla de la forma callada: la API ya manda `selector`
    # y el JavaScript de antes lo tira a la basura sin decir nada. El formulario
    # se ve entero, se envía, se decide y escribe la rutina de la rotación. O
    # sea que funciona, y justamente por eso no se notaría: lo único que faltaría
    # es poder elegir otra cosa, que es todo lo que se pidió.
    "v9": "b28b496ae5508b6805e85f7e3c248944946d63f8c3bd5debe4cc35c0370c33ce",
    # v10: la pantalla del umbral de la bici. Vista nueva entera -`comun.js` por
    # la barra, `graficos.js` por los dos dibujos, `metricas.js` por la vista y
    # `styles.css` por la fila señalada de las tablas-, o sea cuatro de los
    # cuatro archivos de JavaScript y CSS del armazón.
    #
    # Y aquí el móvil viejo falla de la peor forma de las tres: la barra de abajo
    # es la de antes, sin «Umbral», así que a la pantalla NO SE LLEGA. No sale
    # rota, no sale vacía, no sale a medias; sencillamente no está, y desde el
    # teléfono eso es indistinguible de que no se haya hecho todavía.
    "v10": "993517bf1516739d131152012f9d48ccbf2a92a23435a2232fea021fd16bd7de",
    # v11: el estado nuevo de la auditoría de reglas. El ámbar por precaución lo
    # pone el motor y no el `config.yaml`, así que no estaba en el catálogo y la
    # vista resolvía esa diferencia como «retirada»: borde rojo y un texto que
    # decía que ya no existe. Se le da estado propio -`no_hizo_falta`- y toca
    # `metricas.js` por el orden de las fichas y `styles.css` por el borde.
    #
    # El móvil viejo falla de la forma callada, como en la v8: la API ya manda
    # `estado: "no_hizo_falta"` y `del_motor`, y el JavaScript de antes no
    # conoce ninguna de las dos cosas. La ficha sale sin borde y la última de la
    # lista, o sea que la pantalla se ve entera y bien; lo único mal ordenado es
    # justo la regla por la que se hizo el cambio.
    "v11": "7b0422fe51d0f5afa8dffd9ca7bd14e864645a1170445b60fa014959d4f1b16f",
}


def test_la_version_del_service_worker_sube_cuando_cambia_el_armazon():
    """Una regla que solo vive en un comentario no es una regla.

    `sw.js` lleva escrito desde siempre que la versión sube cada vez que cambia
    el armazón, y entre medias `metricas.js` cambió dos veces con la versión
    clavada en "v5". Nadie lo vio, y no por descuido: el `fetch` del service
    worker va a la red primero, así que con cobertura el archivo nuevo llega
    igual y la pantalla se ve bien. Lo que la versión protege es el móvil que
    estuvo sin red -se queda con el `armazon-v5` entero, `activate` no lo borra
    porque el nombre no ha cambiado, y abre un `metricas.js` viejo contra una
    API nueva-. Ese caso no aparece mirando la pantalla ningún día.

    Así que se ata aquí: la huella del contenido, al lado del número.
    """
    version = _version_del_sw()
    huella = _huella_del_armazon()

    assert version in HUELLAS_DEL_ARMAZON, (
        f"`sw.js` va por la versión {version!r} y no está en "
        f"HUELLAS_DEL_ARMAZON. Añade la línea {version!r}: {huella!r}."
    )
    assert HUELLAS_DEL_ARMAZON[version] == huella, (
        f"el armazón ha cambiado y `VERSION` sigue en {version!r}. Sube la "
        f"versión en `static/sw.js` y apunta aquí la huella nueva:\n"
        f'    "vN": "{huella}",\n'
        f"Sin eso, el caché nuevo se llama igual que el viejo, `activate` no "
        f"borra nada y un móvil que estuvo sin cobertura se queda con el "
        f"JavaScript de antes hablando con la API de ahora."
    )


def test_el_armazon_cachea_los_iconos_que_pide_el_manifest():
    """Instalar la aplicación sin cobertura tiene que dar el icono bueno."""
    manifest = json.loads((ESTATICOS / "manifest.webmanifest").read_text("utf-8"))
    armazon = set(_lista(SW, "ARMAZON"))
    for icono in manifest["icons"]:
        assert icono["src"] in armazon, (
            f"el manifest pide `{icono['src']}` y el armazón no lo cachea"
        )


# ---------------------------------------------------------------------------
# Los iconos: un binario en el repositorio no tiene forma de seguir siendo verdad
# ---------------------------------------------------------------------------


def _manifest() -> dict:
    return json.loads((ESTATICOS / "manifest.webmanifest").read_text("utf-8"))


def test_los_png_son_el_dibujo_del_svg_de_hoy():
    """El único fallo que un PNG guardado puede tener, y no avisa de ninguno.

    Se cambia un color en el SVG, el PNG se queda con el de antes, y nadie mira
    un icono de 192 píxeles lo bastante de cerca como para verlo: quedan dos
    semáforos de colores distintos según por dónde se abra la aplicación.

    Se comparan PÍXELES y no bytes. Bytes compararía de paso la versión de zlib
    de la máquina, que no es asunto de este proyecto: el mismo dibujo comprimido
    por otro zlib son otros bytes y el mismo icono, y un test que falla por eso
    se acaba borrando.
    """
    from scripts.generar_iconos import de_png, pintar_todo

    for nombre, png in pintar_todo().items():
        ruta = ESTATICOS / "icons" / nombre
        assert ruta.is_file(), (
            f"falta `{nombre}`. Se hace con: python scripts/generar_iconos.py"
        )
        assert de_png(ruta.read_bytes()) == de_png(png), (
            f"`{nombre}` ya no es lo que dibuja su SVG. Vuelve a generarlo con: "
            f"python scripts/generar_iconos.py"
        )


def test_cada_proposito_del_manifest_tiene_un_png():
    """El fallo original: el manifest solo ofrecía SVG.

    No da ningún error en ningún sitio. Da un cuadrado blanco con una letra
    dentro en la pantalla de inicio, que uno lee como "la instalación no ha ido
    bien" en vez de como "falta un formato".
    """
    por_proposito: dict[str, list[str]] = {}
    for icono in _manifest()["icons"]:
        por_proposito.setdefault(icono["purpose"], []).append(icono["type"])

    assert set(por_proposito) == {"any", "maskable"}, (
        f"propósitos en el manifest: {sorted(por_proposito)}. `maskable` es el "
        f"que coge Android para la pantalla de inicio y `any` el resto; faltando "
        f"uno, ese caso cae en el icono genérico"
    )
    for proposito, tipos in por_proposito.items():
        assert "image/png" in tipos, (
            f"el propósito `{proposito}` solo se ofrece en {sorted(set(tipos))}: "
            f"el SVG en el manifest solo lo entiende un Chrome reciente"
        )


def test_el_apple_touch_icon_esta_enlazado_y_es_opaco():
    """Safari no mira el manifest para esto, y no perdona la transparencia.

    Dos fallos distintos, los dos silenciosos. Sin el `<link>`, "Añadir a
    pantalla de inicio" en un iPhone guarda un RECORTE DE LA PÁGINA como icono.
    Y con el icono equivocado -el normal, que trae sus propias esquinas
    redondeadas y transparentes- iOS le aplica encima su máscara y rellena lo
    que falta: sale un rectángulo redondeado dentro de otro, con una costura.

    Por eso el `apple-touch-icon` sale del maskable, que es a sangre. Se
    comprueba mirando las esquinas del PNG, que es donde se nota, y no el nombre
    del SVG de origen: el nombre lo cambia un renombrado y las esquinas no.
    """
    from scripts.generar_iconos import de_png

    for pagina in ("index.html", "metricas.html"):
        html = (ESTATICOS / pagina).read_text(encoding="utf-8")
        assert 'rel="apple-touch-icon"' in html, (
            f"{pagina} no enlaza el apple-touch-icon: en iOS el icono de la "
            f"pantalla de inicio sería una captura de la propia página"
        )

    lado, pixeles = de_png((ESTATICOS / "icons" / "apple-touch-icon.png").read_bytes())
    esquinas = [(0, 0), (lado - 1, 0), (0, lado - 1), (lado - 1, lado - 1)]
    for x, y in esquinas:
        alfa = pixeles[(y * lado + x) * 4 + 3]
        assert alfa == 255, (
            f"la esquina ({x}, {y}) del apple-touch-icon tiene alfa {alfa}: iOS "
            f"rellena lo transparente por su cuenta y deja costura"
        )


def test_un_svg_con_algo_que_el_dibujante_no_entiende_no_pasa_en_silencio(tmp_path):
    """Lo que no se sabe dibujar tiene que reventar, no saltarse.

    Un `<path>` nuevo ignorado en silencio daría un PNG al que le falta un trozo
    del icono; un `opacity="0.5"` ignorado daría un PNG con un color distinto al
    del SVG. Ninguna de las dos cosas se ve en un icono de 192 píxeles, y las
    dos convierten el SVG y el PNG en dos dibujos diferentes.
    """
    import pytest as _pytest

    from scripts.generar_iconos import SvgNoEntendido, leer_svg

    cabecera = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
    casos = {
        "figura_nueva": '<path d="M0 0 L10 10"/>',
        "atributo_nuevo": '<circle cx="10" cy="10" r="5" fill="#ffffff" opacity="0.5"/>',
        "color_por_nombre": '<circle cx="10" cy="10" r="5" fill="white"/>',
    }
    for nombre, cuerpo in casos.items():
        ruta = tmp_path / f"{nombre}.svg"
        ruta.write_text(f"{cabecera}{cuerpo}</svg>", encoding="utf-8")
        with _pytest.raises(SvgNoEntendido):
            leer_svg(ruta)

    # Y el control: sin la parte rara, el mismo SVG sí se lee. Sin esto, los
    # tres casos de arriba podrían estar fallando por cualquier otro motivo.
    bueno = tmp_path / "bueno.svg"
    bueno.write_text(
        f'{cabecera}<circle cx="10" cy="10" r="5" fill="#ffffff"/></svg>',
        encoding="utf-8",
    )
    assert leer_svg(bueno) == (512.0, [
        {"t": "circulo", "cx": 10.0, "cy": 10.0, "r": 5.0, "color": (255, 255, 255)}
    ])


def test_el_service_worker_no_cachea_nada_de_la_api():
    """El motivo por el que existe el service worker tal y como está escrito.

    Servir un `/api/checkin/today` de ayer abre el formulario diciendo "ya está
    hecho" un día en que no lo está: el sistema espera un envío que la pantalla
    da por hecho, tira del trabajo de respaldo de las 09:00 y decide sin
    check-in. Un semáforo de ayer pintado como el de hoy es una mentira que no
    se distingue de la verdad.

    Se comprueba de dos formas porque son dos afirmaciones distintas: que
    ninguna ruta de la API esté en la lista de cacheados, y que el código tenga
    el corte explícito.
    """
    assert not [r for r in _lista(SW, "ARMAZON") if r.startswith("/api")]
    assert "/api/" in SW and "return" in SW, "falta el corte explícito de /api/"


def test_el_ultimo_recurso_del_service_worker_solo_vale_para_paginas():
    """`/index.html` a un `<script src>` deja la pantalla en blanco y muda.

    El navegador recibiría el HTML del check-in con `Content-Type: text/html`,
    se negaría a ejecutarlo como JavaScript y no habría un solo error legible
    desde el móvil. Es mejor que el `fetch` falle: un recurso que falta se ve.
    """
    assert 'request.mode !== "navigate"' in SW, (
        "el último recurso del service worker tiene que estar limitado a las "
        "navegaciones, o servirá una página a quien pedía un script"
    )


# ---------------------------------------------------------------------------
# El contrato de la cobertura
# ---------------------------------------------------------------------------


def test_la_cobertura_de_la_pwa_lee_el_mismo_contrato_que_escribe_el_backend():
    """Las claves que `pintarCobertura` mira, contra las que el backend manda.

    Este es el test del fallo que motivó el archivo. `pintarCobertura` hacía
    `v.length === 2` sobre un `{desde, hasta}`; como un objeto no tiene
    `length`, las cuatro fuentes caían en la rama de "vacía" y la vista abría
    afirmando que no había ni un dato encima de ciento setenta y nueve días de
    Garmin.

    Lo grave no es el fallo, es que fuera invisible: ningún error, ningún
    `undefined` en pantalla, y una frase perfectamente escrita diciendo lo
    contrario de lo que pasaba, justo en la línea que existe para decir cómo
    hay que leer todo lo demás.

    Se comprueban las dos direcciones. Las claves de FUENTES contra las de
    `como_dict` -para que añadir una fuente en el backend no deje media
    pantalla muda- y las propiedades que se leen de cada ventana contra las que
    `como_dict` mete dentro.
    """
    contrato = Cobertura(
        checkin=(date(2026, 3, 15), date(2026, 9, 9)),
        garmin=(date(2026, 3, 15), date(2026, 9, 9)),
        bici=(date(2026, 3, 15), date(2026, 9, 9)),
        fuerza=(date(2026, 3, 15), date(2026, 9, 9)),
    ).como_dict()

    fuentes = set(_claves_de_objeto(COMUN, "FUENTES"))
    assert fuentes == set(contrato), (
        f"la PWA nombra las fuentes {sorted(fuentes)} y el backend manda "
        f"{sorted(contrato)}. Las que sobren se pintan como vacías siempre; "
        f"las que falten no se pintan y nadie lo dice."
    )

    # Lo que el bucle lee de cada ventana: `v.desde && v.hasta`.
    #
    # Sin quitar los comentarios, esto se lee a sí mismo: dentro de la función
    # hay un comentario largo explicando que ANTES ponía `v.length === 2`, y el
    # test fallaría eternamente por su propia explicación del fallo.
    cuerpo = COMUN[COMUN.index("function pintarCobertura") :]
    cuerpo = _sin_comentarios(cuerpo[: cuerpo.index("\n}\n")])
    leidas = set(re.findall(r"\bv\.([a-z_]+)\b", cuerpo))
    assert leidas, "no encuentro ninguna lectura `v.algo` en pintarCobertura"

    dentro = set(contrato["garmin"])
    assert leidas <= dentro, (
        f"pintarCobertura lee {sorted(leidas - dentro)} de una ventana, y "
        f"`Cobertura.como_dict` solo manda {sorted(dentro)}. Leer una clave "
        f"que no viene no da error: elige la rama contraria en silencio."
    )
    assert "length" not in leidas, (
        "pintarCobertura vuelve a mirar `v.length`. Es un objeto: `length` es "
        "`undefined` y la comprobación da `false` siempre"
    )


# ---------------------------------------------------------------------------
# Las rutas
# ---------------------------------------------------------------------------


@pytest.fixture
def base():
    """Base en memoria compartida entre hilos.

    `TestClient` atiende en un hilo distinto al del test, así que hacen falta
    `StaticPool` -para que las dos vean la MISMA base y no una vacía por
    conexión- y `check_same_thread=False`.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as ses:
        yield ses


@pytest.fixture
def cliente(base, cfg):
    """La API real contra la base en memoria.

    Los tests que no siembran nada la usan vacía a propósito: comprueban que las
    rutas EXISTAN y contesten 200 sin datos, que es el estado en el que se abre
    la aplicación el día de la instalación. Una vista que revienta con la base
    vacía es una pantalla de error el primer día.
    """
    app.dependency_overrides[get_session] = lambda: base
    app.dependency_overrides[get_config] = lambda: cfg
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_todas_las_rutas_que_pide_la_pwa_existen_en_la_api(cliente):
    """Cada URL de `RUTAS` contesta, y contesta 200 con la base vacía.

    Renombrar un endpoint en Python no rompe nada en Python. Rompe una pantalla
    del móvil, que es donde nadie está mirando cuando se hace el cambio.

    Y AL REVÉS TAMBIÉN, que es el que ha vuelto a pasar. Aquí había un `len(urls)
    == 6` escrito a mano: un número que dice cuántas rutas hay hoy y que no sabe
    nada de cuántas debería haber. `/api/metrics/portada` llevaba desde el día 13
    calculada, servida y con sus tests, y `metricas.js` no la pedía. Este test
    pasaba en verde con la vista más importante del panel sin un solo lector,
    porque contar seis de seis es exactamente lo mismo cuando faltan cero que
    cuando falta la séptima.

    Así que el número se fue y el denominador lo pone ahora la TABLA DE RUTAS de
    la aplicación. Un endpoint de métricas nuevo que el móvil no pida rompe aquí
    el día que se escribe, que es cuando todavía cuesta cinco minutos arreglarlo.
    """
    rutas = re.search(r"const RUTAS = \{(.*?)^\};", METRICAS, re.S | re.M)
    assert rutas is not None, "no encuentro `const RUTAS` en metricas.js"
    urls = set(re.findall(r'"(/api/[^"]+)"', rutas.group(1)))

    servidas = {
        r.path
        for r in app.routes
        if getattr(r, "path", "").startswith("/api/metrics/")
    }
    assert servidas, "no encuentro ni una ruta de métricas en la tabla de la app"

    huerfanas = servidas - urls
    assert not huerfanas, (
        f"el servidor calcula y sirve {sorted(huerfanas)} y `metricas.js` no lo "
        f"pide: son vistas enteras publicadas sin un solo lector, que es como "
        f"estuvo la portada desde que se escribió"
    )
    inventadas = urls - servidas
    assert not inventadas, (
        f"`metricas.js` pide {sorted(inventadas)} y la API no lo sirve: eso es "
        f"una pantalla del móvil que se queda con el error puesto"
    )

    for url in sorted(urls):
        r = cliente.get(url, params={"dias": 30})
        assert r.status_code == 200, f"{url} → {r.status_code} {r.text[:300]}"


def test_el_armazon_se_sirve_de_verdad_por_la_url_con_la_que_se_cachea(cliente):
    """Que el archivo exista en `static/` no quiere decir que se sirva.

    Los estáticos van montados al final, en `/`, y `/sw.js` tiene además su
    propia ruta. Un montaje mal puesto -o una ruta declarada después- deja
    `/metricas.html` devolviendo el 404 de la API, y en el móvil eso es tocar la
    pestaña de una métrica y que no pase nada.

    Se piden las URL EXACTAS de `ARMAZON`, que son las que el service worker
    mete en `addAll`. Si una de ellas no se sirve, la instalación del caché
    falla entera y la aplicación se queda sin modo sin conexión.
    """
    for ruta in _lista(SW, "ARMAZON"):
        r = cliente.get(ruta)
        assert r.status_code == 200, f"{ruta} → {r.status_code}"

    # El `Content-Type` importa tanto como el 200: un `.js` servido como
    # `text/html` no lo ejecuta el navegador, y la pantalla sale en blanco sin
    # un solo error legible desde el móvil.
    assert cliente.get("/metricas.js").headers["content-type"].startswith(
        ("text/javascript", "application/javascript")
    )
    assert cliente.get("/sw.js").headers["content-type"].startswith(
        ("text/javascript", "application/javascript")
    )


def test_todo_el_armazon_obliga_al_navegador_a_preguntar_antes_de_reusar(cliente):
    """Sin `Cache-Control`, el navegador se inventa el plazo. Y se lo inventa largo.

    Es la cabecera que no estaba y que dejó el panel nuevo invisible desde el
    navegador con el contenedor ya reconstruido. `StaticFiles` manda `ETag` y
    `Last-Modified` y nada más; sin una instrucción explícita el navegador aplica
    caducidad heurística y reutiliza la respuesta SIN preguntar. No hay 304, no
    hay petición, no hay nada que mirar.

    Y encima va el service worker, que es lo que lo vuelve grave. Su `fetch` dice
    «primero la red» en el código y en el comentario, pero `fetch()` pasa por el
    caché HTTP; con la heurística delante, «primero la red» es «primero lo
    viejo», y además guarda lo viejo en `CacheStorage`. La pantalla sale entera,
    bien pintada y de antes de ayer.

    `/sw.js` llevaba su `Cache-Control` puesto a mano desde hacía meses, con el
    motivo escrito al lado. Los otros doce ficheros del armazón, no: el
    razonamiento se quedó en el único sitio donde alguien lo pensó. Por eso esto
    se comprueba sobre `ARMAZON` ENTERO y no sobre una lista escrita aquí, que
    volvería a dejar fuera al siguiente.
    """
    for ruta in _lista(SW, "ARMAZON"):
        cc = cliente.get(ruta).headers.get("cache-control", "")
        assert cc, (
            f"`{ruta}` se sirve sin `Cache-Control`. El navegador no se queda "
            f"sin caché: se queda sin instrucción, y entonces se inventa cuánto "
            f"tiempo puede reutilizarlo sin preguntar."
        )
        # `max-age` a secas es permiso para reutilizar sin preguntar durante ese
        # rato, que es justo lo que no puede pasar con el armazón: el contenedor
        # se reconstruye y el móvil sigue con el JavaScript de antes.
        assert "no-cache" in cc or "no-store" in cc or "max-age=0" in cc, (
            f"`{ruta}` manda `Cache-Control: {cc}`, que permite reutilizarlo sin "
            f"revalidar. El armazón tiene que preguntar siempre: con `ETag` la "
            f"pregunta se contesta con un 304 sin cuerpo y sale gratis."
        )


# ---------------------------------------------------------------------------
# Las cinco vistas, pintadas de verdad
# ---------------------------------------------------------------------------

# El último día sembrado. Va anclado a HOY DE VERDAD y no a una fecha escrita,
# porque la API no sabe nada de esta constante: llama a `date.today()`. Estuvo
# clavada en el 11 de septiembre de 2026, y cuatro días después la portada ya
# pintaba sus doce líneas por la rama de "solo 3 de los últimos 7 días traen
# este dato" -el sembrado se había quedado fuera de la ventana de la semana- sin
# que nada se pusiera rojo. Una semana más y habrían sido cero días: el arnés
# habría seguido en verde pintando doce veces "no hay bastante", que es
# exactamente el vacío que este archivo existe para no dar por bueno.
HOY = date.today()
DIAS = 120

# Las dos preguntas de Sí/No, repartidas en ciclo. La lista no es decorativa:
# la tabla de discordancia tiene CUATRO casillas y una rama entera de "no hay
# bastante", y si el sembrado deja cualquiera de las cuatro a cero, el
# renderizador se pinta igual pero sin haber leído `pct` -que es `None` cuando
# la casilla está vacía- ni `discordante` en el caso que importa. Sembrando las
# cuatro, el arnés de Node lee de verdad todas las claves de cada celda.
#
# Hay también días con UNA sola contestada y días con NINGUNA, porque el tercer
# estado -"no me lo han dicho"- no es un `False` y tiene que llegar al conteo de
# `sin_las_dos`, que es el denominador que la ficha enseña.
#
# En trece días, para que el ciclo no cuadre con el 7 de la fuerza ni con el 3
# de la bici: si cuadrara, "los días que entreno" y "los días que digo que voy a
# entrenar" serían el mismo conjunto y las correlaciones saldrían perfectas por
# construcción, que es la forma más silenciosa de que un test deje de mirar.
_LAS_DOS_PREGUNTAS = [
    (True, True),  # lo corriente: apetece y va
    (True, True),
    (False, True),  # discordancia: no apetece y va igual
    (True, True),
    (True, False),  # discordancia: apetece y no va
    (False, False),  # ni apetece ni va
    (True, True),
    (None, None),  # el día que no contestó nada
    (True, True),
    (False, True),
    (True, None),  # contestó una y no la otra
    (None, False),
    (False, False),
]


def _sembrar(ses) -> None:
    """Una base con las cuatro fuentes y los dos tipos de exposición dentro.

    No vale sembrar poco. Media vista se pinta por la rama de "no hay datos", y
    lo que se persigue aquí son las claves que se leen en la OTRA rama: las
    medias por grupo de una exposición binaria, la ficha de cada ejercicio del
    ranking, la lista de activaciones de una regla especial, la tira de
    componentes de una sesión juzgada. Con la base vacía todo eso no se llega a
    tocar y el test pasaría sin haber mirado nada.

    Los números son deliberadamente regulares -senos y módulos- porque aquí no
    se comprueba ningún resultado estadístico: eso es cosa de los tests de
    `app/analysis/`. Lo único que hace falta es que las correlaciones se puedan
    calcular y que las casillas salgan con contenido.
    """
    for i in range(DIAS):
        d = HOY - timedelta(days=i)
        apetece, voy = _LAS_DOS_PREGUNTAS[i % len(_LAS_DOS_PREGUNTAS)]
        ses.add(
            DailyMetrics(
                date=d,
                fetch_status="ok",
                hrv=55.0 + (i % 7) * 1.5,
                rhr=52.0 - (i % 5) * 0.5,
                sleep_min=400 + (i % 9) * 10,
                sleep_score=70 + (i % 6) * 3,
                body_battery=60 + (i % 8) * 2,
            )
        )
        ses.add(
            Checkin(
                date=d,
                fatigue=3 + (i % 5),
                mood=4 + (i % 4),
                upper_discomfort=i % 3,
                lower_discomfort=(i + 1) % 4,
                sleep_quality=5 + (i % 4),
                training_desire=4 + (i % 5),
                yesterday_rpe=5 + (i % 4),
                wants_to_train=apetece,
                will_train=voy,
            )
        )

        # Una salida de bici cada tres días: la exposición BINARIA -salí o no
        # salí- que parte los días en dos grupos y hace que el backend mande
        # `media_expuesto` y `media_no_expuesto`.
        if i % 3 == 0:
            ses.add(
                Activity(
                    garmin_activity_id=90000 + i,
                    date=d,
                    name="Salida",
                    is_cycling=True,
                    duration_s=3600.0 + (i % 4) * 300,
                    moving_duration_s=3400.0 + (i % 4) * 300,
                    distance_m=30000.0 + (i % 5) * 2000,
                    elevation_gain_m=200.0 + (i % 6) * 40,
                    avg_hr=130.0 + (i % 7),
                    training_load=90.0 + (i % 9) * 5,
                    intensity_level="medium",
                    classification_source="garmin",
                )
            )

        # Fuerza dos días de cada siete. El volumen es la exposición CONTINUA:
        # no hay "los días con esto" porque todos tienen un poco.
        if i % 7 in (1, 4):
            ses.add(
                WorkoutLog(
                    hevy_workout_id=f"w{i}",
                    date=d,
                    routine_key="empuje",
                    title="Empuje",
                    duration_s=3000 + (i % 5) * 120,
                    total_sets=18 + (i % 4),
                    total_volume_kg=4000.0 + (i % 11) * 150,
                    all_sets_at_target=(i % 3 != 0),
                )
            )

        # El semáforo. Un día de cada once sin fila, para que la vista tenga que
        # pintar también el "ese día no hubo decisión" y no solo colores.
        if i % 11 != 5:
            luz = ["green", "green", "amber", "green", "red"][i % 5]
            prog = {
                "routine": "empuje",
                "gate_open": i % 4 != 2,
                "gate_reason": "ámbar: no se sube carga" if i % 4 == 2 else None,
                "sets_allowed": i % 6 != 3,
                "sets_reason": "molestia lumbar por encima del umbral" if i % 6 == 3 else None,
                "reps_allowed": True,
                "reps_reason": None,
                "exercises": [
                    {
                        "key": "press_banca",
                        "prescribed_kg": 60.0 + (i // 20) * 2.5,
                        "milestone": "up" if i % 17 == 0 else None,
                        "blocked_by": "ámbar" if i % 4 == 2 else None,
                    }
                ],
            }
            ses.add(
                Decision(
                    date=d,
                    light=luz,
                    trigger_rule="hrv_baja" if luz != "green" else None,
                    fired_rules_json=json.dumps(["hrv_baja"] if luz != "green" else []),
                    skipped_rules_json=json.dumps([]),
                    inputs_snapshot_json=json.dumps(
                        {"fatigue": 3 + (i % 5), "mood": 4 + (i % 4)}
                    ),
                    # Dos hashes distintos: la vista tiene que poder pintar la
                    # recalibración, que es una fila con forma propia.
                    config_hash="aaaa1111" if i < DIAS // 2 else "bbbb2222",
                    source="checkin",
                    is_current=True,
                    planned_session_json=json.dumps({"routine": "empuje"}),
                    progression_json=json.dumps(prog),
                )
            )

        # Sesiones ya juzgadas, que es sobre lo que trabaja la vista 5. Una de
        # cada once disociada, que es la frecuencia que se pidió.
        if i % 7 in (1, 4) or i % 3 == 0:
            fuerza = i % 7 in (1, 4)
            per = 20.0 + (i % 9) * 7
            rend = 55.0 + (i % 8) * 5
            disociada = i % 11 == 1
            ses.add(
                SessionPerformance(
                    date=d,
                    kind="strength" if fuerza else "bike",
                    source_key=f"{'s' if fuerza else 'b'}:{d.isoformat()}",
                    routine_key="empuje" if fuerza else None,
                    garmin_activity_id=None if fuerza else 90000 + i,
                    perceived_fatigue=3 + (i % 5),
                    perceived_mood=4 + (i % 4),
                    perception_index=2.0 + (i % 6) * 0.5,
                    perception_pct=15.0 if disociada else per,
                    comp_compliance=80.0 + (i % 5) * 4 if fuerza else None,
                    comp_progression=50.0 if fuerza else None,
                    comp_rpe=60.0 + (i % 4) * 5 if fuerza else None,
                    comp_bike_hr=None if fuerza else 65.0 + (i % 6) * 4,
                    comp_bike_speed=None if fuerza else 60.0 + (i % 7) * 4,
                    comp_bike_elevation=None if fuerza else 40.0 + (i % 5) * 6,
                    performance_index=70.0,
                    performance_pct=80.0 if disociada else rend,
                    gap_pct=65.0 if disociada else round(per - rend, 2),
                    direction="perception_worse" if per < rend else "perception_better",
                    dissociation=disociada,
                    n_sessions_base=25,
                )
            )

    # Una regla retirada y otra viva: la vista de auditoría pinta la lista de
    # activaciones de cada una, con su `desde`, su `hasta` y su motivo.
    ses.add(
        RuleState(
            rule_name="molestia_lumbar",
            entity="peso_muerto_smith",
            active_from=HOY - timedelta(days=40),
            active_until=HOY - timedelta(days=12),
            reason="molestia lumbar tres días seguidos",
            notify=True,
        )
    )
    ses.add(
        RuleState(
            rule_name="molestia_lumbar",
            entity="*",
            active_from=HOY - timedelta(days=6),
            active_until=None,
            reason="molestia lumbar por encima del umbral",
            notify=True,
        )
    )
    ses.commit()


def _payloads(cliente) -> dict[str, object]:
    """Las ocho respuestas de verdad, tal cual las recibe el móvil."""
    rutas = {
        "portada": ("/api/metrics/portada", {}),
        "concordancia": ("/api/metrics/concordancia", {}),
        "desfase": ("/api/metrics/desfase", {}),
        "impacto": ("/api/metrics/impacto", {}),
        "auditoria": ("/api/metrics/auditoria", {}),
        "percepcion": ("/api/metrics/percepcion", {}),
        "umbral": ("/api/metrics/umbral", {}),
    }
    salida = {}
    for nombre, (url, extra) in rutas.items():
        r = cliente.get(url, params={"dias": DIAS, **extra})
        assert r.status_code == 200, f"{url} → {r.status_code} {r.text[:400]}"
        salida[nombre] = r.json()

    # El ranking va aparte porque NO se pide por su cuenta: el cliente lo pide
    # para la respuesta con la que la vista de impacto ha abierto, y esa la
    # decide el servidor. Clavarla aquí -estaba clavada a `lower_discomfort`, que
    # no tiene ni un día- serviría en el andamio un payload que el cliente de
    # verdad nunca pediría, y el test pasaría sin haber pintado el ranking que se
    # ve en el móvil.
    respuesta = salida["impacto"]["respuesta_por_defecto"]
    assert respuesta, (
        "la vista de impacto no trae `respuesta_por_defecto` con la base "
        "sembrada: o el sembrado no llena ni una casilla, o el servidor ha "
        "dejado de mandarla y el desplegable volverá a abrir en la primera"
    )
    r = cliente.get(
        "/api/metrics/ranking-ejercicios",
        params={"dias": DIAS, "respuesta": respuesta},
    )
    assert r.status_code == 200, f"ranking → {r.status_code} {r.text[:400]}"
    salida["ranking"] = r.json()

    # LA LÍNEA DE LA DISCORDANCIA, POR LA RAMA BUENA.
    #
    # La línea puede traer su tabla y salir igualmente con `na` -"solo 3 de los
    # últimos 7 días traen este dato"-, y el `na` manda en el renderizador: se
    # pinta el motivo y poco más. Eso es lo que pasaba con `HOY` clavado a una
    # fecha escrita, y mirar solo la tabla no lo habría visto.
    linea = _linea_de_la_portada(salida["portada"])
    assert linea and not linea["na"], (
        f"la línea de discordancia de la portada sale por la rama de «no hay "
        f"bastante» ({(linea or {}).get('na')!r}). Casi siempre significa que "
        f"el sembrado ha quedado fuera de la ventana de la última semana, y "
        f"entonces la portada entera se pinta sin un solo número y este test "
        f"la da por buena."
    )

    # LA TABLA DE LAS CUATRO CASILLAS, CON LAS CUATRO LLENAS.
    #
    # Mismo motivo que el `assert` del ranking, y descubierto igual de tarde: el
    # arnés pasaba en verde con el sembrado que no contestaba las dos preguntas,
    # porque entonces `tabla` llega con `na` puesto, el renderizador se va por la
    # rama corta y `celdas` no se llega a leer. Una vista que pinta "no hay
    # bastante" supera todas las comprobaciones de `render_pwa.mjs` sin haber
    # mirado ni una de las claves que se quieren vigilar.
    #
    # Se mira aquí, en el payload, y no en el HTML: si el sembrado deja de llenar
    # las casillas, lo que hay que arreglar es el sembrado, y el mensaje tiene
    # que decir eso y no "falta una palabra en la pantalla".
    for donde, tabla in (
        ("portada", _tabla_de_la_portada(salida["portada"])),
        ("concordancia", _tabla_de_la_vista(salida["concordancia"])),
    ):
        assert tabla is not None, (
            f"`{donde}` no trae la tabla de discordancia: o el sembrado no "
            f"contesta las dos preguntas, o el servidor ha dejado de mandarla"
        )
        assert not tabla["na"], (
            f"la tabla de `{donde}` viene por la rama de «no hay bastante» "
            f"({tabla['na']!r}). Así el renderizador no lee ni `celdas` ni "
            f"`pct` ni `discordante`, y este test aprueba sin haberlos mirado."
        )
        vacias = [c["etiqueta"] for c in tabla["celdas"] if not c["n"]]
        assert not vacias, (
            f"en `{donde}` hay casillas a cero: {vacias}. El sembrado tiene "
            f"que llenar las cuatro, porque la casilla vacía manda `pct` a "
            f"`None` y es otra rama distinta de la que se persigue."
        )

    # LA PANTALLA DEL UMBRAL, TAMBIÉN POR LA RAMA BUENA.
    #
    # El mismo argumento de arriba, y ésta es la vista más expuesta a él:
    # `pintarUmbral` tiene cuatro sitios por los que puede irse por el camino
    # corto -la tabla de tramos, la frontera, cada curva y la gráfica-, y cada
    # uno se lleva por delante un puñado de claves sin que nadie se entere. Con
    # un sembrado sin bici esta pantalla pinta cuatro motivos escritos, no se
    # lee ni un número, y el arnés la daría por buena.
    u = salida["umbral"]
    assert not u["umbral"]["na"], (
        f"los tramos del umbral vienen por la rama de «no hay bastante» "
        f"({u['umbral']['na']!r}): el sembrado no tiene salidas de bici con HRV "
        f"la mañana de antes y la de después."
    )
    assert u["umbral"]["frontera"]["carga"] is not None, (
        f"la frontera no encuentra corte ({u['umbral']['frontera']['na']!r}). "
        f"Sin corte no se leen ni `vecinos`, ni `miradas`, ni la lectura, ni el "
        f"escalón, que son media pantalla."
    )
    llenas = [c for c in u["recuperacion"]["curvas"] if not c["na"]]
    assert llenas, (
        "las dos curvas de recuperación vienen con el motivo escrito: el "
        "sembrado no deja ni una salida aislada. Así no se pinta una sola barra "
        "y no se leen ni `por_dia`, ni `escala`, ni `vuelve_el_dia`."
    )
    assert not u["grafica"]["na"], (
        f"la gráfica del umbral no se dibuja ({u['grafica']['na']!r}), así que "
        f"no se leen ni `puntos`, ni `salidas`, ni `altura_corte`."
    )
    return salida


def _linea_de_la_portada(payload: dict) -> dict | None:
    """La línea de «cómo voy» que lleva la tabla de discordancia, o `None`."""
    for linea in payload["como_voy"]["lineas"]:
        if linea.get("tabla"):
            return linea
    return None


def _tabla_de_la_portada(payload: dict) -> dict | None:
    """La tabla de discordancia dentro de «cómo voy», o `None`."""
    linea = _linea_de_la_portada(payload)
    return linea["tabla"] if linea else None


def _tabla_de_la_vista(payload: dict) -> dict | None:
    """La tabla de discordancia dentro de la lista de series, o `None`."""
    for serie in payload["series"]:
        if serie.get("tabla"):
            return serie["tabla"]
    return None


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_los_renderizadores_no_leen_ni_una_clave_que_el_backend_no_mande(
    base, cliente, tmp_path
):
    """Las cinco vistas, pintadas en Node contra los payloads de verdad.

    Este es el único test del repositorio que ejecuta el JavaScript de la PWA, y
    existe por un motivo muy concreto: en JavaScript, leer una clave que no
    existe NO da ningún error. Da `undefined`, y a partir de ahí hay dos
    finales, los dos malos:

      - se concatena en una frase y la pantalla dice "hecho undefined día(s)",
        que desde el móvil parece parte del texto;
      - se lee dentro de un `if`, no deja ni rastro, y la vista elige la rama
        contraria. Así estuvo escribiendo "Sin ningún dato en esta ventana de:
        check-ins, datos de Garmin, salidas de bici, entrenos de fuerza" encima
        de ciento setenta y nueve días de Garmin, con una frase perfecta, sin un
        solo error, y justo en la línea que existe para decir cómo hay que leer
        todo lo demás.

    El segundo final es el que importa y es el que ninguna búsqueda de texto
    puede encontrar. Por eso el andamio envuelve cada objeto del payload en un
    `Proxy` que apunta las lecturas de claves ausentes: no busca el síntoma,
    busca el acto.

    Se salta -no falla- si no hay `node`. La batería tiene que poder correr en
    una máquina sin él; lo que no puede es pasar en silencio habiéndose saltado
    la comprobación, y de eso se encarga el motivo del `skip`.
    """
    _sembrar(base)
    payloads = tmp_path / "payloads.json"
    payloads.write_text(
        json.dumps(_payloads(cliente), ensure_ascii=False), encoding="utf-8"
    )

    r = subprocess.run(
        ["node", "tests/render_pwa.mjs", str(payloads)],
        cwd=RAIZ,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert r.returncode == 0, (
        f"la PWA no pinta limpio contra los payloads de verdad:\n"
        f"{r.stdout}\n{r.stderr}"
    )
    # QUE SE HAYAN PINTADO TODAS, y "todas" se cuenta desde `metricas.js`.
    #
    # Aquí ponía un 6 escrito a mano, y ese 6 tenía que subir a mano cada vez que
    # se añadía una vista. Lo que protege este `assert` es que el andamio no se
    # calle habiéndoselas saltado, y con el número escrito el fallo más probable
    # -añadir la vista a `metricas.js` y olvidarla en la lista `VISTAS` de
    # `render_pwa.mjs`- dejaba el andamio pintando las de siempre y este test en
    # verde, porque el 6 tampoco se había tocado. Dos listas escritas a mano
    # comparadas contra un número escrito a mano no comprueban nada.
    #
    # Contando las claves reales de `VISTAS` en `metricas.js`, el olvido en el
    # andamio se pone rojo solo.
    cuantas = len(_claves_de_objeto(METRICAS, "VISTAS"))
    assert cuantas, "no encuentro las claves de VISTAS en metricas.js"
    assert r.stdout.count("ok ") == cuantas, (
        f"`metricas.js` tiene {cuantas} vistas y el andamio solo ha pintado "
        f"{r.stdout.count('ok ')}. Casi siempre es una vista nueva que falta en "
        f"la lista `VISTAS` de `tests/render_pwa.mjs`:\n{r.stdout}"
    )


def test_las_pantallas_de_la_nav_llevan_a_algo_que_existe():
    """Un enlace de la barra que no lleva a ninguna vista es un callejón.

    La barra se pinta desde `PANTALLAS` en `comun.js` y las vistas viven en
    `VISTAS` en `metricas.js`: dos listas en dos archivos que tienen que decir
    lo mismo. Sin esto, un enlace roto cae en la vista por defecto y la barra
    marca como activa una pestaña que no es la que se ve.
    """
    pantallas = re.findall(r'href: "([^"]+)"', COMUN)
    vistas = set(_claves_de_objeto(METRICAS, "VISTAS"))
    assert vistas, "no encuentro las claves de VISTAS en metricas.js"

    # Una por vista, más el check-in. El número NO se escribe: se cuenta desde
    # `VISTAS`, que es la lista de la que depende de verdad. Escrito -y estuvo
    # escrito- había que subirlo a mano con cada pantalla nueva, y un test que
    # hay que editar para que siga pasando acaba editándose sin mirar qué decía.
    assert len(pantallas) == len(vistas) + 1, (
        f"la barra tiene {len(pantallas)} enlaces y `VISTAS` tiene "
        f"{len(vistas)} vistas más el check-in: {pantallas}"
    )
    assert "/" in pantallas, (
        "la barra ha perdido el enlace al check-in, que es la única pantalla "
        "que no es una métrica y la que se abre todas las mañanas"
    )

    for href in pantallas:
        if href == "/":
            assert (ESTATICOS / "index.html").is_file()
            continue
        pagina, _, ancla = href.partition("#")
        assert (ESTATICOS / pagina.lstrip("/")).is_file(), f"{pagina} no existe"
        assert ancla in vistas, (
            f"la barra enlaza a `{href}` y `{ancla}` no está en VISTAS: ese "
            f"toque cae en la vista por defecto sin decir nada"
        )

    assert vistas <= {a.partition("#")[2] for a in pantallas}, (
        "hay vistas en metricas.js a las que no llega ningún enlace de la barra"
    )


# ---------------------------------------------------------------------------
# La frontera del cálculo
# ---------------------------------------------------------------------------

# Lo que no puede aparecer en el JavaScript que pinta números.
#
# La regla es del usuario y es una sola: todo el cálculo en el backend, la PWA
# solo pinta. El motivo no es de estilo. Un estadístico calculado en el móvil
# no tiene tests, no tiene n, no tiene ventana y no puede decir por qué no se
# pudo calcular: pintaría un número sin poder distinguirlo de un cero.
#
# Multiplicar y dividir sí se puede: pasar un `r` de −1 a 1 a un ancho en
# píxeles es una cuenta de dibujo, no una cuenta de estadística.
#
# Se buscan LLAMADAS, no palabras. "percentil" y "mediana" salen en pantalla
# constantemente -son las etiquetas de los números que manda el servidor, y hay
# un `aria-label="percentil esperado frente a percentil hecho"`-, así que
# buscar la palabra suelta marcaría como cálculo el texto que describe el
# cálculo de otro. Lo que delata a un estadístico hecho aquí es el paréntesis.
PROHIBIDO = [
    (r"Math\.sqrt\s*\(", "una raíz es el final de una desviación típica"),
    (r"Math\.log\s*\(", "un logaritmo es una transformación de los datos"),
    (r"Math\.exp\s*\(", "un exponencial es una transformación de los datos"),
    (r"Math\.pow\s*\(", "una potencia es una transformación de los datos"),
    (r"\bspearman\s*\(", "la correlación la calcula el servidor"),
    (r"\bpearson\s*\(", "la correlación la calcula el servidor"),
    (r"\bpercentil\w*\s*\(", "los percentiles los calcula el servidor"),
    (r"\bmediana\s*\(", "la mediana la calcula el servidor"),
    (r"\bpromedio\s*\(", "la media la calcula el servidor"),
    (r"\bdesviacion\s*\(", "la desviación la calcula el servidor"),
    (r"\.reduce\s*\(", "sumar una lista en el móvil es calcular en el móvil"),
]


@pytest.mark.parametrize("archivo", ["metricas.js", "graficos.js", "comun.js"])
def test_la_pwa_no_calcula_estadistica(archivo):
    """Ningún número que se lee sale de una cuenta hecha aquí.

    Un `Math.sqrt` en `metricas.js` no da ningún error y pinta un número que
    parece igual de sólido que los demás. Este test es la única forma de que la
    frontera se note al cruzarla.
    """
    # Los comentarios hablan de estadística todo el rato; lo que se vigila es
    # el código.
    codigo = _sin_comentarios((ESTATICOS / archivo).read_text(encoding="utf-8"))

    for patron, motivo in PROHIBIDO:
        encontrado = re.search(patron, codigo)
        assert encontrado is None, (
            f"`{encontrado.group(0)}` en static/{archivo}: {motivo}. El cálculo "
            f"va en el backend, donde tiene tests, n y ventana."
        )


def test_los_html_cargan_comun_antes_que_lo_que_lo_usa():
    """El orden de los `<script>`, que no se ve hasta que se rompe.

    `comun.js` define `$`, `escapar`, `fechaLarga` y `pintarNav`, que usan los
    otros dos. Cargado después, lo que se ve es media pantalla dibujada y
    ningún error visible desde el móvil. Por eso los dos archivos comprueban
    además en tiempo de ejecución que `comun.js` esté, pero mejor no llegar.
    """
    for pagina, propio in [("index.html", "app.js"), ("metricas.html", "metricas.js")]:
        html = (ESTATICOS / pagina).read_text(encoding="utf-8")
        assert html.index("/comun.js") < html.index(f"/{propio}"), (
            f"{pagina} carga {propio} antes que comun.js"
        )


def test_los_dos_html_tienen_donde_pintar_la_barra():
    """La barra se pinta desde JavaScript sobre un hueco que tiene que existir.

    `pintarNav` sale sin hacer nada si no encuentra el `#nav`. Es lo correcto
    -no reventar la pantalla por una barra- pero significa que olvidarse del
    hueco en un HTML deja esa página sin salida y sin ninguna queja.
    """
    for pagina in ["index.html", "metricas.html"]:
        html = (ESTATICOS / pagina).read_text(encoding="utf-8")
        assert 'id="nav"' in html, f"{pagina} no tiene dónde pintar la barra"
        assert "con-nav" in html, (
            f"{pagina} no lleva `con-nav` en el body: la barra es fija y taparía "
            f"lo último de la página"
        )


def test_no_hay_dos_copias_del_escape_de_html():
    """`escapar` vive en `comun.js` y en ningún sitio más.

    Estaba duplicada letra por letra en `app.js`. Dos copias de un escape de
    HTML es de las peores cosas que se pueden duplicar: el día que aparezca un
    carácter que se cuela se arregla una y la otra se queda rota, sin que nada
    lo diga.
    """
    definiciones = [
        a for a in ["comun.js", "app.js", "metricas.js", "graficos.js"]
        if re.search(
            r"^function escapar\b", (ESTATICOS / a).read_text("utf-8"), re.M
        )
    ]
    assert definiciones == ["comun.js"], (
        f"`escapar` está definida en {definiciones}; tiene que estar solo en "
        f"comun.js"
    )


# ---------------------------------------------------------------------------
# Las dos preguntas de Sí/No, rellenadas de verdad
# ---------------------------------------------------------------------------
#
# Lo que se persigue en este bloque entero es UNA sola cosa: que el tercer estado
# sobreviva al formulario.
#
# El motor distingue `True`, `False` y `None`, la base guarda la columna nulable
# y el mensaje del día mira `is not False` precisamente para no confundir "he
# dicho que no" con "no me lo han dicho". Todo eso está probado en Python y todo
# eso da igual si la pantalla que GENERA el dato aplasta los tres estados en dos.
#
# Y aplastarlos es de lo más fácil que hay aquí, porque JavaScript lo hace solo y
# en las dos direcciones: `Number(false)` es 0 -que en un deslizador es un
# extremo del rango, no un hueco- y `Boolean(0)` es `false` -que en una pregunta
# es una respuesta-. Ninguna de las dos conversiones da error, ninguna deja
# rastro, y las dos producen un check-in impecable con una respuesta que nadie
# dio. Contra eso no vale leer el archivo: hay que pulsar el botón y mirar el
# JSON que sale por el cable, y eso es lo que hace `tests/checkin_pwa.mjs`.


def _rellenar(tmp_path, hoy: dict, acciones: list[dict], borrador=None) -> dict:
    """Abre el formulario contra un `/api/checkin/today` de mentira y lo rellena.

    Devuelve lo que quedó en pantalla y, sobre todo, `cuerpo`: el JSON EXACTO
    del POST, o `None` si no llegó a salir.

    `borrador` siembra el `localStorage` antes de abrir, que es como se abre una
    pantalla por segunda vez después de un envío que no salió. Cada llamada es un
    proceso de `node` nuevo y no recuerda nada de la anterior: eso es a propósito
    -un test que dependiera del orden de ejecución de los otros sería peor que
    no tenerlo- y por eso el estado previo se pasa explícitamente.
    """
    guion = tmp_path / "guion.json"
    guion.write_text(
        json.dumps(
            {"hoy": hoy, "acciones": acciones, "borrador": borrador},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    r = subprocess.run(
        ["node", "tests/checkin_pwa.mjs", str(guion)],
        cwd=RAIZ, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert r.returncode == 0, f"el arnés no terminó:\n{r.stdout}\n{r.stderr}"
    # `app.js` escribe por consola al cargarse -el aviso del service worker-, así
    # que el JSON es la ÚLTIMA línea.
    return json.loads(r.stdout.strip().splitlines()[-1])


def _hoy(**cambios) -> dict:
    """Un `/api/checkin/today` con la forma que manda el endpoint de verdad."""
    base = {
        "day": "2026-09-15",
        "submitted": False,
        "values": {},
        "comments": None,
        "sliders": [
            {"key": "fatigue", "label": "Fatiga", "hint_low": "ninguna",
             "hint_high": "mucha"},
            {"key": "yesterday_rpe", "label": "Esfuerzo de ayer", "optional": True},
        ],
        "preguntas": [
            {"key": "wants_to_train", "label": "¿Te apetece entrenar hoy?"},
            {"key": "will_train", "label": "¿Vas a entrenar hoy?"},
        ],
        "selector": _selector(),
        "comment_label": "Comentarios",
    }
    base.update(cambios)
    return base


def _selector(propuesta: str = "dia_3", **parados) -> dict:
    """El `selector` de `/api/checkin/today`, con la forma que manda el endpoint.

    `parados` marca opciones como paradas: `_selector(dia_1=7)` es el Día 1 siete
    sesiones sin hacerse. El endpoint de verdad ya manda `pendiente: null` en las
    caducadas -filtra igual que el mensaje de la mañana-, así que aquí una opción
    sin número es a la vez "está al día" y "lleva tanto que ya no se nombra". Que
    la pantalla no distinga las dos es justo lo que se quiere: el encargo dice
    que al caducar cambie la línea del mensaje, no lo que se puede elegir.
    """
    def opcion(clave: str, etiqueta: str, fuerza: bool) -> dict:
        n = parados.get(clave)
        return {
            "key": clave,
            "label": etiqueta,
            "es_fuerza": fuerza,
            "pendiente": n,
            "ultima_vez": "2026-08-20" if n else None,
        }

    return {
        "key": "chosen_session",
        "label": "¿Qué vas a hacer hoy?",
        "nota": "Lo que de verdad cuenta es lo que registres en Hevy.",
        "propuesta": propuesta,
        "opciones": [
            opcion("dia_1", "Día 1 · Empuje", True),
            opcion("dia_2", "Día 2 · Tirón", True),
            opcion("dia_3", "Día 3 · Pierna", True),
            opcion("bici", "Bici", False),
            opcion("otro", "Otro", False),
        ],
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_un_no_viaja_como_false_y_no_como_cero(tmp_path):
    """EL TEST DE TODO ESTO. Un «No» pulsado llega al servidor como `false`.

    `fijar()` hacía -y el resto del archivo sigue haciendo, para los
    deslizadores- `estado.valores[key] = Number(valor)`. Metida una pregunta por
    ese camino, un "No" se habría enviado como `0`.

    Y `0` no explota en ningún sitio. Pydantic lo acepta en un `bool | None` y lo
    convierte de vuelta a `False`, la columna lo guarda, el motor lo lee como un
    "no" y el mensaje se calla la sesión. O sea: hoy habría funcionado. Lo que se
    habría roto es el día que alguien mire la columna para contar discordancias y
    se encuentre ceros y unos donde debería haber tres estados, sin forma de
    saber cuáles vinieron de un dedo y cuáles de un `Number()`.

    Se comprueba con `is` y no con `==` a propósito: en Python `0 == False` es
    verdadero, así que un `assert cuerpo["will_train"] == False` daría verde con
    el fallo puesto. Este test tiene que mirar el TIPO.
    """
    salida = _rellenar(tmp_path, _hoy(), [
        {"tipo": "deslizar", "key": "fatigue", "valor": 3},
        {"tipo": "responder", "key": "wants_to_train", "respuesta": "si"},
        {"tipo": "responder", "key": "will_train", "respuesta": "no"},
        {"tipo": "enviar"},
    ])

    cuerpo = salida["cuerpo"]
    assert cuerpo is not None, "el formulario no llegó a enviarse"
    assert cuerpo["will_train"] is False, (
        f"un «No» ha salido como {cuerpo['will_train']!r}. Si es 0, alguien ha "
        f"metido las preguntas por el camino de los deslizadores."
    )
    assert cuerpo["wants_to_train"] is True
    assert cuerpo["fatigue"] == 3 and isinstance(cuerpo["fatigue"], int)

    # Y que se VEA lo contestado, que es la otra mitad. Un estado interno
    # correcto con los dos botones en blanco es un formulario que ha decidido
    # por su cuenta y no lo enseña.
    assert salida["elegidas"] == {"wants_to_train": "si", "will_train": "no"}
    assert salida["aria"]["will_train.no"] == "true"
    assert salida["aria"]["will_train.si"] == "false"


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_una_pregunta_sin_tocar_no_viaja_de_ninguna_manera(tmp_path):
    """El tercer estado se manda no mandando nada, y mientras tanto no se envía.

    Es la misma regla que ya tenían los deslizadores -"uno que nadie ha tocado no
    vale 5, vale nada"- aplicada a las preguntas. Con una casilla de verificación
    esto sería imposible de escribir: la posición "sin marcar" tendría que
    significar a la vez "no" y "no lo he mirado", y el `false` inventado saldría
    hacia el servidor sin que nada pudiera distinguirlo después.
    """
    salida = _rellenar(tmp_path, _hoy(), [
        {"tipo": "deslizar", "key": "fatigue", "valor": 5},
    ])

    assert salida["cuerpo"] is None and salida["veces_enviado"] == 0
    assert salida["enviar_deshabilitado"] is True
    assert salida["valores"] == '{"fatigue":5}', (
        f"algo se ha colado en los valores sin haberlo contestado: "
        f"{salida['valores']}"
    )
    assert salida["sin_contestar"] == ["wants_to_train", "will_train"]
    assert salida["elegidas"] == {"wants_to_train": None, "will_train": None}
    # Y con nombre y apellidos, que es la regla de esta pantalla: un botón gris
    # sin explicación es un callejón sin salida.
    assert "¿Te apetece entrenar hoy?" in salida["faltan"]
    assert "¿Vas a entrenar hoy?" in salida["faltan"]


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_un_cero_de_un_deslizador_si_viaja(tmp_path):
    """El control del test de arriba, y no sobra.

    Sin él, "la clave no está en el cuerpo" quedaría demostrado para el caso
    falso sin haber demostrado nunca que el formulario sabe mandar un valor
    falso. Un `if (estado.valores[s.key])` en vez de un `=== undefined` haría
    pasar el test anterior y tiraría a la basura todos los ceros: "ninguna
    molestia lumbar", que es el dato con el que se levanta un freno.
    """
    salida = _rellenar(tmp_path, _hoy(), [
        {"tipo": "deslizar", "key": "fatigue", "valor": 0},
        {"tipo": "responder", "key": "wants_to_train", "respuesta": "no"},
        {"tipo": "responder", "key": "will_train", "respuesta": "no"},
        {"tipo": "enviar"},
    ])

    cuerpo = salida["cuerpo"]
    assert cuerpo["fatigue"] == 0, "un 0 contestado se ha perdido por el camino"
    assert cuerpo["wants_to_train"] is False and cuerpo["will_train"] is False
    assert salida["enviar_deshabilitado"] is False


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_la_pantalla_pregunta_lo_que_diga_el_config_y_no_lo_que_lleve_escrito(tmp_path):
    """Ni una pregunta escrita a mano en el JavaScript.

    Es la misma regla que ya regía para los deslizadores y por el mismo motivo:
    con la lista escrita en la pantalla, añadir una pregunta al `config.yaml` la
    dejaría fuera del formulario y el sistema decidiría sin ese dato sin que
    nadie lo notara.

    Se sirven TRES, y una de ellas no existe en el proyecto. Una pantalla con el
    par escrito a mano pintaría dos y aprobaría todo lo demás.
    """
    hoy = _hoy(preguntas=[
        {"key": "wants_to_train", "label": "¿Te apetece entrenar hoy?"},
        {"key": "will_train", "label": "¿Vas a entrenar hoy?"},
        {"key": "pregunta_inventada", "label": "¿Una que no existe?",
         "nota": "con su nota debajo"},
    ])
    salida = _rellenar(tmp_path, hoy, [])

    assert salida["preguntas"] == [
        "wants_to_train", "will_train", "pregunta_inventada"
    ], "la pantalla no pinta las preguntas que le manda el servidor"
    assert "¿Una que no existe?" in salida["html_preguntas"]
    assert "con su nota debajo" in salida["html_preguntas"]

    # Y NO por el camino de los deslizadores. Un `<input type=range>` de 0 a 10
    # para «¿Vas a entrenar hoy?» se contestaría con un número, el backend lo
    # rechazaría con un 422 y desde el móvil eso es "el servidor ha rechazado el
    # check-in" sin más pistas.
    assert salida["rangos_en_preguntas"] == 0
    assert "pregunta_inventada" not in salida["deslizadores"]


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_lo_ya_contestado_hoy_vuelve_a_la_pantalla_con_los_tres_estados(tmp_path):
    """Reabrir el formulario después de haberlo enviado no cambia la respuesta.

    `recuperar()` hacía `if (v !== null && v !== undefined) fijar(k, v)`, que ya
    era correcto para `false` -es la comprobación explícita, no una de
    veracidad-. Lo que faltaba era que `fijar` supiera repartir. Sin eso, un
    `will_train: false` recuperado habría buscado un deslizador con esa clave, no
    lo habría encontrado y se habría ido por la salida de "una clave que ya no
    está en el config": la pregunta se quedaría en blanco y el siguiente envío la
    mandaría sin contestar, borrando la respuesta de esta mañana.

    El `wants_to_train: null` del payload es el tercer estado viniendo del
    servidor, y tiene que llegar SIN CONTESTAR, no como un "no".
    """
    hoy = _hoy(
        submitted=True,
        values={"fatigue": 4, "wants_to_train": None, "will_train": False},
        comments="dormí fatal",
    )
    salida = _rellenar(tmp_path, hoy, [])

    assert salida["elegidas"] == {"wants_to_train": None, "will_train": "no"}
    assert salida["sin_contestar"] == ["wants_to_train"]
    # Y el botón sigue gris, porque de verdad falta una respuesta.
    assert salida["enviar_deshabilitado"] is True
    assert "¿Te apetece entrenar hoy?" in salida["faltan"]


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_el_borrador_del_movil_guarda_el_no_como_no(tmp_path):
    """Lo que se escribió y no llegó a salir, incluidas las preguntas.

    El borrador es la red de la pantalla para el envío que no llega, y guarda
    `estado.valores` entero. Que un booleano sobreviva a `JSON.stringify` y a
    `JSON.parse` no es gratis por el hecho de ser JSON: lo que lo decide es que
    `fijar()` no lo pase por `Number()` al recuperarlo, que es exactamente el
    mismo fallo de antes en el único sitio donde nadie lo estaría mirando.
    """
    salida = _rellenar(tmp_path, _hoy(), [
        {"tipo": "deslizar", "key": "fatigue", "valor": 2},
        {"tipo": "responder", "key": "will_train", "respuesta": "no"},
    ])

    borrador = salida["borrador"]
    assert borrador is not None, "no se ha guardado nada en el móvil"
    assert borrador["day"] == "2026-09-15", (
        "el borrador se marca con el día DEL SERVIDOR; con el del móvil, un "
        "check-in de madrugada no casaría al recargar y se perdería en silencio"
    )
    assert borrador["valores"]["will_train"] is False
    # Y el deslizador que se movió también, que es la otra mitad del borrador: si
    # solo se guardaran las preguntas, volver a abrir la pantalla dejaría la
    # fatiga en blanco al lado de un "No" recordado, y eso se lee como que la
    # pantalla se ha inventado la mitad de lo que muestra.
    assert borrador["valores"]["fatigue"] == 2
    # Y la que no se tocó no está, ni como `false` ni como `null`.
    assert "wants_to_train" not in borrador["valores"]

    # LA VUELTA, que es la mitad que importa: ese mismo borrador, abierto en una
    # pantalla nueva. Aquí es donde `recuperar()` vuelve a llamar a `fijar()`, y
    # donde un `Number()` mal puesto convertiría el "No" guardado en un hueco.
    vuelta = _rellenar(tmp_path, _hoy(), [], borrador=borrador)
    assert vuelta["elegidas"] == {"wants_to_train": None, "will_train": "no"}
    assert vuelta["valores"] == '{"fatigue":2,"will_train":false}'
    # Sigue faltando una, así que sigue sin poder enviarse.
    assert vuelta["enviar_deshabilitado"] is True


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_mover_un_deslizador_y_nada_mas_ya_guarda_el_borrador(tmp_path):
    """Sin contestar ninguna pregunta, que es lo que lo hace un test distinto.

    El de arriba mueve la fatiga Y contesta una pregunta, y el borrador se
    guarda igual porque el clic de la pregunta salva `estado.valores` ENTERO.
    Así que ese test pasa aunque el deslizador no guarde nada por su cuenta:
    quitarle el `guardarBorrador()` al deslizador no ponía rojo a nadie, y eso
    lo dijo la batería de mutación, no la lectura del archivo.

    No es de esta tanda -el borrador de los deslizadores lleva aquí desde el
    principio-, pero taparlo sería escoger no saberlo. Lo que está en juego es
    el caso normal: se abre el formulario, se mueven los tres deslizadores, se
    sale de la app a mirar otra cosa y se vuelve. Media pantalla recordada y
    media en blanco es igual de mala que ninguna, y además desconcierta más.
    """
    salida = _rellenar(tmp_path, _hoy(), [
        {"tipo": "deslizar", "key": "fatigue", "valor": 7},
    ])
    borrador = salida["borrador"]
    assert borrador is not None, (
        "mover un deslizador no ha guardado nada en el móvil"
    )
    assert borrador["valores"] == {"fatigue": 7}


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_un_valor_guardado_del_tipo_que_no_es_no_pinta_nada(tmp_path):
    """El día que una clave se cambia de lista en el `config.yaml`.

    Este test existe porque la batería de mutación lo pidió. Los dos guardias de
    tipo de `fijar()` -`typeof valor !== "boolean"` y `typeof valor !== "number"`-
    se podían quitar los dos y no se ponía nada rojo, así que hasta hoy eran un
    comentario largo defendiendo una línea que no defendía nada.

    Y son alcanzables. `recuperar()` filtra los `null` antes de llamar a `fijar`,
    sí, pero solo por el camino de lo ya enviado; el del BORRADOR llama a `fijar`
    con lo que haya, sin mirar. Basta con editar `config.yaml` una mañana en la
    que hay un borrador sin enviar -mover una clave de `checkin_sliders` a
    `checkin_preguntas` o al revés- para que el valor guardado sea del tipo de la
    lista de ayer.

    Lo que pasaría sin los guardias son las dos conversiones de siempre, una en
    cada dirección y las dos silenciosas:

      - un `0` guardado cuando eso era un deslizador se pintaría hoy como un "No"
        a «¿Vas a entrenar hoy?», que es la única respuesta que cambia lo que el
        sistema escribe esta mañana;
      - un `false` guardado cuando eso era una pregunta pondría el deslizador en
        su mínimo, y el mínimo de «Fatiga» no es un hueco: es "ninguna".

    Lo correcto es lo aburrido: no pintar nada, dejarlo sin contestar y que el
    botón siga gris. Un hueco se ve; una respuesta inventada, no.
    """
    salida = _rellenar(tmp_path, _hoy(), [], borrador={
        "day": "2026-09-15",
        # Los tipos, cruzados: un número para la pregunta y un booleano para el
        # deslizador.
        "valores": {"will_train": 0, "fatigue": False},
        "comentarios": "",
    })

    # Ni uno de los dos entra. `"{}"` y no "no está `will_train`": lo que se
    # exige es que no quede NADA, porque lo que viaja en el POST es este objeto.
    assert salida["valores"] == "{}", (
        "un valor del tipo que no es se ha colado en el check-in"
    )
    assert salida["cuerpo"] is None and salida["veces_enviado"] == 0

    # Y se nota en la pantalla, que es lo que hace que se vuelva a contestar.
    assert salida["sin_contestar"] == ["wants_to_train", "will_train"]
    assert salida["elegidas"] == {"wants_to_train": None, "will_train": None}
    assert salida["enviar_deshabilitado"] is True
    assert "Fatiga" in salida["faltan"]
    assert "¿Vas a entrenar hoy?" in salida["faltan"]


# ---------------------------------------------------------------------------
# El selector de qué se va a hacer hoy
# ---------------------------------------------------------------------------
#
# LO QUE SE PROTEGE AQUÍ ES QUE LA PROPUESTA NO SE CONVIERTA EN UNA RESPUESTA.
#
# El encargo dice "preseleccionado con la propuesta del sistema", y la lectura
# literal -meterla en `estado.valores` al abrir- es una línea más corta y hace lo
# mismo en todo salvo en una cosa: cada mañana que se envíe el formulario sin
# mirar el selector guardaría `chosen_session: "dia_3"`, una declaración que
# nadie hizo. Lo que se entrena no cambia -sin elección, el motor planifica la
# propuesta y escribe esa misma rutina-, así que el fallo no se ve por ningún
# lado: la rutina de Hevy es la correcta, el mensaje es el correcto, y la única
# consecuencia aparece meses después, el día que se mire la columna para contar
# cuántas veces me desvié y salga que elegí explícitamente todos los días.
#
# Es exactamente el aplastamiento de los tres estados contra el que están las dos
# preguntas de Sí/No, en el mismo archivo y en la misma pantalla. Aquí es más
# fácil de cometer porque la interfaz que lo comete es la que se pidió.


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_la_propuesta_se_ve_marcada_pero_no_cuenta_como_elegida(tmp_path):
    """EL TEST DE ESTE BLOQUE. Las dos marcas existen y son distintas.

    Se abre el formulario, se contesta todo lo demás y se envía SIN TOCAR el
    selector. Tienen que pasar las tres cosas a la vez:

      - el Día 3 se ve marcado como lo que toca hoy, porque si no se viera la
        pantalla no diría lo que el sistema va a hacer;
      - ninguna opción está elegida, porque nadie ha pulsado ninguna;
      - `chosen_session` NO viaja en el POST, ni como cadena ni como `null`.

    La tercera es la que importa y es la que no se ve mirando la pantalla.
    """
    salida = _rellenar(tmp_path, _hoy(), [
        {"tipo": "deslizar", "key": "fatigue", "valor": 3},
        {"tipo": "responder", "key": "wants_to_train", "respuesta": "si"},
        {"tipo": "responder", "key": "will_train", "respuesta": "si"},
        {"tipo": "enviar"},
    ])

    sel = salida["selector"]
    assert sel is not None, "el selector no se ha pintado"
    assert sel["propuesta"] == "dia_3", (
        "la propuesta del servidor no se ve por ninguna parte: la pantalla no "
        "dice qué rutina va a escribir el sistema si no se toca nada"
    )
    assert sel["elegida"] is None, (
        f"la propuesta se ha pintado como respuesta ({sel['elegida']!r}). Es la "
        f"preselección literal, y convierte cada mañana sin tocar el selector "
        f"en una declaración que nadie hizo."
    )
    assert sel["aria"] == {
        "dia_1": "false", "dia_2": "false", "dia_3": "false",
        "bici": "false", "otro": "false",
    }

    cuerpo = salida["cuerpo"]
    assert cuerpo is not None, "el formulario no llegó a enviarse"
    assert "chosen_session" not in cuerpo, (
        f"el selector sin tocar ha viajado igualmente: {cuerpo['chosen_session']!r}"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_no_tocar_el_selector_no_deja_el_boton_gris(tmp_path):
    """El único hueco del formulario donde no contestar es un camino previsto.

    Y por eso va en su propio test en vez de quedarse implícito en el de arriba.
    Exigirlo sería lo natural -es una pregunta más- y rompería el dato que se
    quiere recoger: con el botón gris hasta pulsar una opción, se pulsaría la
    marcada para desbloquearlo y lo que quedaría apuntado sería el trámite.

    Lo que lo hace seguro es que no contestar tiene un camino: el motor planifica
    la propuesta y escribe esa rutina en Hevy exactamente igual.
    """
    salida = _rellenar(tmp_path, _hoy(), [
        {"tipo": "deslizar", "key": "fatigue", "valor": 5},
        {"tipo": "responder", "key": "wants_to_train", "respuesta": "si"},
        {"tipo": "responder", "key": "will_train", "respuesta": "si"},
    ])

    assert salida["enviar_deshabilitado"] is False
    assert salida["faltan"] is None, (
        f"el selector se está pidiendo como obligatorio: {salida['faltan']!r}"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_elegir_otra_rutina_viaja_como_la_clave_y_deja_ver_cuál_tocaba(tmp_path):
    """El caso que motivó todo esto: tocaba el Día 1 y hago el Día 2.

    Lo que sale por el cable es la CLAVE -`dia_2`- y no la etiqueta que se lee en
    pantalla. Mandar «Día 2 · Tirón» no daría error aquí: lo daría en el
    servidor, donde `upsert_checkin` valida contra las opciones del config, y
    desde el móvil un 422 del check-in entero se lee como una avería.

    Y la marca de la propuesta SIGUE PUESTA sobre el Día 1 después de elegir el
    Día 2. Es deliberado: las dos cosas son ciertas a la vez -tocaba aquello y
    voy a hacer esto- y son justo las dos que el aviso del día siguiente
    necesita distinguir.
    """
    salida = _rellenar(tmp_path, _hoy(selector=_selector(propuesta="dia_1")), [
        {"tipo": "deslizar", "key": "fatigue", "valor": 6},
        {"tipo": "responder", "key": "wants_to_train", "respuesta": "si"},
        {"tipo": "responder", "key": "will_train", "respuesta": "si"},
        {"tipo": "elegir", "key": "chosen_session", "opcion": "dia_2"},
        {"tipo": "enviar"},
    ])

    cuerpo = salida["cuerpo"]
    assert cuerpo["chosen_session"] == "dia_2", (
        f"lo elegido ha salido como {cuerpo['chosen_session']!r}; el servidor "
        f"espera la clave del config, no lo que se lee en el botón"
    )
    sel = salida["selector"]
    assert sel["elegida"] == "dia_2"
    assert sel["propuesta"] == "dia_1", (
        "elegir otra cosa ha borrado la marca de lo que tocaba: en pantalla ya "
        "no se puede ver de qué me estoy desviando"
    )
    assert sel["aria"]["dia_2"] == "true" and sel["aria"]["dia_1"] == "false"
    assert sel["sin_contestar"] is False


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_la_bici_es_una_opcion_como_las_demas(tmp_path):
    """«Hice algo» tiene que poder decirse sin que cuente como «no contesté».

    Bici y Otro no son rutinas y no mueven el ciclo, pero son respuestas, y en
    esta pantalla se pulsan igual que las otras tres. El día que se pintaran
    aparte -o peor, que no se pintaran- todo lo que no fuera una de las tres
    rutinas volvería a caer en el silencio, que es de donde se sacaron.
    """
    salida = _rellenar(tmp_path, _hoy(), [
        {"tipo": "deslizar", "key": "fatigue", "valor": 4},
        {"tipo": "responder", "key": "wants_to_train", "respuesta": "no"},
        {"tipo": "responder", "key": "will_train", "respuesta": "si"},
        {"tipo": "elegir", "key": "chosen_session", "opcion": "bici"},
        {"tipo": "enviar"},
    ])

    assert salida["cuerpo"]["chosen_session"] == "bici"
    assert salida["selector"]["elegida"] == "bici"
    # Y la propuesta sigue siendo la rutina que el sistema va a escribir en Hevy
    # de todas formas, que es el motivo por el que elegir «Bici» no la borra: si
    # acabo yendo al gimnasio, la rutina tiene que estar puesta.
    assert salida["selector"]["propuesta"] == "dia_3"


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_las_opciones_son_las_del_servidor_y_no_las_del_javascript(tmp_path):
    """Ni una rutina escrita a mano en la pantalla.

    La misma regla que ya rige para los deslizadores y las preguntas, y aquí con
    más motivo: la lista sale de `rotation.order`, la misma de la que el motor
    saca qué toca hoy. Escrita a mano, añadir un `dia_4` al ciclo dejaría el
    formulario ofreciendo tres opciones mientras el sistema rota entre cuatro, y
    el día que propusiera el Día 4 no habría forma de aceptarlo.

    Se sirven SEIS, y una de ellas no existe en el proyecto.
    """
    sel = _selector(propuesta="dia_4")
    sel["opciones"].append({
        "key": "dia_4", "label": "Día 4 · Inventado",
        "es_fuerza": True, "pendiente": None, "ultima_vez": None,
    })
    salida = _rellenar(tmp_path, _hoy(selector=sel), [
        {"tipo": "elegir", "key": "chosen_session", "opcion": "dia_4"},
    ])

    visto = salida["selector"]
    assert visto["opciones"] == [
        "dia_1", "dia_2", "dia_3", "bici", "otro", "dia_4"
    ], "la pantalla no pinta las opciones que le manda el servidor"
    assert "Día 4 · Inventado" in visto["etiquetas"]
    assert visto["propuesta"] == "dia_4", (
        "la propuesta que el servidor manda no se pinta si no es una de las que "
        "la pantalla esperaba: eso es tenerlas escritas a mano"
    )
    assert visto["elegida"] == "dia_4"


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_lo_que_lleva_parado_se_ve_al_ir_a_elegir(tmp_path):
    """La línea (e) del encargo, en el momento en que sirve para algo.

    El sitio donde esto se dice es el mensaje de la mañana. Aquí está por otra
    razón: el mensaje se lee a las siete y el selector se toca al salir de casa,
    y entre las dos cosas se olvida. Tenerlo delante mientras se elige es lo que
    convierte el apunte en algo que se puede usar.

    Va sobre la opción y solo sobre ella. Un aviso arriba del bloque diciendo
    «llevas siete sesiones sin hacer el Día 1» sería una regañina; el mismo dato
    al lado del botón que lo arregla es información.
    """
    salida = _rellenar(
        tmp_path, _hoy(selector=_selector(propuesta="dia_2", dia_1=7)), [],
    )

    parados = salida["selector"]["parados"]
    assert list(parados) == ["dia_1"], (
        f"el apunte de lo que lleva parado está sobre {list(parados)}; solo lo "
        f"lleva el Día 1"
    )
    assert "7 sesiones sin hacerlo" in parados["dia_1"]
    # La fecha, con el mismo formato que el mensaje de Telegram: dos sitios
    # distintos contando lo mismo tienen que poder leerse a la vez.
    assert "20/8" in parados["dia_1"]
    # Y sigue pulsándose como cualquier otra: el apunte no la destaca ni la
    # estorba.
    assert salida["selector"]["elegida"] is None


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_una_rutina_caducada_pierde_el_apunte_pero_no_el_boton(tmp_path):
    """Literal del encargo: al caducar cambia la línea, nunca lo que puedo elegir.

    El endpoint ya manda `pendiente: null` en las caducadas -filtra igual que el
    mensaje de la mañana-, así que lo que se comprueba aquí es que la pantalla no
    añada nada por su cuenta: sin número, la opción se pinta exactamente igual
    que las que están al día.

    Una rutina que llevo dos meses sin hacer es la que más fácil es que quiera
    elegir hoy. Esconderla, apagarla o ponerle un aviso encima sería convertir el
    caducado en un castigo, y el sistema no está para eso.
    """
    salida = _rellenar(tmp_path, _hoy(selector=_selector(propuesta="dia_2")), [
        {"tipo": "elegir", "key": "chosen_session", "opcion": "dia_1"},
    ])

    visto = salida["selector"]
    assert visto["parados"] == {}, (
        f"la pantalla se ha inventado un apunte de parada: {visto['parados']}"
    )
    assert "dia_1" in visto["opciones"], "la caducada ha desaparecido del selector"
    assert visto["elegida"] == "dia_1", "la caducada no se deja elegir"


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_elegir_y_nada_mas_ya_guarda_el_borrador(tmp_path):
    """El clic del selector puede ser el único toque de la mañana.

    Se abre el formulario, se elige el Día 2, suena el teléfono y se sale de la
    app. Sin `guardarBorrador()` en el manejador eso se pierde entero, y la
    batería de mutación ya enseñó -con el deslizador- que una llamada que falta
    ahí no pone rojo a nadie salvo que haya un test que la pida.

    Y LA VUELTA, que es la mitad que importa: ese mismo borrador abierto en una
    pantalla nueva. Es donde `recuperar()` llama a `fijar()`, y donde un valor de
    otro tipo se convertiría en una rutina inventada.
    """
    salida = _rellenar(tmp_path, _hoy(), [
        {"tipo": "elegir", "key": "chosen_session", "opcion": "dia_2"},
    ])

    borrador = salida["borrador"]
    assert borrador is not None, "elegir una rutina no ha guardado nada en el móvil"
    assert borrador["valores"] == {"chosen_session": "dia_2"}

    vuelta = _rellenar(tmp_path, _hoy(), [], borrador=borrador)
    assert vuelta["selector"]["elegida"] == "dia_2"
    assert vuelta["valores"] == '{"chosen_session":"dia_2"}'
    # La propuesta sigue siendo la del servidor de HOY, no la del borrador: el
    # borrador guarda lo que contesté, no lo que el sistema proponía ayer.
    assert vuelta["selector"]["propuesta"] == "dia_3"


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_una_eleccion_guardada_que_ya_no_se_ofrece_no_pinta_nada(tmp_path):
    """El día que una rutina sale del ciclo con un borrador sin enviar encima.

    Es el hermano del test de los tipos cruzados, y es alcanzable por el mismo
    camino: el borrador llama a `fijar()` con lo que haya guardado, sin mirar, y
    el `config.yaml` se puede editar entre una mañana y otra.

    Sin la comprobación, `estado.valores.chosen_session` valdría `dia_4`, no se
    pintaría ningún botón -porque no existe- y el envío se iría con una rutina
    que el servidor no conoce. `upsert_checkin` lo rechaza, y lo que se ve en el
    móvil es «el servidor ha rechazado el check-in (422)» sobre un formulario en
    el que todo parecía correcto.

    Los otros dos valores cruzados -un número y un booleano- están aquí por lo de
    siempre: `String(false)` es "false" y `String(0)` es "0", dos rutinas que no
    existen y ningún error por el camino.
    """
    for guardado in ("dia_4", 0, False):
        salida = _rellenar(tmp_path, _hoy(), [], borrador={
            "day": "2026-09-15",
            "valores": {"chosen_session": guardado},
            "comentarios": "",
        })
        assert salida["valores"] == "{}", (
            f"un `chosen_session` de {guardado!r} se ha colado en el check-in: "
            f"{salida['valores']}"
        )
        assert salida["selector"]["elegida"] is None
        assert salida["selector"]["sin_contestar"] is True


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_lo_ya_elegido_hoy_vuelve_a_la_pantalla(tmp_path):
    """Reabrir el formulario después de enviarlo no borra la elección.

    Es la misma vuelta que ya se comprueba para las preguntas, y con la misma
    consecuencia si falla: el selector saldría en blanco y el siguiente envío
    mandaría `chosen_session` sin contestar, borrando la declaración de esta
    mañana y dejando en el histórico un día en el que no se eligió nada.
    """
    hoy = _hoy(
        submitted=True,
        values={"fatigue": 4, "will_train": True, "chosen_session": "dia_1"},
        comments="las piernas cansadas",
        selector=_selector(propuesta="dia_3"),
    )
    salida = _rellenar(tmp_path, hoy, [])

    assert salida["selector"]["elegida"] == "dia_1"
    assert salida["selector"]["propuesta"] == "dia_3"
    assert salida["selector"]["sin_contestar"] is False
    assert salida["elegidas"] == {"wants_to_train": None, "will_train": "si"}


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_un_servidor_sin_selector_no_rompe_el_formulario(tmp_path):
    """La pantalla nueva contra una API vieja, que es el orden en que se despliega.

    El contenedor se recrea y el móvil abre lo que tenga cacheado; durante un
    rato la pantalla puede ser más nueva que el servidor. Sin este camino,
    `datos.selector` sería `undefined`, `sel.opciones` reventaría dentro de
    `arrancar()` -que es `async`- y la excepción se quedaría en una promesa que
    nadie mira: «Cargando…» para siempre, sin un solo error legible.

    Lo que hace es no pintar nada. NO pintar un selector vacío: eso se leería
    como "hoy no hay nada que elegir".
    """
    salida = _rellenar(tmp_path, _hoy(selector=None), [
        {"tipo": "deslizar", "key": "fatigue", "valor": 5},
        {"tipo": "responder", "key": "wants_to_train", "respuesta": "si"},
        {"tipo": "responder", "key": "will_train", "respuesta": "si"},
        {"tipo": "enviar"},
    ])

    assert salida["selector"] is None, "se ha pintado un selector sin opciones"
    assert salida["cuerpo"] == {
        "fatigue": 5, "wants_to_train": True, "will_train": True
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_la_propuesta_se_dice_con_palabras_y_no_solo_con_un_borde(tmp_path):
    """Que la marca de lo que toca se LEA, y no solo se pinte.

    `class="propuesta"` es un borde: lo entiende la hoja de estilos y nadie más.
    Quitar el rótulo dejaba la pantalla con las cinco opciones visualmente casi
    iguales -un borde algo más claro en una de ellas- y con eso el formulario
    deja de contestar la pregunta que justifica que el selector sea opcional: si
    no toco nada, ¿qué se va a escribir en Hevy? Sin esa respuesta a la vista, no
    tocar nada pasa de ser una decisión informada a ser un salto al vacío.

    La batería de mutación borró el rótulo y la suite entera siguió verde.
    """
    salida = _rellenar(tmp_path, _hoy(selector=_selector(propuesta="dia_2")), [
        {"tipo": "deslizar", "key": "fatigue", "valor": 5},
    ])

    sel = salida["selector"]
    assert list(sel["toca"]) == ["dia_2"], (
        f"el rótulo de lo que toca hoy está en {list(sel['toca'])} y la "
        f"propuesta es {sel['propuesta']!r}: o no se dice, o se dice de más"
    )
    assert sel["toca"]["dia_2"].strip(), (
        "el botón de la propuesta lleva el hueco del rótulo pero sin texto: en "
        "pantalla eso es no decir nada"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_lo_que_viaja_es_la_clave_del_boton_sin_respaldos(tmp_path):
    """Pulsar una opción manda ESA opción, aunque su clave sea rara.

    Parece una obviedad y no lo es: `boton.dataset.opcion || sel.propuesta` es
    una línea que cualquiera escribiría para «curarse en salud», y hace algo muy
    distinto de lo que parece. Una clave vacía -un `config.yaml` mal editado- deja
    de mandar la basura que se pulsó y manda la propuesta, o sea la mutación de
    siempre disfrazada de prudencia: una declaración que nadie hizo, y encima en
    el día en que la pantalla estaba rota, que es cuando menos se va a mirar.

    Mandar la cadena vacía es peor a corto plazo -el servidor la rechaza con un
    422- y muchísimo mejor a largo: el fallo se ve el primer día.
    """
    sel = _selector(propuesta="dia_3")
    sel["opciones"].append({
        "key": "", "label": "Sin clave", "es_fuerza": False,
        "pendiente": None, "ultima_vez": None,
    })
    salida = _rellenar(tmp_path, _hoy(selector=sel), [
        {"tipo": "deslizar", "key": "fatigue", "valor": 5},
        {"tipo": "responder", "key": "will_train", "respuesta": "si"},
        {"tipo": "elegir", "key": "chosen_session", "opcion": ""},
        {"tipo": "enviar"},
    ])

    enviado = salida["cuerpo"].get("chosen_session")
    assert enviado != "dia_3", (
        "pulsar un botón sin clave ha declarado la propuesta. Lo que viaja tiene "
        "que ser lo que se pulsó, siempre, sin valores de respaldo"
    )
    assert enviado == "", f"ha viajado {enviado!r} en vez de la clave del botón"


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_un_borrador_con_una_lista_dentro_no_se_convierte_en_rutina(tmp_path):
    """La coerción más traicionera de JavaScript, en el sitio donde más duele.

    `String(["dia_2"])` es `"dia_2"`. No `"[\\"dia_2\\"]"` ni `"[object Array]"`:
    exactamente la clave de una rutina de verdad, indistinguible de haberla
    pulsado. Una lista de un elemento se convierte en ese elemento y ya está.

    Por eso `fijar()` mira el TIPO antes que nada, y por eso ese `typeof` no
    sobra aunque justo debajo se compruebe que el valor es una de las opciones en
    pantalla: esa segunda comprobación no ve la diferencia. Para ella `["dia_2"]`
    coercionado ya es `dia_2`, una opción perfectamente válida, y pinta el botón.

    El borrador del móvil es JSON que escribió una versión anterior de esta misma
    pantalla. Es el único sitio del formulario donde entra un dato con forma
    libre, y entra directo a `fijar()`.
    """
    salida = _rellenar(
        tmp_path, _hoy(), [{"tipo": "deslizar", "key": "fatigue", "valor": 5}],
        borrador={
            "day": "2026-09-15",
            "valores": {"chosen_session": ["dia_2"]},
            "comentarios": "",
        },
    )

    sel = salida["selector"]
    assert sel["elegida"] is None, (
        f"una lista guardada en el borrador se ha pintado como la rutina "
        f"{sel['elegida']!r} elegida a dedo"
    )
    assert json.loads(salida["valores"]).get("chosen_session") is None, (
        "la lista ha entrado en `estado.valores`: de ahí sale el cuerpo del POST"
    )


def test_el_arnes_no_se_traga_un_selector_con_espacios(tmp_path):
    """Un test del arnés, a propósito, y solo para esto.

    `casa()` prometía en su comentario que reventaría con un selector que no sabe
    resolver, «en vez de devolver la lista vacía, que es lo que haría pasar un
    test en verde». No lo hacía. Durante toda la vida del arnés,
    `.pregunta input[type="range"]` devolvió `false` en silencio, y la línea que
    comprueba que las preguntas no se pintan como deslizadores contó siempre cero
    sin poder contar otra cosa.

    Lo que hace que eso no vuelva a pasar es este aviso, así que el aviso tiene
    que estar vivo. Un guardia contra assertions muertas que sea él mismo una
    assertion muerta no protege de nada, y se quedaría así igual de callado.
    """
    salida = _rellenar(tmp_path, _hoy(), [])

    assert salida["arnes_rechaza_descendientes"] is True, (
        "`casa()` vuelve a tragarse un selector con descendientes y a devolver "
        "una lista vacía. Todo lo que se busque así contará cero para siempre"
    )


def test_el_html_tiene_donde_pintar_el_selector():
    """El hueco que `pintarSelector()` necesita, comprobado sin `node`.

    Mismo motivo que el de las preguntas: sin el `<div id="selector">`,
    `$("selector")` da `null`, la primera línea de `pintarSelector` revienta
    dentro de un `async` y lo que se ve en el móvil es un «Cargando…» eterno.
    """
    html = (ESTATICOS / "index.html").read_text(encoding="utf-8")
    assert 'id="selector"' in html, "falta el hueco del selector en index.html"
    # Y en su sitio: DESPUÉS de las preguntas. «¿Vas a entrenar hoy?» decide si
    # hay sesión y esto decide cuál; al revés, se estaría eligiendo rutina para
    # un día que a lo mejor se contesta que no.
    assert (
        html.index('id="preguntas"')
        < html.index('id="selector"')
        < html.index('id="comentarios"')
    )


def test_el_html_tiene_donde_pintar_las_preguntas():
    """El hueco que `pintarPreguntas()` necesita, comprobado sin `node`.

    Sin el `<div id="preguntas">`, `$("preguntas")` da `null` y la primera línea
    de `pintarPreguntas` revienta DENTRO de `arrancar()`, que es `async`: la
    excepción se queda en una promesa que nadie mira y el formulario no llega a
    mostrarse nunca. Desde el móvil eso es un "Cargando…" eterno sin un solo
    error legible.
    """
    html = (ESTATICOS / "index.html").read_text(encoding="utf-8")
    assert 'id="preguntas"' in html, (
        "falta el hueco de las preguntas en index.html"
    )
    # Y en su sitio: después de los deslizadores y antes de los comentarios. El
    # orden no es decoración -«¿Vas a entrenar hoy?» es la única respuesta que
    # cambia lo que el sistema escribe esta mañana, y va pegada al botón-, pero
    # lo que este test defiende es lo otro: que no acabe DENTRO del bloque de
    # deslizadores, donde se leería como uno más.
    assert (
        html.index('id="deslizadores"')
        < html.index('id="preguntas"')
        < html.index('id="comentarios"')
    )


def test_el_formulario_lee_del_servidor_exactamente_lo_que_el_servidor_manda(cliente):
    """Las claves que `arrancar()` saca de `/api/checkin/today`, contra las reales.

    El mismo cruce que ya protege la cobertura de las métricas, aplicado al
    formulario. `datos.preguntas` mal escrito -`datos.pregunta`- no da ningún
    error: da `undefined`, el `|| []` lo convierte en una lista vacía y el
    formulario abre sin las dos preguntas, exactamente igual que antes de que
    existieran. Nada que mirar, nada que se queje.
    """
    servidas = set(cliente.get("/api/checkin/today").json())

    # Las DOS funciones que reciben esa respuesta, y solo ésas. `enviar()` tiene
    # otra variable llamada igual con la respuesta del POST -de donde saca
    # `datos.detail`-, y barrer el archivo entero la contaría como una clave
    # inventada de este endpoint.
    fuente = _sin_comentarios((ESTATICOS / "app.js").read_text(encoding="utf-8"))
    leidas: set[str] = set()
    for nombre in ("async function arrancar", "function recuperar"):
        trozo = fuente[fuente.index(nombre) :]
        leidas |= set(re.findall(r"\bdatos\.([a-z_]+)\b", trozo[: trozo.index("\n}\n")]))

    assert leidas, "no encuentro ninguna lectura `datos.algo` en arrancar()"
    assert "preguntas" in leidas, (
        "`arrancar()` no lee `datos.preguntas`: el servidor las manda y el "
        "formulario abriría sin ellas, igual que antes de que existieran"
    )
    inventadas = leidas - servidas
    assert not inventadas, (
        f"`arrancar()` lee {sorted(inventadas)} y `/api/checkin/today` manda "
        f"{sorted(servidas)}. Leer una clave que no viene no da error: da "
        f"`undefined`, y el `|| []` de al lado lo convierte en un formulario "
        f"sin esa mitad."
    )


# ---------------------------------------------------------------------------
# Los tres botones de comprobación
# ---------------------------------------------------------------------------


def test_cada_boton_de_prueba_llama_a_una_ruta_que_existe(cliente):
    """Los tres botones, comprobados en las DOS direcciones.

    Es el mismo criterio que con las opciones muertas del panel: conéctalas o
    bórralas, pero que no quede ninguna. Un botón sin ruta es un botón que no
    hace nada y que además tranquiliza; una ruta sin botón es código que nadie
    puede llamar y que nadie va a borrar porque no se sabe si sobra.

    Y se comprueba que son POST. Que contesten a GET sería el fallo silencioso
    de verdad: mandar un Telegram porque a un precargador de enlaces le apeteció
    seguir una URL.
    """
    app_js = (ESTATICOS / "app.js").read_text(encoding="utf-8")
    index = (ESTATICOS / "index.html").read_text(encoding="utf-8")

    bloque = re.search(r"probar: \{(.*?)^  \},", app_js, re.S | re.M)
    assert bloque is not None, "no encuentro `probar:` en el mapa API de app.js"
    declaradas = dict(re.findall(r'(\w+): "(/api/probar/[^"]+)"', bloque.group(1)))
    assert declaradas, "el mapa `probar` está vacío"

    en_html = set(re.findall(r'data-prueba="(\w+)"', index))

    assert en_html == set(declaradas), (
        f"botones en el HTML: {sorted(en_html)}; rutas en app.js: "
        f"{sorted(declaradas)}. Sobra o falta en un lado."
    )

    for cual, url in declaradas.items():
        r = cliente.post(url)
        assert r.status_code != 404, f"{cual}: {url} no existe en la API"
        # Con las credenciales de los tests el servicio fallará, y da igual: lo
        # que se comprueba aquí es que la ruta EXISTE y que contesta el informe,
        # no que Telegram esté configurado en la máquina que corre los tests.
        assert r.status_code in (200, 503), f"{cual}: {url} → {r.status_code}"

        # 405 sería la respuesta de libro, pero aquí sale 404: los estáticos van
        # montados en `/`, así que un GET que no casa con ninguna ruta de la API
        # cae en el montaje y se lleva el 404 de ahí. Lo que importa no es cuál
        # de los dos números salga, sino que el GET NO ejecute la prueba, y los
        # dos lo demuestran igual. Lo que no puede salir es un 200.
        get = cliente.get(url)
        assert get.status_code in (404, 405), (
            f"{url} contesta a GET ({get.status_code}): esto tiene efecto en el "
            "mundo y no puede colgar de un verbo que se dispara solo"
        )


def test_el_informe_de_una_prueba_trae_siempre_las_mismas_claves(cliente):
    """El renderizador lee `resumen`, `ok` y `pasos`. Si cambian, pinta vacío."""
    r = cliente.post("/api/probar/hevy")
    if r.status_code != 200:
        pytest.skip("sin cliente de Hevy en este entorno")
    d = r.json()
    assert {"servicio", "ok", "resumen", "pasos"} <= set(d)
    for p in d["pasos"]:
        assert {"nombre", "ok", "detalle", "error"} == set(p)


def test_la_pantalla_avisa_de_todo_lo_que_el_servidor_sabe_marcar():
    """Si el servidor puede ver un problema, la pantalla tiene que pintarlo.

    LA AVERÍA QUE LO TRAJO. El 13 de septiembre `/api/health` decía, con todas
    las letras y en su propio bloque, que el `config.yaml` cargado no era el del
    disco y que el del disco ni se podía leer. La pantalla del móvil no miraba
    ese bloque, así que pintaba un sistema impecable durante treinta horas. El
    dato estaba servido y publicado; lo que faltaba era que alguien lo leyera.

    Esto es la regla de las opciones muertas mirada del otro lado: allí se
    prohíbe un botón sin ruta detrás, aquí se prohíbe un aviso calculado en el
    servidor que no llega a ninguna pantalla. Las dos formas terminan igual -en
    trabajo que no sirve para nada- pero ésta es peor, porque la primera se nota
    al pulsar y ésta sólo se nota el día que hacía falta.

    Se cruzan los BLOQUES, no las frases: el texto de cada aviso es cosa de la
    pantalla y tiene que poder reescribirse sin romper un test.

    SE CUENTAN AVISOS, NO BLOQUES DEL JSON. Empezó siendo lo segundo, y se queda
    corto en cuanto un bloque aprende a marcar dos cosas distintas: `writes`
    marca cuatro -escritura a medias, marca ilegible, rutina huérfana de hoy, y
    no haber podido mirar si la hay- y con el recuento por bloques bastaba con
    que la pantalla pintase una de las cuatro para que el test diera por buenas
    las otras tres sin haberlas mirado nunca.
    """
    from app.api import _problemas_de_salud

    # Un estado en el que TODO está mal a la vez. Cada aviso lleva su marca para
    # poder saber cuál de ellos generó cada frase.
    todo_mal = {
        "secrets_missing": ["HEVY_API_KEY"],
        "dry_run": False,
        "writes": {
            # EL MISMO OBJETO QUE MANDA EL SERVIDOR, NO UNA CADENA. `read_pending`
            # devuelve el contenido ENTERO de la marca -un diccionario-, y aquí
            # ponía `"rutina_dia_2"`. Un doble que no se parece al original no
            # prueba la integración: prueba el doble.
            "pending_write": {
                "routine_id": "29ce5818-5442-4a40-9e70-1e74904d5867",
                "backup": "/app/data/hevy_backups/x/20260914-090018.json",
                "started_at": "2026-09-14T09:00:18",
            },
            "pending_error": "no se ha podido leer la marca",
            "stale_write": "en Hevy quedó el Día 1 y hoy toca Recuperación",
            "stale_error": "no se ha podido mirar si quedó una rutina huérfana",
        },
        "scheduler": {"running": False, "jobs": {}, "error": None},
        "clock": {"matches": False, "error": None},
        "config_file": {"in_sync": False, "error": "x"},
    }
    # Cada cosa que el servidor sabe marcar, con la expresión con la que la
    # pantalla tiene que estar leyéndola.
    bloques = {
        "secrets_missing": "secrets_missing",
        "escritura a medias": "pending_write",
        "marca de escritura ilegible": "pending_error",
        "rutina huerfana": "stale_write",
        "huerfana no comprobable": "stale_error",
        "scheduler": "scheduler",
        "clock": "clock",
        "config_file": "config_file",
    }

    problemas = _problemas_de_salud(todo_mal)
    assert len(problemas) == len(bloques), (
        "el servidor marca un número de problemas distinto del de avisos que "
        f"este test conoce ({problemas}): si se ha añadido uno nuevo, hay que "
        "añadirlo también a la pantalla y a esta lista"
    )

    codigo = _sin_comentarios((ESTATICOS / "app.js").read_text(encoding="utf-8"))
    faltan = [b for b, expr in bloques.items() if expr not in codigo]
    assert not faltan, (
        f"el servidor sabe avisar de {faltan} y `comprobarSalud()` en app.js no "
        "lo mira: el problema se calcula, se sirve por /api/health y no llega "
        "nunca al móvil, que es el único sitio donde se lee"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_la_pantalla_pinta_de_verdad_cada_aviso_que_el_servidor_sabe_marcar(tmp_path):
    """Lo mismo que el test de arriba, pero EJECUTANDO el JavaScript.

    POR QUÉ NO BASTA EL CRUCE POR TEXTO. Una batería de mutaciones sobre el
    aviso de la rutina huérfana mató ocho de diez y dejó vivas exactamente dos:
    las que cambiaban `if (esc.stale_write) {` por `if (false) {` y dejaban el
    cuerpo del bloque intacto. La expresión seguía escrita en el archivo -dentro
    del bloque, en la línea que rellena el texto-, así que el `in codigo` daba
    verdadero mientras el aviso no se pintaba nunca. Un guardián que sobrevive a
    que se le mate lo que vigila no vigila.

    El cruce por texto se queda igualmente, y no es duplicar: pilla el caso que
    ocurrió de verdad -alguien añade un problema al servidor y no toca la
    pantalla- sin depender de que haya `node` en la máquina. Éste pilla el otro,
    que es el aviso escrito y desconectado.

    Y NO SE BUSCA LA FRASE, SE BUSCA EL DATO. Lo que se comprueba es que el
    valor que mandó el servidor aparezca en el HTML: los títulos y las
    explicaciones son cosa de la pantalla y tienen que poder reescribirse. Para
    la rutina huérfana eso es además lo único que sirve, porque el texto útil
    -«Abre Hevy y NO hagas X: hoy toca Y»- viene entero del servidor y es el que
    tiene que llegar al móvil sin recortar.
    """
    # Un `/api/health` con TODO encendido a la vez. Cada valor es único y
    # reconocible para poder decir cuál de los avisos falta.
    salud = {
        "secrets_missing": ["CLAVE-QUE-FALTA"],
        "dry_run": True,
        "writes": {
            # LA MARCA VA COMO LA MANDA EL SERVIDOR: UN OBJETO.
            #
            # Aquí ponía `"MARCA-A-MEDIAS"`, una cadena, y por eso este test
            # -que ejecuta el JavaScript de verdad, que es justo el que se
            # equivocaba- dio verde mientras el móvil pintaba «([object
            # Object])» en el aviso más grave de la pantalla. `read_pending`
            # devuelve el contenido entero del fichero de marca y nunca ha
            # devuelto una cadena.
            #
            # Es el tercer sitio del mismo día donde aparece la misma figura, y
            # conviene decirlo entero porque es la lección: un doble que no se
            # parece al original no prueba la integración, prueba el doble. Con
            # la cadena, el arnés comprobaba que una cadena se interpola bien
            # dentro de una plantilla de cadena. Eso no falla nunca.
            #
            # Los valores son los de la escritura real del 2026-09-14 para que,
            # si alguien rompe `describirPendiente`, el fallo enseñe un caso que
            # ocurrió y no un inventado.
            "pending_write": {
                "routine_id": "29ce5818-5442-4a40-9e70-1e74904d5867",
                "backup": "/app/data/hevy_backups/x/20260914-090018.json",
                "started_at": "2026-09-14T09:00:18",
            },
            "pending_error": "MARCA-ILEGIBLE",
            "stale_write": "HUERFANA: abre Hevy y NO hagas «Día 1»",
            "stale_error": "HUERFANA-NO-COMPROBABLE",
        },
        "scheduler": {"running": False, "jobs": {}, "error": "PLANIFICADOR-PARADO"},
        # El reloj va SIN `error` a propósito: con él la pantalla lo repite y se
        # queda sin ejercitar la rama que redacta la frase con los dos valores,
        # que es la que de verdad sirve -dice en qué zona están las reglas y en
        # cuál va el servidor, que es lo que hace falta para arreglarlo-.
        "clock": {"matches": False, "error": None,
                  "timezone": "Europe/Madrid", "offset": "+00:00"},
        "config_file": {"in_sync": False, "error": "CONFIG-VIEJO"},
    }
    fichero = tmp_path / "health.json"
    fichero.write_text(json.dumps(salud, ensure_ascii=False), encoding="utf-8")

    r = subprocess.run(
        ["node", "tests/salud_pwa.mjs", str(fichero)],
        cwd=RAIZ, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert r.returncode == 0, f"el arnés no terminó:\n{r.stdout}\n{r.stderr}"
    # `app.js` escribe por consola al cargarse -el aviso del service worker-, así
    # que el JSON es la ÚLTIMA línea, no la primera.
    pintado = json.loads(r.stdout.strip().splitlines()[-1])

    assert pintado["visible"], "la caja de avisos se queda oculta con todo en rojo"
    html = pintado["html"]

    esperado = {
        "faltan credenciales": "CLAVE-QUE-FALTA",
        "planificador": "PLANIFICADOR-PARADO",
        "config del disco": "CONFIG-VIEJO",
        # De la marca se exigen las DOS cosas que sirven para ir a mirarla: qué
        # rutina y cuándo se intentó. El id entero porque es lo que se pega en
        # el comando de revertir.
        "escritura a medias (que rutina)": "29ce5818-5442-4a40-9e70-1e74904d5867",
        "escritura a medias (cuando)": "2026-09-14 09:00",
        "marca de escritura ilegible": "MARCA-ILEGIBLE",
        "rutina huerfana": "abre Hevy y NO hagas",
        "huerfana no comprobable": "HUERFANA-NO-COMPROBABLE",
    }
    faltan = [k for k, v in esperado.items() if v not in html]
    assert not faltan, (
        f"el servidor manda {faltan} y la pantalla no lo pinta. El HTML que sale "
        f"es:\n{html}"
    )

    # El reloj es el raro: la pantalla redacta su propia frase con `timezone` y
    # `offset` en vez de repetir el `error` del servidor, así que se comprueba
    # por los dos valores que sí usa.
    assert "Europe/Madrid" in html and "+00:00" in html, (
        f"el aviso del reloj no llega con los datos con los que se arregla:\n{html}"
    )

    # Y que no se haya colado un `undefined` en medio de ninguna frase, que es
    # como se lee una clave mal adivinada desde el móvil.
    assert "undefined" not in html, f"hay una clave inventada en un aviso:\n{html}"

    # NI UN `[object Object]`, QUE ES EL OTRO MODO DE PERDER EL DATO Y NO SE
    # PARECE EN NADA AL PRIMERO. `undefined` sale de leer una clave que no
    # existe; esto sale de leer la clave BUENA y meter el objeto entero en una
    # plantilla de texto. El aviso conserva su título, su color rojo y su pinta
    # de aviso, y donde iba el único dato accionable pone una cadena que no
    # significa nada. Nadie revienta, nada se pone en la consola: hay que estar
    # mirando el móvil para enterarse. Se comprueba sobre el HTML ENTERO y no
    # sobre el bloque de la marca, porque cualquier otro aviso que algún día
    # reciba un objeto se romperá exactamente igual.
    assert "[object Object]" not in html, (
        "un aviso ha metido un objeto entero en una plantilla de texto y el "
        f"dato se ha perdido por el camino:\n{html}"
    )
