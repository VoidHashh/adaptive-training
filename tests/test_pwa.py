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
from app.engine.session_builder import FULL, RECOVERY, REDUCED
from app.models import (
    Activity,
    Base,
    Checkin,
    DailyMetrics,
    Decision,
    Preview,
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
    # v12: la sección de la portada que explica los vacíos deja de vaciarse en
    # silencio. Solo toca `metricas.js` -es una rama de `bloqueLoQueFalta`-, y
    # el cambio de verdad va en el servidor: el umbral pasa de cero a
    # `MINIMO_REFERENCIA`, o sea que la lista deja de quedarse vacía con una
    # fila de cada.
    #
    # El móvil viejo falla aquí de la forma MENOS grave de todas las que lleva
    # esta lista, y conviene decirlo: como el umbral nuevo lo aplica el
    # servidor, el JavaScript de antes recibe las cuatro fichas y las pinta
    # perfectamente. Lo único que se pierde es la frase del día en que ya no
    # falte nada, que es un día que todavía no ha llegado. Se sube igual porque
    # la regla no es "sube si se nota": es "sube si el armazón cambia", y una
    # excepción por criterio propio es exactamente lo que dejó la versión
    # clavada en "v5" durante dos cambios.
    "v12": "55e614f192e4f1e8b4a2aff3dc19a6ff7a944efa4edb2f8fee303260c1b245cb",
    # v13: el ranking de ejercicios escribe la corrección por comparaciones
    # múltiples. El servidor la calculaba sobre la tanda entera y la mandaba
    # dentro de cada casilla; la sección se pintaba con `ficha()`, que no lleva
    # ni `p_corregida` ni `significativa`, así que los ciento catorce veredictos
    # se tiraban a la basura justo en la única pantalla que ordena
    # correlaciones en un podio numerado.
    #
    # Aquí el móvil viejo falla de la forma GRAVE, y conviene decirlo tan claro
    # como en v12 se dijo que era leve: el cambio es entero del navegador, el
    # servidor ya mandaba los dos campos desde hace commits. Un teléfono que se
    # quede con el JavaScript de antes sigue enseñando el podio de siempre -con
    # el «r = −0,38» en negrita en el primer puesto- y ni una palabra de que,
    # medido contra la HRV el 2026-09-17, de 66 relaciones calculadas no aguanta
    # ninguna. No se ve roto: se ve seguro de sí mismo, que es peor.
    "v13": "eeb477ab59917edd0785b2fb3fb472641f8472235fe4c9ccc2c28808a50bff65",
    # v14: la portada dibuja. Era la única de las siete vistas que no pintaba un
    # solo SVG -medido el 2026-09-17 sobre los datos de verdad: cero gráficos y
    # mil cincuenta y cuatro palabras, mientras desfase pintaba cincuenta curvas
    # e impacto ochenta y seis barras-, y es la puerta, la segunda de la barra
    # de abajo, la que se abre por la mañana. Ahora cada señal con percentil
    # lleva su barra de 0 a 100 con el 50 marcado.
    #
    # El móvil viejo falla aquí de la forma LEVE, y se dice con la misma
    # claridad con la que en v13 se dijo que era grave, porque esa diferencia es
    # el motivo de que esta lista se escriba a mano. Un teléfono que se quede
    # con el JavaScript de antes sigue leyendo «por encima del 10 % de tus días»
    # en la ficha de cada línea: no le falta un dato ni le sobra una afirmación,
    # solo tiene que ir recordando cinco números de memoria en vez de verlos
    # alineados. Se pierde la comparación de un vistazo, no la verdad.
    "v14": "0fa9d3dfeb8912aa9b0566a989dc142689251cf893404db8185269349a601fa5",
    # v15: las siete vistas pasan a gráfico primero, una frase debajo y todo lo
    # demás plegado. Es el cambio más grande que ha tenido el armazón: toca los
    # cuatro archivos -`comun.js`, `graficos.js`, `metricas.js` y `styles.css`-
    # y no reordena la pantalla, la sustituye. Desfase pasa de mil seiscientas
    # setenta y siete palabras a ciento trece, auditoría de mil trescientas
    # noventa y seis a ciento sesenta, y la portada de setecientas setenta y
    # cinco a trescientas noventa, todo medido el 2026-09-17 sobre los datos de
    # verdad.
    #
    # El móvil viejo falla de la forma GRAVE, y esta vez no por lo que le falta
    # sino por lo que le sobra. Tres de las cinco reglas que se pidieron son
    # sobre lo que NO puede estar en la vista principal -ni p corregida, ni
    # correlación de rangos, ni percentiles, ni mitad central-, y un teléfono
    # que se quede con el JavaScript de antes sigue sirviendo exactamente eso:
    # la pantalla entera de estudiar, con sus párrafos y sus tablas de
    # correlaciones, sin un solo error en la consola y sin nada que lo delate.
    # Se ve perfecta y es justo la que se ha pedido retirar, o sea que el único
    # síntoma sería que el trabajo pareciera no estar hecho.
    #
    # Y hay un segundo motivo para que este número tenga que moverse sí o sí:
    # esta tanda añade claves nuevas al payload -`reparto_desfases`,
    # `resumen_puertas`, `como_voy.resumen`, `al_reves` y `resumen_pares`-
    # porque la PWA no cuenta ninguna cifra que luego se lea. El JavaScript de
    # antes no conoce ninguna de las cinco y las tira a la basura en silencio,
    # que es el fallo callado de la v8 repetido cinco veces.
    "v15": "f04eec657e86de02f1a8070107e1a136a222417c3e2ad52c0feb9d8ce96d827f",
    # v16: la tarjeta de «guardado, pero sin decidir» deja de afirmar lo que no
    # sabe. Tenía una frase fija -«no se ha tocado la rutina de Hevy ni se ha
    # enviado ningún mensaje»- y ahora escribe el estado que informa el
    # servidor, con «no se sabe» cuando no informa ninguno.
    #
    # El móvil viejo falla de la forma GRAVE, y es el único caso de esta lista
    # en que lo grave no es que falte una pantalla sino que SOBRA UNA MENTIRA.
    # Un teléfono que se quede con el `app.js` de antes seguirá diciendo que no
    # se envió nada exactamente en el escenario en que sí se envió: el fallo
    # tardío, con la rutina escrita en Hevy y el Telegram ya entregado, que es
    # lo que pasó de verdad el 18 de septiembre de 2026. Y lo dice sin un error
    # en la consola y con el detalle del fallo justo debajo dándole autoridad,
    # o sea que el usuario reenviaría a mano algo que ya estaba hecho.
    #
    # Aquí la red primero no salva: la respuesta nueva de `/api/checkin` trae
    # `hevy` y `telegram` también en la rama de fallo, y el JavaScript de antes
    # no los mira. Llega el dato bueno y se tira, que es el fallo callado de la
    # v8 otra vez, sólo que esta vez el hueco lo rellena una afirmación falsa en
    # lugar de un guion.
    "v16": "39c1f111438685821f039b32b0bbd8bb74124122c7553b6cdcf788f2dee0e927",
    # v17: el botón de PREVISUALIZAR y su tarjeta. Toca `index.html` -el botón
    # nuevo encima del de enviar- y `app.js` -la tarjeta entera, la cabecera de
    # lo que no se ha tocado y los dos toques del desacuerdo-.
    #
    # El móvil viejo ENTERO falla como en la v10: el botón no está, y desde el
    # teléfono eso es indistinguible de que el trabajo no se haya hecho todavía.
    # Molesto, pero honrado: no se puede pulsar lo que no se ve.
    #
    # Lo que obliga a subir el número es el móvil A MEDIAS, y es el primer
    # cambio de esta lista donde esa mezcla importa de verdad. El `fetch` del
    # service worker va a la red primero y hace `cache.put` ARCHIVO A ARCHIVO,
    # así que un teléfono que se traiga `/index.html` con cobertura y pierda la
    # red antes de pedir `/app.js` se queda con el HTML nuevo y el JavaScript de
    # antes. Sin subir la versión, ese `app.js` viejo sigue siendo el que hay en
    # `armazon-v16` y `activate` no lo borra, porque el nombre no ha cambiado.
    # Queda un botón PREVISUALIZAR pintado y sin oyente: no hace nada al
    # pulsarlo. Ni error en la consola, ni petición, ni tarjeta.
    #
    # Y no es un modo de fallo hipotético: es el bug que tenía este mismo commit
    # antes de escribirse los tests -`previsualizar()` estaba entera y nadie la
    # había enganchado-. Aquí la red primero tampoco salva: el problema no es
    # que llegue un dato bueno y se tire, como en la v8, sino que el botón que
    # existe para prometer «esto todavía no ha escrito nada» incumple la otra
    # mitad de la promesa, que era hacer algo.
    "v17": "ab1a09f2ab556a81577ede60f26f72d571ceee251b5f97a97d6f75427937fa09",
    # v18: la octava vista, calibración. Toca `comun.js` -la barra de abajo pasa
    # de ocho botones a nueve, y `pintarCobertura` gana el tercer argumento- y
    # `metricas.js` -la vista entera, más el sitio donde percepción pasa a decir
    # su propia frase de cobertura-.
    #
    # El móvil viejo ENTERO falla como en la v10 y por lo mismo: la barra es la
    # de antes, sin «Calibra», así que a la pantalla no se llega. Molesto y
    # honrado; desde el teléfono es indistinguible de que no se haya hecho.
    #
    # Lo GRAVE es otra vez el móvil A MEDIAS, y esta vez el que se trae
    # `metricas.js` nuevo y se queda con el `comun.js` de antes. A la vista sí se
    # llega -el enrutador es `location.hash`, y `#calibracion` está en un
    # marcador o se teclea-, y el `pintarCobertura` viejo solo acepta dos
    # argumentos: el tercero se cae sin un error y sin un aviso, y la pantalla
    # imprime la frase que ese `pintarCobertura` llevaba cosida dentro, «trabaja
    # sobre las sesiones ya cruzadas, y cada una lleva dentro de qué pudo
    # juzgarse». Es verdad de percepción y mentira de calibración, que no mira ni
    # una sesión cruzada: mira la tabla de previsualizaciones.
    #
    # O sea el fallo de la v16 repetido -no falta nada, SOBRA UNA MENTIRA- y con
    # el agravante de que la frase está bien escrita, en su sitio y en su tipo de
    # letra, justo debajo del encabezado de una vista que existe para calibrar.
    # Es exactamente el defecto que se acaba de quitar de `comun.js` al sacarle
    # la frase de dentro, servido por un caché en vez de por el código.
    "v18": "8f10be6a94d4940fcdf720ac5eec7bbdc3ffdd509ef454c806417237ba6f2893",
    # v19: el service worker deja de escribir el armazón al vuelo. Toca `sw.js`
    # -que NO está en el armazón y por tanto no mueve esta huella- y `comun.js`
    # -el `throw` de `pintarCobertura`-, que sí la mueve. O sea que el número
    # tendría que subir de todas formas, pero conviene decir que si el cambio
    # hubiera sido sólo el del service worker esta huella no se habría movido y
    # esta línea no haría falta: `sw.js` se pide por su cuenta y el navegador lo
    # revisa él solo, sin pasar por ningún caché nuestro. Es el único archivo de
    # la PWA que llega siempre.
    #
    # EL MÓVIL VIEJO FALLA DE LA FORMA LEVE, y es de las pocas veces. Un teléfono
    # con el `comun.js` de la v18 y el `metricas.js` de ahora no imprime ninguna
    # mentira: la vista de calibración pasa su tercer argumento, el
    # `pintarCobertura` de antes lo ignora sin reventar y sale «Esta vista no
    # mira las fuentes una a una.» a secas. Es una frase corta y verdadera a la
    # que le falta el porqué. Molesta y no engaña.
    #
    # LO QUE HAY QUE ENTENDER DE ESTA SUBIDA es que es la ÚLTIMA que se juega
    # bajo la regla vieja. El teléfono que está ahí fuera lleva el service worker
    # de la v18 activo, y ese todavía va a la red primero y hace `put` archivo a
    # archivo: la instalación del v19 se produce, por tanto, en el mismo mundo
    # que fabricaba el móvil a medias. Lo que arregla este cambio no es esta
    # actualización sino todas las siguientes. Decirlo aquí para que nadie lea el
    # commit y dé por cerrada una ventana que sigue abierta un arranque más.
    #
    # Y desde aquí el número deja de ser un seguro contra un caso raro: el
    # armazón ya no se reescribe con lo que llegue por red, así que no subirlo no
    # deja atrás al móvil que estuvo sin cobertura -deja atrás a todos, con red o
    # sin ella, hasta el siguiente arranque después de la siguiente subida-. Esta
    # lista pasa de ser una bitácora a ser el mecanismo.
    "v19": "132753ca626dcc2a9ea9c335e67aefd12a8d09240339a9b9fb229e25a3fcfcdc",
    # v20: el botón de previsualizar y su tarjeta, que llevaban desde el
    # commit `67962aa` sin una sola línea de CSS. Toca `styles.css` y nada más.
    #
    # EL MÓVIL VIEJO FALLA DE LA FORMA LEVE, y por una vez es LA MISMA avería
    # que ya tiene. Un teléfono que se quede con el `styles.css` de antes sigue
    # viendo el botón de 92 × 20 píxeles en gris claro sobre fondo oscuro, que
    # es exactamente lo que se ve hoy. No aparece nada roto que antes
    # funcionara: sencillamente no llega el arreglo. Es el único caso de esta
    # lista donde no subir el número no empeora nada, y aun así hay que
    # subirlo, porque desde la v19 es el ÚNICO camino por el que entra un
    # archivo nuevo y sin él no llega tampoco al que sí tiene cobertura.
    #
    # LO QUE HAY QUE SABER DE ESTA HUELLA es que es la primera que se calcula
    # sobre un `styles.css` que algún test abre. Hasta hoy este número se movía
    # cuando cambiaba la hoja de estilos -está en `ARMAZON`, entra en el
    # cálculo- y nadie había mirado nunca su contenido: la huella sabía decir
    # «esto ha cambiado» y no «esto está completo». Lo segundo lo dice ahora
    # `tests/test_estilos.py`, y es lo que faltaba para que un botón no pudiera
    # volver a nacer invisible.
    "v20": "7bad666e85b7b6d605a7a72dd092d629024e874c84ea108e701fb96d40aee455",
    # v21: `svg.g-hrv` entra en la regla de los otros gráficos, o sea una línea
    # de `styles.css` y nada más. Le faltaba el `display: block`: un SVG en
    # línea se sienta sobre la línea base y deja debajo el hueco que el
    # navegador reserva para las colas de las letras.
    #
    # EL MÓVIL VIEJO FALLA DE LA FORMA LEVE, y es la más leve de toda esta
    # lista: tres o cuatro píxeles de más debajo de la curva de HRV. Se apunta
    # igual, y el motivo de apuntarlo no es este cambio sino la regla que lo
    # obliga: desde la v19 subir el número es el único camino por el que entra
    # un archivo nuevo, así que la pregunta «¿merece la pena subir la versión
    # por esto?» ya no tiene sentido. O sube, o no llega.
    "v21": "c2c08eed83f4f0ed00a1b0cdf10e87403b05084fcee05de6cb034f660ca54de9",
    # v22: el control para elegir qué sesión voy a hacer. Toca `app.js` -el
    # bloque, sus oyentes, la pregunta del rojo y los dos cuerpos que salen- y
    # `styles.css`.
    #
    # EL MÓVIL VIEJO FALLA DE LA FORMA LEVE, y por una razón que conviene
    # entender antes de darla por buena: un teléfono que se quede con el
    # `app.js` de la v21 no enseña el control, así que no puede pedir nada, así
    # que manda el mismo cuerpo que mandaba ayer y el sistema decide como
    # decidía ayer. No aparece nada roto ni nada mentiroso: falta una
    # capacidad, y su ausencia se ve -no hay control que pulsar-.
    #
    # Lo que NO es leve es el motivo por el que esto existe. El 21-09-2026 el
    # sistema puso ámbar por `lumbar_medio`, el usuario se veía bien, y al
    # enviar se le escribió la reducida en Hevy; tuvo que arreglarlo a mano. El
    # motor sabía recibir la anulación desde semanas antes -`SesionPedida`,
    # `SesionAnulada`, `ConfirmacionNecesaria`, las columnas
    # `override_session_type` y `forced_on_red` en `previews`- y la pantalla no
    # tenía por dónde pedirla. O sea que un móvil sin actualizar deja al
    # usuario exactamente donde estaba ese día: teniendo que saltarse el
    # sistema, y por tanto sin que el desacuerdo quede registrado.
    "v22": "88a13967679f1afa7739f5978f03555f54fb43415c6ecd72984991842810e9bf",
    # v23: el contador de anulaciones por regla en la vista de calibración, y
    # el arreglo de la contradicción del bloque «Por qué». Toca `metricas.js`
    # -el bloque nuevo y su tabla- y `app.js` -`porQue`-.
    #
    # EL MÓVIL VIEJO FALLA DE LA FORMA CALLADA, y es de las pocas veces que
    # esta lista tiene que decir eso de un cambio pequeño. Un teléfono con el
    # `metricas.js` de la v22 abre la calibración y NO enseña el contador: el
    # backend manda `anulaciones` y el JavaScript de antes no conoce la clave,
    # así que la tira a la basura sin un error. La pantalla se ve entera y
    # correcta, y lo que falta es justamente el número que contesta «¿cuánto
    # queda para poder proponer algo?». Es el fallo callado de la v8 otra vez.
    #
    # Lo de `porQue` es distinto y más leve: la contradicción que arregla -«Ninguna
    # regla ha saltado hoy» encima de la regla que saltó- NO se daba con la
    # respuesta que manda el endpoint, que trae `trigger_rule` también en la
    # raíz. Se daba al pintar la tarjeta desde el payload guardado en
    # `previews`, que no la trae. O sea que un móvil sin actualizar no empeora:
    # sigue sin poder llegar a ese caso por el camino normal.
    "v23": "88047a36f6850846f354a46456f2e3daa602af3c37033d7ec3b73779a7df26b8",
    # v24: con un «no voy a entrenar» contestado, la tarjeta deja de prescribir.
    # Toca `app.js` y nada más.
    #
    # EL MÓVIL VIEJO FALLA DE LA FORMA GRAVE, y es del tipo que esta lista ya
    # ha tenido que apuntar dos veces: no falta nada, SOBRA una instrucción. Un
    # teléfono con el `app.js` de la v23 sigue enseñando «Lo que propondría»,
    # los cambios sobre la rutina y qué ejercicio se retira, el día en que el
    # usuario acaba de contestar que no va a entrenar. Y lo enseña debajo de una
    # nota que dice «hoy no entrenas», así que la pantalla se contradice a sí
    # misma con las dos afirmaciones a la vista.
    #
    # Encima ofrece el control de qué sesión hacer, y ahí deja de ser
    # cosmético: una opción pulsada ese día se apunta como ANULACIÓN y cuenta
    # hacia el umbral de diez de una regla con la que nadie está discutiendo.
    # El móvil sin actualizar no solo enseña de más: ensucia la medida.
    "v24": "b07fb43dc6a44b11f6f898ba989c9e5d2b55d88f5e35957a894d40f56dec6ed8",
    # v25: la pantalla de después de entrenar, la página de Avanzado, y la
    # barra de nueve pestañas reducida a cuatro. Cuatro archivos nuevos en el
    # armazón -`despues.html`, `despues.js`, `avanzado.html`, `avanzado.js`- y
    # cambios en `comun.js` y `styles.css`.
    #
    # EL MÓVIL VIEJO FALLA DE LA FORMA LEVE, y es de las pocas veces que lo
    # hace sin matices. Se queda con la barra de nueve pestañas de la v24, y las
    # nueve siguen llevando a algo que existe, porque ninguna vista se ha
    # borrado: solo se han movido de sitio. No ve «Después» ni «Más», así que no
    # tiene cómo llegar a las páginas nuevas, y no llegar a algo nuevo no es
    # enseñar algo falso.
    #
    # El único camino roto de verdad es entrar a `avanzado.html` por una URL
    # escrita a mano: cargaría el `comun.js` de la v24, que no tiene
    # `pintarAvanzado`, y la lista saldría vacía. Pero la barra vieja no enlaza
    # ahí, así que hay que ir a buscarlo.
    "v25": "780cc97b6854126eeeac5cab24ef9d2708297e817603ff3aeabcd01aeb13241f",
    # v26: «Cómo vas» más ligera. La portada pierde el selector de ventana y
    # deja de mandar `dias`; la cobertura y «lo que todavía no se puede
    # contestar» se pliegan al final; «qué ha cambiado» sube junto al semáforo.
    # Toca `metricas.html` (un `id` en la barra), `metricas.js` y `styles.css`.
    #
    # EL MÓVIL VIEJO FALLA DE LA FORMA LEVE. Sigue viendo la portada como en la
    # v25 -selector y cobertura arriba, lo pendiente desplegado-, que es una
    # pantalla más densa pero no una pantalla que mienta. Los dos subtítulos
    # nuevos los recibe igual, porque los redacta el servidor.
    #
    # La única mezcla peligrosa sería `metricas.js` nuevo con `metricas.html`
    # viejo: el script pide `$("barra-ventana")`, el HTML viejo no tiene ese
    # `id`, y `cargar()` reventaría en TODAS las vistas de métricas, no solo en
    # la portada. Es exactamente la mezcla de generaciones que el armazón
    # versionado existe para impedir -los dos van en el mismo caché y se
    # renuevan juntos-, y la vigila `test_el_armazon_no_mezcla_dos_generaciones`.
    "v26": "0f06eec4cf7f1b45fe6b6aa2143d5de6d3368bd382035760ae59d605791c994f",
    # v27: el recálculo al abrir. Con el check-in hecho, `app.js` pide
    # `POST /api/decision/recalcular` y pinta el aviso que conteste el
    # servidor. Solo toca `app.js`.
    #
    # EL MÓVIL VIEJO FALLA CALLADO, y es el caso que motivó el cambio. Abre la
    # app, no pide nada, y el día a ciegas se queda a ciegas sin que la pantalla
    # lo diga, mientras el mensaje de la mañana -que lo redacta el servidor y le
    # llega ya nuevo- le promete que abrir la app lo recalcula. Dura lo que
    # tarde en coger la v27: dos arranques. Los dos reintentos con hora (07:30
    # y 09:00) siguen existiendo y cubren ese hueco si el equipo está despierto.
    #
    # No hay mezcla peligrosa de generaciones: `app.js` nuevo con `index.html`
    # viejo funciona, porque el aviso se cuelga de `#formulario`, que ya existía.
    "v27": "b4f6706ed63a8fd84186e1ef840feba759d0a57dd1662d77cc00bf2626971d58",
    # v28: «Hoy» abre con la decisión cuando el día ya está decidido, con el
    # formulario plegado detrás de «Cambiar mis respuestas»; la tarjeta dice
    # Hevy y Telegram en palabras y no en código. Toca `index.html` (el botón),
    # `app.js` y `styles.css`.
    #
    # EL MÓVIL VIEJO FALLA DE LA FORMA LEVE. Ignora `decision_de_hoy` y abre con
    # el formulario relleno, como hasta hoy: menos cómodo, pero nada falso.
    #
    # La mezcla peligrosa sería `app.js` nuevo con `index.html` viejo: el script
    # engancha `$("cambiar-respuestas")` al cargar, el HTML viejo no tiene ese
    # `id`, y la pantalla entera reventaría antes de pintar el formulario. Es la
    # mezcla de generaciones que el armazón versionado existe para impedir, y la
    # vigila `test_el_armazon_no_mezcla_dos_generaciones`.
    "v28": "c31be18e7cdca7c7d58eb2aa970df8fddd904aea6168e464656dd561d635a6a4",
}


