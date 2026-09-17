"""Maqueta APROBADA de la pantalla del umbral: mirar, no estudiar.

POR QUÉ ESTE ARCHIVO ESTÁ EN EL REPOSITORIO Y NO EN `out/`
----------------------------------------------------------
Porque vivía en `out/`, que está en `.gitignore`, y eso costó dos días de
trabajo en dirección contraria.

Se escribió el 2026-09-15 contestando a un encargo muy concreto -las cinco
reglas de abajo- y se enseñó. La conversación siguió a otra cosa, la maqueta se
quedó donde estaba, y cuando semanas después llegó «lleva la pantalla del umbral
al código real y aplica el mismo criterio al resto», no había forma de encontrar
«el mismo criterio»: no salía en `git log`, no salía buscando en el árbol
versionado, no salía en ningún commit. Se entendió por otro criterio distinto y
se hicieron dos commits que empeoraban justo lo que esto venía a arreglar.

Una decisión de diseño aprobada que solo existe en un directorio ignorado es una
decisión que se va a perder. Por eso está aquí, y por eso lo primero que hace
`tests/test_maqueta.py` es comprobar que sigue ejecutándose.

LAS CINCO REGLAS, LITERALES, QUE ES LO QUE HAY QUE CONSERVAR
-------------------------------------------------------------
  1. El gráfico primero y grande. El texto, debajo y en una frase.
  2. Nada de p corregida, correlación, percentiles ni mitad central en la vista
     principal. Todo eso detrás de «ver detalle».
  3. Ejes con números normales y etiquetas en español claro.
  4. Si hace falta un párrafo para explicar el gráfico, el gráfico está mal.
  5. Legible en móvil, en vertical, sin zoom.

QUÉ ES Y QUÉ NO ES
------------------
Es un HTML suelto que se abre en el móvil y no depende de nada. No toca
`static/` ni `app/`. Lo que se lleva al panel de verdad es EL REPARTO -gráfico
grande arriba, una frase debajo, lo estadístico detrás de «ver detalle»- y no
este archivo: aquí el HTML se escribe en Python y allí en JavaScript.

Se queda como referencia ejecutable de lo aprobado. Cuando el panel de verdad y
esta maqueta discrepen, la que tiene razón es esta, o hay que venir a cambiarla
a propósito y dejarlo escrito.

LOS NÚMEROS SON REALES Y SALEN DEL PAYLOAD
------------------------------------------
Se lee `umbral-payload.json`, aquí al lado, que es la respuesta de verdad de
`/api/metrics/umbral` sobre los 180 días hasta 2026-09-15. Aquí no se inventa
ningún dato: las medias, las n y las frases («baja 7,0 ms») vienen escritas del
servidor. Lo único que se calcula aquí son PÍXELES, igual que en `graficos.js`.

El payload viaja CON la maqueta y congelado. Apuntar a la base de verdad la
haría depender de un contenedor encendido y de unos datos que cambian cada
noche, y entonces dejaría de ser una referencia: sería otra vista más.

DOS EXCEPCIONES, Y VAN DICHAS
-----------------------------
La línea de HRV pide «tu media», y el payload de entonces no la traía: traía la
banda de cuartiles, que es justo lo que se ha pedido quitar. Se calcula aquí
para la maqueta, y lo que hay que hacer NO es calcularla en el móvil, sino
añadir `grafica.media` en `app/analysis/umbral.py`, donde hay tests.
Lo mismo con el «3 de cada 10» del quesito.
"""

from __future__ import annotations

import json
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

# El payload congelado vive AL LADO de la maqueta, no en `out/`: junto le da
# igual desde dónde se lance y no depende de un directorio ignorado.
AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parents[1]
sys.stdout.reconfigure(encoding="utf-8")

AZUL = "#4c8dff"
NARANJA = "#e08a3c"
TENUE = "#9aa4b2"
REJILLA = "#2b313b"
TEXTO = "#e7eaef"

MESES = ["ene", "feb", "mar", "abr", "may", "jun",
         "jul", "ago", "sep", "oct", "nov", "dic"]


# ---------------------------------------------------------------------------
# Números como se leen en español
# ---------------------------------------------------------------------------

