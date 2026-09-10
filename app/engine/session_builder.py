"""Construcción de la sesión del día.

Toma la rutina que toca por calendario y le aplica, EN ESTE ORDEN:

  1. reglas especiales que retiran ejercicios   (`retirada_peso_muerto`)
  2. progresión                                  (solo si el semáforo la permite)
  3. recorte de la semana de descarga            (carga y volumen)
  4. reglas especiales que recortan carga        (`descarga_press_hombro`)
  5. recorte del ámbar                           (-25% de series efectivas)
  6. bloque HIIT                                 (solo si procede)

El orden importa y no es intercambiable. Dos ejemplos de por qué:

  - la retirada de ejercicios va PRIMERO porque no tiene sentido calcular la
    progresión de un peso muerto que hoy no se va a hacer, y porque hacerlo
    ensuciaría el mensaje de Telegram con una subida de un ejercicio ausente
  - el recorte del ámbar va DESPUÉS de la progresión porque el -25% se aplica
    sobre la sesión que tocaba hoy, no sobre la de la semana pasada; si fuera
    al revés, un día ámbar congelaría el esquema en el estado anterior

EL CALENTAMIENTO NUNCA SE TOCA
------------------------------
Ni el ámbar, ni la descarga, ni el recorte de expendables lo eliminan. El día
que vienes tocado calentar importa más, no menos.

SUPERSERIES
-----------
`superset_id` viaja intacto. El PUT de Hevy reemplaza la rutina completa, así
que reconstruirla sin ese campo deshace la superserie en la app.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.engine.progression import (
    ProgressionPlan,
    apply_deload_volume,
    con_carga_vigente,
    series_efectivas_vigentes,
)
from app.engine.sets import excludes, warmup_flags

FULL = "full"
REDUCED = "reduced"
RECOVERY = "recovery"
REST = "rest"


@dataclass
class BuiltSession:
    """La sesión de hoy, lista para escribirse en Hevy y contarse por Telegram."""

    day: date
    kind: str  # full | reduced | recovery | rest | pool | bike
    routine_key: str | None
    title: str
    exercises: list[dict[str, Any]] = field(default_factory=list)
    hevy_routine_id: str | None = None
    write_to_hevy: bool = True
    hiit_block: str | None = None
    # Todo lo que se le ha hecho a la rutina base, en orden, con su motivo.
    changes: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    deferred_from: date | None = None
    # La carga que queda VIGENTE a partir de hoy, por clave de ejercicio. Se
    # captura justo después de la progresión y ANTES de la descarga, los
    # recortes por regla y el ámbar, porque esos tres son modulaciones del día y
    # no un objetivo nuevo: persistir un ×0,9 de semana de descarga bajaría el
    # objetivo de verdad y la carga no volvería a subir sola nunca. Lo que se
    # escribe en Hevy hoy puede ser menos que esto; lo de aquí es a lo que se
    # vuelve mañana.
    target_sets: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "kind": self.kind,
            "routine": self.routine_key,
            "title": self.title,
            "hevy_routine_id": self.hevy_routine_id,
            "write_to_hevy": self.write_to_hevy,
            "hiit_block": self.hiit_block,
            "changes": self.changes,
            "dropped": self.dropped,
            "notes": self.notes,
            "deferred_from": self.deferred_from.isoformat() if self.deferred_from else None,
            "exercises": self.exercises,
        }

    def total_effective_sets(self, set_cfg: dict[str, Any]) -> int:
        n = 0
        for ex in self.exercises:
            flags = warmup_flags(ex.get("sets") or [], set_cfg, ex.get("key"))
            n += sum(1 for f in flags if not f)
        return n


# ---------------------------------------------------------------------------
# Calendario
# ---------------------------------------------------------------------------


def today_plan(config: Any, day: date) -> dict[str, Any]:
    """Qué toca hoy según la variante activa del calendario."""
    raw = config.raw if hasattr(config, "raw") else config
    cal = raw.get("calendar", {}) or {}
    variant = cal.get("variants", {}).get(cal.get("active_variant"), {}) or {}
    from app.engine.signals import WEEKDAY_NAMES

    return variant.get(WEEKDAY_NAMES[day.weekday()], {}) or {}


# ---------------------------------------------------------------------------
# Transformaciones
# ---------------------------------------------------------------------------


def _split(ex: dict[str, Any], set_cfg: dict[str, Any]):
    sets = ex.get("sets") or []
    flags = warmup_flags(sets, set_cfg, ex.get("key"))
    warm = [s for s, f in zip(sets, flags, strict=True) if f]
    work = [s for s, f in zip(sets, flags, strict=True) if not f]
    return warm, work


def apply_progression(
    exercises: list[dict[str, Any]],
    plan: ProgressionPlan,
    set_cfg: dict[str, Any],
) -> list[str]:
    """Aplica las mutaciones del plan de progresión. Devuelve el log."""
    by_key = {e.key: e for e in plan.changes}
    log: list[str] = []

    for ex in exercises:
        e = by_key.get(ex.get("key"))
        if e is None:
            continue
        warm, work = _split(ex, set_cfg)
        if not work:
            continue

        if e.add_sets:
            # La serie nueva copia la última efectiva: mismo peso y mismas
            # reps. Es lo que se hace en el gimnasio, y evita inventarse una
            # carga intermedia que nadie ha probado.
            for _ in range(e.add_sets):
                work.append(copy.deepcopy(work[-1]))
        if e.new_reps is not None:
            for s in work:
                if s.get("reps") is not None:
                    s["reps"] = e.new_reps
        if e.new_duration_s is not None:
            for s in work:
                if s.get("duration_s") is not None:
                    s["duration_s"] = e.new_duration_s
        if e.weight_delta_kg is not None:
            if e.apply_to == "all_sets":
                for s in work:
                    s["weight_kg"] = (s.get("weight_kg") or 0) + e.weight_delta_kg
            else:
                top = max(work, key=lambda s: s.get("weight_kg") or 0)
                top["weight_kg"] = (top.get("weight_kg") or 0) + e.weight_delta_kg

        ex["sets"] = warm + work
        log.append(f"{ex.get('name')}: {e.what}")
    return log


def apply_amber_reduction(
    exercises: list[dict[str, Any]],
    amber_cfg: dict[str, Any],
    set_cfg: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """-25% de SERIES EFECTIVAS y retirada de los ejercicios prescindibles."""
    reduction = float(amber_cfg.get("set_reduction", 0.25))
    floor = int(amber_cfg.get("min_sets_per_exercise", 2))
    drop = bool(amber_cfg.get("drop_expendable", True))

    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    log: list[str] = []

    for ex in exercises:
        if drop and ex.get("expendable_in_amber"):
            dropped.append(str(ex.get("name", ex.get("key"))))
            continue

        warm, work = _split(ex, set_cfg)
        if not excludes(set_cfg, "amber_set_reduction") and not set_cfg.get(
            "never_drop_warmup", True
        ):
            # Solo si el config dice a la vez que el calentamiento cuenta para
            # el recorte Y que se puede tirar. `never_drop_warmup` manda: es la
            # invariante del módulo y no la rompe una opción de conteo.
            warm, work = [], (ex.get("sets") or [])

        target = max(floor, int(len(work) * (1 - reduction)))
        if target < len(work):
            log.append(f"{ex.get('name')}: {len(work)}→{target} series efectivas")
            work = work[:target]

        ex = dict(ex)
        # El calentamiento se conserva SIEMPRE, incluso si el recorte dejara el
        # ejercicio en dos series: es el día en que más falta hace.
        ex["sets"] = warm + work
        kept.append(ex)

    if dropped:
        log.append("retirados por ser prescindibles en ámbar: " + ", ".join(dropped))
    return kept, log, dropped


def apply_load_factor(
    exercises: list[dict[str, Any]],
    factor: float,
    only: list[str] | None = None,
) -> list[str]:
    """Multiplica la carga por `factor`, redondeando a 0,5 kg."""
    log: list[str] = []
    for ex in exercises:
        if only is not None and ex.get("key") not in only:
            continue
        touched = False
        for s in ex.get("sets") or []:
            w = s.get("weight_kg")
            if w:
                s["weight_kg"] = round(float(w) * factor * 2) / 2
                touched = True
        if touched:
            log.append(f"{ex.get('name')}: carga al {int(factor * 100)}%")
    return log


def apply_rule_removals(
    exercises: list[dict[str, Any]],
    active_rules: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Retiradas de ejercicio por regla especial. Va ANTES de la progresión.

    `active_rules` son las que el motor de reglas ya ha decidido que están
    vigentes (incluidas las que vienen de días anteriores por `duration_days`).
    """
    log: list[str] = []
    dropped: list[str] = []

    for rule in active_rules:
        action = rule.get("action", {}) or {}
        remove = set(action.get("remove_exercises") or [])
        if not remove:
            continue
        before = len(exercises)
        names = [str(e.get("name")) for e in exercises if e.get("key") in remove]
        exercises = [e for e in exercises if e.get("key") not in remove]
        if len(exercises) < before:
            dropped.extend(names)
            log.append(f"'{rule.get('name')}': retirado {', '.join(names)}")

    return exercises, log, dropped


