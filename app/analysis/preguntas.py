"""Las cuatro casillas de «¿te apetece?» contra «¿vas a entrenar?».

POR QUÉ UN MÓDULO PARA UNA TABLA DE CUATRO NÚMEROS
---------------------------------------------------
Porque el binario `discordancia` no se puede enseñar solo, y eso no es una
preferencia de presentación: es que el binario, solo, dice algo falso.

`discordancia` vale 1 cuando lo que te apetecía y lo que hiciste no coincidieron.
Eso junta dos días que no se parecen en nada:

    te apetecía y NO fuiste       -> algo te lo impidió
    NO te apetecía y SÍ fuiste    -> te lo saltaste, para bien o para mal

Un 0,30 de media significa «tres de cada diez días no coincidieron» y no dice
cuál de las dos cosas pasó. Los dos repartos extremos -las treinta de un tipo o
las treinta del otro- dan exactamente el mismo número, y describen a dos personas
distintas. Por eso `series.py` declara la serie `neutro` y por eso esta tabla
viaja SIEMPRE con ella: el binario es lo que se puede correlacionar, y estas
cuatro casillas son lo único que dice en qué dirección pasó.

EL CERO QUE NO ES UN CERO
-------------------------
Una casilla vacía se pinta como «0 días», con el total al lado, y nunca como un
porcentaje a secas. «0 %» sobre cinco días contestados se lee igual que «0 %»
sobre ciento ochenta, y son dos frases completamente distintas: la primera es
«todavía no ha pasado», la segunda es «no te pasa». Es el mismo cuidado que
`stats.redondear_p` tiene con las p, aplicado a un recuento en vez de a una
probabilidad, y por el mismo motivo: un cero impreso sin su denominador se lee
como imposibilidad.

De ahí que `n` sea obligatorio en la ficha y que por debajo de `N_MINIMO_FIABLE`
se marque -no se esconda- con `aviso`. Esconderla sería decidir por él que no
quiere ver sus primeras semanas, que es justo lo contrario de lo que pidió.

LO QUE ESTA TABLA NO HACE
-------------------------
No juzga. No hay una casilla "buena" ni una "mala": ir sin ganas no es virtud ni
es terquedad hasta que se mire qué pasó después, y eso lo contesta la Vista 3 -el
impacto- con datos, no esta tabla con adjetivos. Las etiquetas describen lo que
pasó y se acaban ahí.

Y no toca el semáforo. Como el resto de lo que sale de estas dos preguntas.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.series import a_fecha
from app.analysis.stats import N_MINIMO_CALCULABLE, N_MINIMO_FIABLE
from app.analysis.texto import cuantos
from app.models import Checkin

# Las cuatro, en el orden en que se leen: primero lo que coincide, después lo que
# no. El orden es el de la pantalla y por eso se escribe una sola vez aquí, y no
# se deja al `dict` ni al navegador. Una tabla de dos por dos ordenada de otra
# manera cada vez que se abre no se puede comparar consigo misma de un día
# para otro.
CASILLAS: tuple[tuple[bool, bool, str], ...] = (
    (True, True, "Te apetecía y entrenaste"),
    (False, False, "No te apetecía y no entrenaste"),
    (True, False, "Te apetecía y no entrenaste"),
    (False, True, "No te apetecía y entrenaste"),
)


def tabla_discordancia(
    session: Session, desde: date, hasta: date
) -> dict[str, Any]:
    """Las cuatro casillas contadas, con su total y su aviso de muestra corta.

    Solo cuentan los días con las DOS contestadas. Un día en el que se dijo si
    apetecía y no se dijo si se iba a entrenar no cabe en ninguna casilla, y
    meterlo en la que "parece" -la de no entrenar, que es lo que suele pasar
    cuando no se contesta- sería inventarse la mitad del dato. Esos días se
    cuentan aparte, en `sin_las_dos`, porque su número también dice algo: si son
    muchos, la tabla entera describe los días que se contestaron enteros y no los
    días.
    """
    filas = session.execute(
        select(Checkin.date, Checkin.wants_to_train, Checkin.will_train).where(
            Checkin.date >= desde, Checkin.date <= hasta
        )
    ).all()

    cuenta: dict[tuple[bool, bool], int] = {}
    sin_las_dos = 0
    for _f, apetece, voy in filas:
        if apetece is None or voy is None:
            sin_las_dos += 1
            continue
        llave = (bool(apetece), bool(voy))
        cuenta[llave] = cuenta.get(llave, 0) + 1

    n = sum(cuenta.values())
    celdas = [
        {
            "apetece": apetece,
            "voy": voy,
            "etiqueta": etiqueta,
            "discordante": apetece != voy,
            "n": cuenta.get((apetece, voy), 0),
            # El porcentaje viaja, pero NUNCA solo: va al lado del recuento y del
            # total, que es lo que lo hace legible. Con `n = 0` sería una
            # división por cero, así que en ese caso no hay porcentaje que dar y
            # se dice con un `None`, no con un 0,0 que parecería una medición.
            "pct": (
                None if n == 0 else round(cuenta.get((apetece, voy), 0) * 100.0 / n, 1)
            ),
        }
        for apetece, voy, etiqueta in CASILLAS
    ]

    discordantes = sum(c["n"] for c in celdas if c["discordante"])
    return {
        "titulo": "Lo que te apetecía y lo que hiciste",
        "n": n,
        "sin_las_dos": sin_las_dos,
        "celdas": celdas,
        "discordantes": discordantes,
        "pct_discordantes": None if n == 0 else round(discordantes * 100.0 / n, 1),
        "desde": desde.isoformat(),
        "hasta": hasta.isoformat(),
        "na": _na(n),
        "aviso": _aviso(n),
        "ficha": _ficha(n, sin_las_dos),
        "lectura": _lectura(celdas, n),
    }


def _na(n: int) -> str | None:
    """Por qué no hay tabla, cuando no la hay. `None` cuando sí la hay."""
    if n >= N_MINIMO_CALCULABLE:
        return None
    if n == 0:
        return (
            "todavía no hay ningún día con las dos preguntas contestadas; la "
            "tabla aparece sola en cuanto lo haya"
        )
    return (
        f"solo {cuantos(n, 'día', 'días')} con las dos contestadas; con menos de "
        f"{N_MINIMO_CALCULABLE} esto no es un reparto, son {cuantos(n, 'día', 'días')}"
    )


def _aviso(n: int) -> str | None:
    """La muestra corta se MARCA, no se esconde.

    Entre tres días y veinte la tabla se pinta entera y con esta frase encima.
    Es la misma regla que `stats`: quien pidió ver sus números desde el primer
    día los ve desde el primer día, sabiendo lo que valen.
    """
    if n == 0 or n >= N_MINIMO_FIABLE:
        return None
    return (
        f"son {cuantos(n, 'día', 'días')} en total: el reparto todavía se mueve "
        f"mucho con cada mañana nueva"
    )


def _ficha(n: int, sin_las_dos: int) -> str:
    """La línea atenuada de debajo. Siempre lleva el denominador.

    Es lo que convierte un «0» en información. Un cero sin total al lado dice
    «no te pasa» tenga detrás cinco días o ciento ochenta, y esa es la lectura
    que no se puede permitir en la pantalla de nadie.
    """
    partes = [f"{cuantos(n, 'día', 'días')} con las dos contestadas"]
    if sin_las_dos:
        partes.append(
            f"{cuantos(sin_las_dos, 'día', 'días')} con solo una y fuera de la cuenta"
        )
    return " · ".join(partes)


def _lectura(celdas: list[dict[str, Any]], n: int) -> str | None:
    """Cuál de las dos discordancias manda, en una frase, o `None` si ninguna.

    Esto es lo único que el binario no podía decir, así que es lo que hay que
    decir. Y se dice sin adjetivos: se cuenta cuál de las dos pasa más y se
    calla lo que eso significa, porque lo que significa depende de qué pasó
    después y eso lo contesta otra vista.
    """
    if n < N_MINIMO_CALCULABLE:
        return None
    impedido = next(c for c in celdas if c["apetece"] and not c["voy"])
    empujado = next(c for c in celdas if not c["apetece"] and c["voy"])
    total = impedido["n"] + empujado["n"]
    if total == 0:
        return "no ha habido ni un día en que una cosa y la otra no coincidieran"
    if impedido["n"] == empujado["n"]:
        return (
            f"las dos discordancias van igualadas: {impedido['n']} de cada una "
            f"de {cuantos(n, 'día', 'días')}"
        )
    if impedido["n"] > empujado["n"]:
        return (
            f"cuando no coinciden es casi siempre porque te apetecía y no "
            f"entrenaste ({impedido['n']} de {total})"
        )
    return (
        f"cuando no coinciden es casi siempre porque entrenaste sin que te "
        f"apeteciera ({empujado['n']} de {total})"
    )