def nm(v, dec=1, signo=False):
    """48,4 y no 48.4; y el menos de verdad (−), que se ve a 6 de la mañana.

    Se redondea al alza en el empate y no al par, que es lo que hace `format`:
    −0,15 sale «−0,1» con `format` y «baja 0,2 ms» en la frase que escribe el
    servidor. Dos redondeos distintos en la misma pantalla para el mismo número
    es exactamente la clase de desajuste que este panel no quiere tener.
    """
    if v is None:
        return "—"
    q = Decimal(str(abs(v))).quantize(Decimal(1).scaleb(-dec), rounding=ROUND_HALF_UP)
    s = f"{q:.{dec}f}".replace(".", ",")
    if v < 0:
        return "−" + s
    return ("+" + s) if signo else s


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def paso_bonito(span, quiero=5):
    """Un paso de rejilla que caiga en números que alguien diría en voz alta."""
    bruto = span / max(quiero, 1)
    mag = 10 ** int(f"{bruto:e}".split("e")[1])
    for m in (1, 2, 2.5, 5, 10):
        if bruto <= m * mag:
            return m * mag
    return 10 * mag


# ---------------------------------------------------------------------------
# Gráfico 1 y 2: barras con el cero dibujado
# ---------------------------------------------------------------------------

def rotulo(frase):
    """«baja 6,0 ms» -> «−6,0». El número es el del servidor, no uno nuevo.

    La tentación era formatear aquí la media del payload, y sale mal: el servidor
    escribe sus frases sobre la media SIN redondear, así que −0,15 le sale «0,2»
    y 1,25 le sale «1,2». Cualquier redondeo que se haga aquí acierta en uno y
    falla en el otro, y entonces la barra y su detalle dirían números distintos
    del mismo dato. Se reusa la frase y no se vuelve a redondear nada.
    """
    partes = str(frase).split()
    return ("−" if partes[0] == "baja" else "+") + partes[1]


def barras(valores, rotulos, etiquetas, pie, ancho=340, alto=236):
    """Barras verticales alrededor del cero, con el valor escrito en cada una.

    El valor va ENCIMA de la barra y no solo en el eje. Un eje se lee si uno se
    para a leerlo; el número sobre la barra se ve sin querer, que es el criterio
    que se ha pedido.
    """
    izq, der, arr, aba = 36, 10, 24, 70
    w, h = ancho - izq - der, alto - arr - aba

    vals = [v for v in valores if v is not None]
    lo, hi = min(0.0, min(vals)), max(0.0, max(vals))
    margen = (hi - lo) * 0.18 or 1.0
    lo, hi = lo - margen, hi + margen
    y = lambda v: arr + (1 - (v - lo) / (hi - lo)) * h  # noqa: E731

    p = []

    # La rejilla, con sus números en milisegundos. Pocas líneas: cuatro rayas
    # ayudan a leer, ocho convierten el dibujo en papel milimetrado.
    paso = paso_bonito(hi - lo, 4)
    t = paso * round(lo / paso)
    while t <= hi + 1e-9:
        if lo <= t <= hi:
            cero = abs(t) < 1e-9
            p.append(
                f'<line x1="{izq}" y1="{y(t):.1f}" x2="{ancho - der}" '
                f'y2="{y(t):.1f}" stroke="{TENUE if cero else REJILLA}" '
                f'stroke-width="{1.4 if cero else 1}"/>'
            )
            p.append(
                f'<text x="{izq - 6}" y="{y(t) + 4:.1f}" fill="{TENUE}" '
                f'font-size="12" text-anchor="end">{esc(nm(t, 0))}</text>'
            )
        t += paso

    paso_x = w / len(valores)
    ancho_barra = min(paso_x * 0.56, 46)

    for i, (v, rot, etq) in enumerate(zip(valores, rotulos, etiquetas)):
        cx = izq + paso_x * i + paso_x / 2
        for j, linea in enumerate(etq):
            p.append(
                f'<text x="{cx:.1f}" y="{alto - 44 + j * 16}" fill="{TEXTO}" '
                f'font-size="13" text-anchor="middle">{esc(linea)}</text>'
            )
        if v is None:
            continue
        color = AZUL if v >= 0 else NARANJA
        y0, y1 = y(0), y(v)
        p.append(
            f'<rect x="{cx - ancho_barra / 2:.1f}" y="{min(y0, y1):.1f}" '
            f'width="{ancho_barra:.1f}" height="{max(abs(y1 - y0), 1.5):.1f}" '
            f'fill="{color}" rx="3"/>'
        )
        # El número, fuera de la barra: dentro se pierde cuando la barra es corta.
        ty = (y1 - 8) if v >= 0 else (y1 + 17)
        p.append(
            f'<text x="{cx:.1f}" y="{ty:.1f}" fill="{color}" font-size="15" '
            f'font-weight="700" text-anchor="middle">{esc(rot)}</text>'
        )

    p.append(
        f'<text x="{izq - 34}" y="{arr - 8}" fill="{TENUE}" font-size="12">ms</text>'
    )
    # El pie va CENTRADO y en su propia línea. Pegado a la derecha se metía
    # debajo de la última etiqueta y las dos se leían como una sola.
    p.append(
        f'<text x="{izq + w / 2:.1f}" y="{alto - 6}" fill="{TENUE}" '
        f'font-size="12.5" text-anchor="middle">{esc(pie)}</text>'
    )

    return (
        f'<svg viewBox="0 0 {ancho} {alto}" width="100%" role="img" '
        f'aria-label="{esc(pie)}">' + "".join(p) + "</svg>"
    )


