"""Esquema de la base de datos.

Principios:

- Todo lo que viene de una API externa se guarda además en crudo, y en un solo
  sitio: `raw_json` si la tabla lo tiene, y si no es porque el crudo ya está
  entero en otra parte. Si dentro de cuatro semanas resulta que hace falta un
  campo que hoy no estamos extrayendo, estará ahí en vez de haberse perdido.
  De `daily_metrics.raw_json` se podan las series por minuto (ver
  `repository.podar_crudo`): los escalares, que es donde vive lo que haría
  falta, sobreviven enteros. `activities` no lleva columna porque su crudo vive
  en `data/cache/activities.json`, que se fusiona y nunca se poda.

  Esto era un principio escrito y no cumplido: las tres columnas `raw_json`
  estaban declaradas y ninguna se escribía. Un principio en un docstring que el
  código no aplica es peor que no tenerlo, porque se le da por hecho.
- La tabla `decisions` es un registro append-only: nunca se actualiza una
  decisión, se inserta otra y se marca la anterior con `is_current=False`.
  Así queda el rastro de que a las 09:00 se decidió sin check-in y a las 09:40
  llegó el formulario y cambió el semáforo.
- Cada decisión guarda `config_hash` y `inputs_snapshot_json`: con eso se puede
  reproducir exactamente por qué salió lo que salió, aunque el YAML haya
  cambiado después.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Datos de entrada
# ---------------------------------------------------------------------------


class DailyMetrics(Base):
    """Métricas de bienestar de Garmin, una fila por día."""

    __tablename__ = "daily_metrics"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, unique=True, index=True)

    hrv: Mapped[float | None] = mapped_column(Float)
    rhr: Mapped[float | None] = mapped_column(Float)
    sleep_min: Mapped[int | None] = mapped_column(Integer)
    sleep_score: Mapped[int | None] = mapped_column(Integer)
    body_battery: Mapped[int | None] = mapped_column(Integer)
    readiness: Mapped[int | None] = mapped_column(Integer)

    # Carga acumulada. Se calcula sumando `activities.training_load`, no se
    # lee de un endpoint: así incluye siempre lo que de verdad se hizo.
    load_3d: Mapped[float | None] = mapped_column(Float)
    load_7d: Mapped[float | None] = mapped_column(Float)

    raw_json: Mapped[str | None] = mapped_column(Text)
    fetch_status: Mapped[str] = mapped_column(String(16), default="ok")  # ok|partial|error
    fetch_error: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # NULL = la fila se escribió el día que le toca, con el sistema en marcha.
    # Con fecha = se rellenó DESPUÉS, pidiéndole a Garmin un día ya pasado.
    #
    # Distinguirlas no es contabilidad: una fila recuperada puede tener huecos
    # que la del día no habría tenido -body battery deja de servirse a partir de
    # unos cuatro meses, comprobado- y al analizar hay que poder saber si un
    # hueco significa "esa noche no hubo reloj" o "se pidió demasiado tarde".
    # Sin la marca, las dos cosas son la misma celda vacía.
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime)


class Activity(Base):
    """Actividades de Garmin (principalmente ciclismo desde el Edge 1040).

    Los campos de zonas vienen directamente en el resumen de
    `get_activities_by_date` como hrTimeInZone_1..5 (segundos), así que no hace
    falta una llamada adicional por actividad — importante para no chocar con
    los límites de peticiones de Garmin.
    """

    __tablename__ = "activities"

    id: Mapped[int] = mapped_column(primary_key=True)
    garmin_activity_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)

    date: Mapped[date] = mapped_column(Date, index=True)
    start_time_local: Mapped[datetime | None] = mapped_column(DateTime)
    name: Mapped[str | None] = mapped_column(String(255))
    type_key: Mapped[str | None] = mapped_column(String(64), index=True)
    is_cycling: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    duration_s: Mapped[float | None] = mapped_column(Float)
    moving_duration_s: Mapped[float | None] = mapped_column(Float)
    distance_m: Mapped[float | None] = mapped_column(Float)
    elevation_gain_m: Mapped[float | None] = mapped_column(Float)
    elevation_loss_m: Mapped[float | None] = mapped_column(Float)

    avg_hr: Mapped[float | None] = mapped_column(Float)
    max_hr: Mapped[float | None] = mapped_column(Float)

    # Segundos en cada zona de FC.
    hr_zone_1_s: Mapped[float | None] = mapped_column(Float)
    hr_zone_2_s: Mapped[float | None] = mapped_column(Float)
    hr_zone_3_s: Mapped[float | None] = mapped_column(Float)
    hr_zone_4_s: Mapped[float | None] = mapped_column(Float)
    hr_zone_5_s: Mapped[float | None] = mapped_column(Float)

    training_load: Mapped[float | None] = mapped_column(Float)
    training_load_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    aerobic_te: Mapped[float | None] = mapped_column(Float)
    anaerobic_te: Mapped[float | None] = mapped_column(Float)

    # suave | media | intensa | desconocida
    intensity_level: Mapped[str | None] = mapped_column(String(16), index=True)
    # zones | fallback_te | none  -> de dónde salió la clasificación
    classification_source: Mapped[str | None] = mapped_column(String(16))

    # Esta tabla NO tiene `raw_json`, y es la única de las tres que no lo
    # necesita: el crudo de cada salida está entero en
    # `data/cache/activities.json`, que se fusiona en cada refresco y nunca se
    # poda. La columna estuvo declarada aquí sin que nadie la escribiera nunca,
    # que es peor que no tenerla: parecía una copia de seguridad y no lo era.
    fetched_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (Index("ix_activities_date_cycling", "date", "is_cycling"),)


class Checkin(Base):
    """Respuestas del formulario diario de la PWA."""

    __tablename__ = "checkins"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, unique=True, index=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    fatigue: Mapped[int | None] = mapped_column(Integer)
    mood: Mapped[int | None] = mapped_column(Integer)
    upper_discomfort: Mapped[int | None] = mapped_column(Integer)
    lower_discomfort: Mapped[int | None] = mapped_column(Integer)
    sleep_quality: Mapped[int | None] = mapped_column(Integer)
    training_desire: Mapped[int | None] = mapped_column(Integer)
    # Sustituye al RPE de Hevy, que no existe (comprobado: la API no lo expone
    # ni al leer ni al escribir). Opcional: si ayer no hubo entreno, va nulo.
    yesterday_rpe: Mapped[int | None] = mapped_column(Integer)

    comments: Mapped[str | None] = mapped_column(Text)


# ---------------------------------------------------------------------------
# Salida del motor
# ---------------------------------------------------------------------------


class Decision(Base):
    """Una decisión del motor. Append-only: ver `is_current`."""

    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    light: Mapped[str] = mapped_column(String(8))  # green | amber | red
    trigger_rule: Mapped[str | None] = mapped_column(String(64))
    # Todas las reglas que dispararon, no solo la primera, y las que se
    # saltaron por falta de datos. Es lo que hace depurable un ámbar raro.
    fired_rules_json: Mapped[str | None] = mapped_column(Text)
    skipped_rules_json: Mapped[str | None] = mapped_column(Text)

    # Fotografía de las señales usadas, para poder reproducir la decisión.
    inputs_snapshot_json: Mapped[str | None] = mapped_column(Text)
    config_hash: Mapped[str | None] = mapped_column(String(32))

    # checkin | fallback_0900 | manual | recompute
    source: Mapped[str] = mapped_column(String(24), default="checkin")
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    planned_session_json: Mapped[str | None] = mapped_column(Text)
    bike_recommendation_json: Mapped[str | None] = mapped_column(Text)

    # Qué subió hoy y por qué. Se guarda por dos motivos independientes.
    #
    # El primero es poder contestar "¿por qué subió el hip thrust el día 12?"
    # tres semanas después. Hasta ahora la progresión solo existía dentro del
    # mensaje de Telegram, y un mensaje no es un registro.
    #
    # El segundo es que la reconciliación de la noche LA NECESITA. Un ejercicio
    # que sube por la mañana tiene que empezar racha de cero, y de noche eso ya
    # no se puede deducir: la sesión guardada dice qué se planificó, no qué
    # cambió respecto a ayer. Sin esto la racha sobreviviría a la subida y el
    # ejercicio podría volver a subir al día siguiente.
    progression_json: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_decisions_date_current", "date", "is_current"),)


class ExerciseTarget(Base):
    """Carga objetivo actual de cada ejercicio, y su racha de sesiones limpias.

    ÁMBITO: la clave es (rutina, ejercicio), no el ejercicio solo.

    La plancha lateral aparece en Día 1 y en Día 3 con dosis distintas, y son
    progresiones independientes: si compartiesen fila, una de las dos subiría
    con las sesiones limpias de la otra y el techo de una cortaría el de la
    otra. Esto refleja `progression.volume_safety.state_scope: routine_exercise`
    en el YAML; si algún día ese ajuste pasa a `exercise`, esta tabla tiene que
    cambiar con él o las dos capas dirán cosas distintas.
    """

    __tablename__ = "exercise_targets"

    id: Mapped[int] = mapped_column(primary_key=True)
    routine_key: Mapped[str] = mapped_column(String(64), index=True)
    exercise_key: Mapped[str] = mapped_column(String(64), index=True)
    template_id: Mapped[str | None] = mapped_column(String(64))

    # LAS SERIES EFECTIVAS VIGENTES, tal y como quedaron tras la última
    # progresión: `[{"reps": 12, "weight_kg": 105.0}, ...]`.
    #
    # Esta columna es la fuente de verdad de la carga. `config.yaml` es el punto
    # de PARTIDA -de dónde sale cada ejercicio la primera vez- y nada más; en
    # cuanto hay una progresión manda esto. Sin esta columna la sesión se
    # construía siempre desde el YAML y el incremento se sumaba encima, así que
    # la carga oscilaba entre dos valores para siempre: Telegram anunciaba
    # "100→105 kg" cada pocas semanas y la rutina volvía a 100 a la siguiente.
    # No daba ningún error; solo no progresaba nunca.
    #
    # Se guarda la LISTA entera y no solo el peso máximo porque la rampa importa
    # y no siempre es reconstruible: en modo `load` el incremento va únicamente a
    # la serie más pesada, así que 40/40/40 pasa a 42,5/40/40. Guardando solo el
    # tope habría que repartirlo al releer, y repartirlo mal es cambiar el
    # entrenamiento sin decirlo.
    #
    # Solo las EFECTIVAS. Los calentamientos salen del YAML en cada
    # construcción, que es lo que ya hacía la progresión: `apply_progression`
    # separa calentamiento de serie efectiva y solo toca la segunda.
    current_sets_json: Mapped[str | None] = mapped_column(Text)
    # Proyección consultable de lo anterior: el peso de la serie efectiva más
    # pesada. NO es una segunda verdad -se calcula al escribir, en el mismo
    # sitio y en la misma transacción- pero evita que mirar la progresión de un
    # ejercicio en SQL obligue a parsear JSON. `docs/analisis.md` la quiere.
    current_target_kg: Mapped[float | None] = mapped_column(Float)
    # Racha de sesiones limpias consecutivas. Se compara con
    # `clean_sessions_required` del ejercicio (2 en cadena posterior).
    clean_streak: Mapped[int] = mapped_column(Integer, default=0)
    # ¿La ÚLTIMA sesión se completó a las reps objetivo?
    #
    # No se deduce de `clean_streak > 0`. Son dos cosas distintas: la racha se
    # pone a cero también cuando un ejercicio acaba de progresar, y ese día el
    # cumplimiento fue bueno. Con un solo campo, el día siguiente a cada subida
    # se leería como "la última sesión se falló", que es justo lo contrario de
    # lo que pasó, y la puerta de la progresión se cerraría sola.
    last_compliant: Mapped[bool | None] = mapped_column(Boolean)
    # Sesiones de ESTA rutina desde la última vez que el ejercicio progresó.
    #
    # Es el turno en la cola de los cupos. Cuando hay más candidatos a subir que
    # `max_volume_increases_per_session`, `queue_policy: waiting_longest` deja
    # pasar primero al que lleva más esperando; a igualdad manda el orden de la
    # rutina. Con este contador siempre a cero, el desempate por orden es lo
    # ÚNICO que decide y los últimos ejercicios de una rutina larga no suben
    # jamás: en 140 días simulados, el perro de caza y la plancha lateral -los
    # dos de estabilidad lumbar- perdían el cupo todas las sesiones mientras la
    # prensa subía carga. El sistema seguía progresando y el mensaje diario
    # seguía siendo correcto; lo que se rompía en silencio era el orden de
    # prioridades, justo al revés de "antes volumen que carga".
    #
    # Vive aquí y no en memoria porque el proceso se reinicia y la cola no puede
    # empezar de cero cada vez que se despliega.
    #
    # `server_default` y no solo `default=0`: son cosas distintas y aquí la
    # diferencia decide si el sistema arranca. `default` lo rellena Python al
    # insertar, así que no existe para el `ALTER TABLE ... ADD COLUMN` que hace
    # `ensure_schema`; una columna NOT NULL sin defecto EN LA BASE no se puede
    # añadir a una tabla que ya tiene filas, y `ensure_schema` -con razón- se
    # niega a arrancar en vez de inventarse el valor. La tabla está vacía hoy y
    # la migración pasaría, pero en cuanto se entrene una vez deja de estar
    # vacía, y entonces el fallo aparecería en el despliegue siguiente: de
    # madrugada, en Umbrel y sin nadie mirando.
    sessions_since_progress: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    # Sesiones SEGUIDAS levantando menos peso del que pedía el plan del día, y la
    # más pesada de ellas. Es la memoria de la adopción hacia abajo
    # (`app/engine/adoption.py`), que no baja el objetivo a la primera: una
    # sesión más floja casi siempre es la máquina ocupada, y bajar por eso es la
    # forma silenciosa de que un programa se desinfle.
    #
    # Tienen que persistir, y no es un detalle. El contador se reinicia con cada
    # despliegue si vive en memoria, y como hacen falta varias sesiones seguidas
    # para bajar, un reinicio semanal dejaría la bajada INALCANZABLE: el objetivo
    # se quedaría para siempre por encima de lo que se levanta, que es
    # exactamente el desfase que este mecanismo existe para cerrar.
    #
    # `server_default` en el contador por lo mismo que arriba: `ensure_schema`
    # añade columnas con `ALTER TABLE`, y una NOT NULL sin defecto EN LA BASE no
    # se puede añadir a una tabla que ya tiene filas.
    below_plan_streak: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    # Nullable a propósito: "no hay ninguna sesión por debajo" no es "la mejor
    # sesión por debajo fue de 0 kg". Un 0 aquí se adoptaría como objetivo.
    below_plan_best_kg: Mapped[float | None] = mapped_column(Float)
    last_progressed_date: Mapped[date | None] = mapped_column(Date)
    last_session_date: Mapped[date | None] = mapped_column(Date)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("routine_key", "exercise_key", name="uq_target_routine_ex"),
    )


class RoutineState(Base):
    """Cómo fue la última vez que se entrenó CADA rutina.

    Es el reloj de los frenos de volumen, y por eso es por rutina y no global:
    un rojo el lunes no tiene por qué cancelar la subida del viernes, que es
    otra sesión con otros ejercicios. Guardarlo en una sola variable global
    haría que cualquier día malo congelase el programa entero.

    Solo se escribe cuando la sesión se ha EJECUTADO. Decidir que hoy toca
    fuerza no es haberla hecho.
    """

    __tablename__ = "routine_states"

    id: Mapped[int] = mapped_column(primary_key=True)
    routine_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    last_light: Mapped[str | None] = mapped_column(String(8))
    last_trained_date: Mapped[date | None] = mapped_column(Date)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ProgramState(Base):
    """Estado del programa que no es de ningún día ni de ninguna rutina.

    Fila única (`id = 1`). De momento solo guarda la última descarga concedida,
    pero es una tabla y no una constante porque ese dato TIENE que sobrevivir a
    un reinicio: la descarga se coloca con un margen de semanas (`jitter_weeks`)
    y sin recordar cuál fue la última, un reinicio en la semana equivocada la
    repite o se la salta, y en ninguno de los dos casos avisa.

    `program_start` NO está aquí a propósito: sale de `config.yaml`, que es el
    sitio donde el usuario lo pone y puede corregirlo. Tenerlo en los dos
    lugares invita a que discrepen.
    """

    __tablename__ = "program_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    last_deload_start: Mapped[date | None] = mapped_column(Date)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class RuleState(Base):
    """Reglas especiales con efecto prolongado (retirada de peso muerto, deload)."""

    __tablename__ = "rule_states"

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_name: Mapped[str] = mapped_column(String(64), index=True)
    # A qué se aplica: una clave de ejercicio, una rutina, o "*" para global.
    entity: Mapped[str] = mapped_column(String(64), default="*")

    active_from: Mapped[date] = mapped_column(Date)
    active_until: Mapped[date | None] = mapped_column(Date)
    reason: Mapped[str | None] = mapped_column(Text)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime)

    # El `action` de la regla, tal cual: es LO QUE HACE (qué ejercicio retira,
    # qué factor de carga aplica). Sin esto, una regla rescatada del disco
    # volvía con la acción vacía y dejaba de hacer nada, en silencio: el peso
    # muerto retirado catorce días reaparecía al primer reinicio, y el mensaje
    # seguía diciendo que la regla estaba activa.
    action_json: Mapped[str | None] = mapped_column(Text)
    notify: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (Index("ix_rule_states_active", "rule_name", "entity", "active_until"),)


class PendingStrength(Base):
    """Sesión de fuerza aplazada por un día rojo. No se salta, se recupera."""

    __tablename__ = "pending_strength"

    id: Mapped[int] = mapped_column(primary_key=True)
    routine_key: Mapped[str] = mapped_column(String(64), index=True)
    deferred_from: Mapped[date] = mapped_column(Date)
    expires_on: Mapped[date | None] = mapped_column(Date)
    resolved_on: Mapped[date | None] = mapped_column(Date)
    # pending | recovered | expired | cancelled
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)


# ---------------------------------------------------------------------------
# Histórico y auditoría
# ---------------------------------------------------------------------------


class WorkoutLog(Base):
    """Lo que realmente se entrenó, leído de Hevy."""

    __tablename__ = "workout_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    hevy_workout_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    date: Mapped[date] = mapped_column(Date, index=True)

    routine_key: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(String(255))
    duration_s: Mapped[int | None] = mapped_column(Integer)
    total_sets: Mapped[int | None] = mapped_column(Integer)
    total_volume_kg: Mapped[float | None] = mapped_column(Float)
    # ¿Se completaron todas las series con las reps objetivo? Es la puerta (b)
    # de la progresión.
    all_sets_at_target: Mapped[bool | None] = mapped_column(Boolean)

    raw_json: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class HevyWrite(Base):
    """Auditoría de cada escritura en Hevy. Si la API falla, queda registrado."""

    __tablename__ = "hevy_writes"

    id: Mapped[int] = mapped_column(primary_key=True)
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("decisions.id"))
    date: Mapped[date] = mapped_column(Date, index=True)

    routine_key: Mapped[str | None] = mapped_column(String(64))
    hevy_routine_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))  # ok | error | skipped | dry_run
    http_status: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[str | None] = mapped_column(Text)
    written_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Notification(Base):
    """Mensajes enviados (o intentados) por Telegram. Append-only.

    POR QUÉ NO HAY UNIQUE SOBRE (date, kind)
    ----------------------------------------
    Lo hubo, y rompía el sistema entero de la peor forma posible.

    Rehacer el check-in es una función declarada -`CheckinIn.day` existe justo
    para eso-. Con la restricción puesta, el segundo envío del día seguía este
    camino: se guardaba el check-in, se decidía, se escribía la rutina en Hevy,
    se ENVIABA el mensaje de Telegram, y al ir a apuntar el envío saltaba el
    `UNIQUE`. La excepción reventaba la petición y hacía `rollback` de toda la
    transacción, así que la decisión no quedaba guardada.

    Resultado: el usuario recibía en el móvil un plan que no existía en la base
    de datos, y la API le contestaba un error de SQLAlchemy ilegible. La
    restricción no impedía el segundo mensaje -ya se había mandado cuando
    saltaba-: solo destruía el registro de lo que sí había pasado.

    Cada intento de aviso es una fila. Dos mensajes en el móvil son dos filas,
    porque el registro tiene que parecerse a la realidad: colapsarlos en uno
    haría que el histórico dijera que solo se avisó una vez.
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    kind: Mapped[str] = mapped_column(String(32))  # decision | special_rule | error
    channel: Mapped[str] = mapped_column(String(16), default="telegram")
    status: Mapped[str] = mapped_column(String(16))  # sent | error | dry_run
    body: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (Index("ix_notifications_date_kind", "date", "kind"),)


