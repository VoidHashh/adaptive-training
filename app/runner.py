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
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import repository as repo
from app.engine.decision import apply_execution, decide
from app.engine.message import render_telegram
from app.engine.signals import Checkin, build_signals
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
    hevy_status: str = "skipped"  # ok | error | skipped | dry_run
    hevy_reason: str = ""
    telegram_status: str = "skipped"  # sent | error | skipped | dry_run
    telegram_reason: str = ""
    # Fallos que NO han impedido terminar. Van aquí en vez de a un log que nadie
    # lee: quien llama decide si los enseña, pero no puede alegar que no los
    # sabía.
    problemas: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problemas


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
) -> DailyResult:
    """Decide el día y lo ejecuta. Los clientes se inyectan a propósito.

    Se pasan `metrics` y `rides` ya leídos en vez de leerlos aquí porque de
    dónde salen los datos es decisión de quien llama -Garmin, la caché, o datos
    de ejemplo en un ensayo- y meterla dentro haría imposible probar el resto
    sin red.
    """
    checkin_row = repo.get_checkin(session, day)
    valores = repo.checkin_values(checkin_row)
    checkin = Checkin(date=day, values=valores) if valores else None

    signals = build_signals(
        cfg, day, metrics=metrics, rides=rides, checkin=checkin
    )

    # El estado sale de la base de datos, no de cero. Es la diferencia entre un
    # sistema que recuerda y uno que cada mañana vuelve a nacer.
    state = repo.load_state(session, program_start=cfg.program_start)
    decision = decide(cfg, day, signals, state, source=source)

    # Lo que la reconciliación de anoche movió de la carga, para contarlo AHORA.
    # Se cuelga antes de guardar la decisión para que quede también en el
    # histórico: el JSON de hoy tiene que poder explicar por qué el hip thrust
    # sale a 62,5 y no a lo de ayer, y dentro de tres meses no habrá otro sitio
    # donde mirarlo. Se sellan como contadas más abajo, y solo si hay mensaje.
    decision.load_adoptions = repo.adopciones_sin_contar(session)

    res = DailyResult(day=day, decision=decision)
    fila = repo.save_decision(session, decision)
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

    s = decision.session
    if not (s.write_to_hevy and s.routine_key and s.hevy_routine_id):
        res.hevy_status = "skipped"
        res.hevy_reason = "hoy la sesión no toca Hevy"
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
        # Un salto legítimo sí existe y sigue siendo salto: que hoy la sesión no
        # toque Hevy (arriba) o que `integrations.hevy.write_enabled` esté en
        # false, que es el interruptor de verdad y se resuelve dentro de
        # `write_routine`. Llegar hasta aquí sin cliente es otra cosa: significa
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
        res.hevy_status = "ok" if r.written else ("dry_run" if dry_run else "skipped")
        res.hevy_reason = r.reason
        if not r.written and not dry_run and r.error:
            res.hevy_status = "error"
            res.problemas.append(f"Hevy: {r.error}")
    except Exception as exc:  # noqa: BLE001
        # Que Hevy falle no puede tumbar la mañana entera: el mensaje todavía
        # tiene que salir, y tiene que decir esto.
        res.hevy_status = "error"
        res.hevy_reason = str(exc)
        res.problemas.append(f"Hevy: {exc}")
        log.exception("fallo escribiendo la rutina en Hevy")

    _anotar_hevy(session, decision, fila, res, payload)


