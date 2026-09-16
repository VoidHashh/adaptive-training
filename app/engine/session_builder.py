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
from app.engine.rules import RuleError
from app.engine.sets import excludes, warmup_flags

FULL = "full"
REDUCED = "reduced"
RECOVERY = "recovery"
# Aquí estaba `REST = "rest"`, y con él las clases `pool` y `bike`. Eran los
# días que el calendario fijo dejaba sin fuerza. Sin calendario no hay días sin
# fuerza asignada: todos los días tienen la rutina que toque en el ciclo, y si
# se entrena o no lo dice el usuario yendo o no yendo. La piscina y la bici
# nunca fueron una sesión que este sistema construyera -no escribía nada en
# Hevy ni progresaba nada-, solo una etiqueta para el mensaje.


def _tipo_de_sesion(action: dict[str, Any], light: str) -> str:
    """Qué sesión toca con esta luz, SIN defecto.

    Aquí había un `str(action.get("session", FULL))`, repetido en tres sitios, y
    ese defecto era el valor más permisivo de los tres posibles. Con `actions`
    sin whitelist -que era el caso-, escribir `sesion:` en vez de `session:`
    bajo `actions.red` no daba ningún error y construía la sesión COMPLETA un
    día rojo, con progresión, y la escribía en Hevy. El fichero decía
    "recovery"; el gimnasio recibía "full".

    De los fallos que ha tenido este proyecto, es el único que SUELTA en vez de
    frenar. Todos los demás -el presupuesto agotado, el freno del lunes, la
    racha en cero- pecaban de prudentes: recortaban de más. Este abre la sesión
    entera el día que el cuerpo ha dicho que pares, y con una hernia L4-L5 eso
    no es un número mal calculado, es una lesión.

    El validador ya exige que la clave exista y que valga uno de los tres. Esto
    es la segunda cerradura, para la ruta que algún día entre sin pasar por él:
    si falta, revienta con el nombre de la luz puesto. Un `KeyError` a las 06:30
    es un mal día; una sesión completa en rojo es un mes fuera.
    """
    sesion = action.get("session")
    if sesion is None:
        raise RuleError(
            f"actions.{light}.session no está en el config. No hay valor por "
            f"defecto a propósito: el que había era 'full', el más permisivo de "
            f"los tres, y en rojo eso significa la sesión entera el día que "
            f"tocaba recuperación."
        )
    sesion = str(sesion)
    if sesion not in (FULL, REDUCED, RECOVERY):
        raise RuleError(
            f"actions.{light}.session vale {sesion!r}, que no es "
            f"{FULL}, {REDUCED} ni {RECOVERY}. Antes un valor desconocido caía "
            f"en la rama de sesión completa sin decir nada."
        )
    return sesion


# Aquí vivía `caducidad_del_aplazamiento`, que leía del YAML cuántos días
# sobrevivía una sesión de fuerza aplazada por un día rojo. Ya no hay
# aplazamiento que caducar: la rotación sale de lo EJECUTADO en Hevy (ver
# `rotation` en el config), un día rojo no ejecuta ninguna rutina del ciclo, el
# puntero no se mueve y mañana vuelve a tocar exactamente la misma sesión. La
# rotación se aplaza sola, sin plazo y sin nada que se pueda perder al vencer.


