"""Escritura de rutinas en Hevy, con copia de seguridad y reversión.

POR QUÉ ESTE FICHERO ES EL MÁS DEFENSIVO DEL PROYECTO
------------------------------------------------------
`PUT /v1/routines/{id}` de Hevy REEMPLAZA la rutina entera. No es un parche.
Eso significa que una escritura mal construida no degrada la rutina: la
sustituye. Y que una escritura interrumpida a mitad puede dejar en la app una
rutina que no es ni la de antes ni la de después.

De ahí las cuatro reglas de este módulo, en orden de importancia:

1. **Sin copia previa verificada no se escribe.** Antes de cada PUT se lee el
   estado remoto actual, se guarda en disco y se vuelve a leer del disco para
   comprobar que se guardó bien. Si cualquiera de esos tres pasos falla, la
   escritura NO se intenta. Es preferible un día sin actualizar la rutina que un
   día con la rutina rota y sin vuelta atrás.

2. **El interruptor general manda sobre todo lo demás.** Si
   `integrations.hevy.write_enabled` es false, este módulo no escribe. No lo
   decide quien llama: se comprueba aquí, en el único sitio por el que pasan
   todas las escrituras. Un interruptor que hay que acordarse de mirar no es un
   interruptor.

3. **Una escritura en vuelo se marca antes de empezar.** Se deja un fichero
   `PENDIENTE` antes del PUT y se borra al confirmar. Si el proceso muere en
   medio, ese fichero sobrevive y el siguiente arranque sabe que hay una rutina
   de estado dudoso, con su copia al lado para revertirla.

4. **Nunca se inventa el `superset_id`.** Se copia tal cual venía. Omitirlo en
   el PUT deshace las superseries en la app, y eso es una pérdida silenciosa de
   configuración que el usuario descubriría en mitad de una sesión.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence

from app.engine.sets import warmup_flags

log = logging.getLogger(__name__)

TIMEOUT_S = 30.0
PENDING_NAME = "ESCRITURA_EN_CURSO.json"


class HevyError(RuntimeError):
    """Fallo hablando con Hevy que el llamante debe ver."""


class HevyWriteDisabled(HevyError):
    """La escritura está apagada por configuración. No es un error: es el modo."""


# ---------------------------------------------------------------------------
# Construcción del cuerpo del PUT (función pura)
# ---------------------------------------------------------------------------
# Separada del cliente HTTP a propósito: es lo que `--dry-run` enseña. Si la
# construcción viviera dentro de la llamada de red, el ensayo tendría que
# simularla y estaría enseñando otra cosa distinta de la que se envía.


def _fecha_workout(w: dict[str, Any]) -> date | None:
    """El día de un entrenamiento de Hevy.

    Devuelve `None` si no se puede leer, y el llamante lo descarta. Aquí SÍ es
    razonable descartar -al revés que en Garmin, donde una fecha ilegible
    revienta-: en Garmin la fecha decide si una salida entra en la carga
    acumulada y perderla falsea un número; aquí solo se usa para saber si el
    entrenamiento cae dentro de la ventana que se está reconciliando, y un
    entrenamiento sin fecha legible no se puede asignar a ningún día por
    definición. Lo que no puede es pasar callando: se anota en el log.
    """
    stamp = w.get("start_time") or w.get("startTime") or w.get("created_at")
    if not stamp:
        log.warning("Hevy: entrenamiento %s sin fecha; se ignora", w.get("id"))
        return None
    texto = str(stamp).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(texto).date()
    except ValueError:
        log.warning(
            "Hevy: entrenamiento %s con fecha ilegible %r; se ignora",
            w.get("id"),
            stamp,
        )
        return None


def routine_key_de(workout: dict[str, Any], config: Any = None) -> str | None:
    """De qué rutina del config salió un entrenamiento, leyendo `routine_id`.

    SE MIRA EL ID Y NUNCA EL TÍTULO, y no es una precaución teórica. Medido el
    2026-09-13 contra los 14 entrenamientos reales de la cuenta: CUATRO llevan
    un título que nombra una rutina distinta de la que dice su `routine_id`. El
    2026-08-14 hay uno titulado «Día 3» cuyo id es el de `dia_1`, y el
    2026-08-17 otro titulado «Día 1» con el id de `dia_3`. El título se congela
    en el momento de ejecutar y las rutinas se renombraron después; el id no
    cambia. Clasificar por nombre habría errado el 29% del histórico, y en el
    sentido peor: contando como fuerza lo que fue HIIT y al revés.

    `None` significa que el entrenamiento no sale de ninguna rutina conocida
    -uno suelto, o una rutina que no está en `config.yaml`-. Es un dato, no un
    fallo: es exactamente lo que hay que poder contar en vez de perder.
    """
    rid = workout.get("routine_id") or workout.get("routineId")
    if not rid:
        return None
    raw = (config.raw if hasattr(config, "raw") else config) or {}
    for key, rutina in (raw.get("routines") or {}).items():
        if str((rutina or {}).get("hevy_routine_id") or "") == str(rid):
            return str(key)
    return None


def _es_ultima_pagina(datos: Any, pagina: int) -> bool:
    """¿La respuesta dice que esta es la última página que existe?

    `False` cuando no lo dice: no se adivina. Equivocarse aquí en el sentido
    optimista sería dar por completa una lista truncada, que es justo lo que el
    guardián de `get_workouts` existe para impedir.
    """
    if not isinstance(datos, dict):
        return False
    total = datos.get("page_count", datos.get("pageCount"))
    try:
        return pagina >= int(total)
    except (TypeError, ValueError):
        return False


def claves_hiit(config: Any = None) -> set[str]:
    """Las claves de rutina que son bloques HIIT, según `hiit.blocks`.

    Sale del config y no de una lista aparte para que no haya dos verdades: el
    día que se añada un tercer bloque, esto lo sabe sin tocarlo.
    """
    raw = (config.raw if hasattr(config, "raw") else config) or {}
    return {str(v) for v in ((raw.get("hiit") or {}).get("blocks") or {}).values()}


@dataclass(frozen=True)
class WorkoutTotals:
    """Los tres números de un entrenamiento que van a `workout_log`."""

    duration_s: int | None = None
    total_sets: int | None = None
    total_volume_kg: float | None = None


def workout_totals(workout: dict[str, Any]) -> WorkoutTotals:
    """Duración, series y volumen de un entrenamiento de Hevy.

    Los tres campos llevaban desde el principio declarados en `WorkoutLog` y
    nadie los llenaba: la fila guardaba el id, el título y si la sesión fue
    limpia, y el resto quedaba a NULL. `docs/analisis.md` daba por hecho que la
    vista de volumen salía de aquí.

    SE CUENTA TODO, TAMBIÉN EL CALENTAMIENTO, y no es un descuido:

    - `total_sets` significa el total. Si excluyera el calentamiento el nombre
      estaría mintiendo, y eso ya ha costado bastante en este proyecto.
    - Y sobre todo: desde que el motor marca las series de calentamiento en Hevy
      (`set_types.write_warmup_type_to_hevy`), la PROPORCIÓN de series marcadas
      cambia de un mes a otro por un cambio de código, no por un cambio de
      entrenamiento. Un total que excluyera el calentamiento daría un escalón en
      la gráfica el día de ese despliegue, y ese escalón no significaría nada.

    El desglose efectivo se puede recalcular exactamente desde `raw_json`, que
    ahora sí se guarda. El total no se puede recuperar si no se guarda.

    Una serie sin peso (peso corporal, plancha) suma 0 al volumen y 1 a las
    series: hacerla no es levantar kilos, pero es una serie.
    """
    ejercicios = workout.get("exercises") or []
    series = 0
    volumen = 0.0
    for ex in ejercicios:
        for s in ex.get("sets") or []:
            series += 1
            peso, reps = s.get("weight_kg"), s.get("reps")
            if peso is not None and reps is not None:
                try:
                    volumen += float(peso) * float(reps)
                except (TypeError, ValueError):
                    log.warning(
                        "Hevy: serie con peso/reps no numéricos (%r x %r) en el "
                        "entrenamiento %s; no suma al volumen",
                        peso, reps, workout.get("id"),
                    )

    return WorkoutTotals(
        duration_s=_duracion(workout),
        # Un entrenamiento sin ejercicios no son 0 series: es que no se pudo
        # leer. Cero y "no se sabe" no son el mismo dato en una gráfica.
        total_sets=series if ejercicios else None,
        total_volume_kg=round(volumen, 1) if ejercicios else None,
    )


def _duracion(w: dict[str, Any]) -> int | None:
    """Segundos entre el inicio y el fin. `None` si no se pueden leer los dos."""
    directo = w.get("duration_seconds") or w.get("duration_s")
    if directo is not None:
        try:
            return int(float(directo))
        except (TypeError, ValueError):
            pass

    def instante(clave: str, alt: str) -> datetime | None:
        stamp = w.get(clave) or w.get(alt)
        if not stamp:
            return None
        try:
            return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            return None

    ini, fin = instante("start_time", "startTime"), instante("end_time", "endTime")
    if ini is None or fin is None:
        return None
    segundos = (fin - ini).total_seconds()
    # Una duración negativa o absurda es un dato malo, y guardarlo lo daría por
    # bueno para siempre. Mejor NULL, que se lee como "esto no se sabe".
    if segundos <= 0 or segundos > 12 * 3600:
        log.warning(
            "Hevy: duración imposible (%.0f s) en el entrenamiento %s; se deja "
            "sin dato", segundos, w.get("id"),
        )
        return None
    return int(segundos)


def _emparejar(
    workout: dict[str, Any], planned: Any, config: Any = None
) -> dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]] | None]]:
    """Cada ejercicio del plan, con sus series efectivas pedidas y hechas.

    Devuelve `{key: (objetivo, reales)}`, donde `reales` es `None` cuando el
    ejercicio NO APARECE en el entrenamiento y una lista -posiblemente vacía-
    cuando aparece. La diferencia importa: "no está" y "está pero solo con
    calentamiento" son dos cosas, y una de ellas ni siquiera es una sesión.

    El emparejamiento se escribe UNA vez, aquí, porque de él cuelgan dos
    lecturas que tienen que ver exactamente lo mismo: si se cumplió
    (`workout_compliance`) y con cuánto peso (`pesos_ejecutados`). Dos
    emparejamientos distintos podrían decir "el ejercicio no aparece, no
    cumple" y a la vez "el ejercicio se hizo a 70 kg", y de esa contradicción
    sale una adopción de carga sobre una sesión que el motor considera fallida.
    """
    raw = (config.raw if hasattr(config, "raw") else config) or {}
    set_cfg = raw.get("set_types", {}) or {}

    hechos: dict[str, list[dict[str, Any]]] = {}
    for ex in workout.get("exercises") or []:
        clave = ex.get("exercise_template_id") or ex.get("title")
        if clave:
            hechos.setdefault(str(clave), []).extend(ex.get("sets") or [])

    salida: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]] | None]] = {}
    for ex in getattr(planned, "exercises", []) or []:
        key = ex.get("key")
        if not key:
            continue
        plan_sets = ex.get("sets") or []
        flags = warmup_flags(plan_sets, set_cfg, key)
        objetivo = [s for s, warm in zip(plan_sets, flags, strict=True) if not warm]

        # Un ejercicio con series en el plan pero NINGUNA efectiva no se puede
        # comprobar: `all([])` es `True`, así que saldría limpio sin haber
        # mirado nada, la racha avanzaría y con ella la carga. Es la misma
        # comprobación-que-no-puede-fallar que ya se cierra dentro de
        # `_alcanza`, un nivel más arriba.
        #
        # El validador lo hace inalcanzable desde el YAML -exige `sets` no vacía
        # y `heuristic.count < sets_gte`-, y por eso mismo está aquí: si alguna
        # vez deja de serlo, que se entere alguien.
        if plan_sets and not objetivo:
            raise HevyError(
                f"'{key}': las {len(plan_sets)} series del plan son todas de "
                f"calentamiento, así que no hay nada efectivo que comprobar y el "
                f"ejercicio saldría 'completado' sin una sola prueba. Revisa "
                f"'set_types' o el marcado de las series."
            )

        candidatos = hechos.get(str(ex.get("template_id") or "")) or hechos.get(
            str(ex.get("name") or "")
        )
        reales = (
            None
            if not candidatos
            else [
                s
                for s in candidatos
                if str(s.get("type", "normal")).lower() not in {"warmup", "warm_up"}
            ]
        )
        salida[str(key)] = (objetivo, reales)
    return salida


def workout_compliance(
    workout: dict[str, Any],
    planned: Any,
    config: Any = None,
) -> dict[str, bool]:
    """¿Se completó cada ejercicio a lo que se le pedía? Una entrada por ejercicio.

    Es la señal que alimenta la racha de sesiones limpias, y por tanto lo único
    que abre la puerta de la subida de carga. Se compara contra lo PLANIFICADO,
    no contra lo que Hevy diga que era la rutina: la rutina en Hevy la reescribe
    este mismo sistema cada mañana, así que compararla consigo misma no diría
    nada.

    Criterio: todas las series efectivas (el calentamiento no cuenta) tienen que
    alcanzar las reps -y el PESO- del plan. Una serie de menos, una serie a
    menos reps o una serie más ligera, y el ejercicio no es limpio.

    Un ejercicio del plan que no aparece en el entrenamiento cuenta como NO
    cumplido. Es lo prudente: si no está, o no se hizo o no se registró, y en
    ninguno de los dos casos hay pruebas de que se completara.
    """
    salida: dict[str, bool] = {}
    for key, (objetivo, reales) in _emparejar(workout, planned, config).items():
        if reales is None or len(reales) < len(objetivo):
            salida[key] = False
            continue
        salida[key] = all(
            _alcanza(real, plan) for real, plan in zip(reales, objetivo)
        )
    return salida


def pesos_ejecutados(
    workout: dict[str, Any],
    planned: Any,
    config: Any = None,
) -> dict[str, float | None]:
    """El peso de la serie efectiva MÁS PESADA de cada ejercicio del plan.

    `None` cuando el ejercicio no aparece en el entrenamiento o cuando ninguna
    de sus series lleva peso apuntado. Ese `None` es deliberadamente distinto de
    `0.0`: "no hay dato" y "se hizo sin carga" llevan a decisiones opuestas en
    `adoptar_cargas` -la primera no toca nada, la segunda sería una bajada- y
    fundirlos en un solo valor haría que un ejercicio sin registrar arrastrase
    el objetivo a cero en tres sesiones.

    El máximo, y no la media ni la última, por lo mismo que en el objetivo: un
    esquema en rampa 50/60/65 se resume por su serie top, que es la que define
    la carga de trabajo. Comparar medias mezclaría el calentamiento efectivo con
    la serie que de verdad manda.
    """
    salida: dict[str, float | None] = {}
    for key, (_objetivo, reales) in _emparejar(workout, planned, config).items():
        pesos = [
            float(s["weight_kg"])
            for s in (reales or [])
            if s.get("weight_kg") is not None
        ]
        salida[key] = max(pesos) if pesos else None
    return salida


# El plan y la respuesta de Hevy no llaman igual a lo mismo: el motor usa
# `duration_s` y la API devuelve `duration_seconds`. La correspondencia se
# escribe aquí, una vez, en vez de repartirla por comparaciones sueltas.
CAMPOS_SERIE = (("reps", "reps"), ("duration_s", "duration_seconds"))


def _alcanza(real: dict[str, Any], plan: dict[str, Any]) -> bool:
    """¿Una serie ejecutada cumple lo que se le pedía?

    Solo se miran las magnitudes que el plan pide. Un ejercicio por tiempo no
    tiene reps, y exigirle reps lo dejaría siempre en "no cumplido": la plancha
    lateral no subiría nunca.

    Hacer MÁS de lo pedido cumple. El criterio es "no se quedó corto", no "clavó
    el número": doce repeticiones cuando se pedían diez es una sesión limpia.

    EL PESO CUENTA, y esto es un arreglo, no un añadido. Antes se comparaban
    reps y segundos y nada más, así que una sesión hecha a 50 kg cuando el plan
    pedía 60 salía LIMPIA -las reps sí se habían hecho- y esa sesión limpia
    pagaba la siguiente subida de carga. El plan se iba a 62,5 mientras la
    realidad se quedaba en 50, separándose un poco más cada semana, sin un solo
    error por ninguna parte y en una espalda con hernia L4-L5.

    Solo se exige peso cuando el plan pide peso MAYOR QUE CERO. Un ejercicio a
    peso corporal -plancha, dominadas- lleva `weight_kg` a 0 o sin poner, y
    exigirle un peso registrado lo dejaría eternamente en "no cumplido".

    Un peso que el plan pide y que en Hevy no está apuntado NO cumple. Es la
    misma prudencia que con las reps: la ausencia de dato no es prueba de nada,
    y tratarla como "cumple" convertiría "no apuntar el peso" en la forma de
    saltarse la comprobación entera.

    Una serie del plan que no pide NINGUNA magnitud es un error duro y no un
    "cumple". El bucle no tendría nada que comprobar y devolvería `True` sin
    haber mirado nada: el ejercicio saldría limpio sin una sola prueba de que se
    hizo, la racha avanzaría y con ella subiría la carga. Es justo el fallo que
    no puede pasar callando en una espalda con hernia.

    El peso no vale como magnitud medible a estos efectos: "60 kg" sin reps ni
    segundos no dice si la serie se terminó. Por eso no marca `comprobado`.
    """
    comprobado = False
    for campo_plan, campo_real in CAMPOS_SERIE:
        objetivo = plan.get(campo_plan)
        if objetivo is None:
            continue
        comprobado = True
        hecho = real.get(campo_real)
        if hecho is None or float(hecho) < float(objetivo):
            return False

    if not comprobado:
        raise HevyError(
            f"serie del plan sin magnitud medible ({sorted(plan)}): no se puede "
            f"decidir si se cumplió. Toda serie efectiva tiene que pedir al menos "
            f"una de {[p for p, _ in CAMPOS_SERIE]}."
        )

    objetivo_kg = plan.get("weight_kg")
    if objetivo_kg is not None and float(objetivo_kg) > 0:
        hecho_kg = real.get("weight_kg")
        if hecho_kg is None:
            return False
        # El epsilon es para el ruido de coma flotante -62,5 escrito y leído por
        # dos caminos distintos-, no una tolerancia de carga. Medio kilo de menos
        # sigue siendo medio kilo de menos.
        if float(hecho_kg) < float(objetivo_kg) - 1e-6:
            return False

    return True


# ---------------------------------------------------------------------------
# La forma del cuerpo del PUT
# ---------------------------------------------------------------------------
#
# Hevy NO acepta en el PUT la misma forma que devuelve en el GET, y la
# diferencia no es cosmética: manda un 400 entero. `index` y `title` viajan en
# la respuesta y están PROHIBIDOS en la petición.
#
#     {"error":"Unrecognized key(s) in object: 'index'..."}
#
# Esto costó descubrirlo lo que costó porque hasta el 2026-09-12 este sistema
# no había hecho NUNCA un PUT que llegara a Hevy: `write_enabled` arrancó en
# `false` y nadie lo abrió. La forma del cuerpo nunca se validó contra la API
# de verdad, solo contra un doble de pruebas que aceptaba cualquier cosa. Los
# seis tests de `restore` pasaban y la reversión estaba rota.
#
# Se usa LISTA BLANCA y no lista negra a propósito. Con lista negra bastaría
# que Hevy añadiera un campo nuevo a la respuesta para que una reversión
# -que reenvía lo que se leyó- volviera a fallar con el mismo 400, y el día que
# eso pasara sería el día en que hace falta revertir. La lista blanca deja esa
# puerta cerrada de una vez.
_EJERCICIO_PUT = frozenset(
    {"exercise_template_id", "superset_id", "rest_seconds", "notes", "sets"}
)
_SERIE_PUT = frozenset(
    {"type", "weight_kg", "reps", "distance_meters", "duration_seconds", "custom_metric"}
)
# Las que Hevy devuelve y rechaza. Se quitan sin ruido porque se sabe qué son;
# cualquier OTRA clave desconocida es un cambio de la API y sí se grita.
_SOLO_RESPUESTA = frozenset({"index", "title"})


def cuerpo_para_put(routine: dict[str, Any]) -> dict[str, Any]:
    """El cuerpo exacto que acepta `PUT /v1/routines/{id}`.

    Es el ÚNICO sitio que sabe qué forma tiene el cuerpo en la red. Lo usan las
    dos escrituras -la del día y la reversión- porque tenerlo en dos sitios
    significaría que la reversión se arregla y la escritura no, o al revés, y
    justo esa asimetría es la que dejó la reversión rota mientras sus tests
    pasaban.

    Acepta las dos formas de entrada -`{"routine": {...}}` o el diccionario
    pelado- porque las dos llegan: la del motor viene envuelta y la de una copia
    de seguridad viene sin envolver, que es tal cual como la devolvió el GET.

    Una clave desconocida es ERROR y no un descarte callado. Podría ser un
    campo nuevo que importe -un RPE, una nota- y tragárselo escribiría en Hevy
    una rutina a la que le falta algo sin que nadie se entere. Que reviente aquí
    es recuperable: la copia ya está hecha y el PUT todavía no ha salido.
    """
    r = routine.get("routine", routine)

    ejercicios: list[dict[str, Any]] = []
    for i, ex in enumerate(r.get("exercises") or []):
        sobra = set(ex) - _EJERCICIO_PUT - _SOLO_RESPUESTA
        if sobra:
            raise HevyError(
                f"el ejercicio {i} lleva claves que Hevy no reconoce en un PUT: "
                f"{sorted(sobra)}. Si la API ha cambiado, hay que decidir si ese "
                f"campo se escribe o se descarta; escribir sin él a ciegas no."
            )
        limpio = {k: v for k, v in ex.items() if k in _EJERCICIO_PUT and k != "sets"}
        series: list[dict[str, Any]] = []
        for j, s in enumerate(ex.get("sets") or []):
            sobra_s = set(s) - _SERIE_PUT - _SOLO_RESPUESTA
            if sobra_s:
                raise HevyError(
                    f"la serie {j} del ejercicio {i} lleva claves que Hevy no "
                    f"reconoce en un PUT: {sorted(sobra_s)}"
                )
            series.append({k: v for k, v in s.items() if k in _SERIE_PUT})
        limpio["sets"] = series
        ejercicios.append(limpio)

    return {
        "routine": {
            "title": r.get("title"),
            "notes": r.get("notes"),
            "exercises": ejercicios,
        }
    }


def _set_payload(
    index: int, s: dict[str, Any], es_calentamiento: bool = False
) -> dict[str, Any]:
    """Una serie en el formato exacto que espera Hevy.

    Los campos que no aplican van a `null` explícito y no se omiten: así el
    cuerpo tiene siempre la misma forma y una comparación entre lo que había y
    lo que se manda no señala diferencias que no existen.

    `es_calentamiento` es la decisión del motor, no la etiqueta que traía la
    serie. Solo puede AÑADIR la marca: si la serie ya venía como `dropset` o
    `failure` y el motor no la considera calentamiento, se respeta lo que
    había. Este módulo no está para reetiquetar series por su cuenta.
    """
    tipo = str(s.get("type") or "normal").lower()
    if es_calentamiento:
        tipo = "warmup"
    return {
        "index": index,
        "type": tipo,
        "weight_kg": s.get("weight_kg"),
        "reps": s.get("reps"),
        "distance_meters": s.get("distance_m"),
        "duration_seconds": s.get("duration_s"),
        "custom_metric": None,
    }


def build_routine_payload(session: Any, config: Any = None) -> dict[str, Any]:
    """Cuerpo del PUT para la sesión de hoy, ANTES de pasarlo por el contrato.

    `session` es el `BuiltSession` del motor.

    Aquí ponía «el resultado es exactamente lo que viaja por la red: no hay
    ningún paso de transformación posterior». Era mentira, y de la cara: este
    diccionario lleva `index` y `title` en cada ejercicio, y Hevy contesta 400 a
    un PUT que los incluya. La frase se escribió cuando era verdad y se quedó
    ahí cuando dejó de serlo, que es como los comentarios hacen daño de verdad:
    el que la leyera daba por comprobado un contrato que nadie había comprobado.

    `index` y `title` se siguen calculando porque los usan `payload_diff` y el
    mensaje de la mañana, donde un nombre legible vale más que un identificador
    de plantilla. Lo que sale a la red es `cuerpo_para_put(...)`, y ese es el
    único sitio que sabe qué acepta Hevy.

    `set_types.write_warmup_type_to_hevy` decide si la marca de calentamiento
    que calcula el motor se ESCRIBE en Hevy. Con la opción activa, la serie que
    el motor considera calentamiento sale del PUT como `warmup`, así que la
    próxima lectura la trae ya marcada y la rama `api` de `set_types.source`
    resuelve sola: se deja de depender de la heurística de "la primera de
    cuatro", que es una suposición sobre la rutina y no un dato.
    """
    raw = (config.raw if hasattr(config, "raw") else config) or {}
    routines = raw.get("routines", {}) or {}
    definicion = routines.get(session.routine_key, {}) or {}
    set_cfg = raw.get("set_types", {}) or {}
    marcar = bool(set_cfg.get("write_warmup_type_to_hevy", True))

    ejercicios: list[dict[str, Any]] = []
    for i, ex in enumerate(session.exercises or []):
        sets = ex.get("sets") or []
        # Las banderas se piden SIEMPRE, se escriban o no: así el ensayo en seco
        # y los tests ven el mismo cálculo que la escritura de verdad, y una
        # excepción de `warmup_flags` no aparece solo el día que se active la
        # opción.
        flags = warmup_flags(sets, set_cfg, ex.get("key")) if sets else []
        if not marcar:
            flags = [False] * len(sets)
        ejercicios.append(
            {
                "index": i,
                "title": ex.get("name") or ex.get("key"),
                "notes": ex.get("notes"),
                "exercise_template_id": ex.get("template_id"),
                # Se copia tal cual. Omitirlo deshace la superserie en la app.
                "superset_id": ex.get("superset_id"),
                "rest_seconds": ex.get("rest_seconds", 0),
                "sets": [
                    _set_payload(j, s, f)
                    for j, (s, f) in enumerate(zip(sets, flags, strict=True))
                ],
            }
        )

    return {
        "routine": {
            "title": session.title or definicion.get("title") or session.routine_key,
            "notes": session.notes if hasattr(session, "notes") else None,
            "exercises": ejercicios,
        }
    }


def payload_diff(antes: dict[str, Any] | None, despues: dict[str, Any]) -> list[str]:
    """Diferencias legibles entre la rutina remota y la que se escribiría.

    Existe para el ensayo en seco: enseñar el JSON entero obliga a compararlo a
    ojo, que es justo donde se cuelan los errores. Lo que importa es qué CAMBIA.
    """
    nuevo = despues.get("routine", despues)
    if antes is None:
        n = len(nuevo.get("exercises") or [])
        return [f"no se pudo leer el estado remoto; se escribirían {n} ejercicios"]

    lineas: list[str] = []
    viejos = {e.get("exercise_template_id"): e for e in (antes.get("exercises") or [])}
    nuevos = {e.get("exercise_template_id"): e for e in (nuevo.get("exercises") or [])}

    for tid in nuevos.keys() - viejos.keys():
        lineas.append(f"+ ALTA  {nuevos[tid].get('title')} ({tid})")
    for tid in viejos.keys() - nuevos.keys():
        lineas.append(f"- BAJA  {viejos[tid].get('title')} ({tid})")

    for tid in sorted(nuevos.keys() & viejos.keys(), key=lambda t: str(t)):
        a, b = viejos[tid], nuevos[tid]
        sa, sb = a.get("sets") or [], b.get("sets") or []
        titulo = b.get("title") or tid
        if len(sa) != len(sb):
            lineas.append(f"~ {titulo}: {len(sa)} -> {len(sb)} series")
        for j, (x, y) in enumerate(zip(sa, sb)):
            for campo, etiqueta in (
                ("weight_kg", "kg"),
                ("reps", "reps"),
                ("duration_seconds", "s"),
            ):
                vx, vy = x.get(campo), y.get(campo)
                if vx != vy:
                    lineas.append(
                        f"~ {titulo} serie {j + 1}: {etiqueta} {vx} -> {vy}"
                    )
    return lineas or ["sin cambios respecto a lo que ya hay en Hevy"]


# ---------------------------------------------------------------------------
# Copias de seguridad
# ---------------------------------------------------------------------------


@dataclass
class Backup:
    """Una copia del estado remoto anterior a una escritura."""

    path: Path
    routine_id: str
    taken_at: datetime
    payload: dict[str, Any]

    def describe(self) -> str:
        n = len(self.payload.get("exercises") or [])
        return (
            f"{self.path.name} ({n} ejercicios, "
            f"{self.taken_at:%Y-%m-%d %H:%M:%S})"
        )


def backup_dir(root: Path | str, routine_id: str) -> Path:
    return Path(root) / "hevy_backups" / str(routine_id)


def save_backup(root: Path | str, routine_id: str, remote: dict[str, Any]) -> Backup:
    """Guarda el estado remoto y VERIFICA que se puede releer.

    La relectura no es paranoia decorativa. Una copia que no se puede leer no
    es una copia, y descubrirlo en el momento de revertir —que es siempre el
    peor momento— es exactamente lo que este módulo existe para evitar.
    """
    ahora = datetime.now()
    carpeta = backup_dir(root, routine_id)
    carpeta.mkdir(parents=True, exist_ok=True)
    destino = carpeta / f"{ahora:%Y%m%d-%H%M%S}.json"

    contenido = {
        "routine_id": routine_id,
        "taken_at": ahora.isoformat(timespec="seconds"),
        "routine": remote,
    }
    try:
        with destino.open("w", encoding="utf-8") as fh:
            json.dump(contenido, fh, ensure_ascii=False, indent=2)
            fh.flush()
        releido = json.loads(destino.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HevyError(
            f"no se pudo guardar (o releer) la copia de seguridad en {destino}: "
            f"{exc}. NO se escribe en Hevy sin copia verificada."
        ) from exc

    if releido.get("routine") != remote:
        raise HevyError(
            f"la copia guardada en {destino} no coincide con lo leído de Hevy. "
            f"NO se escribe sin una copia fiable."
        )

    return Backup(path=destino, routine_id=routine_id, taken_at=ahora, payload=remote)


def prune_backups(root: Path | str, routine_id: str, keep_last: int) -> list[Path]:
    """Deja solo las `keep_last` copias más recientes. Devuelve las borradas.

    `integrations.hevy.backup.keep_last` llevaba escrito `30` desde el principio
    y no lo leía nadie. Se escribe una copia por escritura y se escribe una vez
    al día, así que la carpeta crecía sin techo: en Umbrel eso es un disco que se
    llena despacio, que es la forma de quedarse sin disco que menos se ve venir.
    Y cuando se llena, lo primero que falla es `save_backup`... que es justo lo
    que impide escribir en Hevy sin copia. El límite no es cosmético.

    Se borra POR FECHA DE NOMBRE y nunca la última: los nombres son
    `AAAAMMDD-HHMMSS.json`, así que ordenar alfabéticamente ya es ordenar por
    fecha. Y se llama DESPUÉS de haber guardado y verificado la copia nueva,
    nunca antes: el orden es lo único que garantiza que no hay un instante sin
    copia buena.

    Un fallo al borrar NO interrumpe nada. Una copia vieja que se queda es un
    problema de disco; abortar la escritura por eso sería convertir un problema
    de limpieza en un día sin entrenamiento. Pero se avisa: quedarse sin borrar
    en silencio es cómo se llega al disco lleno.
    """
    if keep_last < 1:
        # No es un límite: es la orden de quedarse sin ninguna copia. Se ignora
        # porque el módulo entero existe para que siempre haya una.
        log.warning(
            "keep_last=%d no tiene sentido (dejaría cero copias); no se borra nada",
            keep_last,
        )
        return []

    carpeta = backup_dir(root, routine_id)
    if not carpeta.is_dir():
        return []

    ficheros = sorted(carpeta.glob("*.json"))
    sobrantes = ficheros[:-keep_last] if len(ficheros) > keep_last else []

    borradas: list[Path] = []
    for f in sobrantes:
        try:
            f.unlink()
        except OSError as exc:
            log.warning("no se pudo borrar la copia vieja %s: %s", f, exc)
        else:
            borradas.append(f)

    if borradas:
        log.info(
            "copias de %s: %d borradas, se conservan las %d últimas",
            routine_id, len(borradas), keep_last,
        )
    return borradas


def leer_backup(path: Path | str, routine_id: str | None = None) -> Backup | None:
    """Lee UNA copia concreta. `None` si no se puede usar, y se dice por qué.

    Está separado de `latest_backup` porque hay dos formas de llegar a una
    copia -la última, o una elegida a mano para saltarse una escritura mala- y
    antes solo existía la primera. Duplicar la lectura habría dejado dos sitios
    donde tratar un fichero corrupto, que es justo donde no conviene tener dos
    criterios.

    `taken_at` se leía sin red. Un JSON válido al que le falte el campo -uno de
    una versión anterior, o escrito a medias- no es un fichero corrupto, así que
    no lo cazaba el `except`: reventaba con un KeyError. Y esto se llama desde
    `restore`, o sea en el peor momento posible. Ahora da el mismo resultado que
    cualquier otra copia inservible: no la hay, y quien llame se entera por «no
    hay ninguna copia» en vez de por una traza.
    """
    fichero = Path(path)
    try:
        datos = json.loads(fichero.read_text(encoding="utf-8"))
        tomada = datetime.fromisoformat(datos["taken_at"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        log.error("copia ilegible %s: %s", fichero, exc)
        return None
    return Backup(
        path=fichero,
        routine_id=routine_id or str(datos.get("routine_id") or fichero.parent.name),
        taken_at=tomada,
        payload=datos.get("routine") or {},
    )


def latest_backup(root: Path | str, routine_id: str) -> Backup | None:
    """La copia más reciente de una rutina, o None si no hay ninguna."""
    carpeta = backup_dir(root, routine_id)
    if not carpeta.is_dir():
        return None
    ficheros = sorted(carpeta.glob("*.json"))
    if not ficheros:
        return None
    return leer_backup(ficheros[-1], routine_id)


def pending_marker(root: Path | str) -> Path:
    return Path(root) / "hevy_backups" / PENDING_NAME


def read_pending(root: Path | str) -> dict[str, Any] | None:
    """Si existe, una escritura anterior no llegó a confirmarse.

    Que esto devuelva algo significa que hay una rutina en Hevy cuyo estado no
    conocemos: puede ser la vieja, la nueva, o una mezcla. Quien lo lea debe
    avisar, no arreglarlo por su cuenta.
    """
    marca = pending_marker(root)
    if not marca.is_file():
        return None
    try:
        return json.loads(marca.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"routine_id": "?", "note": "marca de escritura en curso ilegible"}


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------


@dataclass
class WriteResult:
    written: bool
    routine_id: str
    backup: Backup | None = None
    diff: list[str] = field(default_factory=list)
    reason: str = ""
    error: str | None = None


@dataclass
class HevyClient:
    """Cliente sobre la API pública de Hevy v1."""

    api_key: str
    base_url: str = "https://api.hevyapp.com"
    data_root: Path = Path("data")
    write_enabled: bool = False  # apagado por defecto, a propósito
    # None = conservarlas todas, que es lo que se hacía antes de que esto se
    # leyera. El defecto de un ajuste ausente tiene que ser el comportamiento
    # anterior; borrar por iniciativa propia sería lo contrario.
    backup_keep_last: int | None = None

    def _headers(self) -> dict[str, str]:
        return {"api-key": self.api_key, "Content-Type": "application/json"}

    def _client(self):
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise HevyError("falta el paquete `httpx`") from exc
        return httpx.Client(base_url=self.base_url, timeout=TIMEOUT_S)

    # --- lectura ------------------------------------------------------------

    def get_routine(self, routine_id: str) -> dict[str, Any]:
        with self._client() as c:
            r = c.get(f"/v1/routines/{routine_id}", headers=self._headers())
        if r.status_code != 200:
            raise HevyError(
                f"GET rutina {routine_id} devolvió {r.status_code}: {r.text[:200]}"
            )
        datos = r.json()
        # La API a veces envuelve en {"routine": {...}} y a veces devuelve una
        # lista de un elemento. Se normaliza aquí para que el resto del módulo
        # vea siempre un dict de rutina.
        if isinstance(datos, dict) and "routine" in datos:
            datos = datos["routine"]
        if isinstance(datos, list):
            datos = datos[0] if datos else {}
        return datos

    def get_workouts(self, since: date, *, max_pages: int = 5) -> list[dict[str, Any]]:
        """Los entrenamientos registrados desde `since` (incluido).

        Sin esto el sistema no progresa NUNCA. La racha de sesiones limpias solo
        avanza cuando `advance_state` recibe qué se completó de verdad, y eso
        únicamente se puede saber leyendo lo que se hizo. Un sistema que decide
        cada mañana pero nunca se entera de si la sesión se ejecutó manda el
        mensaje correcto todos los días con los mismos pesos para siempre.

        Se pagina hacia atrás y se corta en cuanto se pasa de `since`: la API
        devuelve lo más reciente primero y no hace falta traerse el histórico
        entero cada mañana.

        POR QUÉ SE MIRA `page_count`
        ----------------------------
        Hevy NO devuelve una página vacía cuando se piden más páginas de las que
        hay: devuelve **404 «Page not found»**. Y como aquí cualquier respuesta
        distinta de 200 es un error duro, pedir una ventana más larga que el
        histórico de la cuenta reventaba. Medido el 2026-09-13 contra la cuenta
        real: 14 entrenamientos, `page_count: 2`, y `POST /api/reconcile?dias=30`
        moría con «página 3 devolvió 404». El trabajo nocturno se libraba de
        casualidad, porque con `dias_atras=3` la primera página ya cubre la
        ventana entera.

        La respuesta trae `page_count` desde siempre y este código lo ignoraba.
        Ahora se usa: al llegar a la última página la búsqueda está COMPLETA -no
        existe nada más antiguo que traer, así que no falta ninguna sesión por
        reconciliar- y no se pide una página que ya se sabe que no está. Si
        algún día la respuesta no lo trajera, se sigue como antes: página vacía
        = fin, y quedarse sin páginas = error. Los dos caminos avisan a gritos y
        ninguno devuelve una lista a medias haciéndola pasar por entera.
        """
        salida: list[dict[str, Any]] = []
        completo = False
        with self._client() as c:
            for pagina in range(1, max_pages + 1):
                r = c.get(
                    "/v1/workouts",
                    headers=self._headers(),
                    params={"page": pagina, "pageSize": 10},
                )
                if r.status_code != 200:
                    raise HevyError(
                        f"GET /v1/workouts (página {pagina}) devolvió "
                        f"{r.status_code}: {r.text[:200]}"
                    )
                datos = r.json() or {}
                lote = datos.get("workouts") if isinstance(datos, dict) else datos
                if not lote:
                    completo = True
                    break

                # "Se ha pasado de la ventana" y "no se le puede leer la fecha"
                # son cosas distintas y antes se trataban igual. Un solo
                # entrenamiento con la fecha rota cortaba la paginación entera y
                # se perdían en silencio todos los anteriores: la sesión de ayer
                # dejaba de contar porque la de hoy tenía el `start_time` raro.
                # Sin fecha se salta el entrenamiento; solo la fecha anterior a
                # `since` da por terminada la búsqueda.
                for w in lote:
                    dia = _fecha_workout(w)
                    if dia is None:
                        continue
                    if dia < since:
                        completo = True
                        break
                    salida.append(w)
                if completo:
                    break
                if _es_ultima_pagina(datos, pagina):
                    # Se ha leído la cuenta entera. No se llega a `since` porque
                    # no hay nada tan antiguo, que es lo contrario de un
                    # truncamiento: no falta nada.
                    completo = True
                    break

        if not completo:
            # Se acabaron las páginas antes de llegar a `since`: lo que se
            # devuelve es un trozo, y un trozo tiene el mismo aspecto que la
            # lista entera. Quien reconcilia daría por no hecha una sesión que
            # sí está, solo que en la página siguiente.
            raise HevyError(
                f"GET /v1/workouts: {max_pages} páginas no bastan para cubrir "
                f"desde {since}; la lista estaría incompleta y faltarían "
                f"sesiones por reconciliar. Sube `max_pages`."
            )
        return salida

    # --- escritura ----------------------------------------------------------

    def write_routine(
        self,
        routine_id: str,
        payload: dict[str, Any],
        *,
        dry_run: bool = False,
    ) -> WriteResult:
        """Escribe una rutina. Copia antes, marca durante, confirma después."""

        # 1. El interruptor general. Se comprueba aquí y no en el llamante.
        if not self.write_enabled:
            return WriteResult(
                written=False,
                routine_id=routine_id,
                reason=(
                    "integrations.hevy.write_enabled está en false: modo solo "
                    "lectura. No se ha tocado nada en Hevy."
                ),
            )

        # 2. Estado remoto actual + copia verificada. Sin esto no se sigue.
        remoto = self.get_routine(routine_id)
        copia = save_backup(self.data_root, routine_id, remoto)
        # Después de guardar y verificar la nueva, nunca antes.
        if self.backup_keep_last is not None:
            prune_backups(self.data_root, routine_id, self.backup_keep_last)
        diff = payload_diff(remoto, payload)

        if dry_run:
            return WriteResult(
                written=False,
                routine_id=routine_id,
                backup=copia,
                diff=diff,
                reason="--dry-run: copia hecha, PUT no enviado",
            )

        # 3. Marca de escritura en curso. Sobrevive a que el proceso muera.
        marca = pending_marker(self.data_root)
        marca.parent.mkdir(parents=True, exist_ok=True)
        marca.write_text(
            json.dumps(
                {
                    "routine_id": routine_id,
                    "backup": str(copia.path),
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        # 4. El PUT. `cuerpo_para_put` es el paso que faltaba: el payload que
        # construye el motor lleva `index` y `title`, que sirven para el diff y
        # para el mensaje pero que Hevy rechaza con un 400.
        try:
            cuerpo = cuerpo_para_put(payload)
        except HevyError as exc:
            # Antes de salir por la red y con la copia ya hecha: el mejor sitio
            # posible para descubrir que el cuerpo no vale.
            return WriteResult(
                written=False,
                routine_id=routine_id,
                backup=copia,
                diff=diff,
                error=f"{exc} Copia en {copia.path}",
            )
        try:
            with self._client() as c:
                r = c.put(
                    f"/v1/routines/{routine_id}",
                    headers=self._headers(),
                    json=cuerpo,
                )
        except Exception as exc:  # noqa: BLE001 - red: puede fallar de mil formas
            # La marca se queda a propósito: no sabemos si el PUT llegó.
            return WriteResult(
                written=False,
                routine_id=routine_id,
                backup=copia,
                diff=diff,
                error=(
                    f"la petición falló ({exc}). NO se sabe si Hevy llegó a "
                    f"aplicarla: la marca de escritura en curso sigue puesta y "
                    f"la copia está en {copia.path}."
                ),
            )

        if r.status_code not in (200, 201):
            return WriteResult(
                written=False,
                routine_id=routine_id,
                backup=copia,
                diff=diff,
                error=(
                    f"PUT devolvió {r.status_code}: {r.text[:200]}. Copia en "
                    f"{copia.path}"
                ),
            )

        # 5. Confirmado: se retira la marca.
        marca.unlink(missing_ok=True)
        return WriteResult(
            written=True,
            routine_id=routine_id,
            backup=copia,
            diff=diff,
            reason="escritura confirmada",
        )

    # --- reversión ----------------------------------------------------------

    def restore(self, routine_id: str, backup: Backup | None = None) -> WriteResult:
        """Devuelve la rutina al estado de una copia.

        Ignora `write_enabled` a propósito: si el interruptor bloqueara la
        reversión, el modo seguro impediría deshacer un desastre causado
        mientras estaba abierto. Restaurar nunca es más peligroso que el estado
        del que se viene.
        """
        copia = backup or latest_backup(self.data_root, routine_id)
        if copia is None:
            raise HevyError(
                f"no hay ninguna copia guardada de la rutina {routine_id}: "
                f"no se puede revertir"
            )

        # Una copia es la respuesta del GET tal cual, con `index` y `title` en
        # cada ejercicio. Devolverla sin limpiar era un 400 seguro, y ahí estuvo
        # rota la reversión desde el primer día: sus seis tests usaban un doble
        # que aceptaba cualquier cuerpo, así que probaban la lógica y no el
        # contrato. Un test que no puede fallar por el motivo real no prueba.
        cuerpo = cuerpo_para_put(copia.payload)
        with self._client() as c:
            r = c.put(
                f"/v1/routines/{routine_id}",
                headers=self._headers(),
                json=cuerpo,
            )
        if r.status_code not in (200, 201):
            raise HevyError(
                f"la reversión devolvió {r.status_code}: {r.text[:200]}"
            )

        pending_marker(self.data_root).unlink(missing_ok=True)
        return WriteResult(
            written=True,
            routine_id=routine_id,
            backup=copia,
            reason=f"revertida al estado de {copia.taken_at:%Y-%m-%d %H:%M:%S}",
        )


def build_client(settings: Any, config: Any = None) -> HevyClient:
    """Construye el cliente leyendo el interruptor del YAML."""
    raw = (config.raw if hasattr(config, "raw") else config) or {}
    hevy_cfg = ((raw.get("integrations") or {}).get("hevy") or {})
    if not settings.hevy_api_key:
        raise HevyError(
            "Falta HEVY_API_KEY en el fichero .env. Escríbelo tú: el sistema no "
            "pide credenciales por consola."
        )
    return HevyClient(
        api_key=settings.hevy_api_key,
        base_url=settings.hevy_api_base,
        data_root=Path(settings.database_url.split("///")[-1]).parent
        if "///" in str(settings.database_url)
        else Path("data"),
        write_enabled=bool(hevy_cfg.get("write_enabled", False)),
        backup_keep_last=(
            int(guardar) if (guardar := (hevy_cfg.get("backup") or {}).get("keep_last"))
            is not None else None
        ),
    )
