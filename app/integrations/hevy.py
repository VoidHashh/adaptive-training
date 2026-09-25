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

    Y LA REGLA ES MÁS ANCHA QUE ESTA FUNCIÓN: el título tampoco vale como prueba
    de lo que el usuario QUERÍA hacer. El mismo 2026-09-13, después de haber
    establecido aquí arriba que 4 de 14 títulos nombran una rutina que no es la
    suya, se usaron dos entrenamientos titulados «Día 2 y 3 HIIT» como prueba de
    que el HIIT del Día 3 seguía formando parte de su práctica, y se propuso
    tocar `hiit.never_routines` en el `config.yaml` por ello. Era falso: esos
    dos títulos son del 14 y el 19 de agosto, de cuatro semanas que ya
    terminaron, y lo de septiembre es «Día 1 HIIT» y «Día 2 HIIT», que es
    exactamente lo que el config ya declaraba. El error no fue leer mal el dato,
    fue tratar como dato una cadena de texto de la que se acababa de demostrar
    que miente el 29% de las veces -y proponer un cambio de configuración
    encima-.

    Dicho de una vez, para que no haya que volver a descubrirlo: el título es
    una etiqueta congelada en el momento de ejecutar, editable a mano, y que no
    se vuelve a tocar cuando la rutina cambia de nombre. NO CLASIFICA, NO DATA Y
    NO PRUEBA INTENCIÓN. Lo que se hizo se lee del `routine_id` y de los
    ejercicios; lo que se quiere hacer se lee del `config.yaml`, o se pregunta.

    REMEDIDO EL 2026-09-17, YA CON LOS 16 ENTRENAMIENTOS EN `workout_log`, que
    es la primera vez que se puede contar sobre la tabla y no sobre una sonda.
    Las discrepancias siguen siendo CUATRO -2026-08-14 y 2026-08-20 titulados
    «Día 3» con el id de `dia_1`, 2026-08-17 y 2026-08-24 titulados «Día 1» con
    el id de `dia_3`- y están TODAS el 2026-08-24 o antes. Del 2026-08-25 en
    adelante los doce coinciden.

    Eso encaja con el renombrado y NO ablanda la regla, ni un poco. Que hoy
    coincidan es una propiedad de los datos de hoy, no una garantía del formato:
    el título se puede editar a mano mañana y el siguiente renombrado vuelve a
    desalinear el pasado entero de golpe. Lo que sí cambia es el reparto del
    daño: el tonelaje recuperado de agosto queda agrupado por `routine_key`, o
    sea bien, y quien mire la tabla a mano y ordene por título verá cuatro filas
    en el día que no es.

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

        # La presencia se mira por la CLAVE, no por si la lista trae algo. Un
        # ejercicio abierto en Hevy y dejado sin una sola serie apuntada entra
        # aquí con lista vacía, y tratar esa vacía como "no aparece" fundía dos
        # cosas distintas: «no lo hice» y «lo hice y no lo apunté». Ninguna de
        # las dos cumple, pero solo de la segunda hay rastro de haber estado
        # ahí, y el mensaje de la noche puede decirlo en vez de acusar.
        presente = next(
            (
                c
                for c in (str(ex.get("template_id") or ""), str(ex.get("name") or ""))
                if c in hechos
            ),
            None,
        )
        reales = (
            None
            if presente is None
            else [
                s
                for s in hechos[presente]
                if str(s.get("type", "normal")).lower() not in {"warmup", "warm_up"}
            ]
        )
        salida[str(key)] = (objetivo, reales)
    return salida


