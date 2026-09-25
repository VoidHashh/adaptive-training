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
from datetime import date, datetime, timedelta
from typing import Any, Sequence

from app.engine.bike_advisor import BikeRecommendation, recommend_bike
from app.engine.progression import ProgressionPlan, plan_progression
from app.engine.rotacion import to_dict as pendientes_to_dict
from app.engine.rules import COMPARISONS, LightDecision, RuleError, evaluate_light
from app.engine.session_builder import (
    BuiltSession,
    SesionPedida,
    build_session,
    clave_del_bloque_hiit,
    orden_de_rotacion,
    siguiente_en_rotacion,
)
from app.engine.signals import (
    CLAVE_VOY_A_ENTRENAR,
    Signals,
    WEEKDAY_NAMES,
    week_start,
)

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
    #
    # La mejor es la SESIÓN, con sus series, y no su peso más alto: la bajada
    # adopta su forma entera (ver `app/engine/adoption.py`). Hasta el 25/09/2026
    # aquí había `below_plan_best_kg`, un número, y con él un objetivo plano
    # hecho en rampa bajaba al tope de la rampa en todas las series.
    below_plan_streak: dict[tuple[str, str], int] = field(default_factory=dict)
    below_plan_best_sets: dict[tuple[str, str], list[dict[str, Any]]] = field(
        default_factory=dict
    )
    # Semáforo del día en que se hizo por última vez CADA rutina. Es el reloj
    # de los frenos de volumen: un rojo en lunes no cancela el viernes.
    last_routine_light: dict[str, str | None] = field(default_factory=dict)
    active_rules: list[ActiveRule] = field(default_factory=list)
    # El puntero de la rotación: la última rutina del ciclo que aparece
    # EJECUTADA en Hevy, con la fecha del día en que se hizo. Aquí había un
    # `pending_strength` con la sesión que un día rojo había dejado aplazada, y
    # la diferencia entre las dos cosas es todo el rediseño: aquello guardaba lo
    # que NO se había hecho y había que reponerlo antes de que caducara; esto
    # recuerda lo que SÍ se hizo y de ahí sale lo siguiente, sin plazos.
    #
    # La fecha no la usa la rotación -para saber qué toca basta la clave-, la
    # usa el mensaje para decir cuántos días llevas sin fuerza.
    last_strength: tuple[str, date] | None = None
    program_start: date | None = None
    # Inicio de la última semana de descarga ya concedida. Sin esto el jitter
    # no tiene memoria y la descarga podría repetirse o saltarse.
    last_deload_start: date | None = None
    # El lunes de la semana en la que tocaba descarga y se aplazó. Mientras
    # tenga valor hay una descarga DEBIDA, y sigue debida aunque el calendario
    # ya no la nombre. Sin esta memoria el aplazamiento no aplazaba: devolvía
    # `shifted` sin apuntarlo en ningún sitio, así que al día siguiente la misma
    # semana volvía a tocar y la descarga entraba igual, un martes.
    deload_aplazada_desde: date | None = None

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

    def estrenada(self, routine_key: str) -> bool:
        """Si el sistema ha llegado a registrar alguna sesión de esta rutina.

        Se mira sobre `compliance` ENTERO y no sobre la proyección de
        `for_routine`, que solo trae las claves de los ejercicios de hoy. La
        diferencia importa el día que se renombra un ejercicio: con la
        proyección, una rutina con dos años de historia y todas las claves
        cambiadas se leería como recién estrenada.

        `apply_session_result` escribe una entrada por cada ejercicio de la
        rutina en cada reconciliación, así que no haber ni una significa que
        ninguna sesión de esta rutina ha llegado nunca a registrarse. Que se
        haya entrenado o no es otra pregunta, y esta función no la contesta:
        contesta la del sistema, que es la que decide.
        """
        return any(rk == routine_key for rk, _ in self.compliance)


@dataclass
class DeloadStatus:
    """Por qué hoy es (o no es) semana de descarga."""

    active: bool
    reason: str
    start: date | None = None
    end: date | None = None
    shifted: bool = False
    # El lunes de la semana en la que TOCABA y no pudo ser. Mientras tenga
    # valor, la descarga sigue debida: es la deuda que impide que un aplazamiento
    # se coma el turno entero. La escribe `advance_state` en el estado.
    aplazada_desde: date | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "reason": self.reason,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "shifted": self.shifted,
            "aplazada_desde": (
                self.aplazada_desde.isoformat() if self.aplazada_desde else None
            ),
        }


