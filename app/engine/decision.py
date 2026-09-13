"""Orquestador del motor: señales -> semáforo -> reglas -> progresión -> sesión.

Este módulo es la única pieza que conoce el orden completo. Los demás módulos
saben hacer su trabajo pero no cuándo les toca; aquí se decide eso y solo eso.

TRES INVARIANTES QUE NO SE NEGOCIAN
-----------------------------------
1. **Es una función pura.** No abre la base de datos, no llama a Garmin, a Hevy
   ni a Telegram. Entra un `Signals` ya construido y un `EngineState` con lo
   que se recuerda de días anteriores; sale un `DayDecision`. Todo el I/O vive
   en las integraciones. Esto es lo que hace que `--dry-run` sea el MISMO
   código que la ejecución real y no una imitación que se desincroniza: la
   diferencia entre ensayo y función está en quién persiste la salida, no en
   cómo se calcula.

2. **El estado entra explícito.** Nada de leer el reloj ni de consultar de
   tapadillo. Si una decisión depende del pasado, ese pasado está en
   `EngineState` y por tanto es reproducible: los mismos datos de entrada dan
   siempre la misma salida. Es la condición para que `inputs_snapshot_json`
   sirva de algo cuando dentro de tres semanas haya que explicar un ámbar.

3. **El ámbito del estado es RUTINA+EJERCICIO.** La plancha lateral del Día 1 y
   la del Día 3 son dosis distintas y no comparten racha. `plan_progression`
   recibe diccionarios indexados solo por ejercicio, así que la proyección de
   `(rutina, ejercicio) -> ejercicio` se hace aquí, que es el único sitio que
   sabe qué rutina se está planificando hoy.

ORDEN DE LAS OPERACIONES
------------------------
El orden importa y estos son los motivos:

  semáforo    -> antes que todo: gobierna la progresión y el tipo de sesión.
  reglas esp. -> después del semáforo porque una de ellas (`semana_de_descarga`)
                 puede necesitar saber si hoy es rojo para decidir el jitter,
                 y antes de la progresión porque la descarga la congela.
  progresión  -> solo si hoy hay fuerza. Es lo único que PERSISTE en el YAML.
  sesión      -> aplica progresión, descarga, recortes y ámbar en su orden.
  bici        -> al final: es independiente de la fuerza salvo por el semáforo.

Lo transitorio y lo permanente no se mezclan: la progresión modifica la rutina
para siempre, el recorte de un ámbar solo vale para hoy y se olvida mañana.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Sequence

from app.engine.bike_advisor import BikeRecommendation, recommend_bike
from app.engine.progression import ProgressionPlan, plan_progression
from app.engine.rules import COMPARISONS, LightDecision, RuleError, evaluate_light
from app.engine.session_builder import BuiltSession, build_session, today_plan
from app.engine.signals import Signals, WEEKDAY_NAMES, week_start

DELOAD_RULE = "semana_de_descarga"


# ---------------------------------------------------------------------------
# Estado que el motor recuerda entre días
# ---------------------------------------------------------------------------


@dataclass
class ActiveRule:
    """Una regla especial con efecto que dura más de un día.

    `entity` es a qué se aplica: una clave de ejercicio, una rutina, o "*" para
    global. Se corresponde con la tabla `rule_states`.
    """

    name: str
    action: dict[str, Any] = field(default_factory=dict)
    active_from: date | None = None
    active_until: date | None = None  # inclusive; None = indefinida
    entity: str = "*"
    reason: str = ""
    notify: bool = False

    def covers(self, day: date) -> bool:
        if self.active_from and day < self.active_from:
            return False
        if self.active_until and day > self.active_until:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "entity": self.entity,
            "active_from": self.active_from.isoformat() if self.active_from else None,
            "active_until": self.active_until.isoformat() if self.active_until else None,
            "reason": self.reason,
            "action": self.action,
        }


@dataclass
class EngineState:
    """Todo lo que el motor necesita recordar de días anteriores.

    Las claves de `clean_sessions` y `compliance` son tuplas
    `(rutina, ejercicio)`. No es un capricho: si fuesen solo el ejercicio, la
    plancha lateral del Día 1 y la del Día 3 compartirían racha y una de las
    dos progresaría con el mérito de la otra.
    """

    clean_sessions: dict[tuple[str, str], int] = field(default_factory=dict)
    compliance: dict[tuple[str, str], bool] = field(default_factory=dict)
    # Las series efectivas VIGENTES de cada (rutina, ejercicio). Es la carga
    # real de hoy, no la de partida: `config.yaml` solo se usa como semilla
    # mientras esto esté vacío para ese ejercicio. Ver `ExerciseTarget`.
    current_sets: dict[tuple[str, str], list[dict[str, Any]]] = field(
        default_factory=dict
    )
    # Sesiones de cada rutina desde que ese ejercicio progresó por última vez.
    # Es el turno en la cola cuando hay más candidatos que cupo. Ver el
    # comentario largo de `ExerciseTarget.sessions_since_progress`: sin esto,
    # los últimos ejercicios de una rutina larga no suben nunca.
    sessions_since_progress: dict[tuple[str, str], int] = field(default_factory=dict)
    # Sesiones SEGUIDAS levantando menos peso del que pedía el plan del día, y
    # la mejor de esas sesiones. Las dos son la memoria de la adopción hacia
    # abajo (`app/engine/adoption.py`): abajo no se adopta a la primera, porque
    # una sesión más floja casi siempre es la máquina ocupada, y hace falta
    # recordar cuántas van y cuál fue la mejor para no fijar el suelo en un día
    # malo. Se vacían en cuanto una sesión alcanza lo pedido.
    below_plan_streak: dict[tuple[str, str], int] = field(default_factory=dict)
    below_plan_best_kg: dict[tuple[str, str], float] = field(default_factory=dict)
    # Semáforo del día en que se hizo por última vez CADA rutina. Es el reloj
    # de los frenos de volumen: un rojo en lunes no cancela el viernes.
    last_routine_light: dict[str, str | None] = field(default_factory=dict)
    active_rules: list[ActiveRule] = field(default_factory=list)
    pending_strength: tuple[str, date] | None = None
    program_start: date | None = None
    # Inicio de la última semana de descarga ya concedida. Sin esto el jitter
    # no tiene memoria y la descarga podría repetirse o saltarse.
    last_deload_start: date | None = None

    def for_routine(
        self, routine_key: str, keys: list[str]
    ) -> tuple[dict[str, bool | None], dict[str, int]]:
        """Proyecta el estado (rutina, ejercicio) al formato que espera
        `plan_progression`, que indexa solo por ejercicio.

        `compliance` sale con TRES valores, no dos:

            True  -> en la última sesión se completaron todas las series
                     efectivas de ese ejercicio.
            False -> no se completaron.
            None  -> nadie ha reconciliado nunca ese ejercicio.

        El `None` no es un detalle de tipos. Esto ponía `default=True`, es
        decir: "no tengo ni un registro de este ejercicio" se convertía en
        "la última sesión fue perfecta". La puerta de progresión pide
        exactamente ese dato para subir carga, así que en una instalación
        recién estrenada -o en cualquier ejercicio nuevo dentro de una rutina
        vieja- el peso subía apoyado en una sesión que no existe.

        `plan_progression` y `evaluate_gate` ya sabían tratar el `None`
        (`"no hay registro de la última sesión con el que comparar"`); era
        esta línea la que lo destruía antes de que llegase hasta ellos.

        `clean_sessions` sí conserva el `0` por defecto, y no es una
        incoherencia: cero sesiones limpias es un valor honesto que CIERRA la
        puerta, mientras que un `True` inventado la ABRE.
        """
        clean = {k: int(self.clean_sessions.get((routine_key, k), 0)) for k in keys}
        comp: dict[str, bool | None] = {}
        for k in keys:
            valor = self.compliance.get((routine_key, k))
            comp[k] = None if valor is None else bool(valor)
        return comp, clean


@dataclass
class DeloadStatus:
    """Por qué hoy es (o no es) semana de descarga."""

    active: bool
    reason: str
    start: date | None = None
    end: date | None = None
    shifted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "reason": self.reason,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "shifted": self.shifted,
        }


@dataclass
class DayDecision:
    """La decisión completa de un día. Es lo que se persiste y lo que se cuenta."""

    day: date
    light: str
    trigger_rule: str | None
    signals: Signals
    light_decision: LightDecision
    session: BuiltSession
    deload: DeloadStatus
    # La rutina de fuerza que TOCABA hoy por calendario, se haya ejecutado o
    # no. En un día rojo `session.routine_key` apunta al bloque de
    # recuperación, así que sin este campo no habría forma de saber qué queda
    # pendiente de recuperar.
    calendar_routine: str | None = None
    active_rules: list[ActiveRule] = field(default_factory=list)
    progression: ProgressionPlan | None = None
    bike: BikeRecommendation | None = None
    notes: list[str] = field(default_factory=list)
    config_hash: str | None = None
    source: str = "checkin"
    # La sesión aplazada que hoy ha caducado, si la hay: (rutina, día en que se
    # aplazó). Va en la decisión y no en `EngineState` porque no es algo que se
    # recuerde, es algo que ha pasado HOY y hay que contar. `advance_state` la
    # usa para borrar el aplazamiento, y `to_dict` para que quede en el
    # histórico: dentro de tres semanas, "esa semana entrené una vez menos" se
    # explica aquí o no se explica.
    expired_deferral: tuple[str, date] | None = None
    # Adopciones de carga de la reconciliación de ANOCHE, ya en formato de
    # diccionario (`Adopcion.to_dict`). No las produce `decide`: las cuelga
    # `run_daily` justo antes de redactar el mensaje, leyéndolas de la base.
    #
    # Van aquí y no se cuentan la noche que pasan porque a las 22:30 no hay
    # nadie leyendo el móvil, y porque el sitio donde importan es el mensaje de
    # la mañana: es el que dice "hip thrust: 3x8 a 62,5" y tiene que poder
    # explicar por qué 62,5 y no lo de ayer.
    load_adoptions: list[dict[str, Any]] = field(default_factory=list)
    # Los entrenamientos que están en Hevy y no emparejaban con el plan de su
    # día: un HIIT, una rutina que no está en el config, algo hecho un sábado.
    # Mismo mecanismo y mismo motivo que `load_adoptions`: los cuelga
    # `run_daily` leyendo de la base, y se sellan como contados solo si el
    # mensaje llega a salir.
    entrenos_sueltos: list[dict[str, Any]] = field(default_factory=list)
    # La lectura de segundo orden del día: rachas, motivo dominante y el último
    # mes contra el trimestre. Un `Tendencia` de `app/engine/tendencia.py`.
    #
    # Tampoco la produce `decide`, y por el mismo motivo que `load_adoptions`:
    # necesita el histórico de decisiones, que está en la base de datos, y
    # `decide` es una función pura que no la abre. La cuelga `run_daily`.
    #
    # Que sea un campo opcional y no un parámetro de `decide` es además lo que
    # permite que `scripts/replay_semaforo.py` la calcule sobre decisiones que
    # nunca existieron -las que el motor HABRÍA tomado- sin construir sesiones ni
    # progresiones que no tendrían sentido en un pasado que no se vivió.
    tendencia: Any = None
    # La cuenta hacia la próxima revisión de umbrales. Un `Recalibracion` de
    # `app/engine/recalibracion.py`, colgado por `run_daily` igual que los dos
    # campos de arriba y por el mismo motivo: necesita saber cuántos días con
    # decisión hay guardados, y eso está en la base de datos.
    #
    # No entra en ninguna decisión ni la cambia. Es mantenimiento del sistema
    # contado por el único canal que se lee todos los días.
    recalibracion: Any = None

    @property
    def weekday(self) -> str:
        return WEEKDAY_NAMES[self.day.weekday()]

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "weekday": self.weekday,
            "light": self.light,
            "trigger_rule": self.trigger_rule,
            "calendar_routine": self.calendar_routine,
            "source": self.source,
            "config_hash": self.config_hash,
            "inputs": self.signals.snapshot(),
            "fired_rules": [r.to_dict() for r in self.light_decision.fired],
            "skipped_rules": [r.to_dict() for r in self.light_decision.skipped],
            "deload": self.deload.to_dict(),
            "active_rules": [r.to_dict() for r in self.active_rules],
            "session": self.session.to_dict(),
            "bike": self.bike.to_dict() if self.bike else None,
            "progression": _progression_dict(self.progression),
            "notes": self.notes,
            "expired_deferral": (
                {
                    "routine": self.expired_deferral[0],
                    "deferred_from": self.expired_deferral[1].isoformat(),
                }
                if self.expired_deferral
                else None
            ),
            "load_adoptions": list(self.load_adoptions),
            "entrenos_sueltos": list(self.entrenos_sueltos),
            "tendencia": self.tendencia.to_dict() if self.tendencia else None,
            "recalibracion": (
                self.recalibracion.to_dict() if self.recalibracion else None
            ),
        }


def _progression_dict(plan: ProgressionPlan | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    return {
        "routine": plan.routine_key,
        "gate_open": plan.gate_open,
        "gate_reason": plan.gate_reason,
        "sets_allowed": plan.sets_allowed,
        "sets_reason": plan.sets_reason,
        "reps_allowed": plan.reps_allowed,
        "reps_reason": plan.reps_reason,
        "changes": [
            {
                "key": e.key,
                "name": e.name,
                "mode": e.mode,
                "kind": e.kind,
                "text": e.text(),
            }
            for e in plan.changes
        ],
        "blocked": [
            {"key": e.key, "name": e.name, "reason": e.blocked_by}
            for e in plan.exercises
            if not e.changed and e.blocked_by
        ],
    }


# ---------------------------------------------------------------------------
# Reglas especiales
# ---------------------------------------------------------------------------


def _trigger_fires(trigger: dict[str, Any], signals: Signals, day: date) -> bool:
    """¿Se cumple el disparador de una regla especial?

    Solo entiende disparadores por señal (`source` + `when` + opcionalmente
    `consecutive_days`). El disparador de calendario (`every_n_weeks`) lo lleva
    `_deload_status`, porque necesita memoria de la última descarga concedida y
    eso no cabe en una comparación puntual.
    """
    source = trigger.get("source")
    if not source:
        return False

    when = trigger.get("when") or {}
    need = int(trigger.get("consecutive_days", 1))

    series = signals.series(source)

    def value_at(d: date) -> Any:
        if d == day:
            v = signals.get(source)
            if v is not None:
                return v
        return series.get(d)

    for i in range(need):
        v = value_at(day - timedelta(days=i))
        if v is None:
            # Sin dato no se dispara. Una regla que retira un ejercicio 14 días
            # no puede activarse por un hueco en los datos.
            return False
        for op, operand in when.items():
            fn = COMPARISONS.get(op)
            if fn is None:
                # Una errata aquí era peor que en un freno: como ninguna rama
                # coincidía, no se devolvía False y la regla se daba por
                # cumplida. `gtee: 7` habría retirado el peso muerto TODOS los
                # días, con la única condición de que hubiera algún dato.
                raise RuleError(
                    f"regla especial: operador desconocido '{op}'. "
                    f"Válidos: {', '.join(sorted(COMPARISONS))}"
                )
            if not fn(v, operand):
                return False
    return True


def evaluate_special_rules(
    config: Any,
    signals: Signals,
    day: date,
    state: EngineState,
) -> tuple[list[ActiveRule], list[str]]:
    """Reglas especiales vigentes hoy: las que siguen corriendo y las nuevas.

    Una regla con `duration_days: 14` que disparó hace tres días sigue vigente
    hoy aunque hoy la molestia lumbar sea 0. Ese es justo su propósito: dar un
    margen de descanso que no se cancele al primer día que uno se encuentra
    bien. Por eso primero se arrastran las vigentes y solo después se miran los
    disparadores.

    Re-disparar una regla ya vigente EXTIENDE su ventana, no la duplica.
    """
    raw = config.raw if hasattr(config, "raw") else config
    notes: list[str] = []

    # 1. las que vienen de días anteriores y aún no han caducado
    active: dict[str, ActiveRule] = {}
    for r in state.active_rules:
        if r.name == DELOAD_RULE:
            continue  # la descarga la gobierna `_deload_status`, no la herencia
        if r.covers(day):
            active[r.name] = copy.deepcopy(r)
        else:
            notes.append(f"regla '{r.name}' caducada el {r.active_until}")

    # 2. las que disparan hoy
    for rule in raw.get("special_rules", []) or []:
        name = str(rule.get("name", ""))
        if name == DELOAD_RULE:
            continue
        trigger = rule.get("trigger") or {}
        if not _trigger_fires(trigger, signals, day):
            continue

        action = rule.get("action") or {}
        dur = int(action.get("duration_days", 1))
        until = day + timedelta(days=dur - 1)
        src = trigger.get("source", "")
        reason = f"{src} cumplió {trigger.get('when')} durante {trigger.get('consecutive_days', 1)} día(s)"

        prev = active.get(name)
        if prev is not None:
            # Ya estaba vigente: se extiende la cola, no se crea otra.
            if prev.active_until is None or until > prev.active_until:
                prev.active_until = until
                notes.append(f"regla '{name}' extendida hasta {until}")
            continue

        active[name] = ActiveRule(
            name=name,
            action=action,
            active_from=day,
            active_until=until,
            reason=reason,
            notify=bool(rule.get("notify", False)),
        )
        notes.append(f"regla '{name}' ACTIVADA hasta {until} ({reason})")

    return sorted(active.values(), key=lambda r: r.name), notes


def _deload_status(
    config: Any,
    day: date,
    state: EngineState,
    light: str,
) -> DeloadStatus:
    """¿Hoy es semana de descarga?

    La descarga es de calendario, no de síntomas: toca cada `every_n_weeks`
    contando desde `program_start`. `jitter_weeks` permite retrasarla una
    semana si la semana que le tocaba arranca en ROJO.

    El motivo del jitter: una descarga que empieza el mismo día que el cuerpo
    ya ha forzado una parada no descarga nada, porque esa semana iba a ser
    suave de todos modos. Retrasarla un lunes hace que caiga sobre una semana
    en la que de verdad haya algo que recortar.

    Solo se retrasa, nunca se adelanta: adelantar exigiría saber que la semana
    que viene será mala, y eso no se sabe. `jitter_weeks` es por tanto un
    margen hacia adelante, y así está documentado en el YAML.
    """
    raw = config.raw if hasattr(config, "raw") else config
    rule = next(
        (r for r in raw.get("special_rules", []) or [] if r.get("name") == DELOAD_RULE),
        None,
    )
    if rule is None:
        return DeloadStatus(False, "no hay regla de semana de descarga en el config")

    trigger = rule.get("trigger") or {}
    every = int(trigger.get("every_n_weeks", 0) or 0)
    if every <= 0:
        return DeloadStatus(False, "la descarga automática está desactivada")

    start_ref = state.program_start
    if start_ref is None:
        return DeloadStatus(
            False,
            "sin `program_start`: no hay origen desde el que contar las semanas",
        )

    action = rule.get("action") or {}
    dur = int(action.get("duration_days", 7))
    jitter = int(trigger.get("jitter_weeks", 0) or 0)

    origin = week_start(start_ref)
    this_week = week_start(day)
    weeks = (this_week - origin).days // 7
    if weeks < 0:
        return DeloadStatus(False, "el programa todavía no ha empezado")

    # ¿Ya se concedió una descarga que aún cubre hoy?
    if state.last_deload_start is not None:
        end = state.last_deload_start + timedelta(days=dur - 1)
        if state.last_deload_start <= day <= end:
            return DeloadStatus(
                True,
                f"semana de descarga en curso desde el {state.last_deload_start}",
                start=state.last_deload_start,
                end=end,
            )
        # Una descarga reciente bloquea la siguiente hasta cumplir el ciclo.
        if (this_week - week_start(state.last_deload_start)).days // 7 < every:
            return DeloadStatus(
                False,
                f"última descarga el {state.last_deload_start}: aún no toca",
            )

    if weeks == 0 or weeks % every != 0:
        nxt = origin + timedelta(weeks=((weeks // every) + 1) * every)
        return DeloadStatus(
            False, f"no toca esta semana (la siguiente empieza el {nxt})"
        )

    # Toca. ¿La aplazamos por el jitter?
    if jitter > 0 and light == "red" and day.weekday() == 0:
        return DeloadStatus(
            False,
            "tocaba descarga pero la semana arranca en ROJO: se retrasa una "
            "semana, porque descargar sobre una semana ya frenada no descarga nada",
            shifted=True,
        )

    start = this_week
    return DeloadStatus(
        True,
        f"toca descarga: {weeks} semanas desde el inicio del programa (cada {every})",
        start=start,
        end=start + timedelta(days=dur - 1),
    )


# ---------------------------------------------------------------------------
# Entrada principal
# ---------------------------------------------------------------------------


def decide(
    config: Any,
    day: date,
    signals: Signals,
    state: EngineState | None = None,
    source: str = "checkin",
) -> DayDecision:
    """Decide el día completo.

    `signals` ya viene construido por `build_signals` a partir de Garmin, Hevy
    y el check-in. `state` es lo que el motor recuerda; si falta se asume un
    arranque en frío, que es un estado válido y no un error: el primer día del
    sistema no tiene rachas ni reglas vigentes.
    """
    raw = config.raw if hasattr(config, "raw") else config
    state = state or EngineState()
    notes: list[str] = []

    # --- 1. semáforo --------------------------------------------------------
    light_decision = evaluate_light(config, signals)
    light = light_decision.light

    # --- 2. reglas especiales y descarga ------------------------------------
    active_rules, rule_notes = evaluate_special_rules(config, signals, day, state)
    notes.extend(rule_notes)

    deload = _deload_status(config, day, state, light)
    if deload.active:
        dl_rule = next(
            (r for r in raw.get("special_rules", []) or []
             if r.get("name") == DELOAD_RULE),
            None,
        )
        if dl_rule is not None:
            active_rules.append(
                ActiveRule(
                    name=DELOAD_RULE,
                    action=dl_rule.get("action") or {},
                    active_from=deload.start,
                    active_until=deload.end,
                    reason=deload.reason,
                    notify=bool(dl_rule.get("notify", False)),
                )
            )
    notes.append(f"descarga: {deload.reason}")

    # --- 3. progresión ------------------------------------------------------
    # Solo si hoy hay fuerza. Sin rutina no hay nada que progresar, y llamar a
    # `plan_progression` con routine_key=None inventaría un plan vacío que
    # luego habría que distinguir de "plan que no subió nada", que es distinto.
    plan_today = today_plan(config, day)
    calendar_routine = plan_today.get("strength")
    routine_key = calendar_routine

    # La sesión aplazada por un rojo se recupera en el primer verde libre.
    # `build_session` toma la decisión final, pero la progresión necesita saber
    # QUÉ rutina se va a planificar, así que se replica el criterio aquí.
    #
    # La caducidad se mira SIEMPRE, no solo en los días verdes y libres. Antes
    # colgaba de ese `if`, y por eso un aplazamiento podía caducar sin que nadie
    # llegara nunca a comprobarlo: bastaba con que los días siguientes tocara
    # bici o el semáforo no fuese verde, que es justo lo que pasa cuando se
    # arrastra una mala racha. La sesión se perdía en el único escenario en el
    # que de verdad importa saberlo.
    expired_deferral: tuple[str, date] | None = None
    if state.pending_strength:
        pkey, pday = state.pending_strength
        expires = int(
            ((raw.get("actions", {}) or {}).get("red", {}) or {}).get(
                "defer_expires_days", 7
            )
        )
        if (day - pday).days > expires:
            # Solo el dato estructurado. El texto lo redacta `message.py`, que
            # es quien sabe a quién se lo está contando, y así no hay dos
            # frases distintas para el mismo hecho ni que deduplicarlas luego.
            expired_deferral = (pkey, pday)
        elif routine_key is None and light == "green" and not plan_today.get("bike"):
            routine_key = pkey
            notes.append(f"se recupera la sesión '{pkey}' aplazada el {pday}")

    progression: ProgressionPlan | None = None
    if routine_key:
        exercises = (raw.get("routines", {}) or {}).get(routine_key, {}).get(
            "exercises", []
        ) or []
        keys = [e["key"] for e in exercises]
        compliance, clean = state.for_routine(routine_key, keys)
        progression = plan_progression(
            config,
            routine_key,
            signals,
            light,
            compliance=compliance,
            clean_sessions=clean,
            deload_active=deload.active,
            last_routine_light=state.last_routine_light.get(routine_key),
            current_sets=state.current_sets,
            # La cola de los cupos, acotada a esta rutina: `plan_progression`
            # trabaja con claves de ejercicio a secas.
            sessions_since_progress={
                k: v
                for (rk, k), v in state.sessions_since_progress.items()
                if rk == routine_key
            },
        )

    # --- 4. sesión ----------------------------------------------------------
    session = build_session(
        config,
        day,
        light,
        progression=progression,
        # `session_builder` espera {name, action}, con la acción anidada. No se
        # aplana: `apply_rule_load_cuts` busca `rule["action"]["reduce_load"]`.
        active_rules=[{"name": r.name, "action": r.action} for r in active_rules],
        deload_active=deload.active,
        pending_strength=state.pending_strength,
        program_start=state.program_start,
        current_sets=state.current_sets,
    )

    # --- 5. bici ------------------------------------------------------------
    bike = recommend_bike(config, signals, light)

    return DayDecision(
        day=day,
        light=light,
        trigger_rule=light_decision.trigger_rule,
        signals=signals,
        light_decision=light_decision,
        session=session,
        deload=deload,
        calendar_routine=calendar_routine,
        active_rules=active_rules,
        progression=progression,
        bike=bike,
        notes=notes,
        config_hash=getattr(config, "hash", None),
        source=source,
        expired_deferral=expired_deferral,
    )


# ---------------------------------------------------------------------------
# Avance del estado
# ---------------------------------------------------------------------------


def advance_state(
    state: EngineState,
    decision: DayDecision,
    executed: dict[str, bool] | None = None,
) -> EngineState:
    """Estado del día siguiente, dado lo que ha pasado hoy.

    `executed` dice, por ejercicio, si se completaron todas las series
    efectivas a las reps objetivo. En producción sale de leer la sesión en
    Hevy; en simulación se genera. Si es None se asume que la sesión no se ha
    ejecutado todavía y el estado no avanza: es lo correcto a las 7 de la
    mañana, cuando la decisión se toma pero el entrenamiento aún no ha pasado.

    Devuelve un objeto NUEVO. El estado es inmutable de cara al llamante para
    que una decisión no pueda contaminar hacia atrás la que la produjo.
    """
    new = EngineState(
        clean_sessions=dict(state.clean_sessions),
        compliance=dict(state.compliance),
        current_sets={k: copy.deepcopy(v) for k, v in state.current_sets.items()},
        sessions_since_progress=dict(state.sessions_since_progress),
        below_plan_streak=dict(state.below_plan_streak),
        below_plan_best_kg=dict(state.below_plan_best_kg),
        last_routine_light=dict(state.last_routine_light),
        active_rules=[copy.deepcopy(r) for r in decision.active_rules],
        pending_strength=state.pending_strength,
        program_start=state.program_start,
        last_deload_start=(
            decision.deload.start if decision.deload.active else state.last_deload_start
        ),
    )

    sess = decision.session
    rkey = sess.routine_key

    # La carga vigente se fija por la MAÑANA, no al reconciliar: es la que se
    # acaba de escribir en Hevy, y es a la que hay que volver mañana aunque esta
    # noche no se entrene. Si esperara a saber si se ejecutó, un día que se
    # salta la sesión revertiría la subida ya escrita en la app.
    if rkey:
        for clave, series in (sess.target_sets or {}).items():
            new.current_sets[(rkey, clave)] = copy.deepcopy(series)

    # Un rojo aplaza la fuerza en vez de saltársela.
    if decision.light == "red" and decision.calendar_routine:
        new.pending_strength = (decision.calendar_routine, decision.day)
    elif rkey and sess.kind in {"full", "reduced"}:
        # Solo lo borra la rutina que estaba pendiente, no una cualquiera.
        #
        # Esto era `new.pending_strength = None` a secas, y el efecto era este:
        # un lunes en rojo aplaza `dia_1`; el jueves toca `dia_2` por
        # calendario; planificar `dia_2` -ni siquiera ejecutarlo- borraba el
        # `dia_1` aplazado. La sesión que un rojo había protegido desaparecía
        # por haber entrenado otra cosa, sin ejecutarse y sin decir nada. El
        # aplazamiento existe justamente para que un día malo no cueste una
        # sesión, y así costaba la sesión igual pero en diferido.
        pendiente = new.pending_strength
        if pendiente and pendiente[0] == rkey:
            new.pending_strength = None

    # Un aplazamiento caducado se borra, y quien decidió que había caducado fue
    # `decide` -que es quien tiene el config con `defer_expires_days`-. Aquí
    # solo se ejecuta.
    #
    # Antes no lo borraba nadie: pasados los días, la fila se quedaba en la base
    # para siempre, `decide` ya no la miraba nunca más y la sesión aplazada
    # dejaba de existir sin que se enterase nadie. Borrarla en silencio sería el
    # mismo fallo con la base más limpia, así que `decide` además lo cuenta en
    # las notas: una sesión perdida es información de entrenamiento.
    if decision.expired_deferral:
        new.pending_strength = None

    if executed is None or not rkey or sess.kind not in {"full", "reduced"}:
        return new

    return apply_execution(
        new,
        routine_key=rkey,
        exercises=sess.exercises,
        executed=executed,
        light=decision.light,
        progressed=[e.key for e in decision.progression.changes]
        if decision.progression
        else (),
    )


def apply_execution(
    state: EngineState,
    *,
    routine_key: str,
    exercises: Sequence[dict[str, Any]],
    executed: dict[str, bool],
    light: str | None = None,
    progressed: Sequence[str] = (),
) -> EngineState:
    """Aplica al estado lo que REALMENTE se ejecutó. Muta y devuelve `state`.

    Está separada de `advance_state` porque las dos mitades ocurren en momentos
    distintos y con información distinta. Por la mañana se decide y se guardan
    reglas, aplazamientos y descarga; por la noche se sabe qué se hizo y avanzan
    las rachas. La reconciliación nocturna llama AQUÍ y no a `advance_state`
    porque `advance_state` reconstruye `active_rules` desde la decisión que
    recibe: al reconciliar habría que rehidratar esa decisión desde la base de
    datos, y una rehidratación incompleta -que es lo normal- vaciaría en
    silencio las reglas activas. El peso muerto retirado catorce días volvería a
    aparecer en la rutina esa misma noche, sin un solo error por ninguna parte.

    `progressed` son los ejercicios que HOY han subido. Su racha vuelve a cero:
    las sesiones limpias que pagaron la subida ya se han gastado en ella. Sin
    esta lista un ejercicio que sube por la mañana y se completa por la noche
    conservaría la racha entera y podría volver a subir al día siguiente, dos
    subidas seguidas sin las sesiones limpias que las justifican. Es el motivo
    de que la progresión se guarde con la decisión: por la noche ya no se puede
    deducir.
    """
    if light is not None:
        state.last_routine_light[routine_key] = light

    for ex in exercises:
        key = ex.get("key")
        if not key:
            continue
        ok = bool(executed.get(key, False))
        scoped = (routine_key, key)
        state.compliance[scoped] = ok
        if ok:
            state.clean_sessions[scoped] = state.clean_sessions.get(scoped, 0) + 1
        else:
            # La racha se rompe entera. Es el punto: "sesiones limpias
            # CONSECUTIVAS". Decrementar en vez de resetear convertiría el
            # requisito en una media, que es otra cosa.
            state.clean_sessions[scoped] = 0

    # La cola de los cupos avanza aquí, con el resto de rachas, y no al
    # planificar: lo que cuenta son las sesiones que de verdad han pasado, no
    # las veces que se ha mirado. Planificar dos veces el mismo día -a las 07:00
    # sin check-in y otra vez a las 09:40 con él- no puede colar a nadie en la
    # cola por delante de los demás.
    subidos = set(progressed)
    for ex in exercises:
        key = ex.get("key")
        if not key:
            continue
        scoped = (routine_key, key)
        state.sessions_since_progress[scoped] = (
            0 if key in subidos else state.sessions_since_progress.get(scoped, 0) + 1
        )

    for key in progressed:
        state.clean_sessions[(routine_key, key)] = 0

    return state
