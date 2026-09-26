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
from typing import Any, NamedTuple

from app.engine.luces import nombre_luz
from app.engine.signals import (
    ClassifiedRide,
    Signals,
    percentile,
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
    # El mismo hecho que `baseline_why`, dicho para leerlo en el móvil: sin
    # percentiles, sin decimales y sin fechas. Va al mensaje; `baseline_why` no.
    # Ver el comentario largo al final de `_baseline_gaps` para por qué son dos
    # frases y no una.
    baseline_en_claro: str | None = None
    # `applies=False` tiene dos sabores muy distintos y confundirlos es lo que
    # hacía el calendario viejo: "hoy es miércoles y aquí no se habla" era un no
    # evento, así que no se decía nada y estaba bien. Ahora el único motivo
    # posible de saltarse la bici -aparte de apagarla en el config- es que NO SE
    # HAYA PODIDO calcular el punto de partida, y eso sí hay que decirlo: es el
    # sistema reconociendo que hoy no tiene base para aconsejar, no el sistema
    # callando porque no toca. Un fallo que no se ve es el fallo peligroso.
    skip_visible: bool = False
    # El techo del semáforo cuando NO hay punto de partida: `(nivel, motivo)`,
    # o None si hoy no recorta nada (verde). Existe desde el 25/09/2026: sin
    # histórico, el mensaje decía «si sales, sal por sensaciones» también en un
    # día ROJO, y lo único que respetaba el techo era un `level` interno que el
    # texto no leía. El techo es del día, no del histórico: se aplica igual.
    techo_sin_base: tuple[str, str] | None = None

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
            "baseline_en_claro": self.baseline_en_claro,
            "downgrades": [
                {"from": a, "to": b, "why": why} for a, b, why in self.downgrades
            ],
            "notas": list(self.notas),
            "techo_sin_base": list(self.techo_sin_base) if self.techo_sin_base else None,
        }

    def text(self, *, declarada: bool = False) -> str:
        """Línea para el mensaje de Telegram.

        `declarada`: el usuario ha dicho en el check-in que hoy sale en bici
        (26/09/2026). Entonces no hay condicional que valga -ya se sabe que
        sale- y la frase va en afirmativo: «Hoy sales en bici: media». El
        condicional de abajo es para los días en que no se sabe.

        EL MODO VERBAL ES LA MITAD DEL CONTENIDO
        -----------------------------------------
        Esto decía «Bici: Intensa (90-150 min). Series en Z4-Z5.», en
        indicativo, como quien lee una agenda. Y el sistema no sabe si hoy se
        sale en bici: eso depende del tiempo que haga, de las ganas y de con
        quién se salga, tres cosas que no ve y no va a ver nunca. Anunciar en
        indicativo algo que no se sabe es aparentar una certeza que no se tiene,
        y encima invita a discutir con el mensaje en vez de con el cuerpo.

        Así que condicional: **«Si sales hoy: intensa»**. Es exactamente el
        mismo arreglo que se le hizo al bloque de fuerza cuando se quitó el
        calendario -«Si vas al gimnasio hoy: Día 2»- y por el mismo motivo. Que
        los dos bloques del mensaje usen el mismo modo verbal no es estética: si
        uno sugiere y el otro ordena, el que ordena parece tener una razón mejor.

        EL DESCANSO SE QUEDA EN INDICATIVO, Y ES A PROPÓSITO
        ----------------------------------------------------
        `descanso` solo puede venir del techo de un semáforo en rojo, o sea de
        HRV, sueño, pulso de reposo y carga. Eso no es el sistema adivinando si
        apetece salir: es lo único de todo este fichero que sí mide el cuerpo, y
        es el sitio donde la certeza está ganada. Ponerle «si sales hoy» delante
        lo ofrecería como una opción entre otras, que es lo contrario de lo que
        significa un rojo. Misma distinción, y por el mismo razonamiento, que la
        rama de `recovery` en `message.py`.

        Y `declarada` no lo toca. El rojo manda sobre la bici declarada -la
        sesión de ese día es la recuperación, `session_builder`-, así que un
        «Hoy sales en bici: descanso» sería una frase que se niega a sí misma.
        """
        if not self.applies:
            if self.skip_visible:
                techo, motivo = self.techo_sin_base or (None, None)
                # Con el descanso no hay afirmativo: «Hoy sales en bici… y hoy
                # toca descanso» se contradice en la misma línea. Ver abajo.
                declarada = declarada and techo != DESCANSO
                quien = "Hoy sales en bici, pero" if declarada else "Bici: hoy"
                sin_base = f"{quien} no hay punto de partida ({self.skip_reason}). "
                # Declarada, «si sales» contradiría la primera mitad de la frase:
                # ya se sabe que sale.
                if techo is None:
                    if declarada:
                        return sin_base + "No se inventa uno: sal por sensaciones."
                    return sin_base + "No se inventa uno; si sales, sal por sensaciones."
                if techo == DESCANSO:
                    # Indicativo, como el descanso de siempre: ver abajo.
                    return sin_base + f"Y hoy toca descanso: {motivo}."
                if declarada:
                    return sin_base + f"Que sea {techo} como mucho: {motivo}."
                return sin_base + f"Si sales, que sea {techo} como mucho: {motivo}."
            return ""
        rango = (
            f"{self.duration_min}-{self.duration_max} min"
            if self.duration_max > self.duration_min
            else f"{self.duration_max} min"
        )
        if self.level == DESCANSO:
            base = f"Bici: {self.label.lower()} ({rango}). {self.detail}"
        elif declarada:
            base = f"Hoy sales en bici: {self.label.lower()} ({rango}). {self.detail}"
        else:
            base = f"Si sales hoy: {self.label.lower()} ({rango}). {self.detail}"
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

    # El techo del semáforo se lee ANTES de saber si hay punto de partida: es
    # del día, no del histórico, y el caso sin histórico también lo respeta.
    ceiling = str((actions.get(light, {}) or {}).get("bike_max", "suave"))

    base = _baseline_gaps(rec, signals, order)
    if base.nivel is None:
        out = build(DESCANSO, DESCANSO, [], notas)
        out.applies = False
        out.skip_reason = base.motivo_sin_base
        out.skip_visible = True
        if ceiling in order and order.index(ceiling) < len(order) - 1:
            out.techo_sin_base = (ceiling, f"el semáforo está en {_light_es(light)}")
        return out

    baseline, why, en_claro = base.nivel, base.why, base.en_claro
    level = baseline
    downs: list[tuple[str, str, str]] = []

    def downgrade(new: str, why: str) -> None:
        nonlocal level
        if new != level and order.index(new) < order.index(level):
            downs.append((level, new, why))
            level = new

    # --- 2. techo del semáforo ---------------------------------------------
    capped = _cap(level, ceiling, order)
    if capped != level:
        downgrade(capped, f"semáforo en {_light_es(light)}, techo {ceiling}")

    # --- 3. contexto: ya calculado arriba, y no recorta ---------------------
    out = build(level, baseline, downs, notas)
    out.baseline_why = why
    out.baseline_en_claro = en_claro
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

    # AQUÍ ESTABA LA NOTA DEL FIN DE SEMANA, Y ERA EL ÚLTIMO CALENDARIO
    # -------------------------------------------------------------------
    # Decía «llevas 1 salida intensa este fin de semana», contando las intensas
    # del sábado y el domingo con `_intense_rides_this_weekend`. Se ha borrado
    # entera, con su función.
    #
    # Tres motivos, y el tercero es el que la condena:
    #
    # 1. Solo hablaba sábado y domingo. De lunes a viernes el bloque de la bici
    #    no decía una palabra de la intensidad reciente, aunque el jueves
    #    hubiera habido series. Es el mismo defecto que tenía la recomendación
    #    antes de quitarle `recommend_on`, sobreviviendo dentro de sus notas.
    # 2. Era un subconjunto del recuento. La línea 🔥 del mensaje dice ya
    #    «llevas N sesiones intensas en los últimos 7 días», que incluye esas
    #    mismas salidas, más el HIIT, y además todos los días. La nota repetía
    #    parte del mismo hecho con otra unidad justo al lado: dos números
    #    distintos sobre lo mismo en un mensaje de seis líneas es cómo se
    #    consigue que no se crea ninguno.
    # 3. Las dos unidades no coincidían nunca del todo, y la del fin de semana
    #    era siempre la más pequeña. Medido sobre las 58 salidas reales de la
    #    caché, la cuenta rodante nunca queda por debajo de la de calendario y
    #    la supera en 39 de 184 días. Quedarse con la corta era quedarse con la
    #    que más veces dice «vas descansado».
    #
    # Nada de esto se pierde: lo cubre `IntensityCount`, que además no depende
    # de que hoy sea domingo para existir.

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