def apply_rule_load_cuts(
    exercises: list[dict[str, Any]],
    active_rules: list[dict[str, Any]],
) -> list[str]:
    """Recortes de carga por regla especial. Va DESPUÉS de la progresión.

    Este orden no es cosmético. `descarga_press_hombro` deja el press al 70%
    porque el hombro se quejó; si el recorte se aplicase antes de la progresión,
    una racha de sesiones limpias podría subirle el peso ese mismo día y
    devolverlo por encima del punto del que la regla lo estaba bajando. El
    recorte tiene que ser lo último que toca la carga para que sea un recorte
    de verdad y no una sugerencia.
    """
    log: list[str] = []
    for rule in active_rules:
        rl = (rule.get("action", {}) or {}).get("reduce_load") or {}
        if not rl:
            continue
        entries = apply_load_factor(
            exercises, float(rl.get("factor", 1.0)), list(rl.get("exercises") or [])
        )
        for entry in entries:
            log.append(f"'{rule.get('name')}': {entry}")
    return log


# ---------------------------------------------------------------------------
# HIIT
# ---------------------------------------------------------------------------


def hiit_applies(
    config: Any,
    routine_key: str,
    light: str,
    day: date,
    program_start: date | None,
) -> tuple[bool, str, bool]:
    """(¿aplica?, motivo, ¿el motivo es de hoy?).

    El tercer elemento separa dos cosas que se confundían. "El HIIT está
    desactivado en el config" y "hoy es ámbar y el HIIT solo va en verde" son
    los dos un `False` con su explicación, pero el primero vale igual mañana y
    dentro de seis meses, y el segundo solo hoy.

    Sin la distinción, el mensaje diario terminaba con un "sin HIIT: el HIIT
    está desactivado en el config" fijo, todos los días del año. Y una línea
    que sale siempre no se lee: se aprende a saltarla, arrastrando consigo las
    que sí cambian. Los motivos permanentes se siguen devolviendo -la traza del
    CLI y los tests los usan- pero no se apuntan como noticia del día.
    """
    raw = config.raw if hasattr(config, "raw") else config
    cfg = raw.get("hiit", {}) or {}

    # --- motivos de configuración: iguales hoy que dentro de seis meses ----
    if not cfg.get("enabled", False):
        return False, "el HIIT está desactivado en el config", False
    if routine_key in {str(r) for r in (cfg.get("never_routines") or [])}:
        return False, f"{routine_key} nunca lleva HIIT", False
    if routine_key not in {str(r) for r in (cfg.get("allowed_routines") or [])}:
        return False, f"{routine_key} no está en allowed_routines", False

    # --- motivos del día: cambian, y por eso se cuentan --------------------
    if cfg.get("only_on_green", True) and light != "green":
        return False, f"el HIIT solo se añade en verde y hoy es {light}", True

    start = cfg.get("program_start_date") or program_start
    if start is None:
        # Este no es "hoy no toca", es "no se ha podido calcular". Se cuenta
        # siempre: es la misma clase de hueco que un freno sin señal.
        return False, "no hay fecha de inicio del programa con la que contar semanas", True
    if isinstance(start, str):
        start = date.fromisoformat(start)
    weeks = ((day - start).days // 7) + 1
    need = int(cfg.get("start_week", 1))
    if weeks < need:
        return False, f"semana {weeks} del programa; el HIIT empieza en la {need}", True

    return True, f"semana {weeks}, verde y {routine_key} lo admite", True


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


def build_session(
    config: Any,
    day: date,
    light: str,
    progression: ProgressionPlan | None = None,
    active_rules: list[dict[str, Any]] | None = None,
    deload_active: bool = False,
    pending_strength: tuple[str, date] | None = None,
    program_start: date | None = None,
    current_sets: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
) -> BuiltSession:
    """Construye la sesión del día completa."""
    raw = config.raw if hasattr(config, "raw") else config
    actions = raw.get("actions", {}) or {}
    set_cfg = raw.get("set_types", {}) or {}
    prog_cfg = raw.get("progression", {}) or {}
    routines = raw.get("routines", {}) or {}
    action = actions.get(light, {}) or {}

    plan = today_plan(config, day)
    routine_key = plan.get("strength")
    deferred_from: date | None = None

    # Una sesión aplazada por un rojo se recupera en el primer día verde libre.
    # No se apila sobre la del día: se hace EN VEZ DE descansar.
    if routine_key is None and pending_strength and light == "green":
        pkey, pday = pending_strength
        expires = int((actions.get("red", {}) or {}).get("defer_expires_days", 7))
        if (day - pday).days <= expires and not plan.get("bike"):
            routine_key, deferred_from = pkey, pday

    if routine_key is None:
        kind = REST
        for k in ("pool", "bike", "rest"):
            if plan.get(k):
                kind = k
                break
        return BuiltSession(
            day=day, kind=kind, routine_key=None,
            title={"pool": "Piscina", "bike": "Bici", "rest": "Descanso"}.get(kind, "Descanso"),
            write_to_hevy=False,
        )

    # --- día rojo: bloque de recuperación -----------------------------------
    if str(action.get("session", FULL)) == RECOVERY:
        block_key = str(action.get("recovery_block", ""))
        block = (raw.get("recovery_blocks", {}) or {}).get(block_key, {}) or {}
        out = BuiltSession(
            day=day, kind=RECOVERY, routine_key=block_key,
            title=str(block.get("title", "Recuperación")),
            exercises=copy.deepcopy(block.get("exercises") or []),
            write_to_hevy=bool(block.get("write_to_hevy", False)),
        )
        if action.get("defer_strength", True):
            out.notes.append(
                f"la sesión de fuerza ({routine_key}) queda pendiente y se "
                f"recupera en el próximo día verde libre"
            )
        return out

    routine = routines.get(routine_key, {}) or {}
    # La carga vigente sale de la base de datos; del YAML solo salen los
    # ejercicios que aún no han progresado nunca. Misma función que usa
    # `plan_progression`, para que lo anunciado y lo escrito coincidan.
    exercises = con_carga_vigente(
        routine.get("exercises") or [], routine_key, current_sets, set_cfg
    )
    out = BuiltSession(
        day=day,
        kind=str(action.get("session", FULL)),
        routine_key=routine_key,
        title=str(routine.get("title", routine_key)),
        hevy_routine_id=routine.get("hevy_routine_id"),
        deferred_from=deferred_from,
    )
    if deferred_from:
        out.notes.append(f"sesión recuperada del {deferred_from.isoformat()}")

    # 1. retiradas por regla especial
    exercises, log, dropped = apply_rule_removals(exercises, active_rules or [])
    out.changes.extend(log)
    out.dropped.extend(dropped)

    # 2. progresión
    if progression is not None and action.get("allow_progression", False):
        out.changes.extend(apply_progression(exercises, progression, set_cfg))
    elif progression is not None and progression.changes:
        out.notes.append(f"progresión no aplicada: el semáforo está en {light}")

    # 2b. El objetivo vigente se fija AQUÍ, con la progresión ya aplicada y
    # antes de que nadie recorte por el día que sea. Ver `BuiltSession.target_sets`.
    out.target_sets = {
        str(ex.get("key")): copy.deepcopy(series_efectivas_vigentes(ex, set_cfg))
        for ex in exercises
        if ex.get("key")
    }

    # 3. descarga
    if deload_active:
        dl_factor = _deload_load_factor(raw)
        out.changes.extend(apply_load_factor(exercises, dl_factor))
        exercises = [apply_deload_volume(ex, prog_cfg, set_cfg) for ex in exercises]
        out.changes.append(
            f"semana de descarga: carga al {int(dl_factor * 100)}% y volumen recortado"
        )

    # 4. recortes de carga por regla especial: lo ÚLTIMO que toca la carga
    out.changes.extend(apply_rule_load_cuts(exercises, active_rules or []))

    # 5. ámbar
    if str(action.get("session", FULL)) == REDUCED:
        exercises, log, dropped = apply_amber_reduction(exercises, action, set_cfg)
        out.changes.extend(log)
        out.dropped.extend(dropped)

    out.exercises = exercises

    # 6. HIIT
    ok, why, es_de_hoy = hiit_applies(config, routine_key, light, day, program_start)
    permitido = bool(action.get("allow_hiit", False))

    if not ok:
        # Solo se apunta si el motivo es del día. Los permanentes -HIIT
        # apagado, rutina que nunca lo lleva- son configuración, no noticia,
        # y repetidos a diario enseñan a no leer la sección entera.
        if es_de_hoy:
            out.notes.append(f"sin HIIT: {why}")
    elif not permitido:
        # Tocaba HIIT y una regla especial lo ha quitado. Antes esta rama caía
        # en el `else` de abajo y apuntaba `why`, que en este caso dice
        # "semana 5, verde y dia_1 lo admite": el motivo de que SÍ tocara,
        # presentado como el motivo de que no. Justo al revés.
        out.notes.append("sin HIIT: una regla especial lo ha desactivado hoy")
    else:
        block_key = str(((raw.get("hiit", {}) or {}).get("blocks") or {}).get(routine_key, ""))
        block = routines.get(block_key, {}) or {}
        if block:
            out.hiit_block = block_key
            out.exercises = out.exercises + copy.deepcopy(block.get("exercises") or [])
            out.changes.append(f"añadido bloque HIIT ({block.get('title', block_key)}): {why}")
        else:
            # Este era el peor de los tres: todo decía que tocaba HIIT, el
            # bloque no aparecía en `routines`, y la sesión salía sin él sin
            # una sola línea en ninguna parte. Una errata en `hiit.blocks`
            # borraba el HIIT del programa en silencio.
            out.notes.append(
                f"sin HIIT: tocaba, pero el bloque '{block_key}' de "
                f"hiit.blocks.{routine_key} no existe en `routines`"
            )

    return out


def _deload_load_factor(raw: dict[str, Any]) -> float:
    for rule in raw.get("special_rules", []) or []:
        if rule.get("name") == "semana_de_descarga":
            return float((rule.get("action") or {}).get("load_factor", 0.6))
    return 0.6
