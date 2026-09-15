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

...Y POR QUÉ ADEMÁS RECOMPUTA
-----------------------------
Había un segundo caso que el fallback no cubría y que en la práctica es el
NORMAL, no el raro. El check-in dispara la decisión en el acto; si se rellena a
las 06:23 y el reloj todavía no ha subido la noche, Garmin contesta vacío y el
día se decide con la HRV, las pulsaciones y el sueño sin evaluar. A las 09:00 el
reloj ya ha sincronizado, pero el trabajo se suprimía por la única razón de que
existía check-in.

Eso confundía dos cosas distintas: «ya tengo tu respuesta» y «ya tengo todos los
datos». La primera es motivo para no atropellar la decisión; la segunda es la
que de verdad importa, y no se estaba mirando.

Ahora, si hay check-in, se mira la decisión vigente: si alguna regla quedó SIN
EVALUAR por un dato de Garmin ausente, se vuelve a pedir el wellness. Y solo si
el dato ha llegado de verdad se decide otra vez, con `source="recompute"` -no
`fallback_0900`, que sería mentir sobre un día que sí tuvo check-in-. Si a las
09:00 sigue sin haber dato, no se escribe nada: una segunda decisión idéntica
marcada con otra fuente estropea el registro sin arreglar la mañana.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app import repository as repo
from app.db import session_scope
from app.engine.decision import DecisionAnulada
from app.integrations.telegram import escapar_html
from app.runner import run_aviso_percepcion, run_daily, run_reconcile

log = logging.getLogger(__name__)

# Una hora de margen. El número no es mágico: es "lo que tarda un Umbrel en
# reiniciarse y volver". Con el defecto de 1 segundo, cualquier arranque que no
# caiga en el segundo exacto se salta el trabajo del día.
MARGEN_S = 3600

# Lo que espera la recuperación de bienestar antes de mirar si falta algo. No es
# un número delicado: es "el tiempo de que el servidor acabe de levantarse". Si
# fuera 0 competiría con el arranque; si fueran diez minutos, un contenedor que
# se reinicia a menudo no llegaría nunca a ejecutarla.
RETRASO_BACKFILL_S = 90

# Las cinco medidas que vienen del reloj. Si una regla se saltó porque faltaba
# alguna de estas, el problema NO es que falte la respuesta del usuario: es que
# a esa hora Garmin no tenía la noche, y volver a preguntarle un rato después
# puede arreglarlo.
#
# Deliberadamente NO están aquí las derivadas (`hrv_ratio`, `rhr_delta`,
# `hrv_baseline`, `rhr_baseline`). Una base que falta no la arregla refrescar el
# dato de hoy: le faltan días de historia, y meterla en esta lista convertiría
# cada mañana de una instalación recién estrenada en una recomputación
# garantizada que nunca puede salir bien. Además viajan acompañadas -la regla
# `fc_reposo_disparada` se salta con `["rhr", "rhr_delta"]`, no con `rhr_delta`
# a secas-, así que mirar las directas ya las cubre.
MEDIDAS_DE_GARMIN = frozenset(
    {"hrv", "rhr", "sleep_min", "sleep_score", "body_battery"}
)


def _medidas_que_faltaban(fila: Any) -> set[str]:
    """Qué medidas del reloj dejaron reglas sin evaluar en la decisión vigente.

    Se lee de `skipped_rules_json`, que el motor ya guardaba: cada regla saltada
    trae la lista de señales que le faltaron. Aquí no se distingue una regla de
    otra, solo interesa el conjunto de medidas ausentes.

    Devuelve un conjunto vacío si no hay fila, si el JSON no se puede leer o si
    lo que faltaba no venía del reloj (el caso de un día sin check-in, donde lo
    ausente es `fatigue` o `lower_discomfort` y volver a preguntar a Garmin no
    arregla nada).
    """
    if fila is None or not getattr(fila, "skipped_rules_json", None):
        return set()
    try:
        saltadas = json.loads(fila.skipped_rules_json)
    except (ValueError, TypeError):
        # Un JSON ilegible no se convierte en «no faltaba nada»: se dice y se
        # sigue sin recomputar. Inventar una recomputación sobre un registro que
        # no se entiende es peor que dejar la mañana como está.
        log.warning("decisión de %s: skipped_rules_json ilegible", fila.date)
        return set()
    if not isinstance(saltadas, list):
        return set()
    fuera: set[str] = set()
    for regla in saltadas:
        if not isinstance(regla, dict):
            continue
        for señal in regla.get("missing") or []:
            if señal in MEDIDAS_DE_GARMIN:
                fuera.add(str(señal))
    return fuera


