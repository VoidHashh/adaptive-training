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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from app.engine.rules import (
    ADAPTIVE_SUFFIX,
    COMPARISONS,
    OPTION_SUFFIX,
    RuleError,
    resolve_option,
)


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

# Los mismos días en español, y a propósito en una lista aparte. `WEEKDAYS` es
# vocabulario del config -lo que el usuario puede escribir en `week_starts_on`- y
# tiene que seguir estando en inglés; esto es solo para redactar mensajes de
# error legibles. Juntarlos obligaría a traducir en el sitio equivocado.
DIAS_ES = [
    "lunes",
    "martes",
    "miércoles",
    "jueves",
    "viernes",
    "sábado",
    "domingo",
]

# Tipos de serie que acepta Hevy en el campo `type`.
VALID_SET_TYPES = {"normal", "warmup", "failure", "dropset"}
VALID_SET_SOURCES = {"api", "heuristic", "api_then_heuristic"}
VALID_PROGRESSION_TYPES = {"load", "double", "volume", "sets", "none"}
# Cómo se reparte una subida de repeticiones entre las series efectivas.
VALID_REP_APPLY_TO = {"lowest_first", "all_sets"}


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
    def recalibrar_cada_dias(self) -> int:
        """Cada cuántos días CON DECISIÓN toca revisar los umbrales cortos.

        Sin defecto y sin `.get(..., 28)`: el validador garantiza que está, y un
        defecto escondido aquí convertiría un borrado accidental de la clave en
        un sistema que sigue avisando con un número que no está escrito en
        ninguna parte.
        """
        return int(self._data["program"]["recalibrar_cada_dias"])

    @property
    def recalibrado_el(self) -> date:
        """El último día en que se revisaron. Origen de la cuenta."""
        value = self._data["program"]["recalibrado_el"]
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
    def wellness(self) -> dict[str, Any]:
        return self._data.get("wellness", {})

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

    def check_keys(obj: Any, allowed: set[str], where: str) -> None:
        """Lista blanca: cualquier clave fuera de `allowed` es un error duro.

        Está definida aquí arriba, y no en mitad del fichero como estuvo, para
        que la puedan usar TODAS las secciones. Mientras vivía a la altura de
        `progression.brakes` las de más arriba -rutinas, cycling- no podían
        llamarla, y ahí es donde se quedaron dos claves muertas.
        """
        if not isinstance(obj, dict):
            return
        for k in obj:
            require(
                k in allowed,
                f"{where}: clave desconocida '{k}'. Válidas: "
                f"{', '.join(sorted(allowed))}. Si es una errata, el valor "
                f"real se estaría ignorando en silencio",
            )

    def _es_num(v: Any) -> bool:
        """¿Es un número de verdad? `True` NO lo es.

        `isinstance(True, int)` vale `True` en Python, así que un `max_jump_kg:
        yes` mal puesto en el YAML pasaría por un 1 y dejaría el tope de salto en
        un kilo sin que nadie lo notase.
        """
        return isinstance(v, (int, float)) and not isinstance(v, bool)

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
        # Obligatoria aunque la capa se pueda apagar. Apagarla se escribe
        # `enabled: false`, que deja constancia; no escribir la sección dejaría
        # la decisión sin rastro y sin sitio donde leer los umbrales.
        "trend",
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
        "wellness",
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
        "trend",
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
    elif prog_start.weekday() != 0:
        # No es tiquismiquis. `_deload` no cuenta desde esta fecha: cuenta desde
        # `week_start()` de esta fecha, porque las descargas empiezan siempre en
        # lunes. Con un martes escrito aquí, el origen real es el lunes anterior
        # y el valor que se lee en el YAML no es el que usa el sistema. El
        # redondeo es correcto; lo que no puede es pasar callando, porque
        # entonces la primera descarga "se adelanta" a las 6,9 semanas y el
        # número no cuadra con nada que esté escrito.
        lunes_real = prog_start - timedelta(days=prog_start.weekday())
        errors.append(
            f"program.start '{prog_start}' es {DIAS_ES[prog_start.weekday()]}, y "
            f"tiene que ser LUNES. El contador de descargas redondea al lunes de "
            f"esa semana, así que el origen de verdad sería {lunes_real} y no la "
            f"fecha que pone aquí. Si querías esa semana, escribe {lunes_real}."
        )

    # --- el recordatorio de recalibración ------------------------------------
    # Las dos claves son OBLIGATORIAS, y no por simetría con `start`. Este
    # recordatorio existe porque un comentario que pide "revisar a las cuatro
    # semanas" no lo lee nadie a las cuatro semanas; si además la clave que lo
    # dispara se puede borrar sin consecuencias, lo que queda es un recordatorio
    # que se puede apagar por descuido y que, apagado, no se distingue de uno
    # que todavía no ha llegado el momento de dar. Se prefiere no arrancar.
    #
    # `check_keys` sobre `program` va aquí abajo por el mismo motivo de siempre:
    # escribir `recalibrar_cada: 28` en vez de `recalibrar_cada_dias` daría un
    # arranque limpio, la clave buena ausente y el aviso desactivado para
    # siempre. Era, además, la única sección de primer nivel sin lista blanca.
    check_keys(
        data.get("program") or {},
        {"start", "recalibrar_cada_dias", "recalibrado_el"},
        "program",
    )

    cada = (data.get("program") or {}).get("recalibrar_cada_dias")
    if cada is None:
        errors.append(
            "program.recalibrar_cada_dias está vacío o no existe. Es cada "
            "cuántos días CON DECISIÓN guardada el mensaje de la mañana avisa "
            "de que toca revisar los umbrales calibrados sobre muestras cortas "
            "(la clasificación de bici, el umbral del fin de semana). Sin él "
            "ese aviso no se da nunca y la revisión se queda en la promesa de "
            "un comentario. Pon un entero de días; 28 son las cuatro semanas."
        )
    elif not _es_num(cada) or int(cada) != cada or int(cada) < 1:
        errors.append(
            f"program.recalibrar_cada_dias '{cada}' no es un número entero de "
            "días mayor que cero."
        )

    recal = (data.get("program") or {}).get("recalibrado_el")
    if recal is None:
        errors.append(
            "program.recalibrado_el está vacío o no existe. Es el día desde el "
            "que se cuentan los días con decisión hasta el próximo aviso de "
            "recalibración, y es también la ÚNICA forma de callar ese aviso: "
            "ponerle la fecha de hoy significa 'ya lo he mirado'. Si nunca has "
            "recalibrado, pon la misma fecha que program.start."
        )
    elif isinstance(recal, datetime):
        errors.append(
            f"program.recalibrado_el '{recal}' lleva hora. Debe ser una fecha "
            "AAAA-MM-DD sin hora: la cuenta va por días."
        )
    elif not isinstance(recal, date):
        try:
            datetime.strptime(str(recal), "%Y-%m-%d")
        except ValueError:
            errors.append(
                f"program.recalibrado_el '{recal}' no es una fecha AAAA-MM-DD válida"
            )
        else:
            errors.append(
                f"program.recalibrado_el '{recal}' está entre comillas. Quítalas "
                "para que YAML lo lea como fecha y no como texto."
            )
    elif isinstance(prog_start, date) and not isinstance(prog_start, datetime):
        # Las dos comprobaciones cruzadas. Ninguna de las dos rompe nada al
        # instante, y las dos dejan el aviso mudo durante meses, que es
        # exactamente lo que este recordatorio no puede permitirse.
        #
        # El techo se calcula UNA vez y se usa en la condición y en el mensaje.
        # Escrito dos veces, un cambio de criterio puede tocar solo una de ellas
        # y dejar un error que rechaza por un motivo y explica otro.
        techo = max(date.today(), prog_start)
        if recal < prog_start:
            errors.append(
                f"program.recalibrado_el '{recal}' es anterior a program.start "
                f"'{prog_start}'. La cuenta arrancaría en días que el programa "
                "todavía no había vivido."
            )
        elif recal > techo:
            # El techo es el ÚLTIMO de los dos, no hoy a secas. Cuando el
            # programa todavía no ha arrancado -se configura el sábado para
            # empezar el lunes- la única fecha que cumple el mínimo de arriba es
            # el propio `program.start`, y esa es futura por definición.
            # Prohibirla dejaría el config sin ningún valor válido: el mínimo
            # pide >= start y el máximo pedía <= hoy, y no hay número entre los
            # dos. Lo que se persigue es el año mal escrito, y ese sigue cayendo.
            #
            # No pasa nada por contar desde una fecha que aún no ha llegado: lo
            # que se cuenta son días CON DECISIÓN, y antes del arranque no hay
            # ninguna. La cuenta sale 0 igual, pero ahora lo dice el calendario
            # en vez de un hueco en la base de datos.
            errors.append(
                f"program.recalibrado_el '{recal}' está más allá de "
                f"{techo.isoformat()} (hoy es {date.today().isoformat()}, "
                f"program.start es {prog_start.isoformat()}). Casi siempre es un "
                "año mal escrito, y el efecto es que el aviso de recalibración "
                "no vuelve a salir hasta esa fecha."
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
    # `no_consecutive_intense` y `after_intense_downgrade_to` recortaban la
    # salida del domingo si el sábado había sido intensa. Se han borrado: eso
    # es una cuenta, no una señal del cuerpo, y ahora sale como nota. Van a la
    # lista negra por lo mismo que las del recuento: reescribirlas aquí sería
    # creer que se recupera un freno que ya no existe.
    for muerta in ("no_consecutive_intense", "after_intense_downgrade_to"):
        require(
            muerta not in rec,
            f"cycling.recommendation.{muerta} ya no existe. La salida intensa "
            f"de ayer ahora se cuenta y se dice, no recorta la de hoy. Quien "
            f"frena por acumulación es el semáforo.",
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
    #
    # `r.get("exercises", [])` devolvía None con `exercises:` a secas y esto
    # reventaba con un TypeError en vez de dar el error de configuración. Un
    # validador que se rompe al validar deja al usuario con una traza de
    # Python donde debería haber una frase, y encima oculta el resto de
    # problemas del fichero: `_validate` los junta todos y los enseña de una
    # vez, y una excepción a mitad se lleva por delante los que faltaban.
    all_exercise_keys = {
        ex["key"]
        for r in routines.values()
        for ex in (r.get("exercises") or [])
        if isinstance(ex, dict) and ex.get("key")
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

    # --- la ventana de salidas que alimenta esos umbrales --------------------
    #
    # `cycling.fetch` estuvo declarado sin que lo leyera nadie mientras el
    # código pedía 190 días a pelo desde dos sitios. Ya está conectado
    # (`activity_cache.ventana_de_salidas`), y conectarlo obliga a comprobar
    # esto: el backfill es lo único que llena la caché sobre la que se calculan
    # los percentiles, así que un backfill más corto que la ventana del
    # percentil apaga `carga_acumulada` y `carga_semanal` PARA SIEMPRE y sin un
    # solo error. La regla no dispara, no falla, y el mensaje de la mañana sale
    # igual de bonito con una señal menos.
    fetch = ((data.get("cycling") or {}).get("fetch") or {})
    if fetch:
        for clave in ("lookback_days", "backfill_days"):
            v = fetch.get(clave)
            require(
                v is None or (isinstance(v, int) and v >= 1),
                f"cycling.fetch.{clave}: '{v}' debe ser un entero >= 1",
            )
        corta = fetch.get("lookback_days")
        larga = fetch.get("backfill_days")
        if isinstance(corta, int) and isinstance(larga, int):
            require(
                corta <= larga,
                f"cycling.fetch: lookback_days ({corta}) no puede ser mayor que "
                f"backfill_days ({larga}). La corta es la relectura de cada "
                f"mañana y la larga el histórico completo; al revés los nombres "
                f"mienten y el 'backfill' dejaría huecos",
            )
        necesarios = max(
            (int(s.get("window_days", 0)) for s in adaptive.values() if isinstance(s, dict)),
            default=0,
        )
        if isinstance(larga, int) and necesarios:
            require(
                larga >= necesarios,
                f"cycling.fetch.backfill_days ({larga}) es menor que la ventana "
                f"más larga de adaptive_thresholds ({necesarios} días). La caché "
                f"de salidas nunca llegaría a cubrirla, así que los percentiles "
                f"de carga se quedarían sin base y las reglas que los usan no se "
                f"evaluarían ningún día, sin dar error",
            )

    # --- el relleno hacia atrás del bienestar --------------------------------
    #
    # Se valida con la misma dureza que `cycling.fetch` y por el mismo motivo:
    # `wellness.backfill` es lo único que llena los días que el sistema se
    # perdió, y un backfill más corto que la ventana que luego se analiza deja
    # huecos que nadie va a ver como huecos -van a parecer días sin reloj-.
    wel = data.get("wellness") or {}
    # `fetch_readiness` estuvo aquí y ya no: se fue con la llamada, el campo y la
    # columna. Que `check_keys` ya no la admita no es un descuido, es la mitad
    # útil de borrarla: un YAML viejo que la traiga tiene que dar error en el
    # arranque y no quedarse callado dando a entender que la opción sigue viva.
    check_keys(wel, {"backfill"}, "wellness")
    bf = wel.get("backfill") or {}
    check_keys(bf, {"recovery_days", "history_days", "pause_seconds"}, "wellness.backfill")
    for clave in ("recovery_days", "history_days"):
        v = bf.get(clave)
        require(
            v is None or (isinstance(v, int) and not isinstance(v, bool) and v >= 1),
            f"wellness.backfill.{clave}: '{v}' debe ser un entero >= 1",
        )
    corta_w, larga_w = bf.get("recovery_days"), bf.get("history_days")
    if isinstance(corta_w, int) and isinstance(larga_w, int):
        require(
            corta_w <= larga_w,
            f"wellness.backfill: recovery_days ({corta_w}) no puede ser mayor que "
            f"history_days ({larga_w}). La corta es el repaso de cada arranque y "
            f"la larga el histórico completo; al revés, el arranque pediría días "
            f"que el backfill largo nunca ha cubierto y tardaría minutos cada vez",
        )
    if "pause_seconds" in bf:
        # Un 0 es legal: es lo que ponen los tests, que no tocan la red. Lo que
        # no puede ser es negativo, que sería un `time.sleep` reventando a mitad
        # del backfill largo después de veinte minutos de trabajo bueno.
        pausa = bf["pause_seconds"]
        require(
            isinstance(pausa, (int, float)) and not isinstance(pausa, bool) and pausa >= 0,
            f"wellness.backfill.pause_seconds: '{pausa}' debe ser un número >= 0",
        )

    # --- operadores: el nombre y, si lo lleva, aquello a lo que apunta -------
    #
    # Un operador mal escrito dentro de una regla del semáforo no daba error de
    # arranque: `evaluate_rule` levanta `RuleError` cuando lo encuentra, o sea a
    # las 06:30 y con el proceso ya decidiendo. Aquí se comprueba antes.
    #
    # Y con los dos operadores que apuntan a otro sitio se comprueba además el
    # destino, cada uno con su criterio:
    #   `_adaptive` -> el umbral tiene que estar declarado en
    #                  `adaptive_thresholds`. Su VALOR puede faltar un día
    #                  concreto (poco historial) y eso es legítimo.
    #   `_option`   -> la ruta tiene que resolver a un número HOY, con este
    #                  fichero delante. No hay un mañana en el que aparezca.
    def check_op_name(op: Any, where: str) -> None:
        base = str(op)
        if base.endswith(OPTION_SUFFIX):
            base = base[: -len(OPTION_SUFFIX)]
        elif base.endswith(ADAPTIVE_SUFFIX):
            base = base[: -len(ADAPTIVE_SUFFIX)]
        require(
            base in COMPARISONS,
            f"{where}: operador desconocido '{op}'. "
            f"Válidos: {', '.join(sorted(COMPARISONS))} "
            f"(y sus variantes '_adaptive' y '_option')",
        )

    def check_op_target(op: Any, operand: Any, where: str) -> None:
        nombre = str(op)
        if nombre.endswith(ADAPTIVE_SUFFIX):
            require(
                operand in adaptive,
                f"{where}: referencia el umbral adaptativo '{operand}', que no "
                f"está definido en adaptive_thresholds",
            )
        elif nombre.endswith(OPTION_SUFFIX):
            # Se resuelve con la MISMA función que usará el motor por la mañana.
            # Dos implementaciones separadas podrían dejar de coincidir, y esa es
            # exactamente la avería que este operador viene a cerrar.
            try:
                resolve_option(operand, data)
            except RuleError as e:
                require(False, f"{where}: {e}")

    # Recorre el `when` de una regla del semáforo, que es el único con gramática
    # anidada (`all`/`any`/`not` + señales).
    def check_when(node: Any, where: str) -> None:
        if isinstance(node, list):
            for item in node:
                check_when(item, where)
            return
        if not isinstance(node, dict):
            return
        for k, v in node.items():
            if k in ("all", "any", "not"):
                check_when(v, where)
                continue
            # `k` es una señal. Su valor es un diccionario de operadores, o el
            # azúcar `{fatigue: 7}`, que no lleva operador que revisar.
            if not isinstance(v, dict):
                continue
            for op, operand in v.items():
                if op == "consecutive_days":
                    continue
                check_op_name(op, f"{where}, señal '{k}'")
                check_op_target(op, operand, f"{where}, señal '{k}'")

    for light in ("red", "amber"):
        for rule in data["thresholds"].get(light, []):
            check_when(
                rule.get("when"),
                f"la regla '{rule.get('name', '<sin nombre>')}'",
            )

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
    #
    # `condition: "Hernia discal L4-L5"` vivía aquí sin que lo leyera nadie. En
    # una sección que se llama `safety` eso es peor que en cualquier otra: una
    # clave se lee como un interruptor, y quien la viera podría pensar que
    # cambiarla o quitarla cambia lo que el sistema permite. No cambiaba nada.
    # Ahora es un comentario del YAML -que es lo que era- y esta lista blanca
    # impide que vuelva en forma de clave, aquí o con cualquier otro nombre.
    safety = data.get("safety") or {}
    check_keys(safety, {"forbidden_in_hiit"}, "safety")
    check_keys(
        safety.get("forbidden_in_hiit") or {},
        {"reason", "template_ids", "name_patterns", "allow_exceptions"},
        "safety.forbidden_in_hiit",
    )
    forbidden = safety.get("forbidden_in_hiit") or {}
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
            for ex in routines.get(rkey, {}).get("exercises") or []:
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

    # --- recuento semanal de intensidad -------------------------------------
    conteo = (
        data["cycling"].get("recommendation", {}).get("intensity_count", {})
    )
    # `enabled` se exige EXPLÍCITO, y no basta con que sea verdadero.
    #
    # La validación de aquí abajo estaba colgada de `.get("enabled")`, mientras
    # que quien lo consume lee `.get("enabled", True)`. Los dos defectos
    # apuntan a lados contrarios: sin la clave, el validador se salta el bloque
    # entero y el motor lo aplica igualmente con los valores que se invente. Un
    # `enable:` por `enabled:` dejaba el fichero pasando la validación.
    # El bloque tiene que ESTAR. Antes daba igual que faltara porque lo único
    # que se perdía era un freno de más; ahora lo que se pierde es el recuento,
    # que es la única razón por la que el bloque sigue existiendo. Un
    # `intensity_cout:` mal tecleado dejaría el bloque ausente, el diccionario
    # vacío, y el número desaparecería del mensaje sin una sola queja. Apagarlo
    # a propósito se hace con `enabled: false`, que es una decisión escrita.
    require(
        bool(conteo),
        "falta cycling.recommendation.intensity_count. Para no contar hay que "
        "escribir `enabled: false` dentro del bloque, no quitar el bloque: "
        "ausente y desactivado se parecen mucho en el YAML y nada en el log.",
    )
    if conteo:
        require(
            isinstance(conteo.get("enabled"), bool),
            "intensity_count.enabled tiene que estar y ser true o false. Sin "
            "ella el validador se salta el bloque y el motor lo aplica igual.",
        )
    if conteo.get("enabled"):
        require(
            conteo.get("week_starts_on") in WEEKDAYS,
            f"intensity_count.week_starts_on '{conteo.get('week_starts_on')}' no es válido",
        )

    # LISTA NEGRA: las claves de cuando esto era un presupuesto y recortaba.
    #
    # Se comprueba SIEMPRE, esté el bloque encendido, apagado o ausente, y
    # también bajo el nombre viejo `intensity_budget`. Una clave muerta que se
    # ignora en silencio es peor que una clave mal escrita: quien escriba
    # `weekly_limit: 2` va a creer que se ha puesto un tope de dos sesiones
    # intensas a la semana, el fichero va a validar, y no va a pasar nada de
    # nada. Es el mismo fallo que el `enable:`/`enabled:` de aquí arriba pero
    # al revés y más caro, porque el silencio cae del lado de creerse protegido.
    #
    # El bloque entero bajo el nombre viejo también revienta: `intensity_budget`
    # ya no lo lee nadie, así que dejarlo puesto sería tener la configuración
    # del recuento escrita en un sitio que el motor no mira.
    viejo = data["cycling"].get("recommendation", {}).get("intensity_budget")
    require(
        viejo is None,
        "cycling.recommendation.intensity_budget ya no existe: se llama "
        "intensity_count y solo cuenta, no limita. Dejarlo aquí es configurar "
        "algo que el motor no lee.",
    )
    for muerta in (
        "weekly_limit",
        "on_budget_exhausted",
        "max_intense_rides_per_weekend",
        "require_green_for_intense",
    ):
        require(
            muerta not in conteo,
            f"intensity_count.{muerta} es una clave de cuando el recuento "
            f"recortaba la salida del fin de semana. Ya no recorta: informa. "
            f"Para frenar por carga acumulada está thresholds.amber.carga_acumulada, "
            f"que mira la carga de Garmin contra tu propio percentil 90.",
        )

    # --- rutinas ------------------------------------------------------------
    #
    # Las que algún día de alguna variante programa como fuerza. Se miran TODAS
    # las variantes y no solo la activa: `dia_3` solo existe en `summer`, y
    # validar únicamente la variante en curso dejaría el fichero pasando en
    # invierno y fallando el día que se cambie de temporada, que es cuando
    # menos se quiere descubrir un error de configuración.
    rutinas_de_fuerza = {
        plan.get("strength")
        for vdata in (cal.get("variants") or {}).values()
        for dia, plan in vdata.items()
        if dia != "description" and isinstance(plan, dict) and plan.get("strength")
    }
    for rkey, routine in routines.items():
        # Una rutina vacía no daba error, y `all([])` es True: "no hay nada que
        # comprobar" se lee igual que "todo comprobado y correcto". Hoy no tiene
        # consecuencia -sin ejercicios no hay nada que subir, y la puerta se
        # cierra por falta de registro-, pero llegar hasta ahí para que la
        # vacuidad se resuelva bien por casualidad es demasiado camino. Y el
        # síntoma sería una mañana sin sesión, que es indistinguible de un día
        # de descanso: exactamente el fallo que no puede quedar mudo.
        require(
            isinstance(routine.get("exercises"), list) and routine["exercises"],
            f"rutina '{rkey}': 'exercises' está vacía o no es una lista. Una "
            f"rutina sin ejercicios se escribiría en Hevy dejándola en blanco y "
            f"la mañana saldría sin sesión, sin un solo error",
        )
        # `standalone: false` vivía aquí sin que lo leyera nadie, en los dos
        # bloques HIIT. No era una opción: repetía en forma de interruptor algo
        # que deciden `calendar` -que no los nombra- y `hiit.blocks` -que los
        # ata a dia_1 y dia_2-. Un `standalone: true` no habría programado nada.
        check_keys(
            routine,
            {"title", "hevy_routine_id", "exercises", "focus"},
            f"rutina '{rkey}'",
        )
        # `focus` ya no es decorativo: es el subtítulo del encabezado del
        # mensaje. Se exige donde se va a leer y se prohíbe donde no.
        if rkey in rutinas_de_fuerza:
            require(
                bool(str(routine.get("focus") or "").strip()),
                f"rutina '{rkey}': falta 'focus'. Es el subtítulo de la sesión "
                f"en el mensaje de la mañana ('Día 1 (sesión completa) — Tren "
                f"inferior + core') y la única frase del fichero que dice de "
                f"qué va el día. Sin él el encabezado se acorta sin avisar",
            )
        elif "focus" in routine:
            require(
                False,
                f"rutina '{rkey}': lleva 'focus' pero no la programa ningún "
                f"día de `calendar` como fuerza, así que nadie lo enseñaría. "
                f"Los bloques HIIT se añaden al final de otra sesión y usan el "
                f"encabezado de esa. Bórralo.",
            )
        keys_here: set[str] = set()
        # `or []` y no `, []`: con `exercises:` a secas YAML devuelve None, y
        # el validador reventaba con un TypeError en vez de dar la frase.
        for ex in routine.get("exercises") or []:
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
                # Una serie efectiva TIENE que pedir reps o segundos. Es lo
                # mismo que exige `_alcanza` al reconciliar, dicho aquí en vez
                # de allí, y la diferencia entre las dos es toda la que hay
                # entre un arranque que se niega y un `HevyError` a las 22:30
                # en un hilo de APScheduler.
                #
                # No es hipotético: `hiit_dia_1` / `suitcase_carry` llevaba tres
                # series `{weight_kg: null, distance_m: null}`, que no prescriben
                # NADA medible. Mientras `hiit.enabled` estuvo en false nadie lo
                # notó, porque el bloque no entraba en el plan. Al activarlo, la
                # reconciliación de la noche reventaba entera -y con ella el
                # cumplimiento, las rachas y la progresión de la sesión de
                # fuerza de ese día-, por un ejercicio que ni siquiera progresa.
                #
                # El peso no cuenta como magnitud medible: "60 kg" no dice si la
                # serie se terminó. Lo dice `_alcanza` y aquí se respeta el mismo
                # criterio, porque tener dos sería peor que no tener ninguno.
                if str(stype or "").lower() != "warmup":
                    require(
                        s.get("reps") is not None or s.get("duration_s") is not None,
                        f"rutina '{rkey}' / '{k}': la serie {i} no pide ni reps "
                        f"ni duration_s, así que no hay forma de saber si se "
                        f"completó. La reconciliación no puede juzgarla y "
                        f"revienta al intentarlo. Ponle una de las dos.",
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

    # `rep_apply_to` decide si una subida de reps respeta la rampa o la aplana.
    # Un valor mal escrito caería al defecto sin decir nada, y el defecto de
    # `volume` (`all_sets`) es justo el contrario del de `double`
    # (`lowest_first`): la errata no se notaría como un fallo, se notaría como
    # que el esquema se aplana solo al cabo de unas semanas.
    for modo, defecto in (("double", "lowest_first"), ("volume", "all_sets")):
        valor = str((modes.get(modo) or {}).get("rep_apply_to", defecto))
        require(
            valor in VALID_REP_APPLY_TO,
            f"progression.modes.{modo}.rep_apply_to '{valor}' no es válido "
            f"(esperado: {sorted(VALID_REP_APPLY_TO)})",
        )

    # `apply_to` es lo mismo para la carga, y la errata natural es `top_sets`
    # en plural, que caería en la rama de subir TODAS las series.
    apply_to = str(prog.get("default_apply_to", "top_set"))
    require(
        apply_to in {"top_set", "all_sets"},
        f"progression.default_apply_to '{apply_to}' no es válido "
        "(esperado: 'top_set' o 'all_sets')",
    )

    sets_mode = modes.get("sets") or {}
    then_default = str(sets_mode.get("then", "double"))
    require(
        then_default in {"load", "double"},
        f"progression.modes.sets.then '{then_default}' no es válido: "
        "al agotar el techo de series solo se puede pasar a 'load' o 'double'",
    )

    # --- adopción de la carga ejecutada --------------------------------------
    # Los tres números de aquí son frenos, y un freno mal puesto no da error: se
    # queda quieto. Un `down_after_sessions: 0` bajaría el objetivo con la
    # primera serie mal apuntada; un `max_jump_pct: 5` dejaría pasar un 600 por
    # un 60. En los dos casos el sistema seguiría funcionando y escribiendo la
    # rutina cada mañana.
    ael = prog.get("adopt_executed_load") or {}
    require(
        isinstance(ael, dict),
        "progression.adopt_executed_load debe ser un mapa",
    )
    if isinstance(ael, dict) and ael:
        n_bajada = ael.get("down_after_sessions", 3)
        require(
            _es_num(n_bajada) and int(n_bajada) >= 1,
            f"progression.adopt_executed_load.down_after_sessions "
            f"({n_bajada!r}) debe ser un entero >= 1. Con 0 el objetivo bajaría a "
            "la primera sesión floja, que casi siempre es la máquina ocupada o "
            "una serie mal apuntada; para desactivar la adopción está 'enabled'",
        )
        salto_kg = ael.get("max_jump_kg", 5)
        require(
            _es_num(salto_kg) and float(salto_kg) > 0,
            f"progression.adopt_executed_load.max_jump_kg ({salto_kg!r}) debe ser "
            "mayor que 0: con 0 no se adoptaría ningún cambio y el mecanismo "
            "entero quedaría desconectado sin decirlo",
        )
        salto_pct = ael.get("max_jump_pct", 0.20)
        require(
            _es_num(salto_pct) and 0 < float(salto_pct) <= 1,
            f"progression.adopt_executed_load.max_jump_pct ({salto_pct!r}) debe "
            "estar entre 0 y 1 (0.20 = 20%). Por encima de 1 el tope dejaría "
            "pasar más del doble del peso actual, que es lo que este límite "
            "existe para frenar",
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

    # --- erratas dentro de `progression` -------------------------------------
    #
    # `progression.modes.*` no tenía lista blanca, y ahí es donde más caro sale:
    # `rep_aply_to` con una pe se leería como ausente, el `.get()` devolvería el
    # defecto `all` y la rampa 12/10/10 se aplanaría a 13/11/11 en vez de subir
    # solo la serie baja. El YAML seguiría diciendo `lowest_first` y el usuario
    # vería una progresión distinta de la que pidió, sin un solo error. Es
    # exactamente la avería que se acaba de arreglar en el código, ahora
    # cerrada también por el lado de la escritura.
    check_keys(
        prog,
        {
            "adopt_executed_load",
            "brakes",
            "deload",
            "gate",
            "modes",
            "volume_safety",
            "default_apply_to",
            "default_clean_sessions_required",
            "default_increment_kg",
            "default_progression_type",
        },
        "progression",
    )
    check_keys(
        prog.get("adopt_executed_load") or {},
        {"enabled", "down_after_sessions", "max_jump_kg", "max_jump_pct"},
        "progression.adopt_executed_load",
    )
    check_keys(modes, {"double", "volume", "sets"}, "progression.modes")
    for nombre, permitidas in (
        ("double", {"deduced_span", "rep_apply_to", "rep_increment"}),
        ("volume", {"max_reps", "max_seconds", "on_ceiling_notify",
                    "rep_apply_to", "reps_increment", "seconds_increment"}),
        ("sets", {"clean_sessions_required", "max_sets", "then"}),
    ):
        check_keys(modes.get(nombre) or {}, permitidas, f"progression.modes.{nombre}")

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
        for op, operand in when.items():
            check_op_name(op, where)
            check_op_target(op, operand, where)

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
    # Una errata aquí -`lookback_dias`- dejaría la ventana en su valor por
    # defecto y el YAML seguiría diciendo otra cosa, que es exactamente la
    # avería que acaba de cerrarse en esta sección.
    check_keys(fetch, {"lookback_days", "backfill_days"}, "cycling.fetch")

    # `hr_zones` era el último bloque de `cycling` que no leía nadie:
    #
    #     hr_zones:
    #       source: garmin          # garmin | computed
    #       max_hr: null            # solo necesario si source: computed
    #
    # Se rechaza en vez de ignorarse, como `cold_start`, y por el mismo motivo:
    # el daño no lo hace la clave sobrante, lo hace la alternativa que anuncia.
    # `source: computed` no existe en el código, así que quien lo escribiera y
    # rellenara `max_hr` cambiaría el fichero, lo releería convencido de haber
    # cambiado el criterio, y seguiría clasificando con las zonas de Garmin sin
    # que nada se lo dijera. El reparto por zonas sale de `timeInZones` de cada
    # actividad y no hay nada que configurar.
    require(
        "hr_zones" not in ((data.get("cycling") or {})),
        "cycling.hr_zones no lo leía nadie: las zonas de FC vienen tal cual de "
        "Garmin (`timeInZones` por actividad) y no hay ningún 'source: "
        "computed' implementado, así que rellenar 'max_hr' no cambiaría ni una "
        "clasificación. Bórralo.",
    )
    # Y con eso la sección queda cerrada: cualquier clave nueva aquí o es una
    # errata o es una opción que alguien ha escrito esperando que se lea.
    check_keys(
        data.get("cycling") or {},
        {
            "activity_types",
            "classification",
            "classification_fallback",
            "fetch",
            "load",
            "recommendation",
            "weekend",
        },
        "cycling",
    )

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
            "perception_notice_time",
            "garmin_retry",
        },
        "schedule",
    )
    HORAS = (
        "garmin_fetch_time",
        "fallback_decision_time",
        "evening_summary_time",
        "perception_notice_time",
    )
    for clave in HORAS:
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

    # El aviso de percepción va DESPUÉS de la decisión, y ponerlo antes no
    # rompería nada visible: el trabajo se ejecutaría, el mensaje saldría y todo
    # parecería correcto. Lo que se perdería es el motivo entero por el que el
    # aviso va aparte -llegar primero lo convierte en el preámbulo del plan de
    # hoy, o sea en un argumento sobre si entrenar- y además a esa hora puede que
    # no haya check-in todavía, que es el dato del que sale el esfuerzo percibido
    # de ayer. Un fallo así solo se nota leyendo los mensajes semanas después.
    def _min(clave: str, defecto: str) -> int | None:
        texto = str(sched.get(clave) or defecto).split(":")
        try:
            return int(texto[0]) * 60 + int(texto[1])
        except (IndexError, ValueError):
            return None  # ya lo ha dicho el validador de formato de arriba

    aviso, decision = _min("perception_notice_time", "09:30"), _min(
        "fallback_decision_time", "09:00"
    )
    if aviso is not None and decision is not None:
        require(
            aviso > decision,
            f"schedule.perception_notice_time ({sched.get('perception_notice_time')}) "
            f"tiene que ser posterior a schedule.fallback_decision_time "
            f"({sched.get('fallback_decision_time')}): el aviso habla de AYER y "
            f"tiene que llegar cuando el plan de hoy ya se ha mandado, o los dos "
            f"mensajes se leen como uno solo y el contador acaba pareciendo un "
            f"argumento a favor o en contra de entrenar hoy",
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

    # --- capa de tendencia ---------------------------------------------------
    #
    # Sin defectos, y a propósito. Estos cinco números deciden durante meses qué
    # se considera una mala racha, y son ABSOLUTOS -no adaptativos- por el motivo
    # largo que está escrito junto a ellos en el YAML. Un defecto en el código
    # sería justo la forma de que dejaran de coincidir con lo que dice el
    # archivo: alguien borra una clave, el sistema sigue arrancando, y el número
    # que de verdad gobierna la capa ya no está escrito en ninguna parte.
    trend = data.get("trend") or {}
    check_keys(
        trend,
        {
            "enabled",
            "racha_min",
            "ventana_corta_dias",
            "ventana_larga_dias",
            "delta_pp_min",
            "motivo_semanas_min",
            "sueno",
        },
        "trend",
    )
    require(
        isinstance(trend.get("enabled"), bool),
        f"trend.enabled: '{trend.get('enabled')}' tiene que ser true o false. "
        f"Es el único interruptor de la capa y no puede quedar implícito",
    )
    for clave, minimo in (
        ("racha_min", 2),
        ("ventana_corta_dias", 7),
        ("ventana_larga_dias", 14),
        ("delta_pp_min", 1),
        ("motivo_semanas_min", 2),
    ):
        v = trend.get(clave)
        require(
            _es_num(v) and v >= minimo,
            f"trend.{clave}: '{v}' tiene que ser un número >= {minimo}. Se "
            f"calibra a mano contra el histórico con "
            f"`scripts/replay_semaforo.py --tendencia`, no se deduce",
        )
    corta, larga = trend.get("ventana_corta_dias"), trend.get("ventana_larga_dias")
    if _es_num(corta) and _es_num(larga):
        require(
            corta < larga,
            f"trend.ventana_corta_dias ({corta}) tiene que ser menor que "
            f"ventana_larga_dias ({larga}): el detector compara el último mes "
            f"CONTRA el trimestre, y al revés -o iguales- daría siempre cero y "
            f"no fallaría nunca, que es la forma más silenciosa de no medir nada",
        )
    sueno = trend.get("sueno") or {}
    check_keys(sueno, {"caida_score_min", "caida_min_min"}, "trend.sueno")
    for clave in ("caida_score_min", "caida_min_min"):
        v = sueno.get(clave)
        require(
            _es_num(v) and v > 0,
            f"trend.sueno.{clave}: '{v}' tiene que ser un número > 0. Con 0 el "
            f"cualificador diría que el sueño ha empeorado todos los días en que "
            f"la media se mueva un decimal",
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