def workout_compliance(
    workout: dict[str, Any],
    planned: Any,
    config: Any = None,
    *,
    ignorar_peso: bool = False,
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

    El veredicto se saca de `motivos_incumplimiento` y no de una segunda cuenta
    en paralelo: cumplir es exactamente no tener motivo. Contarlo dos veces
    dejaría abierta la puerta a que el mensaje explicara un fallo que el estado
    no tiene, o peor, a que el estado castigara un fallo que nadie sabe nombrar.
    """
    return {
        key: motivo is None
        for key, motivo in _motivos(
            workout, planned, config, ignorar_peso=ignorar_peso
        ).items()
    }


# Un ejercicio que no aparece no dice por sí solo qué pasó, y esta frase es lo
# único honesto que se puede escribir. Es una constante porque quien une los
# entrenamientos de un día -una sesión partida en dos ratos es normal- necesita
# reconocerla para preferir el motivo del rato donde el ejercicio SÍ estaba.
SIN_RASTRO = "no aparece en el entrenamiento: o no se hizo, o se hizo sin apuntarlo"


def motivos_incumplimiento(
    workout: dict[str, Any],
    planned: Any,
    config: Any = None,
    *,
    ignorar_peso: bool = False,
) -> dict[str, str]:
    """Por qué NO cumplió cada ejercicio que no cumplió. Los demás no salen.

    Existe porque `workout_compliance` devuelve un booleano y el booleano se
    traga la causa. Con esa pérdida, el mensaje de la noche tenía que inventarse
    una: decía «no se completó a las reps objetivo» SIEMPRE, también cuando las
    reps estaban perfectas y lo que se había quedado corto era el peso. Mandaba
    a revisar unas reps que estaban bien mientras callaba lo que de verdad había
    fallado.
    """
    return {
        key: motivo
        for key, motivo in _motivos(
            workout, planned, config, ignorar_peso=ignorar_peso
        ).items()
        if motivo is not None
    }


def _motivos(
    workout: dict[str, Any],
    planned: Any,
    config: Any = None,
    *,
    ignorar_peso: bool = False,
) -> dict[str, str | None]:
    """Cada ejercicio del plan con su motivo, o `None` si cumplió."""
    salida: dict[str, str | None] = {}
    for key, (objetivo, reales) in _emparejar(workout, planned, config).items():
        salida[key] = _motivo(objetivo, reales, ignorar_peso=ignorar_peso)
    return salida


def _motivo(
    objetivo: list[dict[str, Any]],
    reales: list[dict[str, Any]] | None,
    *,
    ignorar_peso: bool,
) -> str | None:
    if reales is None:
        return SIN_RASTRO
    if objetivo and not reales:
        return (
            "está en el entrenamiento pero sin una sola serie efectiva apuntada: "
            "se hizo y no se registró, o se abrió y se dejó"
        )
    if len(reales) < len(objetivo):
        return f"se apuntaron {len(reales)} de las {len(objetivo)} series"
    for i, (real, plan) in enumerate(zip(reales, objetivo), start=1):
        falla = _falla(real, plan, ignorar_peso=ignorar_peso)
        if falla is not None:
            return f"la serie {i} {falla}"
    return None


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

# Cómo se lee cada magnitud cuando se queda corta. Las frases están escritas
# enteras, y no compuestas con el nombre del campo, porque "reps" es femenino y
# "segundos" masculino: «no lleva segundos apuntadas» en el móvil parece un
# fallo del sistema y hace dudar del aviso correcto que lo acompaña.
FRASES_SERIE = {
    "reps": ("no lleva reps apuntadas", "se quedó en {hecho} de las {pedido} reps"),
    "duration_s": (
        "no lleva segundos apuntados",
        "se quedó en {hecho} de los {pedido} segundos",
    ),
}


def _num(x: Any) -> str:
    return f"{float(x):g}".replace(".", ",")


def _alcanza(
    real: dict[str, Any], plan: dict[str, Any], *, ignorar_peso: bool = False
) -> bool:
    """¿Una serie ejecutada cumple lo que se le pedía? Las reglas, en `_falla`."""
    return _falla(real, plan, ignorar_peso=ignorar_peso) is None


def _falla(
    real: dict[str, Any], plan: dict[str, Any], *, ignorar_peso: bool
) -> str | None:
    """En qué se quedó corta una serie ejecutada, o `None` si cumple.

    `ignorar_peso` NO TIENE DEFECTO, y eso lo decidió el banco de mutaciones del
    25/09/2026: con `= False` puesto, cambiarlo a `= True` no ponía rojo ni un
    test, porque los dos únicos llamantes -`_alcanza` y `_motivo`- lo pasan
    siempre explícito. Un defecto que nadie lee es un defecto que no defiende
    nada y que el día que alguien añada un tercer llamante decidirá por él, en
    la dirección de mirar menos. Los defectos viven arriba, en las dos puertas
    públicas, que es donde hay llamadas que de verdad los usan.

    `ignorar_peso` SOLO lo usa la adopción hacia arriba, y el motivo está en
    `app/engine/adoption.py`. En dos líneas: una serie más LIGERA de lo pedido
    no es una serie fallada si sus reps están completas, es el escalón de abajo
    de una rampa. El 21/09/2026 la patada atrás se hizo 30/40/50 kg con las 24
    reps en las tres, y el sistema se negó a adoptar los 50 porque la primera
    iba a 30 cuando pedía 35. Estaba rechazando la prueba MÁS fuerte -24 reps a
    50- por culpa de la más floja.

    Lo que NO se ignora nunca es que las reps o los segundos se queden cortos:
    70 kg a 4 reps cuando se pedían 10 sigue sin ser un objetivo nuevo. Y el
    veredicto normal -el que alimenta la racha de sesiones limpias y por tanto
    la progresión- sigue exigiendo el peso, porque ahí el riesgo es el
    contrario: una sesión hecha a 50 cuando el plan pedía 60 no puede pagar la
    siguiente subida.

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

    DEVUELVE LA FRASE Y NO UN BOOLEANO
    ----------------------------------
    Porque el booleano se tragaba la causa y quien tenía que explicarla se la
    inventaba. El mensaje de la noche decía «no se completó a las reps objetivo»
    en los cinco casos, también cuando las reps estaban clavadas y lo corto era
    el peso: mandaba a mirar un número que estaba bien y callaba el que no.
    """
    comprobado = False
    for campo_plan, campo_real in CAMPOS_SERIE:
        objetivo = plan.get(campo_plan)
        if objetivo is None:
            continue
        comprobado = True
        sin_dato, corta = FRASES_SERIE[campo_plan]
        hecho = real.get(campo_real)
        if hecho is None:
            return sin_dato
        if float(hecho) < float(objetivo):
            return corta.format(hecho=_num(hecho), pedido=_num(objetivo))

    if not comprobado:
        raise HevyError(
            f"serie del plan sin magnitud medible ({sorted(plan)}): no se puede "
            f"decidir si se cumplió. Toda serie efectiva tiene que pedir al menos "
            f"una de {[p for p, _ in CAMPOS_SERIE]}."
        )

    objetivo_kg = plan.get("weight_kg")
    if not ignorar_peso and objetivo_kg is not None and float(objetivo_kg) > 0:
        hecho_kg = real.get("weight_kg")
        if hecho_kg is None:
            return "no lleva peso apuntado"
        # El epsilon es para el ruido de coma flotante -62,5 escrito y leído por
        # dos caminos distintos-, no una tolerancia de carga. Medio kilo de menos
        # sigue siendo medio kilo de menos.
        if float(hecho_kg) < float(objetivo_kg) - 1e-6:
            return (
                f"se hizo a {_num(hecho_kg)} kg y pedía {_num(objetivo_kg)}"
            )

    return None


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

# Y LA LISTA BLANCA COMPROBABA NOMBRES, QUE ES LA MITAD DEL CONTRATO
# ------------------------------------------------------------------
# La otra mitad son los TIPOS, y por ahí se coló la escritura del 2026-09-14:
# `notes` viajaba como array JSON en un campo de texto. El nombre estaba en la
# lista blanca, así que el filtro lo dejó pasar sin una palabra, y el error
# apareció en el único sitio donde ya no se puede hacer nada: la respuesta de
# Hevy, un 400 cuyo texto este módulo no llegaba a guardar en ningún sitio.
#
# Se comprueba aquí y no más tarde porque aquí todavía es recuperable: la copia
# ya está hecha, el PUT aún no ha salido y el mensaje de la mañana puede decir
# exactamente qué campo tiene qué tipo. Un cuerpo mal tipado no se manda para
# que lo rechacen: se para antes.
#
# LO QUE VALIDA HEVY Y LO QUE NO, MEDIDO (`scripts/sondeo_contrato_hevy.py`)
# -----------------------------------------------------------------------
# Sondeado el 2026-09-14 contra la API real, con un `routine_id` inexistente
# para no tocar nada. Hevy valida el cuerpo ANTES de buscar la rutina, así que
# el control -cuerpo bueno- dio 404 y todos los 400 vinieron del cuerpo:
#
#     CAMPOS DE TEXTO: se validan de verdad.
#       notes = []      -> 400 Expected string, received array
#       notes = 5       -> 400 Expected string, received number
#       title ausente   -> 400 Required
#
#     CAMPOS NUMÉRICOS: NO se validan, se COERCIONAN, que es mucho peor.
#       weight_kg = []  -> ACEPTADO. `Number([])` es 0: el peso se escribiría
#                          como CERO KILOS y nadie diría una palabra.
#       reps = True     -> ACEPTADO. `Number(true)` es 1: una repetición.
#       reps = "8"      -> ACEPTADO como 8.
#       reps = "ocho"   -> 400 Expected number, received nan (el único que salta)
#
# De ahí que esta comprobación no sobre por el hecho de que Hevy valide. En los
# campos de texto Hevy avisa pero NO DICE QUÉ CAMPO ES -el 400 entero es
# «Expected string, received array», sin ruta ni índice-, y en los numéricos no
# avisa en absoluto: convierte en silencio. Este proyecto tiene un nombre para
# eso desde hace meses, y es el motivo por el que la comprobación es local,
# estricta y anterior al envío.
def _es_escalar(valor: Any) -> bool:
    """¿Es un valor que puede ir tal cual en un campo simple de la API?

    `bool` se excluye a propósito aunque en Python sea un `int`. Y la razón NO
    es que Hevy lo rechace: está medido que lo acepta. `reps: true` pasa la
    validación y se guarda como UNA repetición, porque `Number(true)` es 1 en
    JavaScript. O sea que un `True` que se cuele en `reps` no da error en
    ninguna parte: escribe una serie de una repetición y sigue la mañana como si
    nada. Por eso se para aquí, que es el último sitio donde todavía es un
    fallo visible en vez de un número plausible.
    """
    if isinstance(valor, bool):
        return False
    return valor is None or isinstance(valor, (str, int, float))


def _exigir_texto(valor: Any, donde: str) -> None:
    """Un campo de texto NULABLE: texto o nada. Es el caso de `notes`."""
    if valor is not None and not isinstance(valor, str):
        raise HevyError(
            f"{donde} tiene que ser texto o nada, y es {type(valor).__name__} "
            f"({valor!r}). Hevy contesta 400 y la rutina se queda como estaba."
        )


def _exigir_texto_obligatorio(valor: Any, donde: str) -> None:
    """Un campo de texto que NO admite nulo. Es el caso de `title`.

    NO ES LA MISMA COMPROBACIÓN, Y TRATARLAS IGUAL ERA UN AGUJERO. Aquí se
    usaba `_exigir_texto` para los dos campos, o sea «texto o nada», y está
    medido que en el título el «o nada» no vale:

        title = None        -> 400
        title ausente       -> 400 Required
        notes = None        -> aceptado
        notes ausente       -> aceptado

    Y el camino para llegar a un título nulo existía de verdad, no es teoría:
    `build_routine_payload` lo resuelve con `session.title or
    definicion.get("title") or session.routine_key`, y `BuiltSession.routine_key`
    está declarado `str | None`. Con un título vacío y una clave nula el cuerpo
    salía con `"title": null`, pasaba el contrato local sin una queja y se comía
    un 400 en la red. El mismo fallo que las notas, en el campo de al lado y
    esperando su turno.
    """
    if not isinstance(valor, str) or not valor.strip():
        raise HevyError(
            f"{donde} tiene que ser un texto con contenido, y es "
            f"{type(valor).__name__} ({valor!r}). Hevy lo exige (contesta 400 "
            f"«Required») y la rutina se queda como estaba."
        )


# Los campos que Hevy trata como números, medidos uno a uno contra la API real.
# NO se valida ni uno solo en el servidor: los seis aceptan `[]`, `None` y
# cadenas sin una queja. Están aquí en una lista, y no repartidos por el código,
# para que añadir un campo numérico nuevo a `_SERIE_PUT` y olvidarse de
# validarlo sea un despiste que se ve de un vistazo. Ver `_exigir_numero`.
_NUMERICOS_EJERCICIO = ("rest_seconds",)
_NUMERICOS_SERIE = (
    "weight_kg",
    "reps",
    "distance_meters",
    "duration_seconds",
    "custom_metric",
)


def _exigir_numero(valor: Any, donde: str) -> None:
    """Un campo numérico: un número de verdad, o nada. NADA DE CADENAS.

    ESTE ES EL GUARDIA QUE NO TIENE PAREJA EN EL SERVIDOR, Y ESTÁ MEDIDO
    ---------------------------------------------------------------------
    Los campos de texto los valida Hevy: un array en `notes` o en `title` da 400
    y la rutina se queda como estaba. Con los numéricos no pasa nada de eso. Se
    sondearon los seis contra la API real y NINGUNO se valida:

        rest_seconds = []       ACEPTADO      weight_kg = ''      ACEPTADO
        weight_kg = []          ACEPTADO      reps = ''           ACEPTADO
        distance_meters = []    ACEPTADO      weight_kg = '  '    ACEPTADO
        duration_seconds = []   ACEPTADO      reps = True         ACEPTADO
        custom_metric = []      ACEPTADO      reps = '8'          ACEPTADO
        weight_kg = None        ACEPTADO      rest_seconds = '90' ACEPTADO

    Lo único que da 400 es `reps = 'ocho'`, y no porque se valide el tipo sino
    porque `Number('ocho')` es `NaN`. No hay validación: hay COERCIÓN, que es
    otra cosa y bastante peor.

    POR QUÉ LA COERCIÓN ES PEOR QUE EL RECHAZO. Un rechazo se nota: 400, la
    rutina se queda como estaba, y el mensaje de la mañana lo dice. Una coerción
    no se nota en ninguna parte, porque en JavaScript:

        Number([])    -> 0        Number('')     -> 0
        Number(null)  -> 0        Number('   ')  -> 0
        Number(true)  -> 1        Number('8')    -> 8

    Un peso mal tipado NO da error: se escribe en la rutina como CERO KILOS. Y
    un cero en un peso es un número perfectamente plausible, así que no hay nada
    -ni un log, ni un aviso, ni una excepción- que distinga «hoy toca barra
    vacía» de «el valor se perdió por el camino». Se descubre en el gimnasio.

    Y LAS CADENAS SE RECHAZAN AUNQUE HEVY LAS ACEPTE. `'60'` funciona: Hevy lo
    convierte a 60 y el peso queda bien. Pero admitir cadenas obliga a admitir
    `''`, porque es la MISMA comprobación (`isinstance(v, str)`), y `''` sale
    cero. Ése es el único camino conocido a un cero silencioso que `_es_escalar`
    NO ve, precisamente porque una cadena vacía es un escalar perfectamente
    válido. Los datos reales de la rutina son `int` o `None` en los seis campos
    -comprobado contra el GET-, así que cerrar la puerta a las cadenas no quita
    nada que se use y tapa el último hueco.

    El `None` SÍ se admite, y no es una concesión: es el valor de reposo de
    verdad. En la rutina real `distance_meters` y `custom_metric` son `None` en
    las 32 series, y `weight_kg` es `None` en 9 -los ejercicios sin peso-.
    Prohibirlo rompería la reversión desde cualquier copia.
    """
    if valor is None:
        return
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        raise HevyError(
            f"{donde} tiene que ser un número o nada, y es "
            f"{type(valor).__name__} ({valor!r}). OJO: Hevy NO rechaza esto "
            f"-no valida ni uno solo de sus campos numéricos-, lo CONVIERTE con "
            f"el `Number()` de JavaScript, y sale un número plausible que se "
            f"escribe en la rutina sin que nadie diga nada. Por eso se para "
            f"aquí: es el último sitio donde todavía es un fallo y no un peso."
        )


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

    Y UN VALOR DEL TIPO EQUIVOCADO TAMBIÉN ES ERROR, por lo mismo. Ver el
    comentario de `_es_escalar` arriba: comprobar solo los nombres dejó salir un
    `"notes": []` que costó la escritura de una mañana entera.
    """
    r = routine.get("routine", routine)
    _exigir_texto_obligatorio(r.get("title"), "el título de la rutina")
    _exigir_texto(r.get("notes"), "las notas de la rutina")

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
        _exigir_texto(limpio.get("notes"), f"las notas del ejercicio {i}")
        for clave in _NUMERICOS_EJERCICIO:
            if clave in limpio:
                _exigir_numero(limpio[clave], f"{clave} del ejercicio {i}")
        for clave, valor in limpio.items():
            if not _es_escalar(valor):
                raise HevyError(
                    f"el ejercicio {i} manda {clave}={valor!r} "
                    f"({type(valor).__name__}) donde Hevy espera un valor simple. "
                    f"El nombre del campo estaba en la lista blanca y el tipo no "
                    f"lo miraba nadie; ese hueco costó la escritura del "
                    f"2026-09-14."
                )
        series: list[dict[str, Any]] = []
        for j, s in enumerate(ex.get("sets") or []):
            sobra_s = set(s) - _SERIE_PUT - _SOLO_RESPUESTA
            if sobra_s:
                raise HevyError(
                    f"la serie {j} del ejercicio {i} lleva claves que Hevy no "
                    f"reconoce en un PUT: {sorted(sobra_s)}"
                )
            limpia = {k: v for k, v in s.items() if k in _SERIE_PUT}
            for clave in _NUMERICOS_SERIE:
                if clave in limpia:
                    _exigir_numero(
                        limpia[clave], f"{clave} de la serie {j} del ejercicio {i}"
                    )
            # El tipo de serie es el ÚNICO campo de texto de la serie, y a
            # diferencia de los numéricos éste SÍ lo valida Hevy: un tipo
            # inventado da `400 Invalid set type` y un array da `400 Expected
            # string, received array`. Aun así se comprueba aquí el tipo del
            # dato, porque el 400 de Hevy no dice qué serie de qué ejercicio.
            _exigir_texto(limpia.get("type"), f"el tipo de la serie {j} del ejercicio {i}")
            for clave, valor in limpia.items():
                if not _es_escalar(valor):
                    raise HevyError(
                        f"la serie {j} del ejercicio {i} manda {clave}={valor!r} "
                        f"({type(valor).__name__}) donde Hevy espera un valor "
                        f"simple."
                    )
            series.append(limpia)
        limpio["sets"] = series
        ejercicios.append(limpio)

    # UNA RUTINA SIN EJERCICIOS NO SE PUEDE ESCRIBIR, Y ESTÁ MEDIDO
    # -------------------------------------------------------------
    # `exercises: []` devuelve 400, igual que si el campo falta
    # (`scripts/sondeo_contrato_hevy.py`). Y es alcanzable desde el motor: las
    # retiradas por regla quitan ejercicios de la sesión, y nada garantiza que
    # quede alguno. El día que una combinación de reglas los quitara todos, esto
    # se iría a la red, volvería un 400 que no explica nada -«Required»- y el
    # mensaje de la mañana diría que la rutina no se escribió, sin más.
    #
    # Dicho aquí se sabe qué pasó: no es que Hevy fallara, es que la sesión se
    # quedó vacía, que es un problema de reglas y no de red.
    if not ejercicios:
        raise HevyError(
            "la rutina se quedaría sin un solo ejercicio y Hevy rechaza eso con "
            "un 400. Si las reglas de hoy han retirado todos los ejercicios, el "
            "problema está en las reglas: escribir una rutina vacía en la app no "
            "es lo que hay que hacer con un día así."
        )

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
            "title": _titulo_de_rutina(session, definicion),
            "notes": _notas_de_rutina(session),
            "exercises": ejercicios,
        }
    }