@dataclass
class BuiltSession:
    """La sesión de hoy, lista para escribirse en Hevy y contarse por Telegram."""

    day: date
    kind: str  # full | reduced | recovery
    routine_key: str | None
    title: str
    exercises: list[dict[str, Any]] = field(default_factory=list)
    hevy_routine_id: str | None = None
    write_to_hevy: bool = True
    hiit_block: str | None = None
    # EL BLOQUE HIIT DEL DÍA, COMO SESIÓN APARTE Y NO PEGADO A LA DE FUERZA.
    #
    # Aquí no había nada: los ejercicios del bloque se concatenaban a
    # `exercises` y `hiit_block` guardaba solo la clave. Con eso, todo lo que
    # mira la sesión de un día veía UNA rutina con la prensa y el wall ball
    # dentro, y de ahí salían tres averías que no se parecen entre sí:
    #
    #   - las cargas del HIIT se sembraban bajo `dia_1`, que no declara esas
    #     claves, así que quedaban filas huérfanas que nadie volvía a leer y
    #     `plancha_frontal` -el único ejercicio del bloque que progresa- no
    #     progresaba nunca;
    #   - el cumplimiento era uno solo: un wall ball que no se hizo cerraba la
    #     puerta de la progresión de la prensa horizontal, que no tiene nada
    #     que ver;
    #   - y en Hevy solo se escribía un destino, el de fuerza, mientras el
    #     bloque vivía en su propia rutina (`hiit.blocks` las nombra) que se
    #     quedaba con lo que hubiera de la última vez.
    #
    # Es un `BuiltSession` y no una lista de ejercicios porque eso es lo que
    # es: tiene su clave de rutina (`hiit_dia_1`), su título, su
    # `hevy_routine_id` y su carga vigente. Serlo de verdad hace que
    # `build_routine_payload`, `apply_execution` y `adoptar_cargas` le sirvan
    # sin una sola rama nueva.
    hiit: "BuiltSession | None" = None
    # Todo lo que se le ha hecho a la rutina base, en orden, con su motivo.
    changes: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
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
            "exercises": self.exercises,
            # El plan del HIIT se guarda ANIDADO y con su propia clave de
            # rutina. `run_reconcile` lo lee de aquí para medir el bloque
            # contra lo que el bloque pedía, que es la mitad entera de la
            # separación: sin esto, la noche solo tendría la lista de fuerza y
            # volvería a no saber qué se había prescrito de HIIT.
            "hiit": self.hiit.to_dict() if self.hiit is not None else None,
        }

    def total_effective_sets(self, set_cfg: dict[str, Any]) -> int:
        n = 0
        for ex in self.exercises:
            flags = warmup_flags(ex.get("sets") or [], set_cfg, ex.get("key"))
            n += sum(1 for f in flags if not f)
        return n


# ---------------------------------------------------------------------------
# Rotación
# ---------------------------------------------------------------------------


def orden_de_rotacion(config: Any) -> list[str]:
    """El ciclo de fuerza tal y como lo declara el config, SIN defecto.

    Aquí había un `today_plan(config, day)` que miraba qué tocaba hoy según el
    día de la semana y la variante de calendario en curso. No hay días
    asignados: hay un ciclo, y lo que toca depende de la última sesión
    ejecutada, no de la fecha.

    Se revienta con la lista vacía en vez de devolver `[]` y seguir. Sin ciclo
    no hay ninguna rutina que escribir, y el efecto de seguir sería una mañana
    sin sesión, que es indistinguible de un día de descanso: el fallo mudo de
    siempre. El validador ya exige que `rotation.order` esté y traiga rutinas de
    verdad; esto es la segunda cerradura para la ruta que entre sin pasar por él.
    """
    raw = config.raw if hasattr(config, "raw") else config
    orden = list(((raw.get("rotation") or {}).get("order") or []))
    if not orden:
        raise RuleError(
            "rotation.order está vacío o no está en el config. Es el ciclo de "
            "fuerza entero: sin él no hay ninguna rutina que programar y la "
            "mañana saldría sin sesión, que por fuera se ve igual que un día de "
            "descanso."
        )
    return orden


def siguiente_en_rotacion(config: Any, ultima: str | None) -> str:
    """Qué rutina toca después de `ultima`, dando la vuelta al final del ciclo.

    `ultima` es la clave de la última sesión de fuerza que aparece EJECUTADA en
    Hevy (ver `repository.load_state`), no la última que se planificó. La
    diferencia es todo el diseño: con lo planificado, un día que se decide y no
    se entrena adelantaría el puntero igual, y a los tres días el sistema
    estaría escribiendo el Día 3 cuando la última sesión real fue el Día 1. Con
    lo ejecutado, un día sin entrenar sencillamente no mueve nada.

    Dos casos devuelven el primero del ciclo:

    - No hay ninguna sesión ejecutada todavía (programa recién arrancado).
    - La última ejecutada ya no está en `rotation.order`, porque se ha sacado
      del ciclo editando el config. Se empieza de nuevo en vez de fallar: el
      fichero es el que manda y una rutina retirada no tiene un «siguiente».
    """
    orden = orden_de_rotacion(config)
    if ultima is None or ultima not in orden:
        return orden[0]
    return orden[(orden.index(ultima) + 1) % len(orden)]


