"""Adoptar la carga que de verdad se levantó, no la que se mandó levantar.

EL AGUJERO QUE CIERRA
---------------------
Hasta aquí el peso viajaba en una sola dirección. Por la mañana el motor
calculaba un objetivo, lo escribía en Hevy y lo guardaba en `current_sets`; por
la noche la reconciliación leía el entrenamiento, comprobaba REPS y segundos, y
tiraba el resto. El `weight_kg` de cada serie ejecutada no lo miraba nadie.

Eso rompe por los dos lados, y el segundo es el caro:

  - **Hacia arriba.** Se corrige el peso a mano en Hevy -porque la máquina no
    tiene ese disco, porque 60 salió fácil- y el sistema no se entera. Al día
    siguiente vuelve a planificar desde SU número y anuncia "60→62,5 kg" a
    alguien que ya está en 65. El mensaje deja de describir la realidad, y un
    mensaje que no la describe se deja de leer.

  - **Hacia abajo.** Con el peso fuera de la comparación, una sesión hecha a 50
    cuando el plan pedía 60 contaba como LIMPIA -las reps sí se cumplieron- y
    esa sesión limpia pagaba la siguiente subida. El plan se iba a 62,5 mientras
    la realidad seguía en 50, y cada semana se separaban un poco más. Sin un
    solo error por ninguna parte, y en una espalda con hernia L4-L5.

LA ASIMETRÍA, Y POR QUÉ NO ES ARBITRARIA
----------------------------------------
Subir y bajar no se miden contra lo mismo:

  - **Subir se mide contra el OBJETIVO VIGENTE** (`current_sets`), porque es el
    objetivo lo que se estaría subiendo. En una semana de descarga el plan del
    día pide el 60%: hacer ese 60% no es superar nada, y comparar contra el plan
    del día haría que cumplir una descarga subiera la carga real.

  - **Bajar se mide contra lo que se PIDIÓ HOY** (el plan ya recortado que se
    escribió en Hevy), porque solo se puede quedar corto de lo que a uno le han
    pedido. Comparar contra el objetivo vigente convertiría cada semana de
    descarga en tres sesiones "por debajo", y a las tres el sistema bajaría el
    objetivo de verdad: exactamente la deriva que `BuiltSession.target_sets`
    evita capturando el objetivo ANTES de los recortes del día.

Y el ritmo tampoco es el mismo. Arriba se adopta a la PRIMERA, porque la prueba
es directa: se levantó. Abajo hace falta `down_after_sessions` seguidas, porque
una sesión más floja casi siempre es el gimnasio lleno, la máquina ocupada o una
serie mal apuntada, y bajar el objetivo por eso es la forma silenciosa de que un
programa se desinfle. Cuando por fin se baja, se baja a la MEJOR de esas
sesiones, no a la última ni a la peor: un día malo no fija el suelo.

NO SE ADOPTA LO QUE SE CORTÓ — PERO UNA SERIE MÁS LIGERA NO ES UN CORTE
------------------------------------------------------------------------
Un peso más alto no se adopta si las REPS o los SEGUNDOS de alguna serie se
quedaron cortos. 70 kg a 4 reps cuando se pedían 10 no es un objetivo nuevo, es
una serie que se cortó, y adoptarlo subiría el objetivo apoyándose en un fallo.

Lo que SÍ se adopta desde el 25/09/2026 es un peso levantado en una sesión donde
alguna serie fue más LIGERA de lo pedido. Antes eso lo bloqueaba todo, y el caso
que lo destapó fue la patada atrás del 21/09: 30, 40 y 50 kg con las 24 reps
completas en las tres, y los 50 rechazados porque la primera iba a 30 cuando el
plan pedía 35. El sistema estaba tirando la prueba MÁS fuerte -24 reps a 50- por
culpa de la más floja. Una rampa no es un fallo.

La distinción, en una frase: se mira si la serie se TERMINÓ, no con cuánto peso
se empezó. Los dos veredictos los calcula `runner._cumplimiento_contra` en el
mismo bucle y llegan aquí por separado; el estricto -el que sí exige el peso-
sigue mandando en la rama de BAJAR y en la racha de sesiones limpias que abre la
progresión, porque ahí el riesgo es el contrario: una sesión hecha a 50 cuando
el plan pedía 60 no puede pagar la siguiente subida.

EL TOPE DE SALTO
----------------
Un 600 en vez de un 60 al teclear en Hevy se convertiría, sin este tope, en la
rutina de mañana. Subiendo, el margen es `min(max_jump_kg, max_jump_pct ×
objetivo)` con un suelo en el `increment_kg` del ejercicio: en porcentaje porque
10 kg en un curl de 12 es otra cosa que 10 kg en una prensa de 150, en MENOR
porque si no el tope se afloja justo donde la carga absoluta es mayor, y con el
suelo para que un ejercicio ligero no quede por debajo de su propia subida
normal. Bajando es el mayor de los dos, y el motivo está en `_margen`.

El 25/09/2026 los dos valores se ensancharon mucho (5 kg y 20% -> 30 kg y 60%).
En seis meses el tope había actuado cuatro veces y las cuatro eran
levantamientos reales, ninguna una errata: frenaba la realidad, no el ruido. El
motivo largo y las cuatro cifras están en `config.yaml`, junto a las claves.

Pasarse del margen NO adopta y NO calla: se cuenta en el mensaje de la mañana
siguiente. Es la diferencia entre un tope y una censura.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.engine.progression import _fmt_kg
from app.engine.sets import warmup_flags

ARRIBA = "up"
ABAJO = "down"


class AdoptionError(ValueError):
    """Se ha pedido mover una carga con la explicación mal formada.

    Revienta en vez de adoptar callando. La carga se movería igual de bien; lo
    que quedaría mal es el motivo que se lee en el móvil, y un motivo que se
    contradice solo junto a un cambio de peso real vale menos que ningún motivo.
    """


@dataclass
class Adopcion:
    """Un cambio de objetivo propuesto por lo que se hizo. Aplicado o no."""

    routine_key: str
    exercise_key: str
    name: str
    direccion: str  # up | down
    # Lo que pedía el plan del día (ya recortado), lo que se hizo, y el objetivo
    # vigente antes y después. Los cuatro se guardan porque la pregunta de dentro
    # de tres meses -"¿por qué el hip thrust está en 62,5 y no en 70?"- no se
    # puede contestar con menos.
    prescrito_kg: float | None
    # `None` cuando no hay peso que leer: el ejercicio no se registró, o se
    # registró sin kilos. Es distinto de `0.0`, que es "se hizo sin carga".
    hecho_kg: float | None
    objetivo_antes_kg: float
    objetivo_despues_kg: float | None
    aplicada: bool
    motivo: str

    def text(self) -> str:
        if self.aplicada:
            # Sin `or 0`, y a propósito. Una adopción aplicada SIEMPRE lleva
            # objetivo nuevo: la única rama que construye una con `aplicada=True`
            # le pasa `tope_efectivo(...)`, que devuelve un float. Las que llevan
            # `None` son todas `aplicada=False` y salen por el `return` de abajo,
            # que ni lo mira.
            #
            # Ese `or 0` era por tanto inalcanzable con datos legítimos, y lo
            # único que podía hacer es convertir una rotura del invariante en la
            # frase "hip thrust: de 62,5 a 0 kg", que se lee como que el objetivo se
            # ha ido al suelo. En el mensaje de la mañana, sobre el ejercicio que
            # toca hacer hoy. Mejor que reviente aquí -`_fmt_kg(None)` peta- y se
            # vea el fallo, que no que salga un cero con cara de decisión.
            #
            # Un 0,0 de verdad sí puede llegar, y entonces "0 kg" es la lectura
            # correcta: es un ejercicio sin peso registrado, no un hueco.
            #
            # -----------------------------------------------------------------
            #
            # «de X a Y» y no «X→Y», y la flecha no era un capricho de estilo:
            # el 25/09/2026 el usuario leyó «Tirón a la cara: 12,5→15 kg — se
            # levantó eso de verdad, y el plan pedía 12,5» y entendió que el
            # sistema le había puesto 12,5 cuando le había puesto 15. Es fácil de
            # ver una vez leído: la flecha es fina, y el motivo TERMINA en el
            # número viejo, así que 12,5 es a la vez el primero y el último de la
            # frase. Con «de 12,5 a 15 kg» no hay forma de leerlo al revés.
            #
            # No es cosmético. Esa frase es lo único que cuenta que una carga se
            # ha movido, y una carga que se cree movida al revés se corrige a
            # mano sobre una espalda con hernia.
            return (
                f"{self.name}: de {_fmt_kg(self.objetivo_antes_kg)} a "
                f"{_fmt_kg(self.objetivo_despues_kg)} kg ({self.motivo})"
            )
        return f"{self.name}: sigue en {_fmt_kg(self.objetivo_antes_kg)} kg ({self.motivo})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "routine": self.routine_key,
            "key": self.exercise_key,
            "direction": self.direccion,
            "prescribed_kg": self.prescrito_kg,
            "executed_kg": self.hecho_kg,
            "before_kg": self.objetivo_antes_kg,
            "after_kg": self.objetivo_despues_kg,
            "applied": self.aplicada,
            "reason": self.motivo,
        }


def tope_efectivo(series: list[dict[str, Any]] | None) -> float:
    """El peso de la serie efectiva más pesada. 0 si no hay ninguno registrado.

    Es la misma definición que usan `_plan_load` para decidir la subida y
    `repository._guardar_ejercicios` para la columna `current_target_kg`. Se
    escribe una vez aquí para que las tres no puedan discrepar.
    """
    if not series:
        return 0.0
    return max(float(s.get("weight_kg") or 0) for s in series)


def _efectivas_del_plan(
    exercise: dict[str, Any], set_cfg: dict[str, Any]
) -> list[dict[str, Any]]:
    sets = exercise.get("sets") or []
    flags = warmup_flags(sets, set_cfg, exercise.get("key"))
    return [s for s, f in zip(sets, flags, strict=True) if not f]


def _desplazar(series: list[dict[str, Any]], delta: float) -> list[dict[str, Any]]:
    """Suma `delta` a todas las series efectivas, sin dejar ninguna en negativo.

    Delta y no peso absoluto, por el mismo motivo que la progresión de carga:
    una rampa 50/60/65 adoptada a peso absoluto saldría 65/65/65, aplanada de un
    golpe. Con delta pasa a 55/65/70 y el esquema sigue siendo el que era.

    El tope inferior es 0 y no un mínimo inventado: una serie a 0 kg significa
    "sin carga registrada", que es un estado que el resto del motor ya sabe leer
    (`_can_raise_load`, `needs_data`).
    """
    salida = []
    for s in series:
        nueva = dict(s)
        actual = float(s.get("weight_kg") or 0)
        nueva["weight_kg"] = round(max(0.0, actual + delta), 3)
        salida.append(nueva)
    return salida


def _margen(
    objetivo: float,
    cfg: dict[str, Any],
    incremento: float = 0.0,
    direccion: str = ARRIBA,
) -> float:
    """Cuánto puede moverse una carga de una sola vez.

    SUBIR: LOS DOS TOPES SON Y, NO O
    --------------------------------
    Estaba en `max`, y eso hacía que el tope se AFLOJARA justo donde la carga
    absoluta es mayor: con 150 kg de objetivo el margen salía 30 kg, así que un
    dedazo en Hevy de 150 a 180 entraba sin que nadie lo mirara. Con una L4-L5
    eso es el sentido contrario del que tiene que tener un tope. En `min`,
    «nunca más de 5 kg de golpe» significa eso, y el porcentaje solo puede
    apretar más en los ejercicios ligeros, nunca aflojar en los pesados.

    El suelo es el incremento del propio ejercicio. Sin él, la cuenta se comía
    su propia progresión: un ejercicio de 10 kg con incremento 2,5 tendría un
    margen de 2 kg, y una subida normal y correcta saldría rechazada como si
    fuera una errata. Un tope que prohíbe el paso que el programa acaba de
    pedir no protege de nada.

    BAJAR: SE QUEDA EN EL TOPE ANCHO, Y NO ES UNA EXCEPCIÓN CÓMODA
    --------------------------------------------------------------
    El tope existe para que una errata al teclear no SUBA la carga. Bajar no es
    ese riesgo, y encima no llega por una lectura suelta: hacen falta
    `down_after_sessions` sesiones seguidas por debajo y se adopta la MEJOR de
    ellas, así que un 5 tecleado por un 50 lo tapan las otras dos.

    Apretando también aquí, un desfase de 10 kg sobre un objetivo de 60 no se
    podría cerrar nunca: la adopción lo rechazaría sesión tras sesión y el
    objetivo se quedaría para siempre por encima de lo que se levanta. Es decir,
    el tope puesto para proteger la espalda acabaría obligando a intentar un peso
    que no sale. La dirección insegura es una sola, y el tope aprieta en esa.
    """
    ancho = max(
        float(cfg.get("max_jump_kg", 5)),
        float(cfg.get("max_jump_pct", 0.20)) * objetivo,
    )
    if direccion == ABAJO:
        return ancho
    return max(
        float(incremento or 0.0),
        min(
            float(cfg.get("max_jump_kg", 5)),
            float(cfg.get("max_jump_pct", 0.20)) * objetivo,
        ),
    )


def adoptar_cargas(
    state: Any,
    *,
    routine_key: str,
    exercises: list[dict[str, Any]],
    pesos_hechos: dict[str, float | None],
    limpio: dict[str, bool],
    motivos: dict[str, str],
    limpio_arriba: dict[str, bool],
    motivos_arriba: dict[str, str],
    set_cfg: dict[str, Any],
    prog_cfg: dict[str, Any],
) -> list[Adopcion]:
    """Ajusta `state.current_sets` a lo que se levantó. Muta el estado.

    `exercises` es el plan del día TAL Y COMO SE ESCRIBIÓ en Hevy -con la
    descarga, los recortes por regla y el ámbar ya aplicados-, que es contra lo
    que se mide quedarse corto. `pesos_hechos` es el peso de la serie efectiva
    más pesada de cada ejercicio, o None si el ejercicio no aparece o no lleva
    peso apuntado. `limpio` es el cumplimiento por ejercicio que ya calcula
    `workout_compliance`, y `motivos` la causa de cada incumplimiento tal y como
    la nombra `motivos_incumplimiento`.

    `limpio` sigue mandando sobre `motivos`: quien decide es el veredicto, y un
    motivo que no llegue solo deja la frase más pobre, nunca una carga más alta.

    DOS VEREDICTOS, Y CADA RAMA USA EL SUYO (25/09/2026)
    -----------------------------------------------------
    `limpio_arriba` y `motivos_arriba` son el mismo cumplimiento SIN mirar el
    peso de cada serie, y son los que usa la rama de SUBIR. El motivo está
    abajo, en esa rama. Los de bajar y los de "no consta" siguen con el
    estricto: ahí el peso sí tiene que contar.

    Van como parámetros y no se calculan aquí porque el criterio de unión de una
    sesión partida en dos ratos vive en `runner._cumplimiento_contra` y tiene
    que ser el mismo para los dos veredictos. Calcular uno aquí habría creado la
    segunda cuenta en paralelo que esa función existe para no tener.

    Devuelve TODAS las adopciones consideradas, aplicadas y rechazadas. Las
    rechazadas también salen: un tope que actúa sin decirlo es un tope que nadie
    puede corregir.
    """
    cfg = prog_cfg.get("adopt_executed_load", {}) or {}
    if not cfg.get("enabled", True):
        return []

    necesarias = int(cfg.get("down_after_sessions", 3))
    salida: list[Adopcion] = []

    for ex in exercises:
        key = str(ex.get("key") or "")
        if not key:
            continue
        clave = (routine_key, key)
        hecho = pesos_hechos.get(key)

        objetivo_series = state.current_sets.get(clave)
        if objetivo_series is None:
            # Sin objetivo guardado no hay nada que mover. No debería pasar -la
            # mañana guarda `target_sets` de todos los ejercicios de la rutina-
            # pero adoptar sobre una lista que no existe crearía una carga
            # vigente a partir de un ejercicio que nunca se planificó.
            continue
        objetivo = tope_efectivo(objetivo_series)
        prescrito_series = _efectivas_del_plan(ex, set_cfg)
        prescrito = tope_efectivo(prescrito_series)

        if hecho is None:
            # Ni se hizo, ni se apuntó el peso. NO cuenta como sesión por debajo:
            # no hay prueba de haber levantado menos, solo ausencia de prueba. Y
            # tampoco rompe la racha de sesiones por debajo, porque un día
            # suelto sin registrar no dice que el problema se haya resuelto.
            #
            # Pero callarlo del todo era el otro extremo. Un ejercicio que cae
            # aquí sesión tras sesión tiene la carga congelada para siempre y no
            # hay nada en el móvil que lo diga: el número no cambia y no cambiar
            # no se ve. Si además NO salió limpio, se cuenta -sin adoptar nada-
            # con el motivo que se sepa, que es lo único que se puede ofrecer
            # cuando no hay ni un kilo que leer.
            if not limpio.get(key, False):
                salida.append(
                    Adopcion(
                        routine_key, key, str(ex.get("name", key)), ARRIBA,
                        prescrito or None, None, objetivo, None, False,
                        motivos.get(key) or "no consta qué pasó con este ejercicio",
                    )
                )
            continue

        # --- hacia arriba: contra el OBJETIVO, y solo con la sesión limpia ----
        if hecho > objetivo:
            _reset_por_debajo(state, clave)
            # AQUÍ MANDA `limpio_arriba`, QUE NO MIRA EL PESO DE CADA SERIE.
            #
            # Con el veredicto estricto, una serie más LIGERA de lo pedido
            # bloqueaba la subida. El 21/09/2026 la patada atrás se hizo
            # 30/40/50 kg con las 24 reps completas en las tres, y los 50 no se
            # adoptaron porque la primera iba a 30 cuando el plan pedía 35: el
            # sistema rechazaba la prueba MÁS fuerte por culpa de la más floja.
            # Una rampa no es un fallo, y el escalón de abajo de una rampa no
            # dice nada malo de lo que se levantó arriba.
            #
            # Lo que sigue bloqueando es que las REPS o los segundos se queden
            # cortos: 70 kg a 4 reps cuando se pedían 10 no es un objetivo
            # nuevo, es una serie que se cortó, y adoptarlo subiría la carga
            # apoyándose en un fallo. En una L4-L5 esa distinción es la que
            # importa, y es justo la que el veredicto estricto no sabía hacer.
            #
            # BAJAR NO CAMBIA. Allí el peso tiene que contar: una sesión hecha a
            # 50 cuando el plan pedía 60 es exactamente lo que hay que detectar,
            # y con el veredicto de aquí saldría limpia.
            if not limpio_arriba.get(key, False):
                # Sin motivo no se inventa uno. Decir «a las reps objetivo»
                # cuando no consta mandaría a revisar un número que a lo mejor
                # estaba bien, y esa frase viaja al móvil junto a una carga que
                # no ha subido: es justo cuando más caro sale equivocarse.
                #
                # El motivo sale de `motivos_arriba` y no de `motivos`: el
                # estricto puede estar contando la serie ligera -que aquí ya no
                # es un problema- mientras lo que de verdad falló son las reps
                # de otra serie. Sería una carga no subida con una explicación
                # que no la explica.
                porque = motivos_arriba.get(key) or "no consta en qué se quedó corto"
                salida.append(
                    Adopcion(
                        routine_key, key, str(ex.get("name", key)), ARRIBA,
                        prescrito or None, hecho, objetivo, None, False,
                        f"se levantaron {_fmt_kg(hecho)} kg pero el ejercicio no "
                        f"quedó completo ({porque}): un peso mayor sobre una serie "
                        f"que se queda corta no es un objetivo nuevo",
                    )
                )
                continue
            salida.append(
                _aplicar(state, clave, ex, objetivo_series, objetivo, hecho,
                         prescrito, ARRIBA, cfg, racha=None)
            )
            continue

        # --- ni por encima ni por debajo de lo pedido -------------------------
        if hecho >= prescrito:
            _reset_por_debajo(state, clave)
            continue

        # --- por debajo de lo que se pidió hoy --------------------------------
        racha = int(state.below_plan_streak.get(clave, 0)) + 1
        mejor = max(float(state.below_plan_best_kg.get(clave) or 0), hecho)
        state.below_plan_streak[clave] = racha
        state.below_plan_best_kg[clave] = mejor

        if racha < necesarias:
            # A propósito NO se cuenta en el mensaje. Una sesión más floja es lo
            # normal, y avisar de cada una entrenaría a saltarse el bloque justo
            # antes del aviso que sí importa. Queda en el estado, que es donde
            # hace falta que quede.
            continue

        if mejor >= objetivo:
            # Se ha quedado corto del plan del día varias veces -típico de una
            # semana de descarga mal seguida- pero nunca por debajo del objetivo
            # real. No hay nada que bajar.
            _reset_por_debajo(state, clave)
            continue

        salida.append(
            _aplicar(state, clave, ex, objetivo_series, objetivo, mejor,
                     prescrito, ABAJO, cfg, racha=racha)
        )

    return salida


def _reset_por_debajo(state: Any, clave: tuple[str, str]) -> None:
    state.below_plan_streak.pop(clave, None)
    state.below_plan_best_kg.pop(clave, None)


def _aplicar(
    state: Any,
    clave: tuple[str, str],
    ex: dict[str, Any],
    objetivo_series: list[dict[str, Any]],
    objetivo: float,
    hecho: float,
    prescrito: float,
    direccion: str,
    cfg: dict[str, Any],
    racha: int | None,
) -> Adopcion:
    """Mueve la carga vigente y redacta lo que se va a leer en el mensaje.

    POR QUÉ `racha` ES OBLIGATORIO Y ADEMÁS ESTÁ ATADO A `direccion`
    ----------------------------------------------------------------
    Tenía `= 0`, y era el caso más disimulado de los cuatro que se barrieron:
    no dejaba de calcular nada. `racha` solo se usa para redactar el motivo de
    la rama ABAJO, así que un olvido no habría cambiado ni una carga ni un
    estado. Habría cambiado la FRASE, y la frase dice por qué el sistema te ha
    bajado un peso.

    Con el defecto puesto, ese olvido produce «0 sesiones seguidas por debajo
    de lo pedido; se adopta la mejor de ellas»: una afirmación que se
    contradice sola -si fueran cero, no habría nada que adoptar- y que aun así
    viaja al móvil junto a una bajada de carga real y verdadera. Es peor que un
    número equivocado: es una explicación que invita a desconfiar de la bajada
    correcta que la acompaña.

    Se pide `None` explícito en ARRIBA en vez de dejar que cada caller invente
    un cero. Así el tipo dice lo que la función necesita saber -«esta dirección
    no tiene racha»- y añadir una tercera dirección obliga a decidir a qué lado
    cae, en vez de heredar un cero por descuido.
    """
    if direccion == ABAJO and not racha:
        raise AdoptionError(
            f"_aplicar({clave}, ABAJO) sin racha: la bajada se aplicaría igual "
            f"pero el motivo diría '0 sesiones seguidas por debajo', que se "
            f"contradice solo. La racha la cuenta quien decide bajar."
        )
    if direccion == ARRIBA and racha is not None:
        raise AdoptionError(
            f"_aplicar({clave}, ARRIBA, racha={racha}): subir no tiene racha de "
            f"sesiones por debajo. Si ha llegado un número aquí, o la dirección "
            f"o el número están mal."
        )

    routine_key, key = clave
    nombre = str(ex.get("name", key))
    delta = hecho - objetivo

    # Un objetivo a 0 no es un salto desde 0: es la PRIMERA carga registrada de
    # ese ejercicio, el caso que hoy deja a la progresión parada avisando "apunta
    # el peso real en Hevy". El tope no aplica ahí porque no hay nada contra lo
    # que medir el salto, y porque negarlo dejaría el ejercicio parado para
    # siempre a base de proteger un número que no existe.
    incremento = float(ex.get("increment_kg") or 0)
    margen = _margen(objetivo, cfg, incremento, direccion)
    if objetivo > 0 and abs(delta) > margen:
        return Adopcion(
            routine_key, key, nombre, direccion, prescrito or None, hecho,
            objetivo, None, False,
            f"salto de {_fmt_kg(abs(delta))} kg sobre {_fmt_kg(objetivo)}: pasa "
            f"del máximo de {_fmt_kg(margen)} kg y no se adopta solo. Si es "
            f"correcto, se fija con «scripts/fijar_carga.py»; si es una errata "
            f"en Hevy, ya está corregida por no hacerle caso",
        )

    state.current_sets[clave] = _desplazar(objetivo_series, delta)
    _reset_por_debajo(state, clave)

    if direccion == ARRIBA:
        motivo = (
            "primera carga registrada en Hevy"
            if objetivo == 0
            else f"se levantó eso de verdad, y el plan pedía {_fmt_kg(prescrito)}"
        )
    else:
        motivo = (
            f"{racha} sesiones seguidas por debajo de lo pedido; se adopta la "
            f"mejor de ellas"
        )

    return Adopcion(
        routine_key, key, nombre, direccion, prescrito or None, hecho,
        objetivo, tope_efectivo(state.current_sets[clave]), True, motivo,
    )