# ---------------------------------------------------------------------------
# Gráfico 3: la línea y la media
# ---------------------------------------------------------------------------

def linea_hrv(puntos, media, ancho=340, alto=210):
    """La HRV de los últimos meses y una raya horizontal: tu media. Nada más.

    Sin banda de cuartiles, sin los palos de las salidas y sin la raya del corte.
    Las tres cosas estaban antes y las tres obligaban a una leyenda.
    """
    izq, der, arr, aba = 34, 10, 22, 34
    w, h = ancho - izq - der, alto - arr - aba

    def dnum(iso):
        a, m, d = (int(x) for x in iso.split("-"))
        return a * 372 + (m - 1) * 31 + d  # orden, no calendario exacto

    suaves = [(p["fecha"], p["suave"]) for p in puntos if p.get("suave") is not None]
    t0, t1 = dnum(suaves[0][0]), dnum(suaves[-1][0])
    span = (t1 - t0) or 1
    x = lambda iso: izq + (dnum(iso) - t0) / span * w  # noqa: E731

    vals = [v for _, v in suaves] + [media]
    lo, hi = min(vals), max(vals)
    margen = (hi - lo) * 0.15 or 1
    lo, hi = lo - margen, hi + margen
    y = lambda v: arr + (1 - (v - lo) / (hi - lo)) * h  # noqa: E731

    p = []
    paso = paso_bonito(hi - lo, 5)
    t = paso * round(lo / paso)
    while t <= hi + 1e-9:
        if lo <= t <= hi:
            p.append(
                f'<line x1="{izq}" y1="{y(t):.1f}" x2="{ancho - der}" '
                f'y2="{y(t):.1f}" stroke="{REJILLA}" stroke-width="1"/>'
                f'<text x="{izq - 6}" y="{y(t) + 4:.1f}" fill="{TENUE}" '
                f'font-size="12" text-anchor="end">{esc(nm(t, 0))}</text>'
            )
        t += paso

    # Los meses, en el eje de abajo. Una etiqueta por mes y en español corto.
    visto = set()
    for fecha, _ in suaves:
        a, m, _d = fecha.split("-")
        if (a, m) in visto:
            continue
        visto.add((a, m))
        xx = x(fecha)
        if xx < izq + 4 or xx > ancho - der - 4:
            continue
        p.append(
            f'<line x1="{xx:.1f}" y1="{arr + h}" x2="{xx:.1f}" y2="{arr + h + 4}" '
            f'stroke="{REJILLA}" stroke-width="1"/>'
            f'<text x="{xx:.1f}" y="{alto - 14}" fill="{TENUE}" font-size="12" '
            f'text-anchor="middle">{MESES[int(m) - 1]}</text>'
        )

    camino = ""
    for i, (fecha, v) in enumerate(suaves):
        camino += f'{"L" if i else "M"}{x(fecha):.1f},{y(v):.1f}'
    p.append(
        f'<path d="{camino}" fill="none" stroke="{AZUL}" stroke-width="2.4" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
    )

    # La media, rotulada DENTRO del dibujo. Fuera, a la derecha, hacía falta un
    # margen de 46 píxeles que se le quitaban a la línea, y aun así el rótulo se
    # salía en una pantalla estrecha.
    p.append(
        f'<line x1="{izq}" y1="{y(media):.1f}" x2="{ancho - der}" '
        f'y2="{y(media):.1f}" stroke="{TENUE}" stroke-width="1.4" '
        f'stroke-dasharray="5 4"/>'
        f'<rect x="{ancho - der - 96}" y="{y(media) - 21:.1f}" width="96" '
        f'height="17" rx="3" fill="#1c2027"/>'
        f'<text x="{ancho - der - 4}" y="{y(media) - 8:.1f}" fill="{TENUE}" '
        f'font-size="12.5" text-anchor="end">tu media, {esc(nm(media, 0))} ms</text>'
    )
    p.append(
        f'<text x="{izq - 34}" y="{arr + 2}" fill="{TENUE}" font-size="12">ms</text>'
    )

    return (
        f'<svg viewBox="0 0 {ancho} {alto}" width="100%" role="img" '
        f'aria-label="la HRV de los últimos meses con tu media">'
        + "".join(p) + "</svg>"
    )


