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
import logging
from dataclasses import fields as dataclass_fields
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from app.engine.decision import ActiveRule, EngineState
from app.engine.tendencia import DecisionDia
from app.models import (
    ExerciseTarget,
    LoadAdoption,
    PendingStrength,
    ProgramState,
    RoutineState,
    RuleState,
    WorkoutLog,
)
from app.models import Checkin as CheckinRow
from app.models import Decision as DecisionRow

log = logging.getLogger(__name__)

# Los campos de `EngineState` que este módulo sabe guardar y recuperar. La
# lista está escrita a mano a propósito: es lo que permite que un campo nuevo
# se note. Si estuviera generada a partir del dataclass, un campo añadido
# entraría solo en la lista y el test dejaría de proteger nada.
CAMPOS_PERSISTIDOS = frozenset(
    {
        "clean_sessions",
        "compliance",
        "current_sets",
        "sessions_since_progress",
        "below_plan_streak",
        "below_plan_best_kg",
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
        # `last_compliant` nulo = todavía no se ha reconciliado ese ejercicio.
        # La clave se deja AUSENTE a propósito, y hay que no tocarlo: guardar
        # un False de relleno cerraría la progresión acusando de un fallo que
        # nadie ha cometido, y guardar un True la abriría sobre una sesión que
        # no existe. `for_routine` propaga esa ausencia como `None` y
        # `evaluate_gate` la trata como lo que es: no se sabe, luego no se
        # sube carga hoy.
        if row.last_compliant is not None:
            state.compliance[clave] = bool(row.last_compliant)
        # La carga vigente. Un JSON ilegible NO se trata como "este ejercicio
        # empieza de cero": eso devolvería la carga al valor de `config.yaml`
        # sin decirlo, que es exactamente el fallo que esta columna arregla.
        if row.current_sets_json:
            try:
                series = json.loads(row.current_sets_json)
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    f"la carga guardada de {row.routine_key}/{row.exercise_key} "
                    f"no se puede leer ({exc}). Es el peso que toca levantar hoy: "
                    f"seguir sin él significaría volver en silencio al peso de "
                    f"partida de config.yaml."
                ) from exc
            if isinstance(series, list) and series:
                state.current_sets[clave] = series
        state.sessions_since_progress[clave] = int(row.sessions_since_progress or 0)
        # Solo se rehidrata la racha por debajo cuando de verdad la hay. Una
        # entrada a 0 en el diccionario y la ausencia de entrada valen lo mismo
        # para `adoptar_cargas`, pero la ausencia es lo que dice la tabla y
        # copiarla tal cual evita que un `save_state` posterior escriba filas de
        # ceros para ejercicios que nunca se han quedado cortos.
        if row.below_plan_streak:
            state.below_plan_streak[clave] = int(row.below_plan_streak)
        if row.below_plan_best_kg is not None:
            state.below_plan_best_kg[clave] = float(row.below_plan_best_kg)

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
    claves = (
        set(state.clean_sessions)
        | set(state.compliance)
        | set(state.current_sets)
        | set(state.sessions_since_progress)
        | set(state.below_plan_streak)
        | set(state.below_plan_best_kg)
    )
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

        series = state.current_sets.get((rutina, ejercicio))
        if series:
            fila.current_sets_json = json.dumps(series, ensure_ascii=False)
            # Proyección para consultas, calculada aquí mismo para que no pueda
            # discrepar de la lista de la que sale.
            pesos = [s.get("weight_kg") or 0 for s in series]
            fila.current_target_kg = max(pesos) if any(pesos) else None

        if (rutina, ejercicio) in state.sessions_since_progress:
            fila.sessions_since_progress = int(
                state.sessions_since_progress[(rutina, ejercicio)]
            )

        # Estas DOS se escriben siempre, presentes o no, y ahí está el detalle.
        # La racha por debajo se anula BORRANDO la clave del diccionario, así que
        # con el patrón de arriba -"solo si está"- la anulación no llegaría nunca
        # a la tabla: el contador se quedaría clavado en 2 para siempre y la
        # siguiente sesión floja, meses después, bajaría la carga como si fuera
        # la tercera seguida. La ausencia es un valor, y hay que guardarlo.
        fila.below_plan_streak = int(state.below_plan_streak.get((rutina, ejercicio), 0))
        mejor = state.below_plan_best_kg.get((rutina, ejercicio))
        fila.below_plan_best_kg = None if mejor is None else float(mejor)


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


def historial_checkins(session: Session, *, desde: date, hasta: date) -> list[Any]:
    """Los check-ins de una ventana, como `Checkin` del motor.

    EL OTRO PARÁMETRO MUERTO, Y EL MÁS CARO DE LOS DOS.
    ---------------------------------------------------
    `build_signals` acepta `checkin_history=` desde el primer día y tampoco se
    lo pasaba nadie: ni `run_daily` ni el ensayo en seco. Con el defecto `()`,
    `sig.history[clave]` acababa conteniendo COMO MUCHO un punto -el del propio
    día- y eso es justo lo que leen los umbrales adaptativos:
    `series = sig.history.get(metric, {})`.

    Hoy no se nota porque los dos únicos umbrales adaptativos del config miran
    métricas derivadas de las salidas (`load_3d_p90`, `load_7d_p90`), que se
    construyen por otro camino. Ninguna regla usa todavía un deslizador del
    formulario a lo largo de varios días. O sea que la mina está armada y
    dormida: el día que se defina un umbral adaptativo sobre la lumbar o el
    cansancio, `resolve_adaptive_threshold` recibiría una serie de un punto y
    devolvería un percentil de sí mismo -un umbral que siempre se cumple o
    nunca- con cara de estadística sobre el histórico propio.

    Ese día es octubre, en la recalibración. Preferimos que reviente ahora, con
    el sistema mirándose, a que calcule percentiles sobre un punto dentro de
    seis semanas sin que nada lo diga.

    Los nulos ya los quita `checkin_values`, así que un deslizador sin contestar
    no entra en la serie en vez de entrar como cero. Un día que no se contestó y
    un día contestado con el mínimo son cosas opuestas, y para un percentil se
    leerían igual.
    """
    from app.engine.signals import Checkin

    filas = session.scalars(
        select(CheckinRow)
        .where(CheckinRow.date >= desde, CheckinRow.date <= hasta)
        .order_by(CheckinRow.date)
    ).all()
    return [Checkin(date=f.date, values=checkin_values(f)) for f in filas]


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
        progression_json=_json(decision.progression.to_dict())
        if decision.progression is not None
        and hasattr(decision.progression, "to_dict")
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


def dias_con_decision(session: Session, *, desde: date, hasta: date) -> int:
    """Cuántos DÍAS distintos tienen decisión vigente en [desde, hasta].

    Días y no filas, y ahí está todo el cuidado: un día puede tener varias
    decisiones -la de las 07:00 sin check-in y la de las 09:40 con él- y contar
    filas daría por acumulada el doble de muestra de la que hay.

    NO se filtra por `is_current`, al revés que `serie_decisiones`. Allí importa
    cuál de las dos decisiones del día quedó vigente, porque se está leyendo el
    semáforo de cada día; aquí solo se pregunta si ese día pasó algo, y un día
    cuya decisión de las 07:00 quedó superada por la de las 09:40 es un día
    vivido igual. Añadir el filtro no cambiaría ninguna cuenta -toda fecha con
    filas tiene al menos una vigente- y habría que escribir junto a él por qué
    está, sin que nada lo comprobara nunca.

    Lo usa el recordatorio de recalibración, que mide muestra acumulada y no
    tiempo transcurrido: ver `app/engine/recalibracion.py`.
    """
    if desde > hasta:
        # No es un rango vacío del que devolver 0 tranquilamente: es que quien
        # llama ha calculado mal los extremos. Un cero aquí dejaría el aviso de
        # recalibración contando desde cero para siempre, sin decir nada.
        raise ValueError(
            f"rango invertido: desde={desde.isoformat()} es posterior a "
            f"hasta={hasta.isoformat()}"
        )
    # Sin `or 0` al final. `COUNT(*)` no devuelve NULL nunca, así que ese `or`
    # no protegería de nada real y sí taparía el día que esta consulta deje de
    # ser un COUNT: convertiría un None inesperado en un cero creíble, que es
    # justo el aviso que este contador existe para no perder.
    return int(
        session.scalar(
            select(func.count(distinct(DecisionRow.date))).where(
                DecisionRow.date >= desde,
                DecisionRow.date <= hasta,
            )
        )
    )


def serie_decisiones(session: Session, *, hasta: date) -> list[DecisionDia]:
    """El histórico de semáforos vigentes hasta `hasta`, para la capa de tendencia.

    Sin ventana. Podría acotarse a los 90 días de `trend.ventana_larga_dias` y
    sería un error en dos sitios: una racha larga se cortaría justo por donde
    empieza a importar, y el detector de racha no podría distinguir "no hay
    histórico suficiente" de "la racha llega hasta el principio de los tiempos",
    que es la diferencia entre callarse y mentir. La tabla crece un registro al
    día; leerla entera cuesta lo que cuesta leer unos miles de filas.

    Solo las `is_current`: un día puede tener varias decisiones -a las 07:00 sin
    check-in y a las 09:40 con él-, y contar las dos daría ese día dos veces en
    la cuenta de ámbares.
    """
    filas = session.scalars(
        select(DecisionRow)
        .where(DecisionRow.is_current.is_(True), DecisionRow.date <= hasta)
        .order_by(DecisionRow.date)
    ).all()
    return [DecisionDia(day=f.date, light=f.light, trigger_rule=f.trigger_rule)
            for f in filas]


def planned_session(fila: DecisionRow | None) -> dict[str, Any]:
    """La sesión que se planificó ese día, tal cual se guardó.

    La reconciliación compara contra ESTO y no contra lo que hoy produciría el
    motor. Volver a decidir por la noche daría la sesión que tocaría con las
    señales de ahora, que no tiene por qué ser la que se escribió por la mañana:
    se estaría midiendo el cumplimiento contra un plan que nunca existió.
    """
    if fila is None or not fila.planned_session_json:
        return {}
    return json.loads(fila.planned_session_json)


def progressed_keys(fila: DecisionRow | None) -> list[str]:
    """Ejercicios que subieron ese día. Su racha tiene que volver a cero."""
    if fila is None or not fila.progression_json:
        return []
    datos = json.loads(fila.progression_json)
    return [
        e["key"]
        for e in (datos.get("exercises") or [])
        if e.get("changed") and e.get("key")
    ]


def _json(valor: Any) -> str | None:
    return json.dumps(valor, ensure_ascii=False, default=str) if valor is not None else None


# ---------------------------------------------------------------------------
# Adopciones de carga: se deciden de noche, se cuentan por la mañana
# ---------------------------------------------------------------------------


def guardar_adopciones(session: Session, day: date, adopciones: Any) -> int:
    """Apunta lo que la carga ejecutada movió -o no- esa noche. Devuelve cuántas.

    Se guardan también las RECHAZADAS. Un tope que actúa sin dejar rastro es un
    tope que nadie puede corregir.
    """
    n = 0
    for a in adopciones or []:
        d = a.to_dict() if hasattr(a, "to_dict") else dict(a)
        session.add(
            LoadAdoption(
                date=day,
                routine_key=str(d.get("routine") or ""),
                exercise_key=str(d.get("key") or ""),
                direction=str(d.get("direction") or ""),
                prescribed_kg=d.get("prescribed_kg"),
                executed_kg=d.get("executed_kg"),
                before_kg=d.get("before_kg"),
                after_kg=d.get("after_kg"),
                applied=bool(d.get("applied")),
                reason=d.get("reason"),
            )
        )
        n += 1
    if n:
        session.flush()
    return n


def adopciones_sin_contar(session: Session) -> list[dict[str, Any]]:
    """Las adopciones que todavía no ha contado ningún mensaje. NO las marca.

    Sin límite de antigüedad: si el sistema pasó tres días sin mandar nada
    -porque el PC estuvo apagado, o Telegram falló-, esas subidas y bajadas
    siguen sin explicarse y el primer mensaje que salga tiene que explicarlas
    todas. Descartar las viejas dejaría un cambio de carga sin motivo visible,
    que es justo lo que esta tabla existe para impedir.

    Leer y marcar están separados A PROPÓSITO. Marcarlas al leerlas parece más
    simple y es peor: `_mandar_telegram` se traga los fallos de envío para que
    un Telegram caído no tumbe la mañana, así que la transacción se confirma
    igual. Las adopciones habrían quedado selladas como contadas por un mensaje
    que nunca llegó al móvil, y el cambio de carga se quedaría sin explicar para
    siempre. Marca `marcar_adopciones_contadas`, y solo cuando hay mensaje.
    """
    return [
        {
            "id": f.id,
            "day": f.date.isoformat(),
            "routine": f.routine_key,
            "key": f.exercise_key,
            "direction": f.direction,
            "prescribed_kg": f.prescribed_kg,
            "executed_kg": f.executed_kg,
            "before_kg": f.before_kg,
            "after_kg": f.after_kg,
            "applied": bool(f.applied),
            "reason": f.reason or "",
        }
        for f in session.scalars(
            select(LoadAdoption)
            .where(LoadAdoption.reported_at.is_(None))
            .order_by(LoadAdoption.date, LoadAdoption.id)
        ).all()
    ]


def marcar_adopciones_contadas(session: Session, ids: Any) -> int:
    """Sella como contadas las adopciones cuyo id se pasa. Devuelve cuántas."""
    quedan = [int(i) for i in ids if i is not None]
    if not quedan:
        return 0
    ahora = datetime.now(UTC).replace(tzinfo=None)
    n = 0
    for fila in session.scalars(
        select(LoadAdoption).where(LoadAdoption.id.in_(quedan))
    ).all():
        fila.reported_at = ahora
        n += 1
    session.flush()
    return n


# ---------------------------------------------------------------------------
# Lo que se entrenó de verdad, de vuelta al motor
# ---------------------------------------------------------------------------


def sesiones_ejecutadas(
    session: Session, cfg: Any, *, desde: date, hasta: date
) -> list[Any]:
    """Las sesiones de Hevy ya registradas, como `StrengthSession` del motor.

    ESTO FALTABA ENTERO, Y ERA UN PARÁMETRO MUERTO.
    ----------------------------------------------
    `build_signals` acepta `sessions=` desde el primer día y NADIE se lo pasaba
    nunca: ni `runner.run_daily` ni `cli.py`. El único sitio del proyecto donde
    se construía un `StrengthSession` era `tests/test_signals.py`. El efecto es
    que `intensity_budget` -que sí sabe contar HIIT y fuerza, y tiene el
    interruptor `counts_as_intense.hiit_executed: true` puesto- llevaba toda la
    vida recibiendo una lista vacía.

    Consecuencia concreta, que es la que importa: el presupuesto semanal de
    sesiones intensas solo contaba las salidas de bici. Un HIIT hecho el martes
    no gastaba nada, así que el sábado el sistema creía tener margen para
    recomendar una salida intensa que en realidad ya no cabía. El límite
    existía y se aplicaba sobre un numerador incompleto, que es peor que no
    tener límite: parece que alguien lo está vigilando.

    `is_hiit` sale de `hiit.blocks` del config y no de una lista aparte, para
    que añadir un tercer bloque no exija acordarse de tocar esto también.
    """
    from app.engine.signals import StrengthSession
    from app.integrations.hevy import claves_hiit

    hiit = claves_hiit(cfg)
    return [
        StrengthSession(
            date=f.date,
            routine_key=f.routine_key,
            is_hiit=bool(f.routine_key) and f.routine_key in hiit,
        )
        for f in session.scalars(
            select(WorkoutLog)
            .where(WorkoutLog.date >= desde, WorkoutLog.date <= hasta)
            .order_by(WorkoutLog.date, WorkoutLog.id)
        ).all()
    ]


def entrenos_sin_contar(session: Session) -> list[dict[str, Any]]:
    """Los entrenamientos fuera de plan que ningún mensaje ha contado todavía.

    Mismo patrón que `adopciones_sin_contar`, y por el mismo motivo: leer y
    marcar van separados porque el envío de Telegram se traga sus propios
    fallos. Si se sellaran al leerlos, un Telegram caído dejaría el aviso
    consumido por un mensaje que nunca llegó al móvil, y un entrenamiento que
    el sistema no esperaba se quedaría sin contar para siempre. Sella
    `marcar_entrenos_contados`, y solo cuando hay mensaje.

    Sin límite de antigüedad, también por lo mismo: si el sistema pasó tres
    días sin mandar nada, esos entrenamientos siguen sin comentarse.
    """
    return [
        {
            "id": f.id,
            "day": f.date.isoformat(),
            "routine": f.routine_key,
            "title": f.title,
            "duration_s": f.duration_s,
            "total_sets": f.total_sets,
            "total_volume_kg": f.total_volume_kg,
            "motivo": f.motivo_suelto,
        }
        for f in session.scalars(
            select(WorkoutLog)
            .where(
                WorkoutLog.unplanned.is_(True),
                WorkoutLog.reported_at.is_(None),
            )
            .order_by(WorkoutLog.date, WorkoutLog.id)
        ).all()
    ]


def marcar_entrenos_contados(session: Session, ids: Any) -> int:
    """Sella como contados los entrenamientos cuyo id se pasa. Devuelve cuántos."""
    quedan = [int(i) for i in ids if i is not None]
    if not quedan:
        return 0
    ahora = datetime.now(UTC).replace(tzinfo=None)
    n = 0
    for fila in session.scalars(
        select(WorkoutLog).where(WorkoutLog.id.in_(quedan))
    ).all():
        fila.reported_at = ahora
        n += 1
    session.flush()
    return n


# ---------------------------------------------------------------------------
# El crudo de las APIs, guardado sin que la base engorde sola
# ---------------------------------------------------------------------------

MAX_ELEMENTOS_LISTA = 50
CLAVE_PODAS = "__podado_al_guardar__"
AVISO_TAMANO = 200_000


def podar_crudo(node: Any) -> tuple[Any, list[str]]:
    """Quita del crudo las series largas. Devuelve `(podado, qué se ha quitado)`.

    POR QUÉ SE PODA
    ---------------
    Las respuestas de sueño y de body battery no son un resumen: traen la serie
    por minutos de la noche entera. Guardarlas tal cual son cientos de KB por
    día y decenas de MB en cuanto se rellenan los 190 días de histórico, en un
    servidor de casa y en una tabla que se consulta desde el móvil.

    POR QUÉ SE PODA POR FORMA Y NO POR NOMBRE
    -----------------------------------------
    La tentación es una lista de campos conocidos -`sleepMovement`,
    `bodyBatteryValuesArray`- y quitar esos. Sería exactamente el fallo que este
    proyecto lleva semanas cerrando: el día que Garmin renombre uno, la lista
    deja de reconocerlo, no falla nada, y la tabla empieza a crecer (o a
    guardar) otra cosa sin que nadie se entere. La forma no se renombra: una
    lista de 480 elementos es una serie temporal se llame como se llame.

    QUÉ SOBREVIVE
    -------------
    Todos los escalares, a cualquier profundidad. Ahí es donde vive lo que de
    verdad haría falta dentro de cuatro semanas -fases de sueño en minutos,
    respiración media, el mínimo de body battery-, y por eso el recorte no
    contradice el motivo de guardar el crudo.

    Y de lo que se corta queda constancia doble: una muestra de dos elementos
    (que dice la FORMA de lo cortado, y con eso se sabe si merece la pena
    volver a guardarlo entero) y una línea en `CLAVE_PODAS` con el nombre y
    cuántos elementos tenía. Un recorte que no se anota es un dato que se
    perdió sin dejar rastro, que es el mismo problema con otro nombre.
    """
    cortes: list[str] = []
    return _podar(node, "", cortes), cortes


def _podar(node: Any, ruta: str, cortes: list[str]) -> Any:
    if isinstance(node, dict):
        return {k: _podar(v, f"{ruta}.{k}" if ruta else str(k), cortes) for k, v in node.items()}
    if isinstance(node, (list, tuple)):
        if len(node) > MAX_ELEMENTOS_LISTA:
            cortes.append(f"{ruta or '<raíz>'}: {len(node)} elementos")
            return {
                "__podado__": len(node),
                "__muestra__": [_podar(x, ruta, []) for x in node[:2]],
            }
        return [_podar(x, ruta, cortes) for x in node]
    return node


def crudo_para_guardar(raw: Any, *, etiqueta: str = "") -> str | None:
    """El crudo ya podado y en JSON, listo para una columna `raw_json`."""
    if not raw:
        return None
    podado, cortes = podar_crudo(raw)
    if cortes:
        if isinstance(podado, dict):
            podado[CLAVE_PODAS] = cortes
        else:
            podado = {"valor": podado, CLAVE_PODAS: cortes}
    texto = json.dumps(podado, ensure_ascii=False, default=str, sort_keys=True)
    if len(texto) > AVISO_TAMANO:
        # No se recorta: recortar por tamaño sería tirar datos por un criterio
        # que no significa nada. Pero que una fila pase de 200 KB DESPUÉS de
        # podar quiere decir que la respuesta ha cambiado de forma, y eso hay
        # que verlo antes de que sean 190 filas así.
        log.warning(
            "crudo de %s: %d KB después de podar. Revisa si la respuesta ha "
            "cambiado de forma", etiqueta or "una API", len(texto) // 1024,
        )
    return texto


# ---------------------------------------------------------------------------
# Lo que se lee de Garmin, guardado
# ---------------------------------------------------------------------------
#
# POR QUÉ ESTAS DOS FUNCIONES EXISTEN
# -----------------------------------
# `daily_metrics` y `activities` llevaban desde el primer día declaradas en
# `models.py` y sin que NADIE las escribiera. El sistema leía las métricas de
# Garmin cada mañana, decidía con ellas y las tiraba. Sobrevivía una copia
# parcial dentro de `decisions.inputs_snapshot_json` -solo de los días en que
# hubo decisión, y solo de las señales que el motor evalúa-, y las salidas
# clasificadas ni eso: `Signals.rides` está fuera de `values` a propósito, así
# que la clasificación (suave/media/intensa) no entraba en el snapshot.
#
# Eso no daba ningún error. Simplemente, el día que se quiera responder a
# "¿cuántos días de HRV cuesta una salida intensa?" no habrá contra qué
# responder, y no se podrá arreglar hacia atrás: un día que no se guarda no se
# recupera. Ver `docs/analisis.md`.
#
# LA REGLA QUE GOBIERNA LAS DOS: UN `None` NUEVO NO PISA UN DATO VIEJO
# --------------------------------------------------------------------
# Estas funciones se llaman con una ventana de varios días, todas las mañanas,
# así que cada día se reescribe unas cuantas veces. Si Garmin contesta hoy y
# mañana falla la llamada de HRV de ese mismo día -un 429, un timeout-, la
# segunda pasada traería `hrv=None`. Copiarlo encima borraría el dato bueno sin
# un solo error: la fila seguiría ahí, con un hueco, indistinguible de un día en
# que el reloj no se llevó puesto.


def upsert_daily_metrics(
    session: Session,
    metrics: Any,
    *,
    errores: dict[date, list[str]] | None = None,
    recuperado: bool = False,
) -> int:
    """Guarda la ventana de wellness. Devuelve cuántos días se han tocado.

    Aquí había un tercer argumento, `loads`, con las cargas acumuladas que
    acababa de calcular el motor. La idea era buena -guardar EXACTAMENTE la
    carga con la que se decidió, no una recalculada después- pero la ejecución
    tenía dos agujeros que juntos la volvían peor que no tenerla: solo lo
    pasaba la pasada diaria, así que el backfill dejó los 179 días a NULL, y no
    leía esas columnas NADIE. Una serie con seis meses vacíos y un escalón el
    día que arranca el sistema es una invitación a leer "antes no entrenaba".
    Las reglas siguen viendo `load_3d`: se recalcula cada mañana sumando las
    actividades, que es de donde salía también este número.

    `errores` son las lecturas que FALLARON ese día, si se sabe cuáles. Sin
    ellas, un 500 de Garmin y una noche sin reloj acaban los dos en la misma
    fila con un hueco y `fetch_status='partial'`, y eso importa más de lo que
    parece: la primera se arregla volviendo a pedirla y la segunda no. La
    ventana diaria disimulaba la diferencia porque relee siete días cada mañana
    y acaba rellenando sola; el backfill visita cada día UNA vez, así que ahí no
    hay segunda oportunidad si nadie apunta cuál fue cuál.

    Los tres estados, y qué hacer con cada uno:

        ok       está todo lo que se pidió. No se vuelve.
        partial  se pidió, contestó, y no había dato. Tampoco se vuelve: Garmin
                 no va a inventarlo mañana.
        error    no se pudo leer. Es el único que hay que reintentar, y por eso
                 `app.backfill.dias_pendientes` lo trata como si no hubiera fila.

    `recuperado` marca la fila como rellenada a posteriori (`recovered_at`). Se
    pone solo cuando la fila se CREA en un backfill: si el día ya tenía fila
    escrita en su momento, era una lectura del día y sigue siéndolo.
    """
    from app.models import DailyMetrics

    ahora = datetime.now()

    campos = ("hrv", "rhr", "sleep_min", "sleep_score", "body_battery")
    tocados = 0

    for m in metrics or []:
        dia = getattr(m, "date", None)
        if dia is None:
            continue

        fila = session.scalars(
            select(DailyMetrics).where(DailyMetrics.date == dia)
        ).first()
        if fila is None:
            fila = DailyMetrics(date=dia)
            if recuperado:
                fila.recovered_at = ahora
            session.add(fila)

        for campo in campos:
            nuevo = getattr(m, campo, None)
            if nuevo is not None:
                setattr(fila, campo, nuevo)

        # El crudo sigue la MISMA regla que los seis números: una pasada que no
        # lo trae no borra el que ya había. Si mañana la llamada de sueño falla,
        # la respuesta buena de hoy se queda donde está.
        crudo = crudo_para_guardar(getattr(m, "raw", None), etiqueta="wellness")
        if crudo is not None:
            fila.raw_json = crudo

        # `partial` no es cosmético: es la diferencia entre "esa noche no dormí
        # con el reloj" y "esa mañana Garmin no contestó". Sin la marca, los dos
        # casos son la misma fila con un hueco, y el segundo se podría reintentar
        # mientras que el primero no.
        #
        # Aquí se descontaban además las métricas que no se habían PEDIDO, que
        # eran una: training readiness. Contarla como hueco dejaba todas las
        # filas en `partial` para siempre -una marca que sale en el 100% de los
        # casos ya no distingue nada- y por eso existía la excepción. Ahora
        # readiness no existe, los cinco campos se piden los cinco, y la
        # excepción ha desaparecido con lo que la justificaba.
        huecos = [c for c in campos if getattr(fila, c, None) is None]
        fallos = list((errores or {}).get(dia) or [])
        partes = []
        if fallos:
            partes.append("no se pudo leer: " + " | ".join(fallos))
        if huecos:
            partes.append("sin dato de: " + ", ".join(huecos))
        fila.fetch_status = "error" if fallos else ("partial" if huecos else "ok")
        fila.fetch_error = "; ".join(partes) or None
        tocados += 1

    session.flush()
    return tocados


# Lo que `upsert_activities` copia tal cual de la salida. Está aquí fuera y no
# inline en el bucle para que el test que busca huecos lea LA MISMA lista que
# se recorre, y no una copia suya que puede quedarse atrás sin que se note.
CAMPOS_ACTIVIDAD = (
    "duration_s",
    "distance_m",
    "training_load",
    "aerobic_te",
    "anaerobic_te",
    # De aquí abajo no lo usa el motor, lo usa la vista 5. Los tres primeros
    # estaban en el modelo y en el análisis pero no en esta lista, así que las
    # tres columnas se quedaban siempre a NULL: el desnivel se calculaba, se
    # guardaba, se comparaba contra el histórico y se contaba en el mensaje de
    # Telegram, todo sobre una columna que no escribía nadie. De ahí el test.
    "elevation_gain_m",
    "moving_duration_s",
    "avg_hr",
    # El resto viene del inventario del crudo: Garmin mandaba estos diez
    # campos en cada salida y se tiraban todos. No se añaden "por si acaso"
    # -eso es justo lo que se quitó- sino porque hay preguntas concretas que
    # hoy no se pueden contestar: cuánto subió el pulso de pico, a qué
    # velocidad, cuánto se bajó, y si julio fue el calor.
    "elevation_loss_m",
    "max_hr",
    "avg_speed_mps",
    "max_speed_mps",
    "calories",
    "avg_respiration",
    "max_respiration",
    "min_respiration",
    "max_temp_c",
    "min_temp_c",
)

# Las demás columnas de `activities`, cada una escrita en su sitio: la clave y
# la fecha al crear la fila, las zonas en su propio bucle, la clasificación al
# final. `id` es autoincremental y `fetched_at` tiene defecto del servidor.
#
# A mano a propósito, por el mismo motivo que `CAMPOS_PERSISTIDOS`: si se
# generara del modelo, una columna nueva entraría sola en la lista y el test
# dejaría de proteger nada. Lo que se quiere es justo lo contrario, que añadir
# una columna obligue a decir aquí quién la escribe.
COLUMNAS_ACTIVIDAD_APARTE = frozenset(
    {
        "id",
        "fetched_at",
        "garmin_activity_id",
        "date",
        "name",
        "is_cycling",
        "hr_zone_1_s",
        "hr_zone_2_s",
        "hr_zone_3_s",
        "hr_zone_4_s",
        "hr_zone_5_s",
        "training_load_estimated",
        "intensity_level",
        "classification_source",
    }
)


def columnas_actividad_sin_escribir() -> set[str]:
    """Columnas de `activities` que no escribe nadie. Debe estar vacío."""
    from app.models import Activity

    todas = {c.name for c in Activity.__table__.columns}
    return todas - set(CAMPOS_ACTIVIDAD) - COLUMNAS_ACTIVIDAD_APARTE


def upsert_activities(session: Session, classified: Any) -> int:
    """Guarda las salidas YA CLASIFICADAS. Devuelve cuántas se han tocado.

    Se guarda la clasificación (`intensity_level`, `classification_source`,
    `training_load`, `training_load_estimated`) y no solo los datos crudos,
    porque la clasificación depende de los umbrales del `config.yaml` del día en
    que se hizo. Reclasificar dentro de seis semanas con un YAML ya cambiado
    daría otras etiquetas, y entonces "el lumbar sube después de una salida
    intensa" se estaría midiendo contra unas intensas que en su momento no lo
    fueron.

    El crudo de Garmin NO se copia aquí: vive en `data/cache/activities.json`,
    que se fusiona y nunca se poda, así que ya está a salvo. Lo que no está en
    ningún otro sitio es esto.
    """
    from app.models import Activity

    tocadas = 0
    sin_id = 0

    for c in classified or []:
        ride = getattr(c, "ride", c)
        aid = getattr(ride, "activity_id", None)
        if aid is None:
            # Solo las salidas sintéticas de los tests llegan sin id; las de
            # Garmin siempre lo traen. No se inventa una clave: dos salidas sin
            # id el mismo día se fundirían en una y la carga del día bajaría.
            sin_id += 1
            continue

        fila = session.scalars(
            select(Activity).where(Activity.garmin_activity_id == int(aid))
        ).first()
        if fila is None:
            fila = Activity(garmin_activity_id=int(aid), date=ride.date)
            session.add(fila)

        fila.date = ride.date
        fila.name = getattr(ride, "name", None) or fila.name
        fila.is_cycling = bool(getattr(ride, "is_cycling", True))
        for campo in CAMPOS_ACTIVIDAD:
            nuevo = getattr(ride, campo, None)
            if nuevo is not None:
                setattr(fila, campo, nuevo)

        zonas = getattr(ride, "zones", None) or ()
        for i, segundos in enumerate(zonas[:5], start=1):
            if segundos is not None:
                setattr(fila, f"hr_zone_{i}_s", float(segundos))

        fila.intensity_level = getattr(c, "level", None) or fila.intensity_level
        fila.classification_source = (
            getattr(c, "source", None) or fila.classification_source
        )
        # La carga estimada SÍ se guarda, pero marcada. Un número estimado y uno
        # medido no se pueden promediar como si fueran lo mismo, y sin la marca
        # nadie sabría cuáles eran cuáles.
        if getattr(c, "load_known", True) and getattr(c, "load", None) is not None:
            fila.training_load = float(c.load)
            fila.training_load_estimated = bool(getattr(c, "load_estimated", False))
        tocadas += 1

    if sin_id:
        log.info("%d salida(s) sin activity_id: no se guardan en `activities`", sin_id)

    session.flush()
    return tocadas


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
