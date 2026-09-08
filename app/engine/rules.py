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

Modificador: consecutive_days: N
                -> la condición debe cumplirse hoy Y los N-1 días anteriores

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

Consecuencia práctica, y es deliberada: `resaca_finde` declara en `requires`
tanto `weekend_intense_rides` como `weekend_total_hours`, pero su `when` es un
`any`. Si el sábado quedó una salida sin clasificar (intense_rides = None) pero
el fin de semana sumó 5 horas, la regla DISPARA igual: la rama de las horas ya
decide por sí sola y el dato que falta no cambiaría la conclusión. `requires`
se usa para explicar qué faltaba cuando la regla sí acaba saltándose, no como
una puerta previa que tire la regla al primer hueco.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from app.engine.signals import WEEKDAY_NAMES, Signals

# Estados posibles de una regla.
FIRED = "fired"
NOT_FIRED = "not_fired"
SKIPPED = "skipped"  # falta algún dato
NOT_APPLICABLE = "not_applicable"  # no toca hoy (only_on_weekday)

_COMPARISONS = {
    "gte": lambda a, b: a >= b,
    "gt": lambda a, b: a > b,
    "lte": lambda a, b: a <= b,
    "lt": lambda a, b: a < b,
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
}

_ADAPTIVE_SUFFIX = "_adaptive"


class RuleError(ValueError):
    """Regla mal escrita en el YAML. Es un error de programación, no de datos."""


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
    """Acumula lo aprendido durante la evaluación, para poder explicarla."""

    missing: list[str] = field(default_factory=list)
    detail: list[str] = field(default_factory=list)


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
        if op.endswith(_ADAPTIVE_SUFFIX):
            base_op = op[: -len(_ADAPTIVE_SUFFIX)]
            threshold = signals.adaptive.get(operand)
            if threshold is None:
                ctx.missing.append(str(operand))
                results.append(None)
                continue
            fn = _COMPARISONS.get(base_op)
            if fn is None:
                raise RuleError(f"operador desconocido: {op}")
            ok = fn(value, threshold)
            results.append(ok)
            if ok:
                ctx.detail.append(
                    f"{label} = {_fmt(value)} {base_op} {operand} ({_fmt(threshold)})"
                )
            continue

        fn = _COMPARISONS.get(op)
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


def evaluate_rule(rule: dict[str, Any], signals: Signals, level: str = "") -> RuleResult:
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

    ctx = _Ctx()
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


def evaluate_light(config: Any, signals: Signals) -> LightDecision:
    """Rojo primero, luego ámbar, luego verde.

    Se evalúan TODAS las reglas de un nivel aunque la primera ya haya disparado:
    el mensaje de Telegram dice cuál lo ha disparado, pero el log guarda todas,
    que es lo que hace depurable un ámbar raro tres semanas después.
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
            res = evaluate_rule(rule, signals, level=level)
            evaluated.append(res)
            if res.status == FIRED:
                level_fired.append(res)
            elif res.status == SKIPPED:
                skipped.append(res)
        if level_fired and light == "green":
            light = level
            trigger = level_fired[0].name
        fired.extend(level_fired)

    return LightDecision(
        light=light,
        trigger_rule=trigger,
        fired=fired,
        skipped=skipped,
        evaluated=evaluated,
    )
