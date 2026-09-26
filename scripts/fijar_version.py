"""Pone la misma versión en los cuatro sitios que tienen que decirla.

POR QUÉ EXISTE ESTO, Y POR QUÉ NO ES UN `sed` EN EL TALLER (25/09/2026)
-----------------------------------------------------------------------
La versión está declarada en cuatro ficheros y `tests/test_despliegue.py` exige
que los cuatro digan lo mismo. Mientras se movía a mano, ese test bastaba: o se
cambiaban los cuatro, o la batería se ponía roja antes de subir nada.

Desde que la mueve el gancho `pre-commit` en cada commit, el test ya no
basta, y la razón es el defecto de siempre: **un `sed` que deja de casar no
falla, no cambia nada.** Si algún día `pyproject.toml` pasa a escribir `version="0.1.0"` sin
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

Y existe como script del repositorio, y no como líneas dentro del gancho ni
del YAML del taller, para que `tests/test_despliegue.py` pueda ejercitarlo. Lo
que vive solo dentro de un gancho o de un `run:` de GitHub Actions no lo prueba
nadie hasta que falla en producción.

QUIÉN LLAMA A QUÉ (26/09/2026)
------------------------------
- `.githooks/pre-commit`  -> `--siguiente`: calcula la versión que le toca al
  commit en curso y la escribe. El commit sale de la máquina ya definitivo.
- el taller de GitHub      -> `--verificar` y `--imprimir`: comprueba que el
  commit subió la versión respecto a su padre y la lee para etiquetar la
  imagen. NO ESCRIBE NADA. La primera versión del taller sí escribía -devolvía
  la versión en un commit suyo- y dejaba la copia local un commit por detrás
  después de cada push: dos historias que tenían que ser la misma y no lo eran.

Uso:

    python scripts/fijar_version.py --siguiente  # lo que hace el gancho
    python scripts/fijar_version.py --verificar  # lo que hace el taller
    python scripts/fijar_version.py --imprimir   # la versión, a secas
    python scripts/fijar_version.py --comprobar  # qué hay hoy en cada sitio
    python scripts/fijar_version.py 0.2.0        # subirla a mano (p.ej. la menor)
"""

from __future__ import annotations

import argparse
import re
import subprocess
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


def _leer(ruta: Path) -> str:
    """El fichero tal cual, CON sus finales de línea.

    `newline=""` y no el `read_text` a secas: en Windows, `read_text` traduce
    CRLF a LF al leer y `write_text` lo vuelve a poner al escribir, así que
    cada pasada del gancho reescribiría los cuatro ficheros con CRLF aunque
    vinieran con LF. Git lo normalizaría al commitear y no se notaría, que es
    exactamente el tipo de «no se nota» que este repositorio no se permite.
    """
    with ruta.open(encoding="utf-8", newline="") as f:
        return f.read()


def _escribir(ruta: Path, texto: str) -> None:
    with ruta.open("w", encoding="utf-8", newline="") as f:
        f.write(texto)


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
        texto = _leer(RAIZ / lugar.ruta)
        m = re.search(lugar.patron, texto)
        if not m:
            fuera.append((lugar, None))
            continue
        fuera.append((lugar, _extraer(m)))
    return fuera


def _extraer(m: "re.Match[str]") -> str:
    """Lo que queda tras el prefijo del grupo 1, sin la comilla final."""
    return m.group(0)[len(m.group(1)):].rstrip('"')


def _tupla(version: str) -> tuple[int, int, int]:
    """`"0.1.195"` -> `(0, 1, 195)`, para poder comparar. Revienta si no lo es."""
    if not VERSION_VALIDA.match(version):
        raise ValueError(f"{version!r} no es una version `mayor.menor.parche`")
    a, b, c = version.split(".")
    return int(a), int(b), int(c)


# ---------------------------------------------------------------------------
# La versión que le toca a un commit, y la comprobación de que subió
# ---------------------------------------------------------------------------


