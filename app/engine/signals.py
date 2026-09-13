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

1. `load_3d` / `load_7d` se calculan SUMANDO las actividades, no leyendo un
   endpoint de Garmin. Así la carga es siempre la que de verdad se hizo.
2. Las ventanas de carga incluyen el propio día. Por la mañana todavía no hay
   actividad de hoy, así que en la práctica son los 3 días anteriores; pero si
   se recalcula por la tarde la cifra sigue siendo correcta.
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


class IntensityCountConfigError(ValueError):
    """El bloque de recuento de intensas trae claves de cuando limitaba.

    `weekly_limit`, `on_budget_exhausted`, `max_intense_rides_per_weekend` y
    `require_green_for_intense` existieron y recortaban la salida del fin de
    semana. Ya no existe nada que las lea. Dejarlas pasar en silencio sería la
    trampa exacta que el sistema lleva meses pagando, y encima en la dirección
    más engañosa que hay: alguien escribe `weekly_limit: 2` convencido de que
    se está poniendo un tope, el fichero valida, y no pasa absolutamente nada.
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
    # `load_3d/7d`. Viaja porque la vista 5 lo necesita para juzgar una salida
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
    load: float  # carga usada para load_3d/7d (real o estimada)
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
    weekend: WeekendSummary | None = None
    intense_count: IntensityCount | None = None

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


def previous_weekday(day: date, weekday_name: str) -> date:
    """La última fecha ESTRICTAMENTE anterior a `day` con ese día de la semana."""
    target = WEEKDAY_NAMES.index(weekday_name)
    delta = (day.weekday() - target) % 7
    if delta == 0:
        delta = 7
    return day - timedelta(days=delta)


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
    """
    start = day - timedelta(days=window_days - 1)
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
    """`load_Nd` para cada uno de los últimos `days` días que terminan en `end_day`."""
    return {
        end_day - timedelta(days=i): rolling_load(rides, end_day - timedelta(days=i), window_days)
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
# Fin de semana y presupuesto semanal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WeekendSummary:
    days: list[date]
    total_hours: float
    intense_rides: int | None  # None = no se puede saber (salidas sin clasificar)
    unknown_rides: int
    rides: list[ClassifiedRide]


def weekend_summary(
    rides: Sequence[ClassifiedRide],
    day: date,
    cycling_cfg: dict[str, Any],
) -> WeekendSummary:
    """Resumen del fin de semana INMEDIATAMENTE ANTERIOR a `day`.

    Sobre las salidas sin clasificar: si hay alguna `desconocida` y ninguna
    intensa confirmada, `intense_rides` va a None y la regla del lunes se salta
    en vez de asumir que el fin de semana fue suave. Si ya hay una intensa
    confirmada, el dato que falta no cambia la conclusión y la regla sí evalúa.
    """
    day_names = (cycling_cfg.get("weekend", {}) or {}).get("days") or ["saturday", "sunday"]
    days = sorted(previous_weekday(day, name) for name in day_names)

    picked = [r for r in rides if r.date in days]
    total_hours = sum(r.hours for r in picked)
    intense = sum(1 for r in picked if r.level == "intensa")
    unknown = sum(1 for r in picked if r.level == UNKNOWN)

    intense_rides: int | None = intense
    if unknown and intense == 0:
        intense_rides = None

    return WeekendSummary(
        days=days,
        total_hours=total_hours,
        intense_rides=intense_rides,
        unknown_rides=unknown,
        rides=picked,
    )


@dataclass(frozen=True)
class IntensityCount:
    """Sesiones intensas de la semana en curso. Cuenta; no limita.

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
    """

    used: int
    detail: list[str]
    week_start: date
    # Salidas de la semana que no se pudieron clasificar. Podrían haber sido
    # intensas, así que `used` es un MÍNIMO, no el número.
    unknown: int = 0

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
        """
        if self.used == 0 and not self.unknown:
            return "ninguna sesión intensa esta semana todavía"
        cuantas = (
            "1 sesión intensa" if self.used == 1 else f"{self.used} sesiones intensas"
        )
        base = f"llevas {cuantas} esta semana"
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
        """
        return {
            "used": self.used,
            "unknown": self.unknown,
            "week_start": self.week_start.isoformat(),
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


def intensity_count(
    rides: Sequence[ClassifiedRide],
    sessions: Sequence[StrengthSession],
    day: date,
    cycling_cfg: dict[str, Any],
) -> IntensityCount:
    """Sesiones intensas HECHAS en la semana en curso, hasta `day` incluido.

    Cuenta lo EJECUTADO: un HIIT programado que no se hizo no cuenta. Y no
    limita nada: el número sale en el mensaje y se guarda, y eso es todo lo
    que hace.
    """
    cfg = ((cycling_cfg.get("recommendation", {}) or {}).get("intensity_count", {})) or {}

    # La guarda vive aquí y no solo en `config_loader` porque `config_loader`
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
    counts = cfg.get("counts_as_intense", {}) or {}
    start = week_start(day, str(cfg.get("week_starts_on", "monday")))

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
        week_start=start,
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
    for name, win in (("load_3d", 3), ("load_7d", 7)):
        series = load_series(classified, day, days=max(history_days, 90), window_days=win)
        sig.values[name] = series.get(day)
        sig.history[name] = series
        if series.get(day) is None:
            desde = day - timedelta(days=win - 1)
            culpables = sorted(
                {r.date for r in classified if desde <= r.date <= day and not r.load_known}
            )
            notes.append(
                f"{name}: sin dato — {len(culpables)} salida(s) sin carga ni forma de "
                f"estimarla ({', '.join(d.isoformat() for d in culpables)}). "
                f"Se prefiere no dar el número a darlo por lo bajo."
            )

    # --- ciclismo ----------------------------------------------------------
    weekend = weekend_summary(classified, day, cycling_cfg)
    sig.values["weekend_total_hours"] = round(weekend.total_hours, 3)
    sig.values["weekend_intense_rides"] = weekend.intense_rides
    if weekend.intense_rides is None:
        notes.append(
            f"weekend_intense_rides: {weekend.unknown_rides} salida(s) sin clasificar "
            "el fin de semana; la regla del lunes se salta en vez de asumir que fue suave"
        )

    conteo = intensity_count(classified, sessions, day, cycling_cfg)
    sig.values["week_intense_count"] = conteo.used
    # `week_intense_remaining` se ha quitado de aquí junto con el límite. Era
    # `limit - used`, y sin límite no queda nada de lo que quedar. Se borra en
    # vez de dejarse a cero: una señal que vale siempre cero es una señal que
    # una regla puede leer y comparar, y entonces el cupo vuelve por la puerta
    # de atrás sin que nadie lo haya decidido.
    if conteo.unknown:
        # Va a `notes` y no solo a la nota de la bici porque
        # `week_intense_count` se enseña como un número redondo -"llevas 2
        # sesiones intensas esta semana"- y ese número es un MÍNIMO, no el
        # dato. Enseñar un mínimo con cara de dato es la forma más limpia que
        # hay de que alguien se fíe de él, y ahora se enseña todos los días.
        cuantas = (
            "1 salida de esta semana sin clasificar que pudo ser intensa"
            if conteo.unknown == 1
            else f"{conteo.unknown} salidas de esta semana sin clasificar y "
            f"cualquiera pudo ser intensa"
        )
        notes.append(
            f"week_intense_count: {conteo.used} es un MÍNIMO, no el número: "
            f"hay {cuantas}"
        )

    lookback = int(
        ((cycling_cfg.get("recommendation", {}) or {}).get("lookback_days", 1))
    )
    sig.values["yesterday_ride_level"] = last_ride_level(classified, day, lookback)

    # --- formulario --------------------------------------------------------
    slider_keys = (
        config.slider_keys()
        if hasattr(config, "slider_keys")
        else [s["key"] for s in raw.get("checkin_sliders", [])]
    )
    for key in slider_keys:
        sig.values[key] = checkin.values.get(key) if checkin else None
        hist: dict[date, Any] = {}
        for c in checkin_history:
            v = c.values.get(key)
            if v is not None:
                hist[c.date] = v
        if checkin and checkin.values.get(key) is not None:
            hist[day] = checkin.values[key]
        sig.history[key] = hist

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
    # `load_3d` y `load_7d`, que sí están construidos a estas alturas: el
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
    sig.weekend = weekend
    sig.intense_count = conteo

    return sig
