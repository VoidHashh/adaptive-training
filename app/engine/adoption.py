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

NO SE ADOPTA LO QUE NO SE COMPLETÓ
----------------------------------
Un peso más alto solo se adopta si la sesión fue limpia en ese ejercicio. 70 kg
a 4 reps cuando se pedían 10 no es un objetivo nuevo, es una serie que se cortó;
adoptarlo subiría el objetivo apoyándose en un fallo.

EL TOPE DE SALTO
----------------
Un 600 en vez de un 60 al teclear en Hevy se convertiría, sin este tope, en la
rutina de mañana. El margen es `max(max_jump_kg, max_jump_pct × objetivo)`: en
porcentaje porque 10 kg en un curl de 12 es otra cosa que 10 kg en una prensa de
150, y con un mínimo en kg para que un ejercicio ligero no quede por debajo del
propio incremento de la progresión.

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
    hecho_kg: float
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
            # frase "hip thrust: 62,5→0 kg", que se lee como que el objetivo se
            # ha ido al suelo. En el mensaje de la mañana, sobre el ejercicio que
            # toca hacer hoy. Mejor que reviente aquí -`_fmt_kg(None)` peta- y se
            # vea el fallo, que no que salga un cero con cara de decisión.
            #
            # Un 0,0 de verdad sí puede llegar, y entonces "0 kg" es la lectura
            # correcta: es un ejercicio sin peso registrado, no un hueco.
            return (
                f"{self.name}: {_fmt_kg(self.objetivo_antes_kg)}→"
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


def _margen(objetivo: float, cfg: dict[str, Any]) -> float:
    return max(
        float(cfg.get("max_jump_kg", 5)),
        float(cfg.get("max_jump_pct", 0.20)) * objetivo,
    )


def adoptar_cargas(
    state: Any,
    *,
    routine_key: str,
    exercises: list[dict[str, Any]],
    pesos_hechos: dict[str, float | None],
    limpio: dict[str, bool],
    set_cfg: dict[str, Any],
    prog_cfg: dict[str, Any],
) -> list[Adopcion]:
    """Ajusta `state.current_sets` a lo que se levantó. Muta el estado.

    `exercises` es el plan del día TAL Y COMO SE ESCRIBIÓ en Hevy -con la
    descarga, los recortes por regla y el ámbar ya aplicados-, que es contra lo
    que se mide quedarse corto. `pesos_hechos` es el peso de la serie efectiva
    más pesada de cada ejercicio, o None si el ejercicio no aparece o no lleva
    peso apuntado. `limpio` es el cumplimiento por ejercicio que ya calcula
    `workout_compliance`.

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

        if hecho is None:
            # Ni se hizo, ni se apuntó el peso. NO cuenta como sesión por debajo:
            # no hay prueba de haber levantado menos, solo ausencia de prueba. Y
            # tampoco rompe la racha de sesiones por debajo, porque un día
            # suelto sin registrar no dice que el problema se haya resuelto.
            continue

        prescrito_series = _efectivas_del_plan(ex, set_cfg)
        prescrito = tope_efectivo(prescrito_series)

        # --- hacia arriba: contra el OBJETIVO, y solo con la sesión limpia ----
        if hecho > objetivo:
            _reset_por_debajo(state, clave)
            if not limpio.get(key, False):
                salida.append(
                    Adopcion(
                        routine_key, key, str(ex.get("name", key)), ARRIBA,
                        prescrito or None, hecho, objetivo, None, False,
                        f"se levantaron {_fmt_kg(hecho)} kg pero la sesión no se "
                        f"completó a las reps objetivo: un peso mayor con series "
                        f"cortas no es un objetivo nuevo",
                    )
                )
                continue
            salida.append(
                _aplicar(state, clave, ex, objetivo_series, objetivo, hecho,
                         prescrito, ARRIBA, cfg)
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
    racha: int = 0,
) -> Adopcion:
    routine_key, key = clave
    nombre = str(ex.get("name", key))
    delta = hecho - objetivo

    # Un objetivo a 0 no es un salto desde 0: es la PRIMERA carga registrada de
    # ese ejercicio, el caso que hoy deja a la progresión parada avisando "apunta
    # el peso real en Hevy". El tope no aplica ahí porque no hay nada contra lo
    # que medir el salto, y porque negarlo dejaría el ejercicio parado para
    # siempre a base de proteger un número que no existe.
    if objetivo > 0 and abs(delta) > _margen(objetivo, cfg):
        return Adopcion(
            routine_key, key, nombre, direccion, prescrito or None, hecho,
            objetivo, None, False,
            f"salto de {_fmt_kg(abs(delta))} kg sobre {_fmt_kg(objetivo)}: pasa "
            f"del máximo de {_fmt_kg(_margen(objetivo, cfg))} kg y no se adopta "
            f"solo. Si es correcto, se sube a mano en config.yaml; si es una "
            f"errata en Hevy, ya está corregida por no hacerle caso",
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