@dataclass
class DecisionAnulada:
    """La decisión de esta mañana que esta otra deja sin efecto, y por qué.

    Existe por un caso muy concreto: el check-in rellenado a las 06:23, antes de
    que el reloj haya subido la noche. Ese día se decide con la HRV, las
    pulsaciones y el sueño sin evaluar, y a las 09:00 -cuando el dato ya está-
    se vuelve a decidir. Si de eso sale otro color, el usuario tiene derecho a
    enterarse de que el mensaje de hace dos horas ya no vale, y a saber qué dato
    lo ha cambiado.

    NO la produce `decide`: la cuelga `run_daily` con lo que le pasa el
    scheduler, igual que `load_adoptions` o `tendencia`. `decide` es una función
    pura y no sabe -ni tiene por qué- que antes hubo otra decisión.

    `sin_llegar` no es decoración. Que la recomputación haya rescatado la HRV no
    significa que haya rescatado el sueño, y una decisión que sigue coja tiene
    que decir en qué sigue coja; si no, el mensaje de las 09:00 se leería como
    definitivo cuando no lo es.
    """

    anterior: str
    decidida_a: datetime | None = None
    fuente_anterior: str | None = None
    medidas: list[str] = field(default_factory=list)
    sin_llegar: list[str] = field(default_factory=list)

    def cambia_el_color(self, ahora: str) -> bool:
        return self.anterior != ahora

    def to_dict(self) -> dict[str, Any]:
        return {
            "anterior": self.anterior,
            "decidida_a": self.decidida_a.isoformat() if self.decidida_a else None,
            "fuente_anterior": self.fuente_anterior,
            "medidas": list(self.medidas),
            "sin_llegar": list(self.sin_llegar),
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
    # La rutina que se planifica hoy, se vaya al gimnasio o no. En un día rojo
    # `session.routine_key` apunta al bloque de recuperación, así que sin este
    # campo no habría forma de saber qué sigue esperando su turno.
    #
    # Se llamaba `calendar_routine` y significaba «la que tocaba hoy por
    # calendario». Ya no la manda el día de la semana: la manda cuál fue la
    # última que se ejecutó en Hevy... salvo que esta mañana se haya elegido otra
    # en el selector, y entonces es esa. Es la que se construye, la que se escribe
    # en Hevy y la que nombra el mensaje, que son las tres cosas que tienen que
    # hablar de lo mismo.
    rotation_routine: str | None = None
    # Lo que decía el ciclo antes de mirar el formulario. Igual a
    # `rotation_routine` casi todos los días: solo se separan cuando se elige un
    # día distinto del propuesto, y ahí está justamente su razón de ser.
    #
    # Se guarda aparte porque sin ella no se puede reconstruir si el día se
    # desvió. `rotation_routine` a solas dice qué se planificó; `last_strength`
    # permite recalcular la propuesta a mano, pero solo si el ciclo del config no
    # ha cambiado desde entonces. Escrito, no hay que recalcular nada.
    propuesta: str | None = None
    # Lo que se contestó en el selector: una clave del ciclo, `bici`, `otro`, o
    # `None` si no se contestó. Crudo, sin traducir a nada, por lo mismo que
    # `va_a_entrenar`: el `None` es «no me lo han dicho» y es todo el histórico
    # anterior a que el selector existiera.
    sesion_elegida: str | None = None
    active_rules: list[ActiveRule] = field(default_factory=list)
    progression: ProgressionPlan | None = None
    # La progresión del BLOQUE HIIT, que es un plan entero y aparte, no un
    # apéndice del de arriba. Va separado por lo mismo que la sesión: los cupos
    # de `volume_safety` -"como mucho dos subidas de volumen por sesión"- y las
    # puertas de `plan_progression` cuentan POR SESIÓN, y meter los dos en un
    # plan haría que una subida de la prensa se comiera el cupo de la plancha.
    #
    # `None` cuando hoy no hay bloque, y se pone a `None` a posta si el bloque
    # acaba no entrando: un plan guardado para un bloque que no se hizo es una
    # subida que el registro da por ocurrida y la app nunca escribió.
    progression_hiit: ProgressionPlan | None = None
    bike: BikeRecommendation | None = None
    notes: list[str] = field(default_factory=list)
    config_hash: str | None = None
    source: str = "checkin"
    # Aquí estaba `expired_deferral`: la sesión aplazada por un rojo que hoy
    # había caducado, para poder contar en el mensaje "esa semana entrené una
    # vez menos". No existe porque no existe la caducidad. Con la rotación leída
    # de lo ejecutado, una sesión que no se hace no se pierde: sigue siendo la
    # siguiente hasta que se haga. Lo que ocupa su sitio en el mensaje es el
    # número de días desde la última sesión de fuerza, que dice lo mismo sin
    # inventar un plazo.
    #
    # La última de fuerza ejecutada y su fecha, tal y como venían en el estado.
    # Es lo que hace que el mensaje pueda decir "llevas N días sin fuerza" y de
    # dónde sale `rotation_routine`. Se guarda en la decisión para que el
    # histórico explique por qué ese día tocaba esa rutina y no otra.
    last_strength: tuple[str, date] | None = None
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
    # La decisión de esta misma mañana que esta deja sin efecto, cuando la hay.
    # Un `DecisionAnulada`; ver su docstring. La cuelga `run_daily` con lo que
    # le pasa `job_decision`, por el mismo motivo que los tres campos de arriba:
    # `decide` no abre la base de datos y no sabe qué se decidió antes.
    anulacion: Any = None
    # Las rutinas del ciclo que llevan más de una vuelta sin hacerse. Una lista
    # de `rotacion.Pendiente`, colgada por `run_daily` como los cuatro campos de
    # arriba y por el mismo motivo: se calcula sobre `workout_log` entero y
    # `decide` no abre la base de datos.
    #
    # NO CAMBIA NADA DE LO QUE SE DECIDE. No reordena el ciclo, no adelanta
    # ninguna rutina y no entra en ninguna regla: la propuesta de mañana la sigue
    # dando `siguiente_en_rotacion` a partir de lo último que se ejecutó. Está
    # aquí para que el mensaje pueda decirlo y para que quede en el histórico,
    # que son las dos cosas que se pidieron.
    #
    # Ordenada por lo que más lleva parado primero, porque el mensaje nombra como
    # mucho una.
    pendientes: list[Any] = field(default_factory=list)
    # Lo que contestaste a «¿Vas a entrenar hoy?». Tres estados, y el `None` no
    # es un hueco: es «no me lo han dicho», que es el caso de todos los días sin
    # check-in y de todo el histórico anterior a la pregunta.
    #
    # Este SÍ lo produce `decide` -sale de `signals`, que es entrada pura- a
    # diferencia de los cuatro de arriba, que necesitan la base de datos. Está
    # aquí y no solo dentro de `signals` porque es lo que decide si el mensaje
    # prescribe la sesión o solo informa del estado, y esa es una propiedad de la
    # decisión del día, no un dato de entrada más entre veinte.
    #
    # No tiene columna propia en `decisions`, y es deliberado: la respuesta ya se
    # guarda dos veces -en `checkins.will_train`, que es su casa, y dentro de
    # `inputs_snapshot_json`- y el efecto que tuvo queda escrito en el motivo de
    # la puerta. Una tercera copia solo añadiría un sitio donde pudieran
    # discrepar.
    va_a_entrenar: bool | None = None

    @property
    def weekday(self) -> str:
        return WEEKDAY_NAMES[self.day.weekday()]

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "weekday": self.weekday,
            "light": self.light,
            "trigger_rule": self.trigger_rule,
            "rotation_routine": self.rotation_routine,
            # Las dos van al lado de la de arriba a propósito: leídas juntas se
            # ve de un vistazo si el día se desvió y hacia dónde. Iguales las
            # tres es el día normal.
            "propuesta": self.propuesta,
            "sesion_elegida": self.sesion_elegida,
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
            "progression_hiit": _progression_dict(self.progression_hiit),
            "notes": self.notes,
            "last_strength": (
                {
                    "routine": self.last_strength[0],
                    "day": self.last_strength[1].isoformat(),
                }
                if self.last_strength
                else None
            ),
            "load_adoptions": list(self.load_adoptions),
            "entrenos_sueltos": list(self.entrenos_sueltos),
            "tendencia": self.tendencia.to_dict() if self.tendencia else None,
            "recalibracion": (
                self.recalibracion.to_dict() if self.recalibracion else None
            ),
            "anulacion": self.anulacion.to_dict() if self.anulacion else None,
            # LAS CADUCADAS TAMBIÉN. La caducidad decide si el mensaje la nombra,
            # no si el dato existe: «el Día 1 lleva nueve sesiones sin hacerse» es
            # justo lo que querrá leer quien mire el histórico dentro de tres
            # meses, y es el día en que el mensaje ya se ha callado cuando más
            # falta hace que esté escrito.
            "pendientes": pendientes_to_dict(self.pendientes),
            # Los tres estados salen tal cual, `None` incluido. Convertirlo a
            # `False` aquí -"total, es un JSON"- haría que cada día sin check-in
            # del histórico quedara escrito como un día en el que dijiste que no
            # ibas a entrenar, y eso es lo que leerá cualquier análisis que se
            # escriba sobre esta salida.
            "va_a_entrenar": self.va_a_entrenar,
        }


