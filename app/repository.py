"""Puente entre el `EngineState` del motor y la base de datos.

El motor es puro: entra un estado y unas señales, sale una decisión y un estado
nuevo. Nunca toca SQL. Este módulo es el único sitio donde ese estado se
convierte en filas y vuelve, y existe separado por dos motivos:

1. El motor se puede probar sin base de datos, que es lo que permite que la
   batería de tests corra en un segundo.
2. Cuando algo no se recuerda de un día para otro, el fallo está aquí y no
   repartido por seis módulos.

POR QUÉ ESTE MÓDULO ES DELICADO
-------------------------------
Un campo de `EngineState` que este fichero no guarde no da ningún error: vuelve
a su valor por defecto en el siguiente arranque y el sistema sigue decidiendo
tan tranquilo, con la memoria en blanco. Una racha de sesiones limpias a cero,
una regla especial que caduca sola, una descarga que se repite.

Por eso `test_repository.py` no comprueba campo por campo a mano: recorre
`dataclasses.fields(EngineState)` y exige que todos sobrevivan a una vuelta
completa. El día que alguien añada un campo al estado y no lo guarde aquí, ese
test lo dice por su nombre.
"""

from __future__ import annotations

import json
from dataclasses import fields as dataclass_fields
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engine.decision import ActiveRule, EngineState
from app.models import (
    ExerciseTarget,
    PendingStrength,
    ProgramState,
    RoutineState,
    RuleState,
)
from app.models import Checkin as CheckinRow
from app.models import Decision as DecisionRow

# Los campos de `EngineState` que este módulo sabe guardar y recuperar. La
# lista está escrita a mano a propósito: es lo que permite que un campo nuevo
# se note. Si estuviera generada a partir del dataclass, un campo añadido
# entraría solo en la lista y el test dejaría de proteger nada.
CAMPOS_PERSISTIDOS = frozenset(
    {
        "clean_sessions",
        "compliance",
        "last_routine_light",
        "active_rules",
        "pending_strength",
        "last_deload_start",
        # `program_start` sale de config.yaml, no de la BD. Ver `load_state`.
        "program_start",
    }
)


def campos_sin_persistir() -> set[str]:
    """Campos de `EngineState` que nadie guarda. Debe estar vacío."""
    return {f.name for f in dataclass_fields(EngineState)} - set(CAMPOS_PERSISTIDOS)


# ---------------------------------------------------------------------------
# Lectura
# ---------------------------------------------------------------------------


def load_state(session: Session, *, program_start: date | None = None) -> EngineState:
    """Reconstruye el estado del motor desde la base de datos.

    `program_start` se pasa desde `config.yaml` en vez de leerse de una tabla:
    es un ajuste del usuario, y tenerlo en dos sitios solo sirve para que un día
    discrepen y nadie sepa cuál manda.
    """
    state = EngineState(program_start=program_start)

    for row in session.scalars(select(ExerciseTarget)).all():
        clave = (row.routine_key, row.exercise_key)
        state.clean_sessions[clave] = int(row.clean_streak or 0)
        # `last_compliant` nulo = todavía no se ha entrenado ese ejercicio. El
        # motor trata la ausencia como True (`for_routine` usa `default=True`),
        # así que un ejercicio estrenado no arranca penalizado.
        if row.last_compliant is not None:
            state.compliance[clave] = bool(row.last_compliant)

    for row in session.scalars(select(RoutineState)).all():
        state.last_routine_light[row.routine_key] = row.last_light

    for row in session.scalars(select(RuleState)).all():
        state.active_rules.append(
            ActiveRule(
                name=row.rule_name,
                action=json.loads(row.action_json) if row.action_json else {},
                active_from=row.active_from,
                active_until=row.active_until,
                entity=row.entity,
                reason=row.reason or "",
                notify=bool(row.notify),
            )
        )

    pendiente = session.scalars(
        select(PendingStrength)
        .where(PendingStrength.status == "pending")
        .order_by(PendingStrength.deferred_from.desc())
    ).first()
    if pendiente is not None:
        state.pending_strength = (pendiente.routine_key, pendiente.deferred_from)

    programa = session.get(ProgramState, 1)
    if programa is not None:
        state.last_deload_start = programa.last_deload_start

    return state


# ---------------------------------------------------------------------------
# Escritura
# ---------------------------------------------------------------------------


def save_state(session: Session, state: EngineState, *, day: date | None = None) -> None:
    """Vuelca el estado del motor a la base de datos.

    No es un `INSERT` ciego: actualiza la fila que ya existe cuando la hay. Las
    tablas tienen clave única por (rutina, ejercicio) y por rutina, así que
    insertar sin mirar reventaría al segundo día.

    `day` es la fecha de la decisión que ha producido este estado; sirve para
    anotar cuándo se entrenó cada rutina por última vez.
    """
    _guardar_ejercicios(session, state)
    _guardar_rutinas(session, state, day)
    _guardar_reglas(session, state)
    _guardar_pendiente(session, state)
    _guardar_programa(session, state)
    session.flush()