def _titulo_de_rutina(session: Any, definicion: dict[str, Any]) -> str:
    """El título de la rutina, que Hevy EXIGE y no admite nulo.

    Aquí ponía `session.title or definicion.get("title") or session.routine_key`
    y se devolvía tal cual. Los dos primeros eslabones están bien; el tercero es
    el problema, porque `BuiltSession.routine_key` está declarado `str | None`.
    Con el título vacío y la clave nula -un día de recuperación cuyo bloque no
    trajera título- el cuerpo salía con `"title": null`.

    Y eso NO lo cazaba nadie: el contrato de abajo usaba la comprobación de
    «texto o nada», que da por bueno el nulo. O sea el gemelo exacto del fallo
    de `notes`, en el campo de al lado, esperando el día en que la cadena de
    reservas se agotara. Está medido que Hevy contesta 400 a `title: null` y 400
    «Required» si falta (`scripts/sondeo_contrato_hevy.py`).

    Se levanta aquí en vez de devolver un título inventado del tipo «Rutina»:
    una rutina que llega sin nombre por ninguna de las tres vías es un fallo de
    construcción del motor, y taparlo con un nombre de relleno escribiría en
    Hevy una rutina llamada «Rutina» sin que nadie se enterara nunca.
    """
    for candidato in (
        getattr(session, "title", None),
        definicion.get("title"),
        getattr(session, "routine_key", None),
    ):
        if isinstance(candidato, str) and candidato.strip():
            return candidato
    raise HevyError(
        f"la sesión no tiene título por ninguna vía: ni `title` "
        f"({getattr(session, 'title', None)!r}), ni el `title` de la definición "
        f"en el config, ni `routine_key` "
        f"({getattr(session, 'routine_key', None)!r}). Hevy exige un título y "
        f"contesta 400 sin él; poner uno de relleno escribiría en la app una "
        f"rutina sin nombre de verdad."
    )


