"""Recomendación de bici para el fin de semana.

QUIÉN DECIDE, Y POR QUÉ ESTE FICHERO SOLO TIENE UN RECORTE
----------------------------------------------------------
El sistema no decide lo que se puede hacer. Decide el usuario; el sistema
registra lo que se hizo y ajusta el resto en consecuencia. Una semana de viaje
con siete días de bici seguidos no es un error que haya que impedir: es una
semana de carga alta que hay que reconocer y de la que hay que ayudar a
recuperarse después.

De eso se sigue una regla que ordena todo lo que hay aquí abajo: **solo frena
lo que mide el cuerpo**. Queda un único recorte, el techo del semáforo, y el
semáforo sale de HRV, sueño, pulso de reposo, carga de Garmin y el check-in.
Si siete días de bici dejan a alguien hecho polvo, eso se ve en esas señales y
el semáforo da ámbar o rojo por ahí, que es el camino honesto. Frenar porque
una cuenta diga que se ha gastado un cupo no lo es: es la cuenta decidiendo en
lugar del cuerpo.

  1. `baseline_by_weekday`   punto de partida (sábado intensa, domingo media)
  2. techo del semáforo      `actions.<luz>.bike_max`   <- EL ÚNICO RECORTE

LO QUE ANTES RECORTABA Y AHORA SOLO SE CUENTA
----------------------------------------------
Había cuatro recortes más. Los cuatro miraban cuentas, no señales:

  - presupuesto semanal agotado
  - una sola intensa por fin de semana
  - no dos intensas seguidas
  - solo intensa con el semáforo en verde

Los tres primeros pasan a `notas`: se dicen, se guardan y no bajan el nivel. El
cuarto se ha borrado entero, porque además estaba muerto: con
`actions.amber.bike_max: suave` y `actions.red.bike_max: descanso`, el techo del
semáforo ya había bajado el nivel antes de llegar ahí, así que la condición
"nivel intensa y semáforo no verde" no se cumplía nunca. Comprobado por
enumeración de las 648 combinaciones de día, luz, ayer, presupuesto y fin de
semana: no disparó ni una vez. Era una opción muerta con aspecto de freno, y de
las peores: se citaba como uno de los tres que sostenían el fin de semana.

`notas` frente a `downgrades`: los dos salen en el mensaje y los dos se
guardan, pero `downgrades` cambia lo que se recomienda y `notas` no. Se
separan para que la diferencia se vea en el JSON de la decisión y no haya que
deducirla leyendo este fichero dentro de seis meses.

NI EN LAS NOTAS NI EN NINGÚN SITIO HAY TONO DE REGAÑINA
--------------------------------------------------------
Si el sistema recomienda suave y se sale a apretar, eso se registra y se tiene
en cuenta después, y ya está. Las notas dicen lo que pasó -"3 sesiones intensas
esta semana"- y nunca lo que habría que haber hecho. Un sistema que juzga se
deja de leer, y un sistema que se deja de leer no avisa de nada el día que
importa.

Todo lo que mira al pasado usa lo que REALMENTE se hizo (las actividades de
Garmin ya clasificadas), no lo que se recomendó. Si el sábado se recomendó
intensa y se salió a rodar suave, el domingo no arrastra nada.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from app.engine.signals import (
    ClassifiedRide,
    IntensityCount,
    Signals,
    previous_weekday,
)

DESCANSO = "descanso"


@dataclass
class BikeRecommendation:
    day_name: str
    level: str
    label: str
    detail: str
    duration_min: int
    duration_max: int
    baseline: str
    # Cada recorte aplicado: (nivel_antes, nivel_después, motivo). Hoy solo
    # puede haber uno, el del techo del semáforo, pero sigue siendo una lista:
    # la estructura no cambia por que ahora mismo haya un único recorte posible.
    downgrades: list[tuple[str, str, str]] = field(default_factory=list)
    # Hechos de contexto que NO cambian el nivel recomendado. Lo que antes eran
    # recortes por cuenta -presupuesto, intensas del fin de semana, intensa de
    # ayer- vive aquí: se dice, se guarda, y la decisión es de quien pedalea.
    notas: list[str] = field(default_factory=list)
    applies: bool = True
    skip_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "applies": self.applies,
            "skip_reason": self.skip_reason,
            "day": self.day_name,
            "level": self.level,
            "label": self.label,
            "detail": self.detail,
            "duration_min": self.duration_min,
            "duration_max": self.duration_max,
            "baseline": self.baseline,
            "downgrades": [
                {"from": a, "to": b, "why": why} for a, b, why in self.downgrades
            ],
            "notas": list(self.notas),
        }

    def text(self) -> str:
        """Línea para el mensaje de Telegram."""
        if not self.applies:
            return ""
        rango = (
            f"{self.duration_min}-{self.duration_max} min"
            if self.duration_max > self.duration_min
            else f"{self.duration_max} min"
        )
        base = f"Bici: {self.label} ({rango}). {self.detail}"
        if self.downgrades:
            # Solo el motivo del último recorte: es el que manda. Los demás
            # quedan en el JSON de la decisión para quien quiera el detalle.
            base += f" [{self.downgrades[-1][2]}]"
        return base

    def texto_notas(self) -> list[str]:
        """Los hechos de contexto, uno por línea, para el mensaje.

        Van aparte del nivel recomendado a propósito. Metidos en la misma
        frase que `text()` se leerían como la explicación de por qué se
        recomienda eso, y no lo son: no han entrado en la decisión. Esa
        confusión es justamente la que se quería quitar al dejar de recortar
        por cuentas.
        """
        return list(self.notas)


class BikeConfigError(ValueError):
    """Un nivel de bici que no está en `intensity_order`."""


def _cap(level: str, ceiling: str, order: list[str]) -> str:
    """Recorta `level` al techo `ceiling` según el orden de intensidad.

    Un nivel desconocido revienta, y antes devolvía `level` intacto.

    Devolver `level` es no recortar, y no recortar es justo lo contrario de lo
    que hace esta función. El techo sale de `actions.<luz>.bike_max`: si dice
    'moderada' y `intensity_order` no tiene ese nombre -por una errata, o por
    haber renombrado un nivel en la lista y no aquí-, el techo del semáforo
    dejaba de existir en silencio.

    El día que se notaría es un ROJO. El punto de partida del sábado es
    'intensa', el techo rojo tendría que bajarlo a 'descanso', y sin techo se
    sale a hacer la intensa del sábado con el semáforo en rojo. La
    recomendación además no lo mencionaría: sin recorte no hay `downgrades`,
    así que el mensaje enseñaría "Bici: intensa" sin una sola pega, que es peor
    que no decir nada.

    Dicho con precisión, hoy esto NO puede pasar por el camino del YAML:
    `config_loader` ya comprueba que los tres `actions.<luz>.bike_max` y los
    `baseline_by_weekday` estén en `intensity_order`, y lo hace al arrancar, que
    es donde mejor duele. (Aquí ponía también `after_intense_downgrade_to`, que
    era el nivel al que se bajaba tras una intensa; esa clave ya no existe,
    porque ese recorte se ha convertido en una nota que no toca el nivel. Se
    deja dicho para que nadie la busque.)

    Esta guarda es la segunda línea: sirve para el día que alguien llame a `_cap`
    con un techo que no venga del config, y sobre todo para que el modo de
    fallo de esta función sea "para" y no "sigue sin recortar". Una función
    cuyo trabajo es poner un techo no puede tener una rama que consiste en no
    ponerlo.
    """
    for nombre, valor in (("nivel", level), ("techo", ceiling)):
        if valor not in order:
            raise BikeConfigError(
                f"{nombre} de bici desconocido: '{valor}' no está en "
                f"intensity_order ({order}). Sin él no se puede comparar la "
                f"intensidad, y devolver el nivel sin tocar sería quitarle el "
                f"techo al semáforo sin decirlo"
            )
    return level if order.index(level) <= order.index(ceiling) else ceiling


def recommend_bike(
    config: Any,
    signals: Signals,
    light: str,
) -> BikeRecommendation:
    raw = config.raw if hasattr(config, "raw") else config
    cycling = raw.get("cycling", {}) or {}
    rec = cycling.get("recommendation", {}) or {}
    actions = raw.get("actions", {}) or {}

    order: list[str] = list(rec.get("intensity_order") or [])
    types: dict[str, Any] = rec.get("types") or {}
    day_name = signals.weekday()

    def build(
        level: str,
        baseline: str,
        downs: list[tuple[str, str, str]],
        notas: list[str] | None = None,
    ):
        t = types.get(level, {}) or {}
        return BikeRecommendation(
            day_name=day_name,
            level=level,
            label=str(t.get("label", level.capitalize())),
            detail=str(t.get("detail", "")),
            duration_min=int(t.get("duration_min", 0)),
            duration_max=int(t.get("duration_max", 0)),
            baseline=baseline,
            downgrades=downs,
            notas=list(notas or []),
        )

    if not rec.get("enabled", True):
        out = build(DESCANSO, DESCANSO, [])
        out.applies = False
        out.skip_reason = "la recomendación de bici está desactivada en el config"
        return out

    recommend_on = {str(d).lower() for d in (rec.get("recommend_on") or [])}
    if day_name not in recommend_on:
        out = build(DESCANSO, DESCANSO, [])
        out.applies = False
        out.skip_reason = f"hoy es {day_name}; solo se recomienda en {sorted(recommend_on)}"
        return out

    # --- 1. punto de partida ------------------------------------------------
    baseline = str((rec.get("baseline_by_weekday") or {}).get(day_name, "suave"))
    level = baseline
    downs: list[tuple[str, str, str]] = []

    def downgrade(new: str, why: str) -> None:
        nonlocal level
        if new != level and order.index(new) < order.index(level):
            downs.append((level, new, why))
            level = new

    # --- 2. techo del semáforo ---------------------------------------------
    ceiling = str((actions.get(light, {}) or {}).get("bike_max", "suave"))
    capped = _cap(level, ceiling, order)
    if capped != level:
        downgrade(capped, f"semáforo en {_light_es(light)}, techo {ceiling}")

    # --- 3. contexto, que se cuenta y no recorta ----------------------------
    #
    # Todo lo que sigue son HECHOS. Ninguno toca `level`. Se emiten siempre que
    # sean ciertos, con el nivel que sea y el semáforo que sea, porque un dato
    # que solo se enseña cuando además te frena se lee como una justificación
    # del frenazo y no como información.
    notas: list[str] = []

    lookback = int(rec.get("lookback_days", 1))
    if signals.get("yesterday_ride_level") == "intensa":
        cuando = "ayer" if lookback <= 1 else f"en los últimos {lookback} días"
        notas.append(f"{cuando} hiciste una salida intensa")

    hechas = _intense_rides_this_weekend(signals, cycling)
    if hechas:
        # Plural concordado, no "salida(s)". Mismo motivo que en
        # `IntensityCount.linea`: esto lo lee una persona el domingo por la
        # mañana, y los paréntesis de plural delatan un texto de máquina.
        cuantas = "1 salida intensa" if hechas == 1 else f"{hechas} salidas intensas"
        notas.append(f"llevas {cuantas} este fin de semana")

    conteo: IntensityCount | None = signals.intense_count
    if conteo is not None:
        notas.append(conteo.linea())

    return build(level, baseline, downs, notas)


def _intense_rides_this_weekend(signals: Signals, cycling: dict[str, Any]) -> int:
    """Salidas intensas YA EJECUTADAS en el fin de semana en curso.

    Cuenta lo hecho, no lo recomendado: si el sábado se recomendó intensa y se
    acabó rodando suave, esa intensidad no ocurrió y no se cuenta.

    LA VENTANA ESTABA UN DÍA -SEIS, EN REALIDAD- DEMASIADO ABIERTA
    --------------------------------------------------------------
    El filtro era `(signals.day - d) <= 6 días`. Un sábado, el domingo anterior
    cae exactamente a 6 días, así que entraba: el sistema contaba el domingo de
    la semana PASADA como parte de "este fin de semana". Con
    `max_intense_rides_per_weekend: 1`, una salida intensa el domingo bajaba la
    del sábado siguiente, seis días después, y el mensaje lo explicaba diciendo
    "ya hay 1 salida intensa este fin de semana", que era falso de plano.

    Ahora que esto solo informa en vez de recortar, el número deja de bajarle a
    nadie la salida, pero sale escrito en el mensaje: un número que se enseña
    tiene que ser verdad aunque no decida nada. Si acaso más, porque un dato
    que no decide es un dato que nadie va a ir a verificar.

    El fin de semana es una tira contigua de `len(day_names)` días que termina
    en el último de la lista, así que un día configurado pertenece al fin de
    semana en curso si cae dentro de esa ventana contando hacia atrás desde
    hoy. Con sábado y domingo: un sábado solo entra el propio sábado; un
    domingo entran el sábado de ayer y el domingo de hoy.
    """
    day_names = [
        str(d).lower() for d in ((cycling.get("weekend", {}) or {}).get("days") or [])
    ]
    if not day_names:
        return 0

    ventana = timedelta(days=len(day_names) - 1)
    days: list = []
    for name in day_names:
        if name == signals.weekday():
            days.append(signals.day)
        else:
            d = previous_weekday(signals.day, name)
            if (signals.day - d) <= ventana:
                days.append(d)

    rides: list[ClassifiedRide] = signals.rides or []
    # Estrictamente anteriores a hoy: la salida de hoy todavía no ha ocurrido
    # cuando se emite la recomendación por la mañana.
    return sum(
        1 for r in rides if r.date in days and r.date < signals.day and r.level == "intensa"
    )


def _light_es(light: str) -> str:
    return {"green": "verde", "amber": "ámbar", "red": "rojo"}.get(light, light)