def _guardar_ejercicios(session: Session, state: EngineState) -> None:
    # `clean_sessions` y `compliance` son dos diccionarios con las mismas
    # claves, pero no siempre las mismas: se recorre la unión para no perder
    # un ejercicio que solo aparezca en uno de los dos.
    claves = set(state.clean_sessions) | set(state.compliance)
    if not claves:
        return

    existentes = {
        (r.routine_key, r.exercise_key): r
        for r in session.scalars(select(ExerciseTarget)).all()
    }
    for rutina, ejercicio in claves:
        fila = existentes.get((rutina, ejercicio))
        if fila is None:
            fila = ExerciseTarget(routine_key=rutina, exercise_key=ejercicio)
            session.add(fila)
        fila.clean_streak = int(state.clean_sessions.get((rutina, ejercicio), 0))
        if (rutina, ejercicio) in state.compliance:
            fila.last_compliant = bool(state.compliance[(rutina, ejercicio)])


def _guardar_rutinas(session: Session, state: EngineState, day: date | None) -> None:
    if not state.last_routine_light:
        return
    existentes = {
        r.routine_key: r for r in session.scalars(select(RoutineState)).all()
    }
    for rutina, luz in state.last_routine_light.items():
        fila = existentes.get(rutina)
        if fila is None:
            fila = RoutineState(routine_key=rutina)
            session.add(fila)
        # Solo se toca `last_trained_date` cuando la luz cambia de valor o la
        # fila es nueva: reescribirla en cada guardado pondría la fecha de hoy
        # en rutinas que hoy no se han tocado.
        if fila.last_light != luz or fila.last_trained_date is None:
            fila.last_trained_date = day
        fila.last_light = luz


def _guardar_reglas(session: Session, state: EngineState) -> None:
    """Las reglas activas se reemplazan enteras, no se acumulan.

    `advance_state` ya devuelve la lista COMPLETA de las que siguen vigentes:
    las caducadas se han caído ahí. Si aquí se hiciera un upsert sin borrar, una
    regla caducada seguiría en la tabla para siempre y volvería a cargarse cada
    mañana. El peso muerto retirado catorce días se quedaría retirado a
    perpetuidad, y el motivo -"caducó hace tres meses"- no estaría en ninguna
    parte.
    """
    for fila in session.scalars(select(RuleState)).all():
        session.delete(fila)
    session.flush()

    for regla in state.active_rules:
        session.add(
            RuleState(
                rule_name=regla.name,
                entity=regla.entity,
                active_from=regla.active_from,
                active_until=regla.active_until,
                reason=regla.reason or None,
                action_json=json.dumps(regla.action, ensure_ascii=False)
                if regla.action
                else None,
                notify=bool(regla.notify),
            )
        )


def _guardar_pendiente(session: Session, state: EngineState) -> None:
    abiertas = session.scalars(
        select(PendingStrength).where(PendingStrength.status == "pending")
    ).all()

    if state.pending_strength is None:
        # La sesión aplazada se ha recuperado (o ya no procede). Se marca, no se
        # borra: que una sesión roja se recuperase tres días después es
        # justamente lo que se querrá mirar dentro de un mes.
        for fila in abiertas:
            fila.status = "recovered"
        return

    rutina, aplazada_el = state.pending_strength
    for fila in abiertas:
        if fila.routine_key == rutina and fila.deferred_from == aplazada_el:
            return  # ya está registrada; no se duplica
        fila.status = "cancelled"
    session.add(
        PendingStrength(
            routine_key=rutina, deferred_from=aplazada_el, status="pending"
        )
    )


def _guardar_programa(session: Session, state: EngineState) -> None:
    fila = session.get(ProgramState, 1)
    if fila is None:
        fila = ProgramState(id=1)
        session.add(fila)
    fila.last_deload_start = state.last_deload_start


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def read_pending(session: Session) -> tuple[str, date] | None:
    """La sesión de fuerza que quedó aplazada, si la hay.

    Se usa al arrancar para poder decirlo: una sesión aplazada que nadie
    menciona es una sesión perdida.
    """
    fila = session.scalars(
        select(PendingStrength)
        .where(PendingStrength.status == "pending")
        .order_by(PendingStrength.deferred_from.desc())
    ).first()
    return (fila.routine_key, fila.deferred_from) if fila else None


# ---------------------------------------------------------------------------
# Check-in
# ---------------------------------------------------------------------------


def sliders_del_config(config: Any) -> list[str]:
    """Las claves de los deslizadores, leídas del YAML.

    No hay una lista escrita a mano en este módulo a propósito: el séptimo
    deslizador (`yesterday_rpe`) se añadió después del diseño inicial, y una
    copia a mano se habría quedado con seis sin que nadie se enterase. Las
    columnas de la tabla sí son fijas -son un esquema-, pero quién puede
    escribir en ellas lo decide el config.
    """
    raw = config.raw if hasattr(config, "raw") else (config or {})
    return [str(s["key"]) for s in (raw.get("checkin_sliders") or []) if s.get("key")]