def _git(*args: str) -> str | None:
    """Salida de `git`, o `None` si el comando falla (sin HEAD, sin padre...)."""
    try:
        return subprocess.run(
            ["git", *args], cwd=RAIZ, capture_output=True, text=True,
            check=True, encoding="utf-8",
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def numero_de_commits() -> int:
    """Cuántos commits tiene HEAD. 0 en un repositorio sin ningún commit."""
    n = _git("rev-list", "--count", "HEAD")
    return int(n) if n and n.isdigit() else 0


def version_en(commit: str) -> tuple[int, int, int] | None:
    """La versión que declara `pyproject.toml` en ese commit; `None` si no hay tal commit."""
    texto = _git("show", f"{commit}:pyproject.toml")
    if texto is None:
        return None
    m = re.search(LUGARES[0].patron, texto)
    return _tupla(_extraer(m)) if m else None


def _version_del_arbol() -> tuple[int, int, int]:
    """La que dicen los cuatro ficheros ahora mismo. Revienta si no coinciden."""
    hay = leer_versiones()
    distintas = {v for _, v in hay}
    if len(distintas) != 1 or None in distintas:
        raise NoCasa(
            "los cuatro sitios no dicen lo mismo: "
            + ", ".join(f"{l.ruta}={v}" for l, v in hay)
            + ". Antes de nada, `python scripts/fijar_version.py --comprobar`"
        )
    return _tupla(distintas.pop())


def siguiente() -> str:
    """La versión que le toca al commit que se está haciendo ahora mismo.

    Casi siempre es el ordinal del commit: `0.1.<nº de commits + 1>`, así que
    el commit N declara `0.1.N`. Pero «casi» no vale para algo que tiene que
    CRECER siempre, y hay dos casos en los que el ordinal no basta:

    - Un `git commit --amend`. HEAD es el commit N y ya declara `0.1.N`; el
      ordinal volvería a dar `0.1.N` y la versión no subiría. Por eso se toma
      el máximo entre el ordinal y «la de HEAD más uno». Tras un amend sale
      `0.1.N+1` y el siguiente commit `0.1.N+2`; el ordinal se desalinea un
      número para siempre y no pasa nada, porque lo que Umbrel necesita es que
      crezca, no que coincida con nada.
    - Alguien la subió A MANO en este mismo commit -para cambiar la menor, por
      ejemplo: `fijar_version.py 0.2.0`-. Entonces el árbol declara MÁS que
      HEAD, y eso se respeta tal cual: pisarlo con un `0.1.<ordinal>` sería
      deshacer una decisión que alguien tomó a propósito.

    En un repositorio sin ningún commit se deja lo que haya.
    """
    arbol = _version_del_arbol()
    head = version_en("HEAD")
    if head is None or arbol > head:
        return "%d.%d.%d" % arbol
    mayor, menor, parche = head
    return f"{mayor}.{menor}.{max(numero_de_commits() + 1, parche + 1)}"


def verificar() -> list[str]:
    """Lo que el taller comprueba antes de construir. Vacía si todo está bien.

    Dos cosas, y las dos son «la pieza que tenía que avisar diciendo que no hay
    hueco»: que los cuatro sitios coincidan, y que este commit declare MÁS que
    su padre. Lo segundo es lo que detecta un commit hecho sin el gancho -o con
    `--no-verify`-: sale con la versión de siempre, el taller publicaría la
    etiqueta de siempre, y Umbrel no vería ninguna actualización que ofrecer.
    """
    try:
        actual = _version_del_arbol()
    except NoCasa as e:
        return [str(e)]
    padre = version_en("HEAD^")
    if padre is not None and not actual > padre:
        return [
            "este commit declara %d.%d.%d y su padre declaraba %d.%d.%d: la version "
            "NO HA SUBIDO. Casi seguro se hizo el commit sin el gancho pre-commit. "
            "Activalo una vez por clon con `git config core.hooksPath .githooks`; "
            "para este commit: `python scripts/fijar_version.py --siguiente` y "
            "`git commit --amend --no-edit`" % (*actual, *padre)
        ]
    return []


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
        nuevos.append((ruta, _sustituir(_leer(ruta), lugar, version)))

    tocados = []
    for (ruta, texto), lugar in zip(nuevos, LUGARES):
        if _leer(ruta) != texto:
            _escribir(ruta, texto)
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
    p.add_argument("--siguiente", action="store_true",
                   help="fija la version que le toca al commit en curso (lo usa el gancho)")
    p.add_argument("--verificar", action="store_true",
                   help="comprueba que este commit subio la version (lo usa el taller)")
    p.add_argument("--imprimir", action="store_true",
                   help="escribe la version actual y nada mas")
    args = p.parse_args(argv)

    if args.siguiente:
        v = siguiente()
        tocados = fijar(v)
        print("version %s (%s)" % (v, "ya estaba" if not tocados else "escrita en " + ", ".join(tocados)))
        return 0

    if args.verificar:
        problemas = verificar()
        for pr in problemas:
            print("NO SE PUBLICA:", pr, file=sys.stderr)
        if not problemas:
            print("version %d.%d.%d, sube respecto al padre" % _version_del_arbol())
        return 1 if problemas else 0

    if args.imprimir:
        print("%d.%d.%d" % _version_del_arbol())
        return 0

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
        p.error("hace falta la version, o una de --siguiente/--verificar/--imprimir/--comprobar")

    tocados = fijar(args.version)
    print(f"version {args.version} fijada en {len(LUGARES)} sitios "
          f"({len(tocados)} han cambiado: {', '.join(tocados) or 'ninguno'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
