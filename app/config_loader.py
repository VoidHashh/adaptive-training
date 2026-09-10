"""Carga, validación y hash de config.yaml.

Tres responsabilidades:

1. Cargar el YAML.
2. Validar las referencias cruzadas. El YAML es grande y está lleno de claves
   que apuntan a otras claves (el calendario nombra rutinas, las reglas
   especiales nombran ejercicios, el HIIT nombra un bloque). Una errata ahí
   no rompe nada al arrancar pero produce una decisión silenciosamente mal
   una mañana cualquiera. Preferimos petar al cargar.
3. Calcular un hash estable del contenido. Se guarda con cada decisión, para
   que dentro de cuatro semanas se pueda saber con qué umbrales se decidió
   cada día, aunque el YAML haya cambiado por medio.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from app.engine.rules import COMPARISONS


def _strip_accents(text: str) -> str:
    """Quita acentos para que los patrones funcionen con nombres en español."""
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )

WEEKDAYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]

# Tipos de serie que acepta Hevy en el campo `type`.
VALID_SET_TYPES = {"normal", "warmup", "failure", "dropset"}
VALID_SET_SOURCES = {"api", "heuristic", "api_then_heuristic"}
VALID_PROGRESSION_TYPES = {"load", "double", "volume", "sets", "none"}


class ConfigError(ValueError):
    """config.yaml es inválido. El mensaje lista TODOS los problemas."""


class Config:
    """Envoltorio de solo lectura sobre el YAML, con el hash calculado."""

    def __init__(self, data: dict[str, Any], config_hash: str, source: Path | None = None):
        self._data = data
        self.hash = config_hash
        self.source = source

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    @property
    def raw(self) -> dict[str, Any]:
        return self._data

    # --- accesos con nombre, para no repetir literales por el código --------
    @property
    def timezone(self) -> str:
        return self._data.get("timezone", "UTC")

    @property
    def program_start(self) -> date:
        """Origen del contador de descargas. El validador garantiza que existe."""
        value = (self._data.get("program") or {}).get("start")
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return datetime.strptime(str(value), "%Y-%m-%d").date()

    @property
    def routines(self) -> dict[str, Any]:
        return self._data.get("routines", {})

    @property
    def cycling(self) -> dict[str, Any]:
        return self._data.get("cycling", {})

    @property
    def set_types(self) -> dict[str, Any]:
        """Sección `set_types`. Si falta, valores por defecto conservadores:
        sin heurística, así nunca se descarta una serie por sorpresa."""
        return self._data.get("set_types") or {
            "source": "api",
            "heuristic": {"enabled": False},
        }

    @property
    def thresholds(self) -> dict[str, Any]:
        return self._data.get("thresholds", {})

    def active_calendar(self) -> dict[str, Any]:
        cal = self._data["calendar"]
        return cal["variants"][cal["active_variant"]]

    def slider_keys(self) -> list[str]:
        return [s["key"] for s in self._data.get("checkin_sliders", [])]

    def all_rules(self) -> list[dict[str, Any]]:
        out = []
        for light in ("red", "amber"):
            for rule in self.thresholds.get(light, []):
                out.append({**rule, "light": light})
        return out


def compute_hash(data: dict[str, Any]) -> str:
    """Hash estable del contenido, insensible al orden de las claves.

    Se hashea el JSON canónico y no los bytes del archivo, para que un cambio
    de comentarios o de indentación no invente una recalibración que no existe.
    """
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _validate(data: dict[str, Any]) -> list[str]:
    """Devuelve la lista de problemas encontrados. Vacía = configuración válida."""
    errors: list[str] = []

    def require(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    # --- secciones obligatorias --------------------------------------------
    for section in (
        "timezone",
        "program",
        "checkin_sliders",
        "thresholds",
        "actions",
        "progression",
        "calendar",
        "routines",
        "cycling",
        "hiit",
    ):
        require(section in data, f"falta la sección obligatoria '{section}'")
    if errors:
        return errors  # sin las secciones base no tiene sentido seguir

    # --- secciones conocidas ------------------------------------------------
    # El mismo criterio que ya se aplica DENTRO de `integrations`, `brakes` o
    # `volume_safety`, aplicado por fin al nivel de arriba, que era el único
    # sitio donde no se miraba. Escribir `special_rule:` en vez de
    # `special_rules:` daba un arranque limpio y cero reglas especiales: el
    # sistema decidía todos los días sin mirar ninguna, y no había forma de
    # notarlo salvo echar de menos un ámbar que nunca llegaba.
    #
    # Una sección entera ignorada en silencio es el fallo más caro de todos,
    # porque una sección es justo donde se escribe lo que no es el defecto.
    #
    # NO ESTÁN EN LA LISTA, A PROPÓSITO
    # ---------------------------------
    # `rules`: nunca la ha leído nadie. Tenía bloque de validación propio
    # (comprobaba los operadores de sus `when`), así que escribirla en vez de
    # `special_rules` pasaba la validación con nota y no hacía absolutamente
    # nada. Un validador que revisa a conciencia una sección muerta es peor
    # que uno que no la mira: certifica por escrito que está bien puesta.
    #
    # `cold_start`: ver el error de más abajo.
    SECCIONES = {
        "version",
        "timezone",
        "program",
        "baseline",
        "adaptive_thresholds",
        "checkin_sliders",
        "checkin_comment",
        "thresholds",
        "actions",
        "safety",
        "progression",
        "calendar",
        "schedule",
        "routines",
        "set_types",
        "special_rules",
        "recovery_blocks",
        "cycling",
        "hiit",
        "integrations",
        "notifications",
    }
    for k in data:
        require(
            k in SECCIONES,
            f"sección desconocida '{k}' en la raíz de config.yaml. Válidas: "
            f"{', '.join(sorted(SECCIONES))}. Si es una errata, TODO lo que "
            f"hay dentro se estaría ignorando en silencio",
        )

    # `version` estaba escrito y no lo miraba nadie. O sirve para algo o sobra:
    # dejarlo decorativo es prometer que el cargador sabe leer formatos viejos.
    # Sabe leer uno, y ahora lo dice.
    version = data.get("version", 1)
    require(
        version == 1,
        f"version: {version!r} no la entiende este cargador, que solo lee la 1. "
        f"Si vienes de un config.yaml más nuevo, actualiza el código antes de "
        f"arrancar: leerlo con las claves de la 1 sería inventarse la mitad",
    )

    # `cold_start` describía un arranque en frío que nunca se programó, y las
    # tres claves prometen cosas que hoy se resuelven en otro sitio:
    #
    #   backfill_days       -> no hay relleno hacia atrás; la ventana de
    #                          bienestar que se pide a Garmin es
    #                          `baseline.window_days`.
    #   on_insufficient_data-> lo cubren `baseline.min_days_required` y el
    #                          `min_days_required` de cada umbral adaptativo,
    #                          que además degradan señal a señal en vez de
    #                          poner el día entero en un modo global.
    #   notify              -> ya se hace SIEMPRE y no es opcional:
    #                          `message.py` vuelca `signals.notes` fuera de
    #                          `include_reasoning`, así que un día decidido con
    #                          datos incompletos lo dice aunque el razonamiento
    #                          esté apagado.
    #
    # Se rechaza en vez de ignorarse porque `notify: true` se lee como una
    # garantía de aviso, y una garantía que nadie cumple es exactamente lo que
    # no puede quedar escrito en este fichero.
    require(
        "cold_start" not in data,
        "cold_start ya no se usa y no lo leía nadie: 'backfill_days' se llama "
        "ahora baseline.window_days, 'on_insufficient_data' lo cubren los "
        "min_days_required de baseline y de cada adaptive_thresholds, y "
        "'notify' es incondicional (los días con datos incompletos se avisan "
        "siempre en el mensaje). Bórralo.",
    )

    # --- origen del programa ------------------------------------------------
    # Se valida aquí arriba y con dureza. `program.start` es el origen desde el
    # que se cuentan las semanas de descarga: sin él la descarga no se activa
    # nunca y el sistema progresa indefinidamente sin descargar. Eso es un fallo
    # silencioso con consecuencias físicas, así que no se avisa: se impide
    # arrancar.
    prog_start = (data.get("program") or {}).get("start")
    if prog_start is None:
        errors.append(
            "program.start está vacío o no existe. Es el origen desde el que se "
            "cuentan las semanas de descarga (special_rules.semana_de_descarga). "
            "Sin él la descarga NUNCA se activa y el programa progresa sin "
            "descargar nunca. Pon una fecha AAAA-MM-DD."
        )
    elif isinstance(prog_start, datetime):
        errors.append(
            f"program.start '{prog_start}' lleva hora. Debe ser una fecha "
            "AAAA-MM-DD sin hora: la descarga se cuenta por semanas."
        )
    elif not isinstance(prog_start, date):
        # PyYAML ya convierte 2026-09-08 a `date`. Si llega como texto es que
        # estaba entrecomillado o mal escrito.
        try:
            datetime.strptime(str(prog_start), "%Y-%m-%d")
        except ValueError:
            errors.append(
                f"program.start '{prog_start}' no es una fecha AAAA-MM-DD válida"
            )
        else:
            errors.append(
                f"program.start '{prog_start}' está entre comillas. Quítalas para "
                "que YAML lo lea como fecha y no como texto."
            )

    routines = data["routines"]
    slider_keys = {s["key"] for s in data["checkin_sliders"]}

    # --- sliders ------------------------------------------------------------
    require(
        len(slider_keys) == len(data["checkin_sliders"]),
        "hay claves duplicadas en checkin_sliders",
    )

    # --- calendario ---------------------------------------------------------
    cal = data["calendar"]
    variant = cal.get("active_variant")
    require(
        variant in cal.get("variants", {}),
        f"calendar.active_variant '{variant}' no existe en calendar.variants",
    )
    # Aquí un descuido no se nota NUNCA, y por eso se mira tan de cerca. El
    # constructor de sesiones lee `strength` y, si no hay, cae a `rest` por
    # defecto (`session_builder.build_session`). O sea que un `strenght: dia_1`
    # mal escrito, o un lunes que se quedó sin escribir, no dan error ni salen en
    # ningún log: el lunes pasa a ser descanso, el mensaje de las nueve anuncia
    # descanso con toda la seguridad del mundo, y el día de fuerza desaparece del
    # programa sin que nadie pueda relacionarlo con el archivo.
    DIA_CLAVES = {"strength", "rest", "pool", "bike"}
    BANDERAS = ("rest", "pool", "bike")
    for vname, vdata in cal.get("variants", {}).items():
        for day, plan in vdata.items():
            if day == "description":
                continue
            require(day in WEEKDAYS, f"calendar.variants.{vname}: '{day}' no es un día válido")
            plan = plan or {}
            sobran = set(plan) - DIA_CLAVES
            require(
                not sobran,
                f"calendar.variants.{vname}.{day}: {sorted(sobran)} no se lee(n). "
                f"Solo existen {sorted(DIA_CLAVES)}. Un día con una clave mal "
                f"escrita se convierte en descanso sin avisar.",
            )

            key = plan.get("strength")
            if key is not None:
                require(
                    key in routines,
                    f"calendar.variants.{vname}.{day}: la rutina '{key}' no existe",
                )

            # `pool: false` y no escribir `pool` acaban en el mismo sitio, así
            # que la primera forma solo sirve para hacer creer que dice algo.
            for bandera in BANDERAS:
                if bandera in plan:
                    require(
                        plan[bandera] is True,
                        f"calendar.variants.{vname}.{day}.{bandera} vale "
                        f"{plan[bandera]!r}. Solo se entiende `true`: quitar la "
                        f"clave y ponerla a false son lo mismo para el código.",
                    )

            # Un día es una cosa y solo una. `{strength: dia_1, pool: true}` es
            # media verdad: gana la fuerza y la piscina no llega a leerse.
            puestas = [b for b in BANDERAS if plan.get(b)] + (
                ["strength"] if key is not None else []
            )
            require(
                len(puestas) == 1,
                f"calendar.variants.{vname}.{day} declara {sorted(puestas)} y "
                f"tiene que declarar exactamente una cosa. "
                + (
                    "Un día vacío se lee como descanso, pero entonces conviene "
                    "escribir `rest: true` y que se vea."
                    if not puestas
                    else "Solo se aplica una y las demás se descartan en silencio."
                ),
            )

        faltan = set(WEEKDAYS) - set(vdata)
        require(
            not faltan,
            f"calendar.variants.{vname} no dice qué toca el/los "
            f"{sorted(faltan)}. Un día que no está se lee como descanso, que es "
            f"justo lo que no se distingue de un olvido.",
        )

    # --- reglas del semáforo ------------------------------------------------
    seen_names: set[str] = set()
    for light in ("red", "amber"):
        for rule in data["thresholds"].get(light, []):
            name = rule.get("name")
            require(bool(name), f"hay una regla en thresholds.{light} sin 'name'")
            require(name not in seen_names, f"nombre de regla duplicado: '{name}'")
            seen_names.add(name)
            require("when" in rule, f"la regla '{name}' no tiene bloque 'when'")
            for day in rule.get("only_on_weekday", []):
                require(day in WEEKDAYS, f"la regla '{name}' referencia el día inválido '{day}'")

    # --- acciones por semáforo ---------------------------------------------
    order = data["cycling"].get("recommendation", {}).get("intensity_order", [])
    types = data["cycling"].get("recommendation", {}).get("types", {})
    require(bool(order), "falta cycling.recommendation.intensity_order")
    require(
        set(order) == set(types),
        f"intensity_order {sorted(order)} y types {sorted(types)} no coinciden",
    )
    for light in ("green", "amber", "red"):
        require(light in data["actions"], f"falta actions.{light}")
        cap = data["actions"].get(light, {}).get("bike_max")
        require(
            cap in order,
            f"actions.{light}.bike_max '{cap}' no está en intensity_order",
        )

    rec = data["cycling"].get("recommendation", {})
    for day, level in rec.get("baseline_by_weekday", {}).items():
        require(day in WEEKDAYS, f"baseline_by_weekday: '{day}' no es un día válido")
        require(level in order, f"baseline_by_weekday.{day}: '{level}' no está en intensity_order")
    require(
        rec.get("after_intense_downgrade_to") in order,
        "after_intense_downgrade_to no está en intensity_order",
    )

    # --- clasificación de salidas ------------------------------------------
    levels = [c.get("level") for c in data["cycling"].get("classification", [])]
    require(bool(levels), "falta cycling.classification")
    require(
        any(c.get("always") for c in data["cycling"].get("classification", [])),
        "cycling.classification no tiene un nivel con 'always: true'; "
        "habría salidas sin clasificar",
    )
    for level in levels:
        require(level in order, f"classification: el nivel '{level}' no está en intensity_order")

    # --- reglas especiales: los ejercicios deben existir --------------------
    all_exercise_keys = {
        ex["key"] for r in routines.values() for ex in r.get("exercises", [])
    }
    for rule in data.get("special_rules", []):
        name = rule.get("name", "<sin nombre>")
        action = rule.get("action", {})
        for key in action.get("remove_exercises", []):
            require(
                key in all_exercise_keys,
                f"regla '{name}': el ejercicio '{key}' no existe en ninguna rutina",
            )
        for key in action.get("reduce_load", {}).get("exercises", []):
            require(
                key in all_exercise_keys,
                f"regla '{name}': el ejercicio '{key}' no existe en ninguna rutina",
            )
        src = rule.get("trigger", {}).get("source")
        if src is not None:
            require(
                src in slider_keys,
                f"regla '{name}': la señal '{src}' no es un deslizador del formulario",
            )

    # --- progresión ---------------------------------------------------------
    for brake in data["progression"].get("brakes", []):
        require(
            brake.get("source") in slider_keys,
            f"freno '{brake.get('name')}': la señal '{brake.get('source')}' "
            "no es un deslizador del formulario",
        )
        require(
            brake.get("blocks") in ("all", "last_session_only"),
            f"freno '{brake.get('name')}': 'blocks' debe ser 'all' o 'last_session_only'",
        )

    # --- umbrales adaptativos ----------------------------------------------
    adaptive = data.get("adaptive_thresholds", {})
    for name, spec in adaptive.items():
        require(
            isinstance(spec.get("percentile"), (int, float)) and 0 < spec["percentile"] < 100,
            f"adaptive_thresholds.{name}: 'percentile' debe estar entre 0 y 100",
        )
        require(
            isinstance(spec.get("window_days"), int) and spec["window_days"] > 0,
            f"adaptive_thresholds.{name}: falta 'window_days'",
        )
        require(
            isinstance(spec.get("min_days_required"), int),
            f"adaptive_thresholds.{name}: falta 'min_days_required'",
        )
        require(
            spec.get("min_days_required", 0) <= spec.get("window_days", 0),
            f"adaptive_thresholds.{name}: min_days_required no puede superar window_days",
        )

    # Toda referencia `gt_adaptive` desde una regla debe existir aquí.
    def _check_adaptive_refs(node: Any, rule_name: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "gt_adaptive":
                    require(
                        v in adaptive,
                        f"la regla '{rule_name}' referencia el umbral adaptativo "
                        f"'{v}', que no está definido en adaptive_thresholds",
                    )
                else:
                    _check_adaptive_refs(v, rule_name)
        elif isinstance(node, list):
            for item in node:
                _check_adaptive_refs(item, rule_name)

    for light in ("red", "amber"):
        for rule in data["thresholds"].get(light, []):
            _check_adaptive_refs(rule.get("when"), rule.get("name", "<sin nombre>"))

    # --- HIIT ---------------------------------------------------------------
    hiit = data["hiit"]
    allowed = hiit.get("allowed_routines", [])
    never = hiit.get("never_routines", [])
    for key in allowed:
        require(key in routines, f"hiit.allowed_routines: la rutina '{key}' no existe")
    for key in never:
        require(key in routines, f"hiit.never_routines: la rutina '{key}' no existe")

    # El día 3 (viernes) es la sesión ligera previa a la bici del fin de semana.
    # Si alguien lo mete en allowed_routines sin recordar el motivo, mejor que
    # el sistema se niegue a arrancar a que le añada intensidad en silencio.
    conflict = sorted(set(allowed) & set(never))
    require(
        not conflict,
        f"hiit: {conflict} está a la vez en allowed_routines y never_routines. "
        "El día 3 no debe llevar HIIT: es la sesión ligera previa a la bici.",
    )

    blocks = hiit.get("blocks", {})
    require(bool(blocks), "falta hiit.blocks (mapa rutina -> bloque HIIT)")
    for routine_key, block_key in blocks.items():
        require(
            routine_key in routines,
            f"hiit.blocks: la rutina '{routine_key}' no existe",
        )
        require(
            block_key in routines,
            f"hiit.blocks.{routine_key}: el bloque '{block_key}' no existe",
        )
        require(
            routine_key not in never,
            f"hiit.blocks: '{routine_key}' tiene bloque HIIT pero está en never_routines",
        )
    for key in allowed:
        require(
            key in blocks,
            f"hiit.allowed_routines incluye '{key}' pero no tiene bloque en hiit.blocks",
        )

    # --- restricción de seguridad: patrones prohibidos en HIIT --------------
    # Es una restricción médica permanente (hernia L4-L5), no un umbral.
    # Se aplica SOLO a las rutinas HIIT: en la fuerza normal el peso muerto está
    # permitido y controlado por la regla `retirada_peso_muerto`.
    forbidden = data.get("safety", {}).get("forbidden_in_hiit", {})
    if forbidden:
        blocked_ids = {
            str(e["id"]).upper() for e in forbidden.get("template_ids", []) if e.get("id")
        }
        exempt_ids = {
            str(e["id"]).upper() for e in forbidden.get("allow_exceptions", []) if e.get("id")
        }
        patterns = [re.compile(p, re.IGNORECASE) for p in forbidden.get("name_patterns", [])]

        # Las rutinas HIIT son las referenciadas desde hiit.blocks.
        hiit_routine_keys = set(blocks.values())
        for rkey in sorted(hiit_routine_keys):
            for ex in routines.get(rkey, {}).get("exercises", []):
                tid = str(ex.get("template_id") or "").upper()
                if tid in exempt_ids:
                    continue
                name = ex.get("name", "")
                if tid in blocked_ids:
                    blocked_name = next(
                        (
                            e["name"]
                            for e in forbidden["template_ids"]
                            if str(e["id"]).upper() == tid
                        ),
                        name,
                    )
                    errors.append(
                        f"SEGURIDAD — rutina HIIT '{rkey}': el ejercicio '{name}' "
                        f"(template {tid} = {blocked_name}) está prohibido en HIIT "
                        f"por la hernia L4-L5. Ver safety.forbidden_in_hiit."
                    )
                    continue
                normalized = _strip_accents(name)
                for pat in patterns:
                    if pat.search(normalized):
                        errors.append(
                            f"SEGURIDAD — rutina HIIT '{rkey}': el nombre del ejercicio "
                            f"'{name}' coincide con el patrón prohibido "
                            f"/{pat.pattern}/ (hernia L4-L5). Si es un falso positivo, "
                            f"añádelo a safety.forbidden_in_hiit.allow_exceptions con "
                            f"el motivo."
                        )
                        break

    # --- presupuesto semanal de intensidad ----------------------------------
    budget = (
        data["cycling"].get("recommendation", {}).get("intensity_budget", {})
    )
    if budget.get("enabled"):
        require(
            isinstance(budget.get("weekly_limit"), int) and budget["weekly_limit"] > 0,
            "intensity_budget.weekly_limit debe ser un entero positivo",
        )
        require(
            budget.get("week_starts_on") in WEEKDAYS,
            f"intensity_budget.week_starts_on '{budget.get('week_starts_on')}' no es válido",
        )
        require(
            budget.get("on_budget_exhausted") in order,
            f"intensity_budget.on_budget_exhausted '{budget.get('on_budget_exhausted')}' "
            "no está en intensity_order",
        )
        require(
            isinstance(budget.get("max_intense_rides_per_weekend"), int),
            "falta intensity_budget.max_intense_rides_per_weekend",
        )

    # --- rutinas ------------------------------------------------------------
    for rkey, routine in routines.items():
        keys_here: set[str] = set()
        for ex in routine.get("exercises", []):
            k = ex.get("key")
            require(bool(k), f"rutina '{rkey}': hay un ejercicio sin 'key'")
            require(
                k not in keys_here,
                f"rutina '{rkey}': la clave de ejercicio '{k}' está duplicada",
            )
            keys_here.add(k)
            require(
                bool(ex.get("template_id")),
                f"rutina '{rkey}' / '{k}': falta template_id (Hevy no aceptará la escritura)",
            )
            require(
                isinstance(ex.get("sets"), list) and len(ex["sets"]) > 0,
                f"rutina '{rkey}' / '{k}': 'sets' debe ser una lista no vacía",
            )
            for i, s in enumerate(ex.get("sets") or []):
                stype = s.get("type")
                require(
                    stype is None or str(stype).lower() in VALID_SET_TYPES,
                    f"rutina '{rkey}' / '{k}': serie {i} tiene type='{stype}', "
                    f"que no es uno de {sorted(VALID_SET_TYPES)}",
                )

    # --- series de calentamiento --------------------------------------------
    st = data.get("set_types") or {}
    require(
        bool(st),
        "falta la sección 'set_types'. Sin ella no se sabe qué series cuentan "
        "como calentamiento, y eso mueve a la vez el cumplimiento, el recorte "
        "del ámbar y la progresión de volumen. Decláralo, aunque sea "
        "'source: api'",
    )
    if st:
        source = st.get("source")
        require(
            source in VALID_SET_SOURCES,
            f"set_types.source '{source}' no es válido "
            f"(esperado: {sorted(VALID_SET_SOURCES)})",
        )
        h = st.get("heuristic") or {}
        if h.get("enabled", True):
            require(
                int(h.get("sets_gte", 4)) >= 2,
                "set_types.heuristic.sets_gte debe ser al menos 2",
            )
            require(
                1 <= int(h.get("count", 1)) < int(h.get("sets_gte", 4)),
                "set_types.heuristic.count debe ser >= 1 y menor que sets_gte: "
                "si no, un ejercicio podría quedarse sin ninguna serie efectiva",
            )
        for ekey in (st.get("overrides") or {}):
            require(
                ekey in all_exercise_keys,
                f"set_types.overrides: el ejercicio '{ekey}' no existe en ninguna rutina",
            )

    # --- modos de progresión -------------------------------------------------
    prog = data.get("progression") or {}
    default_mode = str(prog.get("default_progression_type", "double"))
    require(
        default_mode in VALID_PROGRESSION_TYPES,
        f"progression.default_progression_type '{default_mode}' no es válido "
        f"(esperado: {sorted(VALID_PROGRESSION_TYPES)})",
    )

    modes = prog.get("modes") or {}
    sets_mode = modes.get("sets") or {}
    then_default = str(sets_mode.get("then", "double"))
    require(
        then_default in {"load", "double"},
        f"progression.modes.sets.then '{then_default}' no es válido: "
        "al agotar el techo de series solo se puede pasar a 'load' o 'double'",
    )

    vs = prog.get("volume_safety") or {}
    scope = str(vs.get("conflict_scope", "session"))
    require(
        scope in {"session", "exercise"},
        f"progression.volume_safety.conflict_scope '{scope}' no es válido "
        "(esperado: 'session' o 'exercise')",
    )
    prefer = str(vs.get("prefer_on_conflict", "volume"))
    require(
        prefer in {"volume", "load"},
        f"progression.volume_safety.prefer_on_conflict '{prefer}' no es válido "
        "(esperado: 'volume' o 'load')",
    )
    policy = str(vs.get("queue_policy", "waiting_longest"))
    require(
        policy in {"waiting_longest", "routine_order"},
        f"progression.volume_safety.queue_policy '{policy}' no es válido "
        "(esperado: 'waiting_longest' o 'routine_order')",
    )
    for ckey in ("max_volume_increases_per_session", "max_load_increases_per_session"):
        cap = vs.get(ckey)
        require(
            cap is None or int(cap) >= 1,
            f"progression.volume_safety.{ckey} debe ser >= 1 "
            "(para desactivar la progresión entera usa la puerta, no un cupo de 0)",
        )

    state_scope = str(vs.get("state_scope", "routine_exercise"))
    require(
        state_scope in {"routine_exercise", "exercise"},
        f"progression.volume_safety.state_scope '{state_scope}' no es válido "
        "(esperado: 'routine_exercise' o 'exercise')",
    )

    # Las dos puertas de volumen. La estricta gobierna añadir una serie; la
    # relajada, subir reps o segundos.
    valid_lights = {"green", "amber", "red"}
    for gname in ("sets_gate", "reps_gate"):
        g = vs.get(gname)
        require(
            isinstance(g, dict),
            f"progression.volume_safety.{gname} falta o no es un mapa. Desde que "
            "las series y las reps tienen frenos separados las dos puertas son "
            "obligatorias: sin una de ellas ese modo no sabría cuándo pararse",
        )
        if not isinstance(g, dict):
            continue
        blocking = g.get("block_if_last_routine_session_in") or []
        require(
            isinstance(blocking, list),
            f"progression.volume_safety.{gname}.block_if_last_routine_session_in "
            "debe ser una lista de semáforos",
        )
        names = {str(x) for x in blocking} if isinstance(blocking, list) else set()
        for lg in sorted(names):
            require(
                lg in valid_lights,
                f"progression.volume_safety.{gname}."
                f"block_if_last_routine_session_in: '{lg}' no es un semáforo válido "
                f"(esperado: {sorted(valid_lights)})",
            )
        require(
            "green" not in names,
            f"progression.volume_safety.{gname} bloquea con la sesión anterior en "
            "VERDE, y eso deja el modo sin ninguna sesión en la que pueda progresar. "
            "Si la intención es congelarlo, usa progression_type: none",
        )

    # La puerta de las series no puede ser más laxa que la de las reps: añadir
    # una serie es siempre el movimiento más arriesgado de los dos.
    sg = {str(x) for x in ((vs.get("sets_gate") or {}).get(
        "block_if_last_routine_session_in") or [])}
    rg = {str(x) for x in ((vs.get("reps_gate") or {}).get(
        "block_if_last_routine_session_in") or [])}
    require(
        rg <= sg,
        "progression.volume_safety: reps_gate es MÁS estricta que sets_gate "
        f"(reps bloquea en {sorted(rg)}, series en {sorted(sg)}). Añadir una serie "
        "es el movimiento más arriesgado de los dos, así que su puerta no puede ser "
        "la más permisiva",
    )
    s_disc = (vs.get("sets_gate") or {}).get("block_if_mean_lower_discomfort_gte")
    r_disc = (vs.get("reps_gate") or {}).get("block_if_mean_lower_discomfort_gte")
    require(
        s_disc is None or r_disc is None or float(s_disc) <= float(r_disc),
        f"progression.volume_safety: el umbral de lumbar de sets_gate ({s_disc}) debe "
        f"ser <= el de reps_gate ({r_disc}); si no, se permitiría añadir una serie con "
        "más molestia de la que hace falta para sumar una repetición",
    )

    lookback = vs.get("discomfort_lookback_days")
    require(
        lookback is None or int(lookback) >= 1,
        "progression.volume_safety.discomfort_lookback_days debe ser >= 1",
    )
    for dead in ("lookback_days", "block_if_red_days_gte", "block_if_amber_days_gte"):
        require(
            dead not in vs,
            f"progression.volume_safety.{dead} ya no se usa: los frenos de volumen se "
            "anclan a la sesión anterior de la misma rutina, no a una ventana de días. "
            "Muévelo a sets_gate/reps_gate.block_if_last_routine_session_in",
        )

    dl = prog.get("deload") or {}
    for fkey in ("sets_factor", "reps_factor", "seconds_factor"):
        f = dl.get(fkey)
        require(
            f is None or 0 < float(f) <= 1,
            f"progression.deload.{fkey} debe estar en (0, 1]: una descarga "
            "recorta volumen, no lo aumenta",
        )

    for rkey, routine in routines.items():
        for ex in routine.get("exercises") or []:
            k = ex.get("key", "<sin key>")
            mode = str(ex.get("progression_type", default_mode))
            require(
                mode in VALID_PROGRESSION_TYPES,
                f"rutina '{rkey}' / '{k}': progression_type '{mode}' no es válido "
                f"(esperado: {sorted(VALID_PROGRESSION_TYPES)})",
            )

            rr = ex.get("rep_range")
            if rr is not None:
                ok = isinstance(rr, list) and len(rr) == 2
                require(ok, f"rutina '{rkey}' / '{k}': rep_range debe ser [min, max]")
                if ok:
                    require(
                        int(rr[0]) < int(rr[1]),
                        f"rutina '{rkey}' / '{k}': rep_range {rr} está invertido o "
                        "es un punto: sin recorrido no hay doble progresión",
                    )

            if mode == "sets":
                # Un `max_sets` por debajo de las series efectivas que ya tiene
                # el ejercicio no es un techo: es un recorte encubierto, y el
                # motor no lo aplicaría nunca (solo suma series). Mejor que
                # falle aquí que dejar un ejercicio congelado en silencio.
                effective = sum(
                    1 for s in (ex.get("sets") or [])
                    if str(s.get("type") or "normal").lower() != "warmup"
                )
                max_sets = int(ex.get("max_sets", sets_mode.get("max_sets", 5)))
                require(
                    max_sets >= effective,
                    f"rutina '{rkey}' / '{k}': max_sets={max_sets} es menor que las "
                    f"{effective} series efectivas que ya tiene",
                )
                then = str(ex.get("then", then_default))
                require(
                    then in {"load", "double"},
                    f"rutina '{rkey}' / '{k}': then '{then}' no es válido",
                )
                if then == "double":
                    require(
                        ex.get("rep_range") is not None,
                        f"rutina '{rkey}' / '{k}': con then=double hace falta "
                        "rep_range, o al agotar las series no habrá a qué pasar",
                    )

            if mode == "volume":
                has_reps = any(s.get("reps") for s in (ex.get("sets") or []))
                has_secs = any(s.get("duration_s") for s in (ex.get("sets") or []))
                require(
                    has_reps or has_secs,
                    f"rutina '{rkey}' / '{k}': progression_type=volume pero el "
                    "ejercicio no tiene ni reps ni duration_s que subir",
                )
                # El techo tiene que estar por encima de lo que ya se hace.
                if has_secs:
                    cur = max(
                        int(s["duration_s"]) for s in ex["sets"] if s.get("duration_s")
                    )
                    cap_s = int(ex.get("max_seconds", (modes.get("volume") or {}).get(
                        "max_seconds", 60)))
                    require(
                        cap_s >= cur,
                        f"rutina '{rkey}' / '{k}': max_seconds={cap_s} está por debajo "
                        f"de los {cur} s que ya hace",
                    )
                elif has_reps:
                    cur = max(int(s["reps"]) for s in ex["sets"] if s.get("reps"))
                    cap_r = int(ex.get("max_reps", (modes.get("volume") or {}).get(
                        "max_reps", 30)))
                    require(
                        cap_r >= cur,
                        f"rutina '{rkey}' / '{k}': max_reps={cap_r} está por debajo "
                        f"de las {cur} reps que ya hace",
                    )

    # --- erratas: operadores y claves desconocidas ---------------------------
    #
    # Esto es la misma lección que el `fatige=5` del check-in, aplicada al
    # YAML. Una clave mal escrita aquí no da error: se lee con un `.get(clave,
    # defecto)` y el defecto decide en su lugar. Los tres casos concretos:
    #
    #   duration_days -> durantion_days   la retirada de peso muerto dura 1
    #                                     día en vez de 14
    #   every_n_weeks -> every_n_week     la descarga no se programa nunca
    #   factor        -> factorr          la reducción de carga se queda en 1.0
    #
    # Ninguno de los tres avisa. Todos cambian el entrenamiento. Por eso una
    # clave que no se reconoce es un error de arranque, no un valor ignorado.
    def check_ops(when: Any, where: str) -> None:
        if not isinstance(when, dict):
            require(False, f"{where}: 'when' debe ser un diccionario de operadores")
            return
        require(bool(when), f"{where}: 'when' está vacío, no compara nada")
        for op in when:
            base = str(op)
            if base.endswith("_adaptive"):
                base = base[: -len("_adaptive")]
            require(
                base in COMPARISONS,
                f"{where}: operador desconocido '{op}'. "
                f"Válidos: {', '.join(sorted(COMPARISONS))} "
                f"(y su variante '_adaptive')",
            )

    def check_keys(obj: Any, allowed: set[str], where: str) -> None:
        if not isinstance(obj, dict):
            return
        for k in obj:
            require(
                k in allowed,
                f"{where}: clave desconocida '{k}'. Válidas: "
                f"{', '.join(sorted(allowed))}. Si es una errata, el valor "
                f"real se estaría ignorando en silencio",
            )

    for i, brake in enumerate(prog.get("brakes") or []):
        where = f"progression.brakes[{i}] ('{brake.get('name', 'sin nombre')}')"
        check_keys(brake, {"name", "source", "when", "blocks", "on_missing"}, where)
        check_ops(brake.get("when"), where)
        require(
            str(brake.get("blocks", "all")) in {"all", "last_session_only"},
            f"{where}: 'blocks' debe ser 'all' o 'last_session_only'",
        )
        require(
            str(brake.get("on_missing", "block")) in {"block", "skip"},
            f"{where}: 'on_missing' debe ser 'block' o 'skip'. Con 'block' "
            f"(por defecto) un freno sin datos cierra la puerta",
        )
        require(
            bool(brake.get("source")),
            f"{where}: falta 'source', no hay señal que vigilar",
        )

    for i, rule in enumerate(data.get("special_rules") or []):
        where = f"special_rules[{i}] ('{rule.get('name', 'sin nombre')}')"
        check_keys(rule, {"name", "description", "trigger", "action", "notify"}, where)

        trig = rule.get("trigger") or {}
        check_keys(
            trig,
            {"source", "when", "consecutive_days", "every_n_weeks", "jitter_weeks"},
            f"{where}.trigger",
        )
        # Un disparador o mira una señal, o va por calendario. Ni las dos ni
        # ninguna: sin esto una regla puede quedarse muda sin que se note.
        por_senal = "source" in trig
        por_calendario = "every_n_weeks" in trig
        require(
            por_senal != por_calendario,
            f"{where}.trigger: tiene que ser por señal ('source' + 'when') o "
            f"por calendario ('every_n_weeks'), y solo uno de los dos",
        )
        if por_senal:
            check_ops(trig.get("when"), f"{where}.trigger")

        action = rule.get("action") or {}
        check_keys(
            action,
            {
                "remove_exercises",
                "reduce_load",
                "load_factor",
                "allow_hiit",
                "duration_days",
            },
            f"{where}.action",
        )
        require(
            "duration_days" in action,
            f"{where}.action: falta 'duration_days'. Sin él la regla dura 1 "
            f"día, que casi nunca es lo que se quiere y no se nota",
        )
        rl = action.get("reduce_load")
        if rl is not None:
            check_keys(rl, {"exercises", "factor"}, f"{where}.action.reduce_load")
            require(
                rl.get("factor") is not None,
                f"{where}.action.reduce_load: falta 'factor'; sin él no se "
                f"reduce nada (equivale a 1.0)",
            )
        for fkey in ("load_factor", "factor"):
            f = action.get(fkey) if fkey == "load_factor" else (rl or {}).get(fkey)
            require(
                f is None or 0 < float(f) <= 1,
                f"{where}: '{fkey}'={f} debe estar en (0, 1]: estas reglas "
                f"recortan carga, no la suben",
            )

    # --- los interruptores de salida -----------------------------------------
    #
    # Aquí una clave decorativa cuesta más que en cualquier otro sitio, porque
    # el usuario la pone precisamente para APAGAR algo. `notifications.telegram`
    # llegó a declarar un `enabled: true` que no leía nadie: el único freno real
    # es `integrations.telegram.send_enabled`, que se comprueba dentro de
    # `send()`. Quien lo hubiera puesto en `false` para callar el bot habría
    # seguido recibiendo mensajes, y el config le habría dado la razón por
    # escrito.
    #
    # Un interruptor que no está conectado a nada es peor que no tener
    # interruptor: promete un control que no existe.
    integ = data.get("integrations") or {}
    check_keys(integ, {"hevy", "telegram"}, "integrations")
    check_keys(
        integ.get("hevy") or {},
        {"write_enabled", "backup"},
        "integrations.hevy",
    )
    # `enabled` NO está en la lista a propósito. Que se hagan copias no es
    # opcional -sin copia verificada no se escribe, y eso vive en el código-, así
    # que aceptar un `enabled: false` que nadie mira sería prometer un apagado
    # que no existe. Se rechaza para que quien lo escriba se entere al arrancar.
    respaldo = (integ.get("hevy") or {}).get("backup") or {}
    check_keys(respaldo, {"keep_last"}, "integrations.hevy.backup")
    # No escribir `keep_last` significa conservarlas todas, que es lo que se
    # hacía antes de que esto se leyera. Escribirlo VACÍO acaba en el mismo
    # sitio, así que sirve solo para aparentar que dice un número. O se pone un
    # número o no se pone la clave.
    if "keep_last" in respaldo:
        guardar = respaldo["keep_last"]
        require(
            isinstance(guardar, int) and not isinstance(guardar, bool) and guardar >= 1,
            f"integrations.hevy.backup.keep_last vale {guardar!r} y tiene que ser "
            f"un entero >= 1 (o no estar, y entonces se guardan todas). Con 0 no "
            f"quedaría ninguna copia, y la copia es lo único que permite deshacer "
            f"una escritura en Hevy.",
        )
    check_keys(
        integ.get("telegram") or {}, {"send_enabled"}, "integrations.telegram"
    )

    notif = data.get("notifications") or {}
    check_keys(notif, {"telegram"}, "notifications")
    check_keys(
        notif.get("telegram") or {},
        {"include_reasoning"},
        "notifications.telegram",
    )

    # --- las horas y el reintento de Garmin ----------------------------------
    #
    # `garmin_retry` estuvo declarado aquí sin que lo leyera nadie mientras el
    # código reintentaba con otros números. No es lo mismo que una clave
    # sobrante: era una contradicción, y la que perdía era la del YAML, que es
    # la que se lee cuando hay que entender qué hace el sistema.
    sched = data.get("schedule") or {}
    check_keys(
        sched,
        {
            "garmin_fetch_time",
            "fallback_decision_time",
            "evening_summary_time",
            "garmin_retry",
        },
        "schedule",
    )
    for clave in ("garmin_fetch_time", "fallback_decision_time", "evening_summary_time"):
        v = sched.get(clave)
        if v is None:
            continue
        partes = str(v).split(":")
        ok = len(partes) >= 2
        if ok:
            try:
                h, m = int(partes[0]), int(partes[1])
                ok = 0 <= h <= 23 and 0 <= m <= 59
            except ValueError:
                ok = False
        require(
            ok,
            f"schedule.{clave}: '{v}' no es una hora 'HH:MM' válida. Una hora "
            f"ilegible reventaría al montar el scheduler, y sería a las seis de "
            f"la mañana del día que se despliegue",
        )

    retry = sched.get("garmin_retry") or {}
    if retry:
        check_keys(
            retry,
            {"attempts", "backoff_seconds"},
            "schedule.garmin_retry",
        )
        intentos = retry.get("attempts")
        require(
            intentos is None or (isinstance(intentos, int) and intentos >= 1),
            f"schedule.garmin_retry.attempts: '{intentos}' debe ser un entero "
            f">= 1. Con 0 no se llama a Garmin ni una vez",
        )
        esperas = retry.get("backoff_seconds")
        if esperas is not None:
            require(
                isinstance(esperas, list) and all(
                    isinstance(x, (int, float)) and x >= 0 for x in esperas
                ),
                "schedule.garmin_retry.backoff_seconds: debe ser una lista de "
                "segundos (números no negativos)",
            )
            # Entre N intentos hay N-1 esperas. Si la lista se queda corta, la
            # última se repetiría: nadie lo vería y el margen real no sería el
            # que pone aquí. Mejor no arrancar.
            if isinstance(esperas, list) and isinstance(intentos, int):
                require(
                    len(esperas) >= intentos - 1,
                    f"schedule.garmin_retry: {intentos} intentos necesitan "
                    f"{intentos - 1} esperas y solo hay {len(esperas)}. "
                    f"Completa 'backoff_seconds': si no, la última se repetiría "
                    f"y el margen real no sería el que dice este archivo",
                )

    return errors


def load_config(path: Path | str) -> Config:
    """Carga y valida config.yaml. Lanza ConfigError con todos los problemas."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"no se encuentra el archivo de configuración: {path}")

    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    if not isinstance(data, dict):
        raise ConfigError(f"{path} no contiene un mapa YAML en la raíz")

    problems = _validate(data)
    if problems:
        listed = "\n".join(f"  - {p}" for p in problems)
        raise ConfigError(f"config.yaml tiene {len(problems)} problema(s):\n{listed}")

    return Config(data, compute_hash(data), source=path)
