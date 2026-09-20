"""Carga, validación y hash de config.yaml.

Tres responsabilidades:

1. Cargar el YAML.
2. Validar las referencias cruzadas. El YAML es grande y está lleno de claves
   que apuntan a otras claves (la rotación nombra rutinas, las reglas
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
    CLAVE_PRECAUCION,
    COMPARISONS,
    OPTION_SUFFIX,
    REGLA_SIN_DATOS,
    RuleError,
    resolve_option,
)
from app.engine.signals import (
    CLAVE_APETECE,
    CLAVE_SESION_ELEGIDA,
    CLAVE_VOY_A_ENTRENAR,
    ELECCIONES_SIN_FUERZA,
)
# Y la MISMA función con la que el motor averigua de qué habla una regla. Se
# importa por lo mismo que `resolve_option` de arriba: aquí hace falta saber qué
# señales mira un `when` para prohibir que mire las preguntas de Sí/No, y esa
# lectura del árbol ya existe. Copiarla sería firmar que las dos copias van a
# decir lo mismo dentro de un año, cuando el `when` admita un operador nuevo que
# solo una de las dos entienda: el validador aprobaría una regla que el motor sí
# sabe evaluar, y la garantía de que el semáforo no puede mirar `will_train`
# quedaría en papel mojado justo en el caso raro.
from app.engine.tendencia import senales_de_regla
# El catálogo de lo que `build_signals` fabrica de verdad, leído del mismo sitio
# que lo fabrica. Una lista de nombres copiada aquí se quedaría atrás a la
# primera señal nueva y entonces el validador rechazaría nombres BUENOS, que es
# peor que no validar: el sistema no arranca y el error acusa a un fichero de
# configuración que está bien.
from app.engine.signals import senales_con_serie, senales_producidas
# La MISMA función que usa el motor por la mañana para decidir cuántos días de
# salidas hay que tener en caché. Se importa en vez de reimplementar el `max`
# aquí: dos versiones del mismo criterio en dos ficheros divergen a la primera
# ventana nueva, y el resultado sería un validador que aprueba una configuración
# que la caché no puede sostener. Mismo motivo por el que `_option` se valida
# llamando a `resolve_option` de arriba.
from app.integrations.activity_cache import dias_adaptativos, ventanas_declaradas


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


def opciones_selector(data: dict[str, Any]) -> list[str]:
    """Todo lo que se puede elegir por la mañana, en orden de pantalla.

    Recibe el YAML crudo y no un `Config` porque esta misma regla la necesita
    `repository.opciones_del_config` para validar lo que llega del formulario, y
    allí el `config` puede venir en dos formas -el objeto y el diccionario
    pelado de algunos tests-. Escrita aquí y llamada desde allí hay UNA
    implementación. Escrita dos veces habría dos listas capaces de decir cosas
    distintas, y la peor de las dos discordancias posibles: la que valida lo que
    se guarda no sería la que dibuja lo que se puede contestar.

    EL CICLO SALE DE `rotation.order` Y NO SE REPITE EN LA SECCIÓN DEL SELECTOR.
    Repetirlo sería poder escribirlo distinto: una lista que enumerase los días
    a mano se quedaría sin el `dia_4` que alguien añadiera al ciclo, y la
    pantalla ofrecería tres opciones mientras el sistema rota entre cuatro. Lo
    único que el selector pone de su cosecha es lo que NO es fuerza -bici,
    otro-, que por definición no puede salir de la rotación.
    """
    dias = [str(k) for k in ((data.get("rotation") or {}).get("order") or [])]
    sel = data.get("checkin_selector") or {}
    sin_fuerza = [str(o["key"]) for o in (sel.get("sin_fuerza") or []) if o.get("key")]
    return dias + sin_fuerza


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

    def rotation_order(self) -> list[str]:
        """El ciclo de fuerza, en orden. Ver la sección `rotation` del YAML.

        Aquí había un `active_calendar()` que devolvía la variante de calendario
        en curso. No hay variantes ni días asignados: lo que toca hoy sale de
        cuál fue la última de estas rutinas que se EJECUTÓ en Hevy.
        """
        return list((self._data.get("rotation") or {}).get("order") or [])

    def slider_keys(self) -> list[str]:
        return [s["key"] for s in self._data.get("checkin_sliders", [])]

    def pregunta_keys(self) -> list[str]:
        """Las de Sí/No, que se guardan y se cuentan pero no deciden nada.

        Separadas de `slider_keys` y no juntas con una bandera, porque esta
        lista es justo la que NO se le pasa al validador de reglas: el semáforo
        solo puede nombrar deslizadores. Fundirlas en un solo método y filtrar
        después convertiría esa garantía en un `if` que alguien puede olvidar.
        """
        return [p["key"] for p in self._data.get("checkin_preguntas", [])]

    def selector(self) -> dict[str, Any]:
        """La sección del selector de sesión, cruda."""
        return self._data.get("checkin_selector") or {}

    def opciones_selector(self) -> list[str]:
        """Todo lo que se puede elegir hoy: el ciclo entero, más bici y otro."""
        return opciones_selector(self._data)

    def checkin_keys(self) -> list[str]:
        """Todo lo que contesta el usuario por la mañana, en orden de pantalla.

        Para quien solo necesita recorrer las respuestas -las series, el
        histórico, la fotografía de señales- y no tiene nada que decidir sobre
        quién puede leerlas.

        EL SELECTOR NO ESTÁ AQUÍ, y no es un olvido. Esta lista es la que
        `build_signals` vuelca en `signals.values` y la que recorre el análisis
        para sacar series y medias, y todo lo que hay en ella es un número o un
        booleano. La respuesta del selector es una cadena -`dia_2`, `bici`- y lo
        que rompe no es el color del día: es el `float()` de la primera media
        que alguien calcule sobre el histórico completo.

        Quien la quiera va a `Signals.sesion_elegida`, que es donde está, o a la
        columna `checkins.chosen_session`, que es de donde sale.
        """
        return self.slider_keys() + self.pregunta_keys()

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
        # Obligatoria por lo mismo que `trend`: el motor busca `will_train` por
        # su nombre para saber si hoy prescribe sesión o no. Sin la sección, esa
        # señal no la escribe nadie, se queda en `None` -que es "no me lo han
        # dicho"- y el mensaje vuelve a prescribir todos los días. Funcionaría,
        # y eso es lo malo: sería el comportamiento de antes con cara de normal.
        "checkin_preguntas",
        # Y obligatoria por la misma razón una vez más: sin el selector, el
        # formulario no pregunta qué se va a hacer, `chosen_session` se queda
        # nula todos los días y el sistema vuelve a proponer sin enterarse nunca
        # de que lo propuesto no era lo que se iba a hacer. Otra vez el
        # comportamiento de antes con cara de normal.
        "checkin_selector",
        "thresholds",
        "actions",
        "progression",
        "rotation",
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
        "checkin_preguntas",
        "checkin_selector",
        "checkin_comment",
        "thresholds",
        "actions",
        "safety",
        "progression",
        "rotation",
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
        "metrics",
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

    # `calendar` era el calendario fijo: variantes de temporada y un día de la
    # semana por rutina. Se ha sustituido entero por `rotation`. Se rechaza con
    # nombre propio en vez de dejarlo caer en "sección desconocida" porque lo
    # que hay que entender no es que la clave sobre, sino que lo que había
    # dentro YA NO SE APLICA: un `monday: {strength: dia_1}` olvidado aquí no
    # programaría nada, y el error genérico no lo dejaría claro.
    require(
        "calendar" not in data,
        "calendar ya no existe: ni las variantes de temporada ni los días "
        "asignados. La fuerza va en ciclo y el ciclo lo lleva `rotation.order`, "
        "leyendo de Hevy cuál fue la última sesión EJECUTADA. Si querías volver "
        "a una semana fija, esto no la restaura: bórralo y ajusta `rotation`.",
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
    pregunta_keys = {p.get("key") for p in (data.get("checkin_preguntas") or [])}

    # --- sliders ------------------------------------------------------------
    require(
        len(slider_keys) == len(data["checkin_sliders"]),
        "hay claves duplicadas en checkin_sliders",
    )

    # --- las dos preguntas de Sí/No -----------------------------------------
    require(
        len(pregunta_keys) == len(data.get("checkin_preguntas") or []),
        "hay claves duplicadas en checkin_preguntas",
    )
    # La misma clave en las dos listas sería el peor de los mundos: el
    # formulario la pintaría dos veces, y el validador de reglas la dejaría
    # entrar en el semáforo por la puerta de los deslizadores mientras el resto
    # del sistema la trata como una pregunta que no decide.
    solapadas = sorted(slider_keys & pregunta_keys)
    require(
        not solapadas,
        f"estas claves están en `checkin_sliders` y en `checkin_preguntas` a la "
        f"vez: {solapadas}. Una respuesta o puede mover el semáforo o no puede, "
        f"y estando en las dos listas puede y no puede.",
    )
    # Estas dos claves el código las busca POR SU NOMBRE, y el nombre está en
    # `app/engine/signals.py`, no aquí. Si una desapareciera del YAML nadie
    # reventaría: `sig.get(...)` devolvería `None` para siempre -que el motor lee
    # como "no me lo han dicho", el tercer estado- y el sistema seguiría
    # funcionando exactamente como antes de que estas preguntas existieran. No es
    # un error, es el comportamiento anterior con cara de normal, que es la clase
    # de avería que este fichero existe para que no ocurra.
    #
    # Las etiquetas sí pueden reescribirse a gusto; lo que no puede es faltar la
    # clave.
    for clave, para_que in (
        (
            CLAVE_VOY_A_ENTRENAR,
            "es la que decide si el mensaje del día prescribe la sesión o solo "
            "informa del estado",
        ),
        (
            CLAVE_APETECE,
            "es la mitad de la que se calcula la discordancia (apetece pero no "
            "voy, o no apetece y voy), y sin ella esa señal se queda muda sin "
            "decir que se ha quedado muda",
        ),
    ):
        require(
            clave in pregunta_keys,
            f"falta la pregunta '{clave}' en `checkin_preguntas`. {para_que}, y "
            f"el código la busca por ese nombre.",
        )

    # --- el selector de sesión ----------------------------------------------
    #
    # Lo que se comprueba aquí es poco porque la sección dice poco: los días del
    # ciclo NO se enumeran en ella -salen de `rotation.order`- y por eso no puede
    # quedarse desincronizada del ciclo, que es el fallo que tendría si los
    # repitiera. Lo único suyo son las dos opciones que no son fuerza.
    sel = data["checkin_selector"]
    require(isinstance(sel, dict), "checkin_selector tiene que ser un mapa")
    if isinstance(sel, dict):
        check_keys(sel, {"key", "label", "nota", "sin_fuerza"}, "checkin_selector")
        # La clave la busca el código por su nombre, como `will_train`. Escrita
        # distinta aquí, el formulario mandaría un campo que `upsert_checkin`
        # rechaza, y el check-in entero fallaría con un error de clave
        # desconocida: ruidoso, que es lo que se quiere.
        require(
            sel.get("key") == CLAVE_SESION_ELEGIDA,
            f"checkin_selector.key tiene que ser '{CLAVE_SESION_ELEGIDA}': es el "
            f"nombre de la columna de `checkins` y el que el motor busca. "
            f"Está puesto '{sel.get('key')}'.",
        )
        require(
            bool((sel.get("label") or "").strip()),
            "checkin_selector.label está vacío: es el enunciado que se lee en el "
            "móvil encima de las opciones.",
        )

        sin_fuerza = sel.get("sin_fuerza") or []
        claves_sin_fuerza = [
            o.get("key") for o in sin_fuerza if isinstance(o, dict)
        ]
        for o in sin_fuerza:
            if isinstance(o, dict):
                check_keys(o, {"key", "label"}, "checkin_selector.sin_fuerza")
        # Éstas sí las busca el código una por una: `bici` no prescribe fuerza
        # pero deja dicho que hubo actividad, y `otro` deja dicho que se entrenó
        # sin plan. Si faltara una, el formulario no la ofrecería y el caso que
        # cubre volvería a ser indistinguible de no contestar.
        faltan = [c for c in ELECCIONES_SIN_FUERZA if c not in claves_sin_fuerza]
        require(
            not faltan,
            f"a checkin_selector.sin_fuerza le faltan {faltan}. El motor las "
            f"busca por ese nombre para saber que esa elección no prescribe "
            f"fuerza; sin ellas el formulario solo dejaría elegir rutinas del "
            f"ciclo y un día de bici no se podría declarar.",
        )
        # Una opción que se llamara como un día del ciclo haría que elegirla
        # fuese ambiguo: el motor la trataría como rutina por estar en `order` y
        # como no-fuerza por estar aquí, y cuál gana depende de en qué orden se
        # pregunte.
        chocan = sorted(set(claves_sin_fuerza) & set(data["rotation"].get("order") or []))
        require(
            not chocan,
            f"checkin_selector.sin_fuerza usa {chocan}, que también están en "
            f"`rotation.order`. Una elección o es una rutina del ciclo o no lo "
            f"es, y llamándose igual es las dos cosas.",
        )
        # Y tampoco puede llamarse como una respuesta del formulario: las tres
        # acaban siendo columnas de `checkins`, y `upsert_checkin` reparte por
        # nombre.
        chocan_form = sorted({sel.get("key")} & (slider_keys | pregunta_keys))
        require(
            not chocan_form,
            f"checkin_selector.key usa {chocan_form}, que ya está en "
            f"`checkin_sliders` o en `checkin_preguntas`.",
        )

    # NINGUNA REGLA DEL SEMÁFORO PUEDE NOMBRARLAS, Y ESTO ES LO QUE LO IMPIDE
    # ----------------------------------------------------------------------
    # Los frenos de progresión y los disparadores de reglas especiales ya se
    # validan contra `slider_keys` unos cientos de líneas más abajo, así que
    # esos dos caminos están cerrados por construcción: una pregunta no es un
    # deslizador y no pasa. El camino que estaba ABIERTO era el principal -las
    # reglas de `thresholds`-, cuyas señales no se validaban contra nada.
    #
    # Se camina el árbol `when` en vez de mirar `requires`, y se camina con LA
    # MISMA función que usa el motor para saber de qué habla una regla, no con
    # una copia escrita aquí: `requires` es documentación -puede quedarse corto
    # sin que nada se rompa- y una segunda versión del recorrido divergiría a la
    # primera forma de `when` que alguien añada, dejando este permiso abierto
    # justo en el caso nuevo.
    reglas_de_luz = [
        r
        for nivel in ("red", "amber")
        for r in (data["thresholds"].get(nivel) or [])
        if isinstance(r, dict)
    ]
    for rule in reglas_de_luz:
        # El selector tampoco, y por un motivo distinto del de las preguntas: no
        # es que no deba decidir el color, es que ni siquiera llega a `values`.
        # Una regla que lo nombrara no daría error ni color raro -se saltaría
        # como "falta el dato", todos los días, para siempre- y eso en el log se
        # lee igual que un día sin reloj.
        if CLAVE_SESION_ELEGIDA in senales_de_regla(rule):
            errors.append(
                f"regla '{rule.get('name')}': mira '{CLAVE_SESION_ELEGIDA}', que "
                f"es el selector del check-in y no es una señal evaluable. Es una "
                f"cadena -`dia_2`, `bici`- y no entra en `signals.values`, así que "
                f"esta regla no dispararía nunca: se anotaría como saltada por "
                f"falta de datos todas las mañanas, que en el log no se distingue "
                f"de un día en que el reloj no sincronizó."
            )
        usadas = sorted(senales_de_regla(rule) & pregunta_keys)
        require(
            not usadas,
            f"regla '{rule.get('name')}': mira {usadas}, que son preguntas del "
            f"check-in y no pueden decidir el semáforo. «No me apetece» y «hoy "
            f"no voy» son decisiones, no medidas: un color que dependiera de "
            f"ellas te daría la razón llamándolo fisiología, y encima enseñaría "
            f"a contestar el formulario según el color que uno quiere que "
            f"salga. Si lo que quieres es que las ganas pesen, para eso está el "
            f"deslizador `training_desire`.",
        )

    # --- rotación -----------------------------------------------------------
    # Aquí vivía la validación del calendario fijo: variantes de temporada, un
    # día de la semana por rutina y siete comprobaciones para que ningún lunes
    # se quedara en blanco. Miraba tan de cerca porque un descuido ahí no se
    # notaba nunca... y aun así el descuido que de verdad pasó se le escapó
    # entero: el calendario estaba impecable, con sus siete días escritos, y
    # sencillamente no nombraba `dia_3` en ningún sitio. Todas las sesiones del
    # Día 3 entraron como entrenos sueltos «que ese día no tocaba fuerza» y sus
    # cargas no se movieron nunca. Un fichero puede estar bien formado y decir
    # una semana que no es la de verdad.
    #
    # `rotation` no puede tener ese fallo por construcción: no hay días que
    # olvidar, solo un ciclo, y una rutina que no está en el ciclo no está en el
    # programa. Lo que queda por comprobar es poco y se comprueba entero.
    rot = data["rotation"]
    require(isinstance(rot, dict), "rotation tiene que ser un mapa con la clave `order`")
    check_keys(rot, {"order"}, "rotation")

    orden = rot.get("order")
    require(
        isinstance(orden, list) and bool(orden),
        "rotation.order tiene que ser una lista con al menos una rutina. Vacía, "
        "no habría ninguna sesión de fuerza que escribir y el sistema se "
        "quedaría callado en vez de dar error.",
    )
    require(
        len(set(orden)) == len(orden),
        f"rotation.order repite rutinas: {sorted({k for k in orden if orden.count(k) > 1})}. "
        f"El puntero sale de la ÚLTIMA sesión ejecutada de la lista, así que una "
        f"rutina que aparece dos veces no tiene un «siguiente» definido: el ciclo "
        f"saltaría siempre al primero de los dos sitios y el segundo tramo no se "
        f"visitaría nunca.",
    )
    for key in orden:
        require(
            key in routines,
            f"rotation.order nombra la rutina '{key}', que no existe en `routines`. "
            f"Existen: {sorted(routines)}.",
        )

    # --- reglas del semáforo ------------------------------------------------
    #
    # El interruptor del ámbar por precaución. Se valida el TIPO y no solo la
    # presencia porque el defecto es `True` y la lectura es
    # `thresholds.get(...) is not False`... es decir, cualquier cosa que no sea
    # un booleano se comportaría como encendido. Escribir `ambar_sin_datos: "no"`
    # -que es lo que sale solo si uno escribe rápido- dejaría la precaución
    # puesta creyendo haberla quitado, y la única señal sería un ámbar
    # inexplicable en la primera mañana en que el reloj llegue tarde.
    if CLAVE_PRECAUCION in data["thresholds"]:
        require(
            isinstance(data["thresholds"][CLAVE_PRECAUCION], bool),
            f"thresholds.{CLAVE_PRECAUCION} tiene que ser true o false, y vale "
            f"{data['thresholds'][CLAVE_PRECAUCION]!r}. Es el interruptor que "
            f"decide si un verde decidido sin los datos del reloj sale ámbar.",
        )

    seen_names: set[str] = set()
    for light in ("red", "amber"):
        for rule in data["thresholds"].get(light, []):
            name = rule.get("name")
            require(bool(name), f"hay una regla en thresholds.{light} sin 'name'")
            require(name not in seen_names, f"nombre de regla duplicado: '{name}'")
            # `resaca_finde` frenaba el lunes contando horas de bici del fin de
            # semana. Se ha borrado porque disparaba con la HRV alta y la carga
            # muy por debajo del propio p90, y porque era el único freno por
            # cuenta que llegaba a la sesión de fuerza. Se rechaza por nombre en
            # vez de dejar que se reescriba: el resto de la validación la daría
            # por buena -la gramática es correcta y los operadores existen- y
            # volvería a decidir los lunes sin que nada lo dijera.
            require(
                name != "resaca_finde",
                "thresholds.{}.resaca_finde ya no existe. Frenaba el lunes por "
                "horas de bici del fin de semana, y sobre 180 días disparó dos "
                "veces como única causa del ámbar con cero salidas intensas, la "
                "HRV en 1,62 y la carga de tres días a un cuarto de tu p90. "
                "Contaba actividades en vez de mirar el cuerpo, que es lo que "
                "decide de verdad; el cuerpo ya tiene sus cuatro reglas."
                .format(light),
            )
            # Y la que la sustituía, por el mismo motivo de fondo. Se rechaza por
            # nombre porque su gramática era impecable -umbral contra el propio
            # percentil, que suena a lo correcto- y la validación entera la daría
            # por buena. Lo que estaba mal no era cómo se medía sino qué se medía.
            require(
                name != "carga_acumulada",
                "thresholds.{}.carga_acumulada ya no existe. Ponía el día en "
                "ámbar cuando la carga de bici pasaba tu p90 habitual. Sobre 185 "
                "días, la única ventana con significación (k=1, p = 0,0006) "
                "aportaba por su cuenta 2 días, los dos con el cuerpo normal: "
                "todo lo demás ya lo cogían hrv_baja_1d y hrv_hundida_2d. Lo que "
                "añadía por su cuenta eran falsas alarmas, y un ámbar gastado es "
                "una sesión recortada de verdad."
                .format(light),
            )
            # Las dos de pulso de reposo. `fc_reposo_disparada` se rechaza por
            # nombre porque su gramática también era impecable y porque el
            # motivo para no reescribirla no se ve leyendo la regla: hay que
            # haber mirado la distribución del `rhr_delta` para saber que pedía
            # algo que no puede pasar.
            require(
                name != "fc_reposo_disparada",
                "thresholds.{}.fc_reposo_disparada ya no existe. Pedía "
                "`rhr_delta >= 7` y en 186 días disparó CERO veces: el máximo "
                "histórico de tu rhr_delta es +4,60. No era estricta, era "
                "inalcanzable, y una regla que no puede disparar da la "
                "impresión de que el pulso está vigilado cuando no lo está. Si "
                "vuelve a hacer falta un rojo por pulso, el umbral hay que "
                "sacarlo de tu distribución, no de un número redondo."
                .format(light),
            )
            # Y esta es la más fácil de reescribir de las cuatro, porque los
            # números del test le dan la razón. Por eso el mensaje empieza por
            # ahí: no murió por no ver nada.
            require(
                name != "fc_reposo_elevada",
                "thresholds.{}.fc_reposo_elevada ya no existe. Y OJO, porque "
                "esta SÍ distingue: a `rhr_delta >= 2` acierta 26/40 contra un "
                "fondo de 22,1 %, p = 0,0000. Murió por la otra pregunta: con "
                "el umbral que tenía pintaba el color ella sola UN día en 186 "
                "(2026-08-05, hrv_ratio 0,936, ninguna otra regla), y bajando "
                "el umbral solo crecen los días que recorta con la HRV normal "
                "(9 a >=2,0; 6 a >=3,0). El pulso de reposo es buen termómetro "
                "y mal interruptor: sube cuando el cuerpo está mal, pero nunca "
                "antes que la HRV, y cuando habla solo es que se ha "
                "equivocado. La señal sigue viva y en el panel; lo que se fue "
                "es su voto."
                .format(light),
            )
            # Y la quinta, que es la que costó matar y la más fácil de querer
            # de vuelta: era el 56 % de los ámbares. Se rechaza por nombre
            # porque la tentación no es reescribirla igual, es reescribirla
            # «mejor» -con la nota en vez de los minutos-, y esa variante SÍ
            # distingue. El motivo por el que aun así no vale no se ve leyendo
            # ninguna regla: hay que haber hecho la prueba de anticipación.
            require(
                name != "sueno_corto",
                "thresholds.{}.sueno_corto ya no existe. Pedía 5-6,5 h de "
                "sueño y era la regla más ruidosa del sistema: 58 disparos en "
                "186 días y 44 ámbares pintados ella sola, 44 de 79. Se "
                "midieron 53 variantes (minutos, sleep_score, las dos "
                "combinadas, dos noches seguidas) con BH una sola vez. Los "
                "minutos no distinguen nada (mejor q = 0,83, y el barrido sube "
                "con MÁS sueño). `sleep_score` sí, y mucho (<70 acierta 8/11, "
                "q = 0,0079). Pero repetido solo sobre los días en que "
                "hrv_baja_1d NO avisa -que es donde un ámbar informaría de "
                "algo- NINGUNA de las 53 gana al fondo del 19,3 %: todas "
                "q = 1,0000, y `score<70` acierta 0 de 3. No es una señal "
                "adelantada, es un eco de la HRV. Si vuelve a hacer falta una "
                "regla de sueño, primero la prueba de anticipación; si no la "
                "pasa, no informa: recorta."
                .format(light),
            )
            # Y el nombre reservado. No es una lápida como las cinco de
            # arriba: es una colisión. `ambar_sin_datos` lo escribe el motor
            # como disparo sintético cuando promueve un verde ciego, y una
            # regla del YAML con ese nombre convertiría dos cosas distintas en
            # la misma entrada de `fired_rules_json` y de `trigger_rule`.
            # Nadie podría luego distinguir en el histórico un ámbar por
            # precaución de un ámbar por regla.
            require(
                name != REGLA_SIN_DATOS,
                f"thresholds.{light}.{REGLA_SIN_DATOS}: ese nombre está "
                f"reservado. Lo usa el motor para el ámbar por precaución "
                f"-el verde que se decidió sin los datos del reloj- y se "
                f"guarda en `trigger_rule` como una regla más. Con dos cosas "
                f"llamadas igual, el histórico deja de poder distinguirlas.",
            )
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
    # -----------------------------------------------------------------------
    # `actions` es el bloque más peligroso del fichero, y era el único sin
    # cerrar. Merece el comentario largo.
    #
    # `session_builder` leía el tipo de sesión así:
    #
    #     kind = str(action.get("session", FULL))
    #
    # y ese defecto es el valor MÁS permisivo de los tres. O sea: escribir
    # `sesion: recovery` en vez de `session: recovery` bajo `actions.red` no
    # daba ningún error -no había whitelist- y construía una sesión COMPLETA un
    # día rojo, con progresión si el resto del bloque lo permitía, y la escribía
    # en Hevy. El fichero diría "recovery" y el gimnasio diría "full".
    #
    # Para una espalda con hernia L4-L5 ese es el único fallo del sistema que
    # SUELTA en vez de frenar, y por eso se cierra por cuatro sitios a la vez:
    #
    #   1. whitelist de `actions` y de cada luz -> una errata no entra;
    #   2. `session` obligatoria y contra la lista de valores válidos -> un
    #      valor inventado no entra;
    #   3. la sesión no puede ser MÁS permisiva cuanto peor es la luz -> un
    #      `actions.red.session: full` bien escrito tampoco entra, que es el
    #      caso que se le escapa a las dos comprobaciones anteriores;
    #   4. y el motor deja de tener defecto: lee la clave y revienta si falta,
    #      en vez de elegir la más permisiva por su cuenta.
    #
    # La 3 no es paranoia de más. Las dos primeras cazan la errata y el disparate
    # tipográfico; la que caza el error de criterio a las once de la noche
    # editando el YAML es esta. Rojo significa que el cuerpo dice que pares: no
    # hay ninguna versión futura de este sistema en la que rojo deba dar una
    # sesión completa, así que se deja escrito en el validador y no en la
    # memoria de nadie.
    SESIONES = ("full", "reduced", "recovery")
    # De más permisiva a menos. El índice es lo que se compara en la 3.
    PERMISIVIDAD = {"full": 0, "reduced": 1, "recovery": 2}
    LUCES_DE_PEOR_A_MEJOR = ("green", "amber", "red")

    check_keys(data["actions"], set(LUCES_DE_PEOR_A_MEJOR), "actions")

    sesion_por_luz: dict[str, str] = {}
    for light in LUCES_DE_PEOR_A_MEJOR:
        require(light in data["actions"], f"falta actions.{light}")
        accion = data["actions"].get(light) or {}

        check_keys(
            accion,
            {
                "session",
                "allow_progression",
                "allow_hiit",
                "bike_max",
                "set_reduction",
                "min_sets_per_exercise",
                "drop_expendable",
                "recovery_block",
            },
            f"actions.{light}",
        )

        sesion = accion.get("session")
        require(
            sesion is not None,
            f"actions.{light}.session no está. Sin ella el motor construía una "
            f"sesión COMPLETA por defecto, que en rojo es la sesión entera el "
            f"día que el cuerpo dice que pares.",
        )
        require(
            sesion in SESIONES,
            f"actions.{light}.session vale {sesion!r} y solo puede ser una de "
            f"{list(SESIONES)}. Un valor que el motor no reconoce no se salta: "
            f"cae en la rama de sesión completa, que es la más permisiva.",
        )
        if sesion in PERMISIVIDAD:
            sesion_por_luz[light] = str(sesion)

        # Los tres booleanos que abren la puerta. Que existan no basta: un
        # `allow_progression: "false"` -con comillas- es una cadena no vacía y
        # en Python es verdadera, así que abriría la progresión justo donde el
        # YAML dice que la cierra.
        for bandera in ("allow_progression", "allow_hiit"):
            require(
                bandera in accion,
                f"falta actions.{light}.{bandera}. El motor tenía un defecto "
                f"en el código y ganaba en silencio.",
            )
            require(
                isinstance(accion[bandera], bool),
                f"actions.{light}.{bandera} vale {accion[bandera]!r}, que no es "
                f"true ni false. Una cadena como 'false' es VERDADERA en Python "
                f"y abriría lo que aquí se quiere cerrar.",
            )

        cap = accion.get("bike_max")
        require(
            cap in order,
            f"actions.{light}.bike_max '{cap}' no está en intensity_order",
        )

    # La comprobación 3: cuanto peor la luz, no puede haber más manga ancha.
    for peor, mejor in zip(LUCES_DE_PEOR_A_MEJOR[1:], LUCES_DE_PEOR_A_MEJOR[:-1]):
        a, b = sesion_por_luz.get(peor), sesion_por_luz.get(mejor)
        if a is None or b is None:
            continue  # ya se ha protestado arriba
        require(
            PERMISIVIDAD[a] >= PERMISIVIDAD[b],
            f"actions.{peor}.session es '{a}' y actions.{mejor}.session es "
            f"'{b}': el día PEOR daría una sesión más exigente que el mejor. "
            f"El semáforo solo puede recortar hacia abajo; si esto se permite, "
            f"el único fallo que suelta en vez de frenar deja de tener red.",
        )

    # --- B-2: el bloque de recuperación del día rojo ------------------------
    #
    # `actions.red.recovery_block` nombra una entrada de `recovery_blocks`, y
    # nadie comprobaba que existiera. El motor hace:
    #
    #     block = (raw.get("recovery_blocks", {}) or {}).get(block_key, {}) or {}
    #
    # o sea que un nombre mal escrito da `{}`, y de `{}` sale una sesión de
    # recuperación con título "Recuperación", CERO ejercicios y `write_to_hevy`
    # en falso. Se lee en Telegram como un día de recuperación normal. El día que
    # más falta hace saber qué hacer, el mensaje no dice nada y parece que sí.
    #
    # `recovery_blocks` además no se validaba en absoluto: ni que fuera un mapa,
    # ni que sus bloques tuvieran ejercicios.
    bloques = data.get("recovery_blocks") or {}
    require(
        isinstance(bloques, dict) and bloques,
        "recovery_blocks no es un mapa con al menos un bloque. Es de donde sale "
        "la sesión del día rojo.",
    )
    if isinstance(bloques, dict):
        for nombre, bloque in bloques.items():
            require(
                isinstance(bloque, dict),
                f"recovery_blocks.{nombre} no es un bloque",
            )
            if not isinstance(bloque, dict):
                continue
            check_keys(
                bloque, {"title", "write_to_hevy", "exercises"}, f"recovery_blocks.{nombre}"
            )
            require(
                bool(bloque.get("exercises")),
                f"recovery_blocks.{nombre} no tiene ejercicios. Un bloque vacío "
                f"sale por Telegram como un día de recuperación con la lista en "
                f"blanco, y eso no se distingue de un bloque bien escrito.",
            )
            require(
                isinstance(bloque.get("write_to_hevy", False), bool),
                f"recovery_blocks.{nombre}.write_to_hevy tiene que ser true o false",
            )

    for light in LUCES_DE_PEOR_A_MEJOR:
        accion = data["actions"].get(light) or {}
        if accion.get("session") != "recovery":
            require(
                "recovery_block" not in accion,
                f"actions.{light}.recovery_block está puesto pero "
                f"actions.{light}.session es {accion.get('session')!r}: el "
                f"bloque no se leería nunca, y el fichero da a entender que sí.",
            )
            continue
        clave = accion.get("recovery_block")
        require(
            bool(clave),
            f"actions.{light}.session es 'recovery' pero no dice qué "
            f"recovery_block usar. Sin él, la sesión sale sin un solo "
            f"ejercicio y con cara de estar bien.",
        )
        require(
            not clave or clave in bloques,
            f"actions.{light}.recovery_block '{clave}' no existe en "
            f"recovery_blocks (hay: {sorted(bloques)}). El motor no falla con "
            f"esto: devuelve un bloque vacío, y el día rojo -el día que más "
            f"falta hace saber qué hacer- el mensaje llega sin nada dentro.",
        )

    # Aquí se validaba `actions.red.defer_expires_days`, el plazo que tenía una
    # sesión aplazada por un rojo antes de darse por perdida. No hay plazo
    # porque no hay aplazamiento: con la rotación leída de lo EJECUTADO (ver
    # `rotation` en el YAML), un día rojo no ejecuta ninguna rutina del ciclo,
    # el puntero no se mueve y mañana vuelve a tocar la misma. Nada que caducar
    # y, sobre todo, ninguna sesión que se pueda perder por caducidad.
    #
    # Que las dos claves ya no estén en la lista blanca de `actions.*` de arriba
    # basta para que reescribirlas dé error: se rechazan como clave que no se
    # lee, que es exactamente lo que serían.

    rec = data["cycling"].get("recommendation", {})

    # --- punto de partida: bandas sobre los huecos del propio histórico -----
    #
    # Se valida entero y en el arranque porque el motor NO tiene valores por
    # defecto para nada de esto: `_baseline_gaps` lee las siete claves con
    # corchetes, a propósito, para que una que falte reviente donde se ve y no
    # se sustituya por una constante silenciosa. Este bloque es lo que hace que
    # reviente el lunes a las 6:00 dentro del contenedor en lugar de a las
    # 6:00:01 en el mensaje de Telegram.
    gaps = rec.get("baseline_from_gaps")
    require(
        isinstance(gaps, dict),
        "falta cycling.recommendation.baseline_from_gaps. Es el punto de "
        "partida de la bici y no hay ninguno por defecto: el defecto sería una "
        "constante inventada, que es justo lo que se quitó al borrar "
        "baseline_by_weekday.",
    )
    for clave in ("window_days", "min_gaps"):
        valor = gaps.get(clave)
        require(
            isinstance(valor, int) and not isinstance(valor, bool) and valor > 0,
            f"baseline_from_gaps.{clave} tiene que ser un entero positivo, "
            f"y vale {valor!r}",
        )
    # `min_gaps` por debajo de 2 no es un mínimo: con un solo hueco, los dos
    # percentiles son el mismo número -sean los que sean, que por eso no se
    # nombran aquí- y la banda de 'intensa' no existe (el motor lo
    # detecta y se niega a aconsejar, así que el efecto sería un sistema mudo
    # con aspecto de estar configurado).
    require(
        int(gaps["min_gaps"]) >= 2,
        f"baseline_from_gaps.min_gaps vale {gaps['min_gaps']}: con menos de 2 "
        f"huecos los percentiles son el mismo punto y la banda intermedia -la "
        f"única que propone 'intensa'- no puede existir.",
    )
    for clave in ("percentile_low", "percentile_high"):
        valor = gaps.get(clave)
        require(
            isinstance(valor, (int, float)) and not isinstance(valor, bool)
            and 0 <= float(valor) <= 100,
            f"baseline_from_gaps.{clave} tiene que ser un percentil entre 0 y "
            f"100, y vale {valor!r}",
        )
    require(
        float(gaps["percentile_low"]) < float(gaps["percentile_high"]),
        f"baseline_from_gaps: percentile_low ({gaps['percentile_low']}) tiene "
        f"que ser menor que percentile_high ({gaps['percentile_high']}). Si son "
        f"iguales o van al revés, la banda intermedia queda vacía y el sistema "
        f"no vuelve a proponer una salida intensa nunca, sin decirlo.",
    )
    for clave in ("level_below_low", "level_between", "level_above_high"):
        require(
            gaps.get(clave) in order,
            f"baseline_from_gaps.{clave}: '{gaps.get(clave)}' no está en "
            f"intensity_order ({order})",
        )

    # `recommend_on` y `baseline_by_weekday` eran el calendario fijo aplicado a
    # la bici: el primero callaba de lunes a viernes -30 de 80 salidas reales
    # del último año sin una palabra, 6 de ellas intensas- y el segundo daba por
    # hecho que el sábado toca intensa y el domingo media. `no_consecutive_intense`
    # y `after_intense_downgrade_to` recortaban la salida del domingo si el
    # sábado había sido intensa; eso es una cuenta, no una señal del cuerpo, y
    # ahora sale como nota. Las cuatro van a la lista negra por lo mismo que las
    # del recuento: reescribir cualquiera de ellas sería creer que se recupera
    # algo que ya no existe, y no enterarse de que no hace nada.
    muertas = {
        "recommend_on": (
            "la bici ya no tiene días asignados: se aconseja todos los días. "
            "El punto de partida sale de baseline_from_gaps."
        ),
        "baseline_by_weekday": (
            "el punto de partida ya no depende del día de la semana, sino de "
            "los días transcurridos desde tu última salida intensa. Ver "
            "baseline_from_gaps."
        ),
        "no_consecutive_intense": (
            "la salida intensa de ayer ahora se cuenta y se dice, no recorta "
            "la de hoy. Quien frena por acumulación es el semáforo."
        ),
        "after_intense_downgrade_to": (
            "no hay recorte tras una intensa, así que no hay nivel al que "
            "bajar. Quien frena por acumulación es el semáforo."
        ),
    }
    for muerta, porque in muertas.items():
        require(
            muerta not in rec,
            f"cycling.recommendation.{muerta} ya no existe: {porque}",
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

    # --- NINGÚN NOMBRE DE SEÑAL PUEDE SER UN NOMBRE QUE NO FABRICA NADIE ----
    #
    # Es el fallo que no da error. El motor -`evaluate_gate`, las reglas del
    # semáforo, los disparadores- resuelve cada señal con `signals.get(nombre)`,
    # y un nombre que nadie escribe devuelve `None`, que todo el sistema trata
    # como «hoy no hay dato». Una errata en un `source` no revienta: convierte
    # el freno en un freno que se salta TODAS las mañanas por falta de datos, y
    # en el log eso se lee exactamente igual que un día en que el reloj no
    # sincronizó. El freno sigue escrito, comentado y validado, y no salta
    # jamás.
    #
    # Es literalmente lo que pasó con `yesterday_routine`, solo que por el otro
    # lado del `get`: la clave se nombraba en el YAML, nadie la producía, y
    # `last_session_only` bloqueó las tres rutinas durante toda la vida del
    # sistema sin que nada lo dijera.
    #
    # El catálogo sale de `signals.py` y se compara con lo que `build_signals`
    # deja de verdad en `values` (`test_signals`), así que no puede quedarse
    # atrás y empezar a rechazar nombres buenos.
    catalogo = senales_producidas(data)

    def check_senal(nombre: Any, donde: str) -> None:
        if nombre is None or nombre in catalogo:
            return
        cerca = sorted(n for n in catalogo if n.startswith(str(nombre)[:4]))
        pista = f" ¿Querías decir {', '.join(cerca)}?" if cerca else ""
        errors.append(
            f"{donde}: la señal '{nombre}' no la produce nadie. No está entre "
            f"las que escribe `build_signals`, así que valdría `None` todos los "
            f"días y quien la mire se saltaría por falta de datos para siempre, "
            f"sin dar error ni decirlo.{pista}"
        )

    for brake in data["progression"].get("brakes", []):
        check_senal(brake.get("source"), f"freno '{brake.get('name')}'")
        # `last_session_only` acota el freno a la rutina de la sesión de ayer, y
        # el motor lee esa rutina de `yesterday_routine`. Sin esa señal el `in
        # (None, routine_key)` se cumple para cualquier rutina y el modo es
        # indistinguible de `all`: un freno más ancho de lo que dice el YAML.
        if str(brake.get("blocks", "all")) == "last_session_only":
            require(
                "yesterday_routine" in catalogo,
                f"freno '{brake.get('name')}': usa 'blocks: last_session_only', "
                f"que necesita la señal 'yesterday_routine' para saber a qué "
                f"rutina acotarse. Sin ella bloquearía TODAS, que es lo "
                f"contrario de lo que pide esta línea.",
            )

    for rule in data.get("special_rules") or []:
        check_senal(
            (rule.get("trigger") or {}).get("source"),
            f"regla '{rule.get('name')}'.trigger",
        )

    for nivel in ("red", "amber"):
        for rule in data["thresholds"].get(nivel) or []:
            if not isinstance(rule, dict):
                continue
            for senal in sorted(senales_de_regla(rule)):
                check_senal(senal, f"regla '{rule.get('name')}'")

    for nombre, spec in (data.get("adaptive_thresholds") or {}).items():
        if isinstance(spec, dict):
            check_senal(spec.get("metric"), f"adaptive_thresholds.{nombre}")

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
        # La puerta trasera del guardia de arriba. Aquel mira las CLAVES del
        # `when`, y en `{load_2d: {gt_adaptive: mi_umbral}}` la clave es
        # `load_2d`: el umbral contra el que se compara viaja en el valor y ahí
        # no llega. Un `metric: will_train` definido aquí y referenciado desde
        # cualquier regla metería el histórico de «hoy no voy» dentro del cálculo
        # del color por un camino que el otro test no ve. Es rebuscado, y por eso
        # mismo es el que nadie revisaría.
        require(
            spec.get("metric") not in pregunta_keys,
            f"adaptive_thresholds.{name}: su métrica es '{spec.get('metric')}', que "
            f"es una pregunta del check-in. Un percentil sobre ella sigue siendo "
            f"ella decidiendo el semáforo, solo que con una vuelta más.",
        )

    # --- la ventana de salidas que alimenta esos umbrales --------------------
    #
    # `cycling.fetch` estuvo declarado sin que lo leyera nadie mientras el
    # código pedía 190 días a pelo desde dos sitios. Ya está conectado
    # (`activity_cache.ventana_de_salidas`), y conectarlo obliga a comprobar
    # esto: el backfill es lo único que llena la caché sobre la que se calcula
    # todo lo que se calibra contra el propio histórico, así que un backfill más
    # corto que esas ventanas las apaga PARA SIEMPRE y sin un solo error. Hoy la
    # que cuelga de ahí es el punto de partida de la bici, y el mensaje de la
    # mañana sale igual de bonito con una base peor.
    fetch = ((data.get("cycling") or {}).get("fetch") or {})
    declaradas_son_enteras = True
    for clave in ("lookback_days", "backfill_days"):
        v = fetch.get(clave)
        bien = v is None or (isinstance(v, int) and v >= 1)
        declaradas_son_enteras = declaradas_son_enteras and bien
        require(bien, f"cycling.fetch.{clave}: '{v}' debe ser un entero >= 1")

    # AQUÍ HABÍA UN `if fetch:` Y SE SALTABA LA COMPROBACIÓN ENTERA.
    #
    # Con la sección declarada no se notaba. Sin ella -borrarla es lo más fácil
    # del mundo, y aparentemente inocuo porque "ya hay defectos"- el validador
    # no miraba nada y el código se iba a sus defectos, que es exactamente el
    # caso en el que más falta hace mirar: nadie ha escrito un número, así que
    # nadie va a ir a revisarlo cuando la ventana de la bici cambie. Se valida
    # el valor EFECTIVO, el que `ventana_de_salidas` va a usar, preguntándoselo
    # a la misma función que se lo da a ella. (Solo si lo declarado es un
    # entero: con un 'diez' escrito en el YAML el error ya está puesto arriba y
    # resolver las ventanas reventaría con un `ValueError` que taparía el
    # mensaje bueno con uno peor.)
    corta = larga = None
    if declaradas_son_enteras:
        corta, larga = ventanas_declaradas(data)
        require(
            corta <= larga,
            f"cycling.fetch: lookback_days ({corta}) no puede ser mayor que "
            f"backfill_days ({larga}). La corta es la relectura de cada "
            f"mañana y la larga el histórico completo; al revés los nombres "
            f"mienten y el 'backfill' dejaría huecos",
        )

    # SE PREGUNTA A `dias_adaptativos`, NO SE VUELVE A CALCULAR AQUÍ.
    #
    # Esto era un `max(...)` sobre `adaptive_thresholds` escrito a mano, o
    # sea una segunda copia de lo que decide `activity_cache.dias_adaptativos`.
    # Dos copias del mismo criterio en dos ficheros aguantan exactamente
    # hasta que a una se le añade algo: al meter la ventana de 180 días del
    # punto de partida de la bici en la de `activity_cache`, esta se habría
    # quedado validando solo los percentiles de carga y habría dado por bueno
    # un `backfill_days: 90` que no puede llenar la ventana que la otra
    # exige. El validador diría que sí y la caché pediría un backfill cada
    # mañana sin conseguirlo nunca.
    necesarios = dias_adaptativos(data)
    if necesarios and larga is not None:
        require(
            larga >= necesarios,
            f"cycling.fetch.backfill_days ({larga}) es menor que la ventana "
            f"más larga de algo que se calibra contra el histórico "
            f"({necesarios} días: adaptive_thresholds y el punto de partida "
            f"de la bici). La caché de salidas nunca llegaría a cubrirla, así "
            f"que esos cálculos se quedarían sin base -o peor, se harían "
            f"sobre menos datos de los que existen- sin dar error",
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

    adaptivos_leidos: set[str] = set()

    def check_op_target(op: Any, operand: Any, where: str) -> None:
        nombre = str(op)
        if nombre.endswith(ADAPTIVE_SUFFIX):
            adaptivos_leidos.add(str(operand))
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

    # Las señales que además de valor diario tienen serie. Solo importa para
    # `consecutive_days`, que es lo único del YAML que lee el histórico.
    con_serie = senales_con_serie(data)

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
                    # NO ES UN OPERADOR, PERO TAMPOCO ES GRATIS
                    # Aquí solo se saltaba, y saltárselo dejaba abierto el único
                    # sitio del YAML donde una regla puede quedarse muerta sin
                    # decir nada. `consecutive_days` no lee el valor de hoy: lee
                    # la SERIE, hoy incluido. Y no todas las señales tienen
                    # serie -ver `SENALES_CON_SERIE_FIJA`-: `sleep_min`, `hrv`,
                    # `rhr`, `body_battery` tienen dato diario y ninguna serie.
                    #
                    # Sobre una de esas, la regla no falla: se anota como
                    # saltada por falta de datos todos los días, para siempre,
                    # mientras el dato que dice que falta está ahí delante. En
                    # el log no se distingue de un día sin reloj. Una regla roja
                    # que no puede ponerse roja es peor que no tenerla, porque
                    # ocupa el sitio de la que sí vigilaría eso.
                    if int(operand) > 1 and k not in con_serie:
                        errors.append(
                            f"{where}, señal '{k}': usa consecutive_days={operand} "
                            f"sobre una señal que no tiene serie histórica, así "
                            f"que la regla no se podría evaluar NUNCA y se "
                            f"anotaría como falta de datos cada mañana. Con "
                            f"serie: {sorted(con_serie)}."
                        )
                    continue
                check_op_name(op, f"{where}, señal '{k}'")
                check_op_target(op, operand, f"{where}, señal '{k}'")

    for light in ("red", "amber"):
        for rule in data["thresholds"].get(light, []):
            check_when(
                rule.get("when"),
                f"la regla '{rule.get('name', '<sin nombre>')}'",
            )

    # --- el guardia al revés: un umbral que no lee nadie ---------------------
    #
    # El de arriba mira que una regla no apunte a un umbral inexistente. Este
    # mira lo contrario, que es lo que de verdad pasó: `load_7d_p90` estuvo
    # declarado meses sin que ninguna regla lo nombrara, y cuando se borró
    # `carga_acumulada` su `load_2d_p90` se quedó igual. Un umbral suelto no se
    # calcula, no falla y no aparece en ningún sitio; simplemente da la impresión
    # de que el sistema vigila una carga que no vigila. Y encima arrastra:
    # `dias_adaptativos` dimensiona la caché de salidas con su `window_days`, o
    # sea que un fósil de aquí se cobra peticiones a Garmin todas las mañanas.
    for name in sorted(set(adaptive) - adaptivos_leidos):
        require(
            False,
            f"adaptive_thresholds.{name}: no lo referencia ninguna regla. "
            f"Un umbral que no lee nadie no vigila nada: o se conecta desde un "
            f"'when' con {{señal: {{gt_adaptive: {name}}}}}, o se borra.",
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
    #
    # Y HUBO UNA SEGUNDA MITAD DEL MISMO DEFECTO, PEOR
    # ------------------------------------------------
    # Durante un tiempo el motor no leyó `enabled` EN ABSOLUTO. Este validador
    # exigía la clave, obligaba a que fuera booleana, y el mensaje de error de
    # aquí abajo prometía por escrito que poniéndola a `false` se dejaba de
    # contar. No se dejaba de contar: `intensity_count()` no la miraba, el
    # número seguía saliendo en el mensaje todas las mañanas, y el fichero
    # validaba sin una queja.
    #
    # O sea que no era una opción muerta de las que no hacen nada y se notan:
    # era una opción muerta que este fichero certificaba como viva. Ya está
    # conectada -`intensity_count()` devuelve `None` y la línea desaparece del
    # mensaje-, y estas dos comprobaciones son ahora verdad.
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
            "ella el validador se salta el bloque y el motor cuenta igual, que "
            "es el defecto por defecto y no una decisión de nadie.",
        )
    if conteo.get("enabled"):
        # El periodo tiene que estar ESCRITO, y no vale un `7` supuesto.
        #
        # Aquí se validaba `week_starts_on in WEEKDAYS`, que es la pregunta de
        # cuando el recuento iba por semana natural. Ahora la ventana rueda y lo
        # que hay que exigir es su ancho. Se exige de verdad -entero >= 1- y no
        # solo "si está, que valga": el número sale escrito en el mensaje («en
        # los últimos N días»), así que un `window_days` ausente no daría un
        # recuento aproximado, daría un recuento con las unidades inventadas por
        # el código y sin forma de que el lector lo sospeche.
        #
        # `bool` se descarta a mano porque en Python `True` es un `int` y
        # `window_days: yes` en YAML es `True`. Sin esta línea, esa errata
        # pasaría la validación y contaría una ventana de un día.
        ventana = conteo.get("window_days")
        require(
            isinstance(ventana, int)
            and not isinstance(ventana, bool)
            and ventana >= 1,
            f"intensity_count.window_days tiene que ser un entero >= 1 y vale "
            f"{ventana!r}. Es el periodo del recuento y sale escrito en el "
            f"mensaje; sin él no se supone una semana, se para.",
        )

    # Las claves de `intensity_count` quedan cerradas. Este bloque ha perdido ya
    # cinco -cuatro del presupuesto y ahora `week_starts_on`- y cada una se
    # escribió creyendo que hacía algo. La lista negra de abajo nombra una a una
    # las que se sabe que existieron; esto es la red por debajo, para la sexta.
    check_keys(
        conteo,
        {"enabled", "window_days", "counts_as_intense"},
        "cycling.recommendation.intensity_count",
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
            f"Y no hay dónde mudarla: la regla que frenaba por carga acumulada "
            f"también se borró, porque contar bici no predecía el cuerpo.",
        )

    # Y la quinta, que es de otra familia: no creía frenar, creía elegir el
    # corte de un contador. Se nombra aparte con su propio motivo porque el que
    # la reescriba no se estará equivocando en lo mismo.
    require(
        "week_starts_on" not in conteo,
        "intensity_count.week_starts_on es la clave de cuando el recuento iba "
        "por semana natural y se ponía a cero los lunes. Ahora es una ventana "
        "rodante que termina hoy, así que no empieza por ningún día: escribir "
        "aquí 'sunday' no movería nada y lo parecería. Medido sobre el "
        "histórico real, el corte del lunes hacía que 8 de 26 lunes el mensaje "
        "dijera 'ninguna sesión intensa esta semana' con 1 o 2 intensas en los "
        "siete días anteriores. Si lo que se quiere es otro periodo, es "
        "window_days.",
    )

    # --- rutinas ------------------------------------------------------------
    #
    # Las del ciclo, que ahora son TODAS las de fuerza que hay: lo que no está
    # en `rotation.order` no lo programa nadie. Antes esto se sacaba de recorrer
    # todas las variantes del calendario, la activa y las que no, porque `dia_3`
    # vivía solo en `summer` y mirar únicamente la variante en curso habría
    # dejado el fichero pasando en invierno y fallando al cambiar de temporada.
    # Ese cuidado ya no hace falta: hay una sola lista y es la que se aplica.
    rutinas_de_fuerza = set(orden)
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
        # que deciden `rotation.order` -que no los nombra- y `hiit.blocks` -que
        # los ata a dia_1 y dia_2-. Un `standalone: true` no habría programado
        # nada.
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
                f"rutina '{rkey}': lleva 'focus' pero no está en "
                f"`rotation.order`, así que no entra en el ciclo y nadie "
                f"enseñaría ese subtítulo. Los bloques HIIT se añaden al final "
                f"de otra sesión y usan el encabezado de esa. Bórralo -o mete "
                f"la rutina en el ciclo, si lo que falta es eso.",
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
        # El mismo cerrojo que en `actions.*`: `allow_hiit: "false"` -con
        # comillas- es una cadena no vacía y en Python es verdadera, así que
        # dejaría entrar el bloque justo donde la regla lo quiere quitar. Aquí
        # la clave es opcional -no ponerla es no opinar-, pero puesta tiene que
        # ser un booleano de verdad.
        if "allow_hiit" in action:
            require(
                isinstance(action["allow_hiit"], bool),
                f"{where}.action.allow_hiit vale {action['allow_hiit']!r}, que "
                f"no es true ni false. Una cadena como 'false' es VERDADERA en "
                f"Python y abriría lo que aquí se quiere cerrar.",
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
    # `cycling.weekend` SE HA BORRADO ENTERO, no solo sus umbrales.
    #
    # Fue perdiendo lectores por tandas. Primero se fueron `total_hours_threshold`
    # e `intense_rides_threshold` con la regla `resaca_finde`, y quedó
    # `days: [saturday, sunday]` alimentando a `weekend_summary` y a
    # `_intense_rides_this_weekend`. Con el recuento rodante se han borrado esas
    # dos funciones, así que el bloque se queda sin un solo lector.
    #
    # Y el bloque entero es el problema, no las claves: agrupar por «sábado y
    # domingo» da por hecho que el esfuerzo grande cae en fin de semana. Es el
    # calendario fijo que se echó de `cycling.recommendation`, sobreviviendo dos
    # bloques más abajo con otro nombre. Quien lo reponga no estará configurando
    # nada: estará describiendo una semana que este sistema ya no mira.
    require(
        "weekend" not in (data.get("cycling") or {}),
        "cycling.weekend ya no lo lee nadie. Sus dos umbrales se fueron con la "
        "regla `resaca_finde`, y `days` se ha ido con `weekend_summary` y con la "
        "nota de fin de semana de la bici, que el recuento rodante de "
        "`intensity_count` sustituye y mejora: dice lo mismo los siete días en "
        "vez de solo sábado y domingo, y sobre el histórico real nunca cuenta "
        "por debajo de lo que contaba el fin de semana. Reescribirlo aquí no "
        "volvería a agrupar nada, solo lo parecería.",
    )

    # `cycling.recommendation` no tenía lista blanca, y es el bloque del que
    # más claves muertas han salido en este proyecto: `recommend_on`,
    # `baseline_by_weekday`, `no_consecutive_intense`,
    # `after_intense_downgrade_to`, y antes `weekly_limit`,
    # `on_budget_exhausted`, `max_intense_rides_per_weekend` y
    # `require_green_for_intense`. Ocho. Cada una se escribió creyendo que
    # decidía algo. La lista negra de arriba las nombra una a una con su motivo,
    # que es el mensaje útil; esto es la red por debajo, para la novena.
    recomendacion = (data.get("cycling") or {}).get("recommendation") or {}
    check_keys(
        recomendacion,
        {
            "enabled",
            "baseline_from_gaps",
            "lookback_days",
            "intensity_count",
            "types",
            "intensity_order",
        },
        "cycling.recommendation",
    )
    check_keys(
        recomendacion.get("baseline_from_gaps") or {},
        {
            "window_days",
            "min_gaps",
            "percentile_low",
            "percentile_high",
            "level_below_low",
            "level_between",
            "level_above_high",
        },
        "cycling.recommendation.baseline_from_gaps",
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
            "recompute_time",
            "watchdog_time",
            "garmin_retry",
        },
        "schedule",
    )
    HORAS = (
        "garmin_fetch_time",
        "fallback_decision_time",
        "evening_summary_time",
        "perception_notice_time",
        "recompute_time",
        "watchdog_time",
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

    # La vigilancia mira el ESTADO en el que ha quedado el sistema, y los dos
    # momentos en que puede quedar mal son las escrituras en Hevy: la de las
    # 06:30 con check-in y la de respaldo de las 09:00. Puesta antes, miraría
    # lo de ayer y diría que todo está bien el día que se rompa; el aviso
    # llegaría veinticuatro horas tarde y nadie ataría una cosa con la otra.
    # El recálculo temprano va ANTES que el fallback, y esa es su razón de ser.
    # Puesto igual o después, no arregla nada que el fallback no arreglara ya, y
    # el problema que existe es justo el contrario: el fallback llega cuando el
    # entreno ya se ha hecho con la rutina que la decisión ciega escribió.
    temprano = _min("recompute_time", "07:30")
    if temprano is not None and decision is not None:
        require(
            temprano < decision,
            f"schedule.recompute_time ({sched.get('recompute_time')}) tiene que "
            f"ser ANTERIOR a schedule.fallback_decision_time "
            f"({sched.get('fallback_decision_time')}): existe para rehacer la "
            f"decisión ciega a tiempo de que sirva, y a la hora del fallback o "
            f"después no aporta nada que el fallback no haga ya",
        )

    vigilancia = _min("watchdog_time", "09:45")
    if vigilancia is not None and decision is not None:
        require(
            vigilancia > decision,
            f"schedule.watchdog_time ({sched.get('watchdog_time')}) tiene que "
            f"ser posterior a schedule.fallback_decision_time "
            f"({sched.get('fallback_decision_time')}): la vigilancia comprueba "
            f"en qué estado ha quedado el sistema DESPUÉS de las escrituras "
            f"del día, y antes de esa hora estaría mirando las de ayer",
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

    # --- el lenguaje del panel ----------------------------------------------
    #
    # Sección OPCIONAL: sin ella el panel sigue funcionando, solo que sin
    # portada. Pero si está, se valida entera, porque cada cosa que hay dentro
    # decide qué se le enseña al usuario y qué no, y las dos formas de
    # equivocarse aquí son calladas: una banda mal puesta hace que un hallazgo
    # real no se cuente, y un grupo mal escrito hace que una exposición entera
    # desaparezca de la portada sin que salte nada.
    if "metrics" in data:
        metrics = data.get("metrics") or {}
        check_keys(
            metrics,
            {"hallazgos_en_portada", "fuerza_relacion", "grupos_exposicion"},
            "metrics",
        )

        n_portada = metrics.get("hallazgos_en_portada")
        require(
            isinstance(n_portada, int)
            and not isinstance(n_portada, bool)
            and n_portada >= 1,
            f"metrics.hallazgos_en_portada: '{n_portada}' tiene que ser un entero "
            f">= 1. Con 0 la portada se quedaría sin su mitad útil y parecería "
            f"que no hay nada que contar, que es distinto de no querer contarlo",
        )

        bandas = metrics.get("fuerza_relacion") or {}
        check_keys(
            bandas,
            {"se_nota_poco", "se_nota", "se_nota_mucho"},
            "metrics.fuerza_relacion",
        )
        for clave in ("se_nota_poco", "se_nota", "se_nota_mucho"):
            v = bandas.get(clave)
            require(
                _es_num(v) and 0 < v < 1,
                f"metrics.fuerza_relacion.{clave}: '{v}' tiene que ser un número "
                f"entre 0 y 1 sin incluirlos. Son cortes sobre |r|, que vive "
                f"justo en ese intervalo",
            )
        poco, medio, mucho = (
            bandas.get("se_nota_poco"),
            bandas.get("se_nota"),
            bandas.get("se_nota_mucho"),
        )
        if all(_es_num(v) for v in (poco, medio, mucho)):
            require(
                poco < medio < mucho,
                f"metrics.fuerza_relacion tiene que ir de menos a más "
                f"({poco} < {medio} < {mucho}). Desordenadas no fallan: se "
                f"aplican en orden y la banda de en medio no se usaría nunca, "
                f"así que todo saldría o flojo o fortísimo",
            )

        grupos = metrics.get("grupos_exposicion")
        require(
            isinstance(grupos, list) and bool(grupos),
            f"metrics.grupos_exposicion: '{grupos}' tiene que ser una lista con "
            f"al menos un grupo. Vacía, TODAS las exposiciones quedarían "
            f"huérfanas y la portada no podría contar nada",
        )
        if isinstance(grupos, list):
            vistas_claves: set[str] = set()
            vistas_exp: dict[str, str] = {}
            vistas_fam: dict[str, str] = {}
            for i, g in enumerate(grupos):
                donde = f"metrics.grupos_exposicion[{i}]"
                if not isinstance(g, dict):
                    require(False, f"{donde}: '{g}' tiene que ser un mapa")
                    continue
                check_keys(
                    g,
                    {"clave", "titulo", "decision", "exposiciones", "familias"},
                    donde,
                )
                for obligatoria in ("clave", "titulo", "decision"):
                    require(
                        isinstance(g.get(obligatoria), str) and g[obligatoria].strip(),
                        f"{donde}.{obligatoria}: hace falta un texto. `decision` "
                        f"es lo que se le enseña al usuario como el eje que "
                        f"puede mover, y sin ella el grupo no se puede titular",
                    )
                clave = g.get("clave")
                if isinstance(clave, str):
                    require(
                        clave not in vistas_claves,
                        f"{donde}.clave: '{clave}' está repetida. Dos grupos con "
                        f"la misma clave se pisan y uno de los dos desaparece",
                    )
                    vistas_claves.add(clave)

                exps = g.get("exposiciones") or []
                fams = g.get("familias") or []
                require(
                    bool(exps) or bool(fams),
                    f"{donde}: un grupo sin `exposiciones` ni `familias` no "
                    f"reclama nada y no puede recibir ningún hallazgo",
                )
                for nombre, lista in (("exposiciones", exps), ("familias", fams)):
                    require(
                        isinstance(lista, list),
                        f"{donde}.{nombre}: '{lista}' tiene que ser una lista",
                    )
                    if not isinstance(lista, list):
                        continue
                    # Reclamar dos veces la misma cosa no da error al repartir
                    # -gana el primero- pero deja al segundo grupo esperando
                    # unos hallazgos que nunca le van a llegar, y eso se lee
                    # como "de esto no se sabe nada" cuando sí se sabe.
                    registro = vistas_exp if nombre == "exposiciones" else vistas_fam
                    for v in lista:
                        require(
                            isinstance(v, str) and v.strip(),
                            f"{donde}.{nombre}: '{v}' tiene que ser un texto",
                        )
                        if not isinstance(v, str):
                            continue
                        require(
                            v not in registro,
                            f"{donde}.{nombre}: '{v}' ya lo reclama el grupo "
                            f"'{registro.get(v)}'. Repartido dos veces, el "
                            f"segundo grupo se queda mudo sin dar ningún error",
                        )
                        if isinstance(clave, str):
                            registro[v] = clave

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
