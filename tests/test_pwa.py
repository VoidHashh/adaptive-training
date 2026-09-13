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
    """
    rutas = re.search(r"const RUTAS = \{(.*?)^\};", METRICAS, re.S | re.M)
    assert rutas is not None, "no encuentro `const RUTAS` en metricas.js"
    urls = re.findall(r'"(/api/[^"]+)"', rutas.group(1))
    assert len(urls) == 6, f"esperaba las seis rutas de métricas, encontré {urls}"

    for url in urls:
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


# ---------------------------------------------------------------------------
# Las cinco vistas, pintadas de verdad
# ---------------------------------------------------------------------------

HOY = date(2026, 9, 11)
DIAS = 120


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
    """Las seis respuestas de verdad, tal cual las recibe el móvil."""
    rutas = {
        "concordancia": ("/api/metrics/concordancia", {}),
        "desfase": ("/api/metrics/desfase", {}),
        "impacto": ("/api/metrics/impacto", {}),
        "ranking": (
            "/api/metrics/ranking-ejercicios", {"respuesta": "lower_discomfort"}
        ),
        "auditoria": ("/api/metrics/auditoria", {}),
        "percepcion": ("/api/metrics/percepcion", {}),
    }
    salida = {}
    for nombre, (url, extra) in rutas.items():
        r = cliente.get(url, params={"dias": DIAS, **extra})
        assert r.status_code == 200, f"{url} → {r.status_code} {r.text[:400]}"
        salida[nombre] = r.json()
    return salida


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
    # Que las cinco se hayan pintado de verdad, y no que el andamio se haya
    # callado por haberlas saltado todas.
    assert r.stdout.count("ok ") == 5, f"no se pintaron las cinco vistas:\n{r.stdout}"


def test_las_pantallas_de_la_nav_llevan_a_algo_que_existe():
    """Un enlace de la barra que no lleva a ninguna vista es un callejón.

    La barra se pinta desde `PANTALLAS` en `comun.js` y las vistas viven en
    `VISTAS` en `metricas.js`: dos listas en dos archivos que tienen que decir
    lo mismo. Sin esto, un enlace roto cae en la vista por defecto y la barra
    marca como activa una pestaña que no es la que se ve.
    """
    pantallas = re.findall(r'href: "([^"]+)"', COMUN)
    assert len(pantallas) == 6, f"esperaba seis pantallas, encontré {pantallas}"

    vistas = set(_claves_de_objeto(METRICAS, "VISTAS"))
    assert vistas, "no encuentro las claves de VISTAS en metricas.js"

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
