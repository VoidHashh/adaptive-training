"""Rellenar los días de bienestar que el sistema se perdió.

POR QUÉ EXISTE
--------------
`daily_metrics` solo se escribía dentro de `run_daily`. Un día en que el proceso
no corre -el PC apagado durante las pruebas, el contenedor parado, un despliegue
a media mañana- es un día que no tiene fila y no la va a tener nunca. Y el
agujero no se ve: en la base de datos un día ausente y un día sin reloj son
exactamente lo mismo, o sea nada.

Eso vale para el análisis lo que valga la serie, y una serie con huecos que no
se sabe de qué son vale poco. Las correlaciones de las vistas 1 y 2 emparejan el
formulario de una mañana con el Garmin de esa mañana: cada día sin fila es un
par menos, y no un par menos elegido al azar sino justo los días raros, que son
los que más informan.

HASTA DÓNDE SE PUEDE RELLENAR (medido, no supuesto)
---------------------------------------------------
En el código ponía, en dos sitios, que "Garmin no sirve histórico antiguo de
sueño ni de body battery". Nadie lo había comprobado. Sondeando días sueltos de
antigüedad creciente (`scripts/sondeo_wellness.py`, 2026-09-11):

    HRV                  -> hasta -175 días
    FC en reposo         -> hasta -175 días
    minutos de sueño     -> hasta -175 días
    nota de sueño        -> hasta -175 días
    body battery         -> hasta -120 días, más atrás ya no
    training readiness   -> nunca, ni ayer (ver `integrations/garmin.py`)

Así que el histórico SÍ se puede reconstruir, y con las cuatro métricas que
alimentan las parejas de la vista 1. Lo que ya no sirve -body battery de hace
cinco meses- queda como hueco explícito en la fila, nunca como cero.

LOS DOS USOS, UN SOLO MOTOR
---------------------------
    recuperar_al_arrancar()   Los últimos `recovery_days`. Barato, automático,
                              y pensado para el PC que no está siempre encendido.
    rellenar()                Lo que pida quien lo llame. Es lo que usa
                              `scripts/backfill_wellness.py` para los 180 días.

Son la misma función con otra ventana a propósito. Dos implementaciones del
mismo relleno acabarían divergiendo justo en el detalle que importa -qué se
considera pendiente-, y la que se usa sin mirar es la automática.

QUÉ CUENTA COMO PENDIENTE
-------------------------
Un día está pendiente si NO tiene fila, o si la que tiene dice `error`. Los tres
estados de `fetch_status` significan cosas distintas y solo uno se reintenta:

    ok       está todo. No se vuelve.
    partial  se preguntó, Garmin contestó, y de esa métrica no había dato.
             Tampoco se vuelve: mañana va a contestar lo mismo, y volver a
             preguntarlo son peticiones tiradas contra un límite que muerde.
    error    no se pudo leer -un 500, un timeout, un 429-. Este sí, y por eso se
             separó de `partial`: sin la distinción, un fallo de red quedaba
             archivado para siempre como "esa noche no dormí con el reloj".

De ahí sale gratis lo que en un trabajo de veinticinco minutos hace falta de
verdad: que sea reanudable. Se guarda día a día, así que un 429 a mitad deja
escrito todo lo anterior y la siguiente ejecución sigue por donde se quedó sin
que haya que llevar la cuenta en ninguna parte.

EL FRENO
--------
Cuatro peticiones por día pedido. Ciento ochenta días son setecientas veinte, y
Garmin limita por IP durante MINUTOS cuando se le pide demasiado seguido -ya ha
devuelto 429 durante el sondeo-. La pausa entre días (`wellness.backfill.
pause_seconds`) es lo que compra que el trabajo termine: a 2 s son unos
veinticinco minutos, y veinticinco minutos que acaban valen más que cinco que se
paran a la mitad.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DailyMetrics

log = logging.getLogger(__name__)

# Las métricas que se piden, con el nombre que se lee. `readiness` no está: va
# apagada porque para esta cuenta vuelve siempre vacía.
METRICAS = ("hrv", "rhr", "sleep_min", "sleep_score", "body_battery")

# Peticiones a Garmin por cada día que se pide. Solo sirve para poder decir de
# antemano cuánto va a costar, que es lo que decide si se lanza o no.
PETICIONES_POR_DIA = 4


@dataclass
class ResultadoBackfill:
    """Lo que ha pasado, con el detalle suficiente para decidir si repetir."""

    pedidos: list[date] = field(default_factory=list)
    escritos: list[date] = field(default_factory=list)
    # Días que no se pidieron porque ya tenían fila buena.
    ya_estaban: int = 0
    # métrica -> días en los que Garmin contestó pero no había dato. Es el mapa
    # de hasta dónde llega cada una, y se imprime porque es la respuesta a "¿me
    # merece la pena pedir más atrás?".
    huecos: dict[str, list[date]] = field(default_factory=dict)
    # Días cuya lectura falló. Se quedan pendientes y se reintentan solos.
    fallidos: list[date] = field(default_factory=list)
    # Texto de cada fallo, para que el informe diga qué pasó y no solo cuántos.
    errores: list[str] = field(default_factory=list)
    # Se paró antes de tiempo (un 429 que sobrevivió a los reintentos).
    interrumpido: str | None = None

    @property
    def peticiones(self) -> int:
        return len(self.pedidos) * PETICIONES_POR_DIA

    def resumen(self) -> str:
        trozos = [
            f"{len(self.escritos)} día(s) escritos de {len(self.pedidos)} pedidos",
            f"{self.ya_estaban} ya estaban",
        ]
        if self.fallidos:
            trozos.append(f"{len(self.fallidos)} fallaron (se reintentan solos)")
        if self.interrumpido:
            trozos.append(f"INTERRUMPIDO: {self.interrumpido}")
        return ", ".join(trozos)


def dias_pendientes(session: Session, desde: date, hasta: date) -> list[date]:
    """Días del rango [desde, hasta] que hay que pedirle a Garmin.

    Pendiente = sin fila, o con fila marcada `error`. Ver el docstring del
    módulo para por qué `partial` NO se reintenta: es la diferencia entre "no
    había dato" y "no se pudo leer", y confundirlas significa o perder el día
    para siempre o pedirlo todos los arranques hasta el fin de los tiempos.
    """
    if hasta < desde:
        return []
    filas = {
        f.date: f.fetch_status
        for f in session.scalars(
            select(DailyMetrics).where(
                DailyMetrics.date >= desde, DailyMetrics.date <= hasta
            )
        )
    }
    total = (hasta - desde).days + 1
    return [
        d
        for d in (desde + timedelta(days=i) for i in range(total))
        if filas.get(d, "error") == "error"
    ]


def rellenar(
    session: Session,
    cliente: Any,
    dias: list[date],
    *,
    pausa: float = 2.0,
    progreso: Callable[[date, Any, list[str]], None] | None = None,
    dormir: Callable[[float], None] = time.sleep,
) -> ResultadoBackfill:
    """Pide esos días a Garmin y los escribe. Uno a uno, y con commit por día.

    El commit por día no es prudencia de más: son cuatro peticiones por día y
    Garmin corta por IP durante minutos. Con una sola transacción al final, un
    429 en el día 150 tira a la basura los 149 anteriores y la siguiente
    ejecución vuelve a pedirlos, que es la forma más rápida de que el corte se
    vuelva permanente.

    `dormir` se inyecta para que los tests no tarden lo que tarda el freno.
    """
    from app.repository import upsert_daily_metrics

    res = ResultadoBackfill(pedidos=list(dias))
    if not dias:
        return res

    for i, dia in enumerate(dias):
        antes = len(getattr(cliente, "fetch_errors", []) or [])
        try:
            m = cliente.day_metrics(dia)
        except Exception as exc:  # noqa: BLE001
            # No se escribe fila. Escribir una vacía diría "ese día no hubo
            # nada" cuando lo que pasó es que no se pudo preguntar, y como las
            # filas sin `error` no se reintentan, el día quedaría perdido.
            res.fallidos.append(dia)
            res.errores.append(f"{dia}: {exc}")
            log.warning("backfill: %s no se pudo pedir: %s", dia, exc)
            # Un corte por límite no se arregla insistiendo con el día
            # siguiente: se para y se deja dicho, y lo escrito hasta aquí queda.
            if type(exc).__name__ == "GarminRateLimited":
                res.interrumpido = (
                    f"Garmin ha cortado por límite de peticiones en {dia}. "
                    f"Lo anterior está guardado; vuelve a lanzarlo dentro de un "
                    f"rato y seguirá por donde se quedó."
                )
                return res
            if i < len(dias) - 1 and pausa:
                dormir(pausa)
            continue

        nuevos = list((getattr(cliente, "fetch_errors", []) or [])[antes:])
        upsert_daily_metrics(
            session, [m], errores={dia: nuevos} if nuevos else None, recuperado=True
        )
        session.commit()

        if nuevos:
            res.fallidos.append(dia)
            res.errores.extend(nuevos)
        else:
            res.escritos.append(dia)

        no_pedidas = set(getattr(m, "not_requested", ()) or ())
        for nombre in METRICAS:
            if nombre in no_pedidas:
                continue
            if getattr(m, nombre, None) is None:
                res.huecos.setdefault(nombre, []).append(dia)

        if progreso is not None:
            progreso(dia, m, nuevos)
        if i < len(dias) - 1 and pausa:
            dormir(pausa)

    return res


def ventana_de_recuperacion(cfg: Any, hoy: date | None = None) -> tuple[date, date] | None:
    """El rango que repasa el arranque, o `None` si la recuperación está apagada.

    Está aparte de `recuperar_al_arrancar` para que quien tenga que decidir SI
    merece la pena conectarse a Garmin pueda calcular la ventana sin conectarse.
    El trabajo del scheduler mira primero si hay pendientes y solo entonces hace
    login; si la ventana se calculara en dos sitios, el día que se toque uno el
    otro seguiría mirando días distintos de los que luego se piden.

    HOY NO SE PIDE, Y AYER TAMPOCO
    ------------------------------
    La ventana acaba anteayer. Los datos de anoche llegan al servidor de Garmin
    cuando el reloj sincroniza, que puede ser a media mañana; pedir hoy o ayer
    desde el arranque devolvería medias tintas, y como una fila `partial` NO se
    reintenta, esas medias tintas se quedarían archivadas como definitivas. De
    los dos últimos días ya se encarga la ventana diaria de `run_daily`, que
    relee siete y sí puede corregirse a sí misma.
    """
    from app.integrations.garmin import wellness_config

    dias_atras = int(
        ((wellness_config(cfg).get("backfill") or {}).get("recovery_days")) or 0
    )
    if dias_atras < 1:
        return None
    hoy = hoy or date.today()
    return hoy - timedelta(days=dias_atras), hoy - timedelta(days=2)


def recuperar_al_arrancar(
    session: Session,
    cliente: Any,
    cfg: Any,
    *,
    hoy: date | None = None,
    dormir: Callable[[float], None] = time.sleep,
) -> ResultadoBackfill:
    """El repaso de cada arranque: los últimos `wellness.backfill.recovery_days`.

    La ventana la fija `ventana_de_recuperacion`; ahí está explicado por qué
    acaba anteayer y no ayer.
    """
    from app.integrations.garmin import wellness_config

    ventana = ventana_de_recuperacion(cfg, hoy)
    if ventana is None:
        return ResultadoBackfill()
    desde, hasta = ventana

    bf = (wellness_config(cfg).get("backfill") or {})
    pendientes = dias_pendientes(session, desde, hasta)
    if not pendientes:
        log.info("backfill de arranque: nada que recuperar entre %s y %s", desde, hasta)
        return ResultadoBackfill()

    log.warning(
        "backfill de arranque: faltan %d día(s) entre %s y %s; los pido a Garmin "
        "(%d peticiones)",
        len(pendientes), desde, hasta, len(pendientes) * PETICIONES_POR_DIA,
    )
    res = rellenar(
        session, cliente, pendientes,
        pausa=float(bf.get("pause_seconds") or 2.0), dormir=dormir,
    )
    log.warning("backfill de arranque: %s", res.resumen())
    return res
