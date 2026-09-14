"""Pinta las seis vistas del panel contra la base de VERDAD y escribe el HTML.

No es un test y no afirma nada: es el equivalente a abrir la PWA en el móvil,
pero sin móvil. Existe porque el andamio de `tests/render_pwa.mjs` corre sobre
una base sembrada -doce días inventados, una correlación por casilla- y eso
contesta "¿el renderizador lee alguna clave que no existe?" pero no contesta
"¿qué pone en la pantalla?". Las dos preguntas hacen falta y son distintas: una
portada puede estar perfectamente bien formada y decir, con todas sus claves en
su sitio, que no hay nada que contar.

SOLO HACE GET. Abre la base en modo lectura y no arranca el planificador, no
toca Hevy y no manda Telegram. Se puede lanzar con el contenedor levantado.

    ./.venv/Scripts/python.exe -X utf8 scripts/ver_panel.py

CONTRA QUÉ BASE, Y POR QUÉ ESO CASI ARRUINA ESTE GUIÓN
-----------------------------------------------------
En este PC hay DOS bases con el mismo nombre. `app/settings.py` apunta por
defecto a `data/app.db`, que es la del repositorio; pero el contenedor de
pruebas monta un volumen con nombre -`datos-pruebas`, ver la cabecera de
`docker-compose.pruebas-lan.yml`- porque el bind mount de Windows no aguanta el
WAL de SQLite. O sea que la base que USA el sistema no es la que abre un guión
lanzado desde el host, y las dos existen, las dos abren y ninguna se queja.

La primera tanda de este guión se pintó entera contra la del repositorio, que
iba seis días atrasada y no tenía ni una decisión guardada. Nada falló: salió
un panel completo, con sus correlaciones y sus fechas, diciendo cosas ciertas
de una base que no es la que decide por las mañanas. Un guión que existe para
comprobar contra la verdad y mira otro sitio es peor que no tenerlo, porque
además tranquiliza.

Así que aquí NO se elige por defecto. Si hay un contenedor levantado, se saca
su base y se pinta contra ella; si no lo hay, se usa la local. En los dos casos
se dice en voz alta de dónde salió, con fecha y recuentos. Y si el contenedor
está arriba pero su base no se puede copiar, esto PARA: caerse hacia la local
sin avisar es exactamente el fallo que este párrafo describe.

Deja `out/panel/<vista>.html` y un `out/panel/payloads.json` con las siete
respuestas tal cual las sirvió la API, que es lo que hay que mirar cuando la
pantalla dice algo raro: casi siempre el texto raro ya venía escrito así.

Y al final pasa el repaso de `SOSPECHOSOS` sobre el texto que se ve. Eso no
convierte esto en un test -no falla, no bloquea nada- sino en lo que hace un
par de ojos que ya sabe qué buscar: "85,50 0-100", "spearman" a pelo,
"día(s)", un "undefined" suelto. Todos esos pasaron por `render_pwa.mjs` sin
que saltara nada, porque para un renderizador no hay ningún fallo en escribir
una cadena válida que no significa lo que parece.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
sys.stdout.reconfigure(encoding="utf-8")

DIAS = 180
SALIDA = RAIZ / "out" / "panel"
CONTENEDOR = "adaptive-training"

# Lo que hay que mirar en el texto que se VE. Cada una de estas es un fallo que
# ya ocurrió y que ningún test vio, porque en los seis casos todas las claves
# existían y todos los valores eran cadenas perfectamente válidas.
SOSPECHOSOS = {
    "plural de plantilla": r"\(s\)|\(es\)|\(a\)|\(as\)",
    # La otra mitad del mismo fallo: cuando el plural no se hace pegando un
    # sufijo hay que escribir las dos palabras, y sale "vez/veces". Faltaba
    # aquí, y por eso "activada 0 vez/veces" estuvo meses en la pantalla:
    # `tests/test_analysis_texto.py` la caza, pero solo en Python, y esa frase
    # vive en `metricas.js`. Los dos repasos tienen que mirar lo mismo o el
    # fallo se muda al idioma que no se mira.
    #
    # El par se distingue de una ruta -`app/analysis/series.py`- en que las dos
    # mitades son la misma palabra: comparten el arranque y la segunda es más
    # larga. Eso no cabe en una expresión regular, así que lo filtra
    # `_es_plural_de_plantilla` después de casar.
    "plural con barra": r"\b[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{2,}/[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{2,}\b",
    # `garmin` y `entreno` NO están en la lista, y no es un olvido: "datos de
    # Garmin" y "cada entreno" son castellano correcto y salen en cinco de las
    # seis vistas. Una lista que marca esas dos marca algo en cada pasada, y un
    # repaso que siempre grita se deja de leer a la tercera vez. `checkin` sí
    # está porque no es una palabra: si aparece, es una clave sin traducir.
    "jerga sin traducir": r"\b(spearman|pearson|alto_peor|alto_mejor|con_datos"
                          r"|nunca_disparo|nunca_evaluada|p_corregida|n_reciente"
                          r"|desplazamiento_dias|checkin)\b",
    # `green` y `amber` estaban saliendo en la auditoría -"green: 0,0 % · amber:
    # 100,0 %"- y esta lista no los veía, porque cuando se escribió solo miraba
    # claves con guion bajo. Se encontraron mirando la pantalla, después de que
    # este mismo repaso diera las seis vistas por limpias. `red` NO entra: es
    # una palabra castellana y el día que el panel hable de una red de algo,
    # este repaso empezaría a gritar en cada pasada. Los tres colores van
    # siempre juntos, así que con dos basta para cazar el terceto.
    "color sin traducir": r"\b(green|amber)\b",
    "hueco de JavaScript": r"\bundefined\b|\bNaN\b|\[object Object\]",
    "rango escrito como sufijo": r"\d\s+\d+-\d+",
}

# El `<style>` y el `<script>` NO son texto que se lea. `.estado-nunca_disparo`
# es un nombre de clase y tiene que llamarse igual que la clave del payload;
# buscar jerga ahí dentro da seis falsos positivos y un repaso que grita
# siempre es un repaso que se deja de mirar.
NO_SE_LEE = r"<style[^>]*>.*?</style>|<script[^>]*>.*?</script>|<[^>]+>"

# Las mismas siete que declara `RUTAS` en `static/metricas.js`. Se leen de ahí y
# no se copian a mano: una lista paralela es una lista que se queda vieja, y la
# portada ya estuvo un día servida y sin leer justo por eso.
def rutas_de_la_pwa() -> dict[str, str]:
    js = (RAIZ / "static" / "metricas.js").read_text(encoding="utf-8")
    bloque = re.search(r"const RUTAS = \{(.*?)\n\};", js, re.S)
    if not bloque:
        raise SystemExit("no encuentro `const RUTAS` en static/metricas.js")
    pares = re.findall(r'(\w+):\s*"(/api/[^"]+)"', bloque.group(1))
    if not pares:
        raise SystemExit("`const RUTAS` está, pero no tiene ninguna ruta dentro")
    return dict(pares)


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, encoding="utf-8"
    )


def base_de_verdad() -> Path:
    """La base que USA el sistema, no la que pilla el import por defecto.

    Devuelve la ruta que hay que poner en `DATABASE_URL` antes de importar
    nada de `app`, porque `Settings` la lee una sola vez al construirse.
    """
    local = RAIZ / "data" / "app.db"
    try:
        ps = _docker("ps", "--filter", f"name={CONTENEDOR}", "--format", "{{.Names}}")
    except FileNotFoundError:
        ps = None
    if ps is None or CONTENEDOR not in (ps.stdout or ""):
        print(f"base: {local}  (no hay contenedor levantado)")
        return local

    # `docker cp` de un SQLite abierto necesita el `-wal` al lado: el fichero
    # principal puede llevar horas sin tocarse mientras todo lo de hoy sigue
    # en el diario. Copiar solo el `.db` da una foto vieja QUE ABRE BIEN, que
    # es la peor forma de equivocarse.
    destino = SALIDA / "app-del-contenedor.db"
    SALIDA.mkdir(parents=True, exist_ok=True)
    for sufijo in ("", "-wal", "-shm"):
        r = _docker("cp", f"{CONTENEDOR}:/app/data/app.db{sufijo}",
                    str(destino) + sufijo)
        if r.returncode and sufijo == "":
            raise SystemExit(
                f"el contenedor {CONTENEDOR} está levantado pero su base no se "
                f"puede copiar: {r.stderr.strip()}\n"
                f"NO se sigue con {local}: pintar el panel contra otra base sin "
                f"decirlo es justo lo que este guión existe para no hacer."
            )
    print(f"base: {destino}  (copiada de {CONTENEDOR}:/app/data/app.db)")
    return destino


def contar(bd: Path) -> None:
    """Cuatro números y una fecha, para que la base se pueda reconocer a ojo."""
    con = sqlite3.connect(f"file:{bd.as_posix()}?mode=ro", uri=True)
    try:
        trozos = [f"{bd.stat().st_size // 1024} KiB",
                  datetime.fromtimestamp(bd.stat().st_mtime).strftime("%d %b %H:%M")]
        for tabla in ("activities", "checkins", "workout_log", "decisions"):
            try:
                n, ultima = con.execute(
                    f"select count(*), max(date) from {tabla}"  # noqa: S608
                ).fetchone()
                trozos.append(f"{tabla} {n}" + (f" hasta {ultima}" if ultima else ""))
            except sqlite3.Error:
                trozos.append(f"{tabla} ?")
    finally:
        con.close()
    print("       " + " · ".join(trozos))


def _es_plural_de_plantilla(par: str) -> bool:
    """Si `algo/algos` son dos formas de la misma palabra o dos cosas distintas.

    Un `\\w+/\\w+` es también la forma de una ruta, y `app/analysis/series.py`
    hizo pinchar en falso al test equivalente de `tests/test_analysis_texto.py`
    la primera vez que se escribió. Lo que distingue un par singular/plural es
    que las dos mitades son la misma palabra: comparten el arranque y la
    segunda es más larga. Es la misma regla que allí, escrita otra vez porque
    un guión del repositorio no importa de `tests/`.
    """
    uno, _, varios = par.partition("/")
    return len(varios) > len(uno) and varios[:2].lower() == uno[:2].lower()


def _frases_del_servidor(payloads: object) -> list[str]:
    """Todas las cadenas que hay dentro de los payloads, a cualquier hondura."""
    fuera: list[str] = []
    pila = [payloads]
    while pila:
        x = pila.pop()
        if isinstance(x, str):
            fuera.append(x)
        elif isinstance(x, dict):
            pila.extend(x.values())
        elif isinstance(x, list):
            pila.extend(x)
    return fuera


def _de_quien(token: str, visible: str, frases: list[str]) -> str:
    """Si la jerga venía ya escrita del servidor o la escribió la PWA.

    La regla que separa los dos casos es el LARGO de la cadena que la trae:

      - si el servidor mandó la palabra SOLA -`"luz": "amber"`-, mandó una
        clave, que es lo que tiene que mandar; verla en la pantalla significa
        que la PWA la pintó en vez de traducirla, y se arregla en el JavaScript.

      - si la mandó DENTRO de una frase más larga -`"el semáforo no está en
        verde (amber)"`- entonces la prosa es del servidor, la PWA no puede
        hacer nada con ella, y además puede estar GUARDADA: `gate_reason` vive
        en el JSON de la decisión del día. Arreglar quien la escribe deja los
        días viejos como estaban.

    Pedir además que la frase larga se vea entera en la pantalla evita apuntar
    al servidor por una cadena que manda pero que no se pinta.
    """
    t = token.lower()
    for f in frases:
        if len(f) > len(token) and t in f.lower() and " ".join(f.split()) in visible:
            return "viene ya escrito del servidor (puede estar GUARDADO)"
    return "lo escribe la PWA"


def repasar(payloads: dict[str, object] | None = None) -> None:
    """Enseña, por vista, qué sospechoso aparece, con qué frase y DE DÓNDE SALE.

    Lo de "de dónde sale" es la mitad útil. Un mismo "amber" en la pantalla
    puede ser dos averías con dos arreglos que no se parecen en nada:

      - la PWA tenía la clave y no supo traducirla. Se arregla en el
        JavaScript, se ve arreglado en la siguiente pasada, y se acabó.

      - el servidor mandó la frase ya escrita con la clave dentro. Eso pasa
        porque el motor GUARDA prosa: `gate_reason` va a parar al JSON de la
        decisión del día, y la vista de auditoría lo pinta tal cual. Arreglar
        quien la escribe NO arregla la pantalla, porque los días ya decididos
        siguen guardando la frase vieja, y siguen saliendo mientras estén
        dentro de la ventana: hasta seis meses de un texto que ya no se genera.

    Sin esta distinción, el segundo caso parece el primero: se toca el
    JavaScript, se vuelve a pintar, sigue igual, y de ahí se sale pensando que
    el arreglo no funcionó. Con ella, el repaso dice a la cara que lo que hay
    en la pantalla es un fósil y que lo que queda por decidir es si se migra
    lo guardado o se deja envejecer.
    """
    frases = _frases_del_servidor(payloads)
    print()
    for fichero in sorted(SALIDA.glob("*.html")):
        visible = re.sub(NO_SE_LEE, " ", fichero.read_text(encoding="utf-8"), flags=re.S)
        avisos: list[str] = []
        for nombre, patron in SOSPECHOSOS.items():
            for m in re.finditer(patron, visible, re.I):
                if nombre == "plural con barra" and not _es_plural_de_plantilla(m.group(0)):
                    continue  # una ruta, una fracción, una fecha: no es un plural
                a, b = max(0, m.start() - 45), min(len(visible), m.end() + 30)
                trozo = " ".join(visible[a:b].split())
                culpa = _de_quien(m.group(0), visible, frases) if payloads else ""
                avisos.append(f"{nombre} -> ...{trozo}..."
                              + (f"   [{culpa}]" if culpa else ""))
                break  # uno por sospechoso y por vista: esto se lee, no se cuenta
        if avisos:
            print(f"{fichero.name}")
            for a in avisos:
                print(f"    {a}")
        else:
            print(f"{fichero.name:20} limpio")


def main() -> int:
    # ANTES de importar nada de `app`: `Settings` lee `DATABASE_URL` una sola
    # vez, al construirse, y `app.api` la construye al importarse.
    bd = base_de_verdad()
    contar(bd)
    os.environ["DATABASE_URL"] = f"sqlite:///{bd.as_posix()}"

    from fastapi.testclient import TestClient

    from app.api import app

    SALIDA.mkdir(parents=True, exist_ok=True)
    rutas = rutas_de_la_pwa()
    payloads: dict[str, object] = {}

    # SIN `with`, y a propósito. El `with` corre el `lifespan` de FastAPI, y el
    # `lifespan` de esta app arranca APScheduler con sus seis trabajos: escribir
    # en Hevy, mandar Telegram, reconciliar lo entrenado. Uno de ellos es
    # literalmente «Mirar qué no corrió mientras no estaba», que es el que está
    # hecho para dispararse nada más arrancar. Este guión existe para MIRAR la
    # pantalla; que mirar la pantalla pueda reescribir la rutina del día es
    # exactamente el tipo de efecto que no se ve hasta que ya ha pasado.
    #
    # Sin `with`, el cliente no corre el `lifespan` y las rutas de métricas
    # siguen funcionando igual: abren su sesión por `app.db`, no dependen de
    # nada que monte el arranque.
    cliente = TestClient(app)
    for clave, url in rutas.items():
        if clave == "ranking":
            continue  # necesita la respuesta que elija el servidor
        r = cliente.get(url, params={"dias": DIAS})
        if r.status_code != 200:
            raise SystemExit(f"{url} -> {r.status_code}: {r.text[:300]}")
        payloads[clave] = r.json()

    respuesta = payloads["impacto"].get("respuesta_por_defecto")  # type: ignore[union-attr]
    print(f"respuesta por defecto del desplegable de Impacto: {respuesta!r}")
    if respuesta:
        r = cliente.get(rutas["ranking"],
                        params={"dias": DIAS, "respuesta": respuesta})
        if r.status_code != 200:
            raise SystemExit(f"ranking -> {r.status_code}: {r.text[:300]}")
        payloads["ranking"] = r.json()
    else:
        payloads["ranking"] = {"respuesta": None}

    fichero = SALIDA / "payloads.json"
    fichero.write_text(json.dumps(payloads, ensure_ascii=False, indent=1),
                       encoding="utf-8")
    print(f"payloads -> {fichero}")

    proc = subprocess.run(
        ["node", str(RAIZ / "scripts" / "ver_panel.mjs"), str(fichero),
         str(SALIDA)],
        cwd=RAIZ, capture_output=True, text=True, encoding="utf-8",
    )
    print(proc.stdout)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    repasar(payloads)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