def _notas_de_rutina(session: Any) -> str | None:
    """Las notas de la sesión, como UNA cadena, que es lo que Hevy acepta.

    ESTO ROMPIÓ LA ESCRITURA DEL 2026-09-14 Y NADIE PUDO DECIR POR QUÉ
    ------------------------------------------------------------------
    Aquí ponía `session.notes if hasattr(session, "notes") else None`, y
    `BuiltSession.notes` es una `list[str]`. O sea que el cuerpo del PUT salía
    con `"notes": []` -un array JSON- en un campo que la propia API devuelve
    siempre como cadena o `null`. Hevy contestó que no, la rutina se quedó como
    estaba desde el 8 de septiembre, y el mensaje de la mañana dijo «la rutina
    NO se ha escrito en Hevy» sin poder añadir una palabra más.

    Y NO ES UNA RECONSTRUCCIÓN, ESTÁ MEDIDO. `scripts/sondeo_contrato_hevy.py`
    manda ese mismo cuerpo a un `routine_id` inexistente y Hevy contesta
    `400 {"error":"Expected string, received array"}`, mientras que el mismo
    cuerpo con `notes` bien tipado llega hasta el 404 de «esa rutina no
    existe». Valida el cuerpo antes de buscar la rutina, así que el 400 es del
    campo y de nada más.

    Lo peor no es el fallo, es por qué no lo cazó nadie: el doble de pruebas de
    `tests/test_hevy.py` declara `notes: str | None = None`. La sesión falsa
    tenía el tipo bueno y la de verdad el malo, así que los tests probaban una
    forma del cuerpo que el motor no construye nunca. Un doble que no se parece
    al original no prueba la integración: prueba el doble.

    Se unen con salto de línea y la lista vacía se va a `None` y no a `""`: una
    cadena vacía en Hevy es una nota vacía puesta a propósito, y no hay ninguna
    nota. Y se acepta que ya venga una cadena porque los dobles de prueba y
    cualquier otro llamante la pasan así; lo que no se acepta es un tipo que no
    sea ninguno de los dos, que se levanta más abajo en el contrato.
    """
    notas = getattr(session, "notes", None)
    if notas is None or isinstance(notas, str):
        return notas or None
    if isinstance(notas, (list, tuple)):
        return "\n".join(str(n) for n in notas if n) or None
    raise HevyError(
        f"las notas de la sesión son {type(notas).__name__} y Hevy espera texto "
        f"({notas!r}). No se convierten a ciegas: un `str()` de cualquier cosa "
        f"escribiría en la rutina la repr de un objeto."
    )


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


