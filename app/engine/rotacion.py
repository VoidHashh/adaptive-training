"""Qué rutina del ciclo lleva demasiado sin hacerse.

POR QUÉ ESTO NO TOCA LA ROTACIÓN
--------------------------------
La rotación no se reordena. El ciclo sigue siendo 1 -> 2 -> 3 -> 1 y lo que
propone cada mañana lo sigue decidiendo `siguiente_en_rotacion` a partir de la
última sesión EJECUTADA. Este módulo no elige nada: solo mira hacia atrás y
cuenta.

El motivo de que baste con contar es que el ciclo ya se cierra solo. Si te toca
el Día 1, haces el Día 2 y sigues, lo siguiente es el Día 3 y después el Día 1:
la que te saltaste vuelve al final de la vuelta sin que nadie la tenga que
guardar en ninguna parte. El desvío puntual no pierde nada, y por eso NO hace
falta ni un puntero, ni una cola de pendientes, ni una tabla.

Lo que el ciclo no puede ver por sí solo es el otro caso: elegir el Día 2 cinco
veces seguidas. Ahí el Día 1 y el Día 3 no se pierden por un desvío, se dejan de
hacer, y la rotación no se entera porque solo mira la última ejecución. ESO es
lo que cuenta este módulo, y es la única razón por la que existe.

LA UNIDAD ES LA SESIÓN, NO EL DÍA
---------------------------------
Todo lo de aquí se mide en sesiones de fuerza ejecutadas, nunca en días de
calendario. Con dos entrenos por semana, catorce días son dos sesiones; con
cuatro, son ocho. Un umbral en días diría cosas distintas según la semana que
estuvieras teniendo, y diría la más alarmante justo en las semanas flojas, que
son las que menos falta hace alarmar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Sequence

# Cuántas sesiones de fuerza más se sigue nombrando una rutina pendiente antes
# de dejar de nombrarla.
#
# No es un plazo para hacerla: no hay ninguna consecuencia al agotarse. Lo único
# que cambia es que el mensaje deja de repetir la línea. Una línea que sale
# todas las mañanas durante meses no informa de nada -se lee una vez y a partir
# de ahí es parte del decorado- y además acaba sonando a reproche por pura
# insistencia, que es justo lo que no se quiere.
#
# Y se cuenta DESPUÉS de que ya haya pasado una vuelta entera, o sea que una
# rutina se nombra durante una ventana de tres sesiones y después se calla.
CADUCA_TRAS = 3


@dataclass(frozen=True)
class Pendiente:
    """Una rutina del ciclo que lleva más de una vuelta sin ejecutarse."""

    clave: str
    # Sesiones de fuerza del ciclo ejecutadas desde la última vez que se hizo
    # ésta. Siempre hay número, y siempre es mayor que la vuelta entera: una
    # rutina que no se ha hecho NUNCA no llega a ser Pendiente -no hay desde
    # cuándo contar-, así que aquí no existe el "no lo sé".
    sesiones_desde: int
    # El día en que se hizo por última vez. Existe por lo mismo de arriba.
    ultima_vez: date
    # Ya no se nombra en el mensaje. Sigue en la lista porque el dato vale para
    # el análisis aunque haya dejado de valer para el aviso de la mañana.
    caducada: bool


def pendientes(
    orden: Sequence[str],
    ejecutadas: Sequence[tuple[str, date]],
    *,
    caduca_tras: int = CADUCA_TRAS,
) -> list[Pendiente]:
    """Las rutinas del ciclo que llevan más de una vuelta sin hacerse.

    `ejecutadas` son las sesiones de fuerza del ciclo leídas de `workout_log`,
    MÁS RECIENTE PRIMERO, ya filtradas a las que pertenecen a `orden`. Se pasan
    hechas y no se consultan aquí porque este módulo no abre la base de datos:
    es el mismo reparto que `tendencia` y `recalibracion`, que se calculan en
    `run_daily` y viajan colgadas de la decisión.

    EL UMBRAL ES `> len(orden)` Y NO `>= len(orden)`, y esa desigualdad es el
    encargo entero. Lo que separa una de otra es UN caso, y resulta ser el que
    lo motivó todo: el desvío puntual, la mañana siguiente.

    En el ciclo corriente -1, 2, 3, 1, 2, 3- la cuenta más alta que se alcanza
    es `len(orden) - 1`: acabas de hacer el Día 3, el Día 2 fue hace una sesión
    y el Día 1 hace dos. Nunca llega a tres. O sea que el ciclo normal no lo
    produce ni con `>` ni con `>=`, y no es ahí donde está la diferencia.

    Donde está es aquí: tocaba el Día 1, se hizo el Día 2 y se acabó la mañana.
    El Día 1 queda a `len(orden)` sesiones justas -tres-, una vuelta EXACTA. Con
    `>=` saldría marcado a la mañana siguiente, que es decirle "llevas sin hacer
    el Día 1" a alguien que se lo saltó ayer una vez y ya lo sabe. Con `>` hace
    falta que pase una vuelta entera Y UNA SESIÓN MÁS, o sea que el ciclo haya
    tenido ocasión de devolvérselo y no se la haya tomado.

    Consecuencia buscada: saltarse el Día 1 una vez y seguir no marca nada.
    Hacer el Día 2 cinco veces seguidas sí marca, que es el caso que la rotación
    no puede ver sola.

    Una rutina que no se ha hecho NUNCA no es pendiente, y ni siquiera sale con
    la cuenta a cero: sale fuera de la lista. El día que se añade una rutina al
    `config.yaml` no es el día de avisar de que llevas sin hacerla, y no hay
    número que ponerle -un cero diría "recién hecha" y un infinito diría
    "abandonada", y las dos son afirmaciones que nadie ha comprobado-.
    """
    if not orden:
        return []

    # Cuántas sesiones del ciclo han pasado desde la última aparición de cada
    # rutina. Se recorre de más reciente a más antigua y se anota la posición de
    # la PRIMERA aparición de cada clave, que es la última vez que se hizo.
    #
    # La posición vale como cuenta directamente: si la rutina está en el índice
    # 4 de la lista, es que después de ella se han hecho 4 sesiones.
    visto: dict[str, tuple[int, date]] = {}
    for i, (clave, dia) in enumerate(ejecutadas):
        if clave not in visto:
            visto[clave] = (i, dia)

    fuera = []
    for clave in orden:
        if clave not in visto:
            # Nunca ejecutada. Ni pendiente ni nada: no hay desde cuándo contar.
            continue
        desde, ultima = visto[clave]
        if desde <= len(orden):
            continue
        fuera.append(
            Pendiente(
                clave=clave,
                sesiones_desde=desde,
                ultima_vez=ultima,
                caducada=desde > len(orden) + caduca_tras,
            )
        )

    # Por lo que más lleva parado primero. El orden importa porque el mensaje
    # nombra como mucho una, y tiene que ser la que más falta hace decir.
    fuera.sort(key=lambda p: (-p.sesiones_desde, p.clave))
    return fuera


def to_dict(lista: Sequence[Pendiente]) -> list[dict]:
    """Para el JSON de la decisión. Las cuatro claves, siempre las cuatro."""
    return [
        {
            "clave": p.clave,
            "sesiones_desde": p.sesiones_desde,
            "ultima_vez": p.ultima_vez.isoformat(),
            "caducada": p.caducada,
        }
        for p in lista
    ]