# ---------------------------------------------------------------------------
# Gráfico 4: el quesito
# ---------------------------------------------------------------------------

def quesito(trozos, total, ancho=340, alto=190):
    """Un anillo con dos trozos y su leyenda al lado, no dentro.

    Los rótulos van fuera porque dentro, en un móvil, o se salen o hay que
    encogerlos hasta que no se lean.
    """
    import math

    cx, cy, r, grueso = 78, 92, 56, 28
    p, ang = [], -math.pi / 2

    for _etq, _sub, n, color in trozos:
        barrido = 2 * math.pi * n / total
        x0, y0 = cx + r * math.cos(ang), cy + r * math.sin(ang)
        ang += barrido
        x1, y1 = cx + r * math.cos(ang), cy + r * math.sin(ang)
        largo = 1 if barrido > math.pi else 0
        p.append(
            f'<path d="M{x0:.1f},{y0:.1f} A{r},{r} 0 {largo} 1 {x1:.1f},{y1:.1f}" '
            f'fill="none" stroke="{color}" stroke-width="{grueso}"/>'
        )

    p.append(
        f'<text x="{cx}" y="{cy + 2}" fill="{TEXTO}" font-size="28" '
        f'font-weight="700" text-anchor="middle">{total}</text>'
        f'<text x="{cx}" y="{cy + 22}" fill="{TENUE}" font-size="13" '
        f'text-anchor="middle">salidas</text>'
    )

    yy = 66
    for etq, sub, n, color in trozos:
        p.append(
            f'<rect x="150" y="{yy - 13}" width="14" height="14" rx="3" fill="{color}"/>'
            f'<text x="172" y="{yy}" fill="{TEXTO}" font-size="16">'
            f'{n} {esc(etq)}</text>'
            f'<text x="172" y="{yy + 19}" fill="{TENUE}" font-size="12.5">'
            f'{esc(sub)}</text>'
        )
        yy += 54

    return (
        f'<svg viewBox="0 0 {ancho} {alto}" width="100%" role="img" '
        f'aria-label="reparto de las salidas entre duras y suaves">'
        + "".join(p) + "</svg>"
    )


# ---------------------------------------------------------------------------
# La página
# ---------------------------------------------------------------------------

def bloque(titulo, svg, frase, detalle):
    return (
        f'<section class="bloque">'
        f'<h2>{esc(titulo)}</h2>'
        f'<div class="lienzo">{svg}</div>'
        f'<p class="frase">{esc(frase)}</p>'
        f'<details class="detalle"><summary>ver detalle</summary>'
        f'<div class="dentro">{detalle}</div></details>'
        f'</section>'
    )


