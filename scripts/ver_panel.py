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
import re
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
sys.stdout.reconfigure(encoding="utf-8")

DIAS = 180
SALIDA = RAIZ / "out" / "panel"

# Lo que hay que mirar en el texto que se VE. Cada una de estas es un fallo que
# ya ocurrió y que ningún test vio, porque en los seis casos todas las claves
# existían y todos los valores eran cadenas perfectamente válidas.
SOSPECHOSOS = {
    "plural de plantilla": r"\(s\)|\(es\)|\(a\)|\(as\)",
    # `garmin` y `entreno` NO están en la lista, y no es un olvido: "datos de
    # Garmin" y "cada entreno" son castellano correcto y salen en cinco de las
    # seis vistas. Una lista que marca esas dos marca algo en cada pasada, y un
    # repaso que siempre grita se deja de leer a la tercera vez. `checkin` sí
    # está porque no es una palabra: si aparece, es una clave sin traducir.
    "jerga sin traducir": r"\b(spearman|pearson|alto_peor|alto_mejor|con_datos"
                          r"|nunca_disparo|nunca_evaluada|p_corregida|n_reciente"
                          r"|desplazamiento_dias|checkin)\b",
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


def repasar() -> None:
    """Enseña, por vista, qué sospechoso aparece y con qué frase alrededor."""
    print()
    for fichero in sorted(SALIDA.glob("*.html")):
        visible = re.sub(NO_SE_LEE, " ", fichero.read_text(encoding="utf-8"), flags=re.S)
        avisos: list[str] = []
        for nombre, patron in SOSPECHOSOS.items():
            m = re.search(patron, visible, re.I)
            if m:
                a, b = max(0, m.start() - 45), min(len(visible), m.end() + 30)
                trozo = " ".join(visible[a:b].split())
                avisos.append(f"{nombre} -> ...{trozo}...")
        if avisos:
            print(f"{fichero.name}")
            for a in avisos:
                print(f"    {a}")
        else:
            print(f"{fichero.name:20} limpio")


def main() -> int:
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
    repasar()
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
