"""Los trabajos que se ejecutan solos, y lo que pasa cuando no salen bien.

El sistema entero existe para funcionar sin que nadie lo mire. Eso convierte el
comportamiento ante el fallo en la parte importante de este módulo: un trabajo
que revienta a las siete de la mañana y solo deja una línea en un log es, desde
el sofá, indistinguible de un día en el que no tocaba entrenar.

LOS DEFECTOS DE APSCHEDULER NO SIRVEN AQUÍ
------------------------------------------
Tres, y los tres fallan callando:

- `misfire_grace_time` vale 1 segundo. Si el contenedor arranca a las 06:31, el
  trabajo de las 06:30 se descarta sin ejecutarse y sin avisar. En un Umbrel que
  se reinicia por una actualización eso es justo el caso normal, no el raro. Se
  sube a horas.
- `coalesce` está bien en `True` (que es el defecto) pero se pone explícito: tras
  una parada larga interesa decidir UNA vez, no catorce veces seguidas.
- una excepción dentro de un trabajo se queda en el log del scheduler. Aquí se
  engancha un escuchador que la manda por Telegram: el usuario tiene que
  enterarse de que hoy no hay decisión, y tiene que enterarse por el mismo sitio
  por el que le llega todo lo demás.

POR QUÉ LA DECISIÓN DE LA MAÑANA ES UN *FALLBACK*
-------------------------------------------------
El camino normal es que el check-in de la PWA dispare la decisión en cuanto se
envía. El trabajo de las 09:00 solo actúa si a esa hora no hay check-in: decide
con lo que haya de Garmin y lo dice. Por eso su `source` es `fallback_0900` y no
`checkin`; que la diferencia quede registrada importa el día que haya que
explicar por qué el semáforo de un martes salió sin la mitad de las señales.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import repository as repo
from app.db import session_scope
from app.runner import run_daily, run_reconcile

log = logging.getLogger(__name__)

# Una hora de margen. El número no es mágico: es "lo que tarda un Umbrel en
# reiniciarse y volver". Con el defecto de 1 segundo, cualquier arranque que no
# caiga en el segundo exacto se salta el trabajo del día.
MARGEN_S = 3600

RIDE_HISTORY_DAYS = 190


def _hora(cfg: Any, clave: str, defecto: str) -> tuple[int, int]:
    sched = (cfg.raw.get("schedule") or {})
    texto = str(sched.get(clave) or defecto)
    h, m = texto.split(":")[:2]
    return int(h), int(m)


# ---------------------------------------------------------------------------
# Los trabajos
# ---------------------------------------------------------------------------
#
# Cada uno recibe lo que necesita en vez de buscarlo por su cuenta: así se
# pueden ejecutar en un test con una base de datos en memoria y sin red, que es
# la única forma de comprobar que hacen lo que dicen.


def job_decision(
    cfg: Any,
    *,
    day: date | None = None,
    source: str = "fallback_0900",
    fetch: Callable | None = None,
    hevy_client: Any = None,
    telegram_client: Any = None,
    dry_run: bool = False,
    solo_si_falta_checkin: bool = True,
) -> Any:
    """Decide el día. Por defecto solo si el check-in no ha llegado.

    `solo_si_falta_checkin` evita el atropello: si a las 09:00 el usuario ya
    rellenó el formulario a las 07:40, la decisión buena ya está tomada y
    volver a tomarla la sustituiría por otra igual pero marcada como
    `fallback_0900`. El registro diría que ese día se decidió sin check-in
    teniéndolo, que es exactamente lo contrario de lo que pasó.
    """
    day = day or date.today()
    with session_scope() as s:
        if solo_si_falta_checkin and repo.get_checkin(s, day) is not None:
            log.info("decisión de %s: ya hay check-in, el fallback no actúa", day)
            return None

        metrics, rides = (fetch or _fetch_garmin)(cfg, day)
        return run_daily(
            s, cfg, day,
            metrics=metrics, rides=rides,
            hevy_client=hevy_client, telegram_client=telegram_client,
            dry_run=dry_run, source=source,
        )


def job_reconcile(
    cfg: Any,
    *,
    day: date | None = None,
    hevy_client: Any = None,
    dias_atras: int = 3,
) -> list[Any]:
    """Lee de Hevy lo que se entrenó y avanza las rachas.

    Mira `dias_atras` días y no solo hoy. Una sesión de las diez de la noche que
    se sincroniza al día siguiente llegaría tarde a su propia reconciliación, y
    la racha se rompería por un problema de reloj y no por una sesión mal hecha.
    Como `run_reconcile` es idempotente, repasar días ya cerrados no cuesta nada.
    """
    day = day or date.today()
    if hevy_client is None:
        log.warning("reconciliación: sin cliente de Hevy, no se puede saber "
                    "qué se entrenó y las rachas se quedan quietas")
        return []

    desde = day - timedelta(days=dias_atras)
    workouts = hevy_client.get_workouts(since=desde)

    salida = []
    with session_scope() as s:
        for i in range(dias_atras + 1):
            d = desde + timedelta(days=i)
            salida.append(run_reconcile(s, cfg, d, workouts=workouts))
    return salida


def job_fetch_garmin(
    cfg: Any, *, day: date | None = None, fetch: Callable | None = None
) -> int:
    """Refresca la caché larga de salidas antes de que nadie la necesite.

    Se hace de madrugada y aparte de la decisión a propósito: Garmin limita por
    IP, y si la lectura y la decisión fueran el mismo trabajo un 429 se llevaría
    por delante las dos cosas.
    """
    from app.integrations.activity_cache import refresh_cache
    from app.settings import REPO_ROOT

    day = day or date.today()
    return refresh_cache(
        REPO_ROOT / "data" / "cache" / "activities.json",
        day,
        days=RIDE_HISTORY_DAYS,
        fetch=fetch,
    )


def dias_de_wellness(cfg: Any) -> int:
    """Cuántos días de wellness hay que pedirle a Garmin, según el config.

    Estaba escrito `7` a pelo, con un comentario en `GarminClient.window`
    diciendo que 7 bastaba "porque las líneas base usan una ventana de esa
    escala". La escala la fija `baseline.window_days`, que es un número que el
    usuario puede cambiar, y cambiarlo no movía esto: subirlo a 14 dejaba la
    línea base de HRV por debajo de `min_days_required` para siempre, y el
    sistema informaba de que no había datos suficientes mientras los datos
    estaban en Garmin sin pedir. Un fallo así no se diagnostica desde el
    síntoma, porque el síntoma acusa a Garmin.

    Se pide `window_days + 1`: la ventana son los días ANTERIORES a la fecha
    (`_baseline_for` cuenta desde `day - 1`), así que con `window_days` justos
    entraban solo `window_days - 1`. Con el config actual, 8 en vez de 7.

    El histórico largo -el que necesitan `load_3d_p90` y `load_7d_p90`- NO sale
    de aquí: son salidas, vienen en una sola llamada por rango y se piden con
    `RIDE_HISTORY_DAYS`. El wellness se consulta día a día y cada día son
    varias peticiones, así que este número se mantiene pequeño a propósito.
    """
    raw = cfg.raw if hasattr(cfg, "raw") else (cfg or {})
    return int((raw.get("baseline") or {}).get("window_days", 7)) + 1


def _fetch_garmin(cfg: Any, day: date) -> tuple[list, list]:
    """Lectura real de Garmin. Aislada para poder inyectar otra en los tests."""
    from app.integrations.activity_cache import load_cached_rides, merge_rides
    from app.integrations.garmin import build_client
    from app.settings import REPO_ROOT, settings

    client = build_client(settings, cfg)
    client.connect()
    metrics, rides = client.window(
        day, dias_de_wellness(cfg), ride_days=RIDE_HISTORY_DAYS
    )

    cache = load_cached_rides(REPO_ROOT / "data" / "cache" / "activities.json")
    return metrics, merge_rides(cache.rides if cache.available else [], rides)


# ---------------------------------------------------------------------------
# Montaje
# ---------------------------------------------------------------------------


def build_scheduler(
    cfg: Any,
    *,
    hevy_client: Any = None,
    telegram_client: Any = None,
    dry_run: bool = False,
    start: bool = True,
) -> BackgroundScheduler:
    """Monta los tres trabajos del día con sus horas del `config.yaml`."""
    tz = ZoneInfo(cfg.timezone)
    sched = BackgroundScheduler(
        timezone=tz,
        job_defaults={
            # Ver la cabecera: los tres defectos que fallan callando.
            "misfire_grace_time": MARGEN_S,
            "coalesce": True,
            # Un único escritor sobre SQLite. Dos decisiones a la vez sobre el
            # mismo día no se corrompen, pero sí se pisan.
            "max_instances": 1,
        },
    )

    def cron(clave: str, defecto: str) -> CronTrigger:
        h, m = _hora(cfg, clave, defecto)
        return CronTrigger(hour=h, minute=m, timezone=tz)

    sched.add_job(
        job_fetch_garmin, cron("garmin_fetch_time", "06:30"),
        args=[cfg], id="garmin_fetch", name="Refrescar caché de Garmin",
    )
    sched.add_job(
        job_decision, cron("fallback_decision_time", "09:00"),
        args=[cfg],
        kwargs={
            "hevy_client": hevy_client, "telegram_client": telegram_client,
            "dry_run": dry_run, "source": "fallback_0900",
            "solo_si_falta_checkin": True,
        },
        id="decision_fallback", name="Decisión sin check-in",
    )
    sched.add_job(
        job_reconcile, cron("evening_summary_time", "22:30"),
        args=[cfg], kwargs={"hevy_client": hevy_client},
        id="reconcile", name="Reconciliar lo entrenado",
    )

    sched.add_listener(
        _avisador(telegram_client), EVENT_JOB_ERROR | EVENT_JOB_MISSED
    )

    if start:
        sched.start()
    return sched


def _avisador(telegram_client: Any) -> Callable:
    """Un trabajo que falla tiene que doler, no quedarse en el log.

    Sin esto el modo de fallo del sistema es el silencio: no llega mensaje, y no
    llegar mensaje se parece mucho a un día de descanso.
    """

    def escuchar(event) -> None:  # noqa: ANN001
        perdido = getattr(event, "exception", None) is None
        if perdido:
            texto = (
                f"⚠️ El trabajo <b>{event.job_id}</b> no llegó a ejecutarse "
                f"(estaba previsto para {getattr(event, 'scheduled_run_time', '?')}). "
                f"Probablemente el sistema estuviera apagado."
            )
            log.error("trabajo perdido: %s", event.job_id)
        else:
            texto = (
                f"⚠️ El trabajo <b>{event.job_id}</b> ha fallado:\n"
                f"<code>{event.exception}</code>\n"
                f"Hoy puede que no tengas decisión. Revisa los registros."
            )
            log.error("trabajo fallido: %s", event.job_id, exc_info=event.exception)

        if telegram_client is None:
            return
        try:
            telegram_client.send(texto)
        except Exception:  # noqa: BLE001
            # Si ni el aviso se puede mandar, al menos que quede escrito.
            log.exception("no se pudo avisar de que el trabajo falló")

    return escuchar