def tabla(cabeceras, filas):
    # Envuelta en un `div` que puede desplazarse a lo ancho. Sin él, en un móvil
    # de 360 puntos la tabla se sale de la caja y arrastra la página entera hacia
    # la derecha: la pantalla se lee torcida sin que nada parezca roto.
    return (
        '<div class="tabla-scroll"><table><thead><tr>'
        + "".join(f"<th>{esc(c)}</th>" for c in cabeceras)
        + "</tr></thead><tbody>"
        + "".join(
            "<tr>" + "".join(f"<td>{c}</td>" for c in f) + "</tr>" for f in filas
        )
        + "</tbody></table></div>"
    )


def main() -> int:
    d = json.loads((AQUI / "umbral-payload.json").read_text("utf-8"))
    u, g, rec = d["umbral"], d["grafica"], d["recuperacion"]
    f = u["frontera"]
    c = f["corte"]

    # --- 1. el escalón por tramos de carga ---------------------------------
    tramos = u["tramos"]
    etiquetas = []
    for t in tramos:
        if t["ultimo"]:
            etiquetas.append(["más de", f"{t['desde_carga']:.0f}"])
        else:
            etiquetas.append([f"{t['desde_carga']:.0f} a", f"{t['hasta_carga']:.0f}"])
    duro = tramos[-1]
    g1 = barras(
        [t["media"] for t in tramos],
        [rotulo(t["frase"]) for t in tramos],
        etiquetas,
        "carga de la salida",
    )
    frase1 = (
        f"Solo las salidas de más de {duro['desde_carga']:.0f} de carga se notan: "
        f"la HRV {duro['frase']} a la mañana siguiente."
    )
    det1 = (
        f"<p>{esc(f['lectura'])}</p>"
        + tabla(
            ["Tramo", "Salidas", "HRV al día siguiente", "Mitad central"],
            [
                (esc(t["etiqueta"]), t["n"],
                 f"{nm(t['media'], 2)} ms" + (" *" if t["aviso"] else ""),
                 f"{nm(t['p25'])} a {nm(t['p75'])}")
                for t in tramos
            ],
        )
        + "<p class='nota'>* pocas salidas en ese tramo para fiarse de la media.</p>"
        + f"<p class='nota'>Los cuatro grupos de la gráfica son cuartiles: "
          f"{', '.join(str(t['n']) for t in tramos[:-1])} y {tramos[-1]['n']} "
          f"salidas. El corte del motor se busca por deciles y cae en "
          f"{nm(f['carga'], 1)}, por eso no coincide con ningún borde de los "
          f"cuatro grupos.</p>"
        + f"<p class='nota'>Partiendo en {nm(f['carga'], 0)}: por debajo "
          f"{c['debajo']['frase']} ({c['n_debajo']} salidas), por encima "
          f"{c['encima']['frase']} ({c['n_encima']} salidas). Diferencia "
          f"{nm(c['diferencia'])} ms · p corregida {nm(c['p_corregida'], 3)} · "
          f"{'aguanta' if c['significativa'] else 'no aguanta'} la corrección sobre "
          f"{f['miradas']} miradas.</p>"
        + f"<p class='nota'>{esc(f['escalon'])}</p>"
        + f"<p class='nota'>Relación suave (sin partir por ningún sitio): "
          f"r = {nm(f['continua']['r'], 2)} · p corregida "
          f"{nm(f['continua']['p_corregida'], 3)} · "
          f"{'aguanta' if f['continua']['significativa'] else 'no aguanta'} la "
          f"corrección. Método: {f['continua']['metodo']}.</p>"
        + "<p class='nota'>Los cortes que se probaron y perdieron:</p>"
        + tabla(
            ["Corte", "Debajo", "Encima", "Diferencia", "p corregida"],
            [
                (nm(k["carga"], 0), k["n_debajo"], k["n_encima"],
                 nm(k["diferencia"]), nm(k["p_corregida"], 3)
                 + (" ✓" if k["significativa"] else ""))
                for k in f["candidatos"]
            ],
        )
    )

    # --- 2. cuánto tarda en volver -----------------------------------------
    dura = rec["curvas"][0]
    g2 = barras(
        [p["media"] for p in dura["por_dia"]],
        [rotulo(p["frase"]) for p in dura["por_dia"]],
        [[f"+{p['dia']}"] for p in dura["por_dia"]],
        "días después de la salida",
    )
    frase2 = (
        f"Una noche: al día siguiente la HRV {dura['por_dia'][0]['frase']} y "
        f"el día +{dura['vuelve_el_dia']} ya ha vuelto."
    )
    det2 = (
        f"<p>{esc(dura['lectura'])}</p>"
        + f"<p class='nota'>{esc(dura['aviso'])}</p>"
        + tabla(
            ["Día", "HRV", "Salidas", "Mitad central"],
            [(f"+{p['dia']}", f"{nm(p['media'], 2)} ms", p["n"],
              f"{nm(p['p25'])} a {nm(p['p75'])}") for p in dura["por_dia"]],
        )
        + "<p class='nota'>La misma cuenta con TODAS las salidas, duras y suaves:</p>"
        + tabla(
            ["Día", "HRV", "Salidas", "Mitad central"],
            [(f"+{p['dia']}", f"{nm(p['media'], 2)} ms", p["n"],
              f"{nm(p['p25'])} a {nm(p['p75'])}")
             for p in rec["curvas"][1]["por_dia"]],
        )
        + f"<p class='nota'>{esc(rec['nota'])}</p>"
        + f"<p class='nota'>{esc(d['sin_p'])}</p>"
    )

    # --- 3. la línea -------------------------------------------------------
    pts = g["puntos"]
    crudos = [p["valor"] for p in pts if p.get("valor") is not None]
    media = sum(crudos) / len(crudos)          # ← al backend si esto se aprueba
    ahora = [p["suave"] for p in pts if p.get("suave") is not None][-1]
    g3 = linea_hrv(pts, media)
    frase3 = (
        f"Tu media son {nm(media, 0)} ms y ahora andas por {nm(ahora, 0)}."
    )
    det3 = (
        f"<p class='nota'>La línea es la media de {g['suavizado']} días sobre "
        f"{g['n']} noches medidas, del {esc(d['ventana']['desde'])} al "
        f"{esc(d['ventana']['hasta'])}. La medida de cada noche suelta va de "
        f"{nm(g['rango'][0], 0)} a {nm(g['rango'][1], 0)} ms.</p>"
        f"<p class='nota'>Tu mitad central está entre {nm(g['banda']['desde'], 0)} y "
        f"{nm(g['banda']['hasta'], 0)} ms: la mitad de tus noches caen ahí dentro.</p>"
        f"<p class='nota'>{esc(d['convenio'])}</p>"
    )

    # --- 4. el quesito -----------------------------------------------------
    n_duras, n_suaves = c["n_encima"], c["n_debajo"]
    total = n_duras + n_suaves
    g4 = quesito(
        [("duras", f"más de {nm(f['carga'], 0)} de carga", n_duras, NARANJA),
         ("suaves", f"hasta {nm(f['carga'], 0)}", n_suaves, AZUL)],
        total,
    )
    de_cada_diez = round(n_duras / total * 10)   # ← al backend si esto se aprueba
    frase4 = (
        f"De cada 10 salidas, {de_cada_diez} son de las que te cuestan una noche."
    )
    det4 = (
        f"<p class='nota'>{esc(d['salidas']['resumen'])}</p>"
        f"<p class='nota'>De las {d['salidas']['medidas']} salidas medidas, "
        f"{d['salidas']['aisladas']} están aisladas -sin otra salida en los "
        f"{rec['aislamiento']} días de antes ni de después- y son las únicas que "
        f"entran en la curva de recuperación.</p>"
        f"<p class='nota'>Garmin cubre del {esc(d['cobertura']['garmin']['desde'])} "
        f"al {esc(d['cobertura']['garmin']['hasta'])}; la bici, del "
        f"{esc(d['cobertura']['bici']['desde'])} al "
        f"{esc(d['cobertura']['bici']['hasta'])}.</p>"
    )

    cuerpo = (
        bloque("Cuánto te baja la HRV según la salida", g1, frase1, det1)
        + bloque("Cuánto tarda en volver", g2, frase2, det2)
        + bloque("Tu HRV desde marzo", g3, frase3, det3)
        + bloque("Cómo son tus salidas", g4, frase4, det4)
    )

    html = PLANTILLA.format(
        cuerpo=cuerpo,
        dias=d["ventana"]["dias"],
        salidas=d["salidas"]["medidas"],
        hasta=d["ventana"]["hasta"],
    )
    # La SALIDA sí va a `out/`, que es donde se tira lo generado. Escribir el
    # HTML dentro del repositorio metería un artefacto de 17 KB en cada commit.
    destino = RAIZ / "out" / "panel" / "umbral.html"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(html, encoding="utf-8")

    print(f"escrito {destino}  ({destino.stat().st_size / 1024:.0f} kB)")
    print(f"  tramos   : {[nm(t['media']) for t in tramos]}")
    print(f"  recupera : {[nm(p['media']) for p in dura['por_dia']]}")
    print(f"  media HRV: {nm(media, 1)}  ·  ahora {nm(ahora, 1)}")
    print(f"  salidas  : {n_duras} duras / {n_suaves} suaves")
    return 0


