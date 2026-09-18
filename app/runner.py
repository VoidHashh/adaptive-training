"""El día completo, en un sitio y sin imprimir nada.

Hasta ahora el recorrido de la mañana vivía dentro de `cli.py`, entremezclado
con los `print` del informe. Eso significaba que el camino que iba a ejecutar
APScheduler cada mañana a las siete no era el mismo que probaban los tests ni el
que enseñaba `--dry-run`: había tres versiones parecidas del mismo proceso y
nada garantizaba que siguieran pareciéndose. Aquí está una sola vez.

LAS DOS MITADES DEL DÍA
-----------------------
No es un proceso, son dos, y separarlos no es una comodidad de diseño sino la
única forma de que los números signifiquen algo:

- **`run_daily`**, por la mañana: lee, decide, escribe la rutina en Hevy y manda
  el mensaje. No sabe -ni puede saber- si la sesión se hará. Guarda reglas
  activas, aplazamientos y descarga, pero NO toca las rachas.
- **`run_reconcile`**, después: lee de Hevy lo que se hizo de verdad y avanza
  las rachas. Es lo único que abre la puerta de la subida de carga.

Sin la segunda mitad el sistema manda el mensaje correcto todas las mañanas con
los mismos pesos para siempre, y lo hace sin dar un solo error: `clean_sessions`
se queda a cero porque nadie le cuenta nunca que la sesión se completó.

QUÉ PASA CUANDO ALGO FALLA A MEDIAS
-----------------------------------
El orden es Hevy primero y Telegram después, y cuando Hevy falla el mensaje se
manda IGUAL, diciendo que ha fallado. Las dos alternativas son peores: callarse
deja al usuario sin plan y sin saber por qué, y mandar el mensaje de siempre le
describe una rutina que en la aplicación no está. Un aviso feo es mejor que una
sesión fantasma.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import repository as repo
from app.engine.decision import apply_execution, decide
from app.engine.message import render_telegram
from app.engine.recalibracion import evaluar_recalibracion
from app.engine.rotacion import pendientes as rutinas_pendientes
from app.engine.session_builder import orden_de_rotacion
from app.engine.signals import DIAS_DE_HISTORIA, Checkin, build_signals
from app.engine.tendencia import DecisionDia, evaluar_tendencia
from app.integrations.hevy import (
    SIN_RASTRO,
    claves_hiit,
    motivos_incumplimiento,
    pesos_ejecutados,
    workout_compliance,
)
from app.integrations.telegram import escapar_html
from app.models import HevyWrite, Notification, WorkoutLog

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Resultados
# ---------------------------------------------------------------------------


@dataclass
class DailyResult:
    """Lo que ha pasado esta mañana. Sin `print`: lo pinta quien llame."""

    day: date
    decision: Any
    # ok | error | skipped | dry_run | read_only | reverted | stale
    # Los dos últimos son del check-in tardío: `reverted` = se deshizo lo que
    # había escrito una decisión anulada; `stale` = había que deshacerlo y no se
    # pudo, así que en Hevy hay una rutina que hoy no toca.
    hevy_status: str = "skipped"
    hevy_reason: str = ""
    # El código HTTP que contestó Hevy, si contestó. Ver `WriteResult.http_status`:
    # la columna existía y nadie la llenaba nunca.
    hevy_http: int | None = None
    # LO MISMO PARA EL SEGUNDO DESTINO, y con campos propios en vez de
    # machacar los de arriba. Desde que el bloque HIIT va a su propia rutina de
    # Hevy, una mañana con HIIT toca DOS rutinas, y las dos pueden ir distinto:
    # que la de fuerza se escriba y la del HIIT devuelva un 400 es un día en el
    # que abro la app, veo el Día 1 al día y el bloque de la semana pasada.
    # Reutilizar `hevy_status` dejaría el resultado del día siendo el de la
    # última que se escribiera, que es una moneda al aire.
    #
    # `skipped` de salida significa la mayoría de los días: hoy no tocaba HIIT.
    hevy_hiit_status: str = "skipped"
    hevy_hiit_reason: str = ""
    telegram_status: str = "skipped"  # sent | error | skipped | dry_run
    telegram_reason: str = ""
    # Fallos que NO han impedido terminar. Van aquí en vez de a un log que nadie
    # lee: quien llama decide si los enseña, pero no puede alegar que no los
    # sabía.
    problemas: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problemas


class DecisionInterrumpida(RuntimeError):
    """La mañana se cayó A MEDIAS, y trae consigo hasta dónde llegó.

    POR QUÉ NO BASTA CON DEJAR SUBIR LA EXCEPCIÓN
    ---------------------------------------------
    Hevy y Telegram están FUERA de la transacción. Cuando algo revienta después
    de escribir la rutina o de entregar el mensaje, el `rollback` deshace la
    decisión en la base de datos y no deshace ninguna de las dos cosas: el
    teléfono se queda con un plan que el sistema ya no recuerda haber tomado.

    Sin esto, la única forma que tenía la PWA de contar ese momento era una
    frase escrita a mano -"no se ha tocado la rutina de Hevy ni se ha enviado
    ningún mensaje"- que acertaba si el fallo era temprano y mentía si era
    tardío. El 18 de septiembre de 2026 fue tardío: la excepción saltó al
    apuntar el aviso en `notifications`, con el mensaje ya en el móvil, y la
    pantalla dijo que no se había mandado nada.

    Así que el resultado parcial viaja con la excepción. `res` es el mismo
    objeto que habría devuelto `run_daily`, con `hevy_status` y
    `telegram_status` diciendo lo que de verdad ocurrió antes del golpe.
    """

    def __init__(self, causa: BaseException, res: DailyResult) -> None:
        super().__init__(str(causa))
        self.causa = causa
        self.res = res


@dataclass
class ReconcileResult:
    """Qué se ha dado por hecho, y de qué pruebas."""

    day: date
    workouts_nuevos: int = 0
    workouts_ya_contados: int = 0
    executed: dict[str, bool] = field(default_factory=dict)
    # El peso de la serie efectiva más pesada de cada ejercicio, y lo que ese
    # peso movió del objetivo. Salen aquí para que `cli.py` los pueda enseñar:
    # una adopción que solo se ve en el mensaje de mañana es una adopción que no
    # se puede comprobar hoy, cuando se está ensayando el sistema a mano.
    pesos: dict[str, float | None] = field(default_factory=dict)
    adopciones: list[dict[str, Any]] = field(default_factory=list)
    # Los entrenamientos que se registraron pero no emparejan con el plan del
    # día: un HIIT, una rutina que no está en `config.yaml`, algo hecho un día
    # que no tocaba. Antes ni siquiera llegaban a `workout_log`; ahora se
    # guardan igual que el resto y además salen por aquí para que el mensaje de
    # la mañana pueda decirlo.
    sueltos: list[dict[str, Any]] = field(default_factory=list)
    avanzado: bool = False
    motivo: str = ""


@dataclass
class AvisoResult:
    """Qué se evaluó de ayer y qué se contó de ello esta mañana.

    `pendientes` y `marcadas` van separados a propósito. Que sean distintos es
    exactamente el caso interesante -había algo que decir y no se pudo decir- y
    con un solo contador ese caso se leería como que no había nada.
    """

    day: date
    evaluadas: int = 0
    pendientes: int = 0
    marcadas: int = 0
    status: str = "skipped"  # sent | error | skipped | dry_run
    motivo: str = ""
    texto: str = ""
    problemas: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problemas


# ---------------------------------------------------------------------------
# Cuánto wellness hace falta tener delante
# ---------------------------------------------------------------------------


def dias_de_wellness_en_memoria(cfg: Any) -> int:
    """Cuántos días de wellness necesita la mañana, contando hacia atrás.

    NO es lo mismo que `scheduler.dias_de_wellness`, y confundirlos fue el
    fallo. Aquel dice cuántos días hay que PEDIRLE A GARMIN, y es pequeño a
    propósito: el wellness se consulta día a día, cada día son varias llamadas,
    y lo que ya se leyó una vez está guardado. Este dice cuántos días hay que
    TENER DELANTE para decidir, que es otra pregunta y sale mucho más grande.

    Durante toda la vida del sistema hubo una sola respuesta para las dos, la
    pequeña, porque nadie leía `daily_metrics` al decidir. Ocho días de wellness
    para un motor que compara el último mes con los dos anteriores.

    Los dos sumandos salen de su propio sitio:

    - `DIAS_DE_HISTORIA + baseline.window_days`. `build_signals` reconstruye la
      línea base y su derivada para los últimos `DIAS_DE_HISTORIA` días, y la
      base del más antiguo mira los `window_days` ANTERIORES a él. Con el config
      actual, 21.
    - `trend.ventana_larga_dias`. El cualificador de sueño compara la media de
      los últimos 30 días contra la de los 60 anteriores. Con el config actual,
      90, y por eso manda.
    """
    raw = cfg.raw if hasattr(cfg, "raw") else (cfg or {})
    base = int((raw.get("baseline") or {}).get("window_days", 7))
    trend = raw.get("trend") or {}
    larga = int(trend.get("ventana_larga_dias", 0)) if trend.get("enabled") else 0
    return max(DIAS_DE_HISTORIA + base, larga)


# ---------------------------------------------------------------------------
# La mañana
# ---------------------------------------------------------------------------


def run_daily(
    session: Session,
    cfg: Any,
    day: date,
    *,
    metrics: list,
    rides: list,
    hevy_client: Any = None,
    telegram_client: Any = None,
    # Por qué NO se pudo construir cada cliente, cuando no se pudo. Lo llena
    # `api._clientes`, que es el único sitio donde se construyen.
    client_errors: dict[str, str] | None = None,
    dry_run: bool = False,
    source: str = "scheduler",
    # La decisión de esta misma mañana que esta deja sin efecto, cuando la hay.
    # La construye `job_decision`, que es el único que sabe qué había antes y
    # qué dato del reloj ha llegado desde entonces.
    anulacion: Any = None,
) -> DailyResult:
    """Decide el día y lo ejecuta. Los clientes se inyectan a propósito.

    Se pasan `metrics` y `rides` ya leídos en vez de leerlos aquí porque de
    dónde salen los datos es decisión de quien llama -Garmin, la caché, o datos
    de ejemplo en un ensayo- y meterla dentro haría imposible probar el resto
    sin red.

    Lo que sí se lee aquí es la MEMORIA: `metrics` es lo que Garmin acaba de
    contestar, y lo que ya se sabía está en la base. Ver abajo.
    """
    checkin_row = repo.get_checkin(session, day)
    valores = repo.checkin_values(checkin_row)
    checkin = Checkin(date=day, values=valores) if valores else None

    # `sessions` es lo que se entrenó DE VERDAD, leído de `workout_log`. Lo leen
    # DOS señales, y aquí se nombraba solo una:
    #
    # - `intensity_count`, que sin esto contaba solo las salidas de bici: un
    #   HIIT hecho el martes no sumaba y el número que sale en el mensaje del
    #   sábado quedaba corto. La ventana son 14 días porque el recuento es
    #   semanal y la semana puede haber empezado hace seis; sobra de propósito.
    # - `yesterday_routine`, que es de qué rutina habla el RPE de ayer. Sin ella
    #   `blocks: last_session_only` no tenía con qué acotar y congelaba las tres
    #   rutinas. Le basta un día, así que los 14 le sobran de largo.
    #
    # `checkin_history` es el mismo arreglo para el otro parámetro que nadie
    # pasaba nunca: sin él, la serie de cada deslizador tenía un punto y los
    # umbrales adaptativos sobre el formulario habrían sacado percentiles de un
    # solo dato. 90 días y no 14 porque un percentil sobre dos semanas de
    # respuestas no es un percentil, y porque estos datos son diez filas: leer
    # tres meses no cuesta nada.

    # --- el wellness que se sabe, no solo el que se acaba de leer ------------
    #
    # `metrics` trae la ventana corta de Garmin: `baseline.window_days + 1`, que
    # con el config actual son OCHO días. Con esos ocho se construía todo, y
    # `daily_metrics` -seis meses guardados, a un SELECT de distancia- no la
    # leía nadie para decidir. Dos averías, las dos silenciosas:
    #
    # 1. LAS LÍNEAS BASE DE LOS DÍAS ANTERIORES SALÍAN RECORTADAS. La base de
    #    ayer mira los siete días anteriores a ayer, y el más viejo de esos
    #    siete caía fuera de la ventana. Quedaba por encima del mínimo, así que
    #    no había nota ni error: solo una media calculada sobre seis días en vez
    #    de siete. Eso mueve `hrv_ratio`, y `hrv_ratio` de ayer es lo que mira
    #    `consecutive_days: 2`. Medido sobre los 167 días del histórico, el
    #    color cambia en CUATRO: el 19/04 se perdía un rojo de verdad y el
    #    31/03, el 11/04 y el 22/06 salían rojos que con el histórico entero son
    #    ámbar.
    #
    # 2. EL CUALIFICADOR DE SUEÑO NO HABLÓ NUNCA. Compara la media de los
    #    últimos 30 días con la de los 60 anteriores: con ocho días delante no
    #    llega ni a la cobertura mínima, así que contestaba «no hay serie
    #    suficiente» todas las mañanas. 185 días acusando al dato, con el dato
    #    en la base.
    #
    # Se fusiona en vez de sustituir porque lo fresco también hace falta: hoy
    # todavía no está guardado -esto se archiva al final de la mañana- y Garmin
    # corrige hacia atrás el sueño y el body battery de ayer.
    #
    # `metrics` a secas sigue siendo lo que se ARCHIVA más abajo. Guardar la
    # lista fusionada reescribiría noventa filas cada mañana y les recalcularía
    # el `fetch_status`, que es justo la marca que distingue «no se pudo leer»
    # de «se leyó y no había».
    wellness = repo.fusionar_metricas(
        repo.metricas_guardadas(
            session,
            desde=day - timedelta(days=dias_de_wellness_en_memoria(cfg)),
            hasta=day,
        ),
        metrics,
    )

    signals = build_signals(
        cfg,
        day,
        metrics=wellness,
        rides=rides,
        checkin=checkin,
        sessions=repo.sesiones_ejecutadas(
            session, cfg, desde=day - timedelta(days=14), hasta=day
        ),
        checkin_history=repo.historial_checkins(
            session, desde=day - timedelta(days=90), hasta=day - timedelta(days=1)
        ),
    )

    # El estado sale de la base de datos, no de cero. Es la diferencia entre un
    # sistema que recuerda y uno que cada mañana vuelve a nacer.
    state = repo.load_state(session, program_start=cfg.program_start, rotation_order=cfg.rotation_order())
    decision = decide(cfg, day, signals, state, source=source)

    # Esto viaja con la decisión hasta el renderizador, y NO hasta la base:
    # `save_decision` escribe columna a columna y no hay ninguna para la
    # anulación. Aquí decía lo contrario -"para que el JSON del histórico la
    # lleve"- y era falso.
    #
    # Tampoco hace falta una columna nueva. Dentro de tres meses, "el martes el
    # semáforo cambió de verde a ámbar a las nueve" se reconstruye con lo que ya
    # se guarda: dos filas del mismo día, la de las 06:23 con `is_current=False`
    # y fuente `checkin`, la de las 09:00 vigente y con fuente `recompute`. Y
    # QUÉ dato llegó sale de comparar los dos `skipped_rules_json`: lo que
    # faltaba en la primera y ya no falta en la segunda es exactamente lo que el
    # reloj subió entre una hora y otra.
    decision.anulacion = anulacion

    # Lo que la reconciliación de anoche movió de la carga, para contarlo AHORA.
    # Se cuelga antes de guardar la decisión para que quede también en el
    # histórico: el JSON de hoy tiene que poder explicar por qué el hip thrust
    # sale a 62,5 y no a lo de ayer, y dentro de tres meses no habrá otro sitio
    # donde mirarlo. Se sellan como contadas más abajo, y solo si hay mensaje.
    decision.load_adoptions = repo.adopciones_sin_contar(session)

    # Y lo que se entrenó sin que el plan lo previera, por lo mismo: es lo único
    # que contesta "¿se ha enterado el sistema de que ayer hice un HIIT?". Se
    # sella igual que las adopciones, más abajo y solo si hay mensaje.
    decision.entrenos_sueltos = repo.entrenos_sin_contar(session)

    # La lectura de segundo orden. Se le pasa el histórico hasta AYER más la
    # decisión de hoy que acaba de salir del motor, todavía en memoria: a estas
    # alturas no está escrita, y en el recálculo de las 09:40 la que sí está
    # escrita es la de las 07:00, que es justo la que hoy ya no vale. Leerla de
    # la base daría una tendencia calculada sobre un semáforo superado.
    decision.tendencia = evaluar_tendencia(
        cfg,
        day,
        repo.serie_decisiones(session, hasta=day - timedelta(days=1))
        + [DecisionDia(day, decision.light, decision.trigger_rule)],
        sleep_score={m.date: m.sleep_score for m in wellness},
        sleep_min={m.date: m.sleep_min for m in wellness},
    )

    # Qué rutina del ciclo lleva más de una vuelta sin hacerse. No cambia la
    # decisión: se cuelga para que el mensaje pueda nombrarla y para que quede en
    # el histórico. Ver `app/engine/rotacion.py`, que explica por qué basta con
    # contar y por qué no hace falta ni puntero ni cola.
    #
    # Se acota a `day` aunque en producción no cambie nada -a las siete de la
    # mañana lo último que hay en `workout_log` es de anoche- porque en un replay
    # sí cambia: sin el tope, volver a decidir una mañana de marzo contaría las
    # sesiones de abril y diría que no había nada parado cuando sí lo había.
    orden = orden_de_rotacion(cfg)
    decision.pendientes = rutinas_pendientes(
        orden, repo.sesiones_del_ciclo(session, orden, hasta=day)
    )

    res = DailyResult(day=day, decision=decision)
    # A PARTIR DE AQUÍ SE TOCAN COSAS DE FUERA, así que a partir de aquí una
    # excepción tiene que llevarse consigo hasta dónde se llegó.
    #
    # Hevy y Telegram no entran en la transacción: el `rollback` de quien llame
    # deshace la decisión y no deshace ni la rutina escrita ni el mensaje
    # entregado. Dejar subir la excepción pelada obliga a la pantalla del fallo
    # a adivinar, y adivinando dijo "no se ha enviado ningún mensaje" el día que
    # el mensaje ya estaba en el móvil. Ver `DecisionInterrumpida`.
    try:
        return _ejecutar_el_dia(
            session, cfg, day, decision, state, signals, metrics, res,
            hevy_client=hevy_client, telegram_client=telegram_client,
            client_errors=client_errors, dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        raise DecisionInterrumpida(exc, res) from exc


def _ejecutar_el_dia(
    session: Session,
    cfg: Any,
    day: date,
    decision: Any,
    state: Any,
    signals: Any,
    metrics: list,
    res: DailyResult,
    *,
    hevy_client: Any,
    telegram_client: Any,
    client_errors: dict[str, str] | None,
    dry_run: bool,
) -> DailyResult:
    """La mitad que escribe: base de datos, Hevy y Telegram, en ese orden.

    Está separada de `run_daily` solo para que el `try` de arriba tenga un
    cuerpo con nombre en vez de cuarenta líneas indentadas. La frontera no es
    caprichosa: por encima se decide -y decidir no toca nada de fuera-, por
    debajo se ejecuta.
    """
    fila = repo.save_decision(session, decision)

    # El recordatorio de recalibración, DESPUÉS de guardar. `save_decision`
    # hace `flush`, así que el día de hoy ya cuenta en la consulta de abajo y no
    # hay que sumarlo a mano: la cuenta que se enseña es la misma que se leería
    # después desde fuera, y no una versión de la cuenta que solo existe aquí.
    #
    # Cuando el día que se decide es ANTERIOR a la última revisión de umbrales
    # -un replay, o volver a decidir una mañana vieja- no hay nada acumulado
    # todavía y el rango saldría invertido, que es algo que `dias_con_decision`
    # rechaza a gritos a propósito. Ese caso se escribe AQUÍ, que es el único
    # sitio donde consta que es legítimo, en vez de ablandar el contador para
    # todos los que lo llamen.
    dias = (
        repo.dias_con_decision(session, desde=cfg.recalibrado_el, hasta=day)
        if day >= cfg.recalibrado_el
        else 0
    )
    decision.recalibracion = evaluar_recalibracion(cfg, dias)

    _guardar_lo_leido(session, signals, metrics, res)

    motivos = client_errors or {}
    _escribir_hevy(
        session, cfg, decision, fila, res, hevy_client, dry_run,
        motivo_sin_cliente=motivos.get("hevy"),
    )
    _mandar_telegram(
        session, cfg, decision, res, telegram_client, dry_run,
        motivo_sin_cliente=motivos.get("telegram"),
    )

    # Solo se dan por contadas si el mensaje SALIÓ. En un ensayo en seco también:
    # ahí el texto se imprime y queda en `notifications`, que es todo el "salir"
    # que hay, y no marcarlas haría que cada mañana de la fase de pruebas
    # repitiera la lista entera desde el primer día.
    #
    # Con "error" o "skipped" NO se marcan, y esa es la parte que importa: un
    # Telegram caído no tumba la mañana -se traga la excepción a propósito-, así
    # que sellarlas aquí las daría por explicadas por un mensaje que nadie leyó.
    # Sin marcar, vuelven a salir mañana.
    if res.telegram_status in {"sent", "dry_run"}:
        repo.marcar_adopciones_contadas(
            session, [a.get("id") for a in decision.load_adoptions]
        )
        repo.marcar_entrenos_contados(
            session, [e.get("id") for e in decision.entrenos_sueltos]
        )

    # El estado se guarda al final y SIN `executed`: a estas horas la sesión no
    # se ha hecho todavía. Lo que avanza aquí son las reglas activas, el
    # aplazamiento y la descarga; las rachas las mueve `run_reconcile`.
    from app.engine.decision import advance_state

    repo.save_state(session, advance_state(state, decision), day=day)
    return res


def _guardar_lo_leido(
    session: Session, signals: Any, metrics: list, res: DailyResult
) -> None:
    """Deja en la base lo que se leyó de Garmin, no solo lo que se decidió con ello.

    Va aquí y no en el trabajo de las 06:30 porque aquí es donde existen las dos
    cosas a la vez: las métricas crudas y la clasificación de cada salida, que
    depende del `config.yaml` de hoy y no se puede reconstruir después.

    NO TUMBA LA MAÑANA. Si esto falla, la decisión ya está tomada y guardada, y
    el mensaje tiene que salir igual: perder un día de histórico es malo, pero
    quedarse sin plan porque no se pudo archivar una fila es peor. Se anota en
    `problemas`, que es lo que el usuario acaba viendo, en vez de en un log que
    nadie lee.
    """
    try:
        # Aquí se pasaban también las cargas acumuladas de `signals.history`
        # para guardarlas en `daily_metrics.load_3d/7d`. Esas dos columnas ya no
        # existen: las escribía solo esta línea -el backfill no-, así que el
        # histórico tenía seis meses a NULL y un escalón el día del arranque, y
        # además no las leía nadie. La carga que usan las reglas se recalcula
        # cada mañana sumando las actividades, que es de donde salía este número.
        dias = repo.upsert_daily_metrics(session, metrics)
        salidas = repo.upsert_activities(session, signals.rides)
        log.debug("archivados %d día(s) de wellness y %d salida(s)", dias, salidas)
    except Exception as exc:  # noqa: BLE001
        res.problemas.append(
            f"no se pudo archivar lo leído de Garmin ({exc}): la decisión de hoy "
            f"está guardada, pero los datos con los que se tomó no. Un día que no "
            f"se guarda no se recupera."
        )
        log.exception("fallo archivando métricas y actividades")


def _escribir_hevy(
    session: Session,
    cfg: Any,
    decision: Any,
    fila: Any,
    res: DailyResult,
    client: Any,
    dry_run: bool,
    motivo_sin_cliente: str | None = None,
) -> None:
    from app.integrations.hevy import build_routine_payload

    # EL SEGUNDO DESTINO SE ESCRIBE SIEMPRE, y por eso va antes de la salida
    # temprana de abajo y en un `try` que no puede tumbar nada.
    #
    # Va ANTES porque el día que la sesión de fuerza no toca Hevy -recuperación,
    # o una decisión anulada- hay que deshacer igualmente el bloque HIIT que se
    # escribió esta mañana. Si esto fuera después del `return`, ese día el
    # bloque se quedaría puesto: Telegram diría «Recuperación» y en la app
    # habría un HIIT esperando, que con una hernia L4-L5 es el error en la
    # única dirección que no se puede permitir.
    _escribir_hiit(session, cfg, decision, fila, res, client, dry_run)

    s = decision.session
    if not (s.write_to_hevy and s.routine_key and s.hevy_routine_id):
        _deshacer_lo_de_hoy(session, cfg, decision, fila, res, client, dry_run)
        return

    payload = build_routine_payload(s, cfg)
    if client is None:
        # "error" y NO "skipped", que es lo que ponía. La diferencia decide si
        # el usuario se entera: el aviso de arriba del mensaje ("la rutina NO se
        # ha escrito en Hevy") se pone solo cuando el estado es "error", así que
        # marcarlo como salto dejaba el mensaje describiendo con todo detalle
        # una sesión que en Hevy no estaba. Se abre la app, se ve la rutina de
        # la semana pasada y se entrena esa.
        #
        # Un salto legítimo sí existe: que hoy la sesión no toque Hevy (arriba).
        # Ese es el único que se calla, porque no hay nada que escribir. El
        # interruptor `integrations.hevy.write_enabled` se resuelve dentro de
        # `write_routine` y NO es un salto silencioso: ver abajo, "read_only".
        # Llegar hasta aquí sin cliente es otra cosa: significa
        # que la sesión SÍ quería escribirse y el cliente no se pudo construir
        # -normalmente HEVY_API_KEY ausente o mal escrita en el .env-. Eso es
        # una avería de configuración, no una decisión.
        #
        # `api._clientes` se traga esa excepción y la deja en un WARNING del
        # log, que en Umbrel no lee nadie a las nueve de la mañana.
        res.hevy_status = "error"
        # El motivo real si viaja, y la sospecha más probable si no. Adivinar
        # cuando se sabe manda a mirar donde no es.
        causa = motivo_sin_cliente or "revisa HEVY_API_KEY en el .env"
        res.hevy_reason = (
            f"no hay cliente de Hevy ({causa}): la rutina de hoy sigue siendo "
            f"la anterior"
        )
        res.problemas.append(f"Hevy: {res.hevy_reason}")
        _anotar_hevy(session, decision, fila, res, payload)
        return

    try:
        r = client.write_routine(s.hevy_routine_id, payload, dry_run=dry_run)
        res.hevy_reason = r.reason
        res.hevy_http = r.http_status
        if r.written:
            res.hevy_status = "ok"
        elif dry_run:
            res.hevy_status = "dry_run"
        elif r.error:
            res.hevy_status = "error"
            # EL MOTIVO ES `r.error`, NO `r.reason`, y esa línea de más es el
            # arreglo de un fallo caro. `write_routine` deja `reason` vacío
            # cuando algo va mal -el texto está en `error`-, así que la fila de
            # `hevy_writes` se guardaba con estado "error" y explicación "".
            # El 2026-09-14 la escritura de las 09:00 falló y lo único que quedó
            # en la base fue la palabra «error»: por qué se cayó vivía en el log
            # de un contenedor que se reconstruyó esa tarde, y se fue con él.
            # Una auditoría que registra que algo falló sin registrar qué no es
            # una auditoría, es un contador.
            res.hevy_reason = r.error
            res.problemas.append(f"Hevy: {r.error}")
        else:
            # Ni escrita, ni ensayo, ni avería. Solo queda una forma de llegar
            # aquí: `integrations.hevy.write_enabled` en false, el modo de solo
            # lectura. Es deliberado -lo pone el usuario a mano- así que NO es un
            # error y no se inventa uno.
            #
            # Pero tampoco es un salto que se pueda callar. Antes se marcaba
            # "skipped" a secas, y como el aviso de cabecera del mensaje solo
            # miraba "error", salía un Telegram impecable describiendo paso a
            # paso una rutina que en Hevy no estaba: se abre la app, se ve la de
            # la semana pasada y se entrena esa, creyendo que es la de hoy. El
            # sistema decide solo; el modo seguro no puede además ser mudo.
            res.hevy_status = "read_only"
            res.problemas.append(f"Hevy: {r.reason}")
    except Exception as exc:  # noqa: BLE001
        # Que Hevy falle no puede tumbar la mañana entera: el mensaje todavía
        # tiene que salir, y tiene que decir esto.
        res.hevy_status = "error"
        res.hevy_reason = str(exc)
        res.problemas.append(f"Hevy: {exc}")
        log.exception("fallo escribiendo la rutina en Hevy")

    _anotar_hevy(session, decision, fila, res, payload)


def _escribir_hiit(
    session: Session,
    cfg: Any,
    decision: Any,
    fila: Any,
    res: DailyResult,
    client: Any,
    dry_run: bool,
) -> None:
    """El segundo destino: la rutina de Hevy donde vive el bloque HIIT.

    POR QUÉ HAY DOS Y NO UNA
    ------------------------
    El motor pegaba los ejercicios del bloque a la rutina de fuerza y escribía
    una sola rutina. Pero en Hevy los bloques HIIT existen como rutinas propias
    -el `config.yaml` les da su `hevy_routine_id` desde siempre- y así es como
    están ejecutados en la cuenta: el 8 y el 9 de septiembre constan como
    entrenamientos separados. O sea que la rutina HIIT de la app nunca la
    escribía nadie: se entrenaba lo que hubiera quedado ahí de la última vez,
    con la carga de fábrica del YAML, mientras la progresión del único
    ejercicio que progresa (`plancha_frontal`) se guardaba en la base y no
    llegaba jamás a la pantalla del gimnasio.

    Y LA OTRA MITAD: DESHACERLO
    ---------------------------
    Si hoy no toca HIIT pero esta mañana se escribió uno -el respaldo de las
    09:00 decidió en verde, el check-in llegó a las 10:30 y salió ámbar- la
    rutina HIIT se queda puesta. Es el mismo fallo que `_deshacer_lo_de_hoy`
    arregla para la fuerza, y aquí es peor: el HIIT solo se prescribe en verde,
    así que un HIIT que sobrevive a un cambio de decisión está SIEMPRE en un
    día que el sistema ha declarado no verde.

    NO TUMBA LA MAÑANA. Cualquier fallo aquí se anota en `problemas` y sigue: la
    decisión ya está tomada y el mensaje tiene que salir igual.
    """
    from app.integrations.hevy import build_routine_payload

    h = getattr(decision.session, "hiit", None)

    if h is None or not (h.write_to_hevy and h.routine_key and h.hevy_routine_id):
        _deshacer_el_hiit_de_hoy(session, cfg, decision, fila, res, client, dry_run)
        return

    try:
        payload = build_routine_payload(h, cfg)
    except Exception as exc:  # noqa: BLE001
        res.hevy_hiit_status = "error"
        res.hevy_hiit_reason = f"no se pudo construir el bloque HIIT: {exc}"
        res.problemas.append(f"Hevy (HIIT): {res.hevy_hiit_reason}")
        log.exception("fallo construyendo el payload del bloque HIIT")
        return

    if client is None:
        res.hevy_hiit_status = "error"
        res.hevy_hiit_reason = (
            "no hay cliente de Hevy: la rutina del HIIT sigue siendo la anterior"
        )
        res.problemas.append(f"Hevy (HIIT): {res.hevy_hiit_reason}")
        _anotar_hevy(session, decision, fila, res, payload,
                     routine_key=h.routine_key, hevy_routine_id=h.hevy_routine_id,
                     estado=res.hevy_hiit_status, motivo=res.hevy_hiit_reason)
        return

    try:
        r = client.write_routine(h.hevy_routine_id, payload, dry_run=dry_run)
        res.hevy_hiit_reason = r.reason
        if r.written:
            res.hevy_hiit_status = "ok"
        elif dry_run:
            res.hevy_hiit_status = "dry_run"
        elif r.error:
            res.hevy_hiit_status = "error"
            res.hevy_hiit_reason = r.error
            res.problemas.append(f"Hevy (HIIT): {r.error}")
        else:
            res.hevy_hiit_status = "read_only"
            res.problemas.append(f"Hevy (HIIT): {r.reason}")
    except Exception as exc:  # noqa: BLE001
        res.hevy_hiit_status = "error"
        res.hevy_hiit_reason = str(exc)
        res.problemas.append(f"Hevy (HIIT): {exc}")
        log.exception("fallo escribiendo la rutina HIIT en Hevy")

    _anotar_hevy(session, decision, fila, res, payload,
                 routine_key=h.routine_key, hevy_routine_id=h.hevy_routine_id,
                 estado=res.hevy_hiit_status, motivo=res.hevy_hiit_reason)


def _deshacer_el_hiit_de_hoy(
    session: Session,
    cfg: Any,
    decision: Any,
    fila: Any,
    res: DailyResult,
    client: Any,
    dry_run: bool,
) -> None:
    """Hoy no toca HIIT. Si esta mañana se escribió uno, se deshace.

    Hermano de `_deshacer_lo_de_hoy` y con el mismo motivo, pero con una
    diferencia que vale la pena escribir: el HIIT solo se prescribe en VERDE
    (`hiit.only_on_green`), así que llegar aquí con una escritura viva significa
    que la decisión de hoy ha dejado de ser verde. El bloque que se quedaría
    puesto es intensidad máxima en un día que el sistema acaba de declarar no
    apto para intensidad.
    """
    previa = _escritura_viva_de_hoy(
        session, decision.day, claves=claves_hiit(cfg), dentro=True
    )
    if previa is None or not previa.hevy_routine_id:
        res.hevy_hiit_status = "skipped"
        res.hevy_hiit_reason = "hoy no toca HIIT y no se ha escrito ninguno"
        return

    puesto = _titulo_de(cfg, previa.routine_key)
    rid = previa.hevy_routine_id
    situacion = (
        f"esta mañana se escribió «{puesto}» en Hevy con una decisión que ya no "
        f"vale, y hoy el plan no lleva HIIT"
    )

    if dry_run:
        res.hevy_hiit_status = "dry_run"
        res.hevy_hiit_reason = f"ensayo: {situacion}. Se habría deshecho"
        _anotar_hevy(session, decision, fila, res, None,
                     routine_key=previa.routine_key, hevy_routine_id=rid,
                     estado=res.hevy_hiit_status, motivo=res.hevy_hiit_reason)
        return

    if client is None or not hasattr(client, "revert_to_day_start"):
        res.hevy_hiit_status = "stale"
        res.hevy_hiit_reason = (
            f"{situacion}. No se ha podido deshacer porque no hay cliente de "
            f"Hevy. Abre Hevy y NO hagas «{puesto}»"
        )
        res.problemas.append(f"Hevy (HIIT): {res.hevy_hiit_reason}")
        _anotar_hevy(session, decision, fila, res, None,
                     routine_key=previa.routine_key, hevy_routine_id=rid,
                     estado=res.hevy_hiit_status, motivo=res.hevy_hiit_reason)
        return

    try:
        r = client.revert_to_day_start(rid, decision.day)
    except Exception as exc:  # noqa: BLE001
        res.hevy_hiit_status = "stale"
        res.hevy_hiit_reason = (
            f"{situacion}. No se ha podido deshacer ({exc}). Abre Hevy y NO "
            f"hagas «{puesto}»"
        )
        res.problemas.append(f"Hevy (HIIT): {res.hevy_hiit_reason}")
        log.exception("fallo deshaciendo el HIIT de %s", decision.day)
        _anotar_hevy(session, decision, fila, res, None,
                     routine_key=previa.routine_key, hevy_routine_id=rid,
                     estado=res.hevy_hiit_status, motivo=res.hevy_hiit_reason)
        return

    res.hevy_hiit_status = "reverted"
    res.hevy_hiit_reason = (
        f"{situacion}, así que esa rutina se ha devuelto a como estaba antes "
        f"({r.reason})"
    )
    _anotar_hevy(session, decision, fila, res,
                 (r.backup.payload if r.backup else None),
                 routine_key=previa.routine_key, hevy_routine_id=rid,
                 estado=res.hevy_hiit_status, motivo=res.hevy_hiit_reason)


def _escritura_viva_de_hoy(
    session: Session, day: date, *, claves: set[str] | None = None, dentro: bool = True
) -> Any | None:
    """La última escritura del día que SÍ llegó a Hevy, si la hay.

    SOLO CUENTA `ok`, y la lista de lo que no cuenta importa tanto como la de lo
    que sí. `dry_run` no tocó nada. `read_only` tampoco -el interruptor la paró
    antes del PUT-. `skipped` no lo intentó. Y `error` es el caso incómodo:
    puede que llegara a medias, pero no se sabe, y deshacer a ciegas una
    escritura que quizá no ocurrió cambiaría la rutina por una TERCERA cosa. Ese
    caso ya tiene su propio aviso -la marca de escritura a medias que
    `/api/health` publica como `pending_write`-, y es mejor sitio para él que
    una reversión adivinada.

    Se mira la más reciente porque es la que describe lo que hay ahora en Hevy.
    Cuál fue la primera es otra pregunta, y la contesta la copia de seguridad.

    `claves` Y `dentro` EXISTEN PORQUE AHORA HAY DOS RUTINAS POR DÍA.
    ---------------------------------------------------------------
    Desde que el bloque HIIT se escribe en su propia rutina, «la última
    escritura del día» dejó de identificar una sola cosa: una mañana con HIIT
    deja dos filas `ok`, y la última es la del HIIT. Sin filtro, el camino que
    deshace la sesión de fuerza cuando la decisión cambia habría cogido la fila
    del HIIT y habría revertido la rutina equivocada, dejando la de fuerza
    puesta -que es justo lo que ese camino existe para impedir-.

    Se pasa el conjunto de claves HIIT y de qué lado se quiere, en vez de un
    booleano `es_hiit`: el conjunto sale de `hiit.blocks` del config y así no
    hay una segunda lista de bloques que mantener.
    """
    q = select(HevyWrite).where(HevyWrite.date == day, HevyWrite.status == "ok")
    for fila in session.scalars(q.order_by(HevyWrite.id.desc())):
        if claves is None:
            return fila
        if (str(fila.routine_key or "") in claves) is dentro:
            return fila
    return None


def _titulo_de(cfg: Any, routine_key: str | None) -> str:
    raw = cfg.raw if hasattr(cfg, "raw") else (cfg or {})
    entrada = ((raw.get("routines") or {}).get(routine_key or "") or {})
    return str(entrada.get("title") or routine_key or "una rutina")


def _deshacer_lo_de_hoy(
    session: Session,
    cfg: Any,
    decision: Any,
    fila: Any,
    res: DailyResult,
    client: Any,
    dry_run: bool,
) -> None:
    """Hoy la sesión no toca Hevy. Si algo se escribió antes, se deshace.

    EL SALTO ERA LEGÍTIMO MIRADO SOLO Y FALSO MIRADO EN SECUENCIA
    ------------------------------------------------------------
    Aquí se ponía `skipped` con el motivo «hoy la sesión no toca Hevy» y se
    volvía. Visto aisladamente es verdad: una sesión de recuperación o un día de
    descanso no tienen rutina que escribir. Visto como secuencia es mentira, y
    la secuencia ocurre:

        09:00  no ha llegado el check-in. El trabajo de respaldo decide con
               Garmin, sale verde y ESCRIBE `Día 1` en Hevy.
        10:30  llega el check-in. Sale rojo. La sesión de hoy es recuperación,
               que no toca Hevy. Se salta.

    Resultado: un Telegram que dice «Recuperación» y una app que enseña el
    `Día 1` entero. Nada avisaba de la discrepancia, y con una hernia L4-L5 el
    error va en la única dirección que no se puede permitir: el día que el
    sistema ha decidido que no se entrene fuerte es el día que la app tiene
    puesta la sesión fuerte.

    Lo que queda en Hevy tiene que corresponder a la ÚLTIMA decisión, no a la
    primera. Como la última no escribe nada, la única forma de cumplirlo es
    devolver la rutina a como estaba antes de la primera escritura de hoy. Eso
    deja el día idéntico a como habría quedado si el respaldo no hubiera
    corrido, que es exactamente lo que se quiere: el respaldo no puede empeorar
    un día por haber actuado.

    Y se registra como escritura. Una reversión es un toque a Hevy como
    cualquier otro, y si no dejara fila el histórico diría que hoy se puso
    `Día 1` y ahí se acabó la historia.
    """
    # La de FUERZA: la fila del HIIT de esta mañana no cuenta aquí. La deshace
    # `_escribir_hiit`, que es quien sabe con qué comparar.
    previa = _escritura_viva_de_hoy(
        session, decision.day, claves=claves_hiit(cfg), dentro=False
    )
    if previa is None or not previa.hevy_routine_id:
        # El salto de verdad: hoy no se ha escrito nada, así que no hay nada que
        # deshacer. Este es el único que se calla, y ahora se calla por haber
        # comprobado que puede, no por no haber mirado.
        res.hevy_status = "skipped"
        res.hevy_reason = "hoy la sesión no toca Hevy"
        return

    puesta = _titulo_de(cfg, previa.routine_key)
    hoy = decision.session.title or "otra cosa"
    rid = previa.hevy_routine_id

    # La frase que hay que poder leer en el móvil y saber qué hacer. Se arma
    # aquí una sola vez porque la usan los tres caminos de abajo, y decir lo
    # mismo de tres formas distintas es como se acaba diciendo tres cosas.
    situacion = (
        f"esta mañana se escribió «{puesta}» en Hevy con una decisión que ya no "
        f"vale, y hoy toca «{hoy}»"
    )

    if dry_run:
        res.hevy_status = "dry_run"
        res.hevy_reason = f"ensayo: {situacion}. Se habría deshecho"
        _anotar_hevy(session, decision, fila, res, None,
                     routine_key=previa.routine_key, hevy_routine_id=rid)
        return

    # Sin cliente no se puede deshacer, y eso NO es un salto. Es el caso peor
    # con el añadido de que el sistema lo sabe y no puede arreglarlo.
    if client is None or not hasattr(client, "revert_to_day_start"):
        res.hevy_status = "stale"
        res.hevy_reason = (
            f"{situacion}. No se ha podido deshacer porque no hay cliente de "
            f"Hevy. Abre Hevy y NO hagas «{puesta}»"
        )
        res.problemas.append(f"Hevy: {res.hevy_reason}")
        _anotar_hevy(session, decision, fila, res, None,
                     routine_key=previa.routine_key, hevy_routine_id=rid)
        return

    try:
        r = client.revert_to_day_start(rid, decision.day)
    except Exception as exc:  # noqa: BLE001
        # Casi siempre: no hay copia del día, o la copia no se puede leer. Da
        # igual cuál: el hecho que el usuario necesita es el mismo, y lo que
        # tiene que hacer también.
        res.hevy_status = "stale"
        res.hevy_reason = (
            f"{situacion}. No se ha podido deshacer ({exc}). Abre Hevy y NO "
            f"hagas «{puesta}»: hoy toca «{hoy}»"
        )
        res.problemas.append(f"Hevy: {res.hevy_reason}")
        log.exception("fallo deshaciendo la escritura de %s", decision.day)
        _anotar_hevy(session, decision, fila, res, None,
                     routine_key=previa.routine_key, hevy_routine_id=rid)
        return

    res.hevy_status = "reverted"
    res.hevy_reason = (
        f"{situacion}, así que Hevy se ha devuelto a como estaba antes "
        f"({r.reason})"
    )
    _anotar_hevy(session, decision, fila, res,
                 (r.backup.payload if r.backup else None),
                 routine_key=previa.routine_key, hevy_routine_id=rid)


def _anotar_hevy(
    session: Session,
    decision: Any,
    fila: Any,
    res: DailyResult,
    payload: dict | None,
    *,
    routine_key: str | None = None,
    hevy_routine_id: str | None = None,
    estado: str | None = None,
    motivo: str | None = None,
) -> None:
    """Una fila por toque a Hevy, incluido el toque que deshace otro.

    `routine_key` y `hevy_routine_id` se pueden forzar porque una reversión NO
    habla de la sesión de hoy: habla de la rutina que se escribió esta mañana,
    que es otra. Sacarlos de `decision.session` como hace el camino normal
    dejaría la fila diciendo que se revirtió «Recuperación» -que no se tocó
    nunca- en vez de `Día 1`.

    `estado` Y `motivo` SE FUERZAN POR EL MISMO MOTIVO, UN ESCALÓN MÁS ARRIBA.
    Una mañana con HIIT toca dos rutinas y las dos pueden ir distinto. El
    resultado del bloque vive en `res.hevy_hiit_status`, no en `res.hevy_status`
    -que es el de la fuerza-, así que sin estos dos la fila del HIIT se
    guardaría con el estado de la OTRA escritura: un 400 en el bloque quedaría
    registrado como `ok` porque la fuerza sí se escribió, y el histórico diría
    que aquel día todo fue bien.

    `http_status` no se fuerza y se deja fuera a propósito: hoy solo lo rellena
    el camino de la fuerza. Ponerle el del otro PUT sería peor que dejarlo a
    nulo, que al menos significa «no se anotó».
    """
    st = estado if estado is not None else res.hevy_status
    why = motivo if motivo is not None else res.hevy_reason
    session.add(
        HevyWrite(
            decision_id=getattr(fila, "id", None),
            date=decision.day,
            routine_key=(routine_key if routine_key is not None
                         else decision.session.routine_key),
            hevy_routine_id=(hevy_routine_id if hevy_routine_id is not None
                             else decision.session.hevy_routine_id),
            status=st,
            error=why if st in ("error", "stale") else None,
            # Estaba declarada, documentada en el modelo, y se guardaba NULL
            # siempre porque nadie la pasaba. Sin ella la fila no distingue «Hevy
            # contestó 400» de «no se pudo ni preguntar», que es justo la
            # diferencia que decide si hay que mirar la rutina o no.
            http_status=res.hevy_http if estado is None else None,
            # El motivo va SIEMPRE, no solo cuando algo falla. Es lo que hace
            # que la secuencia del día se pueda leer entera meses después.
            reason=why or None,
            payload_json=repo._json(payload) if payload is not None else None,
        )
    )


def _mandar_telegram(
    session: Session,
    cfg: Any,
    decision: Any,
    res: DailyResult,
    client: Any,
    dry_run: bool,
    motivo_sin_cliente: str | None = None,
) -> None:
    texto = render_telegram(decision, cfg)

    # Si la rutina no llegó a Hevy, el mensaje NO puede describirla como si
    # estuviera. Se avisa arriba del todo, donde se lee antes que el plan.
    #
    # Dos estados llegan aquí y el aviso es el mismo porque el hecho es el
    # mismo: en Hevy hay otra cosa. Que la causa sea una avería ("error") o el
    # interruptor de solo lectura ("read_only") cambia qué hacer después, y eso
    # lo cuenta `hevy_reason`, que va en la segunda línea.
    if res.hevy_status == "stale":
        # El peor de los estados y el que menos se parece a los demás: aquí NO
        # falta nada en Hevy, sobra. Hay una rutina puesta que el sistema ya ha
        # decidido que hoy no toca, y el aviso de abajo -«tendrás que montarlo a
        # mano»- diría justo lo contrario de lo que hay que hacer.
        texto = (
            "⚠️ <b>En Hevy ha quedado una rutina que hoy NO toca</b>\n"
            f"{escapar_html(res.hevy_reason)}\n\n"
        ) + texto
    elif res.hevy_status == "reverted":
        # Salió bien, y aun así se cuenta. Que la rutina de Hevy cambie sola
        # entre las nueve y las once es de las cosas que hay que enterarse de
        # que han pasado, no descubrir abriendo la app.
        texto = (
            "↩️ <b>Hevy se ha devuelto a como estaba</b>\n"
            f"{escapar_html(res.hevy_reason)}\n\n"
        ) + texto
    elif res.hevy_status in ("error", "read_only"):
        # `hevy_reason` es `str(excepción)` cuando el estado es "error", o sea
        # texto que viene de httpx o de la respuesta de Hevy, o sea texto con
        # ángulos dentro más a menudo de lo que parece. Sin escapar, Telegram
        # devuelve 400 «can't parse entities» y este aviso -que es el que dice
        # que la rutina de hoy NO está en Hevy- se pierde entero. Fallo de Hevy
        # y silencio de Telegram a la vez, y la app enseñando la rutina vieja.
        texto = (
            "⚠️ <b>La rutina NO se ha escrito en Hevy</b>\n"
            f"{escapar_html(res.hevy_reason)}\n"
            "Lo de abajo es lo que tocaba hoy; tendrás que montarlo a mano.\n\n"
        ) + texto

    # El registro se escribe SIEMPRE, también cuando no hay a quién avisar.
    # Salir antes por aquí dejaba sin fila los días en los que la decisión se
    # tomó y no se contó a nadie, que son justo los que hay que poder encontrar
    # después: en el histórico no se distinguirían de un día en el que el
    # mensaje salió bien.
    if client is None:
        res.telegram_status = "skipped"
        res.telegram_reason = motivo_sin_cliente or "sin cliente de Telegram configurado"
        res.problemas.append(
            f"no hay cliente de Telegram ({res.telegram_reason}): la decisión "
            f"de hoy no se ha contado a nadie"
        )
    else:
        r = None
        try:
            r = client.send(texto, dry_run=dry_run)
            if r.sent:
                res.telegram_status = "sent"
                res.telegram_reason = r.reason
            elif dry_run:
                res.telegram_status = "dry_run"
                res.telegram_reason = r.reason
            elif r.error:
                # Aquí se perdía el motivo. `send()` NO lanza cuando la API
                # contesta que no: devuelve `sent=False` con el fallo en
                # `error`, y `reason` vacío. Como esta rama guardaba `r.reason`
                # y llamaba "skipped" a todo, un rechazo de Telegram quedaba
                # registrado igual que un `send_enabled: false` deliberado, y
                # con el motivo tirado a la basura. Los dos casos se leen luego
                # en la misma columna, así que eran indistinguibles: "hoy no se
                # mandó nada, y no consta por qué".
                res.telegram_status = "error"
                res.telegram_reason = r.error
                res.problemas.append(f"Telegram: {r.error}")
                log.error("Telegram no aceptó el mensaje: %s", r.error)
            else:
                res.telegram_status = "skipped"
                res.telegram_reason = r.reason
        except Exception as exc:  # noqa: BLE001
            res.telegram_status = "error"
            res.telegram_reason = str(exc)
            res.problemas.append(f"Telegram: {exc}")
            log.exception("fallo enviando el mensaje")

        # Llegó, pero no como se escribió. No es un error -el mensaje está
        # entero- pero tiene que quedar registrado: significa que algo se
        # interpoló sin escapar y que hay un `escapar_html` que falta.
        if getattr(r, "plain_parts", 0):
            res.problemas.append(
                f"Telegram: {r.plain_parts} parte(s) salieron sin formato "
                f"porque la API rechazó el HTML"
            )

    session.add(
        Notification(
            date=decision.day,
            kind="decision",
            channel="telegram",
            status=res.telegram_status,
            body=texto,
            error=res.telegram_reason if res.telegram_status != "sent" else None,
        )
    )


# ---------------------------------------------------------------------------
# La noche
# ---------------------------------------------------------------------------


def run_reconcile(
    session: Session,
    cfg: Any,
    day: date,
    *,
    workouts: list[dict[str, Any]],
) -> ReconcileResult:
    """Cuenta lo que se hizo de verdad y avanza las rachas.

    ES IDEMPOTENTE, y no por elegancia. Este job se ejecuta cada noche, se
    reintenta si falla y se puede lanzar a mano; si contara dos veces el mismo
    entrenamiento, la racha avanzaría el doble y el ejercicio subiría de peso
    antes de tiempo, sin ningún error visible y en una espalda con hernia. El
    seguro es `workout_log.hevy_workout_id`, que es único: un entrenamiento ya
    registrado no vuelve a contar.
    """
    from app.engine.adoption import adoptar_cargas
    from app.integrations.hevy import (
        _fecha_workout,
        routine_key_de,
        workout_totals,
    )

    res = ReconcileResult(day=day)

    # LO PRIMERO ES REGISTRAR, Y ES DELIBERADO QUE VAYA ANTES QUE NADA.
    #
    # Esto empezaba al revés: buscaba la decisión del día, comprobaba que
    # tocaba fuerza, y solo entonces miraba los entrenamientos. Las tres
    # salidas tempranas -sin decisión, día que no era de fuerza, rutina
    # desconocida- devolvían sin escribir una sola fila en `workout_log`, así
    # que un entrenamiento hecho en sábado, o un HIIT suelto, o cualquier cosa
    # que no estuviera en el plan, DESAPARECÍA. No quedaba ni el registro de
    # que hubo un entrenamiento: ni en las vistas de métricas, ni en el volumen
    # de fuerza, ni en el presupuesto de sesiones intensas.
    #
    # Registrar y reconciliar son dos cosas distintas y ahora están separadas.
    # Que el sistema no supiera prever algo no es motivo para no anotarlo: es
    # justo el motivo para anotarlo.
    del_dia = [w for w in workouts if _fecha_workout(w) == day]
    if not del_dia:
        res.motivo = "no hay ningún entrenamiento registrado ese día"
        return res

    ya = {
        r.hevy_workout_id
        for r in session.scalars(
            select(WorkoutLog).where(WorkoutLog.date == day)
        ).all()
    }
    nuevos = [w for w in del_dia if str(w.get("id")) not in ya]
    res.workouts_ya_contados = len(del_dia) - len(nuevos)
    res.workouts_nuevos = len(nuevos)

    if not nuevos:
        res.motivo = (
            f"los {len(del_dia)} entrenamientos del {day} ya estaban contados; "
            f"no se avanza nada otra vez"
        )
        return res

    # De qué rutina salió cada uno, por `routine_id` y jamás por el título.
    # `None` = no sale de ninguna rutina conocida: un entrenamiento suelto.
    rutinas = {str(w.get("id")): routine_key_de(w, cfg) for w in nuevos}

    fila = repo.current_decision(session, day)
    plan = repo.planned_session(fila) if fila is not None else {}
    rkey = plan.get("routine")
    es_fuerza = bool(rkey) and plan.get("kind") in {"full", "reduced"}

    # El bloque HIIT del día también estaba previsto, aunque se registre aparte.
    # En Hevy las rutinas HIIT existen sueltas y se ejecutan como un
    # entrenamiento propio: así es como están los del 8 y el 9 de septiembre en
    # la cuenta. Sin `bloque_hiit`, hacer exactamente lo que el plan pedía salía
    # cada noche en el mensaje como «visto fuera del plan», que es la clase de
    # aviso que enseña a no leer los avisos.
    hiit = claves_hiit(cfg)
    plan_hiit = plan.get("hiit") or {}
    # `hiit_block` es la clave a secas y `hiit["routine"]` la misma clave dentro
    # de la sesión anidada. Se prefiere la segunda porque es la que trae los
    # ejercicios consigo: si un día hubiera bloque declarado sin sesión, lo que
    # no se puede reconciliar es justo lo que no tiene ejercicios.
    bloque_hiit = plan_hiit.get("routine") or plan.get("hiit_block")

    # LOS DOS PLANES SE MIDEN POR SEPARADO, Y ESE ES EL PUNTO.
    #
    # Antes el HIIT viajaba dentro de `plan["exercises"]`, así que un wall ball
    # que no hice contaba como un ejercicio incumplido DE LA SESIÓN DE FUERZA:
    # rompía la racha de la prensa y frenaba su progresión. Son dos cosas
    # distintas y su cumplimiento se evalúa aparte.
    #
    # El reparto de entrenamientos es por rutina de origen, nunca por el título.
    # A la fuerza van también los sueltos -`routine_key` a None-, que es lo que
    # permite que un rato registrado sin rutina siga contando como media sesión;
    # al HIIT solo lo que sale de una rutina HIIT.
    nuevos_hiit = [w for w in nuevos if rutinas.get(str(w.get("id"))) in hiit]
    nuevos_fuerza = [w for w in nuevos if rutinas.get(str(w.get("id"))) not in hiit]

    executed: dict[str, bool] = {}
    pesos: dict[str, float | None] = {}
    motivos: dict[str, str] = {}
    if es_fuerza:
        executed, pesos, motivos = _cumplimiento_contra(nuevos_fuerza, plan, cfg)
    res.executed = executed
    res.pesos = pesos

    # Solo cuenta el HIIT que salió de la rutina QUE HOY TOCABA. Hacer el Día 2
    # del HIIT un día de Día 1 es un entrenamiento fuera del plan, y medirlo
    # contra el plan de hoy diría que se incumplió un bloque que nadie llegó a
    # abrir.
    del_bloque = [
        w for w in nuevos_hiit if rutinas.get(str(w.get("id"))) == bloque_hiit
    ]
    ex_hiit = plan_hiit.get("exercises") or []
    executed_hiit: dict[str, bool] = {}
    pesos_hiit: dict[str, float | None] = {}
    motivos_hiit: dict[str, str] = {}
    if ex_hiit:
        executed_hiit, pesos_hiit, motivos_hiit = _cumplimiento_contra(
            del_bloque, plan_hiit, cfg
        )

    # El veredicto del día es el veredicto del PLAN DE FUERZA de ese día, así
    # que solo se le pone a las filas que salen de esa rutina. Antes se le
    # estampaba a todas las del día, y eso convertía el HIIT de después en una
    # sesión de fuerza «con todas las series al objetivo» que nadie había
    # evaluado. El HIIT tiene ahora el suyo, calculado contra su propio plan;
    # para lo demás -entreno suelto, día sin plan- queda a NULL, que es lo que
    # esa columna ya significaba: no hay dato.
    veredicto = all(executed.values()) if (es_fuerza and executed) else None
    veredicto_hiit = all(executed_hiit.values()) if executed_hiit else None

    # LOS DOS DATOS QUE HACEN INFORMATIVO EL AVISO DE «HE ENTRENADO OTRA COSA».
    #
    # Declarar el Día 2 y acabar registrando el Día 1 ya se detectaba: el Día 1
    # no era la rutina del plan, así que la fila salía `unplanned` y el motivo
    # decía «es dia_1 y en la rotación tocaba dia_2». Eso cuenta el desajuste y
    # se queda a medias en lo único que tiene consecuencias: los pesos.
    #
    # Lo que hay dentro de una rutina de Hevy es lo que el motor escribió la
    # última vez que ESA rutina se planificó. Nadie la toca entre medias. Así
    # que entrenar el Día 1 una mañana en la que se escribió el Día 2 es
    # entrenar con la carga de hace dos semanas, sin el semáforo ni la
    # progresión de hoy, y sin que nada lo diga uno lo achaca a tener un mal
    # día. `declarada` sirve para no atribuirle a la rotación una elección que
    # fue mía: si el selector dijo «Día 2», la frase correcta es «declaraste» y
    # no «tocaba».
    #
    # Se resuelven aquí, antes del bucle, y no dentro de `_motivo_suelto`:
    # ese helper redacta y no consulta, y mantenerlo sin base de datos es lo que
    # permite leerlo entero para saber qué frases puede llegar a decir.
    #
    # `declarada` NO SE FILTRA CONTRA EL CICLO, y aquí hubo una línea que lo
    # hacía. Parecía prudente -«que un bici no acabe de sujeto de la frase»- y
    # era imposible de disparar: lo único que se hace con `declarada` es
    # compararla con la rutina PLANIFICADA, y a `_motivo_otro_dia` solo se llega
    # con las dos claves dentro del ciclo. Un «bici» nunca puede ser igual a un
    # `dia_2`, así que el filtro no podía cambiar ninguna frase. Una guarda que
    # no puede fallar no protege: ocupa sitio y hace creer que sí.
    ciclo = orden_de_rotacion(cfg)
    ci = repo.get_checkin(session, day)
    declarada = getattr(ci, "chosen_session", None)
    fechas_pesos = {
        k: repo.fecha_de_los_pesos(session, k, hasta=day)
        for k in {r for r in rutinas.values() if r}
    }

    sueltos: list[dict[str, Any]] = []
    for w in nuevos:
        wid = str(w.get("id"))
        rk = rutinas.get(wid)
        es_la_de_fuerza = bool(rk) and es_fuerza and rk == rkey
        es_la_del_hiit = bool(rk) and rk == bloque_hiit
        previsto = es_la_de_fuerza or es_la_del_hiit
        motivo = (
            None
            if previsto
            else _motivo_suelto(
                rk,
                rkey,
                plan,
                es_fuerza,
                fila,
                hiit,
                cfg=cfg,
                ciclo=ciclo,
                declarada=declarada,
                fecha_pesos=fechas_pesos.get(rk),
                dia=day,
            )
        )
        totales = workout_totals(w)
        fuera = WorkoutLog(
            hevy_workout_id=wid,
            date=day,
            routine_key=rk,
            title=w.get("title"),
            all_sets_at_target=(
                veredicto
                if es_la_de_fuerza
                else (veredicto_hiit if es_la_del_hiit else None)
            ),
            unplanned=not previsto,
            motivo_suelto=motivo,
            duration_s=totales.duration_s,
            total_sets=totales.total_sets,
            total_volume_kg=totales.total_volume_kg,
            # El entrenamiento entero, que hasta ahora se leía, se usaba para
            # decidir si la sesión fue limpia y se tiraba. Es el único sitio
            # donde queda el peso y las reps de CADA serie: ni la decisión ni
            # el estado del motor guardan lo que de verdad se levantó, solo si
            # alcanzó el objetivo. Sin esto no se puede responder nunca a
            # "¿cuánto subió el hip thrust en tres meses?" y no se puede
            # arreglar hacia atrás.
            raw_json=repo.crudo_para_guardar(w, etiqueta="entrenamiento Hevy"),
        )
        session.add(fuera)
        if not previsto:
            sueltos.append(
                {
                    "hevy_workout_id": wid,
                    "date": day.isoformat(),
                    "routine": rk,
                    "title": w.get("title"),
                    "duration_s": totales.duration_s,
                    "total_sets": totales.total_sets,
                    "motivo": motivo,
                }
            )
    session.flush()
    res.sueltos = sueltos

    # LO QUE SE APRENDE DE CADA PLAN VA A SU PROPIA CLAVE DE RUTINA.
    #
    # Las dos mitades se aplican al MISMO `state` y se guardan de una vez: son
    # dos rutinas dentro de un estado, no dos estados.
    raw = cfg.raw if hasattr(cfg, "raw") else (cfg or {})
    state = repo.load_state(
        session, program_start=cfg.program_start, rotation_order=cfg.rotation_order()
    )
    adopciones: list[Any] = []

    # EL BLOQUE HIIT, BAJO `hiit_dia_N` Y NO BAJO EL `dia_N` DE LA FUERZA.
    #
    # Antes de separarlos sus ejercicios entraban en `state.compliance` y en
    # `state.current_sets` con la clave de la rutina de FUERZA de ese día. Como
    # el bloque rota por su cuenta, la plancha frontal tenía una racha distinta
    # cada semana según qué día le hubiera tocado, y su carga ejecutada se
    # adoptaba bajo una clave que al día siguiente ya no se consultaba: nunca
    # llegó a progresar.
    #
    # Va ANTES del `return` de «hoy no tocaba fuerza» aunque hoy no se pueda
    # llegar ahí con bloque -el motor solo lo añade en verde, y un verde
    # siempre trae sesión de fuerza-. Ponerlo después sería apoyarse en esa
    # coincidencia: el día que un rojo lleve bloque de movilidad, la progresión
    # del HIIT desaparecería sin que nada lo dijera.
    hubo_hiit = bool(ex_hiit and bloque_hiit)
    if hubo_hiit:
        apply_execution(
            state,
            routine_key=str(bloque_hiit),
            exercises=ex_hiit,
            executed=executed_hiit,
            light=fila.light if fila is not None else None,
            # Las del BLOQUE, sacadas de SU plan. Aquí había un filtro de las
            # claves de la fuerza por pertenencia al bloque, y estaba bien
            # mientras el bloque no progresaba: la lista salía vacía siempre.
            # Ahora que progresa, filtrar sería preguntarle a la progresión de
            # la prensa qué subió en el HIIT, y contestaría que nada -las claves
            # del bloque no están en el plan de la fuerza-, dejando la racha de
            # la plancha intacta después de haber subido.
            progressed=repo.progressed_keys(fila, hiit=True),
        )
        adopciones += adoptar_cargas(
            state,
            routine_key=str(bloque_hiit),
            exercises=ex_hiit,
            pesos_hechos=pesos_hiit,
            limpio=executed_hiit,
            motivos=motivos_hiit,
            set_cfg=(raw.get("set_types") or {}),
            prog_cfg=(raw.get("progression") or {}),
        )

    if not es_fuerza:
        # Con el calendario fijo aquí caían casi todos los días de la semana, y
        # la frase decía "el {day} no tocaba fuerza". Ahora todos los días
        # tienen su rutina del ciclo, así que solo se llega hasta aquí por dos
        # caminos: un día rojo, en el que lo planificado fue un bloque de
        # recuperación, o un día del que no quedó decisión guardada.
        if hubo_hiit:
            repo.save_state(session, state, day=day)
            repo.guardar_adopciones(session, day, adopciones)
            res.adopciones = [a.to_dict() for a in adopciones]
            res.avanzado = True
        motivo_no_fuerza = (
            f"lo planificado fue {plan.get('kind')}"
            if fila is not None
            else "no quedó decisión guardada de ese día"
        )
        res.motivo = (
            f"{len(nuevos)} entrenamiento(s) registrados; el {day} "
            f"{motivo_no_fuerza}: nada que reconciliar contra el plan"
        )
        if hubo_hiit:
            res.motivo += "; sí se ha reconciliado el bloque HIIT"
        return res

    apply_execution(
        state,
        routine_key=str(rkey),
        exercises=plan.get("exercises") or [],
        executed=executed,
        light=fila.light,
        progressed=repo.progressed_keys(fila),
    )

    # La carga que de verdad se levantó pasa a ser la carga vigente, con el
    # freno asimétrico de `app/engine/adoption.py`. Va DESPUÉS de
    # `apply_execution` porque necesita el cumplimiento ya calculado -no se
    # adopta hacia arriba un peso levantado con las series cortas- y ANTES de
    # `save_state`, que es quien lo baja a la tabla.
    #
    # Se le pasa `plan["exercises"]`: el plan del día YA RECORTADO, exactamente
    # lo que se escribió en Hevy. Es contra eso, y no contra el objetivo
    # vigente, contra lo que se mide haberse quedado corto; si no, una semana de
    # descarga bien hecha contaría como tres sesiones flojas y acabaría bajando
    # la carga de verdad.
    adopciones += adoptar_cargas(
        state,
        routine_key=str(rkey),
        exercises=plan.get("exercises") or [],
        pesos_hechos=pesos,
        limpio=executed,
        motivos=motivos,
        set_cfg=(raw.get("set_types") or {}),
        prog_cfg=(raw.get("progression") or {}),
    )
    res.adopciones = [a.to_dict() for a in adopciones]

    repo.save_state(session, state, day=day)
    repo.guardar_adopciones(session, day, adopciones)

    res.avanzado = True
    limpios = sum(1 for v in executed.values() if v)
    res.motivo = f"{limpios}/{len(executed)} ejercicios completos"
    if hubo_hiit:
        limpios_hiit = sum(1 for v in executed_hiit.values() if v)
        res.motivo += f"; HIIT {limpios_hiit}/{len(executed_hiit)}"
    if sueltos:
        res.motivo += f"; {len(sueltos)} entrenamiento(s) fuera del plan"
    return res


def _motivo_suelto(
    rk: str | None,
    rkey: Any,
    plan: dict[str, Any],
    es_fuerza: bool,
    fila: Any,
    hiit: set[str],
    *,
    cfg: Any = None,
    ciclo: Sequence[str] = (),
    declarada: str | None = None,
    fecha_pesos: date | None = None,
    dia: date | None = None,
) -> str:
    """Por qué este entrenamiento no se reconcilia contra ningún plan.

    Se escribe aquí y se guarda con la fila porque el mensaje de la mañana lo
    va a leer tal cual. «Hiciste algo que no esperaba» sin decir el qué obliga
    a abrir la base de datos para entenderlo, y a las nueve de la mañana desde
    el móvil eso equivale a no avisar.

    `hiit` no tiene defecto -lo tuvo mientras se escribía esto- porque un
    conjunto vacío no da error: da una FRASE DISTINTA. Un HIIT hecho por libre
    se explicaría como «es hiit_dia_1 y ese día tocaba dia_1», que sugiere
    haberse equivocado de rutina cuando lo que pasó es que se añadió trabajo.
    El aviso de la mañana es lo único que se lee, así que un defecto que cambia
    lo que dice el aviso es un defecto que cambia lo que yo entiendo que hice.

    LOS CINCO ÚLTIMOS SÍ TIENEN DEFECTO, Y ES EL CONTRARIO DEL DE `hiit`.
    --------------------------------------------------------------------
    Sin ellos se cae en la frase de siempre -«es dia_1 y en la rotación tocaba
    dia_2»-, que es peor pero no es falsa. Con `hiit` el defecto inventaba una
    explicación; aquí solo deja de dar una mejor. La diferencia importa porque
    esta función se llama desde un sitio donde los cinco están disponibles y
    desde los tests, donde escribir cinco argumentos irrelevantes para
    comprobar el motivo de un HIIT sería ruido.
    """
    if fila is None:
        return "no había decisión guardada de ese día"
    if rk is not None and rk in hiit:
        # Que el HIIT se haga por su cuenta no es un error, y decir «es
        # hiit_dia_1 y ese día tocaba dia_1» daría a entender que era una
        # alternativa a la fuerza cuando es un añadido. Lo que hay que contar
        # es que el plan de ese día no lo pedía: cuenta igual para el
        # presupuesto de intensas, pero no lo decidió el motor.
        previsto = plan.get("hiit_block")
        if previsto:
            return f"HIIT por libre: ese día el plan pedía {_titulo_de(cfg, previsto)}"
        return "HIIT por libre: el plan de ese día no llevaba HIIT"
    if not es_fuerza:
        # ESTA FRASE DECÍA «ese día no tocaba fuerza», Y ESE ERA EL FALLO ENTERO
        # ----------------------------------------------------------------------
        # Con el calendario fijo, que no nombraba `dia_3` ningún día, TODAS las
        # sesiones del Día 3 aterrizaban aquí con ese motivo: entrenamientos
        # reales, marcados como sueltos, sin reconciliar, sin racha, sin
        # adopción de carga y sin progresión. El sistema los veía y los
        # archivaba diciendo que no tocaban.
        #
        # Ahora no hay días en los que no toque fuerza. Se llega aquí cuando lo
        # planificado no era una sesión del ciclo, que en la práctica es un día
        # rojo con su bloque de recuperación: entrenar es entonces una decisión
        # del usuario por encima de la del sistema, y se cuenta como tal, sin
        # dar a entender que sobraba.
        return (
            f"ese día el plan era {plan.get('kind')} y entrenaste fuerza igual"
        )
    if rk is None:
        return "no sale de ninguna rutina del plan"
    if rk in ciclo and rkey in ciclo:
        return _motivo_otro_dia(
            cfg, rk, rkey, declarada=declarada, fecha_pesos=fecha_pesos, dia=dia
        )
    return f"es {rk} y en la rotación tocaba {rkey}"


def _motivo_otro_dia(
    cfg: Any,
    hecha: str,
    puesta: str,
    *,
    declarada: str | None,
    fecha_pesos: date | None,
    dia: date | None,
) -> str:
    """Entrené un día del ciclo y el sistema había puesto otro.

    LO QUE ESTA FRASE AÑADE A «HAS ENTRENADO OTRA COSA» ES LA FECHA DE LOS PESOS
    ---------------------------------------------------------------------------
    Que las dos rutinas no coincidan ya se decía. Lo que no se decía es lo único
    que cambia lo que levanté: la rutina que abrí en el gimnasio llevaba dentro
    los pesos del último día en que se planificó, porque entre medias nadie la
    toca. El semáforo de esa mañana, la progresión de esa mañana y el recorte
    del ámbar fueron a la OTRA. Sin esta frase, una sesión que se hace pesada
    porque arrastra la carga de hace dos semanas se lee como un mal día, y el
    sistema tenía el dato para evitarlo.

    Se decidió esto en lugar de escribir las tres rutinas cada mañana, que era
    la alternativa: triplicar las llamadas a la API por un caso raro no
    compensa, y el caso raro deja de doler en cuanto se nombra.

    «DECLARASTE» Y «TOCABA» NO SON LA MISMA FRASE.
    ---------------------------------------------
    Si el selector del check-in eligió esa rutina, atribuírsela a la rotación
    sería contarme mal mi propio día: la elegí yo. Y al revés, llamar
    «declaración» a lo que propuso el ciclo cuando no contesté el formulario
    convertiría el silencio en una afirmación.

    `declarada` llega tal cual salió del check-in, sin filtrar: puede ser
    `None`, una rutina del ciclo, «bici» u «otro». No hace falta limpiarla
    porque lo único que se hace con ella es compararla con `puesta`, que aquí
    siempre es una rutina del ciclo; las tres que no lo son fallan la igualdad
    solas y caen en «tocaba», que es la frase correcta para ellas.
    """
    t_hecha = _titulo_de(cfg, hecha)
    t_puesta = _titulo_de(cfg, puesta)
    cabeza = (
        f"declaraste {t_puesta} y entrenaste {t_hecha}"
        if declarada == puesta
        else f"tocaba {t_puesta} y entrenaste {t_hecha}"
    )
    cola = f": los ajustes de la mañana fueron a {t_puesta}"

    if fecha_pesos is None:
        # Nunca escrita: no es un fallo, es el estado normal de una rutina que
        # todavía no ha salido en ninguna rotación, y decir «llevaba los pesos
        # del ...» con una fecha inventada sería peor que no decir nada.
        return f"{cabeza}, sin pesos escritos nunca por el sistema{cola}"
    if dia is not None and fecha_pesos >= dia:
        # SE ESCRIBIÓ ESA MISMA MAÑANA, que pasa por un camino concreto: a las
        # 07:00 sin check-in el motor propuso y escribió esta rutina, a las
        # 09:40 llegó el formulario eligiendo otra y se escribió la otra
        # encima. Los pesos de la primera son de hoy, así que la frase de
        # siempre -«llevaba los pesos del 03/09»- sería falsa, y la cola
        # también: los ajustes de la mañana fueron a las dos, por turnos.
        return f"{cabeza}, que también se había escrito esa mañana"
    return (
        f"{cabeza}, con los pesos del "
        f"{fecha_pesos.strftime('%d/%m')}{cola}"
    )


class _PlanLeido:
    """Adapta la sesión guardada a lo que `workout_compliance` espera leer."""

    def __init__(self, plan: dict[str, Any]):
        self.exercises = plan.get("exercises") or []


def _cumplimiento_contra(
    workouts: list[dict[str, Any]], plan: dict[str, Any], cfg: Any
) -> tuple[dict[str, bool], dict[str, float | None], dict[str, str]]:
    """Qué de `plan` se hizo, uniendo todos los `workouts` que lo ejecutaban.

    Existe como función aparte porque ahora hay DOS planes que reconciliar cada
    noche -la fuerza y el bloque HIIT- y hasta ahora esto era un bucle suelto
    dentro de `run_reconcile` que solo sabía del primero. Copiarlo para el
    segundo habría dejado dos criterios de unión que podían separarse sin que
    nada fallara.

    Devuelve `(executed, pesos, motivos)`.
    """
    executed: dict[str, bool] = {}
    pesos: dict[str, float | None] = {}
    motivos: dict[str, str] = {}
    plan_obj = _PlanLeido(plan)
    for w in workouts:
        # Un ejercicio cuenta como hecho si CUALQUIERA de los entrenamientos
        # lo completó: partir la sesión en dos ratos es normal y no debería
        # romper la racha.
        for key, ok in workout_compliance(w, plan_obj, cfg).items():
            executed[key] = executed.get(key, False) or ok
        # El motivo se une con el mismo criterio, y por eso «no aparece» cede
        # ante cualquier otro: en una sesión partida en dos, el ejercicio no
        # está en uno de los dos ratos por definición, y quedarse con esa
        # frase taparía lo que sí se vio en el rato donde estaba.
        for key, porque in motivos_incumplimiento(w, plan_obj, cfg).items():
            if motivos.get(key, SIN_RASTRO) == SIN_RASTRO:
                motivos[key] = porque
        # El máximo entre entrenamientos, por lo mismo que el cumplimiento se
        # une con un OR: partir la sesión en dos ratos es normal, y la serie
        # más pesada del día es la más pesada de los dos ratos.
        for key, kg in pesos_ejecutados(w, plan_obj, cfg).items():
            if kg is None:
                continue
            previo = pesos.get(key)
            pesos[key] = kg if previo is None else max(previo, kg)
    # Lo que acabó completo no tiene nada que explicar, aunque en uno de los
    # ratos se quedara corto.
    motivos = {k: v for k, v in motivos.items() if not executed.get(k)}
    return executed, pesos, motivos


# ---------------------------------------------------------------------------
# La mañana siguiente
# ---------------------------------------------------------------------------


def run_aviso_percepcion(
    session: Session,
    cfg: Any,
    day: date,
    *,
    telegram_client: Any = None,
    dry_run: bool = False,
    motivo_sin_cliente: str | None = None,
) -> AvisoResult:
    """Evalúa lo que ya se pueda evaluar y cuenta las disociaciones pendientes.

    VA APARTE DEL MENSAJE DE LA DECISIÓN, Y ESO ES EL PUNTO
    -------------------------------------------------------
    Es un envío propio, no un párrafo añadido al plan del día. Mezclarlos
    convertiría el contador en un argumento a favor o en contra de entrenar hoy,
    y esta vista no opina sobre hoy: mira a ayer y no propone nada. Además, el
    mensaje de la decisión se manda por dos caminos distintos -el check-in de la
    PWA y el fallback de las nueve-, así que colgarse de él significaría o
    duplicar el aviso o perderlo según a qué hora se rellenara el formulario.

    POR QUÉ EVALÚA AQUÍ Y NO POR LA NOCHE
    -------------------------------------
    Porque la sesión de ayer necesita el check-in de HOY para tener su esfuerzo
    percibido, y ese check-in llega esta mañana. Evaluando por la noche, la
    sesión del lunes no estaría lista hasta el martes por la noche y el aviso
    saldría el miércoles: dos mañanas tarde para algo que se pidió para "la
    mañana siguiente". Evaluar y avisar en el mismo trabajo es lo que hace que
    el lunes se cuente el martes.

    EL ORDEN IMPORTA Y NO ES NEGOCIABLE
    -----------------------------------
    Se marca `reported_at` DESPUÉS de que el envío haya salido bien. Al revés
    -marcar y luego enviar- un fallo de red borraría el aviso sin haberlo dado,
    y nadie se enteraría de que faltó: la fila quedaría como contada para
    siempre y ese día desaparecería del único sitio donde iba a aparecer.

    Por lo mismo, un `dry_run` NO marca nada. Si marcara, un ensayo se comería
    el aviso de verdad.
    """
    from app.analysis.rendimiento import (
        contador_historico,
        evaluar_pendientes,
        marcar_reportadas,
        mensaje_disociacion,
        pendientes_de_avisar,
    )

    res = AvisoResult(day=day)

    # Se evalúa SIEMPRE, haya o no a quién avisar. La tabla es el histórico que
    # sostiene la vista de métricas; dejar de escribirla porque hoy no hay
    # Telegram configurado ataría el registro a que funcione el mensajero.
    res.evaluadas = len(evaluar_pendientes(session, cfg, hasta=day))
    session.flush()

    pendientes = pendientes_de_avisar(session, hasta=day)
    res.pendientes = len(pendientes)
    if not pendientes:
        res.motivo = "no hay ninguna disociación sin contar"
        return res

    # Las cifras son las del acumulado, las mismas que enseña la pantalla.
    acumulado = contador_historico(session, hasta=day)
    res.texto = "\n\n".join(
        mensaje_disociacion(f, veces=acumulado["veces"], de=acumulado["de"])
        for f in pendientes
    )

    if telegram_client is None:
        res.status = "skipped"
        res.motivo = motivo_sin_cliente or "sin cliente de Telegram configurado"
        res.problemas.append(
            f"no hay cliente de Telegram ({res.motivo}): las "
            f"{len(pendientes)} disociaciones sin contar siguen pendientes"
        )
    else:
        try:
            r = telegram_client.send(res.texto, dry_run=dry_run)
            res.status = "sent" if r.sent else ("dry_run" if dry_run else "skipped")
            res.motivo = r.reason
        except Exception as exc:  # noqa: BLE001
            res.status = "error"
            res.motivo = str(exc)
            res.problemas.append(f"Telegram: {exc}")
            log.exception("fallo enviando el aviso de percepción")

    # El registro se escribe pase lo que pase, igual que el de la decisión: un
    # día en el que la disociación existió y no se contó a nadie tiene que poder
    # encontrarse después, y sin fila sería indistinguible de un día tranquilo.
    session.add(
        Notification(
            date=day,
            kind="perception",
            channel="telegram",
            status=res.status,
            body=res.texto,
            error=res.motivo if res.status != "sent" else None,
        )
    )

    # `not dry_run` va aparte de `status` a propósito, aunque el cliente de
    # verdad ya devuelve `sent=False` en un ensayo y el estado sea "dry_run".
    # Marcar de más es la única equivocación irreversible que hay aquí: borra el
    # aviso sin haberlo dado y ese día no vuelve a salir nunca. Que la garantía
    # dependa de lo que conteste un cliente es dejarla en manos de un cliente;
    # el modo de pruebas se comprueba aquí, donde se sabe con certeza.
    if res.status == "sent" and not dry_run:
        marcar_reportadas(
            session, pendientes, cuando=datetime.now(UTC).replace(tzinfo=None)
        )
        res.marcadas = len(pendientes)

    return res