def first_backup_of_day(
    root: Path | str, routine_id: str, dia: date
) -> Backup | None:
    """La copia MÁS ANTIGUA tomada el día `dia`. `None` si ese día no hay ninguna.

    POR QUÉ LA MÁS ANTIGUA Y NO `latest_backup`
    -------------------------------------------
    Esto existe para deshacer TODO lo que se ha escrito hoy, no lo último. El
    caso es el del check-in tardío: a las 09:00 el trabajo de respaldo decide
    sin formulario y escribe `Día 1`; a las 10:30 llega el check-in, sale rojo,
    y la sesión de hoy ya no toca Hevy. Lo que tiene que quedar en la app es lo
    que había ANTES de las 09:00, porque la decisión de las 09:00 está anulada.

    `latest_backup` daría la copia previa a la ÚLTIMA escritura del día, que si
    hubo dos es el estado intermedio: `Día 1` puesto por la primera. Revertir a
    eso y llamarlo reversión sería exactamente la clase de mentira que este
    proyecto persigue —queda `Día 1`, y el mensaje dice que se ha devuelto—.

    Se filtra POR EL NOMBRE DEL FICHERO, que `save_backup` escribe como
    `AAAAMMDD-HHMMSS.json` a partir del mismo instante que guarda dentro en
    `taken_at`. Ordenar alfabéticamente es ordenar por hora.

    Si la copia más antigua del día no se puede leer se devuelve `None` y NO se
    prueba con la siguiente. La siguiente describe el estado de después de la
    primera escritura: restaurarla dejaría la rutina de hoy puesta mientras se
    anuncia que se ha quitado. Mejor no poder revertir y decirlo.
    """
    carpeta = backup_dir(root, routine_id)
    if not carpeta.is_dir():
        return None
    ficheros = sorted(carpeta.glob(f"{dia:%Y%m%d}-*.json"))
    if not ficheros:
        return None
    return leer_backup(ficheros[0], routine_id)


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
    # El código que contestó Hevy, cuando contestó. `None` significa que no hubo
    # respuesta -no salió la petición, o se cayó la red-, que es un caso
    # distinto de un 400 y lleva a una decisión distinta sobre la marca. La
    # columna `hevy_writes.http_status` existía desde el principio y NADIE la
    # rellenaba: `_anotar_hevy` no la ponía y `WriteResult` no la traía, así que
    # se guardaba `NULL` siempre y la auditoría no podía distinguir «Hevy dijo
    # que no» de «no se pudo preguntar».
    http_status: int | None = None


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

        # 3. El cuerpo, ANTES de la marca. `cuerpo_para_put` es el paso que
        # faltaba: el payload que construye el motor lleva `index` y `title`,
        # que sirven para el diff y para el mensaje pero que Hevy rechaza.
        #
        # EL ORDEN IMPORTA Y ANTES ESTABA AL REVÉS. La marca se escribía primero,
        # así que un cuerpo mal construido -que no llega a salir del proceso, que
        # no toca la red y que deja la rutina intacta por definición- dejaba
        # puesta una marca que significa «hay una rutina en estado desconocido».
        # Un aviso que grita en un caso en el que se sabe perfectamente lo que
        # hay es un aviso que se acaba ignorando, y eso es exactamente lo que no
        # puede pasarle a este.
        try:
            cuerpo = cuerpo_para_put(payload)
        except HevyError as exc:
            # Con la copia ya hecha y sin haber tocado nada: el mejor sitio
            # posible para descubrir que el cuerpo no vale.
            return WriteResult(
                written=False,
                routine_id=routine_id,
                backup=copia,
                diff=diff,
                error=f"{exc} Copia en {copia.path}",
            )

        # 4. Marca de escritura en curso. Sobrevive a que el proceso muera.
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
            # QUE HEVY DIGA QUE NO ES UNA RESPUESTA, NO UN SILENCIO
            # -----------------------------------------------------
            # La marca existe para un caso concreto: no saber en qué estado
            # quedó la rutina. Un 4xx no es ese caso. Hevy ha mirado el cuerpo,
            # lo ha rechazado y no ha aplicado nada; el estado remoto es el de
            # la copia que se acaba de hacer, y eso se sabe. Dejar la marca
            # puesta convertía «te he dicho que no» en «a saber», y así es como
            # el 2026-09-14 amaneció un aviso de escritura a medias que en
            # realidad describía una rutina intacta desde el 8 de septiembre.
            #
            # Un 5xx sí se queda marcado: ahí Hevy ha fallado por dentro y no
            # dice en qué momento, así que la rutina puede haber cambiado.
            # Igual que un 429 o cualquier otra cosa rara, que se trata como
            # desconocida por prudencia y no por lo que diga el número.
            servidor = r.status_code >= 500
            if not servidor:
                marca.unlink(missing_ok=True)
            return WriteResult(
                written=False,
                routine_id=routine_id,
                backup=copia,
                diff=diff,
                http_status=r.status_code,
                error=(
                    f"PUT devolvió {r.status_code}: {r.text[:200]}. "
                    + (
                        f"Hevy no ha aplicado nada: la rutina sigue como en "
                        f"{copia.path}"
                        if not servidor
                        else f"Es un fallo del servidor, así que NO se sabe si "
                        f"llegó a aplicarse: la marca sigue puesta y la copia "
                        f"está en {copia.path}"
                    )
                ),
            )

        # 5. Confirmado: se retira la marca.
        marca.unlink(missing_ok=True)
        return WriteResult(
            written=True,
            routine_id=routine_id,
            backup=copia,
            diff=diff,
            http_status=r.status_code,
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

    def revert_to_day_start(self, routine_id: str, dia: date) -> WriteResult:
        """Deja la rutina como estaba antes de la PRIMERA escritura de `dia`.

        Para el check-in tardío: si por la mañana se escribió con una decisión
        que luego quedó anulada, lo correcto no es escribir otra cosa encima
        -la decisión nueva puede no tocar Hevy en absoluto- sino dejar la app
        como si aquella escritura no hubiera ocurrido.

        NO CAE HACIA `latest_backup` si no hay copia de hoy, y esa omisión es
        deliberada. La copia más reciente de otro día describe un estado
        anterior a la escritura de ESE día, o sea la rutina de la semana pasada.
        Ponerla sería inventarse una reversión: se cambiaría la rutina por una
        tercera cosa que no es ni la de hoy ni la de antes de hoy. Sin copia del
        día se levanta `HevyError` y quien llame avisa.
        """
        copia = first_backup_of_day(self.data_root, routine_id, dia)
        if copia is None:
            raise HevyError(
                f"no hay ninguna copia de la rutina {routine_id} tomada el "
                f"{dia:%Y-%m-%d}: no se puede deshacer lo escrito hoy"
            )
        return self.restore(routine_id, copia)


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