PLANTILLA = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>La bici y tu descanso</title>
<style>
:root {{
  --fondo: #14161a; --caja: #1c2027; --borde: #2b313b;
  --texto: #e7eaef; --tenue: #9aa4b2; --acento: #4c8dff;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 1rem 1rem 3rem;
  background: var(--fondo); color: var(--texto);
  font: 17px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  -webkit-text-size-adjust: 100%;
}}
main {{ max-width: 34rem; margin: 0 auto; }}
header {{ margin-bottom: 1.6rem; }}
h1 {{ margin: 0; font-size: 1.5rem; letter-spacing: -0.01em; }}
.sub {{ margin: 0.2rem 0 0; color: var(--tenue); font-size: 0.95rem; }}

.bloque {{ margin: 0 0 2.4rem; }}
.bloque h2 {{
  margin: 0 0 0.6rem; font-size: 1.14rem; font-weight: 600;
  letter-spacing: -0.01em;
}}
.lienzo {{
  background: var(--caja); border: 1px solid var(--borde);
  border-radius: 0.8rem; padding: 0.7rem 0.6rem 0.4rem;
}}
.frase {{ margin: 0.85rem 0 0; font-size: 1.05rem; line-height: 1.45; }}

details.detalle {{ margin-top: 0.5rem; }}
details.detalle > summary {{
  color: var(--tenue); font-size: 0.92rem; padding: 0.45rem 0;
  cursor: pointer; list-style: none;
}}
details.detalle > summary::-webkit-details-marker {{ display: none; }}
details.detalle > summary::after {{ content: " ▾"; }}
details.detalle[open] > summary::after {{ content: " ▴"; }}
.dentro {{
  border-left: 2px solid var(--borde); padding-left: 0.85rem;
  color: var(--tenue); font-size: 0.92rem;
}}
.dentro p {{ margin: 0 0 0.7rem; }}
.dentro .nota {{ font-size: 0.88rem; }}
.tabla-scroll {{ overflow-x: auto; margin: 0 0 0.8rem; }}
.dentro table {{
  border-collapse: collapse; font-size: 0.86rem; min-width: 100%;
}}
.dentro th, .dentro td {{
  text-align: left; padding: 0.3rem 0.4rem 0.3rem 0;
  border-bottom: 1px solid var(--borde); white-space: nowrap;
}}
.dentro th {{ color: var(--tenue); font-weight: 600; }}

footer {{
  margin-top: 2rem; padding-top: 1rem; border-top: 1px solid var(--borde);
  color: var(--tenue); font-size: 0.84rem;
}}
</style>
</head>
<body>
<main>
<header>
  <h1>La bici y tu descanso</h1>
  <p class="sub">Últimos {dias} días · {salidas} salidas · hasta el {hasta}</p>
</header>
{cuerpo}
<footer>
  Maqueta de la pantalla del umbral con el criterio nuevo. Los números son los
  reales de <code>/api/metrics/umbral</code>; lo único que se ha añadido a mano
  es tu media de HRV y el «de cada 10» del quesito, que hoy el servidor no manda.
</footer>
</main>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
