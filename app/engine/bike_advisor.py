"""Recomendación de bici, todos los días.

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

  1. `baseline_from_gaps`    punto de partida, contra el propio histórico
  2. techo del semáforo      `actions.<luz>.bike_max`   <- EL ÚNICO RECORTE

FUERA EL CALENDARIO: NI DÍAS ASIGNADOS NI PUNTO DE PARTIDA POR DÍA DE LA SEMANA
-------------------------------------------------------------------------------
Aquí había dos claves de calendario, `recommend_on: [saturday, sunday]` y
`baseline_by_weekday: {saturday: intensa, sunday: media}`. La primera hacía que
el sistema no dijese absolutamente nada de lunes a viernes, y la segunda daba
por hecho lo que tocaba según el día del mes en que cayera. Medido sobre las 80
salidas reales del último año de Garmin: el sistema calló en 30 de ellas (38%),
seis de las cuales fueron intensas. Los miércoles: siete salidas, cero consejos.

Ahora el punto de partida sale de comparar los días transcurridos desde la
última salida intensa contra los percentiles de los huecos entre intensas del
propio histórico. Los detalles y los números medidos están en `_baseline_gaps`,
que es donde se calculan, y el razonamiento largo en `config.yaml`.

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
    Signals,
    percentile,
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
    # De dónde salió `baseline`: los días desde la última intensa y las dos
    # bandas contra las que se comparó. Se guarda porque el punto de partida ya
    # NO es una constante escrita en el YAML que se pueda ir a mirar: es un
    # cálculo contra el histórico que cambia cada día, y sin esto no hay manera
    # de reconstruir después por qué el sistema dijo lo que dijo.
    baseline_why: str | None = None
    # `applies=False` tiene dos sabores muy distintos y confundirlos es lo que
    # hacía el calendario viejo: "hoy es miércoles y aquí no se habla" era un no
    # evento, así que no se decía nada y estaba bien. Ahora el único motivo
    # posible de saltarse la bici -aparte de apagarla en el config- es que NO SE
    # HAYA PODIDO calcular el punto de partida, y eso sí hay que decirlo: es el
    # sistema reconociendo que hoy no tiene base para aconsejar, no el sistema
    # callando porque no toca. Un fallo que no se ve es el fallo peligroso.
    skip_visible: bool = False

    @property
    def se_muestra(self) -> bool:
        """Si este bloque ocupa sitio en el mensaje, con nivel o con excusa."""
        return self.applies or self.skip_visible

    def to_dict(self) -> dict[str, Any]:
        return {
            "applies": self.applies,
            "skip_reason": self.skip_reason,
            "skip_visible": self.skip_visible,
            "day": self.day_name,
            "level": self.level,
            "label": self.label,
            "detail": self.detail,
            "duration_min": self.duration_min,
            "duration_max": self.duration_max,
            "baseline": self.baseline,
            "baseline_why": self.baseline_why,
            "downgrades": [
                {"from": a, "to": b, "why": why} for a, b, why in self.downgrades
            ],
            "notas": list(self.notas),
        }

    def text(self) -> str:
        """Línea para el mensaje de Telegram."""
        if not self.applies:
            if self.skip_visible:
                return (
                    f"Bici: hoy no hay punto de partida ({self.skip_reason}). "
                    f"No se inventa uno; si sales, sal por sensaciones."
                )
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

    El día que se notaría es un ROJO. Cuando el punto de partida sale 'intensa'
    -porque los días desde la última caen en la banda media-, el techo rojo
    tendría que bajarlo a 'descanso', y sin techo se sale a hacer series con el
    semáforo en rojo. La recomendación además no lo mencionaría: sin recorte no
    hay `downgrades`, así que el mensaje enseñaría "Bici: intensa" sin una sola
    pega, que es peor que no decir nada.

    Dicho con precisión, hoy esto NO puede pasar por el camino del YAML:
    `config_loader` ya comprueba que los tres `actions.<luz>.bike_max` y los
    tres niveles de `baseline_from_gaps` estén en `intensity_order`, y lo hace
    al arrancar, que es donde mejor duele. (Aquí ponía también
    `after_intense_downgrade_to`, que era el nivel al que se bajaba tras una
    intensa, y `baseline_by_weekday`, el punto de partida por día de la semana;
    ninguna de las dos existe ya. Se deja dicho para que nadie las busque.)

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

    # NO HAY PUERTA DE DÍAS. Aquí estaba `recommend_on`, y de lunes a viernes
    # devolvía `applies=False` sin más. Se ha ido con el calendario: si se sale
    # un miércoles, se quiere consejo el miércoles.

    # --- 1. punto de partida, contra el propio histórico ---------------------
    # Las notas se calculan SIEMPRE, incluso si no hay punto de partida. Son
    # hechos independientes del baseline -lo que se hizo ayer, lo que llevas
    # esta semana- y perderlos el día que el histórico no llega para calcular
    # las bandas sería castigar al mensaje por un problema que no es suyo.
    notas = _notas_de_contexto(rec, signals, cycling)

    baseline, why, motivo_sin_base = _baseline_gaps(rec, signals, order)
    if baseline is None:
        out = build(DESCANSO, DESCANSO, [], notas)
        out.applies = False
        out.skip_reason = motivo_sin_base
        out.skip_visible = True
        return out

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

    # --- 3. contexto: ya calculado arriba, y no recorta ---------------------
    out = build(level, baseline, downs, notas)
    out.baseline_why = why
    return out


def _notas_de_contexto(
    rec: dict[str, Any], signals: Signals, cycling: dict[str, Any]
) -> list[str]:
    """Hechos que se dicen y no recortan.

    Ninguno toca el nivel. Se emiten siempre que sean ciertos, con el nivel que
    sea y el semáforo que sea, porque un dato que solo se enseña cuando además
    te frena se lee como una justificación del frenazo y no como información.
    """
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

    # AQUÍ ESTABA EL RECUENTO SEMANAL DE INTENSAS, Y SE HA IDO AL MENSAJE.
    #
    # Estaba en las notas de la bici desde que dejó de ser un presupuesto, y con
    # el calendario tenía su lógica: la bici solo hablaba sábado y domingo, así
    # que el recuento salía aquí el fin de semana y en una línea suelta el resto
    # de la semana. `message.py` elegía una de las dos para no repetirlo.
    #
    # Quitado el calendario, la bici habla todos los días y esa elección dejaba
    # la línea suelta muerta: el recuento pasaba a salir SIEMPRE por aquí. Y eso
    # destapó un acoplamiento que no se veía: la nota se fabrica cuando se
    # construye la recomendación, así que el recuento solo aparecía en el
    # mensaje si ya estaba en `signals` en ese momento. Calcularlo después
    # -cualquier ruta que rellene `intense_count` más tarde- lo hacía desaparecer
    # del mensaje entero, sin error y sin hueco. Un dato que se enseña todos los
    # días no puede depender de en qué orden se han construido dos objetos.
    #
    # Ahora el recuento lo pinta `message.py` en su propia línea, incondicional,
    # leyendo `signals.intense_count` en el momento de escribir. La distinción
    # que importaba -que cuenta y no recorta- no se pierde: si acaso se ve
    # mejor, porque ya ni siquiera vive dentro de la recomendación.

    return notas


def _baseline_gaps(
    rec: dict[str, Any], signals: Signals, order: list[str]
) -> tuple[str | None, str | None, str | None]:
    """Punto de partida a partir de los huecos entre salidas INTENSAS propias.

    Devuelve `(nivel, por_qué, motivo_si_no_hay)`. Sigue el patrón de
    `resolve_adaptive_threshold`: cuando no se puede calcular algo con sentido,
    se dice por qué y NO se devuelve un valor por defecto. El valor por defecto
    aquí sería una constante inventada, que es exactamente lo que se acaba de
    quitar al borrar `baseline_by_weekday`.

    LAS TRES BANDAS, Y POR QUÉ LA TERCERA NO ES MONÓTONA
    -----------------------------------------------------
        días < p40          -> suave    (se acaba de hacer una)
        p40 <= días < p60   -> intensa  (se está en ritmo y toca)
        días >= p60         -> media    (vuelta de un parón: volumen, no carga)

    La primera versión de esto sí era monótona -cuantos más días, más duro- y se
    cayó con datos reales: los días desde la última intensa crecen sin tope, así
    que durante un bloque suave largo el sistema gritaba INTENSA seis semanas
    seguidas. Habría recomendado intensa en 52 de 88 salidas (59%) contra una
    tasa real del 26%. Volver de un parón de dos meses pidiendo series es
    justamente el consejo que no se le puede dar a una espalda con una hernia
    L4-L5: ahí toca volumen antes que carga, y por eso la banda alta baja a
    media.

    POR QUÉ p40/p60 Y NO p25/p75: LA MASA NO ES LA ANCHURA
    -------------------------------------------------------
    Aquí ponía p25/p75, con el razonamiento de que la banda de en medio es "el
    50% central de mis huecos, o sea la mitad de las veces". Es falso, y el
    error es sutil porque confunde dos cosas que suenan igual:

      - la MASA de la distribución de huecos: cuántos huecos caen dentro
      - el TIEMPO que el contador de días pasa dentro de la banda

    `dias` sube de uno en uno y se queda en cada banda tantos días como ANCHA
    sea la banda, no tantos como probable sea. Con los huecos reales del usuario
    -[26, 1, 26, 2, 5, 2, 10, 20, 55, 6, 8] en 180 días- p25 sale 3.5 y p75 sale
    23: la banda de en medio mide 19 días de ancho y la de abajo 3, así que la
    banda que dice «intensa» se come el eje del tiempo. Medido: decía intensa el
    61% de las veces contra una tasa real del 21%, que es PEOR que el 59%
    contra 26% por el que se tiró la versión monótona.

    Y no lo cazó nadie durante un tiempo, porque los dos bordes de seguridad
    seguían a cero y la coincidencia no se movía. `scripts/falsear_bici.py`
    tiene desde entonces una guarda de tasa que habría matado a los dos.

    p40/p60 es simétrico alrededor de la mediana y se puede enunciar sin mirar
    el resultado: «intensa solo cuando los días desde la última caen en el
    quinto central de tus propios huecos». Se elige por eso y no por ajustar
    mejor; el ajuste son 36 salidas, y a esa escala la diferencia entre el 17%
    y el 22% son dos salidas.

    LO QUE ESTO NO ES, DICHO AQUÍ Y NO SOLO EN EL COMMIT
    -----------------------------------------------------
    No es un predictor mejor que decir siempre lo mismo. Medido sobre las 88
    salidas reales del histórico, acierta el 30% de las veces; la constante
    'suave' acierta el 38%. Está escrito aquí a propósito, porque el día que
    alguien quiera defender este bloque con "es que se ajusta a tus datos" tiene
    que tropezarse con el número. Lo que compra es otra cosa, y son los dos
    extremos: cero «intensa» al día siguiente de una intensa (el calendario
    producía 1) y cero «intensa» volviendo de un parón de 30+ días (el
    calendario producía 5). `scripts/falsear_bici.py` vuelve a medirlo.
    """
    spec = rec.get("baseline_from_gaps")
    if not isinstance(spec, dict):
        raise BikeConfigError(
            "falta `cycling.recommendation.baseline_from_gaps`. Sin él no hay "
            "punto de partida, y no hay ninguno por defecto a propósito: el "
            "defecto sería una constante inventada, que es lo que se acaba de "
            "quitar al borrar `baseline_by_weekday`"
        )

    window = int(spec["window_days"])
    min_gaps = int(spec["min_gaps"])
    p_low = float(spec["percentile_low"])
    p_high = float(spec["percentile_high"])
    niveles = (
        str(spec["level_below_low"]),
        str(spec["level_between"]),
        str(spec["level_above_high"]),
    )
    for nivel in niveles:
        if nivel not in order:
            raise BikeConfigError(
                f"nivel de bici desconocido en baseline_from_gaps: '{nivel}' no "
                f"está en intensity_order ({order})"
            )

    hoy = signals.day
    rides: list[ClassifiedRide] = signals.rides or []
    # ESTRICTAMENTE ANTERIORES A HOY. La salida de hoy todavía no ha ocurrido
    # cuando se emite la recomendación por la mañana; contarla haría que el
    # consejo dependiera de lo que aún no se ha hecho.
    intensas = sorted({r.date for r in rides if r.level == "intensa" and r.date < hoy})
    if not intensas:
        return None, None, (
            f"no hay ninguna salida intensa registrada antes de hoy en las "
            f"{len(rides)} salidas leídas de Garmin"
        )

    desde = hoy - timedelta(days=window)
    en_ventana = [d for d in intensas if desde <= d]
    huecos = [(b - a).days for a, b in zip(en_ventana, en_ventana[1:])]
    if len(huecos) < min_gaps:
        cuantos = (
            "ningún hueco" if not huecos
            else "solo 1 hueco" if len(huecos) == 1
            else f"solo {len(huecos)} huecos"
        )
        return None, None, (
            f"{cuantos} entre intensas en los últimos {window} "
            f"días y hacen falta {min_gaps}: con menos, los percentiles serían "
            f"dos puntos sueltos y no una distribución"
        )

    lo = percentile(huecos, p_low)
    hi = percentile(huecos, p_high)
    if lo is None or hi is None:
        # Inalcanzable con `huecos` no vacío, pero `percentile` puede devolver
        # None y tragárselo aquí sería fabricar un baseline con un umbral que no
        # existe. Si esta rama salta alguna vez, quiero saberlo.
        raise BikeConfigError(
            f"los percentiles de los huecos salen None sobre {huecos!r}"
        )

    # GUARDA CONTRA LA BANDA QUE DESAPARECE. Con huecos todos iguales -por
    # ejemplo [7, 7, 7, 7]- los dos percentiles valen lo mismo sean los que
    # sean, y entonces la condición
    # `lo <= días < hi` no se cumple NUNCA: la banda intermedia, que es la única
    # que dice "intensa", se evapora y el sistema no vuelve a recomendar una
    # intensa jamás sin que nada lo indique. Es el mismo modo de fallo que el
    # umbral degenerado de `resolve_adaptive_threshold`, y se trata igual: sin
    # bandas utilizables no se da punto de partida.
    if hi <= lo:
        return None, None, (
            f"los percentiles de tus huecos entre intensas salen iguales "
            f"(p{p_low:g}={lo:.1f}, p{p_high:g}={hi:.1f}): la banda que dice "
            f"'intensa' no existiría y el sistema no volvería a proponer una"
        )

    dias = (hoy - intensas[-1]).days
    if dias < lo:
        nivel, banda = niveles[0], f"por debajo de p{p_low:g} ({lo:.1f})"
    elif dias < hi:
        nivel, banda = niveles[1], f"entre p{p_low:g} ({lo:.1f}) y p{p_high:g} ({hi:.1f})"
    else:
        nivel, banda = niveles[2], f"por encima de p{p_high:g} ({hi:.1f})"

    why = (
        f"{dias} {'día' if dias == 1 else 'días'} desde la última intensa "
        f"({intensas[-1].isoformat()}), {banda} de tus {len(huecos)} huecos "
        f"de los últimos {window} días"
    )
    return nivel, why, None


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