class Baseline(NamedTuple):
    """Lo que devuelve `_baseline_gaps`, con los campos por nombre.

    POR QUÉ NO ES UNA TUPLA SUELTA, QUE ES LO QUE ERA
    --------------------------------------------------
    Era `tuple[str | None, str | None, str | None]` y le creció un cuarto campo
    -`en_claro`, la frase que llega al móvil-. Los dos llamantes tenían que
    cambiar a la vez, y uno se quedó atrás: `scripts/falsear_bici.py` seguía
    haciendo `nivel, _why, _motivo = _baseline_gaps(...)` y reventaba con un
    `ValueError: too many values to unpack` a mitad de la tabla.

    No lo cazó nadie porque los 1849 tests estaban verdes: ningún test ejecuta
    ese guión, y no puede, porque necesita la caché de actividades reales. O sea
    que la batería que existe para falsar el punto de partida llevaba rota desde
    el commit anterior y el único síntoma era un guión que nadie corrió ese día.

    Con nombres, añadir un campo no rompe a quien lee los que ya había. No
    arregla que el guión esté fuera de los tests -eso no tiene arreglo barato-,
    pero quita de en medio el modo de fallo que lo tumbó.
    """

    nivel: str | None
    why: str | None
    en_claro: str | None
    motivo_sin_base: str | None


