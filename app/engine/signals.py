"""Señales de entrada del motor de decisión.

Todo este módulo son funciones puras: no toca la base de datos ni la red. La
capa de persistencia construye las dataclasses de entrada y este módulo produce
un objeto `Signals` con:

  - los valores de hoy (`values`)
  - la serie histórica de cada señal (`history`), necesaria para las condiciones
    con `consecutive_days`
  - los umbrales adaptativos ya resueltos (`adaptive`), o None si no hay
    histórico suficiente

Decisiones de diseño que conviene tener presentes:

1. `load_2d` / `load_7d` se calculan SUMANDO las actividades, no leyendo un
   endpoint de Garmin. Así la carga es siempre la que de verdad se hizo.
2. Las ventanas de carga TERMINAN LA VÍSPERA y no incluyen el propio día. `load_2d`
   del día d es la carga de [d-2, d-1], siempre dos días enteros y siempre
   pasados. Da igual a qué hora se calcule: a las 07:00 y a las 22:30 sale lo
   mismo, que es lo que uno espera de un número que se guarda en el histórico.

   ESTO ES UN ARREGLO, Y CONVIENE SABER DE QUÉ
   -------------------------------------------
   Antes la ventana era [d-n+1, d] -incluía el día-. Ninguna función estaba mal
   por separado, pero en la junta entre dos de ellas vivía un sesgo: por la
   mañana la salida de hoy todavía no existe, así que el valor de HOY sumaba n-1
   días; los días PASADOS de la misma serie sí llevaban su propia salida dentro
   y sumaban n. `resolve_adaptive_threshold` sacaba el percentil de esa serie, o
   sea que comparaba una suma de n-1 contra una distribución de sumas de n. En
   los 184 días medidos la media de lo comparado era 66,9 contra 99,5: el valor
   de hoy jugaba estructuralmente por debajo y la regla se inclinaba a NO
   disparar, todos los días, sin que se viera leyendo ninguna de las dos
   funciones.

   No se arregló tocando el umbral, que es lo que habría parecido: se arregló
   haciendo que los dos lados sean la MISMA MAGNITUD -carga de los k días que
   terminan ayer, el valor y la distribución-. Un percentil solo significa algo
   si lo que se compara con él está medido igual que lo que lo formó.

   El comentario que había aquí decía «los 3 días anteriores» cuando eran dos y
   además no eran ésos. Se deja escrito porque durante semanas se razonó sobre
   esa regla dando por buena esa frase.
3. Las líneas base (HRV, FC reposo) se calculan con los días ANTERIORES a hoy.
   Meter el valor de hoy en su propia media lo amortiguaría justo cuando más
   interesa que destaque.
4. Los umbrales adaptativos se calculan sobre la ventana que termina AYER, por
   el mismo motivo: no se compara un valor contra una distribución que lo
   contiene.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

# Orden ISO: lunes = 1.
WEEKDAY_NAMES = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]

UNKNOWN = "desconocida"

# Las dos preguntas de Sí/No del check-in, por su nombre, aquí y no en el
# `config.yaml`.
#
# En el config viven sus etiquetas -el texto que se lee en el móvil, que puede
# reescribirse cuantas veces haga falta- pero NO la decisión de cuál de las dos
# manda en el mensaje. Esa es de código: `CLAVE_VOY_A_ENTRENAR` es la que apaga
# la prescripción del día, y ponerla como bandera por pregunta en el YAML sería
# inventar una opción que nadie va a cambiar nunca, con el efecto secundario de
# que un `false` por descuido dejaría al sistema prescribiendo sesión los días
# que has dicho que no vas. `config_loader` comprueba a cambio que la clave siga
# existiendo en `checkin_preguntas`: si se borrara, el motor estaría leyendo una
# señal que nadie escribe y se quedaría en `None` para siempre, que es el
# comportamiento anterior con cara de normal.
#
# Ninguna de las dos está en `checkin_sliders`, y eso no es una omisión: estar en
# esa lista es tener permiso para que una regla del semáforo te mire. Estas dos
# son intenciones, no medidas. Se guardan, se cuentan y se correlacionan; no
# deciden el color.
CLAVE_APETECE = "wants_to_train"
CLAVE_VOY_A_ENTRENAR = "will_train"

# El selector de sesión: qué dijo que iba a hacer hoy.
#
# Aquí por lo mismo que las dos de arriba -el código la busca por su nombre- y
# con una diferencia que importa: ésta NO está en `checkin_sliders` ni en
# `checkin_preguntas`, que son las dos listas que el bucle de `build_signals`
# vuelca en `values`. Su sitio es un campo propio de `Signals`, al lado de
# `rides` y de `intense_count`, porque `values` es el espacio de nombres
# evaluable y una cadena no se evalúa.
CLAVE_SESION_ELEGIDA = "chosen_session"

# Las dos elecciones que NO son una rutina del ciclo.
#
# Se distinguen de las demás en una sola cosa, y es la que tiene efecto: no
# prescriben fuerza y no mueven nada del ciclo. Lo que sí hacen es dejar dicho
# que ese día hubo actividad, que es lo que las separa de no contestar.
#
# `bici` no es lo mismo que la clasificación de salidas de `cycling`: aquélla
# sale de Garmin y es un hecho medido, ésta es lo que uno declaró a las siete de
# la mañana. Pueden no coincidir, y ése es el motivo de guardar las dos.
ELECCION_BICI = "bici"
ELECCION_OTRO = "otro"
ELECCIONES_SIN_FUERZA = (ELECCION_BICI, ELECCION_OTRO)


class IntensityCountConfigError(ValueError):
    """El bloque de recuento de intensas está mal configurado.

    Dos familias de motivo, y las dos son el mismo fallo:

    1. CLAVES DE CUANDO LIMITABA. `weekly_limit`, `on_budget_exhausted`,
       `max_intense_rides_per_weekend` y `require_green_for_intense` existieron
       y recortaban la salida del fin de semana. Ya no existe nada que las lea.
       Dejarlas pasar en silencio sería la trampa exacta que el sistema lleva
       meses pagando, y encima en la dirección más engañosa que hay: alguien
       escribe `weekly_limit: 2` convencido de que se está poniendo un tope, el
       fichero valida, y no pasa absolutamente nada.

    2. CLAVES DE CUANDO ERA UNA SEMANA NATURAL. `week_starts_on` decía por qué
       día empezaba la cuenta, y la cuenta ya no empieza por ningún día: es una
       ventana rodante que termina hoy. Quien lo reescriba estará eligiendo un
       lunes que no existe.

    Y la ventana que SÍ se lee tiene que estar escrita. Sin `window_days` no se
    supone un 7: se para. Un recuento cuyo periodo se lo inventa el código es
    un número sin unidades en el mensaje de la mañana.
    """


# ---------------------------------------------------------------------------
# Entradas
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DayMetrics:
    """Una fila de wellness de Garmin.

    `raw` son las respuestas completas de las que salen los cinco números, una
    por llamada. Existe porque de esas cuatro respuestas este sistema extrae
    cinco escalares y tira TODO lo demás, y el wellness -al revés que las
    salidas, que quedan enteras en `data/cache/activities.json`- no tiene caché
    ninguna.

    HASTA DÓNDE SIRVE GARMIN HACIA ATRÁS (medido, no supuesto)
    ----------------------------------------------------------
    Aquí ponía que "Garmin no sirve histórico antiguo de sueño ni de body
    battery", y era falso. Nadie lo había comprobado nunca. Sondeando días
    sueltos de antigüedad creciente (`scripts/sondeo_wellness.py`, 2026-09-11):

        HRV, FC en reposo, minutos de sueño, nota de sueño -> a -175 días
        body battery                                       -> a -120 días
        training readiness                                 -> NUNCA, ni ayer

    O sea que el wellness sí se puede rellenar hacia atrás, y por eso existe
    `app/backfill.py`. La frase de antes no era inocua: justificaba no tener
    backfill, y sin backfill las vistas de concordancia y desfase arrancan con
    cero días en vez de con seis meses.

    TRAINING READINESS YA NO ESTÁ, Y NO VA A VOLVER
    -----------------------------------------------
    Fue campo aquí, columna en `daily_metrics` y clave en `values`, siempre a
    None. El sondeo explicó por qué: `get_training_readiness` devuelve lista
    VACÍA los nueve días probados, de -1 a -175. La calcula el reloj, y este
    reloj no la calcula. No era un hueco del backfill pendiente de rellenar,
    era una métrica que este hardware no produce, y una columna que solo puede
    contener None no es un dato pendiente: es ruido con nombre de dato.

    Con ella se ha ido `not_requested`, que existía únicamente para que su
    ausencia no contase como hueco y no dejase TODAS las filas marcadas
    `partial` para siempre. Sin readiness se piden los cinco campos que hay, y
    un hueco vuelve a significar lo que decía: que ese día faltó el dato.

    `raw` no entra en la comparación (`compare=False`): dos filas de wellness
    son la misma fila si coinciden los números. Meterlo en el `__eq__` -y por
    tanto en el `__hash__`, que la dataclass congelada genera de los mismos
    campos- convertiría en no hasheable algo que hoy sí lo es, por un campo que
    ni decide ni se compara.
    """

    date: date
    hrv: float | None = None
    rhr: float | None = None
    sleep_min: int | None = None
    sleep_score: int | None = None
    body_battery: int | None = None
    raw: dict[str, Any] | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class Ride:
    """Una actividad de Garmin ya normalizada.

    `zones` son segundos en Z1..Z5. Puede ser None (actividad importada o sin
    sensor de FC), y entonces se recurre a `classification_fallback`.
    """

    date: date
    duration_s: float | None = None
    distance_m: float | None = None
    zones: tuple[float | None, ...] | None = None
    training_load: float | None = None
    aerobic_te: float | None = None
    anaerobic_te: float | None = None
    is_cycling: bool = True
    activity_id: int | None = None
    name: str | None = None
    # Nada de lo de abajo lo mira el motor: no entra en ninguna regla ni en
    # `load_2d/7d`. Viaja porque la vista 5 lo necesita para juzgar una salida
    # -sin potenciómetro, el esfuerzo se lee en FC relativa a zonas, velocidad y
    # desnivel- y porque el sitio donde se normaliza una actividad de Garmin es
    # este, no dos capas más arriba.
    #
    # El nombre de cada campo es el de su columna en `activities` a propósito:
    # `upsert_activities` copia por nombre recorriendo `CAMPOS_ACTIVIDAD`, así
    # que renombrar uno aquí y no allí deja la columna a NULL sin un solo error.
    elevation_gain_m: float | None = None
    elevation_loss_m: float | None = None
    moving_duration_s: float | None = None
    avg_hr: float | None = None
    max_hr: float | None = None
    avg_speed_mps: float | None = None
    max_speed_mps: float | None = None
    calories: float | None = None
    avg_respiration: float | None = None
    max_respiration: float | None = None
    min_respiration: float | None = None
    max_temp_c: float | None = None
    min_temp_c: float | None = None


@dataclass(frozen=True)
class ClassifiedRide:
    """Salida de `classify_ride`: la actividad más su etiqueta y su carga."""

    ride: Ride
    level: str  # suave | media | intensa | desconocida
    source: str  # zones | fallback_te | none
    load: float  # carga usada para load_2d/7d (real o estimada)
    load_estimated: bool
    # False cuando la carga no se ha podido saber NI estimar y `load` es un 0
    # de relleno. Un 0 de relleno y un día de descanso son el mismo número y
    # significan lo contrario, así que quien sume cargas necesita distinguirlos.
    load_known: bool = True
    zone_pct: dict[int, float] = field(default_factory=dict)

    @property
    def date(self) -> date:
        return self.ride.date

    @property
    def hours(self) -> float:
        return (self.ride.duration_s or 0.0) / 3600.0


@dataclass(frozen=True)
class StrengthSession:
    """Una sesión de fuerza EJECUTADA, leída de Hevy."""

    date: date
    routine_key: str | None = None
    is_hiit: bool = False


@dataclass(frozen=True)
class Checkin:
    """Respuestas del formulario. Cualquiera puede faltar."""

    date: date
    values: dict[str, int | None] = field(default_factory=dict)
    comments: str | None = None


# ---------------------------------------------------------------------------
# Salida
# ---------------------------------------------------------------------------


@dataclass
class Signals:
    """Fotografía completa de las señales de un día."""

    day: date
    values: dict[str, Any] = field(default_factory=dict)
    history: dict[str, dict[date, Any]] = field(default_factory=dict)
    adaptive: dict[str, float | None] = field(default_factory=dict)
    # Notas para el log: por qué una señal salió a None, qué salidas no se
    # pudieron clasificar, etc.
    notes: list[str] = field(default_factory=list)

    # Objetos completos para las capas de arriba (constructor de sesión y
    # recomendación de bici). Fuera de `values` a propósito: `values` es el
    # espacio de nombres que ven las reglas y lo que se serializa en el
    # snapshot, y ahí solo deben vivir señales evaluables.
    rides: list[ClassifiedRide] = field(default_factory=list)
    # Aquí vivía `weekend: WeekendSummary | None`, y se ha borrado el campo
    # entero junto con la función que lo llenaba. Se escribía en cada decisión y
    # no lo leía NADIE: ni una regla, ni el mensaje, ni el panel, ni las
    # métricas. Un campo que solo se escribe es una opción muerta con otra ropa,
    # y encima esta agrupaba por sábado y domingo, que es el calendario fijo
    # otra vez. Lo que hacía falta de ahí -cuántas intensas se llevan encima- lo
    # da ahora `intense_count` sin preguntarle al calendario qué día es.
    #
    # `None` quiere decir NO SE CUENTA, y cubre los tres caminos que llevan ahí:
    # unas señales que nadie ha construido, un config sin el bloque, y un
    # `enabled: false` escrito a propósito. Los tres se parecen en lo único que
    # importa: nadie ha mirado, así que el mensaje no dice nada. Lo que NO puede
    # ser es un `IntensityCount(used=0)`, porque un cero sí afirma algo -«no has
    # hecho ninguna intensa»- y se queda escrito en el histórico igual que uno
    # contado de verdad.
    intense_count: IntensityCount | None = None

    # Lo que eligió en el selector del formulario: una clave de `rotation.order`,
    # `bici`, `otro`, o `None` si no contestó.
    #
    # Campo propio y no una entrada de `values`, por la misma regla que `rides`:
    # `values` solo admite señales evaluables, y esto es una cadena categórica.
    # Metida ahí serviría para que una regla escribiera `{chosen_session: {gte:
    # 7}}` -que no reventaría, simplemente no dispararía nunca- y para que el día
    # que el análisis recorra `values` sacando medias se encuentre un `dia_2`
    # donde esperaba un número.
    #
    # Y no tiene histórico en `history` por lo mismo. Cuando haga falta la serie
    # de lo elegido, sale de `checkins` con una consulta que sabe que es texto.
    sesion_elegida: str | None = None

    def get(self, name: str) -> Any:
        if name in self.values:
            return self.values[name]
        return self.adaptive.get(name)

    def has(self, name: str) -> bool:
        """¿Hay dato para esta señal? None cuenta como 'no hay'."""
        return self.get(name) is not None

    def series(self, name: str) -> dict[date, Any]:
        return self.history.get(name, {})

    def weekday(self) -> str:
        return WEEKDAY_NAMES[self.day.weekday()]

    def snapshot(self) -> dict[str, Any]:
        """Lo que se guarda en `decisions.inputs_snapshot_json`.

        `intense_count` entra aquí desde que el recuento dejó de recortar. Antes
        no hacía falta guardarlo: la cuenta acababa escrita en el motivo del
        recorte de bici y de ahí se podía reconstruir. Al quitar el recorte, ese
        rastro desaparece, y sin esta línea el sistema habría dejado de
        registrar exactamente el dato que se le pidió que registrase. El detalle
        va entero -qué día y de qué tipo fue cada sesión- porque un contador sin
        desglose no se puede auditar y un contador que no se puede auditar acaba
        siendo un número en el que nadie confía.
        """
        return {
            "day": self.day.isoformat(),
            "weekday": self.weekday(),
            "values": {k: v for k, v in sorted(self.values.items())},
            "adaptive": {k: v for k, v in sorted(self.adaptive.items())},
            "notes": list(self.notes),
            "intense_count": (
                self.intense_count.to_dict() if self.intense_count else None
            ),
        }


# ---------------------------------------------------------------------------
# Utilidades numéricas
# ---------------------------------------------------------------------------


def percentile(values: Sequence[float], p: float) -> float | None:
    """Percentil por interpolación lineal entre rangos contiguos.

    Mismo método que numpy por defecto, para que los números que salen aquí
    coincidan con los que se usaron para calibrar el YAML.
    """
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return None
    clean.sort()
    if len(clean) == 1:
        return clean[0]
    k = (len(clean) - 1) * (p / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return clean[int(k)]
    return clean[lo] + (clean[hi] - clean[lo]) * (k - lo)


def mean_excluding_outliers(values: Sequence[float], exclude_outliers: bool) -> float | None:
    """Media de la ventana, opcionalmente sin el máximo y el mínimo.

    Solo se descartan los extremos si quedan al menos 3 valores. Con 4 datos,
    quitar máximo y mínimo dejaría una media de 2 puntos, que no es más robusta
    que la original: es simplemente otra cifra con menos información.
    """
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return None
    if exclude_outliers and len(clean) >= 5:
        clean = sorted(clean)[1:-1]
    return sum(clean) / len(clean)


def week_start(day: date, starts_on: str = "monday") -> date:
    """Primer día de la semana que contiene `day`."""
    try:
        target = WEEKDAY_NAMES.index(starts_on)
    except ValueError:  # pragma: no cover - lo valida el config_loader
        target = 0
    return day - timedelta(days=(day.weekday() - target) % 7)


# Aquí estaba `previous_weekday`, que daba la última fecha estrictamente
# anterior con un día de la semana dado. Sus dos únicos llamantes eran
# `weekend_summary` y `_intense_rides_this_weekend`, y los dos se han borrado
# con el recuento rodante. Se va con ellos en vez de quedarse "por si acaso":
# una utilidad de calendario sin llamantes, en un sistema del que se acaba de
# echar el calendario, es exactamente el atajo que lo devolvería.
#
# `week_start` se queda, y no es incoherencia: lo usan el ciclo de descarga
# (`deload_every_weeks`) y `tendencia`, donde la semana natural SÍ es la unidad
# real —los bloques de entrenamiento se cuentan en semanas—. Lo que no puede
# ser unidad es el periodo sobre el que se le informa al usuario de lo que
# lleva hecho, porque ahí el corte del lunes no describe nada del cuerpo.


# ---------------------------------------------------------------------------
# Clasificación de salidas
# ---------------------------------------------------------------------------


def zone_percentages(zones: Iterable[float | None] | None) -> dict[int, float]:
    """Reparto porcentual del tiempo por zona de FC.

    El porcentaje se calcula sobre el tiempo TOTAL EN ZONAS, no sobre la
    duración de la actividad: las paradas y el tiempo sin FC válida no deben
    diluir el porcentaje de Z4-Z5.
    """
    if not zones:
        return {}
    secs = [float(z or 0.0) for z in zones]
    total = sum(secs)
    if total <= 0:
        return {}
    return {i + 1: (s / total) * 100.0 for i, s in enumerate(secs)}


def classify_ride(ride: Ride, cycling_cfg: dict[str, Any]) -> ClassifiedRide:
    """Etiqueta una salida como suave / media / intensa / desconocida.

    Orden de preferencia:
      1. reparto por zonas de FC (`classification`)
      2. efecto de entrenamiento anaeróbico (`classification_fallback`)
      3. `desconocida` — nunca se asume suave, que sería la lectura optimista
    """
    pcts = zone_percentages(ride.zones)
    level: str | None = None
    source = "none"

    if pcts:
        source = "zones"
        for entry in cycling_cfg.get("classification", []):
            if entry.get("always"):
                level = entry["level"]
                break
            zones = entry.get("zones") or []
            min_pct = float(entry.get("min_time_pct", 0))
            total = sum(pcts.get(int(z), 0.0) for z in zones)
            # Estricto: "más del 30%" es > 30, no >= 30.
            if total > min_pct:
                level = entry["level"]
                break

    if level is None:
        fallback = cycling_cfg.get("classification_fallback", {}) or {}
        metric = ride.anaerobic_te if fallback.get("use") == "anaerobic_training_effect" else None
        if metric is not None:
            source = "fallback_te"
            if metric >= float(fallback.get("intensa_if_gte", 2.0)):
                level = "intensa"
            elif metric >= float(fallback.get("media_if_gte", 1.0)):
                level = "media"
            else:
                level = "suave"
        else:
            source = "none"
            level = str(fallback.get("on_no_data", UNKNOWN))

    load, estimated, known = _ride_load(ride, level, cycling_cfg)
    return ClassifiedRide(
        ride=ride,
        level=level,
        source=source,
        load=load,
        load_estimated=estimated,
        load_known=known,
        zone_pct=pcts,
    )


def _ride_load(
    ride: Ride, level: str, cycling_cfg: dict[str, Any]
) -> tuple[float, bool, bool]:
    """Carga de la salida: (valor, ¿estimada?, ¿se sabe?).

    El tercer elemento existe porque los dos primeros no distinguían "salió a
    rodar y no gastó nada" de "salió a rodar y no sé cuánto gastó". Los dos
    devolvían `0.0, False`, que sumado a `load_7d` es literalmente lo mismo que
    un día de sofá.
    """
    if ride.training_load is not None:
        return float(ride.training_load), False, True

    est = (cycling_cfg.get("load", {}) or {}).get("fallback_estimate", {}) or {}
    if not est.get("enabled") or not ride.duration_s:
        return 0.0, False, False

    per_hour = (est.get("load_per_hour") or {}).get(level)
    if per_hour is None:
        # Sin factor para ese nivel (p. ej. `desconocida`): no inventamos.
        return 0.0, False, False
    return (ride.duration_s / 3600.0) * float(per_hour), True, True


def classify_all(rides: Iterable[Ride], cycling_cfg: dict[str, Any]) -> list[ClassifiedRide]:
    types = set(cycling_cfg.get("activity_types") or [])
    out: list[ClassifiedRide] = []
    for ride in rides:
        if types and not ride.is_cycling:
            continue
        out.append(classify_ride(ride, cycling_cfg))
    return out


# ---------------------------------------------------------------------------
# Carga acumulada
# ---------------------------------------------------------------------------


def rolling_load(
    rides: Sequence[ClassifiedRide], day: date, window_days: int
) -> float | None:
    """Suma de carga en la ventana de `window_days` que termina en `day`.

    Devuelve `None` si alguna salida de la ventana tiene carga desconocida.
    Antes esa salida entraba como 0 y el resultado era un número más bajo que
    el real, sin marca de ninguna clase: `carga_acumulada` comparaba contra su
    umbral una carga incompleta y salía verde el día que tocaba ámbar.

    `None` no es un fallo: es "hoy este dato no se puede dar". El semáforo ya
    sabe tratarlo -la regla se marca como no evaluable y se dice en el
    mensaje-, y el percentil adaptativo lo descarta de la ventana en vez de
    calibrarse contra un cero falso, que es lo que rebajaba el umbral para
    todos los días siguientes.

    NO MIRAR NO ES NO ENTRENAR, Y ESTA FUNCIÓN LAS CONFUNDÍA
    --------------------------------------------------------
    Un día sin salidas daba `sum([]) == 0.0`, y eso está bien cuando el día se
    miró y no hubo bici. Pero daba lo mismo -un cero perfectamente creíble-
    para un día ANTERIOR a la primera actividad que se ha llegado a leer, que
    es un día del que no se sabe absolutamente nada. Los dos ceros salían
    idénticos de aquí, y a partir de ahí ya no había forma de distinguirlos.

    Lo que eso rompía no era esta función, era la guarda de la de al lado.
    `resolve_adaptive_threshold` exige `min_days_required` días para calcular
    un percentil, y esa exigencia existe justamente para no calibrar contra
    cuatro datos. Medido sobre el histórico real: el 2026-03-15 la ventana de
    60 días de `load_2d_p90` llevaba 52 ceros de días sin ningún dato y 8 días
    de verdad. El mínimo de 30 se cumplía de sobra, el percentil salía de los
    ceros, y la primera salida real lo superaba sin despeinarse. En el replay
    entero `carga_acumulada` disparó 32 veces de 181 y se saltó CERO: la guarda
    que tenía que decir "no sé" no lo dijo ni una vez en seis meses.

    Así que la ventana que empieza antes de la primera observación no vale
    cero: vale `None`. Es el mismo criterio que ya se aplicaba a una salida con
    carga desconocida, extendido al único caso que faltaba, y devuelve a
    `min_days_required` su papel de mínimo de DATOS en vez de mínimo de
    casillas rellenas.

    El horizonte se deduce de las propias salidas porque es lo único que hay:
    el volcado solo contiene actividades, así que de antes de la primera no se
    puede afirmar nada. Se queda corto cuando el backfill miró más atrás y no
    encontró nada -esos ceros sí eran reales y aquí se descartan-, y ese error
    es el que se prefiere: retrasa el primer percentil unos días y nunca
    fabrica uno.
    """
    if not rides:
        return None
    start = day - timedelta(days=window_days - 1)
    if start < min(r.date for r in rides):
        return None
    ventana = [r for r in rides if start <= r.date <= day]
    if any(not r.load_known for r in ventana):
        return None
    return sum(r.load for r in ventana)


def load_series(
    rides: Sequence[ClassifiedRide],
    end_day: date,
    days: int,
    window_days: int,
) -> dict[date, float | None]:
    """`load_Nd` de cada uno de los últimos `days` días que terminan en `end_day`.

    `load_Nd` del día d es la carga de los N días que TERMINAN LA VÍSPERA:
    [d-N, d-1]. El propio día NO entra, y eso no es un detalle de
    implementación: es lo único que hace la serie comparable consigo misma.

    POR QUÉ NO ENTRA EL PROPIO DÍA
    ------------------------------
    Aquí se llamaba a `rolling_load(rides, d, N)`, cuya ventana es [d-N+1, d] e
    incluye el día. Para los días pasados eso sumaba N días con su salida
    dentro. Para HOY, a las 07:00, la salida de hoy todavía no está en la base,
    así que sumaba N-1. La misma serie tenía dos magnitudes distintas según se
    mirara el último elemento o cualquier otro, y el último es justamente el que
    `build_signals` publica como valor del día.

    `resolve_adaptive_threshold` saca el percentil de esta serie sobre la
    ventana que termina ayer. Juntando las dos cosas, la regla comparaba una
    suma de N-1 días contra una distribución de sumas de N días. Medido sobre
    los 184 días reales del histórico: media 66,9 del lado del valor contra 99,5
    del lado de la distribución. No es ruido ni es un caso raro -es todos los
    días- y el efecto siempre va en la misma dirección: el valor de hoy juega
    por debajo y la regla se inclina a NO disparar.

    Ninguna de las dos funciones estaba mal. `rolling_load` hacía lo que dice su
    nombre y `resolve_adaptive_threshold` también. El sesgo vivía en la junta, y
    por eso no lo encontraba nadie leyendo cualquiera de las dos: hay que tener
    las dos delante A LA VEZ y acordarse de que a las 07:00 falta un dato.

    LO QUE NO SE HIZO
    -----------------
    No se tocó el percentil. Mover el umbral para compensar habría tapado el
    número sin arreglar la medida, y además habría dejado el error dependiendo
    de la hora: recalculado a las 22:30 -con la salida de hoy ya dentro- el
    valor cambiaba de magnitud y el umbral compensado pasaba a estar mal en el
    otro sentido. Ahora la cifra no depende de la hora.
    """
    return {
        end_day - timedelta(days=i): rolling_load(
            rides, end_day - timedelta(days=i + 1), window_days
        )
        for i in range(days)
    }


def resolve_adaptive_threshold(
    spec: dict[str, Any],
    series: dict[date, float],
    day: date,
) -> tuple[float | None, str | None]:
    """Resuelve un umbral de `adaptive_thresholds` sobre la serie dada.

    Devuelve (valor, motivo_si_None). La ventana termina AYER: comparar el
    valor de hoy contra una distribución que ya lo incluye lo hace más difícil
    de superar justo el día que interesa.
    """
    window_days = int(spec.get("window_days", 60))
    min_days = int(spec.get("min_days_required", 0))
    include_zero = bool(spec.get("include_zero_days", True))
    pct = float(spec.get("percentile", 90))

    end = day - timedelta(days=1)
    start = end - timedelta(days=window_days - 1)
    values = [v for d, v in series.items() if start <= d <= end and v is not None]
    if not include_zero:
        values = [v for v in values if v > 0]

    if len(values) < min_days:
        return None, (
            f"histórico insuficiente: {len(values)} días con dato de {min_days} necesarios"
        )

    value = percentile(values, pct)

    # Guarda contra el umbral degenerado. `min_days_required` cuenta días CON
    # DATO, y con `include_zero_days: true` un día de descanso es un día con
    # dato: tras un parón largo se pueden tener 60 días "válidos" cuyo percentil
    # 90 es 0. Un umbral de 0 no es un umbral — significa "dispara con
    # cualquier cosa por encima de nada" — y convertiría `carga_acumulada` en
    # un ámbar permanente justo al volver de una lesión o unas vacaciones, que
    # es cuando menos falta hace. Sin umbral utilizable, la regla se salta.
    if value is None or value <= 0:
        return None, (
            "el percentil sale 0: no hay carga suficiente en la ventana para "
            "calcular un umbral con sentido"
        )
    return value, None


# ---------------------------------------------------------------------------
# Recuento rodante de sesiones intensas
# ---------------------------------------------------------------------------
#
# AQUÍ ESTABAN `WeekendSummary` Y `weekend_summary`, Y SE HAN BORRADO
# -------------------------------------------------------------------
# Resumían el fin de semana inmediatamente anterior: horas totales, salidas
# intensas y salidas sin clasificar del sábado y el domingo. De ahí salían dos
# señales, `weekend_total_hours` y `weekend_intense_rides`, y un objeto entero
# colgado de `Signals.weekend`.
#
# No lo leía nadie. Ni una regla del semáforo -`resaca_finde`, la única que lo
# miraba, se borró hace tiempo con sus dos umbrales-, ni el mensaje, ni el
# panel, ni las métricas. Lo que quedaba era un cálculo que se ejecutaba todos
# los días para escribir tres cosas que nadie leía jamás, y que además metía
# una nota en `notes` -«N salida(s) sin clasificar el fin de semana; la regla
# del lunes se salta»- que hablaba de una regla del lunes que ya no existe. El
# sistema se estaba avisando a sí mismo de las consecuencias de algo borrado.
#
# Y no es solo código sobrante: es el calendario. Agrupar por «sábado y
# domingo» presupone que el esfuerzo grande cae en fin de semana, que es
# exactamente lo que se echó de la bici por no ser verdad.
#
# Lo que hacía falta de ahí -cuánta intensidad se lleva encima hace poco- lo da
# `IntensityCount` sobre una ventana rodante, sin preguntar qué día es hoy. Lo
# que se pierde -las horas del fin de semana- no se pierde: las salidas enteras
# siguen en `data/cache/activities.json` y en la tabla `activities`, y de ahí
# se recalcula cuando haga falta. Un agregado derivado que nadie consulta no es
# un registro, es una copia sin dueño.


@dataclass(frozen=True)
class IntensityCount:
    """Sesiones intensas de los últimos N días. Cuenta; no limita.

    ESTO ERA UN PRESUPUESTO Y AHORA ES UN CONTADOR
    -----------------------------------------------
    Tenía un `limit`, un `remaining`, un `exhausted` y un `indeterminate`, y
    con ellos `bike_advisor` bajaba la salida del sábado. Ya no: el sistema no
    decide lo que se puede hacer, registra lo que se hizo. Los cuatro campos
    se han borrado enteros en vez de dejarlos sin usar, porque un `limit` que
    no limita es exactamente la clase de nombre que en este proyecto acaba
    costando una tarde: alguien lo lee dentro de seis meses, deduce que hay un
    tope, y se pone a buscar por qué no se aplica.

    Lo que sí se conserva es `unknown`, y es lo único de todo esto que sigue
    siendo crítico. `used` solo suma salidas con `level == "intensa"`; una que
    Garmin no pudo clasificar -sin zonas de FC y sin Training Effect- sale
    'desconocida'. O sea que `used` es un MÍNIMO y no el número, y ahora que
    se enseña en el mensaje todos los días eso hay que decirlo cada vez que
    pase, no solo cuando además frenaba.

    Y AHORA LA VENTANA RUEDA, QUE ES EL SEGUNDO CALENDARIO QUE SE VA
    -----------------------------------------------------------------
    Esto contaba desde el lunes: `week_start(day, "monday")`. Un contador que
    se pone a cero a las doce de la noche del domingo, sin que el cuerpo se
    entere de nada.

    Medido sobre las 58 salidas reales de la caché (184 días, 12 intensas), la
    cuenta natural y la rodante de 7 días NO COINCIDEN en 39 de esos 184 días,
    o sea uno de cada cinco. Y el desacuerdo tiene signo: el mínimo es 0 y el
    máximo +2, siempre a favor de la rodante. La semana natural nunca cuenta de
    más; cuenta de MENOS, que es el lado peligroso con una hernia detrás.

    El caso claro es el lunes. En 8 de 26 lunes -el 31%- el mensaje decía
    «ninguna sesión intensa esta semana todavía» habiendo 1 o 2 intensas en los
    siete días anteriores; tres de esos lunes venían de sábado Y domingo
    intensos, doce horas antes. La frase era literalmente cierta y
    prácticamente mentira, que es la peor combinación posible: no se puede
    discutir con ella y engaña igual.

    Con la ventana rodante el desacuerdo desaparece por construcción, porque no
    hay corte: todos los días se miran los mismos siete días hacia atrás y el
    número solo cambia cuando cambia lo que se hizo.

    POR QUÉ 7 Y NO UN PERCENTIL DE MI DISTRIBUCIÓN
    ------------------------------------------------
    La norma de la casa es calcular contra la propia distribución en vez de
    contra constantes. Aquí no aplica, y el motivo es que esto NO ES UN UMBRAL:
    no se compara contra nada, no hay denominador, no frena. Es la unidad en la
    que se cuenta, y una unidad tiene que ser estable para poder comparar el
    número de hoy con el de la semana pasada. Un periodo que se recalculara
    cada día haría que el contador subiera o bajara sin que se hubiera hecho ni
    dejado de hacer nada, que es justo el defecto que se está quitando.
    """

    used: int
    detail: list[str]
    # Los dos extremos de la ventana, incluidos los dos, y guardados en vez de
    # deducidos. `hasta` es el día de la decisión. Se almacenan los dos porque
    # es lo que va al snapshot: dentro de un año, «7» no dice sobre qué siete
    # días se contó, y la fecha de la decisión podría no estar a mano.
    desde: date
    hasta: date
    # Salidas de la ventana que no se pudieron clasificar. Podrían haber sido
    # intensas, así que `used` es un MÍNIMO, no el número.
    unknown: int = 0

    @property
    def dias(self) -> int:
        """Ancho de la ventana en días, extremos incluidos."""
        return (self.hasta - self.desde).days + 1

    def linea(self) -> str:
        """La frase informativa del mensaje. Sin referencia y sin juicio.

        No lleva un "de 4" detrás a propósito. Un denominador convierte un
        recuento en una nota, y una nota con denominador se lee como aprobado
        o suspenso aunque no frene nada. Lo que hace falta saber es cuánto se
        lleva hecho; lo que sobra es que el sistema opine sobre si son muchas.

        Y se concuerda el plural en vez de escribir "sesion(es)". La frase la
        lee una persona a las siete de la mañana, no un log: los paréntesis de
        plural son la marca de un texto generado, y un texto que parece
        generado se lee como relleno. Cuesta dos líneas.

        SE FUE LA PALABRA «TODAVÍA», Y NO ES UN DETALLE DE ESTILO
        ----------------------------------------------------------
        Decía «ninguna sesión intensa esta semana todavía». El «todavía»
        pertenecía a la semana natural: presuponía un periodo abierto que
        quedaba por llenar, o sea una cuota implícita justo en la frase que se
        escribió para no tener cuota. En una ventana rodante no hay nada
        pendiente de llenarse -los siete días de atrás ya pasaron enteros-, así
        que la palabra sobra y además empujaba.

        El periodo se dice con todas las letras en la frase en vez de darlo por
        supuesto. «Esta semana» obligaba a saber qué día es hoy para entender
        el número; «en los últimos 7 días» se entiende un martes y un domingo
        igual, que es exactamente lo que se busca.
        """
        periodo = f"en los últimos {self.dias} días" if self.dias != 1 else "hoy"
        if self.used == 0 and not self.unknown:
            return f"ninguna sesión intensa {periodo}"
        cuantas = (
            "1 sesión intensa" if self.used == 1 else f"{self.used} sesiones intensas"
        )
        base = f"llevas {cuantas} {periodo}"
        if self.unknown:
            sueltas = (
                "1 salida sin clasificar que pudo serlo"
                if self.unknown == 1
                else f"{self.unknown} salidas sin clasificar que pudieron serlo"
            )
            base += f", y {sueltas}"
        return base

    def to_dict(self) -> dict[str, Any]:
        """Para `inputs_snapshot_json`, o sea para las métricas de dentro de un año.

        Antes esto no se guardaba en ninguna parte: del recuento solo
        sobrevivía la frase del recorte de bici, dentro de `bike.downgrades`,
        y solo los días en que hubiera recorte. Al dejar de recortar, el dato
        habría desaparecido del histórico por completo justo cuando pasa a ser
        su única razón de existir.

        `week_start` era la clave de cuando la cuenta empezaba el lunes, y no
        se ha renombrado a secas: se ha sustituido por los dos extremos. Un
        `desde` suelto obligaría a saber de qué día era la decisión para
        reconstruir la ventana, y el que lea esto dentro de un año estará
        leyendo un JSON, no una fila con su fecha al lado.
        """
        return {
            "used": self.used,
            "unknown": self.unknown,
            "desde": self.desde.isoformat(),
            "hasta": self.hasta.isoformat(),
            "dias": self.dias,
            "detail": list(self.detail),
        }


# Las claves de cuando esto era un presupuesto. Si reaparecen en el YAML hay
# que parar: quien las escriba estará creyendo que pone un tope.
CLAVES_DE_CUANDO_LIMITABA = (
    "weekly_limit",
    "on_budget_exhausted",
    "max_intense_rides_per_weekend",
    "require_green_for_intense",
)

# La clave de cuando la ventana era una semana natural. Va aparte de la lista
# de arriba porque el error que describe es otro: quien escriba `weekly_limit`
# cree que está frenando algo; quien escriba `week_starts_on` cree que está
# eligiendo por dónde corta un contador que ya no corta por ningún sitio. El
# mensaje tiene que decir cada cosa, y un mensaje que las junta no dice
# ninguna.
CLAVES_DE_CUANDO_ERA_SEMANA_NATURAL = ("week_starts_on",)


def intensity_count(
    rides: Sequence[ClassifiedRide],
    sessions: Sequence[StrengthSession],
    day: date,
    cycling_cfg: dict[str, Any],
) -> IntensityCount | None:
    """Sesiones intensas HECHAS en los `window_days` que terminan en `day`.

    Devuelve `None` cuando NO SE CUENTA, que no es lo mismo que contar cero.
    Un cero dice «no has hecho ninguna intensa» y eso es una afirmación sobre el
    entrenamiento de quien lo lee; `None` no dice nada, y el mensaje se limita a
    no sacar la línea. Ver abajo por qué la distinción tuvo que hacerse.

    Cuenta lo EJECUTADO: un HIIT programado que no se hizo no cuenta. Y no
    limita nada: el número sale en el mensaje y se guarda, y eso es todo lo
    que hace.

    La ventana incluye los dos extremos y termina HOY, no ayer. Es coherente
    con `rolling_load` y por el mismo motivo: por la mañana todavía no hay nada
    de hoy, así que en la práctica son los días anteriores; pero si algo
    recalcula por la tarde, lo que se hizo hoy ya cuenta y el número no se
    queda corto. Un contador que ignora el propio día se desmiente solo en
    cuanto alguien mira el panel después de entrenar.
    """
    cfg = ((cycling_cfg.get("recommendation", {}) or {}).get("intensity_count", {})) or {}

    # NO CONTAR, CONTAR CERO Y CONTAR A MEDIAS SON TRES COSAS DISTINTAS
    # -----------------------------------------------------------------
    # Aquí había un `return IntensityCount(used=0, detail=[], desde=day,
    # hasta=day)` y era un cero fabricado. Volvía SIN MIRAR `rides`, así que el
    # mensaje de la mañana salía afirmando «ninguna sesión intensa hoy» un día
    # en que podía haber una: no es que el recuento fuera aproximado, es que la
    # frase era falsa y no había forma de sospecharlo leyéndola. Y el test que
    # cubría este camino pasaba `rides=[]`, con lo cual el cero le salía bien
    # por los dos motivos a la vez y no distinguía uno del otro.
    #
    # Ahora son tres respuestas para tres situaciones:
    #
    #   - Sin bloque no hay nada configurado: `None`. No se cuenta y no se dice
    #     nada. `message.py` no saca la línea.
    #   - Con `enabled: false` la decisión de no contar está ESCRITA: `None`
    #     también, por el mismo motivo y con más razón.
    #   - Con bloque encendido pero sin `window_days` sí hay un número que va a
    #     salir en el mensaje, y suponerle una ventana sería ponerle unidades
    #     falsas. Eso revienta, unas líneas más abajo.
    #
    # Que el bloque ESTÉ en el YAML de verdad lo exige `config_loader`, que es
    # la aduana del fichero. Esto de aquí es una función de cálculo y la llaman
    # también scripts y tests con diccionarios escritos a mano.
    if not cfg:
        return None

    # `enabled` NO SE LEÍA, Y EL VALIDADOR PROMETÍA QUE SÍ
    # ----------------------------------------------------
    # El bloque exigía la clave -presente y booleana- y el motor no la miraba en
    # ningún sitio. O sea que `enabled: false` validaba perfectamente y seguía
    # contando: el interruptor estaba puesto, se podía apagar, y no apagaba
    # nada. Peor todavía, el error de `config_loader` que exige el bloque dice
    # literalmente «para no contar hay que escribir `enabled: false`», con lo
    # cual el fichero documentaba un comportamiento que no existía.
    #
    # Es el defecto que da nombre a media docena de comentarios de este
    # repositorio -el validador y el motor mirando a lados distintos- y estaba
    # dentro del bloque que este cambio venía a reescribir.
    #
    # El defecto de la clave ausente es `True` porque en el YAML real
    # `config_loader` garantiza que está escrita, y en los diccionarios a mano
    # de scripts y tests contar es lo que se espera. Donde importa, los dos
    # lados miran ahora lo mismo.
    if not cfg.get("enabled", True):
        return None

    # Las guardas viven aquí y no solo en `config_loader` porque `config_loader`
    # protege el YAML del repositorio, y esta función la llaman además scripts
    # y tests con diccionarios de configuración escritos a mano que no pasan
    # por el validador. Una clave muerta tiene que doler en los dos caminos.
    muertas = [k for k in CLAVES_DE_CUANDO_LIMITABA if k in cfg]
    if muertas:
        raise IntensityCountConfigError(
            f"cycling.recommendation.intensity_count trae {muertas}, que son "
            f"claves de cuando esto recortaba la salida del fin de semana. Ya no "
            f"lo hace: el recuento informa y quien decide es el usuario. Si lo "
            f"que se busca es frenar por carga acumulada, el camino es el "
            f"semáforo (HRV, sueño, pulso de reposo, carga de Garmin), no un cupo."
        )
    de_calendario = [k for k in CLAVES_DE_CUANDO_ERA_SEMANA_NATURAL if k in cfg]
    if de_calendario:
        raise IntensityCountConfigError(
            f"cycling.recommendation.intensity_count trae {de_calendario}, que "
            f"son claves de cuando el recuento iba por semana natural. Ahora es "
            f"una ventana rodante de `window_days` que termina hoy, así que no "
            f"empieza ningún día: elegir el lunes no cambiaría nada y parecería "
            f"que sí. Si lo que se quiere es otro periodo, es `window_days`."
        )

    # `window_days` NO tiene valor por defecto, y es deliberado. El resto del
    # bloque se lee con `.get(..., True)` porque un `counts_as_intense` incompleto
    # da un recuento distinto pero comprensible; aquí lo que se supondría es la
    # UNIDAD del número, y esa unidad sale escrita en el mensaje de la mañana.
    # Un «llevas 2 sesiones intensas en los últimos 7 días» calculado sobre una
    # ventana que el código se inventó porque el YAML no la decía es un número
    # con unidades falsas, y el lector no tiene forma de sospecharlo.
    ventana = cfg.get("window_days")
    if not isinstance(ventana, int) or isinstance(ventana, bool) or ventana < 1:
        raise IntensityCountConfigError(
            f"cycling.recommendation.intensity_count.window_days tiene que ser un "
            f"entero >= 1 y vale {ventana!r}. No se supone un 7: el periodo sale "
            f"escrito en el mensaje («en los últimos N días»), así que inventarlo "
            f"aquí sería ponerle unidades falsas a un número que se lee todos los "
            f"días."
        )

    counts = cfg.get("counts_as_intense", {}) or {}
    start = day - timedelta(days=ventana - 1)

    detail: list[str] = []
    used = 0
    unknown = 0

    if counts.get("ride_intensa", True):
        for r in rides:
            if not (start <= r.date <= day):
                continue
            if r.level == "intensa":
                used += 1
                detail.append(f"{r.date.isoformat()}: salida INTENSA")
            elif r.level == UNKNOWN:
                # Ni se suma ni se ignora. Sumarla sería inventarse una intensa
                # que a lo mejor fue un paseo; ignorarla -lo que se hacía- es
                # afirmar que fue un paseo, que es igual de inventado y además
                # cae del lado que quita el freno.
                unknown += 1
                detail.append(f"{r.date.isoformat()}: salida SIN CLASIFICAR")

    if counts.get("hiit_executed", True):
        for s in sessions:
            if start <= s.date <= day and s.is_hiit:
                used += 1
                detail.append(f"{s.date.isoformat()}: HIIT ejecutado ({s.routine_key})")

    if counts.get("strength_session", False):
        for s in sessions:
            if start <= s.date <= day and not s.is_hiit:
                used += 1
                detail.append(f"{s.date.isoformat()}: fuerza ({s.routine_key})")

    return IntensityCount(
        used=used,
        detail=sorted(detail),
        desde=start,
        hasta=day,
        unknown=unknown,
    )


def last_ride_level(
    rides: Sequence[ClassifiedRide],
    day: date,
    lookback_days: int = 1,
) -> str | None:
    """Nivel de la salida más reciente dentro de los `lookback_days` previos.

    Mira lo que REALMENTE se hizo, no lo que se recomendó. Si hubo varias
    salidas ese día, manda la más intensa.
    """
    start = day - timedelta(days=lookback_days)
    picked = [r for r in rides if start <= r.date <= day - timedelta(days=1)]
    if not picked:
        return None
    order = ["suave", "media", "intensa"]
    known = [r.level for r in picked if r.level in order]
    if not known:
        return UNKNOWN
    return max(known, key=order.index)


# ---------------------------------------------------------------------------
# Construcción del conjunto de señales
# ---------------------------------------------------------------------------


def _baseline_for(
    metrics: dict[date, DayMetrics],
    attr: str,
    day: date,
    window_days: int,
    min_days: int,
    exclude_outliers: bool,
) -> float | None:
    """Media móvil de los `window_days` ANTERIORES a `day`."""
    values: list[float] = []
    for i in range(1, window_days + 1):
        m = metrics.get(day - timedelta(days=i))
        if m is None:
            continue
        v = getattr(m, attr)
        if v is not None:
            values.append(float(v))
    if len(values) < min_days:
        return None
    return mean_excluding_outliers(values, exclude_outliers)


def build_signals(
    config: Any,
    day: date,
    metrics: Sequence[DayMetrics],
    rides: Sequence[Ride],
    sessions: Sequence[StrengthSession],
    checkin_history: Sequence[Checkin],
    checkin: Checkin | None = None,
    history_days: int = 14,
) -> Signals:
    """Construye el `Signals` de un día a partir de los datos crudos.

    `config` puede ser un `Config` o un dict; solo se le piden secciones.

    POR QUÉ NI `sessions` NI `checkin_history` TIENEN VALOR POR DEFECTO
    -------------------------------------------------------------------
    Son el mismo fallo dos veces, y el segundo todavía no ha explotado.

    `sessions` lo tuvo -`= ()`- y por eso estuvo MUERTO desde el primer día.
    Ningún caller de producción se lo pasaba: ni `runner.run_daily` ni `cli.py`.
    El único sitio del proyecto donde se construía un `StrengthSession` era su
    propio test. `intensity_budget` recibía siempre una lista vacía, así que el
    presupuesto semanal de sesiones intensas contó durante toda la vida del
    sistema únicamente las salidas de bici: un HIIT hecho el martes no gastaba
    nada y el sábado quedaba margen para una salida intensa que en realidad ya
    no cabía.

    `checkin_history` estaba exactamente igual, y sigue sin haber explotado solo
    por casualidad. Con `= ()`, `sig.history[clave]` termina con como mucho un
    punto -el de hoy- y eso es justo lo que leen los umbrales adaptativos unas
    líneas más abajo: `series = sig.history.get(metric, {})`. Hoy los dos únicos
    umbrales adaptativos del config miran carga derivada de las salidas, que se
    construye por otro camino, así que el agujero está tapado por que nadie ha
    pasado por encima todavía. En cuanto se defina un umbral adaptativo sobre la
    lumbar o el cansancio -la recalibración de octubre-, sería un percentil
    calculado sobre un solo punto: un umbral que siempre se cumple o nunca, con
    aspecto de estadística sobre el histórico propio.

    Un defecto vacío convierte "se me olvidó pasarlo" en "no hay histórico", que
    son cosas opuestas y se leen igual. Sin defecto, olvidarlo es un `TypeError`
    en el arranque -ruidoso, inmediato, delante de quien acaba de tocar el
    código- en vez de un número plausible que no se nota hasta que la espalda lo
    nota. Se pasa una lista vacía cuando DE VERDAD no hay histórico, y entonces
    es una afirmación de quien llama, no un olvido.
    """
    raw = config.raw if hasattr(config, "raw") else config
    baseline_cfg = raw.get("baseline", {}) or {}
    cycling_cfg = raw.get("cycling", {}) or {}
    adaptive_cfg = raw.get("adaptive_thresholds", {}) or {}

    window = int(baseline_cfg.get("window_days", 7))
    min_days = int(baseline_cfg.get("min_days_required", 4))
    excl = bool(baseline_cfg.get("exclude_outliers", True))

    by_date = {m.date: m for m in metrics}
    classified = classify_all(rides, cycling_cfg)

    sig = Signals(day=day)
    notes = sig.notes

    # --- wellness de hoy ---------------------------------------------------
    today = by_date.get(day)
    for attr in ("hrv", "rhr", "sleep_min", "sleep_score", "body_battery"):
        sig.values[attr] = getattr(today, attr) if today else None

    # --- líneas base y señales derivadas, con histórico --------------------
    for attr, base_name, derived_name in (
        ("hrv", "hrv_baseline", "hrv_ratio"),
        ("rhr", "rhr_baseline", "rhr_delta"),
    ):
        base_hist: dict[date, float] = {}
        derived_hist: dict[date, float] = {}
        for i in range(history_days):
            d = day - timedelta(days=i)
            base = _baseline_for(by_date, attr, d, window, min_days, excl)
            if base is None:
                continue
            base_hist[d] = base
            m = by_date.get(d)
            value = getattr(m, attr) if m else None
            if value is None:
                continue
            # hrv_ratio: cociente contra la base (0,85 = 15% por debajo).
            # rhr_delta: diferencia en lpm (+7 = 7 pulsaciones por encima).
            derived_hist[d] = (float(value) / base) if derived_name.endswith("ratio") else (
                float(value) - base
            )

        sig.values[base_name] = base_hist.get(day)
        sig.values[derived_name] = derived_hist.get(day)
        sig.history[base_name] = base_hist
        sig.history[derived_name] = derived_hist
        if base_hist.get(day) is None:
            notes.append(
                f"{base_name}: sin línea base (hacen falta {min_days} días con dato "
                f"en los {window} anteriores)"
            )

    # --- carga acumulada ---------------------------------------------------
    #
    # CUÁNTA SERIE HAY QUE CONSTRUIR LO DICE EL CONFIG, NO UN 90 ESCRITO AQUÍ
    # -----------------------------------------------------------------------
    # Aquí ponía `days=max(history_days, 90)`. El 90 era de cuando el único
    # consumidor de esta serie pedía 60 días de ventana, y sobraba. Pero quien
    # decide cuántos días hacen falta es `adaptive_thresholds.*.window_days`, y
    # esos viven en el YAML y se pueden subir: el día que alguien pusiera 120 o
    # 180 -que es justo lo que pide el criterio de calcular los umbrales contra
    # la propia distribución- `resolve_adaptive_threshold` habría recortado la
    # ventana a los 90 días que hubiera, sin error y sin nota. Otra vez el valor
    # que se lee no siendo el valor que se usa, y van unas cuantas.
    #
    # Se calcula en vez de fijarse, y el `+ 1` es porque la ventana del percentil
    # termina AYER: para 60 días de ventana hacen falta 61 días de serie.
    ventanas_pedidas = [
        int(spec.get("window_days", 60))
        for spec in adaptive_cfg.values()
        if isinstance(spec, dict)
    ]
    dias_de_serie = max([history_days, 90, *(v + 1 for v in ventanas_pedidas)])
    for name, win in (("load_2d", 2), ("load_7d", 7)):
        series = load_series(classified, day, days=dias_de_serie, window_days=win)
        sig.values[name] = series.get(day)
        sig.history[name] = series
        if series.get(day) is None:
            # La ventana termina AYER, así que los culpables están en
            # [d-win, d-1]. Buscarlos en [d-win+1, d] -como se hacía- señalaba al
            # día equivocado por los dos extremos: acusaba a una salida de hoy
            # que no había entrado en la cuenta y absolvía a la de hace `win`
            # días, que sí. La nota nombra fechas concretas y se lee; nombrar la
            # fecha que no es cuesta más que no nombrar ninguna.
            desde = day - timedelta(days=win)
            hasta = day - timedelta(days=1)
            culpables = sorted(
                {r.date for r in classified if desde <= r.date <= hasta and not r.load_known}
            )
            notes.append(
                f"{name}: sin dato — {len(culpables)} salida(s) sin carga ni forma de "
                f"estimarla ({', '.join(d.isoformat() for d in culpables)}). "
                f"Se prefiere no dar el número a darlo por lo bajo."
            )

    # --- ciclismo ----------------------------------------------------------
    #
    # Aquí se llamaba a `weekend_summary` y se escribían `weekend_total_hours` y
    # `weekend_intense_rides`, más una nota sobre «la regla del lunes». Las tres
    # cosas se han borrado con la función: nadie las leía y la regla del lunes
    # llevaba meses sin existir. El motivo largo está donde estaba la función.
    conteo = intensity_count(classified, sessions, day, cycling_cfg)
    # `conteo` es `None` cuando no se cuenta: sin bloque o con `enabled: false`.
    # Entonces NO se escribe la señal, en vez de escribirla a cero.
    #
    # Escribir un cero sería exactamente el fallo que se acaba de quitar de
    # `intensity_count`, pero un piso más abajo y más caro: `sig.values` es lo
    # que leen las reglas del YAML y lo que se guarda en la base para el panel y
    # para los replays. Un `intense_count_7d: 0` puesto por un bloque apagado es
    # indistinguible, mirándolo, de un 0 que significa «esta semana no has
    # apretado», y se queda escrito en el histórico para siempre. Que la clave
    # falte es incómodo de leer y por eso es honesto: se nota.
    if conteo is not None:
        # El nombre de la señal lleva la ventana dentro. Se llamaba
        # `week_intense_count` y eso dejó de ser verdad en cuanto la cuenta dejó
        # de ir por semanas: una regla que lo leyera creería estar mirando la
        # semana en curso. Un nombre desfasado es la forma más barata que hay de
        # mentirle al que llegue después, porque no hace falta ni leerlo entero
        # para creérselo.
        sig.values[f"intense_count_{conteo.dias}d"] = conteo.used
        # `week_intense_remaining` se ha quitado de aquí junto con el límite. Era
        # `limit - used`, y sin límite no queda nada de lo que quedar. Se borra en
        # vez de dejarse a cero: una señal que vale siempre cero es una señal que
        # una regla puede leer y comparar, y entonces el cupo vuelve por la puerta
        # de atrás sin que nadie lo haya decidido.
        if conteo.unknown:
            # Va a `notes` y no solo a la nota de la bici porque el recuento se
            # enseña como un número redondo -"llevas 2 sesiones intensas en los
            # últimos 7 días"- y ese número es un MÍNIMO, no el dato. Enseñar un
            # mínimo con cara de dato es la forma más limpia que hay de que alguien
            # se fíe de él, y se enseña todos los días.
            cuantas = (
                "1 salida de la ventana sin clasificar que pudo ser intensa"
                if conteo.unknown == 1
                else f"{conteo.unknown} salidas de la ventana sin clasificar y "
                f"cualquiera pudo ser intensa"
            )
            notes.append(
                f"intense_count_{conteo.dias}d: {conteo.used} es un MÍNIMO, no el "
                f"número: hay {cuantas}"
            )

    lookback = int(
        ((cycling_cfg.get("recommendation", {}) or {}).get("lookback_days", 1))
    )
    sig.values["yesterday_ride_level"] = last_ride_level(classified, day, lookback)

    # --- formulario --------------------------------------------------------
    #
    # El mismo bucle para los deslizadores y para las dos preguntas de Sí/No. Que
    # compartan bucle es lo que hace que las preguntas lleguen GRATIS al
    # histórico, a las series y al `snapshot`, que es justo lo que se pidió:
    # guardadas y contadas en todas las métricas como cualquier otra respuesta.
    #
    # Compartir bucle NO les da poder sobre el semáforo. Eso se decide en otro
    # sitio: una regla solo puede nombrar lo que esté en `checkin_sliders`, y las
    # preguntas no están ahí -`config_loader` rechaza el arranque si una regla lo
    # intenta-. Aquí se rellenan señales; quién puede leerlas es una frontera de
    # configuración, no de bucle.
    slider_keys = (
        config.slider_keys()
        if hasattr(config, "slider_keys")
        else [s["key"] for s in raw.get("checkin_sliders", [])]
    )
    pregunta_keys = (
        config.pregunta_keys()
        if hasattr(config, "pregunta_keys")
        else [p["key"] for p in raw.get("checkin_preguntas", [])]
    )
    for key in [*slider_keys, *pregunta_keys]:
        sig.values[key] = checkin.values.get(key) if checkin else None
        hist: dict[date, Any] = {}
        for c in checkin_history:
            v = c.values.get(key)
            if v is not None:
                hist[c.date] = v
        if checkin and checkin.values.get(key) is not None:
            hist[day] = checkin.values[key]
        sig.history[key] = hist

    # El selector, a su campo y no a `values`. Ver `CLAVE_SESION_ELEGIDA`.
    #
    # Se lee de `checkin.values` porque ahí es donde lo deja `checkin_values`,
    # que vuelca la fila entera; lo que no hace es seguir el camino de las otras
    # respuestas hacia `sig.values`.
    sig.sesion_elegida = checkin.values.get(CLAVE_SESION_ELEGIDA) if checkin else None

    # La discordancia: te apetecía y no vas, o no te apetecía y vas.
    #
    # Se deriva AQUÍ y no en el análisis porque tiene que quedar en el histórico
    # día a día, igual que las dos respuestas de las que sale. Calculada después
    # sobre la tabla daría el mismo número hoy y ninguno el día que se reescriba
    # la consulta.
    #
    # `None` si falta cualquiera de las dos, y este es el punto entero: dos de
    # los cuatro estados posibles son «no contestó», y un `False` ahí diría
    # «contestó lo mismo a las dos» sobre un día en el que no contestó nada. El
    # nombre es castellano porque es una métrica DERIVADA; las respuestas crudas
    # van en inglés como el resto de la tabla.
    apetece = sig.values.get(CLAVE_APETECE)
    voy = sig.values.get(CLAVE_VOY_A_ENTRENAR)
    sig.values["discordancia"] = (
        None if apetece is None or voy is None else bool(apetece) != bool(voy)
    )

    # Y el histórico, que es la mitad que faltaba.
    #
    # Ese párrafo de ahí arriba -"tiene que quedar en el histórico día a día"-
    # estuvo escrito sin cumplirse: solo se rellenaba `values`, o sea el día de
    # hoy, y cualquier umbral adaptativo o cualquier vista que pidiera la serie
    # de `discordancia` habría encontrado un hueco donde el comentario prometía
    # una serie. No daba error: daba "no hay serie para la métrica
    # discordancia", que se lee como "todavía no hay datos".
    #
    # La INTERSECCIÓN y no la unión, al revés que en `series.py`, y la razón es
    # que aquí el diccionario significa otra cosa: el bucle de arriba solo mete
    # en `history` los días con valor -`if v is not None`-, así que un hueco ya
    # es "no se sabe" por convenio del módulo. Meter un día con `None` rompería
    # esa regla para todo el que recorra el histórico contando días.
    hist_apetece = sig.history.get(CLAVE_APETECE, {})
    hist_voy = sig.history.get(CLAVE_VOY_A_ENTRENAR, {})
    sig.history["discordancia"] = {
        d: bool(hist_apetece[d]) != bool(hist_voy[d])
        for d in sorted(set(hist_apetece) & set(hist_voy))
    }

    if checkin is None:
        notes.append("sin check-in: solo se evalúan las reglas objetivas")

    # --- umbrales adaptativos ---------------------------------------------
    #
    # VA AL FINAL, Y ESO ES EL ARREGLO, NO EL ORDEN DE SIEMPRE.
    #
    # Estaba arriba, antes del bloque del formulario, y ahí leía
    # `sig.history[metrica]` de deslizadores que este mismo cuerpo todavía no
    # había rellenado. Efecto: cualquier umbral adaptativo definido sobre un
    # campo del check-in salía None SIEMPRE, con la nota "no hay serie para la
    # métrica X" -que es verdad en ese instante y mentira tres líneas después- y
    # la regla que dependiera de él se saltaba entera para siempre.
    #
    # No se había notado porque los dos únicos umbrales del config miran
    # `load_2d` y `load_7d`, que sí están construidos a estas alturas: el
    # segundo fusible de la misma mina que `checkin_history`. Arreglar solo el
    # parámetro habría dejado la serie llegando bien a un sitio que se leía
    # antes de que existiera, o sea el mismo None con una causa distinta.
    #
    # Las series de carga no dependen de nada de aquí abajo, así que bajarlo no
    # le quita nada a lo que ya funcionaba.
    for name, spec in adaptive_cfg.items():
        metric = spec.get("metric")
        series = sig.history.get(metric, {})
        if not series:
            sig.adaptive[name] = None
            notes.append(f"{name}: no hay serie para la métrica '{metric}'")
            continue
        value, why = resolve_adaptive_threshold(spec, series, day)
        sig.adaptive[name] = value
        if why:
            notes.append(f"{name}: {why}")

    sig.rides = classified
    sig.intense_count = conteo

    return sig
