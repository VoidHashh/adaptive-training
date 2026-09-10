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
from datetime import date
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
    avanzado: bool = False
    motivo: str = ""


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

    res = DailyResult(day=day, decision=decision)
    fila = repo.save_decision(session, decision)
    _guardar_lo_leido(session, signals, metrics, res)

    _escribir_hevy(session, cfg, decision, fila, res, hevy_client, dry_run)
    _mandar_telegram(session, cfg, decision, res, telegram_client, dry_run)

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
        cargas = {
            d: (
                signals.history.get("load_3d", {}).get(d),
                signals.history.get("load_7d", {}).get(d),
            )
            for d in {m.date for m in metrics or [] if getattr(m, "date", None)}
        }
        dias = repo.upsert_daily_metrics(session, metrics, loads=cargas)
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
) -> None:
    from app.integrations.hevy import build_routine_payload

    s = decision.session
    if not (s.write_to_hevy and s.routine_key and s.hevy_routine_id):
        res.hevy_status = "skipped"
        res.hevy_reason = "hoy la sesión no toca Hevy"
        return

    payload = build_routine_payload(s, cfg)
    if client is None:
        res.hevy_status = "skipped"
        res.hevy_reason = "sin cliente de Hevy configurado"
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
        res.telegram_reason = "sin cliente de Telegram configurado"
        res.problemas.append(
            "no hay cliente de Telegram: la decisión de hoy no se ha contado a nadie"
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
    from app.integrations.hevy import _fecha_workout, workout_compliance

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
    for w in nuevos:
        for key, ok in workout_compliance(w, plan_obj, cfg).items():
            executed[key] = executed.get(key, False) or ok
    res.executed = executed

    state = repo.load_state(session, program_start=cfg.program_start)
    apply_execution(
        state,
        routine_key=str(rkey),
        exercises=plan.get("exercises") or [],
        executed=executed,
        light=fila.light,
        progressed=repo.progressed_keys(fila),
    )
    repo.save_state(session, state, day=day)

    for w in nuevos:
        session.add(
            WorkoutLog(
                hevy_workout_id=str(w.get("id")),
                date=day,
                routine_key=str(rkey),
                title=w.get("title"),
                all_sets_at_target=all(executed.values()) if executed else None,
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