def test_la_version_del_service_worker_sube_cuando_cambia_el_armazon():
    """Una regla que solo vive en un comentario no es una regla.

    `sw.js` lleva escrito desde siempre que la versión sube cada vez que cambia
    el armazón, y entre medias `metricas.js` cambió dos veces con la versión
    clavada en "v5". Nadie lo vio, y no por descuido: el `fetch` del service
    worker iba entonces a la red primero, así que con cobertura el archivo nuevo
    llegaba igual y la pantalla se veía bien. Lo que la versión protegía era el
    móvil que estuvo sin red -se queda con el `armazon-v5` entero, `activate` no
    lo borra porque el nombre no ha cambiado, y abre un `metricas.js` viejo
    contra una API nueva-. Ese caso no aparece mirando la pantalla ningún día.

    DESDE LA v19 YA NO ES UN CASO RARO. El armazón se sirve del caché de su
    versión y no se reescribe al vuelo, o sea que subir el número es el único
    camino por el que entra un archivo nuevo: dejarlo clavado ya no deja atrás
    al móvil que estuvo sin cobertura, los deja atrás a todos. Este test pasa de
    cubrir un hueco a sostener el mecanismo entero, y sigue valiendo igual
    porque lo que compara es lo mismo.

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
# El service worker, ejecutado
# ---------------------------------------------------------------------------
#
# Todo lo de aquí arriba lee `sw.js` como texto, y contra lo que hay que
# demostrar ahora el texto no llega. La afirmación es «una petición del armazón
# no escribe en el caché», y el `caches.put` sigue en el archivo -tiene que
# seguir, porque el camino de lo que NO es armazón lo conserva-. Lo que cambió
# no es si la llamada existe, es por qué rama se pasa, y una rama no se lee con
# una expresión regular.


def _correr_sw(tmp_path, guion: dict) -> dict:
    """Despacha los eventos del service worker de verdad y devuelve qué pasó."""
    fichero = tmp_path / "guion_sw.json"
    fichero.write_text(json.dumps(guion, ensure_ascii=False), encoding="utf-8")
    r = subprocess.run(
        ["node", "tests/sw_pwa.mjs", str(fichero)],
        cwd=RAIZ, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120,
    )
    assert r.returncode == 0, f"el arnés no terminó:\n{r.stdout}\n{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_pedir_el_armazon_no_escribe_en_el_cache(tmp_path):
    """EL TEST DE ESTE BLOQUE. El armazón se lee del caché y no lo reescribe.

    El `fetch` iba a la red primero y guardaba cada respuesta buena con un `put`
    suelto. `CACHE` es el del service worker ACTIVO, que durante una
    actualización es todavía el viejo, así que un móvil con el SW anterior
    conectándose contra el contenedor nuevo escribía el JavaScript nuevo DENTRO
    del caché viejo. Con eso, `armazon-v18` dejaba de contener la v18: contenía
    una mezcla, y la mezcla no se distingue de lo correcto mirando la pantalla.

    Se despacha el evento y se mira quién tocó el caché. Un
    `assert "caches.put" not in SW` no serviría: el `put` sigue en el archivo a
    propósito, y lo que se quiere afirmar es por qué rama se pasa.
    """
    salida = _correr_sw(tmp_path, {
        "peticiones": [
            {"url": "/metricas.js"},
            {"url": "/comun.js"},
            {"url": "/", "mode": "navigate"},
            {"url": "/icons/icon-192.png"},
        ],
    })

    for p in salida["peticiones"]:
        assert p["interceptada"], f"`{p['url']}` tenía que contestarla el caché"
        assert p["escrituras"] == [], (
            f"pedir `{p['url']}` ha escrito en el caché: {p['escrituras']}. El "
            f"armazón se llena una vez, en `install`, y no se retoca pieza a "
            f"pieza: un `put` aquí mete la generación de ahora en el caché de la "
            f"versión de antes."
        )
        assert p["fue_a_la_red"] == [], (
            f"`{p['url']}` estaba en el caché y aun así se ha ido a la red"
        )

    # Y el número de un vistazo: fuera de `install`, ni una escritura.
    assert salida["escrituras_fuera_de_install"] == [], (
        f"el caché se ha tocado fuera de la instalación: "
        f"{salida['escrituras_fuera_de_install']}"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_el_armazon_no_mezcla_dos_generaciones_en_el_mismo_cache(tmp_path):
    """El fallo entero, montado: caché viejo, red nueva y cobertura intermitente.

    Es el escenario de verdad de una actualización. El caché tiene la generación
    de antes; el contenedor ya sirve la de después; y la red va y viene, que es
    como va una red desde un móvil.

    Con el `put` al vuelo pasaba esto: `metricas.js` salía de la red, NUEVO, y se
    escribía en el caché viejo. `comun.js` se pedía con la red caída, y el
    `catch` lo sacaba del mismo caché, VIEJO. Dos generaciones en la misma
    pantalla, sin un error en la consola, y con el agravante de que la pantalla
    se pinta: no se ve rota, se ve rara.

    Lo que se exige es que las dos salgan de la MISMA generación. No que salgan
    nuevas -eso es lo que se ha renunciado a propósito, y está escrito en la
    cabecera del `fetch`-: que sean coherentes entre sí.
    """
    salida = _correr_sw(tmp_path, {
        # La instalación no corre: se simula el SW viejo ya activo, que es quien
        # tiene el problema. Un `install` aquí llenaría el caché de nuevo y
        # borraría el escenario.
        "instalar": False,
        "activar": False,
        # El nombre es el de la versión ANTERIOR a propósito, y por eso no se
        # queda obsoleto al subir `VERSION`: lo que representa es «el caché que
        # ya estaba», no el de ahora. Si el `put` volviera, escribiría en el de
        # ahora -en `CACHE`- y `escrituras` lo cazaría igual.
        "precargado": {
            "armazon-de-la-version-anterior": {
                "/metricas.js": "vieja",
                "/comun.js": "vieja",
            },
        },
        "marca_red": "nueva",
        "red": {"/comun.js": "fallo"},
        "peticiones": [{"url": "/metricas.js"}, {"url": "/comun.js"}],
    })

    marcas = {p["url"]: (p["resultado"] or {}).get("marca") for p in salida["peticiones"]}
    assert len(set(marcas.values())) == 1, (
        f"la pantalla se ha montado con dos generaciones a la vez: {marcas}. Es "
        f"el fallo que el `put` al vuelo fabricaba, y no da ningún error: la "
        f"pantalla se pinta entera y a medias."
    )
    assert salida["escrituras"] == [], (
        f"la respuesta de la red se ha metido en el caché de la versión de "
        f"antes: {salida['escrituras']}"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_la_instalacion_llena_el_cache_de_una_vez_y_entera(tmp_path):
    """La otra mitad: si no se escribe al vuelo, `install` tiene que entrar todo.

    Sin esta, la de arriba se aprueba sola con un service worker que no cachee
    nada -cero escrituras es cero escrituras-. Aquí se exige lo contrario: que
    `install` deje dentro el armazón ENTERO, que es de dónde sale ahora todo lo
    que se sirve.
    """
    salida = _correr_sw(tmp_path, {"peticiones": []})
    # `/` es un alias de `/index.html` y el doble los guarda por su ruta, así que
    # se comparan las claves de `ARMAZON` tal cual.
    assert set(salida["instalacion"]["cacheado"]) == set(salida["armazon"]), (
        "`install` no ha dejado el armazón entero en el caché de su versión"
    )
    assert salida["instalacion"]["skip_waiting"]


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_una_sola_ruta_caida_deja_el_cache_sin_estrenar(tmp_path):
    """`addAll` es atómico, y de eso depende que no quepan dos generaciones.

    Si `install` guardara las que van saliendo, un corte de red a mitad dejaría
    medio caché de la versión nueva y el resto por llenar, que es exactamente la
    mezcla que este cambio existe para impedir, solo que por el otro lado.

    No se comprueba que `addAll` esté escrito -eso es texto- sino que una ruta
    caída deja el caché VACÍO. Es la propiedad, no la llamada.
    """
    salida = _correr_sw(tmp_path, {
        "red": {"/metricas.js": "fallo"},
        "peticiones": [],
    })
    assert salida["instalacion"]["cacheado"] == [], (
        f"una ruta del armazón se cayó y el caché se ha quedado a medias: "
        f"{salida['instalacion']['cacheado']}. Medio armazón de una versión es "
        f"la mezcla de generaciones otra vez."
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_el_service_worker_no_se_mete_en_las_peticiones_de_la_api(tmp_path):
    """Lo de siempre, ahora despachado en vez de leído.

    `test_el_service_worker_no_cachea_nada_de_la_api` mira que exista el corte;
    esto mira que el corte CORTE. Son dos cosas distintas: el `return` podría
    seguir escrito debajo de un `respondWith` puesto encima, y la lectura no lo
    vería.
    """
    salida = _correr_sw(tmp_path, {
        "peticiones": [
            {"url": "/api/checkin/today"},
            {"url": "/api/decision"},
            {"url": "/api/metrics/calibracion"},
        ],
    })
    for p in salida["peticiones"]:
        assert not p["interceptada"], (
            f"el service worker se ha metido en `{p['url']}`. Servir un "
            f"`/api/checkin/today` de ayer abre el formulario diciendo «ya está "
            f"hecho» un día en que no lo está."
        )
    assert salida["escrituras_fuera_de_install"] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_lo_que_no_es_armazon_sigue_yendo_a_la_red_primero(tmp_path):
    """El control, y no sobra: el cambio es del armazón, no del `fetch` entero.

    Un service worker que dejara de escribir en el caché del todo aprobaría los
    tests de arriba y perdería la copia sin cobertura de lo que no está en
    `ARMAZON`. Aquí el `put` tiene que seguir ocurriendo.
    """
    salida = _correr_sw(tmp_path, {
        "peticiones": [{"url": "/algo-que-no-esta-en-el-armazon.txt"}],
    })
    p = salida["peticiones"][0]
    assert p["interceptada"]
    assert p["fue_a_la_red"] == ["/algo-que-no-esta-en-el-armazon.txt"]
    assert [e["tipo"] for e in p["escrituras"]] == ["put"], (
        "lo que no está en `ARMAZON` tiene que seguir guardándose al vuelo: no "
        "lo precarga `install`, así que sin esto no tiene copia ninguna"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_sin_red_y_sin_cache_solo_las_navegaciones_reciben_la_portada(tmp_path):
    """El último recurso, despachado. Un `<script src>` prefiere fallar.

    Devolverle `/index.html` a quien pedía un script le da el HTML del check-in
    con `Content-Type: text/html`: el navegador se niega a ejecutarlo y la
    pantalla sale en blanco sin un solo error legible desde el móvil.
    """
    salida = _correr_sw(tmp_path, {
        # `install` no corre y el armazón se pone a mano: así la red puede estar
        # caída para TODO, que es el escenario -el móvil que ya tiene la
        # aplicación instalada y se queda sin cobertura-. Con `install` corriendo
        # habría que darle red para llenar el caché y entonces ya no estaría
        # caída. El nombre del caché da igual a propósito: `caches.match` busca en
        # todos, como el de verdad.
        # `activate` tampoco, y no es un detalle del arnés: `activate` borra todo
        # caché que no se llame como el de esta versión, así que correrlo aquí se
        # llevaría por delante el armazón que se acaba de poner. Lo que se está
        # simulando es un service worker YA activo abriendo la aplicación sin
        # cobertura, que es cuando el último recurso sirve para algo.
        "instalar": False,
        "activar": False,
        "precargado": {"armazon-ya-instalado": {"/index.html": "instalada"}},
        "red_por_defecto": "fallo",
        "peticiones": [
            {"url": "/pagina-que-no-existe", "mode": "navigate"},
            {"url": "/script-que-no-existe.js", "mode": "no-cors"},
        ],
    })
    pagina, script = salida["peticiones"]
    assert pagina["resultado"]["marca"] == "instalada", (
        f"una navegación sin red tenía que caer en `/index.html`; salió "
        f"{pagina['resultado']}"
    )
    assert script["resultado"]["marca"] == "error-de-red", (
        "a un script que no está se le contesta con un error, no con una página"
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

    Y encima va el service worker, que es lo que lo vuelve grave. Su `fetch`
    decía «primero la red» en el código y en el comentario, pero `fetch()` pasa
    por el caché HTTP; con la heurística delante, «primero la red» era «primero
    lo viejo», y además guardaba lo viejo en `CacheStorage`. La pantalla salía
    entera, bien pintada y de antes de ayer.

    DESDE LA v19 EL ARMAZÓN YA NO SE PIDE POR RED EN CADA ARRANQUE, y eso no
    quita esta cabecera: la vuelve más importante y le deja un solo momento. El
    armazón se busca en la red exactamente una vez por versión, dentro del
    `addAll` de `install`; si esa única petición la contesta el caché HTTP con la
    heurística, lo viejo no se sirve una vez, se queda GRABADO en el caché con
    nombre nuevo y ahí sigue hasta la siguiente subida de `VERSION`. Antes la
    heurística costaba una pantalla vieja un arranque; ahora cuesta una
    generación entera mal etiquetada.

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


# El semáforo de cada día, en un solo sitio.
#
# Lo leen DOS bucles -el de las decisiones y el de las previsualizaciones- y
# tienen que coincidir: una previsualización es la foto de la decisión de ese
# día, así que si aquí sale ámbar y allí rojo, la vista de calibración agrupa por
# un color que nunca existió y la tabla sale bien formada y falsa.
_LUCES = ["green", "green", "amber", "green", "red"]

# Qué sesión propone cada color cuando nadie lleva la contraria. Es el reparto
# corriente del `config.yaml`, y hace falta escrito porque de él sale el `kind`
# que `_sesion_ejecutada` va a leer.
_SESION_POR_LUZ = {"green": FULL, "amber": REDUCED, "red": RECOVERY}


# LAS PREVISUALIZACIONES, ESCRITAS A MANO Y NO POR MÓDULOS.
#
# El resto del sembrado va con `i % 7` y senos porque ahí solo hace falta que
# haya filas. Aquí no: cada una de estas nueve líneas existe para llevar a
# `pintarCalibracion` por una rama distinta, y con un módulo no se sabe cuál de
# ellas se está perdiendo cuando cambie el número de días.
#
# Lo que hay que conseguir, y por qué cada cosa:
#
#   - los TRES estados de `disagreed` con filas dentro. El anillo de «cuántas
#     veces» tiene tres trozos y el tercero -«sin opinar»- es el que hace que el
#     porcentaje se entienda; sembrado solo con síes y noes, el trozo que da
#     sentido a los otros dos no se pinta nunca.
#   - las TRES direcciones con al menos una cada una, porque una casilla a cero
#     manda `pct` a `None` y eso es otra rama.
#   - CINCO juzgables -pediste otra sesión, se ejecutó la que pediste y esa
#     sesión tiene resultado medido-, que es `MINIMO_JUICIOS`. Con cuatro, el
#     bloque de «quién acertó» se pinta entero por la rama del contador de lo que
#     falta y el veredicto no se llega a leer.
#   - los CUATRO motivos de `_na_del_caso` representados, uno por línea, porque
#     la lista de casos los pinta por separado a propósito: «no lo enviaste», «no
#     pediste nada», «se ejecutó otra cosa» y «no hay resultado» son cuatro cosas
#     distintas que arreglar.
#
# Los días no son libres. Una previsualización juzgable necesita que ese día
# tenga decisión (`i % 11 != 5`) Y sesión medida (`i % 7 in (1, 4)` o
# `i % 3 == 0`), así que los índices están elegidos contra esas dos condiciones y
# no valen otros cualesquiera.
_PREVIAS = [
    # Dos el mismo día, que es el caso que justifica la tabla entera: se miró,
    # no se dijo nada, se cambió una respuesta y se volvió a mirar. La segunda NO
    # tapa a la primera.
    {"dia": 12, "seq": 1, "opina": None, "propuesta": REDUCED},
    {"dia": 12, "seq": 2, "opina": True, "propuesta": REDUCED, "pedida": FULL,
     "ejecutada": True},
    # Ámbar y pidiendo más: el grueso de los desacuerdos juzgables.
    {"dia": 22, "opina": True, "propuesta": REDUCED, "pedida": FULL, "ejecutada": True},
    {"dia": 32, "opina": True, "propuesta": REDUCED, "pedida": FULL, "ejecutada": True},
    {"dia": 42, "opina": True, "propuesta": REDUCED, "pedida": FULL, "ejecutada": True},
    # La forzada en rojo, que va contada aparte y NUNCA como cuarta dirección.
    {"dia": 4, "opina": True, "propuesta": RECOVERY, "pedida": REDUCED,
     "forzada": True, "ejecutada": True},
    # Hacia el otro lado, para que «más suave» no salga a cero.
    {"dia": 6, "opina": True, "propuesta": FULL, "pedida": REDUCED, "ejecutada": True},
    # Los cuatro motivos por los que un desacuerdo no juzga nada:
    #   (1) se miró y no se envió -no hay sesión que juzgar-;
    {"dia": 8, "opina": True, "propuesta": FULL, "pedida": REDUCED, "enviada": False},
    #   (2) se dijo que no y no se pidió otra sesión -salió la del motor-;
    {"dia": 18, "opina": True, "propuesta": FULL},
    {"dia": 25, "opina": True, "propuesta": FULL},
    #   (3) se pidió una y acabó ejecutándose la propuesta;
    {"dia": 39, "opina": True, "propuesta": RECOVERY, "pedida": REDUCED},
    #   (4) se ejecutó lo pedido y esa sesión no tiene resultado medido. El día
    #       2 no cae ni en fuerza ni en bici, que es justo lo que hace falta.
    {"dia": 2, "opina": True, "propuesta": REDUCED, "pedida": FULL, "ejecutada": True},
    # Conformes y miradas sin decir nada, que son la mayoría en la vida real.
    {"dia": 1, "opina": False, "propuesta": FULL},
    # El día 5 no tiene decisión -`i % 11 == 5`-, así que ésta se quedó en
    # previsualización: `decision_id` a nulo y ninguna sesión detrás.
    {"dia": 5, "opina": False, "propuesta": FULL, "enviada": False},
    {"dia": 7, "opina": False, "propuesta": REDUCED},
    {"dia": 13, "opina": False, "propuesta": FULL},
    {"dia": 20, "opina": False, "propuesta": FULL},
    {"dia": 3, "opina": None, "propuesta": FULL},
    {"dia": 10, "opina": None, "propuesta": FULL},
    {"dia": 16, "opina": None, "propuesta": FULL},
]


def _anulacion_ejecutada(i: int) -> str | None:
    """La sesión que de verdad se guardó ese día, cuando se pidió otra y se hizo.

    Existe para que el `kind` de la decisión y el `override_session_type` de la
    previsualización no se escriban por separado. `_sesion_ejecutada` compara
    justo esos dos, así que si se sembraran a mano en dos sitios, un despiste
    dejaría los seis casos juzgables en cero y el bloque de «quién acertó» se
    pintaría entero por la rama del contador sin que nada fallara.
    """
    for p in _PREVIAS:
        if p["dia"] == i and p.get("ejecutada"):
            return p["pedida"]
    return None


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

    Las previsualizaciones son la excepción y van escritas a mano en `_PREVIAS`,
    por el motivo que allí se explica: cada una lleva a una rama distinta y un
    módulo no dice cuál se pierde.
    """
    decisiones: dict[date, Decision] = {}
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
                    # EL CRUDO, QUE ES DE DONDE SALE EL RANKING ENTERO.
                    #
                    # `ejercicios_por_dia` desglosa `raw_json` y NO mira los
                    # `targets`, a propósito: los targets dicen lo que el motor
                    # planea y el ranking pregunta por lo que el cuerpo aguantó.
                    # Sembrar el `WorkoutLog` sin crudo dejaba el desglose vacío,
                    # y con él vacío `seccionRanking` se iba SIEMPRE por la rama
                    # de «no hay ni un ejercicio con días suficientes». O sea que
                    # el arnés llevaba desde que existe dando por pintada una
                    # sección de la que no había visto ni una fila.
                    #
                    # Los tres ejercicios no son intercambiables. `press_banca`
                    # va todos los días de fuerza -es el que NO tiene días sin
                    # él, el que sale el último con el motivo escrito- y los
                    # otros dos se reparten los dos días de la semana, que es lo
                    # que les da variación y permite que salga una `r`. Sin al
                    # menos uno de cada, el ranking se pinta con una sola rama.
                    raw_json=json.dumps(
                        {
                            "exercises": [
                                {
                                    "exercise_template_id": "press_banca",
                                    "title": "Press de banca",
                                },
                                {
                                    "exercise_template_id": (
                                        "remo" if i % 7 == 1 else "sentadilla"
                                    ),
                                    "title": "Remo" if i % 7 == 1 else "Sentadilla",
                                },
                            ]
                        }
                    ),
                )
            )

        # El semáforo. Un día de cada once sin fila, para que la vista tenga que
        # pintar también el "ese día no hubo decisión" y no solo colores.
        if i % 11 != 5:
            luz = _LUCES[i % 5]
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
            # LA SESIÓN PLANIFICADA, CON SU `kind` DENTRO.
            #
            # Aquí ponía solo `{"routine": "empuje"}`, y `PlannedSession.to_dict`
            # escribe las dos claves. La de menos era justo la que
            # `_sesion_ejecutada` lee para saber qué dureza salió de verdad ese
            # día: sin ella devuelve `None` para todas las decisiones sembradas,
            # ningún desacuerdo puede juzgarse nunca y el bloque de «quién
            # acertó» se pinta siempre por la rama de «van 0 de los 5 que hacen
            # falta». O sea que el arnés habría aprobado media vista sin verla.
            #
            # Y el `kind` no es el del color a secas: si ese día se pidió otra
            # sesión y se ejecutó, lo guardado es lo que se pidió. Es lo que
            # separa «discrepé y se hizo lo mío» de «discrepé y salió lo del
            # motor igual», que son los dos casos que la vista NO puede juntar.
            dec = Decision(
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
                planned_session_json=json.dumps(
                    {
                        "routine": "empuje",
                        "kind": _anulacion_ejecutada(i) or _SESION_POR_LUZ[luz],
                    }
                ),
                progression_json=json.dumps(prog),
            )
            ses.add(dec)
            decisiones[d] = dec

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

    # LAS PREVISUALIZACIONES, EN UNA SEGUNDA PASADA Y NO DENTRO DEL BUCLE.
    #
    # Hace falta el `id` de la decisión de ese día, y el `id` no existe hasta que
    # SQLAlchemy manda el INSERT. El `flush` lo fuerza sin cerrar la transacción.
    #
    # `decision_id` nulo no es un descuido: es «lo miré y no llegué a enviarlo»,
    # que es uno de los usos previstos del botón y el primer motivo por el que un
    # desacuerdo no juzga nada.
    ses.flush()
    for p in _PREVIAS:
        d = HOY - timedelta(days=p["dia"])
        luz = _LUCES[p["dia"] % 5]
        dec = decisiones.get(d) if p.get("enviada", True) else None
        pedida = p.get("pedida")
        ses.add(
            Preview(
                date=d,
                seq=p.get("seq", 1),
                answers_json=json.dumps({"fatigue": 3 + (p["dia"] % 5)}),
                light=luz,
                session_type=p["propuesta"],
                # La foto de la decisión que se previsualizó. De aquí saca la
                # vista la regla con la que agrupa, y por eso el `trigger_rule`
                # se calcula igual que en el bucle de arriba en vez de copiarse.
                decision_json=json.dumps(
                    {
                        "light": luz,
                        "trigger_rule": "hrv_baja" if luz != "green" else None,
                    }
                ),
                disagreed=p["opina"],
                disagreement_reason=(
                    "me encuentro mejor de lo que dice" if p["opina"] else None
                ),
                override_session_type=pedida,
                override_routine="empuje" if pedida else None,
                forced_on_red=p.get("forzada", False),
                decision_id=dec.id if dec is not None else None,
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
    """Las nueve respuestas de verdad, tal cual las recibe el móvil."""
    rutas = {
        "portada": ("/api/metrics/portada", {}),
        "concordancia": ("/api/metrics/concordancia", {}),
        "desfase": ("/api/metrics/desfase", {}),
        "impacto": ("/api/metrics/impacto", {}),
        "auditoria": ("/api/metrics/auditoria", {}),
        "percepcion": ("/api/metrics/percepcion", {}),
        "umbral": ("/api/metrics/umbral", {}),
        "calibracion": ("/api/metrics/calibracion", {}),
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

    # EL RANKING, CON ALGÚN VEREDICTO DE LA CORRECCIÓN DENTRO.
    #
    # `correccionNoPintada` en `render_pwa.mjs` compara `p_corregida` y
    # `significativa` contra la pantalla, y recorre las casillas del payload: si
    # el sembrado no trae ni una calculada, el bucle no da ni una vuelta y la
    # comprobación sale verde sin haber mirado nada. Una guarda que no puede
    # dispararse es peor que ninguna, porque además ocupa el sitio de la que sí.
    #
    # Se mira aquí y no allí por el motivo de siempre: si esto se queda a cero,
    # lo que hay que arreglar es el sembrado, y el mensaje tiene que decir eso.
    con_veredicto = [
        c
        for e in salida["ranking"]["ranking"]
        for c in e["por_dia"]
        if c["r"] is not None and c["significativa"] is not None
    ]
    assert con_veredicto, (
        "el ranking sembrado no trae ni una casilla con `r` y `significativa`: "
        "o no hay ejercicio con días suficientes, o `corregir_tanda` ha dejado "
        "de marcar la tanda. Sin ninguna, el arnés no comprueba que el podio "
        "escriba la corrección y aprueba una pantalla que la tire a la basura."
    )

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
        "y no se leen ni `por_dia`, ni `vuelve_el_dia`, ni la frase de cada día."
    )
    assert not u["grafica"]["na"], (
        f"la gráfica del umbral no se dibuja ({u['grafica']['na']!r}), así que "
        f"no se leen ni `puntos`, ni `salidas`, ni `altura_corte`."
    )

    # LA CALIBRACIÓN, CON EL VEREDICTO DICHO Y NO CALLADO.
    #
    # Es la vista más fácil de aprobar sin haberla mirado, porque es la única que
    # se calla a propósito: sin previsualizaciones sembradas, los cuatro bloques
    # salen los cuatro con su motivo escrito -«todavía no has dicho ni que sí ni
    # que no», «van 0 de los 5 que hacen falta»-, la pantalla se pinta entera,
    # no hay un solo `undefined`, y el arnés diría «ok calibracion 9000 bytes»
    # sin haber leído ni una de las claves que esta vista existe para pintar.
    #
    # Cada `assert` de aquí abajo es una rama que se perdería en silencio.
    c = salida["calibracion"]
    assert not c["cuantas"]["na"], (
        f"el recuento de calibración viene por la rama de «no hay bastante» "
        f"({c['cuantas']['na']!r}): el sembrado no deja ni una previsualización "
        f"opinada, y sin denominador no se pinta ni el anillo ni el porcentaje."
    )
    for clave in ("discrepadas", "conformes", "sin_opinar"):
        assert c["cuantas"][clave], (
            f"`{clave}` sale a cero. Los tres trozos del anillo tienen que "
            f"tener filas: el de «sin opinar» es el que explica que el "
            f"porcentaje vaya sobre las opinadas y no sobre las miradas, y a "
            f"cero se pinta un anillo lleno que dice lo contrario."
        )
    vacias = [d["clave"] for d in c["direcciones"]["celdas"] if not d["n"]]
    assert not vacias, (
        f"direcciones a cero: {vacias}. Las tres tienen que tener desacuerdos "
        f"dentro, porque una casilla vacía manda `pct` a `None` y es otra rama."
    )
    # EL CONTADOR DE ANULACIONES, POR LA RAMA QUE PINTA LA TABLA.
    #
    # Mismo argumento que el resto de este bloque, y aquí especialmente fácil de
    # incumplir: si el sembrado no deja ni una anulación, la vista sale con `na`
    # puesto, `bloqueAnulaciones` se va por la rama corta y ni la tabla ni la
    # frase de «van N de 10» se llegan a pintar. El arnés aprobaría un bloque
    # que no ha ejecutado.
    assert not c["anulaciones"]["na"], (
        f"el contador de anulaciones viene por la rama de «todavía nada» "
        f"({c['anulaciones']['na']!r}): el sembrado no deja ni una "
        f"previsualización con `override_session_type`."
    )
    assert c["anulaciones"]["por_regla"], "sin filas no hay tabla que pintar"
    assert any(g["forzadas_en_rojo"] for g in c["anulaciones"]["por_regla"]), (
        "ninguna anulación forzada en rojo en el sembrado: la columna que separa "
        "la subida del día rojo del resto no se pinta nunca."
    )
    assert c["direcciones"]["lectura"], (
        "la lectura de las direcciones se calla: hacen falta tres desacuerdos "
        "para que hable, y sin ella la pantalla enseña tres barras sin frase."
    )
    assert c["direcciones"]["forzadas_en_rojo"], (
        "ninguna forzada en rojo. Es la línea que va aparte de las tres barras "
        "-subir de dureza con el semáforo en rojo- y a cero no se comprueba que "
        "se pinte fuera del recuento en vez de como cuarta casilla."
    )
    assert c["donde"]["lectura"], (
        "los desacuerdos no se agolpan en ninguna regla del sembrado, así que "
        "la frase que dice DÓNDE mirar no se escribe. Suele ser un empate en "
        "cabeza: hacen falta más desacuerdos en una regla que en la siguiente."
    )
    assert c["quien_acerto"]["veredicto"] and not c["quien_acerto"]["na"], (
        f"el veredicto se calla ({c['quien_acerto']['na']!r}). Van "
        f"{c['quien_acerto']['n']} juzgables de los "
        f"{c['quien_acerto']['hacen_falta']} que hacen falta: un desacuerdo solo "
        f"juzga si pediste otra sesión, se ejecutó la que pediste y esa sesión "
        f"tiene resultado medido. Sin veredicto no se leen ni las dos medianas."
    )
    juzgan = [x for x in c["quien_acerto"]["casos"] if not x["na"]]
    no_juzgan = {x["na"] for x in c["quien_acerto"]["casos"] if x["na"]}
    assert juzgan and len(no_juzgan) >= 4, (
        f"la lista de casos no trae las dos clases de fila: {len(juzgan)} que "
        f"juzgan y {len(no_juzgan)} motivos distintos de los que no. Se pintan "
        f"diferente -percentil o motivo- y los cuatro motivos son cuatro cosas "
        f"distintas que arreglar, así que juntarlos en la muestra deja media "
        f"lista sin mirar."
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


def _hrefs_de(lista: str) -> list[str]:
    """Los `href` de UNA lista de `comun.js`, no de todo el fichero.

    Antes bastaba con buscar `href:` en todo `comun.js`, porque había una sola
    lista de enlaces. Desde que existe `AVANZADO` hay dos, y buscar en todo el
    fichero las mezclaría: la barra parecería tener once entradas.
    """
    m = re.search(rf"const {lista} = \[(.*?)\n\];", COMUN, re.S)
    assert m, f"no encuentro `const {lista} = [...]` en comun.js"
    return re.findall(r'href: "([^"]+)"', m.group(1))


def _lleva_a_algo(href: str, vistas: set[str]) -> None:
    pagina, _, ancla = href.partition("#")
    fichero = "index.html" if pagina == "/" else pagina.lstrip("/")
    assert (ESTATICOS / fichero).is_file(), f"`{href}` apunta a {fichero}, que no existe"
    if ancla:
        assert ancla in vistas, (
            f"`{href}` lleva a la vista `{ancla}`, que no está en VISTAS: ese "
            f"toque cae en la vista por defecto sin decir nada"
        )


def test_las_pantallas_de_la_nav_llevan_a_algo_que_existe():
    """Ningún enlace es un callejón y ninguna vista se queda sin enlace.

    ESTE TEST SE REESCRIBIÓ EL 25/09/2026, Y NO PARA AFLOJARLO. Antes exigía una
    entrada en la barra por cada vista, porque así estaba hecha la aplicación:
    nueve pestañas. Ese día la barra pasó a cuatro y seis vistas técnicas se
    fueron detrás de «Más», a una página propia. La regla de «una por vista» se
    habría cumplido volviendo a meter las nueve, que es justo lo que se quería
    quitar.

    Lo que el test protegía no era el número, eran dos cosas, y siguen aquí:
    que ningún enlace lleve a nada, y que ninguna vista quede huérfana. Ahora
    una vista puede estar en la barra O en Avanzado; lo que no puede es no estar
    en ninguno de los dos.
    """
    vistas = set(_claves_de_objeto(METRICAS, "VISTAS"))
    assert vistas, "no encuentro las claves de VISTAS en metricas.js"
    barra = _hrefs_de("PANTALLAS")
    avanzado = _hrefs_de("AVANZADO")

    assert "/" in barra, (
        "la barra ha perdido el enlace al check-in, que es la pantalla que se "
        "abre todas las mañanas"
    )
    for href in barra + avanzado:
        _lleva_a_algo(href, vistas)

    alcanzables = {h.partition("#")[2] for h in barra + avanzado if "#" in h}
    huerfanas = vistas - alcanzables
    assert not huerfanas, (
        f"estas vistas de metricas.js no tienen enlace ni en la barra ni en "
        f"Avanzado: {sorted(huerfanas)}. Existen y no hay forma de llegar a ellas"
    )


def test_la_barra_es_corta():
    """La promesa del cambio del 25/09/2026, escrita donde se pueda romper.

    La barra tenía nueve pestañas y en un móvil no cabían: había que deslizarla
    de lado para llegar a la última. Se dejó en cuatro. Sin este test, la
    siguiente vista nueva entraría en la barra por costumbre -es donde estaban
    todas- y en tres meses volveríamos a las nueve sin que nadie lo decidiera.

    Cinco y no cuatro: deja sitio a una pantalla principal más, que es una
    decisión razonable. Una sexta ya no lo es sin pensarlo.
    """
    barra = _hrefs_de("PANTALLAS")
    assert len(barra) <= 5, (
        f"la barra tiene {len(barra)} entradas: {barra}. Lo que no es de uso "
        f"diario va en `AVANZADO`, detrás de «Más»"
    )


def test_ninguna_vista_esta_a_la_vez_en_la_barra_y_en_avanzado():
    """Dos caminos a lo mismo en dos sitios distintos acaban diciendo cosas
    distintas: el título de una tarjeta de Avanzado y la etiqueta corta de la
    barra se escriben por separado y se separan con el tiempo."""
    barra = set(_hrefs_de("PANTALLAS"))
    avanzado = set(_hrefs_de("AVANZADO"))
    assert not barra & avanzado, f"en los dos sitios: {sorted(barra & avanzado)}"


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


def _rellenar(
    tmp_path,
    hoy: dict,
    acciones: list[dict],
    borrador=None,
    respuesta=None,
    previsualizaciones=None,
    desacuerdo=None,
    recalculo=None,
) -> dict:
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
            {
                "hoy": hoy,
                "acciones": acciones,
                "borrador": borrador,
                # Lo que contesta el POST. `None` deja el de siempre, que es un
                # día decidido y salido bien.
                "respuesta": respuesta,
                # Y lo que contesta `/api/preview`, una entrada por llamada.
                # `None` deja el defecto del arnés, que sube `seq` con cada una.
                "previsualizaciones": previsualizaciones,
                "desacuerdo": desacuerdo,
                # Lo que contesta el recálculo al abrir. `None` deja el de
                # casi todos los días: nada que recalcular, sin aviso.
                "recalculo": recalculo,
            },
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


def _fallar(tmp_path, **estados) -> str:
    """Rellena, envía, y devuelve el HTML de la tarjeta de un día NO decidido."""
    salida = _rellenar(
        tmp_path, _hoy(),
        [
            {"tipo": "deslizar", "key": "fatigue", "valor": 3},
            {"tipo": "responder", "key": "wants_to_train", "respuesta": "si"},
            {"tipo": "responder", "key": "will_train", "respuesta": "si"},
            {"tipo": "enviar"},
        ],
        respuesta={
            "decided": False,
            "error": "UNIQUE constraint failed: notifications.date, "
                     "notifications.kind",
            **estados,
        },
    )
    assert salida["resultado"] is not None, "no se ha pintado ninguna tarjeta"
    assert "mal" in salida["resultado"]["clase"]
    return salida["resultado"]["html"]


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_si_el_mensaje_SI_se_envio_la_tarjeta_del_fallo_no_dice_que_no(tmp_path):
    """La pantalla del 18 de septiembre de 2026, que decía lo contrario de todo.

    Ese día el fallo llegó TARDE: la rutina estaba escrita en Hevy, el Telegram
    estaba entregado en el móvil, y lo que reventó fue apuntar el aviso en la
    tabla `notifications`. El `rollback` se llevó la fila de la base de datos y
    no se llevó ni la rutina ni el mensaje, porque los dos están fuera de la
    transacción.

    Y la tarjeta, que tenía una frase fija, dijo que no se había tocado la rutina
    de Hevy ni se había enviado ningún mensaje. Con el mensaje en la pantalla de
    al lado. Eso no es un texto impreciso: es la instrucción exacta para que el
    usuario lo repita todo a mano y acabe con la rutina escrita dos veces.
    """
    html = _fallar(tmp_path, hevy="ok", telegram="sent")

    assert "ningún mensaje" not in html and "ningun mensaje" not in html, (
        "ha vuelto la frase fija. La tarjeta no puede afirmar que no se envió "
        "nada: cuando el fallo es tardío, se envió"
    )
    assert "sí se ha enviado el mensaje" in html, (
        f"el servidor ha dicho telegram='sent' y la tarjeta no lo cuenta:\n{html}"
    )
    assert "sí se ha escrito la rutina" in html
    # El detalle técnico sigue estando: lo que sobraba era la afirmación, no el
    # volcado del fallo.
    assert "UNIQUE constraint failed" in html
    assert "no se sabe" not in html


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_un_fallo_sin_rastro_dice_que_no_se_sabe_y_no_que_no_se_toco(tmp_path):
    """La otra mitad, que es la que hace honrada a la primera.

    Si la tarjeta pintara siempre lo optimista cuando no hay dato, habríamos
    cambiado una mentira por otra. Sin `hevy` ni `telegram` en la respuesta el
    estado es DESCONOCIDO, y desconocido no es «no se ha tocado»: hay que
    decirlo y mandar al usuario a mirar antes de repetir.
    """
    html = _fallar(tmp_path)

    # En los `<dd>` y no en el HTML entero: el aviso de abajo entrecomilla la
    # frase para explicarla, y contarlo también haría que este número dependiera
    # de cómo esté redactado el aviso.
    assert html.count("<dd>no se sabe</dd>") == 2, (
        f"con la respuesta muda hay que decirlo en los dos canales:\n{html}"
    )
    assert "no vaya a hacerse dos veces" in html, (
        "decir «no se sabe» y no decir qué hacer con eso deja al usuario en el "
        "mismo sitio que la frase falsa"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_un_fallo_temprano_si_puede_decir_que_no_se_toco_nada(tmp_path):
    """Y cuando de verdad no se tocó nada, se dice, sin el aviso de ir a mirar.

    Es la rama de Garmin: se cae leyendo las métricas, antes de escribir en
    ningún sitio, y el servidor manda `skipped` en los dos. Ahí la tarjeta sí
    puede tranquilizar, y si además soltara el «mira el móvil por si acaso»
    estaría mandando a mirar todos los días por nada.
    """
    html = _fallar(tmp_path, hevy="skipped", telegram="skipped")

    assert "<dd>no se ha tocado</dd>" in html
    assert "<dd>no se ha enviado</dd>" in html
    assert "no se sabe" not in html
    assert "no vaya a hacerse dos veces" not in html


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


# ---------------------------------------------------------------------------
# El recálculo al abrir (25/09/2026)
# ---------------------------------------------------------------------------
#
# Un ámbar «sin datos» prometía recalcular cuando el reloj subiera la noche, y
# los dos trabajos con hora que lo cumplían caían con el equipo dormido. Abrir
# la app es lo único que garantiza un servidor despierto.


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_con_el_checkin_hecho_abrir_la_app_pide_el_recalculo(tmp_path):
    """Y lo pide con POST: cuando actúa, escribe en Hevy y manda un Telegram."""
    salida = _rellenar(tmp_path, _hoy(submitted=True, values={"fatigue": 3}), [])
    assert salida["veces_recalculado"] == 1
    assert salida["metodo_recalculo"] == "POST"


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_sin_checkin_no_se_pide_nada(tmp_path):
    """Sin check-in no hay decisión a ciegas que rehacer, y a esa hora la
    pantalla está para contestar, no para mover el día."""
    salida = _rellenar(tmp_path, _hoy(), [])
    assert salida["veces_recalculado"] == 0


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_lo_que_contesta_el_recalculo_se_pinta_tal_cual(tmp_path):
    """La frase y el tono son del servidor: la pantalla no calcula."""
    salida = _rellenar(
        tmp_path, _hoy(submitted=True, values={"fatigue": 3}), [],
        recalculo={"respuesta": {
            "estado": "recalculado", "tono": "bien",
            "aviso": "Ya han llegado la variabilidad y lo que duermes: el día "
                     "se ha recalculado y pasa de ámbar a verde.",
        }},
    )
    (arriba, *_resto) = salida["avisos"]
    assert arriba == {
        "clase": "aviso bien",
        "texto": "Ya han llegado la variabilidad y lo que duermes: el día se ha "
                 "recalculado y pasa de ámbar a verde.",
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_casi_siempre_no_hay_nada_que_contar_y_no_se_pinta_nada(tmp_path):
    """Un aviso que saliera cada vez que se abre se aprendería a no leer."""
    salida = _rellenar(tmp_path, _hoy(submitted=True, values={"fatigue": 3}), [])
    assert [a["texto"] for a in salida["avisos"]] == [
        "Hoy ya has hecho el check-in. Si lo envías otra vez se recalcula el día "
        "con las respuestas nuevas."
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
@pytest.mark.parametrize(
    "recalculo, causa",
    [({"status": 500}, "el servidor ha contestado 500"), ({"red": True}, "sin red")],
    ids=["servidor", "red"],
)
def test_si_no_se_puede_mirar_se_dice_y_no_se_calla(tmp_path, recalculo, causa):
    """Callar aquí es decir «no hacía falta» el día que seguía a ciegas."""
    salida = _rellenar(
        tmp_path, _hoy(submitted=True, values={"fatigue": 3}), [],
        recalculo=recalculo,
    )
    (arriba, *_resto) = salida["avisos"]
    assert arriba["clase"] == "aviso mal"
    assert "No se ha podido comprobar si el reloj ya ha subido la noche" in arriba["texto"]
    assert causa in arriba["texto"]


# ---------------------------------------------------------------------------
# «Hoy» abre con la decisión cuando el día ya está decidido (25/09/2026)
# ---------------------------------------------------------------------------


def _decidida(**cambios) -> dict:
    """`decision_de_hoy` con la forma que manda `/api/checkin/today`."""
    base = {
        "decided": True, "light": "amber", "session": "Día 3", "kind": "reduced",
        "hevy": "ok", "telegram": "sent", "problems": [],
        "cuando": "Decidido a las 06:57 con tu check-in.",
    }
    base.update(cambios)
    return base


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_con_el_dia_decidido_abre_con_la_decision_y_el_formulario_plegado(tmp_path):
    """Lo que se viene a mirar a esa hora es qué toca, no las respuestas de la
    mañana. Antes había que buscarlo en Telegram."""
    salida = _rellenar(tmp_path, _hoy(
        submitted=True, values={"fatigue": 3}, decision_de_hoy=_decidida(),
    ), [])

    assert salida["formulario_oculto"] is True
    assert salida["cambiar_visible"] is True
    assert "semaforo-amber" in salida["resultado"]["clase"]
    html = salida["resultado"]["html"]
    assert "Día 3" in html and "Decidido a las 06:57 con tu check-in." in html


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_la_tarjeta_dice_hevy_y_telegram_en_palabras_y_no_en_codigo(tmp_path):
    """Pintaba «Hevy: ok» y «Telegram: sent» tal cual, y un `null` como «—», que
    no distingue «no se tocó» de «no se sabe»."""
    salida = _rellenar(tmp_path, _hoy(
        submitted=True, values={"fatigue": 3},
        decision_de_hoy=_decidida(telegram=None),
    ), [])
    html = salida["resultado"]["html"]
    assert "sí se ha escrito la rutina" in html
    assert "<dd>ok</dd>" not in html
    assert "no se sabe" in html, "un estado que falta se dice, no se rellena"


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_cambiar_mis_respuestas_despliega_el_formulario(tmp_path):
    salida = _rellenar(tmp_path, _hoy(
        submitted=True, values={"fatigue": 3}, decision_de_hoy=_decidida(),
    ), [{"tipo": "cambiar-respuestas"}])
    assert salida["formulario_oculto"] is False
    assert salida["cambiar_visible"] is False


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_sin_decision_todavia_abre_con_el_formulario(tmp_path):
    """Check-in guardado y decisión que no salió: no hay nada que enseñar
    arriba, y esconder el formulario dejaría la pantalla vacía."""
    salida = _rellenar(tmp_path, _hoy(
        submitted=True, values={"fatigue": 3}, decision_de_hoy=None,
    ), [])
    assert salida["formulario_oculto"] is False
    assert salida["cambiar_visible"] is False
    assert salida["resultado"] is None


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_si_el_recalculo_cambia_el_dia_la_tarjeta_se_repinta_con_su_aviso(tmp_path):
    """La tarjeta de delante deja de ser la vigente al recalcular. Y el aviso va
    dentro de ella: con el formulario plegado, metido ahí no lo vería nadie."""
    salida = _rellenar(
        tmp_path,
        _hoy(submitted=True, values={"fatigue": 3}, decision_de_hoy=_decidida()),
        [],
        recalculo={"respuesta": {
            "estado": "recalculado", "tono": "bien",
            "aviso": "Ya han llegado la variabilidad y lo que duermes: el día se "
                     "ha recalculado y pasa de ámbar a verde.",
            "decision_de_hoy": _decidida(light="green", cuando="Decidido a las "
                                         "09:12 al llegar la noche del reloj."),
        }},
    )
    assert "semaforo-green" in salida["resultado"]["clase"]
    assert "pasa de ámbar a verde" in salida["resultado"]["html"]
    assert salida["avisos"][0]["clase"] == "aviso bien"


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


# ---------------------------------------------------------------------------
# Previsualizar: mirar qué decidiría el sistema SIN que pase nada
# ---------------------------------------------------------------------------
#
# Lo que se compra con este botón es calibración: contestar, ver qué sale, y con
# el tiempo que lo que dice el sistema y lo que uno sabe de sí mismo converjan.
# Lo que se arriesga es la confusión más cara de la aplicación -creerse que el
# día ya está mandado cuando solo se ha mirado, o al revés-, y por eso casi todo
# lo que se comprueba aquí abajo es la CABECERA y no el semáforo.
#
# El semáforo lo calcula el motor y tiene sus propios tests. Lo que no tiene
# tests en ninguna otra parte es que esta pantalla no mienta sobre lo que ha
# pasado, y eso solo se puede mirar pulsando el botón.


def _mirar(tmp_path, acciones=None, **kwargs) -> dict:
    """Rellena lo mínimo para que el formulario esté completo y previsualiza."""
    return _rellenar(
        tmp_path,
        _hoy(),
        [
            {"tipo": "deslizar", "key": "fatigue", "valor": 7},
            {"tipo": "responder", "key": "wants_to_train", "respuesta": "no"},
            {"tipo": "responder", "key": "will_train", "respuesta": "si"},
            {"tipo": "previsualizar"},
        ]
        + (acciones or []),
        **kwargs,
    )


def test_previsualizar_no_puede_ser_un_boton_de_enviar():
    """`type="button"`, y este test vale por todo lo demás de este bloque.

    Dentro de un `<form>`, un botón SIN `type` es un botón de envío. Es el
    defecto del HTML, no una rareza. O sea que olvidar el atributo convierte
    «previsualizar» en «guardar el check-in, escribir la rutina en Hevy y mandar
    el Telegram», y lo hace en silencio: el botón sigue diciendo Previsualizar.

    Es el fallo más caro que cabe en `index.html` y cabe en una palabra. Se mira
    leyendo el HTML y no con el arnés porque el arnés fabrica un `<div>` por cada
    `id` y no conoce ni la etiqueta ni sus atributos.
    """
    html = (ESTATICOS / "index.html").read_text(encoding="utf-8")

    m = re.search(r"<button([^>]*\bid=\"previsualizar\"[^>]*)>", html)
    assert m, "no hay ningún <button id=\"previsualizar\"> en index.html"
    assert 'type="button"' in m.group(1), (
        "el botón de previsualizar no lleva `type=\"button\"`. Dentro de un "
        "<form>, eso lo convierte en un botón de ENVÍO: previsualizar guardaría "
        "el check-in, escribiría en Hevy y mandaría el Telegram"
    )

    # Y encima del de enviar: leídos de arriba abajo son los dos pasos en el
    # orden en que se dan. Al revés, el que solo mira queda debajo del que ya lo
    # hizo todo, que es la posición del botón que se pulsa por error.
    assert html.index('id="previsualizar"') < html.index('id="enviar"')
    assert html.index('id="previsualizacion"') < html.index('id="resultado"'), (
        "la tarjeta de previsualizar tiene que tener su PROPIO hueco, separado "
        "del de `pintarResultado`"
    )


def test_el_boton_esta_enganchado_a_algo(tmp_path):
    """Que al pulsarlo pase algo. Parece tonto y no lo fue.

    `previsualizar()` existió escrita entera -con su tarjeta, su cabecera y su
    desacuerdo- y sin una sola línea que la enganchara al botón. En el móvil eso
    es un botón que no hace absolutamente nada: sin error en consola, sin aviso,
    sin nada. Y todos los tests de la tarjeta habrían pasado igual si el arnés
    llamara a la función por dentro en vez de pulsar el botón.
    """
    salida = _mirar(tmp_path)

    assert salida["veces_previsualizado"] == 1, (
        "pulsar «Previsualizar» no ha llegado a preguntarle nada al servidor: "
        "el botón no está enganchado a `previsualizar()`"
    )


def test_previsualizar_no_envia_el_checkin(tmp_path):
    """La línea entera: mirar no es hacer.

    Si esto se rompiera, cada previsualización guardaría un check-in. Y no daría
    ningún error: el día quedaría decidido, la rutina escrita y el Telegram
    enviado, con la pantalla enseñando una tarjeta que dice que no ha pasado
    nada.
    """
    salida = _mirar(tmp_path)

    assert salida["veces_enviado"] == 0, (
        "previsualizar ha llamado a `/api/checkin`: eso guarda el check-in, "
        "escribe en Hevy y manda el Telegram"
    )
    assert salida["resultado"] is None, (
        "previsualizar ha pintado la tarjeta del ENVÍO, que es la que dice que "
        "el día está decidido"
    )


def test_la_tarjeta_empieza_diciendo_que_no_se_ha_escrito_nada(tmp_path):
    """La condición explícita del encargo: «que no me quede duda».

    Y el desglose debajo, hecho por hecho, porque es lo que hace que la frase
    valga algo: una cabecera tranquilizadora sin nada que la sostenga es
    exactamente lo que había en `pintarResultado` el 18 de septiembre de 2026,
    afirmando que no se había mandado ningún mensaje con el Telegram ya en el
    móvil.
    """
    salida = _mirar(tmp_path)

    tarjeta = salida["previsualizacion"]
    assert tarjeta is not None, "no se ha pintado ninguna tarjeta"

    texto = tarjeta["texto"]
    assert "Todavía no se ha escrito nada" in texto
    assert "Ojo" not in texto

    # Los cinco hechos, uno por uno. La cabecera se calcula a partir de ellos,
    # así que comprobar solo la cabecera dejaría sin mirar de dónde sale.
    assert "no se han guardado" in texto, "falta el hecho de las respuestas"
    assert "no se ha tomado" in texto, "falta el hecho de la decisión"
    assert texto.count("sin tocar") == 2, "faltan Hevy y/o Telegram"
    # Y la quinta, la que impide que la cabecera sea una mentira pequeña: la
    # previsualización SÍ se apunta, y callarlo para que el mensaje quedara más
    # redondo sería empezar esta pantalla haciendo lo que vino a arreglar.
    assert "queda apuntada" in texto
    assert "no se sabe" not in texto


@pytest.mark.parametrize(
    "toque",
    [
        {"checkin_guardado": True},
        {"decision_guardada": True},
        {"ejecutado": True},
        {"hevy": "ok"},
        {"telegram": "sent"},
        # Y los que NO son una afirmación. `null` no es «no se tocó»: es que el
        # servidor no lo ha dicho, y esta es la línea de la pantalla que menos
        # puede permitirse afirmar de más.
        {"hevy": None},
        {"telegram": None},
        {"checkin_guardado": None},
    ],
)
def test_si_algo_no_consta_como_intacto_la_tarjeta_deja_de_tranquilizar(
    tmp_path, toque
):
    """Cualquiera de los cinco hechos apaga la frase buena. Los cinco.

    Escrito como parametrización y no como un test con cinco assertions porque
    lo que puede fallar aquí es que UNO de los cinco se deje de mirar, y en un
    solo test eso se ve como un fallo y no como cuál.
    """
    respuesta = {
        "day": "2026-09-15",
        "ejecutado": False,
        "checkin_guardado": False,
        "decision_guardada": False,
        "hevy": "sin tocar",
        "telegram": "sin tocar",
        "previsualizacion_guardada": True,
        "preview_id": 1,
        "seq": 1,
        "revision": False,
        "light": "green",
        "trigger_rule": None,
        "decision": {"session": {"title": "Día 1", "kind": "full"}},
    }
    respuesta.update(toque)

    salida = _mirar(
        tmp_path, previsualizaciones=[{"status": 200, "respuesta": respuesta}]
    )

    texto = salida["previsualizacion"]["texto"]
    assert "Ojo: esto no ha sido solo mirar" in texto, (
        f"con {toque!r} la tarjeta sigue diciendo que no se ha escrito nada"
    )
    assert "Todavía no se ha escrito nada" not in texto


def test_el_cuerpo_previsualizado_lleva_las_respuestas_y_nada_mas(tmp_path):
    """Lo que viaja al mirar son las respuestas, sin comentario y sin día.

    El comentario no viaja porque `/api/preview` no lo guarda en ninguna parte:
    sería un campo que sale del móvil y se tira, y la regla de esta casa es que
    no haya campos así. El día no viaja porque lo pone el reloj del servidor, y
    que las dos rutas lo resuelvan igual es lo único que garantiza que lo que se
    mira y lo que se manda sean del mismo día.
    """
    salida = _mirar(tmp_path)

    cuerpo = salida["cuerpos_previsualizados"][0]
    assert cuerpo["fatigue"] == 7
    # El mismo `is False` que en el envío, y por el mismo motivo: `0 == False`
    # en Python, así que un `== False` daría verde con el fallo puesto.
    assert cuerpo["wants_to_train"] is False
    assert cuerpo["will_train"] is True
    assert "comments" not in cuerpo, (
        "el comentario viaja al previsualizar y el servidor no lo guarda"
    )
    assert "day" not in cuerpo, "el día lo pone el reloj del servidor"


def test_se_puede_mirar_con_respuestas_sin_contestar(tmp_path):
    """El botón NO se pone gris cuando falta algo, al revés que el de enviar.

    Es deliberado y es media razón de que el botón exista: «¿qué pasa si hoy no
    contesto el dolor lumbar?» es una pregunta legítima -el motor tiene camino
    para las señales que faltan, las anota como saltadas y lo dice- y ésta es la
    única pantalla donde se puede hacer sin consecuencias. Gris hasta rellenarlo
    todo, esa pregunta se queda sin forma de hacerse.
    """
    salida = _rellenar(tmp_path, _hoy(), [
        {"tipo": "deslizar", "key": "fatigue", "valor": 7},
        {"tipo": "previsualizar"},
    ])

    assert salida["previsualizar_deshabilitado"] is False, (
        "el botón de previsualizar se ha puesto gris por faltar respuestas"
    )
    assert salida["enviar_deshabilitado"] is True, (
        "el de ENVIAR sí tiene que estar gris: si los dos se comportan igual, "
        "esta diferencia se ha perdido"
    )
    assert salida["veces_previsualizado"] == 1

    # Y lo que contesta el motor cuando le faltan señales se ve, que es lo que
    # da sentido a haber podido preguntar.
    assert "No se han podido mirar" in salida["previsualizacion"]["texto"]
    assert "dolor_lumbar: falta lower_back_pain" in salida["previsualizacion"]["texto"]


def test_la_segunda_mirada_no_tapa_a_la_primera(tmp_path):
    """«No quiero que la segunda tape a la primera: la diferencia es el dato».

    La tabla es de añadir y el servidor manda `revision` ya resuelto. Lo que se
    comprueba aquí es que la pantalla lo DICE: dos filas guardadas que en el
    móvil se vean igual que una son dos filas que nadie va a ir a comparar.
    """
    salida = _mirar(tmp_path, acciones=[
        {"tipo": "deslizar", "key": "fatigue", "valor": 2},
        {"tipo": "previsualizar"},
    ])

    assert salida["veces_previsualizado"] == 2
    cuerpos = salida["cuerpos_previsualizados"]
    assert cuerpos[0]["fatigue"] == 7 and cuerpos[1]["fatigue"] == 2, (
        "las dos miradas tienen que llevar las respuestas con las que se hizo "
        "cada una: si no, la diferencia entre ellas no se puede medir"
    )

    texto = salida["previsualizacion"]["texto"]
    assert "No es la primera vez que miras hoy" in texto
    assert "la nº 2 de hoy" in texto, (
        "la tarjeta no dice cuál de las de hoy es"
    )


def test_enviar_retira_la_tarjeta_de_mirar(tmp_path):
    """La tarjeta afirma algo sobre un MOMENTO, y ese momento se acaba al enviar.

    «Todavía no se ha escrito nada» deja de ser verdad exactamente cuando se
    pulsa Enviar. Dejarla en pantalla al lado del resultado es el mismo fallo del
    18 de septiembre con otra ropa: una frase que fue cierta cuando se escribió y
    que nadie vuelve a mirar.
    """
    salida = _mirar(tmp_path, acciones=[{"tipo": "enviar"}])

    assert salida["previsualizacion"] is None, (
        "la tarjeta de «todavía no se ha escrito nada» sigue en pantalla "
        "después de enviar"
    )
    assert salida["resultado"] is not None, "no se ha pintado el resultado"


def test_el_desacuerdo_se_declara_en_dos_toques_y_viaja_con_su_id(tmp_path):
    """Discrepar cuesta dos toques, y va contra la fila que se está mirando.

    Los dos toques no son ceremonia: en uno solo, cada tarjeta llevaría una
    invitación permanente a discrepar encima. El encargo dice lo contrario con
    todas las letras -«el objetivo no es forzar el resultado que me apetece»- y
    que haya que buscarlo es parte de que signifique algo.

    Y el `preview_id` en la URL es lo que impide que el juicio se pegue a una
    tarjeta que nadie vio: volver a previsualizar relee Garmin y vuelve a correr
    el motor, así que la fila nueva puede traer OTRA decisión.
    """
    salida = _mirar(tmp_path, acciones=[
        {"tipo": "abrir-desacuerdo"},
        {"tipo": "motivo", "texto": "el lumbar está bien hoy"},
        {"tipo": "guardar-desacuerdo"},
    ])

    assert salida["cuerpo_desacuerdo"] == {
        "disagreed": True,
        "reason": "el lumbar está bien hoy",
    }
    assert salida["url_desacuerdo"].endswith("/api/preview/1/desacuerdo"), (
        f"el desacuerdo ha ido a {salida['url_desacuerdo']!r}"
    )
    # Y no ha enviado nada por el camino.
    assert salida["veces_enviado"] == 0
    assert salida["veces_previsualizado"] == 1, (
        "guardar el desacuerdo ha vuelto a previsualizar: eso subiría `seq` y "
        "marcaría una revisión que nadie hizo"
    )


def test_un_motivo_en_blanco_viaja_como_nada_y_se_dice(tmp_path):
    """El motivo NO es obligatorio, y un motivo vacío no viaja como espacios.

    Exigirlo dejaría sin registrar justo los días con prisa, que no son una
    muestra al azar. Y un desacuerdo sin explicación sigue sirviendo para las dos
    medidas que se cuentan: cuántas veces y en qué umbral.
    """
    salida = _mirar(
        tmp_path,
        acciones=[
            {"tipo": "abrir-desacuerdo"},
            {"tipo": "motivo", "texto": "   "},
            {"tipo": "guardar-desacuerdo"},
        ],
    )

    assert salida["cuerpo_desacuerdo"] == {"disagreed": True, "reason": None}, (
        "un motivo en blanco tiene que viajar como `null` y no como espacios"
    )
    texto = salida["previsualizacion"]["texto"]
    assert "Queda apuntado que no estás de acuerdo, sin motivo escrito" in texto
    assert "Lo que decide sigue siendo lo que envíes" in texto


def test_la_confirmacion_dice_lo_guardado_y_no_lo_tecleado(tmp_path):
    """Se repinta desde la RESPUESTA, no desde la caja de texto.

    Aquí el servidor contesta un motivo DISTINTO del que se escribió, que es la
    única forma de distinguir las dos cosas: mientras coincidan, una pantalla que
    repite lo tecleado y una que lee lo guardado se ven exactamente igual.

    Y no es un supuesto de laboratorio. `marcar_desacuerdo` ya normaliza -un
    motivo de solo espacios se guarda como nada-, y cualquier recorte o limpieza
    que se le añada mañana entra por aquí. Una confirmación que repitiera el
    cuerpo enseñaría un motivo apuntado que en la base de datos no está, y esa
    diferencia no se descubre hasta que alguien va a leer la columna.
    """
    salida = _mirar(
        tmp_path,
        acciones=[
            {"tipo": "abrir-desacuerdo"},
            {"tipo": "motivo", "texto": "lo que tecleé"},
            {"tipo": "guardar-desacuerdo"},
        ],
        desacuerdo={"respuesta": {
            "day": "2026-09-15", "preview_id": 1, "seq": 1,
            "disagreed": True,
            "disagreement_reason": "lo que el servidor guardó",
            "ejecutado": False, "checkin_guardado": False,
            "decision_guardada": False,
            "hevy": "sin tocar", "telegram": "sin tocar",
        }},
    )

    assert salida["cuerpo_desacuerdo"]["reason"] == "lo que tecleé"
    texto = salida["previsualizacion"]["texto"]
    assert "«lo que el servidor guardó»" in texto, (
        "la confirmación repite lo tecleado: enseñaría un motivo apuntado que "
        "en la base de datos no está"
    )
    assert "lo que tecleé" not in texto


def test_un_desacuerdo_que_no_se_guarda_lo_dice(tmp_path):
    """Y deja el botón otra vez pulsable.

    Un desacuerdo que se pierde no se nota hoy -la pantalla parecería haberlo
    recogido- y sale como un cero limpio dentro de seis meses, midiendo cuántas
    veces se discrepó. Es el peor tipo de fallo que puede tener esta pantalla:
    silencioso y que solo estropea el dato que vino a recoger.
    """
    salida = _mirar(
        tmp_path,
        acciones=[
            {"tipo": "abrir-desacuerdo"},
            {"tipo": "motivo", "texto": "no lo veo"},
            {"tipo": "guardar-desacuerdo"},
        ],
        desacuerdo={"status": 500, "respuesta": {"detail": "la base está caída"}},
    )

    # SU bloque, no la tarjeta entera. Leía toda la pantalla, y eso la ató a
    # que ningún otro texto de la tarjeta usara nunca la frase «Queda
    # apuntado»: el día que se añadió el control de qué sesión hacer, su texto
    # de ayuda la usó para hablar de otra cosa y esta guarda empezó a fallar
    # sin que el desacuerdo tuviera nada malo. Acotada, vigila lo suyo.
    texto = salida["desacuerdo_texto"]
    assert texto is not None, "no hay bloque de desacuerdo que mirar"
    assert "NO se ha guardado el desacuerdo" in texto
    assert "Queda apuntado" not in texto, (
        "la pantalla ha dicho que lo apuntó y no lo apuntó"
    )
    # El botón vuelve a estar disponible: si no, el desacuerdo se pierde para
    # siempre por un corte de un segundo.
    assert "Guardar el desacuerdo" in texto


def test_sin_preview_id_no_se_ofrece_declarar_el_desacuerdo(tmp_path):
    """Un botón que no puede hacer su trabajo es peor que no tener botón.

    Sin `preview_id` no hay contra qué guardarlo. Pintarlo igual daría un botón
    que al pulsarse manda un POST a `/api/preview/undefined/desacuerdo`.
    """
    salida = _mirar(tmp_path, previsualizaciones=[{"status": 200, "respuesta": {
        "day": "2026-09-15",
        "ejecutado": False, "checkin_guardado": False,
        "decision_guardada": False,
        "hevy": "sin tocar", "telegram": "sin tocar",
        "previsualizacion_guardada": False,
        "light": "green", "trigger_rule": None,
        "decision": {"session": {"title": "Día 1", "kind": "full"}},
    }}])

    assert "abrir-desacuerdo" not in salida["previsualizacion"]["html"]
    # Y que no se haya apuntado también se dice, en vez de callarlo.
    assert "no se ha llegado a apuntar" in salida["previsualizacion"]["texto"]


def test_una_mirada_que_falla_sigue_diciendo_lo_que_no_se_ha_tocado(tmp_path):
    """El 502 de Garmin, que es cuando más falta hace la cabecera.

    Un error es exactamente el momento en que uno se pregunta si ha pasado algo,
    y es el momento en que esta clase de pantalla se calla. El servidor manda el
    desglose en las tres salidas -la buena, el 409 y el 502- para esto.
    """
    salida = _mirar(tmp_path, previsualizaciones=[{"status": 502, "respuesta": {
        "detail": {
            "error": "no se ha podido leer Garmin, así que no hay nada que "
                     "previsualizar: 429 Too Many Requests",
            "ejecutado": False, "checkin_guardado": False,
            "decision_guardada": False,
            "hevy": "sin tocar", "telegram": "sin tocar",
        },
    }}])

    tarjeta = salida["previsualizacion"]
    assert tarjeta is not None, "un 502 no ha pintado nada: la pantalla se calla"
    assert "mal" in tarjeta["clase"]

    texto = tarjeta["texto"]
    assert "No se ha podido previsualizar (502)" in texto
    assert "429 Too Many Requests" in texto, (
        "el motivo no se ve: sin él, esto manda a buscar a ciegas"
    )
    # Y lo que de verdad importa del error: que no ha pasado nada.
    assert "Todavía no se ha escrito nada" in texto
    # `previsualizacion_guardada` no viene en el 502 -no se llegó a apuntar
    # nada-, y eso se dice como lo que es: no se sabe, no que sí.
    assert "no se sabe" in texto


def test_la_tarjeta_pinta_las_frases_del_motor_y_no_las_suyas(tmp_path):
    """Ni un número se calcula aquí.

    El semáforo, el motivo, la sesión, los cambios de carga y la bici llegan ya
    redactados por quien tiene el `config.yaml`, el histórico y los tests. Esta
    pantalla elige dónde va cada frase y no escribe ninguna: si empezara a
    redactar, habría dos sitios donde se explica el mismo día y uno de los dos se
    quedaría viejo sin que nada lo dijera.
    """
    salida = _mirar(tmp_path)
    texto = salida["previsualizacion"]["texto"]

    # El porqué del color: la regla que disparó CON su detalle, y las demás.
    assert "fatiga_alta: fatigue 7 ≥ 6" in texto
    assert "sueño_corto: sleep 5.1 h < 6 h" in texto
    assert "Ayer fue día de pierna" in texto

    # La sesión, su dureza en castellano y los recortes, en tres listas.
    assert "Día 3 · Pierna" in texto
    assert "sesión reducida" in texto
    assert "Sentadilla: 4×5 → 3×5" in texto
    assert "Peso muerto rumano" in texto

    # La puerta cerrada se dice aunque no suba nada, y sobre todo entonces:
    # «hoy no sube nada» y «hoy no PUEDE subir nada, y este es el motivo» son la
    # misma pantalla en blanco con dos significados opuestos.
    assert "La progresión está cerrada mientras el día no sea verde" in texto

    # La bici entera, incluido de dónde sale el punto de partida.
    assert "Rodaje suave" in texto and "40–55 min" in texto
    assert "Punto de partida: 50 min, de tus 4 últimas salidas" in texto
    assert "Día ámbar: se recorta un escalón" in texto

    # Y el color, con su nombre y no con su clave.
    assert "Ámbar" in texto
    assert "semaforo-amber" in salida["previsualizacion"]["clase"]


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


# ---------------------------------------------------------------------------
# Elegir qué sesión voy a hacer
# ---------------------------------------------------------------------------
#
# EL CASO REAL, del 21 de septiembre de 2026. El sistema puso ÁMBAR por
# `lumbar_medio` -lumbar en 5-, el usuario se veía bien para la sesión
# completa, y al enviar se le escribió la REDUCIDA en Hevy. Tuvo que entrar en
# la aplicación y arreglarlo a mano.
#
# Lo llamativo es que el motor sabía recibir esa anulación desde semanas antes:
# `SesionPedida`, `SesionAnulada` con su `forzada_en_rojo`, `ConfirmacionNecesaria`
# para subir en rojo, y las columnas `override_session_type` y `forced_on_red`
# en `previews`. Los dos endpoints la aceptaban. Lo único que no existía era el
# sitio desde el que pedirla.
#
# Y esa es exactamente la avería que `docs/cita-de-recalibracion.md` daba por
# aceptable «a propósito»: sin el control, la medida (b) de calibración no
# podía acumular un solo caso. No era que faltara un adorno; era que el
# desacuerdo obligaba a saltarse el sistema, y un desacuerdo que obliga a eso
# no queda registrado y por tanto no se puede medir nunca.


def _pedir(tmp_path, acciones, **kw) -> dict:
    """Previsualiza, toca el control y envía. Devuelve lo que salió por el cable."""
    return _rellenar(
        tmp_path,
        _hoy(),
        [
            {"tipo": "deslizar", "key": "fatigue", "valor": 7},
            {"tipo": "responder", "key": "wants_to_train", "respuesta": "no"},
            {"tipo": "responder", "key": "will_train", "respuesta": "si"},
            {"tipo": "previsualizar"},
            *acciones,
        ],
        **kw,
    )


def test_por_defecto_no_se_pide_ninguna_sesion(tmp_path):
    """El día normal no puede parecer una anulación.

    `requested_session` AUSENTE, y no puesto al tipo que el sistema propone.
    El motor distingue «no se pidió nada» de «se pidió justo lo propuesto», y
    solo el segundo deja rastro. Mandar siempre la clave -que es lo cómodo:
    leer el tipo de la tarjeta y reenviarlo- convertiría cada mañana corriente
    en una anulación, y la medida de cuántas veces se discrepa contaría todos
    los días del año.
    """
    salida = _pedir(tmp_path, [{"tipo": "enviar"}])

    assert "requested_session" not in salida["cuerpo"], (
        f"sin tocar el control ya se pide una sesión: {salida['cuerpo']}"
    )
    assert "override_reason" not in salida["cuerpo"]
    assert "confirm_upgrade" not in salida["cuerpo"]


def test_pedir_la_sesion_completa_llega_al_envio(tmp_path):
    """EL TEST. Es el fallo del 21-09-2026, de punta a punta.

    Se pulsa la opción y se envía, y lo que se mira es el CUERPO del POST: es
    lo único que decide qué se escribe en Hevy. Un arnés que comprobara
    `estado.sesionPedida` demostraría que la variable se guarda, que es la
    mitad que ya funcionaba.
    """
    salida = _pedir(
        tmp_path,
        [{"tipo": "pedir-sesion", "sesion": "full"}, {"tipo": "enviar"}],
    )
    assert salida["cuerpo"].get("requested_session") == "full", (
        f"se pidió la completa y el envío no la lleva: {salida['cuerpo']}"
    )


def test_cambiar_de_eleccion_vuelve_a_previsualizar_con_lo_pedido(tmp_path):
    """Elegir a ciegas es elegir mal.

    Sin esto, el usuario marca «completa» y no ve lo que ha pedido hasta
    DESPUÉS de enviarlo, que es justo el momento en que ya se ha escrito en
    Hevy. Además cada mirada queda apuntada con su anulación, que es de donde
    sale la medida de calibración.
    """
    salida = _pedir(tmp_path, [{"tipo": "pedir-sesion", "sesion": "recovery"}])

    assert salida["veces_previsualizado"] == 2, (
        f"cambiar la elección no ha vuelto a mirar: "
        f"{salida['veces_previsualizado']} previsualización(es)"
    )
    assert salida["cuerpos_previsualizados"][0].get("requested_session") is None
    assert salida["cuerpos_previsualizados"][1].get("requested_session") == "recovery"


def test_el_motivo_escrito_viaja_con_la_anulacion(tmp_path):
    """Sin el porqué, la medida dice cuántas veces y nunca por qué."""
    salida = _pedir(
        tmp_path,
        [
            {"tipo": "pedir-sesion", "sesion": "full"},
            {"tipo": "motivo-anulacion", "texto": "  la lumbar va bien hoy  "},
            {"tipo": "enviar"},
        ],
    )
    assert salida["cuerpo"].get("override_reason") == "la lumbar va bien hoy", (
        "el motivo no viaja, o viaja sin recortar los espacios"
    )


def test_un_motivo_en_blanco_no_viaja(tmp_path):
    """Un campo que viaja vacío es un campo que el servidor tiene que limpiar,
    y dos sitios decidiendo qué es «nada» acaban discrepando."""
    salida = _pedir(
        tmp_path,
        [
            {"tipo": "pedir-sesion", "sesion": "full"},
            {"tipo": "motivo-anulacion", "texto": "   "},
            {"tipo": "enviar"},
        ],
    )
    assert "override_reason" not in salida["cuerpo"]


def test_volver_a_lo_del_sistema_deja_de_pedir_nada(tmp_path):
    """Se puede cambiar de opinión, y volver atrás tiene que borrar el rastro.

    Si «lo que propone el sistema» dejara puesto el último tipo pedido, el
    usuario que se lo piensa dos veces acabaría enviando una anulación que
    retiró, y en Hevy se escribiría lo que decidió NO hacer.
    """
    salida = _pedir(
        tmp_path,
        [
            {"tipo": "pedir-sesion", "sesion": "full"},
            {"tipo": "pedir-sesion", "sesion": None},
            {"tipo": "enviar"},
        ],
    )
    assert "requested_session" not in salida["cuerpo"], (
        f"volver al defecto no ha limpiado la petición: {salida['cuerpo']}"
    )


# ---------------------------------------------------------------------------
# Subir de dureza con el semáforo en ROJO
# ---------------------------------------------------------------------------
#
# La única cosa que esta pantalla no hace sin preguntar. El motor no la decide
# ni se traga la petición: lanza `ConfirmacionNecesaria` y los dos endpoints
# devuelven un 409 con las dos sesiones puestas. Lo que se comprueba aquí es
# que la pantalla trate ese 409 como una PREGUNTA y no como un rechazo, porque
# las dos cosas llegan por el mismo código de estado y la diferencia es
# enorme: un rechazo se lee como «no se ha guardado tu check-in».


CONFIRMACION_409 = {
    "status": 409,
    "respuesta": {
        "detail": {
            "confirmacion_necesaria": True,
            "propuesta": "recovery",
            "pedida": "full",
            "motivo": "la luz está en rojo y se ha pedido subir de recovery a full",
            "ejecutado": False,
            "checkin_guardado": False,
            "decision_guardada": False,
            "hevy": "sin tocar",
            "telegram": "sin tocar",
        }
    },
}


def test_subir_en_rojo_pregunta_y_no_parece_un_rechazo(tmp_path):
    """Un 409 con `confirmacion_necesaria` NO es «el servidor ha dicho que no».

    Si cayera por el camino del error, el usuario leería que su check-in ha
    sido rechazado -y no lo ha sido: no se ha guardado nada, y eso es otra
    cosa- y no tendría por dónde contestar. La subida en rojo se volvería
    imposible de pedir, que es justo lo contrario de lo que se acordó: se
    permite, pero preguntando.
    """
    salida = _pedir(
        tmp_path,
        [{"tipo": "pedir-sesion", "sesion": "full"}],
        previsualizaciones=[{}, CONFIRMACION_409],
    )
    tarjeta = (salida["previsualizacion"] or {}).get("html", "")
    assert "ROJO" in tarjeta, f"no se está preguntando nada:\n{tarjeta[:400]}"
    assert "confirmar-si" in tarjeta and "confirmar-no" in tarjeta, (
        "la pregunta no ofrece las dos respuestas"
    )
    assert salida["veces_enviado"] == 0, "no se puede haber enviado nada todavía"


def test_confirmar_la_subida_en_rojo_vuelve_a_pedirla_con_la_marca(tmp_path):
    """El «sí» no se queda en el cliente: vuelve al servidor.

    Y vuelve como `confirm_upgrade`, que allí se contrasta con la LUZ. Un
    cliente que decidiera por su cuenta que esto ya está confirmado se
    saltaría la única guarda que hay, y peor: `forzada_en_rojo` se calcula en
    el servidor a partir de la luz precisamente para que una pantalla que
    mandara `true` siempre no pudiera ensuciar la medida.
    """
    salida = _pedir(
        tmp_path,
        [
            {"tipo": "pedir-sesion", "sesion": "full"},
            {"tipo": "confirmar-rojo", "respuesta": "si"},
        ],
        previsualizaciones=[{}, CONFIRMACION_409, {}],
    )
    ultimo = salida["cuerpos_previsualizados"][-1]
    assert ultimo.get("requested_session") == "full"
    assert ultimo.get("confirm_upgrade") is True, (
        f"la confirmación no ha viajado: {ultimo}"
    )


def test_decir_que_no_a_la_subida_en_rojo_vuelve_a_lo_propuesto(tmp_path):
    """Poder salirse es parte de la pregunta. Una pregunta con una sola
    respuesta posible no es una pregunta, es un trámite."""
    salida = _pedir(
        tmp_path,
        [
            {"tipo": "pedir-sesion", "sesion": "full"},
            {"tipo": "confirmar-rojo", "respuesta": "no"},
        ],
        previsualizaciones=[{}, CONFIRMACION_409, {}],
    )
    ultimo = salida["cuerpos_previsualizados"][-1]
    assert "requested_session" not in ultimo, (
        f"decir que no ha dejado la petición puesta: {ultimo}"
    )
    assert "confirm_upgrade" not in ultimo


def test_cambiar_de_opcion_tira_la_confirmacion_del_rojo(tmp_path):
    """La confirmación es de UNA pregunta concreta, no un permiso abierto.

    Se contestó «sí, quiero la completa aunque hoy sea rojo». Si al cambiar
    después a «reducida» siguiera puesta, la siguiente petición viajaría ya
    confirmada sin que nadie haya confirmado ESA, y bastaría con pasar una vez
    por el diálogo para que el resto del día dejara de preguntar.
    """
    salida = _pedir(
        tmp_path,
        [
            {"tipo": "pedir-sesion", "sesion": "full"},
            {"tipo": "confirmar-rojo", "respuesta": "si"},
            {"tipo": "pedir-sesion", "sesion": "reduced"},
        ],
        previsualizaciones=[{}, CONFIRMACION_409, {}, {}],
    )
    ultimo = salida["cuerpos_previsualizados"][-1]
    assert ultimo.get("requested_session") == "reduced"
    assert "confirm_upgrade" not in ultimo, (
        f"la confirmación del rojo ha sobrevivido al cambio de opción: {ultimo}"
    )


# ---------------------------------------------------------------------------
# «Por qué»: el bloque que no se puede contradecir
# ---------------------------------------------------------------------------
#
# Es el único de la tarjeta cuyo trabajo entero es explicar por qué el sistema
# ha decidido lo que ha decidido. Una contradicción aquí no es fea: es que la
# explicación no sirve.
#
# LO QUE HABÍA. La frase «Ninguna regla ha saltado hoy» se imprimía cuando
# faltaba `trigger_rule`, que es OTRO HECHO del que la frase afirma. El
# servidor manda esa clave dos veces -suelta en la raíz de la respuesta y
# dentro de `decision`- y aquí se leía solo la de la raíz. Con la de la raíz
# ausente y `fired_rules` lleno, la tarjeta imprimía «Ninguna regla ha saltado
# hoy» y justo debajo la regla que había saltado, con su detalle.
#
# Salió al renderizar la tarjeta a mano con el payload real guardado en
# `previews`, que no lleva la copia de la raíz. Un dato con dos fuentes es un
# sitio donde pueden discrepar, y éste discrepaba.


def _por_que(tmp_path, respuesta: dict) -> list[str]:
    """Las líneas del bloque «Por qué», pintadas de verdad."""
    salida = _rellenar(
        tmp_path,
        _hoy(),
        [
            {"tipo": "deslizar", "key": "fatigue", "valor": 7},
            {"tipo": "responder", "key": "wants_to_train", "respuesta": "no"},
            {"tipo": "responder", "key": "will_train", "respuesta": "si"},
            {"tipo": "previsualizar"},
        ],
        previsualizaciones=[{"respuesta": respuesta}],
    )
    return (salida["previsualizacion"] or {}).get("texto", "")


_BASE = {
    "day": "2026-09-15", "ejecutado": False, "checkin_guardado": False,
    "decision_guardada": False, "hevy": "sin tocar", "telegram": "sin tocar",
    "previsualizacion_guardada": True, "preview_id": 1, "seq": 1,
    "revision": False, "light": "amber",
}


def test_la_frase_de_ninguna_regla_no_convive_con_una_regla_que_salto(tmp_path):
    """EL TEST. La contradicción, reproducida por el camino que la producía.

    `trigger_rule` NO va en la raíz -como en el payload que guarda `previews`-
    y `fired_rules` sí trae la regla. Antes salían las dos frases seguidas.
    """
    texto = _por_que(tmp_path, {
        **_BASE,
        "decision": {
            "light": "amber",
            "trigger_rule": "lumbar_medio",
            "fired_rules": [{"name": "lumbar_medio", "detail": ["lower_discomfort = 5 gte 5"]}],
            "skipped_rules": [], "notes": [], "session": {"title": "Día 1", "kind": "reduced"},
        },
    })
    assert "lumbar_medio" in texto, "no se nombra la regla que decidió el color"
    assert "Ninguna regla ha saltado hoy" not in texto, (
        "la tarjeta dice que no ha saltado ninguna regla y debajo enseña la que "
        "saltó, con su detalle. Es el bloque que existe para explicar la "
        "decisión contradiciéndose consigo mismo"
    )


def test_un_dia_sin_ninguna_regla_si_lo_dice(tmp_path):
    """El contrapeso, y no es de adorno: la forma más fácil de poner verde el
    test de arriba es borrar la frase, y entonces un día en que de verdad no
    salta nada dejaría el bloque vacío sin decir que está vacío a propósito."""
    texto = _por_que(tmp_path, {
        **_BASE,
        "light": "green",
        "decision": {
            "light": "green", "trigger_rule": None, "fired_rules": [],
            "skipped_rules": [], "notes": [], "session": {"title": "Día 1", "kind": "full"},
        },
    })
    assert "Ninguna regla ha saltado hoy" in texto


def test_la_regla_disparadora_se_lee_tambien_de_dentro_de_la_decision(tmp_path):
    """Un dato con dos fuentes se lee de una, con la otra de respaldo.

    Si esto leyera solo la copia de la raíz, el día que alguien la quite por
    ordenar la respuesta -es redundante, y lo redundante se limpia- la tarjeta
    dejaría de nombrar la regla que decidió el color sin que nada reventara.
    """
    # DOS reglas saltadas y la disparadora la SEGUNDA de la lista. Con una
    # sola, este test no distinguía «se nombra como la que decidió» de «sale
    # en la lista de las que saltaron»: leyendo solo la copia de la raíz, la
    # regla aparecía igual -por el bucle de las demás- y el test pasaba. La
    # afirmación de verdad es sobre el ORDEN, porque lo que el bloque promete
    # es que la primera línea es la que decidió el color.
    texto = _por_que(tmp_path, {
        **_BASE,
        "decision": {
            "light": "amber", "trigger_rule": "fatiga_alta",
            "fired_rules": [
                {"name": "sueno_corto", "detail": ["sleep 5.1 h < 6 h"]},
                {"name": "fatiga_alta", "detail": ["fatigue 7 ≥ 6"]},
            ],
            "skipped_rules": [], "notes": [], "session": {"title": "Día 1", "kind": "reduced"},
        },
    })
    assert "fatiga_alta" in texto and "fatigue 7" in texto, (
        "sin la copia de la raíz la tarjeta ya no dice qué regla decidió el color"
    )
    assert texto.index("fatiga_alta") < texto.index("sueno_corto"), (
        f"la regla que decidió el color no va la primera: se está leyendo solo "
        f"la copia de la raíz y la disparadora cae al montón de las demás. "
        f"{texto[:300]}"
    )


# ---------------------------------------------------------------------------
# «Hoy no entrenas»: la tarjeta deja de prescribir
# ---------------------------------------------------------------------------
#
# EL 23-09-2026, después de contestar que NO iba a entrenar, la tarjeta seguía
# diciendo «Lo que propondría: Día 2, sesión completa», la lista de cambios
# sobre la rutina, «Hoy se quedan fuera: Peso muerto», los apuntes de la sesión
# y el control para elegir qué sesión hacer. Todo ello debajo de una nota que
# ya decía «hoy no entrenas»: la pantalla se contradecía y encima daba
# instrucciones para un entreno que el usuario acababa de descartar.
#
# Y LO LLAMATIVO ES QUE YA ESTABA RESUELTO EN EL OTRO SITIO. `message.py` tiene
# `prescribe = va_a_entrenar is not False` desde hace tiempo, con su razón
# escrita: con un «no voy», el Telegram cuenta el día en vez de darte el plan.
# La misma decisión salía prescribiendo por una pantalla y no por la otra,
# porque son dos renderizadores y solo uno se enteró.


def _tarjeta(tmp_path, decision: dict) -> dict:
    """La tarjeta entera, pintada de verdad, con la decisión que se le pase."""
    salida = _rellenar(
        tmp_path,
        _hoy(),
        [
            {"tipo": "deslizar", "key": "fatigue", "valor": 7},
            {"tipo": "responder", "key": "wants_to_train", "respuesta": "no"},
            {"tipo": "responder", "key": "will_train", "respuesta": "no"},
            {"tipo": "previsualizar"},
        ],
        previsualizaciones=[{"respuesta": {**_BASE, "decision": decision}}],
    )
    return salida["previsualizacion"] or {}


_SESION_DEL_DIA = {
    "kind": "full",
    "title": "Día 2",
    "routine": "dia_2",
    "changes": ["'retirada_peso_muerto': retirado Peso muerto (máquina guiada)"],
    "dropped": ["Peso muerto (máquina guiada)"],
    "notes": ["sin HIIT: el HIIT solo se añade en verde y hoy es amber"],
    "exercises": [],
}


def _decision(va_a_entrenar) -> dict:
    return {
        "light": "amber", "trigger_rule": "hrv_baja_1d",
        "fired_rules": [{"name": "hrv_baja_1d", "detail": ["hrv_ratio = 0.9 lt 0.9"]}],
        "skipped_rules": [], "notes": [],
        "va_a_entrenar": va_a_entrenar,
        "session": _SESION_DEL_DIA,
    }


def test_con_un_no_voy_a_entrenar_la_tarjeta_no_prescribe(tmp_path):
    """EL TEST. Ninguna instrucción para un entreno que se acaba de descartar."""
    t = _tarjeta(tmp_path, _decision(False))
    texto = t.get("texto", "")

    assert "Hoy no entrenas" in texto, f"no lo dice:\n{texto[:300]}"
    for frase in ("Lo que propondría", "Cambios sobre la rutina",
                  "Hoy se quedan fuera", "Apuntes de la sesión"):
        assert frase not in texto, (
            f"la tarjeta sigue prescribiendo con «{frase}» después de contestar "
            f"que hoy no se entrena"
        )
    assert "Peso muerto" not in texto, (
        "sigue diciendo qué ejercicio se retira de una sesión que no se va a hacer"
    )


def test_el_dia_que_no_entrenas_contesta_las_dos_preguntas_que_quedan(tmp_path):
    """Ocupa el sitio de la sesión, no lo deja vacío.

    Un bloque que simplemente desapareciera se lee como una pantalla rota
    -¿se ha caído algo?, ¿se ha olvidado de qué toca?- y la duda acaba en
    abrir Hevy a comprobarlo, que es el trabajo que esto existe para ahorrar.
    Las dos frases son las mismas que ya da el Telegram: que no se pierde el
    turno y que la rutina está escrita por si se cambia de idea.
    """
    texto = _tarjeta(tmp_path, _decision(False)).get("texto", "")

    assert "Día 2 sigue siendo la siguiente" in texto, (
        "no dice que la rotación no se mueve: queda la duda de si se pierde el turno"
    )
    assert "por si cambias de idea" in texto, (
        "no dice que la rutina está escrita igual: decir «no» parece una puerta cerrada"
    )


def test_sin_entreno_no_se_ofrece_elegir_que_sesion_hacer(tmp_path):
    """No es solo que sobre en pantalla.

    Cada opción que se pulsara ahí quedaría apuntada como una ANULACIÓN y
    contaría hacia el umbral de diez de una regla con la que el usuario ni
    siquiera está discutiendo. El contador acabaría midiendo, en parte, los
    días que no se entrena.
    """
    html = _tarjeta(tmp_path, _decision(False)).get("html", "")
    assert "eleccion-sesion" not in html, (
        "se ofrece elegir el tipo de sesión un día en que no hay sesión"
    )


def test_sin_contestar_la_pregunta_la_tarjeta_SIGUE_proponiendo(tmp_path):
    """EL CONTRAPESO, y es el que impide el arreglo fácil y catastrófico.

    Son TRES estados y no dos. `null` es «no lo has contestado» -o un día del
    archivo anterior a que la pregunta existiera- y tiene que seguir
    prescribiendo como siempre. Escrito `if (!va_a_entrenar)`, la tarjeta
    dejaría de proponer sesión todos los días en que no se rellena el
    formulario, que son la mayoría, y lo haría en silencio.
    """
    texto = _tarjeta(tmp_path, _decision(None)).get("texto", "")

    assert "Lo que propondría" in texto, (
        "sin contestar la pregunta la tarjeta ha dejado de proponer: se está "
        "tratando `null` como un «no»"
    )
    assert "Hoy no entrenas" not in texto


def test_diciendo_que_si_la_tarjeta_propone_como_siempre(tmp_path):
    """El día normal, que es el que no puede romperse al arreglar el raro."""
    texto = _tarjeta(tmp_path, _decision(True)).get("texto", "")

    assert "Lo que propondría" in texto
    assert "Hoy se quedan fuera" in texto
    assert "Hoy no entrenas" not in texto


# ---------------------------------------------------------------------------
# La barra y Avanzado, pintadas de verdad
# ---------------------------------------------------------------------------
#
# Los tests de arriba leen las listas de `comun.js`. Estos ejecutan las dos
# funciones que las pintan, porque una lista perfecta que nadie pinta es la
# pieza que certifica que no falta nada mientras falta: las seis vistas de
# Avanzado serían «alcanzables» según el test e inalcanzables en la pantalla.


def _pinta_comun(llamada: str) -> dict[str, str]:
    """Carga `comun.js` en node, hace `llamada` y devuelve el HTML de cada hueco."""
    script = (
        "const fs=require('fs'),vm=require('vm');"
        "const els={};"
        "const doc={getElementById:(id)=>(els[id]=els[id]||{innerHTML:''})};"
        "const ctx={document:doc,console};vm.createContext(ctx);"
        "vm.runInContext(fs.readFileSync('static/comun.js','utf8'),ctx);"
        f"vm.runInContext({json.dumps(llamada)},ctx);"
        "console.log(JSON.stringify(Object.fromEntries("
        "Object.entries(els).map(([k,v])=>[k,v.innerHTML]))));"
    )
    r = subprocess.run(
        ["node", "-e", script], cwd=RAIZ, capture_output=True, text=True,
        encoding="utf-8", timeout=60,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_avanzado_pinta_un_enlace_por_cada_vista_de_la_lista():
    html = _pinta_comun("pintarAvanzado()")["lista-avanzado"]
    for href in _hrefs_de("AVANZADO"):
        assert f'href="{href}"' in html, (
            f"`AVANZADO` nombra `{href}` y la página no lo pinta: la vista existe, "
            f"el test de la barra la da por alcanzable, y no hay dónde tocar"
        )


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_una_vista_de_avanzado_enciende_mas_en_la_barra():
    """Sin esto, abrir una de ellas dejaba la barra sin ninguna pestaña activa, y
    no se sabía ni dónde se estaba ni cómo volver."""
    html = _pinta_comun('pintarNav("#concordancia")')["nav"]
    activa = re.findall(r'<a href="([^"]+)" class="activa"', html)
    assert activa == ["/avanzado.html"], activa


@pytest.mark.skipif(shutil.which("node") is None, reason="hace falta node")
def test_la_portada_enciende_como_vas_y_no_mas():
    """La pareja del anterior: «Cómo vas» vive en metricas.html como las de
    Avanzado, y no por eso es una de ellas."""
    html = _pinta_comun('pintarNav("#portada")')["nav"]
    activa = re.findall(r'<a href="([^"]+)" class="activa"', html)
    assert activa == ["/metricas.html#portada"], activa