def _lo_que_llego(metrics: list, day: date, medidas: set[str]) -> set[str]:
    """De las medidas que faltaban, cuáles trae ya la lectura nueva."""
    hoy = next((m for m in metrics or [] if getattr(m, "date", None) == day), None)
    if hoy is None:
        return set()
    return {m for m in medidas if getattr(hoy, m, None) is not None}

# El histórico de salidas ya NO se pide con una constante: sale de
# `cycling.fetch` a través de `ventana_de_salidas`, que elige entre la ventana
# corta de cada mañana y el backfill según lo que la caché pueda sostener.
# Aquí había un `RIDE_HISTORY_DAYS = 190` que contradecía al YAML.


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
    client_errors: dict[str, str] | None = None,
    dry_run: bool = False,
    solo_si_falta_checkin: bool = True,
) -> Any:
    """Decide el día. Por defecto solo si el check-in no ha llegado, o si la
    decisión que hay se tomó sin datos del reloj y ahora sí los hay.

    `solo_si_falta_checkin` evita el atropello: si a las 09:00 el usuario ya
    rellenó el formulario a las 07:40, la decisión buena ya está tomada y
    volver a tomarla la sustituiría por otra igual pero marcada como
    `fallback_0900`. El registro diría que ese día se decidió sin check-in
    teniéndolo, que es exactamente lo contrario de lo que pasó.

    La excepción -ver la cabecera del módulo- es la decisión CIEGA: la que se
    tomó con reglas sin evaluar porque a esa hora Garmin no tenía la noche. Esa
    sí se vuelve a tomar, y solo cuando el dato que faltaba ha llegado.
    """
    day = day or date.today()
    with session_scope() as s:
        recomputando: set[str] = set()
        if solo_si_falta_checkin and repo.get_checkin(s, day) is not None:
            previa = repo.current_decision(s, day)
            recomputando = _medidas_que_faltaban(previa)
            if not recomputando:
                log.info("decisión de %s: ya hay check-in, el fallback no actúa", day)
                return None
            log.info(
                "decisión de %s: hay check-in, pero se decidió sin %s; se "
                "vuelve a pedir el wellness",
                day, ", ".join(sorted(recomputando)),
            )

        metrics, rides = (fetch or _fetch_garmin)(cfg, day)

        anulacion = None
        if recomputando:
            llegado = _lo_que_llego(metrics, day, recomputando)
            if not llegado:
                # Sigue sin haber dato. Se deja la mañana como está: escribir
                # una segunda decisión idéntica solo para cambiarle la fuente
                # ensucia el histórico y no evalúa ni una regla más.
                log.info(
                    "decisión de %s: el wellness sigue sin llegar (%s); no se "
                    "recomputa",
                    day, ", ".join(sorted(recomputando)),
                )
                return None
            anulacion = DecisionAnulada(
                anterior=previa.light,
                decidida_a=previa.computed_at,
                fuente_anterior=previa.source,
                medidas=sorted(llegado),
                sin_llegar=sorted(recomputando - llegado),
            )
            source = "recompute"

        return run_daily(
            s, cfg, day,
            metrics=metrics, rides=rides,
            hevy_client=hevy_client, telegram_client=telegram_client,
            client_errors=client_errors,
            dry_run=dry_run, source=source,
            anulacion=anulacion,
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
        # Se lanza en vez de devolver [], y no es una elección de estilo: es la
        # ÚNICA forma de que esto se sepa. Un WARNING en el log a las 22:30, en
        # un hilo de APScheduler y en un Umbrel al que nadie se conecta, es
        # exactamente igual de visible que no escribir nada.
        #
        # Lanzando, salta `_avisador` (EVENT_JOB_ERROR) y llega un Telegram.
        #
        # Lo que se pierde callando no es un log: sin reconciliación no se sabe
        # qué se entrenó, así que el cumplimiento no se registra, las rachas se
        # quedan quietas y la progresión se para. Y se para EN SILENCIO, con la
        # peor forma posible de enterarse: notar semanas después que no sube
        # nada y no tener por dónde empezar a mirar, porque el sistema lleva
        # todo ese tiempo mandando su mensaje de las nueve como si tal cosa.
        raise RuntimeError(
            "reconciliación sin cliente de Hevy: no se puede saber qué se "
            "entrenó, así que el cumplimiento no se registra, las rachas se "
            "quedan quietas y la progresión se para. Revisa HEVY_API_KEY en "
            "el .env."
        )

    desde = day - timedelta(days=dias_atras)
    workouts = hevy_client.get_workouts(since=desde)

    salida = []
    with session_scope() as s:
        for i in range(dias_atras + 1):
            d = desde + timedelta(days=i)
            salida.append(run_reconcile(s, cfg, d, workouts=workouts))
    return salida


def job_aviso_percepcion(
    cfg: Any,
    *,
    day: date | None = None,
    telegram_client: Any = None,
    client_errors: dict[str, str] | None = None,
    dry_run: bool = False,
) -> Any:
    """Evalúa las sesiones que ya se pueden juzgar y cuenta las disociaciones.

    Va DESPUÉS de la decisión del día -es "la mañana siguiente" de la sesión de
    ayer, no un comentario sobre la de hoy- y en un envío propio. Ver
    `run_aviso_percepcion` para por qué no se pega al mensaje de la decisión y
    por qué evalúa aquí en vez de por la noche.

    NO lanza cuando no hay cliente de Telegram, y es la diferencia con
    `job_reconcile`. Allí la falta de cliente para de verdad el sistema: sin
    reconciliación no se registra el cumplimiento y la progresión se congela en
    silencio. Aquí no se para nada -la tabla se escribe igual, la pantalla
    enseña el contador igual- y lo único que pasa es que el aviso se queda sin
    mandar, sin marcar y a la espera. Reventar el trabajo por eso mandaría un
    Telegram de error... por el mismo canal que no funciona.
    """
    day = day or date.today()
    with session_scope() as s:
        res = run_aviso_percepcion(
            s, cfg, day,
            telegram_client=telegram_client,
            dry_run=dry_run,
            motivo_sin_cliente=(client_errors or {}).get("telegram"),
        )

    for p in res.problemas:
        log.error("aviso de percepción: %s", p)
    log.info(
        "aviso de percepción del %s: %d evaluadas, %d pendientes, %d contadas (%s)",
        day, res.evaluadas, res.pendientes, res.marcadas, res.status,
    )
    return res


def job_fetch_garmin(
    cfg: Any, *, day: date | None = None, fetch: Callable | None = None
) -> int:
    """Refresca la caché larga de salidas antes de que nadie la necesite.

    Se hace de madrugada y aparte de la decisión a propósito: Garmin limita por
    IP, y si la lectura y la decisión fueran el mismo trabajo un 429 se llevaría
    por delante las dos cosas.

    Por eso mismo la ventana la decide `ventana_de_salidas` y no una constante:
    pedir 190 días cada noche para añadir la salida de ayer es gastar el cupo
    de peticiones en datos que ya están en disco.
    """
    from app.integrations.activity_cache import (
        RUTA_CACHE_SALIDAS, load_cached_rides, refresh_cache, ventana_de_salidas,
    )

    day = day or date.today()
    ruta = RUTA_CACHE_SALIDAS
    dias, motivo = ventana_de_salidas(cfg, day, load_cached_rides(ruta))
    # El motivo se registra siempre. Un backfill que se repite cada madrugada
    # es un síntoma -la caché no se está escribiendo- y sin esta línea el único
    # rastro sería la factura de peticiones a Garmin.
    log.info("caché de salidas: %s", motivo)
    return refresh_cache(ruta, day, days=dias, fetch=fetch)


def job_backfill_wellness(
    cfg: Any, *, day: date | None = None, cliente: Any = None
) -> Any:
    """Al arrancar, rellena los días de bienestar que falten.

    POR QUÉ AL ARRANCAR Y NO A UNA HORA
    -----------------------------------
    Los agujeros de `daily_metrics` no los abre una hora del día: los abre que
    el proceso no estuviera corriendo. Un trabajo a las 06:40 no arregla nada si
    el PC estuvo apagado la semana entera, porque a las 06:40 de esos días
    tampoco había nadie. El único momento en que se sabe con seguridad que el
    sistema está vivo es justo después de arrancar, y es entonces cuando hay que
    preguntarse qué se perdió mientras no lo estaba.

    POR QUÉ NO SE HACE EN EL `lifespan`
    -----------------------------------
    Porque tarda. Un login contra Garmin más cuarenta y cinco días a dos
    segundos son minutos, y el `lifespan` de FastAPI bloquea el arranque del
    servidor: la PWA no respondería hasta que esto acabara. Como trabajo del
    scheduler se lleva gratis las tres cosas que hacen falta -`max_instances=1`
    para que dos arranques seguidos no se pisen, `misfire_grace_time` de una
    hora, y el escuchador que manda los fallos por Telegram-.

    EL LOGIN SE HACE SOLO SI HAY ALGO QUE PEDIR
    -------------------------------------------
    Se mira la base ANTES de conectarse. En el caso normal -el PC encendido de
    ayer a hoy- no falta ningún día, y entonces esto no gasta ni una petición ni
    una sesión de Garmin. Conectarse primero y preguntar después convertiría el
    arranque de cada despliegue en un login, que es de las cosas que Garmin
    cuenta para cortar por IP.
    """
    from app.backfill import (
        ResultadoBackfill, dias_pendientes, recuperar_al_arrancar,
        ventana_de_recuperacion,
    )

    day = day or date.today()
    ventana = ventana_de_recuperacion(cfg, day)
    if ventana is None:
        log.info("backfill de arranque: apagado (wellness.backfill.recovery_days)")
        return ResultadoBackfill()

    with session_scope() as s:
        if not dias_pendientes(s, *ventana):
            log.info(
                "backfill de arranque: nada que recuperar entre %s y %s",
                ventana[0], ventana[1],
            )
            return ResultadoBackfill()

        if cliente is None:
            from app.integrations.garmin import build_client
            from app.settings import settings

            cliente = build_client(settings, cfg)
            cliente.connect()

        res = recuperar_al_arrancar(s, cliente, cfg, hoy=day)

    # Fuera del `with` a propósito: `rellenar` va haciendo commit día a día, así
    # que aquí no queda nada por escribir y lanzar no tira nada a la basura.
    #
    # Y se lanza, en vez de devolver el resultado y ya está, porque un corte por
    # límite deja el trabajo A MEDIAS y nadie lo va a repetir: la recuperación
    # solo se dispara al arrancar, y un PC que se queda encendido no vuelve a
    # arrancar en semanas. Sin este aviso, el agujero que este trabajo existe
    # para tapar se quedaría tapado a medias y en silencio, que es justo la
    # forma en que se abrió.
    if res.interrumpido:
        raise RuntimeError(f"backfill de arranque: {res.interrumpido}")
    return res


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

    El histórico largo -el que necesitan `load_2d_p90` y `load_7d_p90`- NO sale
    de aquí: son salidas, vienen en una sola llamada por rango y las gobierna
    `ventana_de_salidas` con `cycling.fetch`. El wellness se consulta día a día
    y cada día son varias peticiones, así que este número se mantiene pequeño a
    propósito.
    """
    raw = cfg.raw if hasattr(cfg, "raw") else (cfg or {})
    return int((raw.get("baseline") or {}).get("window_days", 7)) + 1


def _fetch_garmin(cfg: Any, day: date) -> tuple[list, list]:
    """Lectura real de Garmin. Aislada para poder inyectar otra en los tests."""
    from app.integrations.activity_cache import (
        RUTA_CACHE_SALIDAS, load_cached_rides, merge_rides, ventana_de_salidas,
    )
    from app.integrations.garmin import build_client
    from app.settings import settings

    # La caché se lee ANTES de llamar a Garmin, no después. Es la que decide
    # cuánto hay que pedir: con una caché sana basta la ventana corta, porque
    # el resto del histórico ya está en disco y se fusiona abajo. Si la caché
    # no está o no llega, esta petición es la única fuente y tiene que traer
    # el histórico entero o los umbrales adaptativos se quedan sin base.
    cache = load_cached_rides(RUTA_CACHE_SALIDAS)
    ride_days, motivo = ventana_de_salidas(cfg, day, cache)
    log.info("caché de salidas: %s", motivo)

    client = build_client(settings, cfg)
    client.connect()
    metrics, rides = client.window(
        day, dias_de_wellness(cfg), ride_days=ride_days
    )

    return metrics, merge_rides(cache.rides if cache.available else [], rides)


# ---------------------------------------------------------------------------
# La auditoría de arranque: qué NO corrió mientras el sistema no estaba
# ---------------------------------------------------------------------------

# Cuántos disparos perdidos se enumeran como mucho. Un contenedor parado tres
# meses tiene noventa disparos perdidos de cada trabajo, y ni el mensaje ni el
# bucle tienen por qué recorrerlos: a partir de un puñado, lo que informa es
# «desde tal día no corre», no la lista. El número exacto SÍ se cuenta hasta el
# tope y se dice que hay más, porque un «20+» que en realidad son 3 mentiría.
TOPE_PERDIDOS = 20

# Cuánto espera la auditoría antes de mirar. Corta, al revés que la de bienestar:
# esto no habla con Garmin ni con Hevy -lee cuatro filas y manda un mensaje-, y
# lo que cuenta es que el aviso llegue mientras el usuario todavía relaciona el
# mensaje con el reinicio que acaba de hacer.
RETRASO_AUDITORIA_S = 10


def disparos_perdidos(
    trigger: CronTrigger,
    desde: datetime,
    hasta: datetime,
    *,
    tope: int = TOPE_PERDIDOS,
) -> tuple[list[datetime], bool]:
    """A qué horas debió saltar este disparador entre `desde` y `hasta`.

    Los dos extremos son ABIERTO por abajo y CERRADO por arriba: un disparo que
    cae exactamente en `desde` ya está contado -`desde` es la última vez que el
    trabajo terminó bien, o hasta dónde miró la auditoría anterior- y volver a
    contarlo avisaría de un hueco que no existe.

    Devuelve la lista y un booleano que dice si se cortó por el tope. El
    booleano existe para que el mensaje pueda distinguir «han sido veinte» de
    «han sido veinte o más», que con el sistema parado una temporada es la
    diferencia entre un dato y un número inventado.
    """
    perdidos: list[datetime] = []
    # El microsegundo NO es cosmético: `get_next_fire_time` devuelve la primera
    # cita MAYOR O IGUAL que el cursor, así que arrancando en `desde` clavado el
    # primer resultado es el propio `desde`. Con `desde` = «la última vez que el
    # trabajo terminó bien», eso hacía que la auditoría acusara de haber perdido
    # justo la ejecución que sí se hizo, y que lo hiciera en CADA arranque. Lo
    # encontró `test_el_disparo_que_cae_justo_en_el_suelo_no_se_vuelve_a_contar`.
    cursor = desde + timedelta(microseconds=1)
    while len(perdidos) < tope:
        # `previous_fire_time=None` a propósito: así `now` manda y el disparador
        # devuelve la primera cita a partir del cursor, que es justo la forma de
        # recorrer hacia delante un intervalo ya pasado.
        siguiente = trigger.get_next_fire_time(None, cursor)
        if siguiente is None or siguiente > hasta:
            return perdidos, False
        perdidos.append(siguiente)
        cursor = siguiente + timedelta(microseconds=1)
    # Se ha llenado el cupo: queda por saber si había alguno más.
    siguiente = trigger.get_next_fire_time(None, cursor)
    return perdidos, bool(siguiente is not None and siguiente <= hasta)


def suelo_de_vigilancia(fila: Any, arranque: datetime) -> datetime:
    """Desde cuándo se cuentan los huecos de un trabajo.

    El más reciente de las tres marcas de `JobRun`, y si no hay fila, el momento
    del arranque. Ver el docstring del modelo para por qué son tres.

    Que el caso «no hay fila» valga `arranque` es lo que hace que el primer
    arranque de la vida del sistema -o el primero tras crear la tabla- no acuse
    a nadie: el intervalo (arranque, ahora] está vacío. No se inventa un pasado
    limpio, es que de verdad no hay pasado que juzgar, y decir lo contrario sería
    exactamente el fallo que esta tabla existe para evitar.
    """
    if fila is None:
        return arranque
    marcas = [
        m for m in (fila.first_seen_at, fila.last_finished_at, fila.checked_through)
        if m is not None
    ]
    return max(marcas) if marcas else arranque


def _a_utc_naive(momento: datetime) -> datetime:
    """A UTC sin tzinfo, que es como guarda las fechas el resto de la base.

    `server_default=func.now()` de SQLite escribe UTC naive, y mezclar en una
    misma columna fechas con zona y sin ella hace que las comparaciones lancen
    o, peor, que se comparen mal en silencio.
    """
    if momento.tzinfo is None:
        return momento
    return momento.astimezone(timezone.utc).replace(tzinfo=None)


def auditar_arranque(
    vigilados: dict[str, CronTrigger],
    tz: ZoneInfo,
    *,
    telegram_client: Any = None,
    dry_run: bool = False,
    ahora: datetime | None = None,
) -> list[tuple[str, list[datetime], bool]]:
    """Qué trabajos debieron correr mientras el sistema no estaba, y avisar.

    POR QUÉ ESTO NO LO CUBRE `EVENT_JOB_MISSED`
    -------------------------------------------
    Porque ese evento necesita un proceso vivo al que se le pase la hora. Cuando
    el contenedor se para y vuelve, el planificador nuevo nace sin pasado y sus
    `CronTrigger` calculan la próxima cita desde ahora: las de ayer no son citas
    perdidas, son citas que para él nunca existieron. Ver `JobRun`.

    Devuelve lo encontrado ADEMÁS de avisar, para que se pueda comprobar sin
    mirar Telegram y para que la llamada tenga algo que afirmar en los tests.
    """
    from app.models import JobRun

    ahora = ahora or datetime.now(tz)
    arranque_utc = _a_utc_naive(ahora)
    encontrados: list[tuple[str, list[datetime], bool]] = []

    with session_scope() as s:
        for job_id, trigger in vigilados.items():
            fila = s.get(JobRun, job_id)
            if fila is None:
                fila = JobRun(job_id=job_id, first_seen_at=arranque_utc)
                s.add(fila)
            suelo = suelo_de_vigilancia(fila, arranque_utc)
            # El disparador piensa en hora local con zona; la base guarda UTC
            # naive. La conversión va aquí, en la frontera, y no repartida.
            desde = suelo.replace(tzinfo=timezone.utc).astimezone(tz)
            perdidos, hay_mas = disparos_perdidos(trigger, desde, ahora)
            # La marca se mueve SIEMPRE, haya habido hueco o no. Si solo se
            # moviera al avisar, un reinicio detrás de otro volvería a mirar el
            # mismo intervalo y repetiría el mismo aviso.
            fila.checked_through = arranque_utc
            if perdidos:
                encontrados.append((job_id, perdidos, hay_mas))
                log.error(
                    "trabajo no ejecutado mientras el sistema no estaba: %s, "
                    "%d disparo(s) desde %s",
                    job_id, len(perdidos), desde,
                )

    if not encontrados:
        log.info(
            "auditoría de arranque: los %d trabajos vigilados al día",
            len(vigilados),
        )
        return encontrados

    if telegram_client is not None:
        texto = mensaje_de_arranque(encontrados, tz)
        try:
            r = telegram_client.send(texto, dry_run=dry_run)
        except Exception:  # noqa: BLE001
            log.exception("no se pudo avisar de los trabajos no ejecutados")
            return encontrados
        # Mismo motivo que en `_avisador`: `send` no lanza cuando Telegram dice
        # que no, devuelve un resultado diciéndolo, y tirarlo dejaba el aviso de
        # avería sin rastro en ninguna parte.
        if r is not None and not getattr(r, "sent", False):
            log.error(
                "el aviso de trabajos no ejecutados NO se ha enviado: %s",
                getattr(r, "error", None) or getattr(r, "reason", ""),
            )
    return encontrados


# Cómo se llama cada trabajo cuando hay que explicárselo a una persona. El
# `job_id` es para el log; en el móvil no dice nada. Si un trabajo no está aquí
# sale su id crudo: feo, pero cierto, y prefiero eso a que un trabajo nuevo se
# quede fuera del aviso por no haberlo apuntado en dos sitios.
NOMBRES_LEGIBLES = {
    "garmin_fetch": "traer los datos de Garmin",
    "decision_fallback": "decidir el semáforo del día",
    "reconcile": "apuntar lo que entrenaste",
    "perception_notice": "revisar las sesiones de ayer",
}


def mensaje_de_arranque(
    encontrados: list[tuple[str, list[datetime], bool]], tz: ZoneInfo
) -> str:
    """El aviso, en el idioma del usuario y no en el del planificador.

    Dice QUÉ no se hizo y desde cuándo, no «job_id decision_fallback misfired».
    Y dice qué significa, que es lo único que el usuario puede usar para decidir
    si tiene que hacer algo.
    """
    lineas = [
        "⚠️ <b>El sistema ha estado parado y se ha perdido trabajo.</b>",
        "",
        "Mientras no estaba en marcha no se ejecutó:",
    ]
    for job_id, perdidos, hay_mas in encontrados:
        que = NOMBRES_LEGIBLES.get(job_id, job_id)
        primero = perdidos[0].astimezone(tz)
        n = len(perdidos)
        varias = n > 1 or hay_mas
        # "1 vez/veces" es de las cosas que delatan que el mensaje lo escribió
        # una plantilla y no una persona, y este aviso llega el día en que hay
        # que entender algo rápido.
        cuantos = f"{n}{'+' if hay_mas else ''} veces" if varias else "1 vez"
        cual = "la primera el" if varias else "el"
        lineas.append(
            f"• <b>{escapar_html(que)}</b> — {cuantos}, "
            f"{cual} {primero.strftime('%d/%m a las %H:%M')}"
        )
    lineas += [
        "",
        "Los días afectados no tienen decisión guardada ni entrenos apuntados. "
        "Lo de Garmin se recupera solo al arrancar; lo demás, no.",
    ]
    return "\n".join(lineas)


def _apuntador() -> Callable:
    """Apunta en `job_runs` cada trabajo que TERMINA bien.

    Solo los que terminan: un trabajo que revienta no ha hecho su trabajo, y
    marcarlo como ejecutado taparía el hueco que la auditoría del próximo
    arranque tendría que encontrar. De eso ya avisa `_avisador` por su lado.
    """
    from app.models import JobRun

    def escuchar(event) -> None:  # noqa: ANN001
        try:
            with session_scope() as s:
                fila = s.get(JobRun, event.job_id)
                ahora = datetime.now(timezone.utc).replace(tzinfo=None)
                if fila is None:
                    s.add(JobRun(job_id=event.job_id, first_seen_at=ahora,
                                 last_finished_at=ahora))
                else:
                    fila.last_finished_at = ahora
        except Exception:  # noqa: BLE001
            # Que no se pueda apuntar NO puede tumbar el trabajo que ya salió
            # bien. Pero se registra: sin esta línea, una base bloqueada dejaría
            # la vigilancia ciega sin que nadie lo supiera, y la próxima
            # auditoría acusaría de un hueco inventado.
            log.exception("no se pudo apuntar la ejecución de %s", event.job_id)

    return escuchar


# ---------------------------------------------------------------------------
# Montaje
# ---------------------------------------------------------------------------


def build_scheduler(
    cfg: Any,
    *,
    hevy_client: Any = None,
    telegram_client: Any = None,
    client_errors: dict[str, str] | None = None,
    dry_run: bool = False,
    start: bool = True,
) -> BackgroundScheduler:
    """Monta los trabajos: tres con hora del `config.yaml` y uno al arrancar."""
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

    # Los disparadores de hora fija, apuntados según se crean. Son los únicos
    # que la auditoría de arranque puede juzgar: de un trabajo con hora se sabe
    # cuándo debió correr, y del que se dispara al arrancar no hay nada que
    # reprochar porque su cita ES el arranque. Recogerlos aquí, en el mismo
    # sitio donde se construyen, evita la segunda lista escrita a mano que se
    # queda vieja el día que se añada un trabajo.
    vigilados: dict[str, CronTrigger] = {}

    def cron(clave: str, defecto: str, job_id: str) -> CronTrigger:
        h, m = _hora(cfg, clave, defecto)
        t = CronTrigger(hour=h, minute=m, timezone=tz)
        vigilados[job_id] = t
        return t

    sched.add_job(
        job_fetch_garmin, cron("garmin_fetch_time", "06:30", "garmin_fetch"),
        args=[cfg], id="garmin_fetch", name="Refrescar caché de Garmin",
    )
    sched.add_job(
        job_decision, cron("fallback_decision_time", "09:00", "decision_fallback"),
        args=[cfg],
        kwargs={
            "hevy_client": hevy_client, "telegram_client": telegram_client,
            "client_errors": client_errors,
            "dry_run": dry_run, "source": "fallback_0900",
            "solo_si_falta_checkin": True,
        },
        id="decision_fallback", name="Decisión sin check-in",
    )
    sched.add_job(
        job_reconcile, cron("evening_summary_time", "22:30", "reconcile"),
        args=[cfg], kwargs={"hevy_client": hevy_client},
        id="reconcile", name="Reconciliar lo entrenado",
    )
    # Después del fallback de las 09:00 a propósito. Para entonces la decisión
    # del día ya se ha mandado por uno de sus dos caminos, así que este llega
    # como lo que es -un mensaje aparte sobre AYER- y no se mezcla con el plan
    # de hoy. Antes de las nueve competiría con el check-in de la mañana, que es
    # justo el dato que hace falta para poder evaluar la sesión de ayer.
    sched.add_job(
        job_aviso_percepcion, cron("perception_notice_time", "09:30", "perception_notice"),
        args=[cfg],
        kwargs={
            "telegram_client": telegram_client,
            "client_errors": client_errors,
            "dry_run": dry_run,
        },
        id="perception_notice", name="Contar las disociaciones de ayer",
    )
    # El cuarto trabajo no tiene hora: tiene un retraso. Ver
    # `job_backfill_wellness` para por qué se dispara al arrancar y no a una
    # hora fija. El minuto y medio es para no competir con el arranque: deja que
    # el servidor termine de levantarse y que la PWA responda antes de ponerse a
    # hablar con Garmin.
    sched.add_job(
        job_backfill_wellness,
        DateTrigger(run_date=datetime.now(tz) + timedelta(seconds=RETRASO_BACKFILL_S)),
        args=[cfg], id="backfill_wellness",
        name="Recuperar días de bienestar perdidos",
    )

    # La auditoría del arranque. Va como trabajo y no dentro de `build_scheduler`
    # por lo mismo que el backfill: manda un Telegram, y una llamada de red en el
    # camino del arranque retrasa la respuesta de la PWA. Diez segundos bastan
    # para no competir con el arranque y son pocos para que el aviso llegue
    # mientras el usuario todavía se acuerda de haber reiniciado.
    sched.add_job(
        auditar_arranque,
        DateTrigger(run_date=datetime.now(tz) + timedelta(seconds=RETRASO_AUDITORIA_S)),
        args=[vigilados, tz],
        kwargs={"telegram_client": telegram_client, "dry_run": dry_run},
        id="startup_audit", name="Mirar qué no corrió mientras no estaba",
    )

    sched.add_listener(
        _avisador(telegram_client), EVENT_JOB_ERROR | EVENT_JOB_MISSED
    )
    # Y el que apunta lo que SÍ sale bien, que es de donde la auditoría del
    # próximo arranque saca el suelo desde el que contar.
    sched.add_listener(_apuntador(), EVENT_JOB_EXECUTED)

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
        # Todo lo que viene de fuera va escapado. El `job_id` lo pone este
        # fichero y sería seguro, pero escaparlo cuesta lo mismo que razonar
        # cada vez sobre si esta interpolación concreta es de las seguras.
        quien = escapar_html(event.job_id)
        if perdido:
            cuando = escapar_html(getattr(event, "scheduled_run_time", "?"))
            texto = (
                f"⚠️ El trabajo <b>{quien}</b> no llegó a ejecutarse "
                f"(estaba previsto para {cuando}). "
                f"Probablemente el sistema estuviera apagado."
            )
            log.error("trabajo perdido: %s", event.job_id)
        else:
            # `escapar_html` aquí no es cosmética. El `str` de una excepción de
            # Python lleva ángulos cada dos por tres -`'<' not supported
            # between instances of...`, un `<Response [500]>`, un repr
            # cualquiera- y sin escapar Telegram contesta 400 «can't parse
            # entities» y el aviso NO SALE. Es decir: el único efecto hacia
            # fuera sin freno, el que existe para que un fallo no sea mudo, se
            # volvía mudo precisamente al fallar. Comprobado contra la API.
            texto = (
                f"⚠️ El trabajo <b>{quien}</b> ha fallado:\n"
                f"<code>{escapar_html(event.exception)}</code>\n"
                f"Hoy puede que no tengas decisión. Revisa los registros."
            )
            log.error("trabajo fallido: %s", event.job_id, exc_info=event.exception)

        if telegram_client is None:
            return
        try:
            r = telegram_client.send(texto)
        except Exception:  # noqa: BLE001
            # Si ni el aviso se puede mandar, al menos que quede escrito.
            log.exception("no se pudo avisar de que el trabajo falló")
            return

        # El `send` de Telegram NO lanza cuando la API contesta que no: devuelve
        # un `SendResult` diciéndolo. Aquí se tiraba ese valor a la basura, así
        # que un rechazo de Telegram no dejaba rastro en ninguna parte: ni
        # mensaje, ni línea de log, ni fila. El aviso de avería desaparecía
        # entero y en silencio, que es el modo de fallo exacto que este
        # `_avisador` existe para impedir.
        if r is not None and not getattr(r, "sent", False):
            log.error(
                "el aviso del trabajo %s NO se ha enviado: %s",
                event.job_id, getattr(r, "error", None) or getattr(r, "reason", ""),
            )

    return escuchar
