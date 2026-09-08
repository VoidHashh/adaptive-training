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


# ---------------------------------------------------------------------------
# Entradas
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DayMetrics:
    """Una fila de wellness de Garmin."""

    date: date
    hrv: float | None = None
    rhr: float | None = None
    sleep_min: int | None = None
    sleep_score: int | None = None
    body_battery: int | None = None
    readiness: int | None = None


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


@dataclass(frozen=True)
class ClassifiedRide:
    """Salida de `classify_ride`: la actividad más su etiqueta y su carga."""

    ride: Ride
    level: str  # suave | media | intensa | desconocida
    source: str  # zones | fallback_te | none
    load: float  # carga usada para load_3d/7d (real o estimada)
    load_estimated: bool
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
    budget: IntensityBudget | None = None

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
        """Lo que se guarda en `decisions.inputs_snapshot_json`."""
        return {
            "day": self.day.isoformat(),
            "weekday": self.weekday(),
            "values": {k: v for k, v in sorted(self.values.items())},
            "adaptive": {k: v for k, v in sorted(self.adaptive.items())},
            "notes": list(self.notes),
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

    load, estimated = _ride_load(ride, level, cycling_cfg)
    return ClassifiedRide(
        ride=ride,
        level=level,
        source=source,
        load=load,
        load_estimated=estimated,
        zone_pct=pcts,
    )


def _ride_load(ride: Ride, level: str, cycling_cfg: dict[str, Any]) -> tuple[float, bool]:
    """Carga de la salida: la real de Garmin, o una estimada por duración."""
    if ride.training_load is not None:
        return float(ride.training_load), False

    est = (cycling_cfg.get("load", {}) or {}).get("fallback_estimate", {}) or {}
    if not est.get("enabled") or not ride.duration_s:
        return 0.0, False

    per_hour = (est.get("load_per_hour") or {}).get(level)
    if per_hour is None:
        # Sin factor para ese nivel (p. ej. `desconocida`): no inventamos.
        return 0.0, False
    return (ride.duration_s / 3600.0) * float(per_hour), True


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


def rolling_load(rides: Sequence[ClassifiedRide], day: date, window_days: int) -> float:
    """Suma de carga en la ventana de `window_days` que termina en `day`."""
    start = day - timedelta(days=window_days - 1)
    return sum(r.load for r in rides if start <= r.date <= day)


def load_series(
    rides: Sequence[ClassifiedRide],
    end_day: date,
    days: int,
    window_days: int,
) -> dict[date, float]:
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
class IntensityBudget:
    limit: int
    used: int
    detail: list[str]
    week_start: date

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit


def intensity_budget(
    rides: Sequence[ClassifiedRide],
    sessions: Sequence[StrengthSession],
    day: date,
    cycling_cfg: dict[str, Any],
) -> IntensityBudget:
    """Sesiones intensas consumidas en la semana en curso, hasta `day` incluido.

    Cuenta lo EJECUTADO: un HIIT programado que no se hizo no gasta presupuesto.
    """
    cfg = ((cycling_cfg.get("recommendation", {}) or {}).get("intensity_budget", {})) or {}
    limit = int(cfg.get("weekly_limit", 3))
    counts = cfg.get("counts_as_intense", {}) or {}
    start = week_start(day, str(cfg.get("week_starts_on", "monday")))

    detail: list[str] = []
    used = 0

    if counts.get("ride_intensa", True):
        for r in rides:
            if start <= r.date <= day and r.level == "intensa":
                used += 1
                detail.append(f"{r.date.isoformat()}: salida INTENSA")

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

    return IntensityBudget(limit=limit, used=used, detail=sorted(detail), week_start=start)


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
    sessions: Sequence[StrengthSession] = (),
    checkin: Checkin | None = None,
    checkin_history: Sequence[Checkin] = (),
    history_days: int = 14,
) -> Signals:
    """Construye el `Signals` de un día a partir de los datos crudos.

    `config` puede ser un `Config` o un dict; solo se le piden secciones.
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
    for attr in ("hrv", "rhr", "sleep_min", "sleep_score", "body_battery", "readiness"):
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

    # --- umbrales adaptativos ---------------------------------------------
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

    # --- ciclismo ----------------------------------------------------------
    weekend = weekend_summary(classified, day, cycling_cfg)
    sig.values["weekend_total_hours"] = round(weekend.total_hours, 3)
    sig.values["weekend_intense_rides"] = weekend.intense_rides
    if weekend.intense_rides is None:
        notes.append(
            f"weekend_intense_rides: {weekend.unknown_rides} salida(s) sin clasificar "
            "el fin de semana; la regla del lunes se salta en vez de asumir que fue suave"
        )

    budget = intensity_budget(classified, sessions, day, cycling_cfg)
    sig.values["week_intense_count"] = budget.used
    sig.values["week_intense_remaining"] = budget.remaining

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

    sig.rides = classified
    sig.weekend = weekend
    sig.budget = budget

    return sig