def _baseline_gaps(
    rec: dict[str, Any], signals: Signals, order: list[str]
) -> Baseline:
    """Punto de partida a partir de los huecos entre salidas INTENSAS propias.

    Devuelve `(nivel, por_qué, en_claro, motivo_si_no_hay)`. Sigue el patrón de
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
        return Baseline(None, None, None, 
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
        return Baseline(None, None, None, 
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
        return Baseline(None, None, None, 
            f"los percentiles de tus huecos entre intensas salen iguales "
            f"(p{p_low:g}={lo:.1f}, p{p_high:g}={hi:.1f}): la banda que dice "
            f"'intensa' no existiría y el sistema no volvería a proponer una"
        )

    dias = (hoy - intensas[-1]).days
    # SIN ADJETIVOS DE MAGNITUD, Y NO ES UN DETALLE DE ESTILO.
    #
    # La primera versión de estas tres frases decía «bastante más de lo que
    # sueles esperar» en la banda alta, y con `hi`=14 eso salía tal cual el día
    # 14: catorce días no son «bastante más» que catorce. El adjetivo afirmaba
    # una distancia que el número no sostenía, que es exactamente el defecto que
    # estas frases vienen a arreglar, cometido dentro del arreglo.
    #
    # Así que se nombra la frontera y se deja que el lector compare. «Llevas 14
    # días, y de 14 en adelante cuenta como volver de un parón» es verdad el día
    # 14 y el día 40 sin cambiar una palabra, y de paso enseña la regla en vez
    # de solo su resultado.
    llevas = f"llevas {dias} {'día' if dias == 1 else 'días'} sin una salida intensa"
    if dias < lo:
        nivel, banda = niveles[0], f"por debajo de p{p_low:g} ({lo:.1f})"
        claro = f"{llevas}, menos de los {lo:g} que sueles dejar pasar"
    elif dias < hi:
        nivel, banda = niveles[1], f"entre p{p_low:g} ({lo:.1f}) y p{p_high:g} ({hi:.1f})"
        claro = f"{llevas}, que es lo que sueles dejar pasar entre una y otra"
    else:
        nivel, banda = niveles[2], f"por encima de p{p_high:g} ({hi:.1f})"
        claro = (
            f"{llevas}. De {hi:g} en adelante cuenta como volver de un parón: "
            f"volumen antes que carga"
        )

    why = (
        f"{dias} {'día' if dias == 1 else 'días'} desde la última intensa "
        f"({intensas[-1].isoformat()}), {banda} de tus {len(huecos)} huecos "
        f"de los últimos {window} días"
    )
    # DOS FRASES Y NO UNA, Y NO ES REDUNDANCIA.
    #
    # `why` es la auditoría: lleva la fecha exacta, los dos percentiles con su
    # valor y sobre cuántos huecos se calcularon. Sirve para reconstruir dentro
    # de un mes por qué el sistema dijo lo que dijo, y va al JSON de la decisión
    # y al panel.
    #
    # `claro` es lo que se lee a las siete de la mañana en el móvil. Dice el
    # MISMO hecho sin una sola cifra de jerga, porque «entre p40 (8.0) y p60
    # (14.0) de tus 5 huecos» no informa a nadie: o se ignora, o peor, se lee
    # como precisión. Un número con un decimal aparenta una exactitud que 5
    # huecos no sostienen.
    #
    # Y la tercera rama es la que de verdad justifica esto. La banda alta es uno
    # de los dos frenos por los que existe este bloque -no pedir series al
    # volver de un parón-, y hasta ahora el mensaje enseñaba «Media» a secas: el
    # freno actuaba y no se veía. Un freno invisible no se puede ni agradecer ni
    # discutir, y el día que se rompa se romperá en silencio.
    return Baseline(nivel, why, claro, None)


# AQUÍ ESTABA `_intense_rides_this_weekend`, Y SE HA BORRADO ENTERA
# ------------------------------------------------------------------
# Contaba las salidas intensas ya ejecutadas del fin de semana en curso, y su
# docstring era casi todo la historia de un fallo suyo: el filtro `<= 6 días`
# metía el domingo de la semana PASADA dentro de «este fin de semana», así que
# un sábado el mensaje podía decir «ya hay 1 salida intensa este fin de semana»
# refiriéndose a una de seis días antes. Se arregló, y el arreglo era correcto.
#
# La función se va igualmente, y conviene que quede escrito por qué: no se
# borra porque estuviera mal, se borra porque la pregunta que contestaba estaba
# mal hecha. «¿Cuántas intensas llevo ESTE FIN DE SEMANA?» solo tiene respuesta
# sábado y domingo, y solo tiene interés si se da por hecho que ahí es donde
# cae el esfuerzo. Las dos cosas son el calendario. La pregunta que sí se
# sostiene los siete días -«¿cuánta intensidad llevo encima?»- la contesta
# `IntensityCount` sobre una ventana rodante.
#
# Con ella se van `previous_weekday` -que no tenía más llamantes- y el bloque
# `cycling.weekend` del YAML, que existía solo para decirle a esta función y a
# `weekend_summary` qué días agrupar.


def _light_es(light: str) -> str:
    """Ya no lleva tabla: la única está en `app/engine/luces.py`.

    Se deja la función en vez de llamar a `nombre_luz` desde el sitio de uso
    porque el nombre dice en qué idioma habla esa frase. Lo que se va es la
    tabla, que era la sexta copia de las mismas tres palabras en el proyecto y
    estaba metida dentro de un `return`, donde no la encontraba ni un `grep` de
    `NOMBRE_LUZ`. Así se encontraron: preguntándole al AST por diccionarios con
    esas tres claves, que es lo que ahora vigila `tests/test_engine_luces.py`.
    """
    return nombre_luz(light)