class LoadAdoption(Base):
    """Cada vez que la carga ejecutada en Hevy movió -o intentó mover- el objetivo.

    Append-only, y con las adopciones RECHAZADAS dentro. Un tope de salto que
    actúa sin dejar rastro es un tope que nadie puede corregir: el ejercicio se
    queda quieto, el motivo se pierde con el proceso y la única pista es un
    número que no cambia.

    La otra razón de que sea una tabla y no una línea de log: el desfase entre
    la noche y la mañana. La adopción ocurre al reconciliar, a las 22:30, cuando
    no hay nadie leyendo el móvil; hay que contarla en el mensaje de las 06:30,
    que es el que dice "hip thrust 3x8 a 62,5" y tiene que poder explicar el
    62,5. `reported_at` es lo que impide contarla dos veces -o ninguna- si el
    trabajo de la mañana se reintenta.

    Y a los tres meses es lo único que contesta a "¿por qué esto está en 62,5 y
    no en 70?", porque `exercise_targets` solo guarda el valor de hoy.
    """

    __tablename__ = "load_adoptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    routine_key: Mapped[str] = mapped_column(String(64), index=True)
    exercise_key: Mapped[str] = mapped_column(String(64), index=True)

    direction: Mapped[str] = mapped_column(String(8))  # up | down
    # Los cuatro pesos: lo que pedía el plan del día ya recortado, lo que se
    # levantó, y el objetivo antes y después. Menos de cuatro no reconstruye la
    # decisión: sin `prescribed_kg` no se distingue una bajada real de una
    # semana de descarga, y sin `before_kg` no se ve el tamaño del salto.
    prescribed_kg: Mapped[float | None] = mapped_column(Float)
    executed_kg: Mapped[float | None] = mapped_column(Float)
    before_kg: Mapped[float | None] = mapped_column(Float)
    after_kg: Mapped[float | None] = mapped_column(Float)

    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # NULL = todavía no se ha contado en ningún mensaje.
    reported_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        Index("ix_load_adoptions_pendientes", "reported_at", "date"),
    )


