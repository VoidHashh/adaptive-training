"""Los guiones de `scripts/` no pueden restaurar con git, y los de mutación
tienen que restaurar desde una copia.

QUÉ PASÓ
--------
Un guión de mutación cambia el código a propósito, corre la batería y deshace
el cambio. La forma cómoda de deshacerlo es `git checkout -- fichero`. La forma
correcta es copiar el fichero antes y volver a copiarlo encima en un `finally`.
No son equivalentes y la diferencia no se ve hasta que es tarde: `git checkout`
no deshace la mutación, **devuelve el fichero a HEAD**. Si había trabajo sin
commitear -que es el caso normal mientras se está escribiendo la cosa que se
quiere mutar-, ese trabajo desaparece sin preguntar y sin dejar rastro. No está
en el índice, no está en un `stash`, no está en el reflog: no llegó a existir
para git.

Pasó en este repositorio. Se perdió la implementación entera de `_baseline_gaps`
y se recuperó rehaciéndola desde el transcript de la sesión, que es una forma
muy cara de tener suerte.

Y la regla ya estaba escrita. Estaba en el docstring de
`scripts/mutar_salud_pwa.py`, en la línea 7, en un guión que la cumple. O sea
que la regla escrita en prosa, al lado de un ejemplo correcto, no impidió nada.
Es la misma figura que este proyecto lleva meses cazando -el validador que
certificaba una sección muerta, el documento que decía "no implementada" con
cinco vistas sirviendo-: **la pieza que tenía que avisar era la que decía que no
hacía falta avisar.**

POR QUÉ UN TEST Y NO UNA NOTA MÁS
---------------------------------
Una nota se lee si se abre el fichero donde está. Un test corre siempre. La
diferencia entre las dos cosas es exactamente la diferencia entre lo que pasó y
lo que se quiere que pase.

Aquí se atan tres cosas:

1. **Ningún guión puede EJECUTAR un git que descarte cambios.** Se mira el AST y
   no el texto, para que la regla se pueda seguir contando en prosa: los
   comentarios no llegan al AST y los docstrings se excluyen a mano. O sea que
   esta misma frase, `git checkout`, no rompe nada, y `subprocess.run(["git",
   "checkout", ...])` sí. Escribir sobre la avería está permitido; provocarla no.

2. **Todo `mutar_*.py` restaura desde copia dentro de un `finally`**, y la
   escritura del fichero mutado ocurre DENTRO de ese `try`. Un `finally` que
   restaura no sirve de nada si el fichero se ensucia antes de entrar en el
   bloque: bastaría un `Ctrl-C` entre las dos líneas para dejar el árbol de
   trabajo con una mutación dentro, y una mutación olvidada en el árbol es un
   fallo mudo de los caros -la suite pasa a mentir en la dirección cómoda-.

3. **El docstring de todo `mutar_*.py` cuenta la regla.** Esto no lo comprueba
   ningún ordenador y es a propósito: lo comprueba quien abra el fichero dentro
   de un año. La comprobación automática solo obliga a que la frase esté. Es
   barato y es lo que convierte "alguien escribió un guión nuevo copiando otro"
   en "alguien escribió un guión nuevo y leyó por qué".

LO QUE ESTO NO CUBRE
--------------------
Un guión que arme el comando desde variables (`cmd = VERBO; subprocess.run(cmd)`)
se escapa, porque el literal no está en la llamada. No se persigue: la red que
hay atrapa la forma en que la avería ocurrió de verdad y las tres o cuatro
maneras naturales de repetirla. Una red que intente atrapar a alguien decidido
a saltársela deja de atrapar accidentes, que son los que pasan.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.settings import REPO_ROOT

GUIONES_DIR = REPO_ROOT / "scripts"

# Las tres carpetas de código que se escriben a mano y pueden acabar corriendo
# git. `app/` entra también: el día que la aplicación restaure algo con git, se
# quiere ver aquí y no en producción.
CARPETAS = ("scripts", "tests", "app")

# Los subcomandos de git que DESCARTAN estado del árbol de trabajo o del índice.
# `log`, `status`, `rev-parse` y compañía no están y no deben estar: leer el
# repositorio es inofensivo y hay guiones que legítimamente querrían hacerlo.
VERBOS_QUE_DESTRUYEN = ("checkout", "restore", "reset", "clean", "stash")


# Este mismo fichero es el único que puede nombrar la avería en código: sus
# mensajes de error tienen que decir `git checkout` para que se entiendan, y
# `VERBOS_QUE_DESTRUYEN` es literalmente la lista de lo prohibido. La primera vez
# que se corrió, la guarda se pilló a sí misma, que es buena señal y mal
# resultado. Se excluye por ruta exacta y no por patrón: una exclusión del tipo
# `test_*guion*.py` sería una puerta que cualquiera puede usar poniéndole el
# nombre adecuado a su fichero.
EXCEPTO = {Path(__file__).resolve()}


def _fuente(ruta: Path) -> str:
    """Lee con `utf-8-sig` porque hay ficheros del repo que empiezan con BOM.

    `ast.parse` sobre un BOM revienta con `invalid non-printable character
    U+FEFF`, y un error de lectura aquí es un falso positivo: el guión no está
    haciendo nada malo, es esta prueba la que no sabe abrirlo. Un falso positivo
    en una guarda de convención es peor que uno en un test de lógica, porque la
    reacción natural a "esto falla y no es culpa mía" es apagar la guarda.
    """
    return ruta.read_text(encoding="utf-8-sig")


def _ficheros() -> list[Path]:
    """Los ficheros que la guarda tiene que mirar.

    LA LISTA VACÍA ES EL PEOR RESULTADO POSIBLE Y NO SE PARECE A UN FALLO.
    Esto alimenta un `parametrize`. Si `REPO_ROOT` apuntara mal, si una carpeta
    se renombrara, o si el paquete acabara instalado de otra forma, el `rglob`
    devolvería nada: cero casos generados, cero tests ejecutados, y pytest
    informa de una guarda que pasa. La convención dejaría de vigilarse el mismo
    día y la suite no bajaría ni un test de la cuenta que se mira.

    Se comprueba también carpeta a carpeta y no solo el total: con tres
    carpetas, que `app/` desaparezca del barrido deja las otras dos llenando la
    lista y el total sigue siendo convincente.
    """
    out: list[Path] = []
    for carpeta in CARPETAS:
        encontrados = sorted((REPO_ROOT / carpeta).rglob("*.py"))
        assert encontrados, (
            f"la guarda no encuentra ni un .py en {REPO_ROOT / carpeta}: no está "
            f"vigilando esa carpeta, y un parametrize sin casos pasa sin mirar nada"
        )
        out.extend(encontrados)
    ficheros = [
        p
        for p in out
        if "__pycache__" not in p.parts and p.resolve() not in EXCEPTO
    ]
    assert ficheros, "no queda ni un fichero que vigilar después de las exclusiones"
    return ficheros


def _ids_de_docstrings(arbol: ast.AST) -> set[int]:
    """Los nodos de cadena que son docstrings, para poder ignorarlos.

    Se identifican por posición (primera sentencia del cuerpo de un módulo, una
    clase o una función) y no por contenido, que es como los identifica Python.
    Los comentarios no hacen falta: el AST no los conserva, así que ya están
    fuera sin hacer nada.
    """
    fuera: set[int] = set()
    for nodo in ast.walk(arbol):
        if not isinstance(
            nodo, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        cuerpo = getattr(nodo, "body", None)
        if not cuerpo:
            continue
        primera = cuerpo[0]
        if isinstance(primera, ast.Expr) and isinstance(primera.value, ast.Constant):
            if isinstance(primera.value.value, str):
                fuera.add(id(primera.value))
    return fuera


def _cadenas_de_codigo(arbol: ast.AST) -> list[str]:
    """Toda cadena literal del módulo menos los docstrings."""
    fuera = _ids_de_docstrings(arbol)
    return [
        n.value
        for n in ast.walk(arbol)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in fuera
    ]


@pytest.mark.parametrize("ruta", _ficheros(), ids=lambda p: p.name)
def test_ningun_guion_restaura_con_git(ruta: Path) -> None:
    """Que no se cuele un git que descarte cambios, ni en `scripts/` ni al lado.

    La comprobación es por bolsa de literales del módulo entero y no por llamada
    a `subprocess`, porque la forma natural de escribirlo es una lista -`["git",
    "checkout", "--", fichero]`- donde cada palabra es un literal suelto y
    ninguno de ellos, por separado, parece nada.
    """
    arbol = ast.parse(_fuente(ruta), filename=str(ruta))
    cadenas = _cadenas_de_codigo(arbol)
    texto = " ".join(cadenas).lower()

    if "git" not in texto:
        return

    culpables = [v for v in VERBOS_QUE_DESTRUYEN if v in texto]
    assert not culpables, (
        f"{ruta.relative_to(REPO_ROOT)} ejecuta git con {culpables} en un literal "
        f"de codigo.\n"
        f"Los literales del modulo son: {sorted(set(cadenas))}\n\n"
        f"`git checkout` no deshace una mutacion: devuelve el fichero a HEAD y se "
        f"lleva por delante todo lo que no estuviera commiteado. Eso ya paso una "
        f"vez aqui y costo recuperar la implementacion desde el transcript de la "
        f"sesion.\n"
        f"Si de verdad hace falta deshacer algo: `shutil.copy2` antes, y volver a "
        f"copiar encima en un `finally`. Ver `scripts/mutar_bici.py`.\n"
        f"Si solo querias NOMBRAR la regla, ponla en un comentario o en el "
        f"docstring: ninguno de los dos llega hasta aqui."
    )


def _mutadores() -> list[Path]:
    return sorted(GUIONES_DIR.glob("mutar_*.py"))


def test_hay_mutadores_que_comprobar() -> None:
    """La guarda de arriba, sin guiones que vigilar, pasaría sin mirar nada.

    Un `parametrize` sobre una lista vacía no falla: no corre. O sea que si
    alguien renombra los guiones de mutación a otra cosa, las dos pruebas de
    abajo dejarían de existir en silencio y el informe seguiría diciendo OK. Es
    literalmente el fallo que se acaba de encontrar en `falsear_bici.py` -una
    batería que aprueba sin medir- y no se va a repetir tres metros más abajo.
    """
    assert len(_mutadores()) >= 3, (
        f"esperaba al menos tres guiones `scripts/mutar_*.py` y encontre "
        f"{[p.name for p in _mutadores()]}. Si se han renombrado, cambia el patron "
        f"aqui: si no, las dos comprobaciones de abajo se quedan sin sujeto y "
        f"pasan sin mirar nada."
    )


def _tries_que_restauran(arbol: ast.AST) -> list[ast.Try]:
    """Los `try` cuyo `finally` llama a `shutil.copy2`."""
    out = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Try) or not nodo.finalbody:
            continue
        for stmt in nodo.finalbody:
            for hijo in ast.walk(stmt):
                if (
                    isinstance(hijo, ast.Call)
                    and isinstance(hijo.func, ast.Attribute)
                    and hijo.func.attr == "copy2"
                ):
                    out.append(nodo)
                    break
            else:
                continue
            break
    return out


ESCRITURAS = ("write", "writelines", "write_text", "write_bytes")


@pytest.mark.parametrize("ruta", _mutadores(), ids=lambda p: p.name)
def test_los_mutadores_restauran_desde_copia(ruta: Path) -> None:
    """Copia antes, restauración en un `finally`, y la escritura dentro del `try`.

    Lo tercero es lo que cuesta ver y lo que de verdad importa. Un guión puede
    tener un `finally` impecable y aun así dejar el árbol sucio si la línea que
    escribe el fichero mutado está fuera del `try`: entre la escritura y el
    `try` cabe una excepción, un `Ctrl-C` o un `return` temprano, y el fichero se
    queda mutado en el disco. Nadie se entera hasta la siguiente vez que se corre
    la suite, que entonces mide otro código del que dice medir.
    """
    arbol = ast.parse(_fuente(ruta), filename=str(ruta))
    rel = ruta.relative_to(REPO_ROOT)

    copias = [
        n
        for n in ast.walk(arbol)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "copy2"
    ]
    assert len(copias) >= 2, (
        f"{rel} usa `shutil.copy2` {len(copias)} vez/veces y hacen falta dos: una "
        f"para guardar el original antes de mutar y otra para devolverlo despues. "
        f"Con una sola, o no hay respaldo o no hay restauracion."
    )

    restauradores = _tries_que_restauran(arbol)
    assert restauradores, (
        f"{rel} no tiene ningun `try` cuyo `finally` llame a `shutil.copy2`.\n"
        f"Sin `finally`, cualquier excepcion a mitad de la bateria -o un Ctrl-C, "
        f"que es lo normal cuando un guion de mutacion tarda- deja el codigo "
        f"mutado en el disco. Ver `scripts/mutar_bici.py`."
    )

    protegidos: set[int] = set()
    for t in restauradores:
        for stmt in t.body:
            protegidos.update(id(n) for n in ast.walk(stmt))

    sueltas = [
        n
        for n in ast.walk(arbol)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in ESCRITURAS
        and id(n) not in protegidos
    ]
    assert not sueltas, (
        f"{rel} escribe fuera del `try` que restaura, en la/s linea/s "
        f"{sorted(n.lineno for n in sueltas)}.\n"
        f"El `finally` solo cubre lo que pasa DENTRO de su `try`. Una escritura "
        f"justo antes deja una ventana en la que el fichero ya esta mutado y "
        f"nadie se ha comprometido todavia a devolverlo. Mueve la escritura a la "
        f"primera linea del `try`."
    )


@pytest.mark.parametrize("ruta", _mutadores(), ids=lambda p: p.name)
def test_los_mutadores_dicen_la_regla(ruta: Path) -> None:
    """El docstring tiene que nombrar la regla y nombrar lo que NO se hace.

    Obligar a que una frase esté escrita no garantiza que se entienda; garantiza
    que se lea. El guión nuevo se escribe copiando uno viejo, y si el viejo trae
    la explicación de por qué el `finally` está ahí, el nuevo la trae también.
    Sin esto, el patrón se copia como superstición y el primero que lo encuentre
    incómodo lo simplifica.
    """
    arbol = ast.parse(_fuente(ruta), filename=str(ruta))
    doc = (ast.get_docstring(arbol) or "").lower()
    rel = ruta.relative_to(REPO_ROOT)

    faltan = [
        frase
        for frase in ("copy2", "finally", "git checkout")
        if frase not in doc
    ]
    assert not faltan, (
        f"el docstring de {rel} no menciona {faltan}.\n"
        f"Tiene que decir las tres cosas: que se copia con `shutil.copy2`, que se "
        f"restaura en un `finally`, y que NUNCA se usa `git checkout` porque se "
        f"lleva por delante el trabajo sin commitear. Copia la frase de "
        f"`scripts/mutar_bici.py`."
    )
