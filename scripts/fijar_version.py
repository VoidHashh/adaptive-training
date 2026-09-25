"""Pone la misma versión en los cuatro sitios que tienen que decirla.

POR QUÉ EXISTE ESTO, Y POR QUÉ NO ES UN `sed` EN EL TALLER (25/09/2026)
-----------------------------------------------------------------------
La versión está declarada en cuatro ficheros y `tests/test_despliegue.py` exige
que los cuatro digan lo mismo. Mientras se movía a mano, ese test bastaba: o se
cambiaban los cuatro, o la batería se ponía roja antes de subir nada.

Desde que la mueve el taller en cada empujón, el test ya no basta, y la razón
es el defecto de siempre: **un `sed` que deja de casar no falla, no cambia
nada.** Si algún día `pyproject.toml` pasa a escribir `version="0.1.0"` sin
espacios, o el compose mete el digest detrás de la etiqueta, el patrón no
encuentra su línea, la sustitución no ocurre, el taller termina en verde y
publica una imagen con la etiqueta de siempre. El Umbrel entonces no ve
actualización ninguna, y desde el móvil eso se parece exactamente a que el
arreglo está puesto.

De ahí las dos decisiones de este fichero:

  - **Cada lugar afirma cuántas veces tiene que casar su patrón.** Ni cero
    -el patrón murió- ni dos -el patrón se ha vuelto ambiguo y estaría tocando
    una línea que no es-.
  - **Se vuelve a leer el fichero después de escribirlo** y se comprueba que
    ahora dice la versión pedida. Escribir y dar por hecho que se escribió es
    justo la clase de fe que este repositorio no se permite.

Y existe como script del repositorio, y no como cinco líneas dentro del YAML
del taller, para que `tests/test_despliegue.py` pueda ejercitarlo. Lo que vive
solo dentro de un `run:` de GitHub Actions no lo prueba nadie hasta que falla
en producción.

Uso:

    python scripts/fijar_version.py 0.1.191      # la fija en los cuatro
    python scripts/fijar_version.py --comprobar  # dice qué hay hoy en cada uno
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

# Una versión es `mayor.menor.parche`, y nada más. Se valida a la entrada
# porque el taller la construye con `git rev-list --count`, y el día que ese
# comando devuelva vacío -un clon superficial, por ejemplo- lo que llegaría
# aquí sería `0.1.` y se escribiría tal cual en los cuatro ficheros.
VERSION_VALIDA = re.compile(r"^\d+\.\d+\.\d+$")


@dataclass(frozen=True)
class Lugar:
    """Un sitio que declara la versión, y cómo reconocerlo."""

    ruta: str
    patron: str          # con UN grupo: lo que se sustituye por la versión
    veces: int           # cuántas veces tiene que casar. Ni una más, ni una menos
    porque: str          # qué se rompe si este no se mueve


LUGARES = (
    Lugar(
        "pyproject.toml",
        r'(?m)^(version\s*=\s*")[^"]+(")',
        1,
        "es la que el taller lee para etiquetar la imagen que publica",
    ),
    Lugar(
        "docker-compose.yml",
        r"(?m)^(\s*image:\s*adaptive-training:)\S+",
        1,
        "es la que se construye en el PC para desarrollo",
    ),
    Lugar(
        "umbrel/docker-compose.yml",
        r"(?m)^(\s*image:\s*ghcr\.io/voidhashh/adaptive-training:)\S+",
        1,
        "es LA QUE UMBREL INSTALA: si no se mueve, no se despliega nada",
    ),
    Lugar(
        "umbrel/umbrel-app.yml",
        r'(?m)^(version:\s*")[^"]+(")',
        1,
        "es la que Umbrel COMPARA para decidir si hay actualizacion que ofrecer",
    ),
)


class NoCasa(RuntimeError):
    """El patrón de un lugar no encontró lo que esperaba."""


def _sustituir(texto: str, lugar: Lugar, version: str) -> str:
    """Devuelve el texto con la versión puesta, o revienta diciendo por qué."""
    encontrados = len(re.findall(lugar.patron, texto))
    if encontrados != lugar.veces:
        raise NoCasa(
            f"{lugar.ruta}: el patron caso {encontrados} veces y tenian que ser "
            f"{lugar.veces}. Si el fichero ha cambiado de forma, hay que "
            f"arreglar el patron en scripts/fijar_version.py: este sitio "
            f"{lugar.porque}"
        )
    # `\g<1>` y no `\1` porque detrás va un dígito y `\11` seria otro grupo.
    #
    # Y el número de grupos se le pregunta al regex COMPILADO, no se cuenta
    # paréntesis en el texto del patrón: contándolos, el `(?m)` del principio
    # sumaba uno y el reemplazo pedía un `\g<2>` que no existía. Reventaba con
    # un error de `re` que hablaba del parser y no de esta línea.
    grupos = re.compile(lugar.patron).groups
    reemplazo = r"\g<1>" + version + (r"\g<2>" if grupos == 2 else "")
    return re.sub(lugar.patron, reemplazo, texto)


def leer_versiones() -> list[tuple[Lugar, str | None]]:
    """Qué versión dice hoy cada lugar. `None` si su patrón ya no casa.

    UNA LISTA Y NO UN DICCIONARIO POR RUTA, Y NO ES ESTILO (25/09/2026)
    -------------------------------------------------------------------
    Esto devolvía `{ruta: version}`, y con eso **dos lugares en el mismo
    fichero se pisaban**: el segundo machacaba la entrada del primero y la
    comprobación de después se quedaba mirando solo uno de los dos. Justo el
    caso que esa comprobación existe para cazar -el segundo `write_text` sobre
    el mismo fichero se lleva por delante el cambio del primero-, y lo dejaba
    pasar en verde. La guarda que tenía que avisar diciendo que no hay hueco.

    Lo encontró `test_dos_lugares_en_el_mismo_fichero_no_pasan_en_silencio`.
    """
    fuera: list[tuple[Lugar, str | None]] = []
    for lugar in LUGARES:
        texto = (RAIZ / lugar.ruta).read_text(encoding="utf-8")
        m = re.search(lugar.patron, texto)
        if not m:
            fuera.append((lugar, None))
            continue
        # Lo que queda tras el prefijo del grupo 1, sin la comilla final.
        fuera.append((lugar, m.group(0)[len(m.group(1)):].rstrip('"')))
    return fuera


def fijar(version: str) -> list[str]:
    """Escribe la versión en los cuatro sitios y COMPRUEBA que quedó escrita."""
    if not VERSION_VALIDA.match(version):
        raise ValueError(
            f"{version!r} no es una version `mayor.menor.parche`. El taller la "
            f"construye con `git rev-list --count`; si ese comando no devuelve "
            f"un numero, esto es lo que hay que ver antes de escribir nada"
        )

    # Se calcula TODO antes de escribir NADA. Con cuatro escrituras seguidas, un
    # patrón roto en el tercero dejaría dos ficheros movidos y dos quietos, que
    # es peor que no haber empezado: la batería se pondría roja en una mezcla
    # que nadie pidió.
    nuevos: list[tuple[Path, str]] = []
    for lugar in LUGARES:
        ruta = RAIZ / lugar.ruta
        nuevos.append((ruta, _sustituir(ruta.read_text(encoding="utf-8"), lugar, version)))

    tocados = []
    for (ruta, texto), lugar in zip(nuevos, LUGARES):
        if ruta.read_text(encoding="utf-8") != texto:
            ruta.write_text(texto, encoding="utf-8")
            tocados.append(lugar.ruta)

    # Y ahora se vuelve a leer del disco. No es paranoia: es la diferencia entre
    # «he escrito» y «está escrito», y el taller se fía de esto para publicar.
    mal = {l.ruta: v for l, v in leer_versiones() if v != version}
    if mal:
        raise NoCasa(
            f"tras escribir, estos siguen sin decir {version!r}: {mal}. "
            f"NO se puede publicar: la imagen saldria con una etiqueta que los "
            f"ficheros no declaran"
        )
    return tocados


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("version", nargs="?", help="mayor.menor.parche")
    p.add_argument("--comprobar", action="store_true",
                   help="solo dice que version hay en cada sitio")
    args = p.parse_args(argv)

    if args.comprobar:
        hay = leer_versiones()
        for lugar, v in hay:
            print(f"  {lugar.ruta:28} {v if v is not None else 'EL PATRON NO CASA'}")
        distintas = {v for _, v in hay}
        if len(distintas) != 1 or None in distintas:
            print("NO COINCIDEN", file=sys.stderr)
            return 1
        print(f"todos dicen {distintas.pop()}")
        return 0

    if not args.version:
        p.error("hace falta la version, o --comprobar")

    tocados = fijar(args.version)
    print(f"version {args.version} fijada en {len(LUGARES)} sitios "
          f"({len(tocados)} han cambiado: {', '.join(tocados) or 'ninguno'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