class SessionPerformance(Base):
    """Cómo se sintió la mañana frente a cómo salió de verdad la sesión.

    Una fila por sesión evaluada, y APPEND-ONLY en el sentido fuerte: la fila
    guarda el juicio que se pudo hacer ESE día, con el histórico que había ese
    día, y no se vuelve a tocar. Los percentiles no son verdades absolutas -son
    la posición dentro de una distribución que sigue creciendo-, así que una fila
    escrita con `n_sessions_base=18` dice algo distinto de la misma fila escrita
    con 200 sesiones detrás. Recalcularlas todas cada noche daría un contador que
    cambia de valor sin que haya pasado nada nuevo, y un contador que reescribe
    su pasado no sirve para lo único que tiene que hacer: estar ahí, con el mismo
    número de ayer, la mañana en que uno se levanta convencido de que no puede.

    `source_key` es la llave natural -`hevy:<workout_id>` o `garmin:<activity_id>`-
    y va con UNIQUE porque es lo que impide que reevaluar la misma sesión la
    cuente dos veces. Aquí sí puede llevarlo, al revés que en `notifications`:
    el fallo de aquella era que el UNIQUE saltaba DESPUÉS de mandar el mensaje y
    tiraba abajo la transacción que guardaba la decisión. Esta fila se escribe
    antes de avisar de nada, y el aviso va aparte, por `reported_at`.

    LOS COMPONENTES VAN SUELTOS A PROPÓSITO
    ---------------------------------------
    `performance_index` es una mezcla, y toda mezcla oculta de dónde viene. Un
    75 puede ser cumplimiento perfecto con RPE altísimo o lo contrario, y son dos
    sesiones que no se parecen en nada. Las columnas `comp_*` guardan cada pieza
    antes de promediarla, y `components_json` guarda los números crudos de los
    que sale cada pieza, para poder rehacer la cuenta dentro de seis meses sin
    depender de que el código de hoy siga existiendo.

    En bici NO hay potencia -comprobado sobre las 89 actividades cacheadas: no
    hay un solo registro con vatios-, así que el esfuerzo se mide por frecuencia
    cardiaca relativa a las zonas, velocidad y desnivel. Es peor que un
    potenciómetro y hay que decirlo, no disimularlo: por eso son tres columnas y
    no una.

    ESTO NO DECIDE NADA
    -------------------
    Ninguna columna de esta tabla entra en el motor. No calibra el semáforo, no
    mueve cargas, no cambia la sesión del día. Es un espejo, y un espejo que
    empujara sería otra cosa.
    """

    __tablename__ = "session_performance"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)  # strength | bike
    source_key: Mapped[str] = mapped_column(String(96), unique=True, index=True)

    routine_key: Mapped[str | None] = mapped_column(String(64))
    garmin_activity_id: Mapped[int | None] = mapped_column(Integer)

    # --- La percepción: el formulario de esa mañana, tal cual se rellenó ------
    perceived_fatigue: Mapped[int | None] = mapped_column(Integer)
    perceived_mood: Mapped[int | None] = mapped_column(Integer)
    perceived_sleep_quality: Mapped[int | None] = mapped_column(Integer)
    perceived_training_desire: Mapped[int | None] = mapped_column(Integer)
    perceived_lower_discomfort: Mapped[int | None] = mapped_column(Integer)
    perceived_upper_discomfort: Mapped[int | None] = mapped_column(Integer)

    perception_index: Mapped[float | None] = mapped_column(Float)
    perception_pct: Mapped[float | None] = mapped_column(Float)

    # --- El rendimiento: cada componente por su lado ------------------------
    # Series y repeticiones frente a lo PRESCRITO ese día, nunca frente a un
    # volumen absoluto: una descarga bien hecha es cumplimiento del 100%.
    comp_compliance: Mapped[float | None] = mapped_column(Float)
    comp_progression: Mapped[float | None] = mapped_column(Float)
    # RPE de la mañana SIGUIENTE (`checkins.yesterday_rpe`), relativizado a la
    # carga que se movió. Por eso una sesión no se puede evaluar del todo hasta
    # el día después, y por eso el aviso sale en el mensaje de mañana.
    comp_rpe: Mapped[float | None] = mapped_column(Float)
    comp_bike_hr: Mapped[float | None] = mapped_column(Float)
    comp_bike_speed: Mapped[float | None] = mapped_column(Float)
    comp_bike_elevation: Mapped[float | None] = mapped_column(Float)
    components_json: Mapped[str | None] = mapped_column(Text)

    performance_index: Mapped[float | None] = mapped_column(Float)
    performance_pct: Mapped[float | None] = mapped_column(Float)

    # --- El cruce ------------------------------------------------------------
    # Positivo = la sesión salió mejor de lo que anunciaba la mañana.
    gap_pct: Mapped[float | None] = mapped_column(Float)
    # perception_worse | perception_better | aligned | na
    #
    # Los tres NOT NULL de abajo llevan `server_default` además del defecto de
    # Python por lo mismo que `exercise_targets.below_plan_streak`: el defecto de
    # Python no existe para `ALTER TABLE ADD COLUMN`, y sin defecto EN LA BASE
    # una columna NOT NULL no se puede añadir a una tabla que ya tiene filas.
    direction: Mapped[str] = mapped_column(
        String(20), default="na", server_default=text("'na'"), index=True
    )
    # Solo las que pasan el umbral. Las demás se guardan igual, porque un
    # contador que solo apunta los días buenos no es un contador, es un cartel.
    dissociation: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0"), index=True
    )
    # Cuántas sesiones había detrás cuando se calcularon los percentiles.
    n_sessions_base: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    # Por qué no se pudo calcular, cuando no se pudo. Nunca un 0 mudo.
    na_reason: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # NULL = todavía no se ha contado en ningún mensaje de Telegram.
    reported_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        Index("ix_session_performance_pendientes", "reported_at", "dissociation"),
    )
