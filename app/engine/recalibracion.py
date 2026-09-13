"""Cuándo toca volver a mirar los umbrales, y decirlo en voz alta.

EL PROBLEMA QUE RESUELVE
------------------------
Varios números del `config.yaml` se calibraron sobre muestras cortas -la
clasificación de las salidas de bici salió de 25 salidas de una temporada, el
umbral del fin de semana de un volumen semanal que va a cambiar- y se dejaron
escritos con la promesa, en un comentario, de "revisar a las 4 semanas".

Ese comentario es el personaje recurrente de este repositorio: la pieza que
tenía que avisar de un hueco y que, en vez de avisar, certifica que no lo hay.
Se lee el día que se escribe, cuando todavía no hay nada que revisar, y no se
vuelve a leer nunca. A las cuatro semanas el que tiene que acordarse está
mirando el mensaje de la mañana, no el YAML.

Así que el aviso va donde está la mirada: en el mensaje de la mañana.

QUÉ SE CUENTA
-------------
Días con una decisión vigente guardada, desde `program.recalibrado_el`. No días
de calendario. Lo que hace recalibrable un umbral es la muestra acumulada, y
cuatro semanas de calendario con el sistema apagado dos no son cuatro semanas de
datos: avisar entonces invitaría a recalibrar sobre la mitad de la muestra que
uno cree tener, que es peor que no recalibrar.

CÓMO SE CALLA
-------------
Cambiando `program.recalibrado_el` a mano. Es a propósito que no haya un botón:
la única forma de que el aviso pare es haber mirado. Y vale igual si de la
revisión sale "no cambio nada" -se pone la fecha y la cuenta vuelve a empezar-,
porque decidir no tocar, habiendo mirado, también es recalibrar.

Lo que NO se hizo fue contar decisiones tomadas bajo el `config_hash` de hoy,
que tenía la gracia de reiniciarse solo al recalibrar. El hash cambia con
cualquier edición: apagar Telegram un día pondría el contador a cero sin que
nadie haya revisado un umbral, y el aviso desaparecería sin haberse atendido.
Un contador que se reinicia por el motivo equivocado no solo se equivoca:
además certifica que no queda nada pendiente.

POR QUÉ INSISTE
---------------
Sale todas las mañanas a partir del día que toca, y no una sola vez. Un aviso
que se da una vez y se retira es un aviso que se pierde el día que Telegram
falla, o el día que se lee el mensaje a medias con el móvil en la otra mano.
Como sí hay una forma barata y honesta de callarlo, insistir no es acoso: es la
única presión que ejerce el sistema sobre un mantenimiento que, si no, no se
hace.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass
class Recalibracion:
    """La cuenta de hoy hacia la próxima revisión de umbrales."""

    desde: date
    dias: int
    cada: int

    @property
    def toca(self) -> bool:
        return self.dias >= self.cada

    @property
    def restantes(self) -> int:
        """Días con decisión que faltan. Nunca negativo: cuando toca, es 0."""
        return max(0, self.cada - self.dias)

    def lineas(self) -> list[str]:
        """Las frases para el mensaje. Vacía mientras no toque.

        Solo habla cuando toca. La cuenta atrás ("faltan 9 días") no se enseña a
        propósito: sería una línea fija en todos los mensajes del año, y una
        línea que sale siempre deja de leerse mucho antes de decir algo.
        """
        if not self.toca:
            return []
        return [
            f"Llevas {self.dias} días con decisión desde la última revisión de "
            f"umbrales ({self.desde.isoformat()}), y el aviso estaba puesto en "
            f"{self.cada}.",
            # Aquí se nombraba también `cycling.weekend.total_hours_threshold`,
            # que era el umbral de `resaca_finde`. Los dos se han borrado, y el
            # aviso no puede seguir mandando a revisar un número que ya no
            # existe: quien fuera a mirarlo no lo encontraría y se quedaría sin
            # saber si es que ya estaba bien o es que buscaba mal.
            #
            # Lo que queda por revisar a mano es uno solo, y no por casualidad:
            # el resto de umbrales de este sistema se recalibran ellos contra la
            # propia distribución histórica (`adaptive_thresholds`). La
            # clasificación de las salidas es el último que sigue siendo un
            # número escrito, y por eso es el único que hay que ir a mirar.
            "Toca mirar con /api/export el único umbral que sigue calibrado a "
            "mano sobre una muestra corta: la clasificación de las salidas de "
            "bici (cycling.classification). Los demás se recalibran solos "
            "contra tu histórico (adaptive_thresholds); comprueba de paso que "
            "sus ventanas siguen teniendo días suficientes.",
            "Para que deje de salir, pon la fecha de hoy en "
            "program.recalibrado_el. Si de mirarlo sale que no cambias nada, "
            "cámbiala igual: eso también es haber recalibrado.",
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "desde": self.desde.isoformat(),
            "dias": self.dias,
            "cada": self.cada,
            "toca": self.toca,
            "restantes": self.restantes,
            "lineas": self.lineas(),
        }


def evaluar_recalibracion(cfg: Any, dias_con_decision: int) -> Recalibracion:
    """Monta la cuenta con lo que dice el `config.yaml` y lo que hay en la base.

    Los días se pasan ya contados en vez de leerse aquí por lo mismo que en el
    resto del motor: esta función no abre la base de datos, así que se puede
    probar entera -y replayar sobre un pasado que no se vivió- sin montar una.

    Se leen los dos valores por el accesor con nombre y NO con `.get(..., 28)`.
    Un defecto aquí dentro haría que borrar la clave del YAML dejara el aviso
    funcionando con un número que no está escrito en ninguna parte, y que
    equivocarse escribiéndola lo dejara mudo para siempre. El validador se niega
    a arrancar sin ellas; aquí se da por hecho que están.
    """
    if dias_con_decision < 0:
        raise ValueError(
            f"días con decisión negativos ({dias_con_decision}): quien los ha "
            f"contado ha contado mal, y con un número así el aviso de "
            f"recalibración no volvería a salir nunca"
        )
    return Recalibracion(
        desde=cfg.recalibrado_el,
        dias=dias_con_decision,
        cada=cfg.recalibrar_cada_dias,
    )
