"""Los PNG del manifest, dibujados desde el SVG y no a mano.

El manifest solo ofrecía SVG. Funciona en un Chrome reciente y no funciona en
todo lo demás: Android dejó de tragar SVG en el icono de inicio hasta hace
relativamente poco, y Safari en el iPhone no mira el manifest para eso -quiere
un `apple-touch-icon` y lo quiere en PNG-. El fallo no es un error en ningún
sitio: es un cuadrado blanco con una letra dentro en la pantalla de inicio, que
uno interpreta como "la instalación no ha ido bien" y no como "falta un
formato".

POR QUÉ UN GENERADOR Y NO CINCO PNG SUELTOS
-------------------------------------------
Porque un binario metido en el repositorio no tiene forma de seguir siendo
verdad. Se cambia un color en el SVG, el PNG se queda con el de antes, y nadie
mira un icono de 192 píxeles lo bastante de cerca como para verlo: acabas con
dos semáforos de colores distintos según por dónde se abra la aplicación.

Y por eso este programa LEE EL SVG en vez de repetir su geometría en Python.
Repetirla sería volver a tener dos descripciones del mismo dibujo, que es
exactamente el problema que se quería quitar, solo que en otro sitio.

`tests/test_pwa.py` vuelve a dibujar y compara PÍXELES -no bytes- con lo que hay
guardado. Bytes compararía también la versión de zlib, que no es asunto nuestro.

CÓMO SE USA
-----------
    python scripts/generar_iconos.py             # los escribe
    python scripts/generar_iconos.py --comprobar # dice si están al día

EL SUBCONJUNTO DE SVG QUE ENTIENDE, Y QUÉ PASA CON EL RESTO
-----------------------------------------------------------
Rectángulos (con esquinas redondeadas y con borde) y círculos. Nada más, porque
nada más hay en los dos ficheros. Lo que no entiende NO lo ignora: revienta. Un
`<path>` nuevo saltado en silencio daría un PNG al que le falta un trozo del
dibujo, y esa es justo la clase de fallo que no se ve hasta que ya está
instalado en el móvil.
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

ICONOS = Path(__file__).resolve().parent.parent / "static" / "icons"

SVG_NS = "{http://www.w3.org/2000/svg}"

# Qué se dibuja, de qué SVG y de qué tamaño.
#
# El `apple-touch-icon` sale del MASKABLE y no del normal, y no es un descuido:
# iOS le aplica su propia máscara redondeada al icono y no respeta la
# transparencia -rellena de blanco o de negro lo que no pintes-. Con el icono
# normal, que ya trae sus esquinas redondeadas, saldría un rectángulo redondeado
# dentro de otro. La versión maskable es a sangre y con el dibujo en el 80%
# central, que es exactamente lo que ese sitio necesita.
SALIDAS = [
    ("icon.svg", "icon-192.png", 192),
    ("icon.svg", "icon-512.png", 512),
    ("icon-maskable.svg", "icon-maskable-192.png", 192),
    ("icon-maskable.svg", "icon-maskable-512.png", 512),
    ("icon-maskable.svg", "apple-touch-icon.png", 180),
]

# Sub-líneas por fila de píxel. El dentado se ve sobre todo en las
# circunferencias y en las esquinas redondeadas, que es casi todo lo que hay
# aquí. En horizontal no hace falta muestrear: la cobertura de cada extremo del
# tramo se calcula exacta, porque un círculo y un rectángulo redondeado cortan
# cada línea en un solo intervalo.
SUBMUESTRAS = 8


class SvgNoEntendido(Exception):
    """Algo del SVG que este programa no sabe dibujar.

    Existe como excepción propia para que el test pueda comprobar que salta, en
    vez de conformarse con que "algo falle".
    """


# ---------------------------------------------------------------------------
# Leer el SVG
# ---------------------------------------------------------------------------


def _color(texto: str) -> tuple[int, int, int]:
    t = (texto or "").strip()
    if not (len(t) == 7 and t[0] == "#"):
        raise SvgNoEntendido(
            f"color {texto!r}: aquí solo se usan colores en #rrggbb, y traducir "
            f"nombres o `rgb()` sería inventarse medio CSS para dos ficheros"
        )
    try:
        return int(t[1:3], 16), int(t[3:5], 16), int(t[5:7], 16)
    except ValueError as e:
        raise SvgNoEntendido(f"color {texto!r} no es hexadecimal") from e


def _num(el, nombre: str, defecto: float | None = None) -> float:
    v = el.get(nombre)
    if v is None:
        if defecto is None:
            raise SvgNoEntendido(f"<{el.tag}> sin `{nombre}`")
        return defecto
    return float(v)


def _sin_atributos_raros(el, conocidos: set[str]) -> None:
    """Un atributo que no se mira es un atributo que cambia el dibujo y no el PNG.

    `opacity="0.5"` o `transform="rotate(...)"` no dan ningún error al leer el
    XML: simplemente no se aplican aquí y sí en el navegador, y entonces el SVG
    y el PNG dejan de ser el mismo icono.
    """
    sobran = set(el.attrib) - conocidos
    if sobran:
        raise SvgNoEntendido(
            f"<{el.tag.replace(SVG_NS, '')}> trae {sorted(sobran)}, que este "
            f"dibujante no aplica. O se implementa, o se quita del SVG: dejarlo "
            f"hace que el PNG y el SVG sean dos iconos distintos."
        )


def leer_svg(ruta: Path) -> tuple[float, list[dict]]:
    """Devuelve el lado del `viewBox` y las figuras, en orden de pintado."""
    raiz = ET.parse(ruta).getroot()
    if raiz.tag != f"{SVG_NS}svg":
        raise SvgNoEntendido(f"{ruta.name}: la raíz no es <svg>")

    caja = [float(v) for v in (raiz.get("viewBox") or "").split()]
    if len(caja) != 4 or caja[0] != 0 or caja[1] != 0 or caja[2] != caja[3]:
        raise SvgNoEntendido(
            f"{ruta.name}: viewBox {raiz.get('viewBox')!r}. Se espera cuadrado y "
            f"empezando en 0 0; un icono no cuadrado se deforma al instalarlo"
        )
    lado = caja[2]

    figuras: list[dict] = []
    for el in raiz:
        etiqueta = el.tag.replace(SVG_NS, "")
        if etiqueta == "rect":
            _sin_atributos_raros(
                el, {"x", "y", "width", "height", "rx", "fill", "stroke", "stroke-width"}
            )
            x = _num(el, "x", 0.0)
            y = _num(el, "y", 0.0)
            w = _num(el, "width")
            h = _num(el, "height")
            rx = _num(el, "rx", 0.0)
            relleno = _color(el.get("fill"))
            borde = el.get("stroke")
            if borde:
                # El borde de SVG va CENTRADO en el trazo: la mitad fuera y la
                # mitad dentro. Así que se pinta el rectángulo crecido medio
                # grosor con el color del borde, y encima el mismo rectángulo
                # encogido medio grosor con el del relleno. Desplazar un
                # rectángulo redondeado da otro rectángulo redondeado, así que
                # sale exacto y no hace falta dibujar contornos.
                g = _num(el, "stroke-width", 1.0) / 2.0
                figuras.append(
                    {"t": "rect", "x": x - g, "y": y - g, "w": w + 2 * g,
                     "h": h + 2 * g, "rx": max(0.0, rx + g), "color": _color(borde)}
                )
                figuras.append(
                    {"t": "rect", "x": x + g, "y": y + g, "w": w - 2 * g,
                     "h": h - 2 * g, "rx": max(0.0, rx - g), "color": relleno}
                )
            else:
                figuras.append(
                    {"t": "rect", "x": x, "y": y, "w": w, "h": h, "rx": rx,
                     "color": relleno}
                )
        elif etiqueta == "circle":
            _sin_atributos_raros(el, {"cx", "cy", "r", "fill"})
            figuras.append(
                {"t": "circulo", "cx": _num(el, "cx"), "cy": _num(el, "cy"),
                 "r": _num(el, "r"), "color": _color(el.get("fill"))}
            )
        else:
            raise SvgNoEntendido(
                f"{ruta.name}: <{etiqueta}> no se sabe dibujar. Saltárselo en "
                f"silencio dejaría el PNG sin ese trozo del icono, y un icono "
                f"de 192 píxeles nadie lo mira tan de cerca."
            )

    if not figuras:
        raise SvgNoEntendido(f"{ruta.name}: no hay nada que dibujar")
    return lado, figuras


# ---------------------------------------------------------------------------
# Dibujar
# ---------------------------------------------------------------------------


def _tramo(fig: dict, yy: float) -> tuple[float, float] | None:
    """Dónde corta la figura a la línea horizontal `yy`, en unidades del SVG.

    Un solo intervalo, siempre: es lo que hace que no haga falta muestrear en
    horizontal. Deja de ser cierto en cuanto alguien meta un `<path>`, y por eso
    `leer_svg` no deja meter uno.
    """
    if fig["t"] == "circulo":
        dy = yy - fig["cy"]
        if abs(dy) >= fig["r"]:
            return None
        medio = math.sqrt(fig["r"] * fig["r"] - dy * dy)
        return fig["cx"] - medio, fig["cx"] + medio

    x, y, w, h, rx = fig["x"], fig["y"], fig["w"], fig["h"], fig["rx"]
    if w <= 0 or h <= 0 or yy < y or yy >= y + h:
        return None
    rx = min(rx, w / 2.0, h / 2.0)
    if rx > 0 and yy < y + rx:
        d = (y + rx) - yy
    elif rx > 0 and yy > y + h - rx:
        d = yy - (y + h - rx)
    else:
        d = 0.0
    mete = rx - math.sqrt(max(0.0, rx * rx - d * d)) if d > 0 else 0.0
    return x + mete, x + w - mete


def _cobertura(fig: dict, lado: int, escala: float) -> list[float]:
    """Cuánto de cada píxel tapa la figura, de 0 a 1."""
    cob = [0.0] * (lado * lado)
    peso = 1.0 / SUBMUESTRAS
    for fila in range(lado):
        # Los dos píxeles de los extremos se llevan su trozo exacto; el relleno
        # de en medio se acumula como diferencias y se suma una sola vez por
        # fila. Sin esto, un icono de 512 son diez millones de vueltas de bucle
        # y el test que lo comprueba pasa a tardar más que el resto juntos.
        borde = [0.0] * lado
        salto = [0.0] * (lado + 1)
        hay = False
        for s in range(SUBMUESTRAS):
            tramo = _tramo(fig, (fila + (s + 0.5) * peso) * escala)
            if tramo is None:
                continue
            x0 = max(0.0, tramo[0] / escala)
            x1 = min(float(lado), tramo[1] / escala)
            if x1 <= x0:
                continue
            hay = True
            i0 = int(math.floor(x0))
            i1 = int(math.ceil(x1))
            if i1 - i0 == 1:
                borde[i0] += (x1 - x0) * peso
                continue
            borde[i0] += (i0 + 1 - x0) * peso
            borde[i1 - 1] += (x1 - (i1 - 1)) * peso
            if i1 - 1 > i0 + 1:
                salto[i0 + 1] += peso
                salto[i1 - 1] -= peso
        if not hay:
            continue
        base = fila * lado
        acumulado = 0.0
        for i in range(lado):
            acumulado += salto[i]
            v = acumulado + borde[i]
            if v > 0.0:
                cob[base + i] = 1.0 if v > 1.0 else v
    return cob


def dibujar(lado_svg: float, figuras: list[dict], lado: int) -> bytearray:
    """RGBA de `lado`x`lado`, pintando las figuras una encima de otra."""
    escala = lado_svg / lado
    lienzo = bytearray(lado * lado * 4)  # transparente
    for fig in figuras:
        r, g, b = fig["color"]
        for i, cob in enumerate(_cobertura(fig, lado, escala)):
            if cob <= 0.0:
                continue
            p = i * 4
            inv = 1.0 - cob
            # Composición sobre fondo transparente ("source-over" con alfa
            # premultiplicado por la cobertura), que es lo que hace el navegador.
            a_dst = lienzo[p + 3] / 255.0
            a = cob + a_dst * inv
            if a <= 0.0:
                continue
            for c, valor in enumerate((r, g, b)):
                mezcla = (valor * cob + lienzo[p + c] * a_dst * inv) / a
                lienzo[p + c] = min(255, max(0, int(round(mezcla))))
            lienzo[p + 3] = min(255, max(0, int(round(a * 255))))
    return lienzo


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------


def _trozo(tipo: bytes, datos: bytes) -> bytes:
    return (
        struct.pack(">I", len(datos))
        + tipo
        + datos
        + struct.pack(">I", zlib.crc32(tipo + datos) & 0xFFFFFFFF)
    )


def a_png(rgba: bytes, lado: int) -> bytes:
    """PNG de color verdadero con alfa, con filtro 0 en todas las filas.

    Filtro 0 -o sea ninguno- a propósito: comprime algo peor y a cambio el
    lector de los tests es restar un byte por fila en vez de implementar los
    cinco filtros del formato. Un icono de 512 son 30 KB de más; un
    descodificador de cinco filtros escrito para un test es una fuente de fallos
    propia.
    """
    filas = bytearray()
    for f in range(lado):
        filas.append(0)
        filas += rgba[f * lado * 4:(f + 1) * lado * 4]
    cabecera = struct.pack(">IIBBBBB", lado, lado, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _trozo(b"IHDR", cabecera)
        + _trozo(b"IDAT", zlib.compress(bytes(filas), 9))
        + _trozo(b"IEND", b"")
    )


def de_png(bruto: bytes) -> tuple[int, bytes]:
    """El inverso justo de `a_png`, para poder comparar píxeles y no bytes.

    Comparar bytes compararía de paso la versión de zlib de la máquina, que no
    es asunto de este proyecto: el mismo dibujo comprimido por otro zlib son
    otros bytes y el mismo icono.
    """
    if bruto[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("esto no es un PNG")
    i = 8
    lado = 0
    datos = bytearray()
    while i < len(bruto):
        n = struct.unpack(">I", bruto[i:i + 4])[0]
        tipo = bruto[i + 4:i + 8]
        cuerpo = bruto[i + 8:i + 8 + n]
        if tipo == b"IHDR":
            ancho, alto, prof, color = struct.unpack(">IIBB", cuerpo[:10])
            if ancho != alto or prof != 8 or color != 6:
                raise ValueError(f"PNG inesperado: {ancho}x{alto}, {prof} bits, tipo {color}")
            lado = ancho
        elif tipo == b"IDAT":
            datos += cuerpo
        elif tipo == b"IEND":
            break
        i += 12 + n
    crudo = zlib.decompress(bytes(datos))
    paso = lado * 4
    pixeles = bytearray()
    for f in range(lado):
        inicio = f * (paso + 1)
        if crudo[inicio] != 0:
            raise ValueError(
                f"fila {f} con filtro {crudo[inicio]}: este lector solo entiende "
                f"el 0, que es el único que escribe `a_png`"
            )
        pixeles += crudo[inicio + 1:inicio + 1 + paso]
    return lado, bytes(pixeles)


# ---------------------------------------------------------------------------


def pintar_todo() -> dict[str, bytes]:
    """Nombre de fichero -> PNG, sin tocar el disco."""
    cache: dict[str, tuple[float, list[dict]]] = {}
    salida = {}
    for origen, destino, lado in SALIDAS:
        if origen not in cache:
            cache[origen] = leer_svg(ICONOS / origen)
        lado_svg, figuras = cache[origen]
        salida[destino] = a_png(bytes(dibujar(lado_svg, figuras, lado)), lado)
    return salida


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--comprobar",
        action="store_true",
        help="no escribe: dice si lo guardado es lo que sale del SVG de hoy",
    )
    args = p.parse_args(argv)

    nuevos = pintar_todo()
    desfasados = []
    for nombre, datos in nuevos.items():
        ruta = ICONOS / nombre
        if args.comprobar:
            if not ruta.exists():
                desfasados.append(f"{nombre}: no está")
            elif de_png(ruta.read_bytes())[1] != de_png(datos)[1]:
                desfasados.append(f"{nombre}: no es lo que dibuja el SVG de hoy")
        else:
            ruta.write_bytes(datos)
            print(f"{nombre}: {len(datos):,} bytes")

    if args.comprobar:
        if desfasados:
            print("\n".join(desfasados), file=sys.stderr)
            print("arréglalo con: python scripts/generar_iconos.py", file=sys.stderr)
            return 1
        print(f"los {len(nuevos)} PNG están al día")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