def _progression_dict(plan: ProgressionPlan | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    return {
        "routine": plan.routine_key,
        "gate_open": plan.gate_open,
        "gate_reason": plan.gate_reason,
        # Sí, esto duplica `gate_reason`: la frase ya dice que es un estreno.
        # Pero la frase se lee y el booleano se CONSULTA. "¿Cuántas de las
        # puertas cerradas de septiembre fueron estrenos y cuántas averías?" se
        # contesta filtrando esta clave; con solo la prosa habría que buscar
        # subcadenas contra un texto que para entonces puede estar reescrito,
        # que es justo la fragilidad por la que `estreno` existe como campo.
        #
        # Y hay que ponerlo AQUÍ además de en `ProgressionPlan.to_dict`, porque
        # esta función es una segunda proyección escrita a mano y es la que
        # acaba en `decisions.progression_json`. Un campo nuevo en el plan no
        # llega solo: el test de serialización pilló este hueco.
        "estreno": plan.estreno,
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
    contando desde `program_start`. `jitter_weeks` permite retrasarla si la
    semana que le tocaba arranca en ROJO.

    El motivo del jitter: una descarga que empieza el mismo día que el cuerpo
    ya ha forzado una parada no descarga nada, porque esa semana iba a ser
    suave de todos modos. Retrasarla hace que caiga sobre una semana en la que
    de verdad haya algo que recortar.

    Solo se retrasa, nunca se adelanta: adelantar exigiría saber que la semana
    que viene será mala, y eso no se sabe. `jitter_weeks` es por tanto un
    margen hacia adelante, y así está documentado en el YAML.

    EL APLAZAMIENTO NO APLAZABA, Y ADEMÁS SE COMÍA EL TURNO
    ------------------------------------------------------
    Devolvía `shifted=True` y no lo apuntaba en ninguna parte, así que el efecto
    duraba un día. Comprobado sobre el config real con el programa empezando el
    05/01/2026: lunes 23/02 en rojo, «se retrasa una semana»; martes 24/02, la
    misma semana seguía tocando -el calendario no había cambiado- y la descarga
    entraba con `start` el lunes 23. Un aplazamiento de veinticuatro horas
    anunciado como de siete días.

    Y si el martes también hubiera sido rojo, o si simplemente no se hubiera
    ejecutado ese día, la semana se habría acabado sin descarga y la siguiente
    ocasión era el 13/04: SIETE semanas más tarde. El aplazamiento no retrasaba
    la descarga, la borraba, y el mensaje decía que la había retrasado.

    LA DEUDA
    --------
    Ahora, aplazar ESCRIBE: `state.deload_aplazada_desde` guarda el lunes de la
    semana en la que tocaba. Mientras tenga valor la descarga sigue debida, y
    sigue debida aunque el calendario ya no la nombre. Eso arregla las dos
    mitades a la vez: la semana aplazada no vuelve a concederse a mitad -ya
    consta aplazada entera- y la deuda no caduca al pasar la semana.

    `jitter_weeks` deja de ser un adorno y pasa a ser un PRESUPUESTO: cuántas
    semanas puede empujarla el rojo en total. Con el config actual, una. Agotado
    el presupuesto la descarga entra aunque la semana siga en rojo, y esa es la
    parte deliberada: con una hernia L4-L5 la descarga es lo último que puede
    saltarse, y una racha de semanas malas es exactamente cuando más falta hace.
    Un presupuesto infinito habría convertido «se retrasa» en «no se hace nunca»
    para quien peor está, que es el fallo de partida disfrazado de prudencia.
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

    debida = state.deload_aplazada_desde

    if debida is None and (weeks == 0 or weeks % every != 0):
        nxt = origin + timedelta(weeks=((weeks // every) + 1) * every)
        return DeloadStatus(
            False, f"no toca esta semana (la siguiente empieza el {nxt})"
        )

    # Una semana ya aplazada lo está ENTERA. Sin esto el aplazamiento del lunes
    # se deshacía solo el martes: la semana seguía siendo múltiplo de `every`.
    if debida is not None and this_week == debida:
        return DeloadStatus(
            False,
            f"la descarga de la semana del {debida} quedó aplazada por el rojo: "
            f"no entra a mitad de semana, se intenta el lunes que viene",
            shifted=True,
            aplazada_desde=debida,
        )

    # Toca. ¿La aplazamos por el jitter?
    empujada = 0 if debida is None else (this_week - debida).days // 7
    if light == "red" and empujada < jitter:
        desde = debida or this_week
        return DeloadStatus(
            False,
            f"tocaba descarga pero la semana arranca en ROJO: se retrasa a la "
            f"semana que viene, porque descargar sobre una semana ya frenada no "
            f"descarga nada. Sigue debida desde el {desde}",
            shifted=True,
            aplazada_desde=desde,
        )

    start = this_week
    if debida is not None:
        return DeloadStatus(
            True,
            f"descarga debida desde el {debida}, aplazada {empujada} semana(s) "
            f"por el rojo y ya sin margen: entra hoy",
            start=start,
            end=start + timedelta(days=dur - 1),
        )
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
    sesion_pedida: SesionPedida | None = None,
) -> DayDecision:
    """Decide el día completo.

    `signals` ya viene construido por `build_signals` a partir de Garmin, Hevy
    y el check-in. `state` es lo que el motor recuerda; si falta se asume un
    arranque en frío, que es un estado válido y no un error: el primer día del
    sistema no tiene rachas ni reglas vigentes.

    `sesion_pedida` NO ES UNA SEÑAL, Y POR ESO ES UN PARÁMETRO
    ---------------------------------------------------------
    Es la anulación del usuario: completa donde el sistema propone reducida, o
    al revés. No entra por `signals` aunque `sesion_elegida` -qué RUTINA del
    ciclo se hace- sí lo haga, y la diferencia importa. Las señales son lo que
    se sabe del cuerpo esta mañana, y parte de ellas alimenta el histórico del
    que salen los percentiles de los umbrales. Una anulación es una instrucción
    sobre qué hacer con esa lectura, no una lectura más: metida ahí dentro
    acabaría moviendo los umbrales que sirven para juzgarla.

    Cuidado con el nombre: `DayDecision.anulacion` es OTRA cosa -la decisión de
    esta misma mañana que un recálculo deja sin efecto cuando Garmin entrega la
    noche tarde-. Esa la pone el sistema sobre sí mismo. Esta la pone el
    usuario sobre el sistema, y viaja dentro de la sesión
    (`session.anulacion`).
    """
    raw = config.raw if hasattr(config, "raw") else config
    state = state or EngineState()
    notes: list[str] = []

    # Si hoy vas a entrenar o no, según lo que contestaste esta mañana. Tres
    # estados, y el `None` -«no me lo han dicho»- es el caso normal: los días sin
    # check-in y todo el histórico anterior a que la pregunta existiera.
    #
    # Se lee UNA vez y aquí arriba, junto al resto de lo que entra, porque lo
    # consultan dos sitios -la puerta de la progresión y el mensaje- y leerlo dos
    # veces sería tener dos sitios donde equivocarse de clave.
    va_a_entrenar = signals.get(CLAVE_VOY_A_ENTRENAR)

    # --- 1. semáforo --------------------------------------------------------
    #
    # Y esto NO mira `va_a_entrenar`, a propósito y por construcción: una regla
    # solo puede nombrar deslizadores del check-in, y las dos preguntas de Sí/No
    # no lo son -`config_loader` rechaza el arranque si alguna lo intenta-. El
    # color dice cómo estás; que vayas o no es otra cosa.
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
    # Qué toca en el ciclo. Se calcula UNA vez, aquí, y se le pasa hecha a
    # `build_session`: antes cada uno leía el calendario por su cuenta y también
    # replicaba a mano el criterio de recuperar aplazamientos, con el resultado
    # previsible de dos copias del mismo razonamiento que podían separarse sin
    # que nada las comparase.
    #
    # SIEMPRE HAY RUTINA, Y NO DEPENDE DE LO QUE SE HAYA CONTESTADO.
    #
    # No existe el día sin fuerza asignada. Existe el día en que se dice que no
    # se va al gimnasio, el día en que se elige salir en bici y el día en que se
    # entrena otra cosa, y en los tres se planifica una rutina igual y se escribe
    # en Hevy igual. Lo que cambia es si el mensaje la PRESCRIBE.
    #
    # El motivo es el mismo en los tres casos y es el de siempre: a las siete de
    # la mañana se contesta una intención, y a las siete de la tarde se cambia de
    # idea. Si la escritura dependiera de la respuesta, cambiar de idea
    # significaría abrir Hevy y encontrar la rutina de hace dos semanas, con los
    # pesos de entonces y sin los ajustes de hoy. Lo que pasó de verdad lo sabe
    # el sistema leyendo Hevy, no decidiéndolo por adelantado.
    #
    # LA PROPUESTA Y LA ELECCIÓN SON DOS COSAS
    # ----------------------------------------
    # `propuesta` es lo que dice el ciclo, que es función de lo último que se
    # EJECUTÓ y de nada más. `sesion_elegida` es lo que se declaró esta mañana en
    # el formulario. Casi siempre coinciden -el selector viene preseleccionado
    # con la propuesta-, y cuando no, manda la elección para hoy y no para
    # mañana: la rotación de mañana la sigue gobernando `workout_log`.
    #
    # Las dos se guardan. Con una sola no se puede preguntar en qué se
    # diferencian, que es justo lo que hay que poder preguntar para saber si uno
    # se salta el Día 1 a menudo.
    ultima = state.last_strength
    propuesta = siguiente_en_rotacion(config, ultima[0] if ultima else None)
    sesion_elegida = getattr(signals, "sesion_elegida", None)

    # Solo una elección que ES una rutina del ciclo cambia lo que se planifica.
    # `bici` y `otro` no nombran ninguna, así que el día se planifica con la
    # propuesta: es la que hay que dejar puesta en Hevy por si acaba yendo.
    del_ciclo = sesion_elegida in orden_de_rotacion(config)
    rotation_routine = sesion_elegida if del_ciclo else propuesta
    routine_key = rotation_routine

    # Lo que cierra la puerta de la progresión, vacío salvo en `bici` y `otro`.
    # Se resuelve por descarte y no contra una lista de no-fuerza: cualquier cosa
    # contestada que no sea una rutina del ciclo es, por definición, un día sin
    # fuerza que prescribir. Comprobarlo contra `checkin_selector.sin_fuerza`
    # sería leer una segunda lista para decidir lo mismo, y dejaría el hueco de
    # una opción añadida al YAML y olvidada aquí.
    eleccion_sin_fuerza = None if del_ciclo else sesion_elegida

    # SIN `if routine_key:` DELANTE, Y ESO ES UN CAMBIO
    # ------------------------------------------------
    # Con el calendario fijo había días sin rutina asignada, y en esos días
    # `progression` se quedaba en `None`. Ya no existe ese día: la rotación
    # siempre tiene una siguiente, y `siguiente_en_rotacion` revienta en vez de
    # devolver nada. Dejar el `if` puesto sería dejar una rama que no se puede
    # ejecutar, y una rama que no se ejecuta no se prueba: el día que alguien
    # cambiara algo debajo, fallaría sin que ningún test pasara por ahí.
    #
    # Que el plan exista no significa que la puerta esté abierta. En un día rojo
    # sale con `gate_open=False` y sin cambios, que es información -"hoy no sube
    # nada, y por esto"- y no un hueco.
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
        # Solo para redactar el motivo de la puerta: una rutina que el sistema
        # no ha visto nunca no es una avería, y el mensaje no tiene por qué
        # sonar como si lo fuera.
        rutina_estrenada=state.estrenada(routine_key),
        # Este sí cierra la puerta. Del check-in, y en tres estados: si hoy no
        # vas, no hay nada que subir. Que la puerta se cierre AQUÍ y no solo al
        # redactar el mensaje es lo que hace que el día quede guardado con su
        # motivo -«hoy no entrenas»- en vez de con un plan de subidas que nadie
        # llegó a ejecutar y que dentro de tres meses parecerá que sí.
        va_a_entrenar=va_a_entrenar,
        # Y la hermana de la de arriba: hoy toca bici, o algo sin plan. Cierra la
        # puerta por lo mismo -no hay fuerza que prescribir- y por eso no se
        # anuncia ninguna subida. Lo que sí sigue pasando es todo lo demás: la
        # sesión se construye, se escribe en Hevy y la carga vigente se fija.
        eleccion_sin_fuerza=eleccion_sin_fuerza,
        # La cola de los cupos, acotada a esta rutina: `plan_progression`
        # trabaja con claves de ejercicio a secas.
        sessions_since_progress={
            k: v
            for (rk, k), v in state.sessions_since_progress.items()
            if rk == routine_key
        },
    )

    # LA SEGUNDA PROGRESIÓN: LA DEL BLOQUE HIIT
    # ----------------------------------------
    # Llevaba parada desde el primer día, y no por una decisión: el bloque se
    # añade en el paso 6 de `build_session` y `apply_progression` corre en el 2,
    # así que nunca pasaba por delante. `plancha_frontal` está declarada
    # `progression_type: volume` con `max_seconds: 60` y no sumó un segundo en
    # seis meses. El config lo decía, el motor no lo hacía y nada fallaba.
    #
    # Se planifica SIEMPRE que el bloque exista, sin mirar si hoy toca: quien
    # decide eso es `hiit_applies`, dentro de `build_session`, y duplicar aquí
    # esa condición sería un segundo juez de lo mismo. Lo que se hace es tirar
    # el plan después si el bloque no ha entrado -unas líneas más abajo-, que es
    # la única forma de que el registro no guarde una subida que no ocurrió.
    bloque_hiit = clave_del_bloque_hiit(raw, routine_key)
    progression_hiit: ProgressionPlan | None = None
    if bloque_hiit and bloque_hiit in (raw.get("routines") or {}):
        ex_hiit = (raw["routines"][bloque_hiit] or {}).get("exercises") or []
        compliance_h, clean_h = state.for_routine(
            bloque_hiit, [e["key"] for e in ex_hiit]
        )
        progression_hiit = plan_progression(
            config,
            bloque_hiit,
            signals,
            light,
            compliance=compliance_h,
            clean_sessions=clean_h,
            deload_active=deload.active,
            last_routine_light=state.last_routine_light.get(bloque_hiit),
            current_sets=state.current_sets,
            rutina_estrenada=state.estrenada(bloque_hiit),
            # Los mismos dos cierres que la fuerza, y por el mismo motivo: si
            # hoy no vas, o vas a la bici, tampoco hay HIIT que subir.
            va_a_entrenar=va_a_entrenar,
            eleccion_sin_fuerza=eleccion_sin_fuerza,
            sessions_since_progress={
                k: v
                for (rk, k), v in state.sessions_since_progress.items()
                if rk == bloque_hiit
            },
        )

    # --- 4. sesión ----------------------------------------------------------
    session = build_session(
        config,
        day,
        light,
        rotation_routine=rotation_routine,
        progression=progression,
        progression_hiit=progression_hiit,
        # `session_builder` espera {name, action}, con la acción anidada. No se
        # aplana: `apply_rule_load_cuts` busca `rule["action"]["reduce_load"]`.
        active_rules=[{"name": r.name, "action": r.action} for r in active_rules],
        deload_active=deload.active,
        program_start=state.program_start,
        current_sets=state.current_sets,
        # La anulación del usuario. Puede levantar `ConfirmacionNecesaria`, que
        # sube entera hasta quien llamó: subir de intensidad en rojo se
        # pregunta, no se resuelve aquí con un valor por defecto.
        sesion_pedida=sesion_pedida,
    )

    # EL BLOQUE NO HA ENTRADO: EL PLAN NO HA PASADO.
    #
    # `build_session` tiene cuatro salidas por las que el HIIT no se añade -no
    # toca por semana, `allow_hiit` apagado por regla especial, el bloque no
    # está en `routines`, día rojo con bloque de recuperación- y en todas ellas
    # `session.hiit` queda en `None`. Guardar el plan de todos modos escribiría
    # en `progression_json` una subida que la app no prescribió; esa noche la
    # reconciliación pondría la racha a cero por una progresión que no hubo, y
    # la plancha volvería a esperar otra sesión limpia para subir de verdad.
    if session.hiit is None:
        progression_hiit = None

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
        rotation_routine=rotation_routine,
        propuesta=propuesta,
        sesion_elegida=sesion_elegida,
        active_rules=active_rules,
        progression=progression,
        progression_hiit=progression_hiit,
        bike=bike,
        notes=notes,
        config_hash=getattr(config, "hash", None),
        source=source,
        last_strength=state.last_strength,
        va_a_entrenar=va_a_entrenar,
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
        below_plan_best_sets={
            k: copy.deepcopy(v) for k, v in state.below_plan_best_sets.items()
        },
        last_routine_light=dict(state.last_routine_light),
        active_rules=[copy.deepcopy(r) for r in decision.active_rules],
        last_strength=state.last_strength,
        program_start=state.program_start,
        last_deload_start=(
            decision.deload.start if decision.deload.active else state.last_deload_start
        ),
        # Se copia tal cual y no se arrastra: `_deload_status` devuelve la deuda
        # en cada decisión, y devuelve `None` en cuanto la descarga entra. Si se
        # conservara el valor viejo cuando hoy no hay deuda, una descarga ya
        # concedida seguiría constando debida para siempre.
        deload_aplazada_desde=decision.deload.aplazada_desde,
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

    # Y el bloque HIIT bajo SU clave, que es `hiit_dia_1` y no `dia_1`.
    #
    # Antes sus ejercicios venían pegados a los de la fuerza, así que este mismo
    # bucle los guardaba como `(dia_1, wall_ball)`. Esa fila no la lee nadie:
    # `dia_1` no declara `wall_ball`, así que `con_carga_vigente` nunca la
    # busca. La carga del bloque salía del YAML cada mañana y `plancha_frontal`
    # -que progresa por volumen- volvía a sus segundos de fábrica todos los días
    # mientras la base guardaba una progresión que no se aplicaba a nada.
    hiit = getattr(sess, "hiit", None)
    if hiit is not None and hiit.routine_key:
        for clave, series in (hiit.target_sets or {}).items():
            new.current_sets[(hiit.routine_key, clave)] = copy.deepcopy(series)

    # Aquí vivía toda la contabilidad del aplazamiento: un rojo guardaba la
    # sesión, planificar otra rutina la borraba -y borrarla por haber
    # planificado, no por haber ejecutado, costó una sesión entera en silencio-,
    # y un aplazamiento caducado se limpiaba en un tercer sitio. Tres reglas
    # para decidir cuál era la siguiente sesión.
    #
    # La regla es una: la siguiente es la que va después de la última que se
    # HIZO. Y por eso el puntero solo se mueve unas líneas más abajo, cuando hay
    # ejecución de verdad.

    if executed is None or not rkey or sess.kind not in {"full", "reduced"}:
        return new

    # El puntero de la rotación. Se mueve aquí y en ningún otro sitio, y solo
    # con `executed` distinto de None: a las 07:00 la decisión está tomada pero
    # el entrenamiento no ha pasado, y adelantar la rotación por la mañana es
    # exactamente el fallo que este diseño evita. Un bloque de recuperación de
    # día rojo tampoco llega hasta aquí, porque su `kind` es `recovery`.
    #
    # En producción esto lo vuelve a calcular `load_state` leyendo `workout_log`
    # -la fuente de verdad es Hevy, no el estado-, pero hace falta igual: es lo
    # que mantiene honesta la simulación de `scripts/sim_12_semanas.py`, que
    # avanza el estado sin base de datos detrás.
    new.last_strength = (rkey, decision.day)

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
    progressed: Sequence[str],
    light: str | None = None,
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

    Y por eso es obligatorio y no `= ()`. Los dos callers lo pasan, así que el
    defecto no servía a nadie; lo único que hacía era dejar preparado el día en
    que un tercer caller se olvidase. Ese olvido no fallaría: adelantaría una
    subida de carga, en silencio y en la dirección de siempre -de más-, sobre
    una espalda que no admite dos subidas seguidas. Cuando de verdad no hay
    nada que hubiera subido hoy se pasa `()` a mano, y entonces es una
    afirmación en vez de un hueco.
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