def upsert_checkin(
    session: Session,
    day: date,
    valores: dict[str, Any],
    *,
    config: Any = None,
    comments: str | None = None,
) -> CheckinRow:
    """Guarda el check-in del día. Si ya existe, lo sustituye.

    Sustituir y no acumular es deliberado: el formulario se puede reenviar
    porque uno se ha equivocado de deslizador, y lo que vale es la última
    respuesta. El rastro de que la decisión cambió al llegar el check-in queda
    en `decisions`, que sí es append-only.

    Una clave desconocida es un ERROR, no un campo que se ignora. Un `fatiga`
    por `fatigue` escrito desde la PWA se guardaría en ninguna parte y el
    sistema decidiría sin ese dato, diciendo que el check-in está completo.
    """
    permitidas = set(sliders_del_config(config)) if config is not None else None
    columnas = {c.name for c in CheckinRow.__table__.columns}

    for clave in valores:
        if clave not in columnas:
            raise ValueError(
                f"el check-in trae '{clave}', que no es una columna de `checkins`. "
                f"Campos válidos: {sorted(columnas - {'id', 'date', 'submitted_at'})}"
            )
        if permitidas is not None and clave not in permitidas:
            raise ValueError(
                f"el check-in trae '{clave}', que no está en `checkin_sliders` del "
                f"config. O sobra en el formulario o falta en el YAML."
            )

    fila = session.scalars(
        select(CheckinRow).where(CheckinRow.date == day)
    ).first()
    if fila is None:
        fila = CheckinRow(date=day)
        session.add(fila)

    for clave, valor in valores.items():
        setattr(fila, clave, valor)
    if comments is not None:
        fila.comments = comments

    session.flush()
    return fila


def get_checkin(session: Session, day: date) -> CheckinRow | None:
    return session.scalars(select(CheckinRow).where(CheckinRow.date == day)).first()


def checkin_values(fila: CheckinRow | None) -> dict[str, Any]:
    """La fila en el formato de diccionario que espera el motor.

    Los nulos se quitan en vez de pasarse como `None`: para las reglas no es lo
    mismo "el deslizador vino a 0" que "el deslizador no se contestó", y un
    `None` dentro de `values` haría que la señal exista con valor nulo en vez de
    no existir.
    """
    if fila is None:
        return {}
    omitir = {"id", "date", "submitted_at", "comments"}
    return {
        c.name: getattr(fila, c.name)
        for c in CheckinRow.__table__.columns
        if c.name not in omitir and getattr(fila, c.name) is not None
    }


# ---------------------------------------------------------------------------
# Decisiones
# ---------------------------------------------------------------------------


def save_decision(session: Session, decision: Any) -> DecisionRow:
    """Registra la decisión del día. Append-only.

    Las anteriores del mismo día se marcan `is_current=False` en vez de
    borrarse: que a las 07:00 se decidiera sin check-in y a las 09:40 llegara el
    formulario y cambiara el semáforo es exactamente lo que se querrá poder
    reconstruir cuando algo salga raro.
    """
    for previa in session.scalars(
        select(DecisionRow).where(
            DecisionRow.date == decision.day, DecisionRow.is_current.is_(True)
        )
    ).all():
        previa.is_current = False

    fila = DecisionRow(
        date=decision.day,
        light=decision.light,
        trigger_rule=decision.trigger_rule,
        fired_rules_json=_json([r.name for r in decision.light_decision.fired]),
        skipped_rules_json=_json(
            [
                {"name": r.name, "missing": list(r.missing)}
                for r in decision.light_decision.skipped
            ]
        ),
        inputs_snapshot_json=_json(decision.signals.snapshot()),
        config_hash=decision.config_hash,
        source=decision.source,
        is_current=True,
        planned_session_json=_json(decision.session.to_dict())
        if hasattr(decision.session, "to_dict")
        else None,
        bike_recommendation_json=_json(decision.bike.to_dict())
        if decision.bike is not None and hasattr(decision.bike, "to_dict")
        else None,
    )
    session.add(fila)
    session.flush()
    return fila


def current_decision(session: Session, day: date) -> DecisionRow | None:
    return session.scalars(
        select(DecisionRow).where(
            DecisionRow.date == day, DecisionRow.is_current.is_(True)
        )
    ).first()


def _json(valor: Any) -> str | None:
    return json.dumps(valor, ensure_ascii=False, default=str) if valor is not None else None


def state_as_dict(state: EngineState) -> dict[str, Any]:
    """El estado en forma legible, para el log de arranque y `/api/state`."""
    return {
        "clean_sessions": {
            f"{r}/{e}": v for (r, e), v in sorted(state.clean_sessions.items())
        },
        "compliance": {
            f"{r}/{e}": v for (r, e), v in sorted(state.compliance.items())
        },
        "last_routine_light": dict(sorted(state.last_routine_light.items())),
        "active_rules": [r.to_dict() for r in state.active_rules],
        "pending_strength": (
            {
                "routine": state.pending_strength[0],
                "deferred_from": state.pending_strength[1].isoformat(),
            }
            if state.pending_strength
            else None
        ),
        "program_start": (
            state.program_start.isoformat() if state.program_start else None
        ),
        "last_deload_start": (
            state.last_deload_start.isoformat() if state.last_deload_start else None
        ),
    }