# ---------------------------------------------------------------------------
# Transformaciones
# ---------------------------------------------------------------------------


def _split(ex: dict[str, Any], set_cfg: dict[str, Any]):
    sets = ex.get("sets") or []
    flags = warmup_flags(sets, set_cfg, ex.get("key"))
    warm = [s for s, f in zip(sets, flags, strict=True) if f]
    work = [s for s, f in zip(sets, flags, strict=True) if not f]
    return warm, work


def _sumar(
    work: list[dict[str, Any]],
    campo: str,
    delta: int,
    modo: str,
    tope: int | None,
) -> None:
    """Suma `delta` a las series efectivas, sin bajar ninguna nunca.

    Es el equivalente para reps y segundos de lo que la carga ya hacía con
    `weight_delta_kg` + `apply_to`. Antes esto era un valor absoluto escrito en
    todas las series, y un 12/10/10 al que le tocaba subir salía 11/11/11: la
    rampa aplanada y el top set con una repetición MENOS que antes de subir.

    `lowest_first` sube UNA serie, la más baja de todas, y si hay empate la
    primera de ellas: 12/10/10 -> 12/11/10 -> 12/11/11 -> 12/12/11 -> 12/12/12.
    Se llena desde abajo hasta emparejar, que es la progresión escalonada de
    toda la vida y la que el YAML pedía.

    `all_sets` sube todas a la vez, cada una topada por su cuenta.
    """
    con_dato = [s for s in work if s.get(campo) is not None]
    if not con_dato or delta <= 0:
        return

    def techo(valor: int) -> int:
        return min(valor + delta, tope) if tope is not None else valor + delta

    if modo == "all_sets":
        for s in con_dato:
            # `max` y no `techo` a secas: una serie que ya esté POR ENCIMA del
            # tope se queda como está en vez de recortarse. El tope existe para
            # frenar la subida, no para nivelar hacia abajo lo que ya se hace.
            s[campo] = max(int(s[campo]), techo(int(s[campo])))
        return

    objetivo = min(int(s[campo]) for s in con_dato)
    for s in con_dato:
        if int(s[campo]) == objetivo:
            s[campo] = max(objetivo, techo(objetivo))
            return


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
            # Absoluto, y solo se usa donde serlo es correcto: la vuelta al
            # mínimo del rango cuando la doble progresión sube carga. Es un
            # recorte declarado, no una subida.
            for s in work:
                if s.get("reps") is not None:
                    s["reps"] = e.new_reps
        elif e.rep_delta:
            _sumar(work, "reps", e.rep_delta, e.rep_apply_to, e.rep_cap)
        if e.new_duration_s is not None:
            for s in work:
                if s.get("duration_s") is not None:
                    s["duration_s"] = e.new_duration_s
        elif e.duration_delta_s:
            _sumar(work, "duration_s", e.duration_delta_s, "all_sets", e.duration_cap)
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

    UNA REGLA QUE DISPARA Y NO RECORTA NADA ES UN ERROR
    ---------------------------------------------------
    Que el ejercicio no esté en la rutina de HOY es normal: una regla sobre el
    peso muerto no hace nada un día de empuje, y ahí callar es lo correcto.

    Lo que no puede pasar en silencio es que el ejercicio SÍ esté en la sesión y
    aun así no se recorte nada, que ocurre cuando ninguna de sus series tiene
    peso: un ejercicio a 0 kg -porque todavía no se ha registrado la carga en
    Hevy- multiplicado por 0,7 sigue siendo 0. El recorte no se aplica, no se
    apunta en `changes`... y el mensaje de la mañana SIGUE anunciando la regla,
    porque `message.py` lista las reglas activas por nombre sin mirar si han
    hecho algo. El resultado es leer "descarga lumbar activa" y entrenar sin
    ninguna descarga. Con una hernia L4-L5 esa diferencia se paga con la
    espalda, así que aquí se para.
    """
    log: list[str] = []
    presentes = {str(e.get("key")) for e in exercises}

    for rule in active_rules:
        rl = (rule.get("action", {}) or {}).get("reduce_load") or {}
        if not rl:
            continue
        objetivos = [str(k) for k in (rl.get("exercises") or [])]
        entries = apply_load_factor(
            exercises, float(rl.get("factor", 1.0)), objetivos or None
        )
        for entry in entries:
            log.append(f"'{rule.get('name')}': {entry}")

        # Si `exercises` está vacío no hay sesión que recortar (día rojo, bloque
        # de recuperación) y no hay nada que denunciar.
        if not presentes:
            continue

        pct = int(float(rl.get("factor", 1.0)) * 100)
        con_peso = {
            str(e.get("key"))
            for e in exercises
            if any(s.get("weight_kg") for s in e.get("sets") or [])
        }

        if objetivos:
            # La regla NOMBRA ejercicios: nombrarlos es afirmar algo sobre cada
            # uno. Los que estén hoy en la sesión tienen que recortarse todos.
            mudos = sorted((set(objetivos) & presentes) - con_peso)
            if mudos:
                raise RuleError(
                    f"la regla '{rule.get('name')}' recorta la carga al {pct}% "
                    f"de {mudos}, esos ejercicios SÍ están en la sesión de hoy y "
                    f"aun así no se ha recortado nada: ninguna de sus series "
                    f"tiene peso. El mensaje anunciaría la regla como activa y se "
                    f"entrenaría sin recorte. Registra la carga de esos "
                    f"ejercicios en config.yaml o quítalos de la regla."
                )
        elif not con_peso:
            # Recorte para toda la sesión. Que no toque los ejercicios de peso
            # corporal es normal -una plancha no se multiplica por 0,7-, así que
            # solo es un error si no ha recortado absolutamente nada.
            raise RuleError(
                f"la regla '{rule.get('name')}' recorta la carga de toda la "
                f"sesión al {pct}% y no ha recortado NADA: ningún ejercicio de "
                f"hoy tiene peso registrado. La regla se anunciaría como activa "
                f"sin haber cambiado una sola serie."
            )
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


def clave_del_bloque_hiit(raw: dict[str, Any], routine_key: str) -> str:
    """El bloque HIIT que le toca a `routine_key`, o `""` si no lleva.

    Existe como función porque ahora hay DOS sitios que necesitan la respuesta:
    aquí abajo, para construir el bloque, y `decision.decide`, para planificar
    su progresión. Dos lecturas a mano de `hiit.blocks` son dos sitios que
    pueden acabar apuntando a bloques distintos sin que nada los compare, y el
    resultado sería una progresión calculada contra el estado de un bloque y
    aplicada a otro: números plausibles y mal.
    """
    return str(((raw.get("hiit", {}) or {}).get("blocks") or {}).get(routine_key, ""))


def build_session(
    config: Any,
    day: date,
    light: str,
    *,
    rotation_routine: str,
    progression: ProgressionPlan | None = None,
    progression_hiit: ProgressionPlan | None = None,
    active_rules: list[dict[str, Any]] | None = None,
    deload_active: bool = False,
    program_start: date | None = None,
    current_sets: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
) -> BuiltSession:
    """Construye la sesión del día completa.

    `rotation_routine` es la rutina que toca según el ciclo, y viene dada: la
    calcula `decision.decide` con `siguiente_en_rotacion` y la guarda además en
    la decisión del día. No se recalcula aquí a propósito. Antes esto leía el
    calendario por su cuenta y `decision` leía el suyo, y dos lectores del mismo
    dato son dos sitios que pueden acabar diciendo cosas distintas sin que nada
    los compare. Es obligatorio y sin defecto por lo de siempre: el defecto
    habría sido «hoy no toca fuerza», que no da error, da una mañana en blanco.
    """
    raw = config.raw if hasattr(config, "raw") else config
    actions = raw.get("actions", {}) or {}
    set_cfg = raw.get("set_types", {}) or {}
    prog_cfg = raw.get("progression", {}) or {}
    routines = raw.get("routines", {}) or {}
    action = actions.get(light, {}) or {}

    routine_key = rotation_routine
    if routine_key not in routines:
        raise RuleError(
            f"la rotación señala la rutina '{routine_key}', que no está en "
            f"`routines` (hay: {sorted(routines)}). Antes de la rotación, una "
            f"rutina que no existía salía como día de descanso."
        )

    # Aquí había un bloque que recuperaba una sesión aplazada por un rojo en el
    # primer día verde libre, y otro que convertía el día en descanso, piscina o
    # bici cuando el calendario no ponía fuerza. Ya no hay ni una cosa ni otra:
    # todos los días tienen una rutina del ciclo -la que toque- y lo que el
    # mensaje dice no es «hoy entrenas», es qué tocaría SI se va al gimnasio.
    # Quién decide si se va es el usuario, y el sistema se entera al leer Hevy.

    sesion = _tipo_de_sesion(action, light)

    # --- día rojo: bloque de recuperación -----------------------------------
    if sesion == RECOVERY:
        block_key = str(action.get("recovery_block", ""))
        bloques = raw.get("recovery_blocks", {}) or {}
        # Un nombre que no existe daba `{}`, y de `{}` salía una sesión de
        # recuperación con título, cero ejercicios y cara de estar bien. El día
        # rojo es justo el día en que el mensaje tiene que decir qué hacer, así
        # que se para. Lo valida también el `config_loader` al arrancar; esto es
        # la cerradura de dentro.
        if block_key not in bloques:
            raise RuleError(
                f"actions.{light}.recovery_block '{block_key}' no está en "
                f"recovery_blocks (hay: {sorted(bloques)}). Antes esto devolvía "
                f"un bloque vacío y el día rojo llegaba a Telegram sin un solo "
                f"ejercicio, sin que nada dijera que faltaba."
            )
        block = bloques.get(block_key) or {}
        out = BuiltSession(
            day=day, kind=RECOVERY, routine_key=block_key,
            title=str(block.get("title", "Recuperación")),
            exercises=copy.deepcopy(block.get("exercises") or []),
            write_to_hevy=bool(block.get("write_to_hevy", False)),
        )
        # La nota ya no promete recuperar nada, porque no hay nada que
        # recuperar: el bloque de recuperación no es ninguna rutina del ciclo,
        # así que no mueve el puntero y mañana vuelve a tocar la misma sesión.
        # Antes esto dependía de `actions.red.defer_strength` y podía quedarse
        # callado con la clave a false, que era la peor combinación: la sesión
        # se guardaba igual y nadie lo decía.
        out.notes.append(
            f"la rotación no se mueve: el próximo día que vayas al gimnasio "
            f"sigue tocando {routine_key}"
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
        kind=sesion,
        routine_key=routine_key,
        title=str(routine.get("title", routine_key)),
        hevy_routine_id=routine.get("hevy_routine_id"),
    )

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
    if sesion == REDUCED:
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
        block_key = clave_del_bloque_hiit(raw, routine_key)
        block = routines.get(block_key, {}) or {}
        if block:
            out.hiit_block = block_key
            out.hiit = _sesion_hiit(
                block_key,
                block,
                day,
                current_sets,
                set_cfg,
                progression=progression_hiit,
                permitida=bool(action.get("allow_progression", False)),
            )
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


def _sesion_hiit(
    block_key: str,
    block: dict[str, Any],
    day: date,
    current_sets: dict[tuple[str, str], list[dict[str, Any]]] | None,
    set_cfg: dict[str, Any],
    *,
    progression: ProgressionPlan | None = None,
    permitida: bool = False,
) -> BuiltSession:
    """El bloque HIIT de hoy como sesión propia, bajo SU clave de rutina.

    Lo que hace y lo que NO hace, que aquí es lo interesante.

    HACE pasar el bloque por `con_carga_vigente` y fijar su `target_sets`, igual
    que la rutina de fuerza. Sin esto la carga del bloque salía del YAML cada
    día, literal, y `plancha_frontal` -que es `progression_type: volume` con
    `max_seconds: 60`, el único del bloque que progresa- volvía a sus segundos
    de fábrica cada mañana. La progresión se adoptaba en la base y no llegaba
    nunca a la app.

    Y HACE aplicar la progresión del bloque, que es un plan APARTE del de la
    fuerza y llega en `progression`. Esto llevaba parado desde el primer día y
    no por una decisión: `apply_progression` corre en el paso 2 de
    `build_session` y el bloque se añade en el 6, así que pasaba de largo. El
    único ejercicio del bloque que progresa -`plancha_frontal`, por volumen,
    con techo de 60 s- nunca sumó un segundo. Ni siquiera era arreglable antes
    de separar el HIIT de la fuerza: su racha se guardaba bajo la clave de la
    rutina equivocada, así que el plan se habría construido sobre un estado que
    no era el suyo.

    Y el par de la clave es `(hiit_dia_1, plancha_frontal)`, que es donde el
    `config.yaml` declara ese ejercicio. Bajo `dia_1` -que es donde iba- la fila
    no la lee nadie, porque `dia_1` no tiene esa clave.

    NO HACE nada de lo que el semáforo le hace a la fuerza: ni recortes de
    ámbar, ni descarga, ni retiradas por regla especial. No es una omisión, es
    que no puede llegar aquí un día en que toque: el bloque solo se añade en
    verde (`hiit.only_on_green`) y con `allow_hiit`, y en verde no hay recorte
    de ámbar ni regla especial que quitar. El día que eso cambie, este
    comentario es lo que hay que releer: aplicarle a un bloque HIIT un -25 % de
    series no es lo mismo que aplicárselo a una rutina de fuerza.

    `permitida` es el mismo `actions[light].allow_progression` que gobierna la
    fuerza, y se pasa en vez de darlo por hecho por la misma razón: hoy el
    bloque solo entra en verde y en verde la progresión está permitida, pero
    atarlo a esa coincidencia significa que el día que el HIIT entre en ámbar
    subiría volumen mientras la fuerza está recortada, y nadie se enteraría.
    """
    ejercicios = con_carga_vigente(
        block.get("exercises") or [], block_key, current_sets, set_cfg
    )
    cambios: list[str] = []
    notas: list[str] = []
    if progression is not None and permitida:
        cambios = apply_progression(ejercicios, progression, set_cfg)
    elif progression is not None and progression.changes:
        notas.append("progresión del HIIT no aplicada: el semáforo no la permite hoy")

    hiit = BuiltSession(
        day=day,
        # `full` y no un cuarto valor: los tres que hay describen cuánto se
        # recorta la sesión respecto de lo prescrito, y el bloque va entero o
        # no va. Un `kind: "hiit"` aquí obligaría a todos los `if kind ==` del
        # sistema a crecer una rama para decir lo mismo. Lo que este bloque ES
        # se sabe por su `routine_key`, que `claves_hiit` reconoce.
        kind=FULL,
        routine_key=block_key,
        title=str(block.get("title", block_key)),
        exercises=ejercicios,
        hevy_routine_id=block.get("hevy_routine_id"),
        write_to_hevy=bool(block.get("write_to_hevy", True)),
    )
    hiit.changes.extend(cambios)
    hiit.notes.extend(notas)
    # DESPUÉS de la progresión, igual que el paso 2b de `build_session`: el
    # objetivo vigente es lo que se acaba de prescribir, no lo que había ayer.
    # Al revés, la plancha subiría a 35 s en la app y la base seguiría diciendo
    # 30 s, que es la forma de que mañana vuelva a subir "por primera vez".
    hiit.target_sets = {
        str(ex.get("key")): copy.deepcopy(series_efectivas_vigentes(ex, set_cfg))
        for ex in ejercicios
        if ex.get("key")
    }
    return hiit


def _deload_load_factor(raw: dict[str, Any]) -> float:
    for rule in raw.get("special_rules", []) or []:
        if rule.get("name") == "semana_de_descarga":
            return float((rule.get("action") or {}).get("load_factor", 0.6))
    return 0.6
