"""Evaluador de las reglas del semáforo.

Las reglas viven en `thresholds.red` y `thresholds.amber` del YAML. Este módulo
no sabe nada del contenido concreto: solo interpreta la gramática.

GRAMÁTICA DE `when`
-------------------
    {all: [expr, expr, ...]}        todas
    {any: [expr, expr, ...]}        alguna
    {not: expr}                     negación
    {señal: {op: valor, ...}}       varias señales o varios operadores = Y

Operadores: gte, gt, lte, lt, eq, ne, in, not_in
            gt_adaptive, gte_adaptive, lt_adaptive, lte_adaptive
                -> comparan contra un umbral de `adaptive_thresholds`, que se
                   recalcula cada día contra la propia distribución histórica
            gt_option, gte_option, lt_option, lte_option, eq_option, ne_option
                -> comparan contra una opción del propio YAML, nombrada por su
                   ruta: `{gt_option: cycling.weekend.total_hours_threshold}`

Modificador: consecutive_days: N
                -> la condición debe cumplirse hoy Y los N-1 días anteriores

POR QUÉ EXISTE `_option`
------------------------
Sin él, un umbral se escribe dos veces: una en la sección que lo documenta y
otra, como literal, dentro de la regla. Y entonces las dos copias se separan.
Pasó: `cycling.weekend.total_hours_threshold: 2.5` llevaba semanas con un
comentario de calibración explicando por qué era 2,5, y la regla `resaca_finde`
comparaba contra un `4.0` escrito a mano que no se alcanzaba nunca. La opción
no estaba mal puesta: es que no la leía nadie.

Un umbral adaptativo puede valer `None` legítimamente -no hay historial
suficiente- y por eso su rama se salta la regla y lo anota. Una opción NO: si
la ruta no existe o no es un número, el YAML está mal escrito, y eso es un
error de arranque, no un dato que falte. Se levanta `RuleError`.

LÓGICA DE TRES VALORES
----------------------
Una condición puede salir cierta, falsa o INDETERMINADA (falta el dato). Es la
diferencia entre "la HRV está bien" y "no sé cómo está la HRV", y el sistema no
debe confundirlas: sin dato no se asume ni bien ni mal, la regla se salta y se
anota en `skipped_rules_json`.

La combinación sigue lógica de Kleene:
    all -> si alguna es falsa, falsa (aunque otras falten)
           si no, si alguna falta, indeterminada
    any -> si alguna es cierta, cierta (aunque otras falten)
           si no, si alguna falta, indeterminada

Consecuencia práctica, y es deliberada: en un `any`, una rama cierta manda
aunque a otra rama le falte el dato. `requires` se usa para explicar qué
faltaba cuando la regla sí acaba saltándose, no como una puerta previa que tire
la regla al primer hueco.

EL EJEMPLO QUE HABÍA AQUÍ YA NO EXISTE, Y CONVIENE QUE SE SEPA
--------------------------------------------------------------
Este párrafo ilustraba lo de arriba con `resaca_finde`, que declaraba en
`requires` tanto `weekend_intense_rides` como `weekend_total_hours` y tenía un
`when` de tipo `any`. Esa regla se borró, y con el paso al recuento rodante se
han ido también las dos señales: `build_signals` ya no escribe ninguna de las
dos. O sea que el ejemplo describía, en presente, una regla muerta que leía dos
señales muertas.

Se deja dicho en vez de sustituirlo por otro ejemplo porque no hay otro ejemplo
que poner: de las DOCE reglas que quedan en `thresholds`, ninguna usa `any`.
`_kleene_any` sigue implementada aquí abajo y `when: {any: [...]}` sigue siendo
gramática válida, pero hoy no la ejercita ni el `config.yaml` ni ningún test.
Es una rama de código viva que nadie recorre: si se rompiera, nadie se
enteraría hasta que alguien escribiera la primera regla con `any` -y entonces
se enteraría por un semáforo equivocado, no por un error-. Queda anotado aquí
en vez de borrarlo a la ligera, porque `any` es gramática del lenguaje de
reglas y no una opción del fichero, y quitarlo es una decisión aparte.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from app.engine.signals import MEDIDAS_DEL_RELOJ, WEEKDAY_NAMES, Signals

# Estados posibles de una regla.
FIRED = "fired"
NOT_FIRED = "not_fired"
SKIPPED = "skipped"  # falta algún dato
NOT_APPLICABLE = "not_applicable"  # no toca hoy (only_on_weekday)

COMPARISONS = {
    "gte": lambda a, b: a >= b,
    "gt": lambda a, b: a > b,
    "lte": lambda a, b: a <= b,
    "lt": lambda a, b: a < b,
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
}

ADAPTIVE_SUFFIX = "_adaptive"
OPTION_SUFFIX = "_option"

# `MEDIDAS_DEL_RELOJ` se define ahora junto a `DayMetrics`, de donde salen sus
# cinco nombres, y se importa arriba. Este módulo sigue siendo quien decide con
# ellas -promover a ámbar un verde decidido a ciegas- pero ya no es quien las
# escribe: eran tres copias sueltas y el porqué está contado en el original.
#
# El nombre local se conserva porque medio proyecto lo importa de aquí, y es el
# MISMO objeto, no una copia.

# El nombre con el que se guarda el ámbar por precaución. No es una regla del
# YAML -no puede serlo, ver `evaluate_light`- pero se escribe en
# `fired_rules_json` y en `trigger_rule` como cualquier otra, para que el
# mensaje, el registro y la auditoría no necesiten un caso especial.
REGLA_SIN_DATOS = "ambar_sin_datos"

# El interruptor, dentro de `thresholds` y al lado de las reglas que modula.
CLAVE_PRECAUCION = "ambar_sin_datos"


class RuleError(ValueError):
    """Regla mal escrita en el YAML. Es un error de programación, no de datos."""


def resolve_option(path: Any, options: dict[str, Any] | None) -> float:
    """Devuelve el número que hay en `path` ('a.b.c') dentro del YAML crudo.

    Pública a propósito: el `config_loader` la usa para validar en el arranque
    exactamente lo mismo que el motor resolverá a las 06:30. Si fueran dos
    implementaciones separadas podrían dejar de coincidir, que es justo la
    familia de fallo que este operador viene a cerrar.

    Todo lo que no sea un número es error duro. Un `bool` tampoco vale, aunque
    en Python sea un `int`: comparar 2,5 horas contra `True` da un resultado
    perfectamente creíble y completamente inventado.
    """
    ruta = str(path)
    if not isinstance(options, dict):
        raise RuleError(
            f"la opción '{ruta}' no se puede resolver: no se ha pasado el config"
        )

    node: Any = options
    recorrido: list[str] = []
    for parte in ruta.split("."):
        if not isinstance(node, dict) or parte not in node:
            hasta = f" (se llegó hasta '{'.'.join(recorrido)}')" if recorrido else ""
            raise RuleError(f"la opción '{ruta}' no existe en config.yaml{hasta}")
        node = node[parte]
        recorrido.append(parte)

    if isinstance(node, bool) or not isinstance(node, (int, float)):
        raise RuleError(
            f"la opción '{ruta}' vale {node!r} y un umbral tiene que ser un número"
        )
    return node


@dataclass
class RuleResult:
    name: str
    level: str  # red | amber
    status: str
    description: str = ""
    missing: list[str] = field(default_factory=list)
    detail: list[str] = field(default_factory=list)

    @property
    def fired(self) -> bool:
        return self.status == FIRED

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "level": self.level, "status": self.status}
        if self.description:
            d["description"] = self.description
        if self.missing:
            d["missing"] = sorted(set(self.missing))
        if self.detail:
            d["detail"] = self.detail
        return d


@dataclass
class LightDecision:
    light: str  # green | amber | red
    trigger_rule: str | None
    fired: list[RuleResult] = field(default_factory=list)
    skipped: list[RuleResult] = field(default_factory=list)
    evaluated: list[RuleResult] = field(default_factory=list)

    def fired_names(self) -> list[str]:
        return [r.name for r in self.fired]


# ---------------------------------------------------------------------------
# Evaluación de expresiones
# ---------------------------------------------------------------------------


@dataclass
class _Ctx:
    """Acumula lo aprendido durante la evaluación, para poder explicarla.

    Lleva además `options`, que es lo único que entra en vez de salir: el YAML
    crudo contra el que se resuelven los operadores `_option`. Viaja aquí y no
    como parámetro suelto porque si no habría que pasarlo por las cuatro
    funciones de la evaluación sin que ninguna intermedia lo mire.
    """

    missing: list[str] = field(default_factory=list)
    detail: list[str] = field(default_factory=list)
    options: dict[str, Any] | None = None


def _kleene_all(results: list[bool | None]) -> bool | None:
    if any(r is False for r in results):
        return False
    if any(r is None for r in results):
        return None
    return True


def _kleene_any(results: list[bool | None]) -> bool | None:
    if any(r is True for r in results):
        return True
    if any(r is None for r in results):
        return None
    return False


def _eval_expr(expr: Any, signals: Signals, ctx: _Ctx) -> bool | None:
    if expr is None:
        return True
    if not isinstance(expr, dict):
        raise RuleError(f"expresión no válida: {expr!r}")

    if "all" in expr:
        return _kleene_all([_eval_expr(sub, signals, ctx) for sub in expr["all"]])
    if "any" in expr:
        return _kleene_any([_eval_expr(sub, signals, ctx) for sub in expr["any"]])
    if "not" in expr:
        inner = _eval_expr(expr["not"], signals, ctx)
        return None if inner is None else (not inner)

    results = [_eval_signal(name, cond, signals, ctx) for name, cond in expr.items()]
    return _kleene_all(results)


def _eval_signal(name: str, cond: Any, signals: Signals, ctx: _Ctx) -> bool | None:
    if not isinstance(cond, dict):
        # Azúcar: `{fatigue: 7}` equivale a `{fatigue: {eq: 7}}`.
        cond = {"eq": cond}

    days = int(cond.get("consecutive_days", 1))
    ops = {k: v for k, v in cond.items() if k != "consecutive_days"}
    if not ops:
        raise RuleError(f"la condición de '{name}' no tiene ningún operador")

    if days <= 1:
        return _eval_ops(name, signals.get(name), ops, signals, ctx, label=name)

    # Con `consecutive_days` hay que mirar la serie, no solo el valor de hoy.
    series = signals.series(name)
    per_day: list[bool | None] = []
    for i in range(days):
        d = signals.day - timedelta(days=i)
        value = series.get(d)
        per_day.append(
            _eval_ops(name, value, ops, signals, ctx, label=f"{name}@{d.isoformat()}")
        )
    out = _kleene_all(per_day)
    if out is True:
        ctx.detail.append(f"{name}: se cumple {days} días seguidos")
    return out


def _eval_ops(
    name: str,
    value: Any,
    ops: dict[str, Any],
    signals: Signals,
    ctx: _Ctx,
    label: str,
) -> bool | None:
    if value is None:
        ctx.missing.append(name)
        return None

    results: list[bool | None] = []
    for op, operand in ops.items():
        if op.endswith(OPTION_SUFFIX):
            base_op = op[: -len(OPTION_SUFFIX)]
            fn = COMPARISONS.get(base_op)
            if fn is None:
                raise RuleError(f"operador desconocido: {op}")
            # Aquí NO se anota como dato que falta: una opción ausente no es un
            # hueco en los datos del día, es una regla mal escrita.
            threshold = resolve_option(operand, ctx.options)
            ok = fn(value, threshold)
            results.append(ok)
            if ok:
                ctx.detail.append(
                    f"{label} = {_fmt(value)} {base_op} {_fmt(threshold)} "
                    f"({operand})"
                )
            continue

        if op.endswith(ADAPTIVE_SUFFIX):
            base_op = op[: -len(ADAPTIVE_SUFFIX)]
            threshold = signals.adaptive.get(operand)
            if threshold is None:
                ctx.missing.append(str(operand))
                results.append(None)
                continue
            fn = COMPARISONS.get(base_op)
            if fn is None:
                raise RuleError(f"operador desconocido: {op}")
            ok = fn(value, threshold)
            results.append(ok)
            if ok:
                ctx.detail.append(
                    f"{label} = {_fmt(value)} {base_op} {operand} ({_fmt(threshold)})"
                )
            continue

        fn = COMPARISONS.get(op)
        if fn is None:
            raise RuleError(f"operador desconocido: {op}")
        ok = fn(value, operand)
        results.append(ok)
        if ok:
            ctx.detail.append(f"{label} = {_fmt(value)} {op} {_fmt(operand)}")

    return _kleene_all(results)


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


# ---------------------------------------------------------------------------
# Evaluación de reglas y del semáforo
# ---------------------------------------------------------------------------


def evaluate_rule(
    rule: dict[str, Any],
    signals: Signals,
    level: str = "",
    options: dict[str, Any] | None = None,
) -> RuleResult:
    name = rule.get("name", "<sin nombre>")
    result = RuleResult(name=name, level=level, status=NOT_FIRED)
    result.description = str(rule.get("description", "")).strip()

    only_on = rule.get("only_on_weekday")
    if only_on:
        wanted = {str(d).lower() for d in only_on}
        unknown = wanted - set(WEEKDAY_NAMES)
        if unknown:  # pragma: no cover - lo valida el config_loader
            raise RuleError(f"regla '{name}': día de la semana desconocido {sorted(unknown)}")
        if signals.weekday() not in wanted:
            result.status = NOT_APPLICABLE
            result.detail = [f"solo se evalúa: {', '.join(sorted(wanted))}"]
            return result

    ctx = _Ctx(options=options)
    outcome = _eval_expr(rule.get("when"), signals, ctx)

    result.missing = ctx.missing

    if outcome is None:
        result.status = SKIPPED
        # `requires` no decide, pero sí documenta: si declara datos que
        # tampoco están, se añaden al motivo para que el log sea legible.
        declared = [r for r in (rule.get("requires") or []) if not signals.has(r)]
        result.missing = sorted(set(ctx.missing) | set(declared))
    elif outcome:
        result.status = FIRED
        # El detalle solo se conserva si la regla dispara. Durante la
        # evaluación se anota cada condición que sale cierta, y en una regla
        # que NO dispara eso deja líneas como "lower_discomfort = 1 lte 6" en
        # una regla que exige además `gte: 5`: cierto en la parte, falso en el
        # conjunto, y muy fácil de leer al revés dentro de tres semanas.
        result.detail = ctx.detail
    return result


def _verde_a_ciegas(skipped: list[RuleResult]) -> list[RuleResult]:
    """De las reglas saltadas, las que se saltaron por un dato del reloj.

    Solo el reloj, y es una frontera pensada, no una simplificación. Un verde
    también puede salir sin mirar media hoja de reglas cuando no hay check-in
    -`lumbar_medio`, `cervicales_hombros` y `cansancio_alto` se saltan las
    tres-, y por el principio que rige esto («no mirar y estar bien no pueden
    pintarse igual») ese verde es igual de ciego.

    La diferencia no está en el principio: está en que uno se puede deshacer y
    el otro no. El ámbar por precaución nace PROVISIONAL: a las 09:00 el
    scheduler vuelve a pedirle la noche a Garmin, y si llega, recalcula y anuncia
    el cambio como anulación. Para el check-in no hay ese rescate -nadie vuelve a
    preguntarle al usuario por la mañana de ayer-, así que un ámbar por falta de
    check-in se quedaría puesto para siempre sin camino de vuelta a verde. Y un
    ámbar del que no se sale no avisa de nada: castiga.

    Queda dicho aquí en vez de omitido en silencio, porque es la mitad del
    problema que este arreglo NO resuelve.
    """
    return [r for r in skipped if set(r.missing) & MEDIDAS_DEL_RELOJ]


def evaluate_light(config: Any, signals: Signals) -> LightDecision:
    """Rojo primero, luego ámbar, luego verde.

    Se evalúan TODAS las reglas de un nivel aunque la primera ya haya disparado:
    el mensaje de Telegram dice cuál lo ha disparado, pero el log guarda todas,
    que es lo que hace depurable un ámbar raro tres semanas después.

    Y UN VERDE SIN DATOS NO ES UN VERDE
    -----------------------------------
    Si al final de las dos pasadas no ha disparado nada PERO alguna regla se
    quedó sin evaluar porque Garmin todavía no había subido la noche, el color
    no se queda en verde: sube a ámbar y se anota la regla sintética
    `ambar_sin_datos` como disparo.

    No puede escribirse como una regla del YAML, y conviene entender por qué
    antes de intentarlo. Una regla dispara cuando su `when` sale cierto, y un
    `when` sobre un dato que no está sale INDETERMINADO, que es exactamente lo
    que significa `SKIPPED`. O sea: la condición que habría que escribir es «esta
    regla se saltó», y eso no es una propiedad de las señales -que es lo único
    que la gramática sabe mirar-, es una propiedad del resultado de evaluar. El
    lenguaje de reglas no puede hablar de sí mismo.

    Lo que sí está en el YAML es el interruptor, `thresholds.ambar_sin_datos`.
    Apagarlo devuelve el comportamiento de antes. Está ahí para que la decisión
    se lea en el fichero que se lee, y no haya que venir al motor a descubrir por
    qué una mañana sin HRV salió ámbar.
    """
    raw = config.raw if hasattr(config, "raw") else config
    thresholds = raw.get("thresholds", {}) or {}

    evaluated: list[RuleResult] = []
    fired: list[RuleResult] = []
    skipped: list[RuleResult] = []
    light = "green"
    trigger: str | None = None

    for level in ("red", "amber"):
        level_fired: list[RuleResult] = []
        for rule in thresholds.get(level, []) or []:
            res = evaluate_rule(rule, signals, level=level, options=raw)
            evaluated.append(res)
            if res.status == FIRED:
                level_fired.append(res)
            elif res.status == SKIPPED:
                skipped.append(res)
        if level_fired and light == "green":
            light = level
            trigger = level_fired[0].name
        fired.extend(level_fired)

    # El ámbar por precaución. Va DESPUÉS del bucle y solo sobre verde: si ya
    # hay rojo o ámbar por una regla de verdad, el color no cambia y el disparo
    # que lo explica tampoco. Aquí solo se corrige el verde que en realidad era
    # un «no lo sé».
    if light == "green" and thresholds.get(CLAVE_PRECAUCION, True):
        ciegas = _verde_a_ciegas(skipped)
        if ciegas:
            faltan = sorted(
                {m for r in ciegas for m in r.missing if m in MEDIDAS_DEL_RELOJ}
            )
            aviso = RuleResult(
                name=REGLA_SIN_DATOS,
                level="amber",
                status=FIRED,
                description=(
                    "ámbar por precaución: el semáforo se ha decidido sin poder "
                    "mirar el bienestar"
                ),
            )
            # `missing` lleva las MEDIDAS y `detail` las REGLAS, y no es lo
            # mismo: el mensaje al usuario habla de medidas -«no se ha podido
            # evaluar la variabilidad»- y la depuración de tres semanas después
            # necesita saber qué reglas quedaron mudas.
            aviso.missing = faltan
            aviso.detail = [
                "reglas sin evaluar: " + ", ".join(sorted(r.name for r in ciegas))
            ]
            light = "amber"
            trigger = REGLA_SIN_DATOS
            fired.append(aviso)
            evaluated.append(aviso)

    return LightDecision(
        light=light,
        trigger_rule=trigger,
        fired=fired,
        skipped=skipped,
        evaluated=evaluated,
    )