def _anotar_hevy(
    session: Session, decision: Any, fila: Any, res: DailyResult, payload: dict
) -> None:
    session.add(
        HevyWrite(
            decision_id=getattr(fila, "id", None),
            date=decision.day,
            routine_key=decision.session.routine_key,
            hevy_routine_id=decision.session.hevy_routine_id,
            status=res.hevy_status,
            error=res.hevy_reason if res.hevy_status == "error" else None,
            payload_json=repo._json(payload),
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
    if res.hevy_status == "error":
        texto = (
            "⚠️ <b>La rutina NO se ha escrito en Hevy</b>\n"
            f"{res.hevy_reason}\n"
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
        try:
            r = client.send(texto, dry_run=dry_run)
            res.telegram_status = (
                "sent" if r.sent else ("dry_run" if dry_run else "skipped")
            )
            res.telegram_reason = r.reason
        except Exception as exc:  # noqa: BLE001
            res.telegram_status = "error"
            res.telegram_reason = str(exc)
            res.problemas.append(f"Telegram: {exc}")
            log.exception("fallo enviando el mensaje")

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
        pesos_ejecutados,
        workout_compliance,
        workout_totals,
    )

    res = ReconcileResult(day=day)

    fila = repo.current_decision(session, day)
    if fila is None:
        res.motivo = f"no hay decisión guardada del {day}: no hay plan contra el que comparar"
        return res

    plan = repo.planned_session(fila)
    rkey = plan.get("routine")
    if not rkey or plan.get("kind") not in {"full", "reduced"}:
        res.motivo = f"el {day} no tocaba fuerza ({plan.get('kind')}): nada que reconciliar"
        return res

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

    # Un ejercicio cuenta como hecho si CUALQUIERA de los entrenamientos del día
    # lo completó: partir la sesión en dos ratos es normal y no debería romper
    # la racha.
    plan_obj = _PlanLeido(plan)
    executed: dict[str, bool] = {}
    pesos: dict[str, float | None] = {}
    for w in nuevos:
        for key, ok in workout_compliance(w, plan_obj, cfg).items():
            executed[key] = executed.get(key, False) or ok
        # El máximo entre entrenamientos, por lo mismo que el cumplimiento se
        # une con un OR: partir la sesión en dos ratos es normal, y la serie
        # más pesada del día es la más pesada de los dos ratos.
        for key, kg in pesos_ejecutados(w, plan_obj, cfg).items():
            if kg is None:
                continue
            previo = pesos.get(key)
            pesos[key] = kg if previo is None else max(previo, kg)
    res.executed = executed
    res.pesos = pesos

    state = repo.load_state(session, program_start=cfg.program_start)
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
    raw = cfg.raw if hasattr(cfg, "raw") else (cfg or {})
    adopciones = adoptar_cargas(
        state,
        routine_key=str(rkey),
        exercises=plan.get("exercises") or [],
        pesos_hechos=pesos,
        limpio=executed,
        set_cfg=(raw.get("set_types") or {}),
        prog_cfg=(raw.get("progression") or {}),
    )
    res.adopciones = [a.to_dict() for a in adopciones]

    repo.save_state(session, state, day=day)
    repo.guardar_adopciones(session, day, adopciones)

    for w in nuevos:
        totales = workout_totals(w)
        session.add(
            WorkoutLog(
                hevy_workout_id=str(w.get("id")),
                date=day,
                routine_key=str(rkey),
                title=w.get("title"),
                all_sets_at_target=all(executed.values()) if executed else None,
                duration_s=totales.duration_s,
                total_sets=totales.total_sets,
                total_volume_kg=totales.total_volume_kg,
                # El entrenamiento entero, que hasta ahora se leía, se usaba
                # para decidir si la sesión fue limpia y se tiraba. Es el único
                # sitio donde queda el peso y las reps de CADA serie: ni la
                # decisión ni el estado del motor guardan lo que de verdad se
                # levantó, solo si alcanzó el objetivo. Sin esto no se puede
                # responder nunca a "¿cuánto subió el hip thrust en tres meses?"
                # y no se puede arreglar hacia atrás.
                raw_json=repo.crudo_para_guardar(w, etiqueta="entrenamiento Hevy"),
            )
        )
    session.flush()

    res.avanzado = True
    limpios = sum(1 for v in executed.values() if v)
    res.motivo = f"{limpios}/{len(executed)} ejercicios completos"
    return res


class _PlanLeido:
    """Adapta la sesión guardada a lo que `workout_compliance` espera leer."""

    def __init__(self, plan: dict[str, Any]):
        self.exercises = plan.get("exercises") or []


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
