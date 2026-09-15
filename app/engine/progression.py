"""Progresión: qué sube hoy, cuánto y por qué.

Cuatro modos, declarados por ejercicio en `progression_type`:

  load    sube carga cuando se cumple la puerta
  double  doble progresión: reps hasta el tope del rango, luego carga y vuelta
          al mínimo
  volume  sin carga: reps o segundos hasta un techo
  sets    añade una serie efectiva hasta `max_sets`, y al llegar pasa a `then`

DOS PUERTAS, NO UNA
-------------------
La puerta general (verde + cumplimiento + frenos) decide si se progresa ALGO.
El volumen tiene además su propia puerta (`volume_safety`), y el motivo es que
carga y volumen no fallan igual: la carga que te pasas la notas ese mismo día y
paras. El volumen que te pasas no lo notas, se acumula, y aparece tres semanas
después convertido en una molestia lumbar que no sabes de dónde viene.

Por eso una semana con un rojo o dos ámbar congela SOLO el volumen: si hoy hay
verde se puede subir peso, pero no series ni reps. Son dos decisiones distintas
y se toman por separado.

NUNCA LAS DOS COSAS A LA VEZ
----------------------------
`never_load_and_volume_same_session` no es prudencia decorativa. Si un ejercicio
sube de 3 a 4 series Y de 60 a 62,5 kg el mismo día, el salto real es del ~35%
en trabajo total, y cuando la semana siguiente venga mal no hay forma de saber
cuál de las dos cosas sobró. Una cosa cada vez es lo que hace la progresión
legible hacia atrás.

CADENA POSTERIOR PRIMERO SERIES, DESPUÉS CARGA
----------------------------------------------
Con la hernia L4-L5, `sets` antes que `load` es una decisión clínica, no de
programación: una serie más de hip thrust a 60 kg carga menos la columna en el
pico que la misma serie a 65. El ejercicio agota primero el techo de series y
solo entonces pasa a `then`.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.engine.luces import LUCES, nombre_luz
from app.engine.rules import COMPARISONS, RuleError
from app.engine.sets import excludes, warmup_flags

LOAD = "load"
DOUBLE = "double"
VOLUME = "volume"
SETS = "sets"
NONE = "none"
VALID_TYPES = {LOAD, DOUBLE, VOLUME, SETS, NONE}

# Qué clase de cambio es cada acción. Lo usa
# `never_load_and_volume_same_session` y el recorte de descarga.
KIND_LOAD = "load"
KIND_VOLUME = "volume"


@dataclass
class ExerciseProgression:
    """Lo que le pasa hoy a UN ejercicio."""

    key: str
    name: str
    mode: str
    changed: bool = False
    kind: str | None = None  # load | volume
    # Descripción corta para Telegram: "prensa 60→62,5 kg".
    what: str = ""
    # El porqué, en el mismo lenguaje: "2 sesiones limpias".
    why: str = ""
    # Motivo de NO haber subido, cuando aplica. Va al JSON, no a Telegram.
    blocked_by: str | None = None
    # Mutaciones a aplicar sobre las series efectivas.
    #
    # La subida de carga viaja como INCREMENTO, no como peso absoluto. Con un
    # peso absoluto y `apply_to: all_sets`, una rampa 3/4/4 se convierte en
    # 6/6/6 a la primera subida: se aplana el esquema entero. Con un delta,
    # 3/4/4 -> 4/5/5 y la rampa se conserva, que es lo que se quiere.
    weight_delta_kg: float | None = None
    apply_to: str = "top_set"
    # Las reps viajan como INCREMENTO por el mismo motivo que la carga, y por
    # uno más. `new_reps` era un valor ABSOLUTO que se escribía en todas las
    # series efectivas, así que un 12/10/10 con la subida calculada sobre la
    # serie más baja (10 -> 11) salía 11/11/11: se aplanaba la rampa Y se
    # QUITABA una repetición a la serie superior. Subir bajando el top set es
    # lo contrario de progresar, y encima no daba ningún error.
    #
    # `rep_apply_to` es la opción que ya estaba en el YAML (`lowest_first`) y
    # que el código no leía:
    #   lowest_first -> sube UNA serie, la más baja. 12/10/10 -> 12/11/10.
    #   all_sets     -> sube todas, cada una topada en `rep_cap`.
    # Ninguno de los dos baja nunca una serie.
    rep_delta: int | None = None
    rep_apply_to: str = "lowest_first"
    # Tope por serie. En doble progresión es el máximo del rango: sin él,
    # `all_sets` sobre 12/10/10 daría 13/11/11 y se saldría del rango.
    rep_cap: int | None = None
    # Reps ABSOLUTAS, y solo donde serlo es correcto: la vuelta al mínimo del
    # rango cuando la doble progresión cierra el ciclo y sube carga. Ahí sí se
    # reescriben todas las series a propósito, porque es un recorte declarado.
    new_reps: int | None = None
    new_duration_s: int | None = None
    duration_delta_s: int | None = None
    duration_cap: int | None = None
    add_sets: int = 0
    # Sesiones que este ejercicio lleva sin progresar. Decide el turno cuando
    # hay más candidatos que cupo.
    waiting: int = 0
    # Techo real: el ejercicio ya no da más de sí y toca cambiarlo.
    at_ceiling: bool = False
    # Distinto de un techo: hay recorrido, pero falta el dato para calcularlo
    # (típicamente el peso sin registrar en Hevy). Se resuelve apuntándolo.
    needs_data: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "mode": self.mode,
            "changed": self.changed,
            "kind": self.kind,
            "what": self.what,
            "why": self.why,
            "blocked_by": self.blocked_by,
            "weight_delta_kg": self.weight_delta_kg,
            "apply_to": self.apply_to,
            "rep_delta": self.rep_delta,
            "rep_apply_to": self.rep_apply_to,
            "rep_cap": self.rep_cap,
            "new_reps": self.new_reps,
            "new_duration_s": self.new_duration_s,
            "duration_delta_s": self.duration_delta_s,
            "duration_cap": self.duration_cap,
            "add_sets": self.add_sets,
            "waiting": self.waiting,
            "at_ceiling": self.at_ceiling,
            "needs_data": self.needs_data,
        }

    def text(self) -> str:
        """Línea para el mensaje de Telegram."""
        if not self.changed:
            return ""
        return f"{self.name}: {self.what} ({self.why})"


@dataclass
class ProgressionPlan:
    """La decisión de progresión del día entero."""

    routine_key: str
    gate_open: bool
    gate_reason: str
    # Dos puertas separadas: añadir una serie efectiva no es el mismo riesgo
    # que sumar dos repeticiones, y con un solo permiso había que elegir entre
    # ser laxo con lo primero o asfixiar lo segundo.
    sets_allowed: bool
    sets_reason: str
    reps_allowed: bool
    reps_reason: str
    exercises: list[ExerciseProgression] = field(default_factory=list)
    ceilings: list[str] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)
    # `progression.modes.volume.on_ceiling_notify`. Gobierna SOLO si el techo se
    # cuenta en el mensaje; `ceilings` se rellena siempre. Un interruptor de
    # notificación que además borrase el registro de la decisión convertiría
    # "no me avises" en "no lo apuntes", y entonces la auditoría del día no
    # podría contestar por qué un ejercicio lleva tres semanas parado.
    notify_ceiling: bool = True
    # La puerta está cerrada porque esta rutina no se ha hecho nunca, no porque
    # algo haya salido mal. Es un booleano y no una comprobación del texto de
    # `gate_reason` porque el mensaje cambia de TONO con esto, y hacer que el
    # tono dependa de una subcadena es garantizar que el día que alguien
    # reescriba la frase el aviso vuelva a sonar a fallo sin que nada avise.
    estreno: bool = False

    @property
    def changes(self) -> list[ExerciseProgression]:
        return [e for e in self.exercises if e.changed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "routine": self.routine_key,
            "gate_open": self.gate_open,
            "gate_reason": self.gate_reason,
            "sets_allowed": self.sets_allowed,
            "sets_reason": self.sets_reason,
            "reps_allowed": self.reps_allowed,
            "reps_reason": self.reps_reason,
            "ceilings": self.ceilings,
            "missing_data": self.missing_data,
            # Va al registro para que el "por qué no me avisó" tenga respuesta:
            # un techo apuntado y no notificado se distingue de uno que no hubo.
            "notify_ceiling": self.notify_ceiling,
            "estreno": self.estreno,
            "exercises": [e.to_dict() for e in self.exercises],
        }

    def stopped_lines(self) -> list[str]:
        """Los ejercicios que hoy NO progresan y por qué.

        Un ejercicio parado es información accionable: ha dejado de progresar y
        va a seguir parado hasta que se cambie algo. Callarlo convierte un
        ejercicio muerto en un ejercicio invisible -y eso es exactamente lo que
        pasaba: estas líneas se generaban aquí y no las leía nadie, porque
        `render_telegram` pintaba solo `changes`. Doce semanas de simulación con
        dos ejercicios sin avanzar ni una vez, y el mensaje de la mañana no lo
        mencionó ninguno de los ochenta y cuatro días.

        Los dos motivos se avisan por separado porque la acción es distinta: un
        techo se resuelve cambiando el ejercicio, un dato que falta se resuelve
        apuntando el peso en Hevy. Mandar "toca cambiar el ejercicio" cuando lo
        único que pasa es que no has apuntado la carga es peor que no mandar
        nada.
        """
        out: list[str] = []
        if self.notify_ceiling:
            out.extend(f"{c}: techo alcanzado, toca cambiar el ejercicio" for c in self.ceilings)
        # Sin interruptor, y a propósito: `on_ceiling_notify` habla de techos.
        # Un ejercicio sin carga registrada no está en su techo, está esperando
        # un dato que solo puede dar el usuario, y silenciarlo lo deja parado
        # para siempre sin que nada lo diga.
        if self.missing_data:
            out.append(
                "Sin progresión por falta de carga registrada en Hevy: "
                + ", ".join(self.missing_data)
            )
        return out

    def text_lines(self) -> list[str]:
        """Todo el bloque de progresión: lo que sube y lo que está parado.

        `render_telegram` no llama a esto, sino a `stopped_lines`: el mensaje
        pinta las subidas con su propia cabecera. Esto lo usa
        `scripts/smoke_progression.py`, que durante meses imprimió estas líneas
        bajo el rótulo "Mensaje de Telegram completo" mientras el mensaje real
        no llevaba ni una. Las dos mitades salen ya de la misma función, así
        que el script no puede volver a enseñar algo que no se manda.
        """
        return [e.text() for e in self.changes] + self.stopped_lines()


# ---------------------------------------------------------------------------
# Puertas
# ---------------------------------------------------------------------------


def _fmt_kg(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")  # 62,5 kg, que es como se lee en España


def _por_que_sin_registro(
    detalle: dict[str, bool | None] | None,
    estrenada: bool | None = None,
    titulo: str | None = None,
) -> str:
    """Motivo de cerrar la puerta por falta de registro, nombrando a los
    ejercicios culpables cuando eso sirve de algo.

    Con la rutina entera sin estrenar la lista no aporta nada -son todos-, y
    en el primer arranque llenaría el mensaje de Telegram de ruido. Con uno o
    dos ejercicios nuevos dentro de una rutina en marcha sí aporta: es la
    diferencia entre "hoy no se sube carga" y "reconcilia el hip thrust".

    Y hay un tercer caso, que es el que más veces se va a leer durante las dos
    primeras semanas: la rutina que el sistema no ha visto NUNCA. Ahí "no hay
    registro de la última sesión con el que comparar" es verdad y suena a
    avería -¿se ha perdido algo?, ¿ha fallado la reconciliación?- cuando lo
    que pasa es lo más normal del mundo el día que estrenas un ciclo. Se dice
    en positivo y con lo que el usuario necesita saber, que es CUÁNDO empieza
    a subir la carga.

    `estrenada` es tri-estado a propósito. `None` es "no me lo han dicho" -los
    scripts y buena parte de los tests llaman a `evaluate_gate` a pelo- y cae
    en la redacción de siempre, que no miente: dice menos.

    El sujeto es EL SISTEMA y no el usuario, y eso también es deliberado. Si
    el Día 2 se entrenó pero la reconciliación no llegó a registrarlo, "primera
    vez que el sistema ve el Día 2" sigue siendo cierto; "es la primera vez que
    haces el Día 2" sería falso y encima delataría el fallo al revés.
    """
    if estrenada is False:
        quien = f"el {titulo}" if titulo else "esta rutina"
        return (
            f"primera vez que el sistema ve {quien}: la progresión arranca la "
            f"próxima vez que toque"
        )

    base = "no hay registro de la última sesión con el que comparar"
    if not detalle:
        return base
    sin = [k for k, v in detalle.items() if v is None]
    if not sin or len(sin) == len(detalle):
        return base
    return (
        f"{base} en: {', '.join(sin)}. Del resto sí se sabe, pero la puerta no "
        f"se abre a medias: subir carga apoyándose solo en los ejercicios que "
        f"tienen registro es progresar con la evidencia elegida."
    )


def evaluate_gate(
    prog_cfg: dict[str, Any],
    light: str,
    signals: Any,
    compliance_ok: bool | None,
    routine_key: str,
    deload_active: bool = False,
    compliance_por_ejercicio: dict[str, bool | None] | None = None,
    rutina_estrenada: bool | None = None,
    titulo_rutina: str | None = None,
    va_a_entrenar: bool | None = None,
) -> tuple[bool, str]:
    """Puerta general. Devuelve (abierta, motivo).

    `compliance_por_ejercicio`, `rutina_estrenada` y `titulo_rutina` son
    opcionales y solo sirven para redactar el motivo: la decisión la toma
    `compliance_ok`, que ya viene resuelto. Ninguno de los tres puede abrir
    una puerta que estaría cerrada ni cerrar una que estaría abierta.

    `va_a_entrenar` sí decide, y es el único de los cuatro que lo hace. Son tres
    estados: `False` cierra la puerta, `True` no hace nada y `None` -«no me lo
    han dicho»- tampoco. Ese `None` es el de todos los días en que no se rellena
    el formulario y el de todo el histórico anterior a que la pregunta
    existiera; leerlo como un no habría congelado la progresión hacia atrás en
    el archivo entero.
    """
    # VA EL PRIMERO, ANTES INCLUSO QUE LA DESCARGA.
    #
    # No porque pese más, sino porque es el que explica el mensaje entero. El día
    # que has dicho que no vas, lo que se lee arriba no es un plan de sesión: es
    # el estado del día. Si el motivo de la puerta dijera «semana de descarga»
    # mientras el mensaje no prescribe nada, serían dos versiones del mismo día,
    # y la que se queda en el histórico es esta.
    #
    # Y NO es un reproche. No dice que hayas fallado ni que se pierda nada: dice
    # que lo que suba, subirá la próxima vez. Que es exactamente lo que pasa -la
    # racha no se rompe, la rotación no avanza, el día no cuenta como saltado- y
    # por eso se puede escribir sin rodeos.
    if va_a_entrenar is False:
        return False, "hoy no entrenas: la progresión se decide la próxima vez que toque"

    if deload_active and (prog_cfg.get("deload") or {}).get("freeze_progression", True):
        return False, "semana de descarga: la progresión está congelada"

    gate = prog_cfg.get("gate", {}) or {}
    if gate.get("require_green", True) and light != "green":
        # El color, por su nombre. Este motivo NO se queda en un log: se guarda
        # en la decisión del día y la vista de auditoría lo pinta tal cual, así
        # que "el semáforo no está en verde (amber)" era una clave del motor
        # escrita en la pantalla. Y decirlo en positivo -"está en ámbar"- ahorra
        # la doble negación de leer "no está en verde (rojo)".
        return False, f"el semáforo está en {nombre_luz(light)}, no en verde"

    if gate.get("require_all_sets_at_target_reps", True):
        if compliance_ok is None:
            return False, _por_que_sin_registro(
                compliance_por_ejercicio, rutina_estrenada, titulo_rutina
            )
        if not compliance_ok:
            return False, "en la última sesión no se completaron todas las series efectivas"

    for brake in prog_cfg.get("brakes", []) or []:
        source = str(brake.get("source"))
        value = signals.get(source)

        if value is None:
            # Un freno que no se puede evaluar NO es un freno que no salta.
            #
            # Antes esto era un `continue`: sin check-in, el freno lumbar
            # -que con una hernia L4-L5 es el que manda- desaparecía sin
            # decir nada y la puerta se quedaba abierta. Se subía peso
            # precisamente el día del que menos se sabía.
            #
            # Por defecto se cierra: no progresar hoy es reversible mañana,
            # subir carga con la lumbar sin evaluar no lo es. `on_missing:
            # skip` existe para las señales legítimamente opcionales -el RPE
            # de ayer es nulo si ayer no se entrenó- y hay que declararlo a
            # mano en el YAML, para que saltárselo sea una decisión escrita.
            if str(brake.get("on_missing", "block")) == "skip":
                continue
            return False, (
                f"freno '{brake.get('name')}' no evaluable: falta '{source}'. "
                f"Sin ese dato no se sube carga."
            )

        cond = brake.get("when") or {}
        hit = False
        for op, operand in cond.items():
            fn = COMPARISONS.get(op)
            if fn is None:
                # Una errata en el operador (`gtee: 4`) dejaba el freno
                # muerto en silencio: ninguna rama coincidía, `hit` se
                # quedaba en False y el freno no saltaba jamás.
                raise RuleError(
                    f"freno '{brake.get('name')}': operador desconocido "
                    f"'{op}'. Válidos: {', '.join(sorted(COMPARISONS))}"
                )
            if fn(value, operand):
                hit = True
        if not hit:
            continue
        blocks = str(brake.get("blocks", "all"))
        if blocks == "all":
            return False, f"freno '{brake.get('name')}': {brake.get('source')} = {value}"
        if blocks == "last_session_only":
            # Solo bloquea la rutina de la sesión que generó ese dato.
            if signals.get("yesterday_routine") in (None, routine_key):
                return False, f"freno '{brake.get('name')}': {brake.get('source')} = {value}"

    return True, "puerta abierta"


# El color EN FEMENINO, porque la única frase que lo usa concuerda con "sesión":
# «la última sesión de esta rutina fue roja». De las tres palabras solo cambia
# "rojo", que es justo lo que hace que esto no se pueda derivar de la tabla de
# `app/engine/luces.py` pegándole una letra: es la misma razón por la que los
# plurales de `portada.py` van escritos enteros y por la que `texto.cuantos`
# pide las dos formas. Las excepciones del castellano no caben en un sufijo.
#
# Lo que sí se comprueba es que hable de los mismos colores que el motor.
LIGHT_ES = {"red": "roja", "amber": "ámbar", "green": "verde"}
assert set(LIGHT_ES) == set(LUCES), (
    f"el femenino de progression {sorted(LIGHT_ES)} y los colores del motor "
    f"{sorted(LUCES)} no hablan de lo mismo"
)


def _volume_gate(
    cfg: dict[str, Any],
    gate_cfg: dict[str, Any],
    signals: Any,
    day: date,
    last_routine_light: str | None,
    label: str,
) -> tuple[bool, str]:
    """Una de las dos puertas de volumen. Devuelve (permitido, motivo).

    Se mira la sesión ANTERIOR de esta misma rutina, no una ventana de días.
    Ver el comentario largo de `volume_safety` en el config: la ventana de
    días medía en una unidad distinta de aquella en la que ocurre el evento y
    un solo día malo cancelaba la semana entera de las tres rutinas.
    """
    blocking = {str(x) for x in (gate_cfg.get("block_if_last_routine_session_in") or [])}

    if last_routine_light is None:
        if cfg.get("allow_if_no_previous_session", True):
            return True, f"no hay sesión anterior de esta rutina con la que comparar ({label})"
        return False, f"no hay sesión anterior de esta rutina con la que comparar ({label})"

    if last_routine_light in blocking:
        return False, (
            f"la última sesión de esta rutina fue "
            f"{LIGHT_ES.get(last_routine_light, last_routine_light)} ({label})"
        )

    limit = gate_cfg.get("block_if_mean_lower_discomfort_gte")
    if limit is not None:
        lookback = int(cfg.get("discomfort_lookback_days", 7))
        days = [day - timedelta(days=i) for i in range(1, lookback + 1)]
        series = signals.series("lower_discomfort")
        vals = [v for d in days if (v := series.get(d)) is not None]
        if vals:
            mean = sum(vals) / len(vals)
            if mean >= float(limit):
                return False, (
                    f"molestia lumbar media {mean:.1f} en los últimos "
                    f"{lookback} días (límite {limit} para {label})"
                )

    return True, f"sin señales que desaconsejen {label}"


def evaluate_volume_gates(
    prog_cfg: dict[str, Any],
    signals: Any,
    day: date,
    last_routine_light: str | None = None,
) -> tuple[tuple[bool, str], tuple[bool, str]]:
    """Las DOS puertas de volumen: (añadir serie, subir reps).

    Añadir una serie efectiva y sumar dos repeticiones no son el mismo riesgo,
    así que no comparten freno. La estricta gobierna el modo `sets`; la
    relajada gobierna el modo `volume` y la fase de reps del modo `double`.
    """
    cfg = prog_cfg.get("volume_safety", {}) or {}
    if not cfg.get("enabled", True):
        off = (True, "los frenos de volumen están desactivados en el config")
        return off, off

    sets_gate = _volume_gate(
        cfg, cfg.get("sets_gate", {}) or {}, signals, day, last_routine_light,
        "añadir una serie",
    )
    reps_gate = _volume_gate(
        cfg, cfg.get("reps_gate", {}) or {}, signals, day, last_routine_light,
        "subir repeticiones",
    )
    return sets_gate, reps_gate


# ---------------------------------------------------------------------------
# Modos
# ---------------------------------------------------------------------------


def series_efectivas_vigentes(
    exercise: dict[str, Any], set_cfg: dict[str, Any]
) -> list[dict[str, Any]]:
    """Las series de un ejercicio que la progresión puede mutar.

    Usa `warmup_flags` a secas, igual que el `_split` de `session_builder`, y a
    propósito NO como `_effective` de aquí abajo: `_effective` consulta
    `excludes(set_cfg, "progression_compliance")`, que responde a otra pregunta
    -si el calentamiento cuenta para dar una sesión por limpia-. Lo que hay que
    persistir es lo que `apply_progression` toca, y eso es siempre el trabajo
    efectivo. Mezclar los dos criterios guardaría un día el calentamiento y otro
    no, según un ajuste que habla de otra cosa.
    """
    sets = exercise.get("sets") or []
    flags = warmup_flags(sets, set_cfg, exercise.get("key"))
    return [s for s, f in zip(sets, flags, strict=True) if not f]


def con_carga_vigente(
    exercises: list[dict[str, Any]],
    routine_key: str,
    current_sets: dict[tuple[str, str], list[dict[str, Any]]] | None,
    set_cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    """Los ejercicios del YAML, pero con la carga que de verdad toca hoy.

    `config.yaml` es el punto de PARTIDA de cada ejercicio. En cuanto ese
    ejercicio ha progresado alguna vez, la serie efectiva vigente vive en la
    base de datos y es la que manda; el YAML no se reescribe nunca.

    Tiene que llamarse ANTES de planificar y antes de construir, y con el mismo
    resultado en los dos sitios: si la progresión decidiera sobre los pesos del
    YAML y la sesión se construyera sobre los de la base, Telegram anunciaría
    una subida y Hevy recibiría otra.

    El calentamiento se deja como esté en el YAML porque la progresión no lo
    toca: subir el trabajo de 100 a 105 no cambia con cuánto se calienta.
    """
    salida = copy.deepcopy(exercises)
    if not current_sets:
        return salida

    for ex in salida:
        guardadas = current_sets.get((routine_key, str(ex.get("key"))))
        if not guardadas:
            # Ejercicio recién estrenado (o recién añadido al YAML): todavía no
            # tiene historia, así que arranca donde diga el fichero. Este es el
            # único caso en el que el YAML manda sobre la carga.
            continue
        sets = ex.get("sets") or []
        flags = warmup_flags(sets, set_cfg, ex.get("key"))
        warm = [s for s, f in zip(sets, flags, strict=True) if f]
        ex["sets"] = warm + copy.deepcopy(guardadas)
    return salida


def _effective(exercise: dict[str, Any], set_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    sets = exercise.get("sets") or []
    if not excludes(set_cfg, "progression_compliance"):
        return list(sets)
    flags = warmup_flags(sets, set_cfg, exercise.get("key"))
    return [s for s, f in zip(sets, flags, strict=True) if not f]


def _can_raise_load(effective: list[dict[str, Any]]) -> bool:
    """¿Tiene sentido subir carga en este ejercicio?

    Un ejercicio con todas las series a 0 kg (o sin campo de peso) no es un
    ejercicio ligero: es un ejercicio cuyo peso no se ha registrado todavía.
    Subir 2,5 kg sobre 0 sería inventarse una carga inicial, que es justo lo
    que el sistema no debe hacer sin un dato detrás.
    """
    return any((s.get("weight_kg") or 0) > 0 for s in effective)


def _rep_range(exercise: dict[str, Any], modes: dict[str, Any],
               effective: list[dict[str, Any]]) -> tuple[int, int] | None:
    rr = exercise.get("rep_range")
    if rr and len(rr) == 2:
        return int(rr[0]), int(rr[1])
    reps = [int(s["reps"]) for s in effective if s.get("reps")]
    if not reps:
        return None
    span = int((modes.get("double") or {}).get("deduced_span", 3))
    return min(reps), min(reps) + span


def _plan_double(ex, exercise, effective, modes, prog_cfg,
                 volume_allowed, why) -> ExerciseProgression:
    rng = _rep_range(exercise, modes, effective)
    if rng is None:
        ex.blocked_by = "sin reps objetivo: no hay rango sobre el que progresar"
        return ex
    lo, hi = rng
    reps = [int(s["reps"]) for s in effective if s.get("reps")]
    if not reps:
        ex.blocked_by = "sin reps objetivo"
        return ex

    if min(reps) < hi:
        # Todavía queda recorrido en reps.
        if not volume_allowed:
            ex.blocked_by = f"subida de reps bloqueada: {why}"
            return ex
        m = modes.get("double") or {}
        inc = int(m.get("rep_increment", 1))
        modo = str(m.get("rep_apply_to", "lowest_first"))
        baja = min(reps)
        # El incremento se topa contra el máximo del rango DESDE LA SERIE MÁS
        # BAJA, que es la que se va a subir. Toparlo contra la más alta dejaría
        # `delta` en 0 en cuanto una serie llegara al tope (12/10/10 con hi=12)
        # y la subida sería una que no sube: exactamente el tipo de regla que en
        # este proyecto es error duro.
        delta = min(inc, hi - baja)
        ex.changed = True
        ex.kind = KIND_VOLUME
        ex.rep_delta = delta
        ex.rep_apply_to = modo
        ex.rep_cap = hi
        if modo == "all_sets":
            ex.what = f"+{delta} rep en todas las series (tope {hi})"
        else:
            ex.what = f"{baja}→{baja + delta} reps en la serie más baja"
        return ex

    # Todas al tope: toca carga y vuelta al mínimo. Esto NO cuenta como subida
    # de volumen aunque toque las reps: bajar de 12 a 10 es un recorte.
    if not _can_raise_load(effective):
        # NO es un techo: es un dato que falta. La diferencia importa, porque
        # un techo se resuelve cambiando de ejercicio y esto se resuelve
        # apuntando el peso en Hevy. Confundirlos manda el aviso equivocado.
        ex.needs_data = True
        ex.blocked_by = (
            f"al tope de {hi} reps pero sin carga registrada: apunta el peso real "
            "en Hevy y la progresión de carga se activa sola"
        )
        return ex
    ex.changed = True
    ex.kind = KIND_LOAD
    top = max((s.get("weight_kg") or 0) for s in effective)
    inc = ex_increment(exercise, prog_cfg)
    ex.weight_delta_kg = inc
    # En doble progresión la subida va a TODAS las series efectivas: el rango
    # se ha cumplido en todas, así que todas se han ganado el incremento. Y al
    # ser un delta, una rampa 50/60/65 pasa a 55/65/70 sin aplanarse ni
    # desordenarse, que es lo que pasaba subiendo solo la serie más pesada
    # sesión tras sesión (60→65→70... con las otras dos clavadas en 40).
    ex.apply_to = "all_sets"
    ex.new_reps = lo
    ex.what = f"{_fmt_kg(top)}→{_fmt_kg(top + inc)} kg (y vuelta a {lo} reps)"
    return ex


def ex_increment(exercise: dict[str, Any], prog_cfg: dict[str, Any]) -> float:
    """Incremento del ejercicio, o el global si no declara el suyo."""
    return float(exercise.get("increment_kg", prog_cfg.get("default_increment_kg", 2.5)))


def _plan_volume(ex, exercise, effective, modes, volume_allowed, why) -> ExerciseProgression:
    if not volume_allowed:
        ex.blocked_by = f"volumen bloqueado: {why}"
        return ex
    m = modes.get("volume") or {}

    secs = [int(s["duration_s"]) for s in effective if s.get("duration_s")]
    if secs:
        cap = int(exercise.get("max_seconds", m.get("max_seconds", 60)))
        cur = min(secs)
        if cur >= cap:
            ex.at_ceiling = True
            ex.blocked_by = f"ya está en el techo de {cap} s"
            return ex
        delta = min(int(m.get("seconds_increment", 5)), cap - cur)
        ex.changed = True
        ex.kind = KIND_VOLUME
        ex.duration_delta_s = delta
        ex.duration_cap = cap
        ex.what = f"{cur}→{cur + delta} s"
        return ex

    reps = [int(s["reps"]) for s in effective if s.get("reps")]
    if reps:
        cap = int(exercise.get("max_reps", m.get("max_reps", 30)))
        cur = min(reps)
        if cur >= cap:
            ex.at_ceiling = True
            ex.blocked_by = f"ya está en el techo de {cap} reps"
            return ex
        delta = min(int(m.get("reps_increment", 2)), cap - cur)
        ex.changed = True
        ex.kind = KIND_VOLUME
        ex.rep_delta = delta
        # `all_sets` y no `lowest_first`, y la diferencia con la doble
        # progresión es deliberada. Aquí no hay rampa que preservar -son
        # planchas y core, 3×20 parejo- y subir de una en una triplicaría lo que
        # tarda en progresar un modo que existe justamente para progresar sin
        # tocar la carga. Se declara en el YAML para que se pueda cambiar.
        ex.rep_apply_to = str(m.get("rep_apply_to", "all_sets"))
        ex.rep_cap = cap
        ex.what = f"{cur}→{cur + delta} reps"
        return ex

    ex.blocked_by = "ni reps ni segundos: no hay nada que subir"
    return ex


def _plan_sets(ex, exercise, effective, modes, prog_cfg, sets_allowed, sets_why,
               reps_allowed, reps_why, clean_sessions) -> ExerciseProgression:
    m = modes.get("sets") or {}
    cap = int(exercise.get("max_sets", m.get("max_sets", 5)))
    current = len(effective)

    if current >= cap:
        # Techo de series alcanzado: a partir de aquí manda `then`. Lo que
        # venga después ya no añade series, así que pasa por la puerta
        # relajada, no por la estricta.
        nxt = str(exercise.get("then", m.get("then", DOUBLE)))
        ex.mode = f"{SETS}->{nxt}"
        if nxt == DOUBLE:
            return _plan_double(ex, exercise, effective, modes, prog_cfg,
                                reps_allowed, reps_why)
        if nxt == LOAD:
            return _plan_load(ex, exercise, effective, prog_cfg)
        ex.blocked_by = f"modo `then` no soportado: {nxt}"
        return ex

    # Añadir una serie efectiva: puerta ESTRICTA.
    if not sets_allowed:
        ex.blocked_by = f"serie extra bloqueada: {sets_why}"
        return ex

    needed = int(exercise.get("clean_sessions_required_sets",
                              m.get("clean_sessions_required", 3)))
    if clean_sessions < needed:
        ex.blocked_by = (
            f"{clean_sessions}/{needed} sesiones limpias para añadir una serie"
        )
        return ex

    ex.changed = True
    ex.kind = KIND_VOLUME
    ex.add_sets = 1
    ex.what = f"{current}→{current + 1} series efectivas"
    return ex


def _plan_load(ex, exercise, effective, prog_cfg) -> ExerciseProgression:
    if not _can_raise_load(effective):
        ex.needs_data = True
        ex.blocked_by = "sin carga registrada: no se inventa un peso inicial"
        return ex
    top = max((s.get("weight_kg") or 0) for s in effective)
    inc = ex_increment(exercise, prog_cfg)
    ex.changed = True
    ex.kind = KIND_LOAD
    ex.weight_delta_kg = inc
    ex.what = f"{_fmt_kg(top)}→{_fmt_kg(top + inc)} kg"
    return ex


# ---------------------------------------------------------------------------
# Orquestación
# ---------------------------------------------------------------------------


def plan_progression(
    config: Any,
    routine_key: str,
    signals: Any,
    light: str,
    compliance: dict[str, bool] | None = None,
    clean_sessions: dict[str, int] | None = None,
    deload_active: bool = False,
    sessions_since_progress: dict[str, int] | None = None,
    last_routine_light: str | None = None,
    current_sets: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
    rutina_estrenada: bool | None = None,
    va_a_entrenar: bool | None = None,
) -> ProgressionPlan:
    """Decide la progresión de todos los ejercicios de una rutina.

    `compliance`: por ejercicio, si la última sesión cumplió todas las series
    efectivas a las reps objetivo. `clean_sessions`: sesiones limpias
    consecutivas acumuladas por ejercicio. `sessions_since_progress`: cuántas
    sesiones lleva cada ejercicio sin subir nada, para repartir los cupos por
    turnos en vez de por orden de aparición.

    `last_routine_light`: el semáforo de la última vez que se hizo ESTA rutina,
    que es lo que gobierna las dos puertas de volumen. No es el semáforo de
    ayer ni el de la última sesión de cualquier rutina: si el lunes fue rojo,
    eso dice algo del Día 1, no del Día 3 del viernes.

    Los tres diccionarios de estado van indexados por clave de ejercicio, pero
    el ámbito lo fija quien llama: con `state_scope: routine_exercise` se
    construyen a partir de la historia de ESTA rutina, de modo que la plancha
    lateral del Día 1 y la del Día 3 no comparten racha.

    `current_sets` es la carga vigente guardada. Se decide SOBRE ella, no sobre
    los pesos de `config.yaml`: el YAML es solo el punto de partida. Decidir
    sobre el YAML es lo que hacía que "100→105 kg" se anunciara una y otra vez
    sin llegar nunca a 107,5.
    """
    raw = config.raw if hasattr(config, "raw") else config
    prog_cfg = raw.get("progression", {}) or {}
    modes = prog_cfg.get("modes", {}) or {}
    set_cfg = raw.get("set_types", {}) or {}
    routine = (raw.get("routines", {}) or {}).get(routine_key, {}) or {}
    compliance = compliance or {}
    clean_sessions = clean_sessions or {}
    sessions_since_progress = sessions_since_progress or {}

    default_mode = str(prog_cfg.get("default_progression_type", DOUBLE))
    default_clean = int(prog_cfg.get("default_clean_sessions_required", 1))

    # La carga vigente, no la de partida. `build_session` hace exactamente lo
    # mismo con la misma función: si los dos no partieran de aquí, se anunciaría
    # una subida y se escribiría otra.
    ejercicios = con_carga_vigente(
        routine.get("exercises") or [], routine_key, current_sets, set_cfg
    )

    # Cumplimiento global: la puerta general mira la rutina entera.
    #
    # Son TRES estados, no dos, y se resuelven con el mismo criterio que
    # `weekend_summary`: si lo confirmado ya decide, lo que falta da igual; si
    # no, se declara que no se sabe.
    #
    #   algún False -> False. Hay un incumplimiento confirmado, y que otros
    #                  ejercicios no tengan registro no lo va a borrar.
    #   algún None  -> None.  Los conocidos son todos True, pero un
    #                  desconocido podría ser False: "todas las series
    #                  efectivas" NO está confirmado.
    #   ninguno     -> True.
    #
    # El caso de en medio es el que estaba mal. `all(known)` devolvía True con
    # 3 de 8 ejercicios registrados: la puerta se abría con la evidencia que
    # había, ignorando los 5 de los que no se sabía nada. Añadir un ejercicio
    # nuevo a una rutina en marcha bastaba para subir carga sin haberlo hecho
    # nunca.
    por_ejercicio: dict[str, bool | None] = {}
    for e in ejercicios:
        por_ejercicio[str(e.get("key"))] = compliance.get(e.get("key"))
    valores = list(por_ejercicio.values())
    if any(v is False for v in valores):
        global_compliance: bool | None = False
    elif not valores or any(v is None for v in valores):
        global_compliance = None
    else:
        global_compliance = True

    gate_open, gate_reason = evaluate_gate(
        prog_cfg, light, signals, global_compliance, routine_key, deload_active,
        compliance_por_ejercicio=por_ejercicio,
        rutina_estrenada=rutina_estrenada,
        # El título del `config.yaml` -"Día 2"-, no la clave -`dia_2`-. Este
        # motivo se lee en Telegram y en la vista de auditoría, no en un log.
        titulo_rutina=str(routine.get("title") or "") or None,
        va_a_entrenar=va_a_entrenar,
    )
    (sets_ok, sets_why), (reps_ok, reps_why) = evaluate_volume_gates(
        prog_cfg, signals, signals.day, last_routine_light
    )

    # Que la rutina esté sin estrenar no basta: tiene que ser ADEMÁS lo que
    # cierra la puerta hoy. Un día rojo en una rutina nueva se cierra por el
    # semáforo, y anunciar ahí "la progresión arranca la próxima vez que toque"
    # sería prometer algo que el color no permite prometer.
    #
    # La comprobación se hace contra el texto que devuelve la MISMA función que
    # lo redacta, no contra una subcadena escrita aquí a mano: así el día que
    # alguien reescriba la frase, esto la sigue reconociendo en vez de dejar de
    # reconocerla en silencio y volver al tono de avería.
    es_estreno = rutina_estrenada is False and gate_reason == _por_que_sin_registro(
        por_ejercicio, rutina_estrenada, str(routine.get("title") or "") or None
    )

    plan = ProgressionPlan(
        routine_key=routine_key,
        gate_open=gate_open,
        gate_reason=gate_reason,
        sets_allowed=sets_ok and gate_open,
        sets_reason=sets_why if gate_open else gate_reason,
        reps_allowed=reps_ok and gate_open,
        reps_reason=reps_why if gate_open else gate_reason,
        # Vive bajo `modes.volume` porque el techo es suyo: `at_ceiling` solo lo
        # pone `_plan_volume` (tope de reps o de segundos). Si algún día otro
        # modo marca techo, esta opción tendrá que subir un nivel.
        notify_ceiling=bool((modes.get("volume") or {}).get("on_ceiling_notify", True)),
        estreno=es_estreno,
    )

    for exercise in ejercicios:
        key = str(exercise.get("key"))
        mode = str(exercise.get("progression_type", default_mode))
        ex = ExerciseProgression(
            key=key, name=str(exercise.get("name", key)), mode=mode,
            apply_to=str(exercise.get("apply_to", prog_cfg.get("default_apply_to", "top_set"))),
            waiting=int(sessions_since_progress.get(key, 0)),
        )

        if mode == NONE:
            ex.blocked_by = "ejercicio sin progresión por diseño"
            plan.exercises.append(ex)
            continue

        if not gate_open:
            ex.blocked_by = gate_reason
            plan.exercises.append(ex)
            continue

        effective = _effective(exercise, set_cfg)
        needed = int(exercise.get("clean_sessions_required", default_clean))
        clean = int(clean_sessions.get(key, 0))

        # La cuenta de sesiones limpias solo gobierna la CARGA. El volumen
        # tiene su propia cuenta dentro del modo `sets`.
        load_ready = clean >= needed
        why_load = f"{clean}/{needed} sesiones limpias"

        if mode == LOAD:
            if load_ready:
                _plan_load(ex, exercise, effective, prog_cfg)
            else:
                ex.blocked_by = why_load
        elif mode == DOUBLE:
            before = _plan_double(
                ex, exercise, effective, modes, prog_cfg,
                plan.reps_allowed, plan.reps_reason,
            )
            # La rama de carga de la doble progresión también respeta la cuenta
            # de sesiones limpias; la de reps no, porque subir una repetición no
            # es el mismo salto que subir un disco.
            if before.kind == KIND_LOAD and not load_ready:
                before.changed = False
                before.kind = None
                before.weight_delta_kg = None
                before.new_reps = None
                before.what = ""
                before.blocked_by = why_load
        elif mode == VOLUME:
            _plan_volume(ex, exercise, effective, modes,
                         plan.reps_allowed, plan.reps_reason)
        elif mode == SETS:
            _plan_sets(ex, exercise, effective, modes, prog_cfg,
                       plan.sets_allowed, plan.sets_reason,
                       plan.reps_allowed, plan.reps_reason, clean)
        else:
            ex.blocked_by = f"modo de progresión desconocido: {mode}"

        if ex.changed and not ex.why:
            ex.why = why_load if ex.kind == KIND_LOAD else _volume_why(ex, clean, modes, exercise)
        if ex.at_ceiling:
            plan.ceilings.append(ex.name)
        if ex.needs_data:
            plan.missing_data.append(ex.name)
        plan.exercises.append(ex)

    _enforce_one_kind(plan, prog_cfg)
    return plan


def _volume_why(ex: ExerciseProgression, clean: int, modes: dict[str, Any],
                exercise: dict[str, Any]) -> str:
    if ex.add_sets:
        needed = int(exercise.get("clean_sessions_required_sets",
                                  (modes.get("sets") or {}).get("clean_sessions_required", 3)))
        return f"{clean} sesiones limpias, tope de series aún no alcanzado"
    return "sesión limpia, aún por debajo del techo"


def _enforce_one_kind(plan: ProgressionPlan, prog_cfg: dict[str, Any]) -> None:
    """No mezclar carga y volumen, y como mucho N subidas de volumen.

    ÁMBITO (`conflict_scope`)
    ------------------------
    `exercise` — un mismo ejercicio nunca sube las dos cosas el mismo día. Es
                 el caso realmente peligroso: 3→4 series Y 60→62,5 kg es un
                 +35% de trabajo EN ESE ejercicio, y cuando la semana siguiente
                 venga mal no hay forma de saber cuál de las dos sobró. Que la
                 prensa suba peso el mismo día que el perro de caza suma
                 repeticiones no tiene ese riesgo.
    `session`  — en toda la sesión se sube una cosa o la otra. Más estricto, y
                 con un efecto de INANICIÓN nada obvio: los ejercicios en modo
                 `double` ya al tope de su rango piden carga TODAS las
                 sesiones, así que con `prefer: load` la cadena posterior en
                 modo `sets` no sube una serie jamás.

    CUPOS
    -----
    Con ámbito `exercise` desaparece el freno natural de "una sola clase de
    progresión por sesión", y los cupos pasan a ser lo único que impide que un
    día bueno dispare siete subidas de carga a la vez. Cuando hay más
    candidatos que cupo, pasa primero el que lleva más tiempo esperando: con
    el orden de la rutina, los últimos ejercicios de una rutina larga no
    subirían nunca.
    """
    cfg = prog_cfg.get("volume_safety", {}) or {}
    scope = (
        str(cfg.get("conflict_scope", "exercise"))
        if cfg.get("never_load_and_volume_same_session", True)
        else None
    )
    prefer = str(cfg.get("prefer_on_conflict", KIND_VOLUME))

    if scope == "session":
        loads = [e for e in plan.changes if e.kind == KIND_LOAD]
        vols = [e for e in plan.changes if e.kind == KIND_VOLUME]
        if loads and vols:
            losers, winner = (loads, "volumen") if prefer == KIND_VOLUME else (vols, "carga")
            for e in losers:
                e.changed = False
                e.blocked_by = (
                    f"hoy la sesión sube {winner} y no se mezclan carga y "
                    "volumen en la misma sesión"
                )
    elif scope == "exercise":
        # Un ExerciseProgression solo lleva un `kind`, así que por construcción
        # no puede haber conflicto dentro del mismo ejercicio. Queda explícito
        # para que el día que un modo devuelva las dos cosas, salte aquí.
        for e in plan.changes:
            if e.weight_delta_kg is not None and (e.add_sets or e.new_duration_s is not None):
                e.add_sets = 0
                e.new_duration_s = None
                e.blocked_by = "no se sube carga y volumen en el mismo ejercicio"

    # --- cupos, después de resolver el conflicto ----------------------------
    policy = str(cfg.get("queue_policy", "waiting_longest"))
    for kind, key, label in (
        (KIND_VOLUME, "max_volume_increases_per_session", "volumen"),
        (KIND_LOAD, "max_load_increases_per_session", "carga"),
    ):
        cap = cfg.get(key)
        if cap is None:
            continue
        cands = [e for e in plan.changes if e.kind == kind]
        if len(cands) <= int(cap):
            continue
        if policy == "waiting_longest":
            # `waiting` lo rellena plan_progression; a igualdad, el orden de la
            # rutina decide, que mantiene el resultado estable y reproducible.
            cands.sort(key=lambda e: -e.waiting)
        for e in cands[int(cap):]:
            e.changed = False
            e.blocked_by = (
                f"máximo {cap} subidas de {label} por sesión; le toca en una "
                f"próxima sesión (lleva {e.waiting} esperando)"
            )


def apply_deload_volume(
    exercise: dict[str, Any],
    prog_cfg: dict[str, Any],
    set_cfg: dict[str, Any],
) -> dict[str, Any]:
    """Recorta el volumen de un ejercicio en semana de descarga.

    `special_rules.semana_de_descarga` ya recorta la carga al 60%. Sin esto el
    volumen quedaba intacto y la descarga era media descarga: menos peso pero
    el mismo número de series y las mismas reps.

    El calentamiento no se toca, igual que en el recorte de los días ámbar.
    """
    cfg = prog_cfg.get("deload", {}) or {}
    if not cfg.get("reduce_volume", True):
        return exercise

    sets = exercise.get("sets") or []
    flags = warmup_flags(sets, set_cfg, exercise.get("key"))
    warm = [s for s, f in zip(sets, flags, strict=True) if f]
    work = [s for s, f in zip(sets, flags, strict=True) if not f]

    keep = max(2, int(len(work) * float(cfg.get("sets_factor", 0.70))))
    work = work[:keep] if len(work) > keep else work

    rf = float(cfg.get("reps_factor", 0.80))
    sf = float(cfg.get("seconds_factor", 0.80))
    new_work = []
    for s in work:
        s = dict(s)
        if s.get("reps"):
            s["reps"] = max(1, int(int(s["reps"]) * rf))
        if s.get("duration_s"):
            s["duration_s"] = max(5, int(int(s["duration_s"]) * sf))
        new_work.append(s)

    out = dict(exercise)
    out["sets"] = warm + new_work
    return out
