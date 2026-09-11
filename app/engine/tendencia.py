"""Segunda derivada del semáforo: lo que no se ve mirando un solo día.

Por qué existe
--------------
Ninguna regla de `thresholds` mira más de tres días hacia atrás. La más larga es
`hrv_hundida_2d`, y la carga acumulada se cierra en 3. Eso significa que el motor
no tiene ni un solo mecanismo capaz de notar que llevas cinco semanas en ámbar:
cada mañana vuelve a empezar de cero, decide bien, y el conjunto se le escapa.

El replay de los 179 días lo dejó medido: el reparto de colores apenas se movió
entre mayo y agosto, pero la composición de lo que disparaba se dio la vuelta
entera. El color no se movió, la razón sí. Nadie lo habría dicho porque nadie lo
estaba mirando.

Esta capa mira exactamente eso y NADA MÁS. No decide. No toca el semáforo, ni la
progresión, ni la sesión. Produce frases para el mensaje de la mañana y se
persiste con la decisión para poder releerla. Si mañana se borra este módulo, el
sistema entrena igual; lo único que se pierde es saber en qué dirección va.

POR QUÉ LOS UMBRALES SON ABSOLUTOS
----------------------------------
La preferencia general del proyecto es comparar contra la propia distribución
histórica en vez de contra constantes. Aquí NO, y es la excepción correcta.

`hrv_ratio` enseña el fallo: al dividir por la media de 7 días, el denominador
persigue al numerador y el cociente no sale de 0,91–1,04 en seis meses. Es un
filtro de paso alto. Una capa de tendencia con umbrales relativos sería el mismo
filtro un piso más arriba: una racha mala larga subiría el listón de lo que
cuenta como "normal", y la capa se callaría justo el mes en que tendría que
hablar. Un indicador que se adapta a lo que mide no mide nada.

Por eso los cinco números viven en `config.yaml` como constantes, con el motivo
escrito al lado, y se cambian a mano mirando el replay. El `--tendencia` de
`scripts/replay_semaforo.py` existe para eso: sin poder falsarlos contra el
histórico, dentro de un año serían folklore.

LOS TRES DETECTORES
-------------------
  racha   -> N días seguidos sin un verde. Es el que avisa pronto.
  motivo  -> la misma regla manda semana tras semana. Dice QUÉ está pasando.
  ventana -> el último mes comparado con el trimestre. RETROSPECTIVO: confirma,
             no anticipa, y el texto lo dice con todas las letras para que no se
             lea como una alerta temprana.

EL CUALIFICADOR DE SUEÑO
------------------------
`sleep_score` no tiene regla propia en el semáforo, y no la va a tener: el
cuadrante de disociación (dormí bastante y dormí mal) disparaba 4 veces en seis
meses sin añadir un solo día nuevo, que es la definición de opción muerta. Pero
el dato sirve para otra cosa: cuando un aviso de esta capa señala al sueño,
separar cantidad de calidad cambia lo que hay que hacer. Dormir menos se
arregla acostándose antes; dormir igual y peor, no.

SIN MUESTRA NO ES "TODO BIEN"
-----------------------------
Un detector que no se puede evaluar se dice en voz alta (`sin_muestra`), igual
que `skipped` no es `not_fired` en `rules.py`. Callarlo convertiría "no lo he
mirado" en "no pasa nada", que es la mentira que más cara sale en un sistema que
decide solo.

ESTO NO ES UN PREDICTOR DE MIGRAÑAS, Y NO SE PUEDE CONVERTIR EN UNO
-------------------------------------------------------------------
Esta capa nació leyendo un histórico que incluía dos episodios (26/06 y 01/07 de
2026), y esa coincidencia es la trampa entera: es muy fácil mirar estos
detectores y empezar a leerlos como una alarma de "viene una". No lo son, y la
decisión de que no lo sean es del usuario y es deliberada, así que queda escrita
aquí y no en el historial de un chat.

Tres razones, y la tercera es la que de verdad cierra la puerta:

  1. Dos episodios no son una muestra. Cualquier umbral que los "acertara" está
     ajustado a dos puntos, y un umbral ajustado a dos puntos no es un umbral,
     es una anécdota con decimales.

  2. Los detectores son RETROSPECTIVOS por construcción. `ventana` compara el
     último mes con los dos anteriores y hacen falta semanas de racha para que
     `motivo` hable. Lo que sale de aquí describe por dónde se ha venido, no por
     dónde se va. Esa lentitud no es un defecto a corregir: es lo que lo hace
     fiable como descripción.

  3. El coste de los dos errores es asimétrico y no hay forma de equilibrarlo.
     Un falso positivo enseña a desconfiar del sistema entero -no solo de este
     aviso-; un falso negativo enseña a confiar en un seguro que no existe, y
     eso es peor que no tener nada, porque quien se fía deja de mirar. Un aviso
     que describe lo que YA pasó no tiene ninguno de los dos fallos: puede ser
     inútil, pero no puede mentir sobre el futuro.

En la práctica, esto significa que aquí NO entra: ningún detector orientado a
anticipar episodios, ninguna señal elegida por su correlación con ellos, ningún
umbral calibrado contra esas fechas, y ninguna frase en el mensaje con forma de
pronóstico. Si algún día hace falta esa herramienta, será otra capa, con su
propio nombre y su propia discusión sobre qué pasa cuando se equivoca. No esta.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable

from app.engine.signals import week_start

LUCES = {"green", "amber", "red"}

# Fracción mínima de días con decisión que hace falta para que una ventana se
# considere medida. Por debajo, el detector no responde: contar "3 de 4 días
# malos" sobre una ventana de 30 daría un 75% inventado.
COBERTURA_MINIMA = 0.5

# De qué habla cada señal, en castellano. Sirve para que el aviso de motivo diga
# "el sueño" en vez de "sueno_corto", que es el nombre de la regla y no el del
# problema. Se comparan por prefijo porque las señales derivadas son varias por
# tema (hrv, hrv_ratio, hrv_baseline) y todas hablan de lo mismo.
TEMAS: tuple[tuple[str, str], ...] = (
    ("sleep_", "el sueño"),
    ("hrv", "el HRV"),
    ("rhr", "la FC en reposo"),
    ("load_", "la carga acumulada"),
    ("fatigue", "el cansancio"),
    ("training_desire", "las ganas de entrenar"),
    ("lower_discomfort", "la zona lumbar"),
    ("upper_discomfort", "el tronco superior"),
    ("weekend_", "el fin de semana en bici"),
)

ORDINALES = {
    2: "segunda", 3: "tercera", 4: "cuarta", 5: "quinta", 6: "sexta",
    7: "séptima", 8: "octava", 9: "novena", 10: "décima", 11: "undécima",
    12: "duodécima",
}

# Tope de semanas que se recorren hacia atrás buscando el final de una racha de
# motivo. Seis meses es más de lo que cualquier racha real va a durar y acota el
# trabajo en un histórico que solo crece.
MAX_SEMANAS = 26

# Cuántas semanas SEGUIDAS sin un tema dominante puede atravesar una racha de
# motivo sin romperse.
#
# Que sea 1 y no 0 salió de medir. Con 0 -una semana sin dominante rompe la
# racha- el detector no disparaba NI UNA VEZ en los 179 días del replay, y no
# porque no hubiera nada que contar: había dos tramos de tres semanas seguidas
# mandando el sueño, partidos cada uno por una semana tranquila con un empate a
# uno. Un umbral que no dispara nunca no es prudente, es una opción muerta.
#
# Y el motivo de fondo es el mismo que gobierna `skipped` frente a `not_fired`
# en `rules.py`: una semana sin dominante NO dice que el tema haya cambiado,
# dice que esa semana no dice nada. Leerlo como un cambio es convertir el
# silencio en información. Lo que rompe la racha es que mande OTRO tema.
#
# Que sea 1 y no 2 es el otro lado: dos semanas seguidas calladas ya no son un
# hueco, son el final. Y la semana neutra no cuenta para el ordinal -"cuarta
# semana" son cuatro semanas mandando, no cuatro de calendario-, así que el
# aviso dice además cuántas semanas abarca para que el hueco no se disimule.
NEUTRAS_SEGUIDAS_MAX = 1


class TendenciaError(ValueError):
    """Entrada imposible: un semáforo inventado, un día repetido, falta `trend`.

    Es un error de programación o de configuración, nunca un dato que falte. Lo
    que falta se cuenta en `sin_muestra`.
    """


@dataclass(frozen=True)
class DecisionDia:
    """Un día del histórico, reducido a lo único que esta capa mira.

    No es `DayDecision`: es deliberadamente pobre. Así el replay puede alimentar
    la capa con decisiones que nunca existieron -las que el motor HABRÍA tomado-
    sin construir sesiones ni progresiones que no tendrían sentido.
    """

    day: date
    light: str
    trigger_rule: str | None = None

    @property
    def verde(self) -> bool:
        return self.light == "green"


@dataclass
class Aviso:
    tipo: str  # racha | motivo | ventana
    texto: str
    n: int
    ventana: str

    def to_dict(self) -> dict[str, Any]:
        return {"tipo": self.tipo, "texto": self.texto, "n": self.n,
                "ventana": self.ventana}


@dataclass
class SinMuestra:
    tipo: str
    motivo: str

    def to_dict(self) -> dict[str, Any]:
        return {"tipo": self.tipo, "motivo": self.motivo}


@dataclass
class Tendencia:
    day: date
    n: int
    avisos: list[Aviso] = field(default_factory=list)
    sin_muestra: list[SinMuestra] = field(default_factory=list)
    activa: bool = True

    def lineas(self) -> list[str]:
        """Las frases para el mensaje de la mañana. Vacía si la capa está apagada.

        El prefijo va en cada línea y no en una cabecera de bloque porque estas
        frases se leen sueltas, mezcladas con el resto del mensaje, y sin él
        "seis días seguidos sin verde" se confunde con algo que ha decidido hoy
        el semáforo.
        """
        if not self.activa:
            return []
        out = [f"Tendencia: {a.texto}" for a in self.avisos]
        if self.sin_muestra:
            detalle = " · ".join(f"{s.tipo} ({s.motivo})" for s in self.sin_muestra)
            out.append(f"Tendencia: sin muestra para {detalle}")
        if not out:
            out.append("Tendencia: sin novedad")
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "n": self.n,
            "activa": self.activa,
            "avisos": [a.to_dict() for a in self.avisos],
            "sin_muestra": [s.to_dict() for s in self.sin_muestra],
            "lineas": self.lineas(),
        }


# ---------------------------------------------------------------------------
# De regla a tema
# ---------------------------------------------------------------------------


def senales_de_regla(rule: dict[str, Any]) -> set[str]:
    """Las señales que una regla del semáforo consulta, leyendo su árbol `when`.

    Se camina el árbol en vez de fiarse de `requires` porque `requires` es
    documentación -`rules.py` lo dice: sirve para explicar qué faltaba, no como
    puerta previa- y puede quedarse corto o sobrar sin que nada se rompa. El
    `when` es lo que de verdad se evalúa.
    """
    out: set[str] = set()

    def camina(expr: Any) -> None:
        if not isinstance(expr, dict):
            return
        for clave, valor in expr.items():
            if clave in ("all", "any"):
                for sub in valor or []:
                    camina(sub)
            elif clave == "not":
                camina(valor)
            else:
                out.add(str(clave))

    camina(rule.get("when"))
    return out


def tema_de_regla(raw: dict[str, Any], nombre: str) -> str:
    """De qué habla una regla, en castellano y para un humano a las 7 de la mañana.

    Si la regla mezcla temas -`sin_ganas_y_reventado` mira cansancio Y ganas- no
    se elige uno de los dos: se devuelve el nombre de la regla. Resumir dos cosas
    en una sería perder justo lo que la regla tiene de particular.
    """
    thresholds = (raw.get("thresholds") or {})
    for nivel in ("red", "amber"):
        for rule in thresholds.get(nivel) or []:
            if rule.get("name") != nombre:
                continue
            temas = set()
            for senal in senales_de_regla(rule):
                for prefijo, tema in TEMAS:
                    if senal.startswith(prefijo):
                        temas.add(tema)
                        break
            if len(temas) == 1:
                return temas.pop()
            return f"«{nombre}»"
    return f"«{nombre}»"


def _ordinal(n: int) -> str:
    return ORDINALES.get(n, f"{n}.ª")


def _dm(d: date) -> str:
    return f"{d.day:02d}/{d.month:02d}"


def _pct(v: float) -> str:
    return f"{round(v)}%"


# ---------------------------------------------------------------------------
# Detectores
# ---------------------------------------------------------------------------


def _detecta_racha(
    day: date,
    por_dia: dict[date, DecisionDia],
    primera: date,
    raw: dict[str, Any],
    minimo: int,
) -> tuple[Aviso | None, SinMuestra | None, list[str]]:
    """Días seguidos sin un verde, contando hacia atrás desde hoy.

    Un hueco de calendario -un día que el sistema no corrió- NO rompe la racha,
    porque no dice nada: no hubo verde, hubo apagón. Pero se cuenta aparte y se
    nombra en el aviso, que es la diferencia entre "seis días malos seguidos" y
    "seis días malos con dos que no sabemos".

    Devuelve además los temas dominantes de la racha -en plural, porque pueden
    empatar- para que el cualificador de sueño sepa si tiene algo que decir.
    """
    if (day - primera).days + 1 < minimo:
        return None, SinMuestra(
            "racha",
            f"hacen falta {minimo} días de histórico y hay "
            f"{(day - primera).days + 1} desde el {_dm(primera)}",
        ), []

    n = 0
    huecos = 0
    pendientes = 0
    inicio = day
    temas: dict[str, int] = {}
    d = day
    while d >= primera:
        dec = por_dia.get(d)
        if dec is None:
            # Todavía no se sabe si este hueco cae DENTRO de la racha o por
            # detrás de ella: solo lo sabrá si más atrás aparece otro día no
            # verde. Hasta entonces queda en el aire.
            pendientes += 1
        elif dec.verde:
            break
        else:
            n += 1
            huecos += pendientes
            pendientes = 0
            inicio = d
            if dec.trigger_rule:
                tema = tema_de_regla(raw, dec.trigger_rule)
                temas[tema] = temas.get(tema, 0) + 1
        d -= timedelta(days=1)

    if n < minimo:
        # Lista vacía y no `None`: hoy el consumidor pregunta `"el sueño" not in
        # temas` detrás de un `aviso is None`, así que un `None` aquí no
        # reventaría nunca -y esa es justo la clase de mina que se pisa el día
        # que alguien reordena la condición-.
        return None, None, []

    cola = f", con {huecos} sin decisión por medio" if huecos else ""
    mandan = _dominantes(temas)

    # El empate SE DICE. Callarlo -que es lo que hacía- tenía el efecto justo al
    # revés del que parece: la racha se contaba igual, pero sin una sola palabra
    # sobre qué la estaba produciendo, y una racha sin motivo se lee como una
    # racha sin explicación en vez de como una con dos. Que manden dos cosas a la
    # vez no es menos información que una, es más, y es de las pocas que esta
    # capa puede dar y el semáforo no.
    #
    # El tope de tres es de lectura, no de estadística: a partir de ahí la frase
    # deja de ser una pista y pasa a ser un inventario, y un inventario a las
    # siete de la mañana no lo lee nadie. Con cuatro temas repartidos lo que hay
    # que decir no es cuáles son, es que no manda ninguno.
    if not mandan:
        quien = ""
    elif len(mandan) == 1:
        quien = f" Manda {mandan[0]}."
    elif len(mandan) <= 3:
        quien = f" Manda {_enumera(mandan)} a partes iguales."
    else:
        quien = f" No manda ninguno: {len(mandan)} temas repartidos por igual."

    texto = (
        f"{n} días seguidos sin un verde ({_dm(inicio)} → {_dm(day)}{cola})."
        f"{quien} Ninguna regla del semáforo mira tan atrás: hoy se ha decidido "
        f"sin saberlo"
    )
    return (
        Aviso("racha", texto, n, f"{inicio.isoformat()}..{day.isoformat()}"),
        None,
        mandan,
    )


def _dominantes(cuenta: dict[str, int]) -> list[str]:
    """Todas las claves empatadas en lo más alto, en orden estable.

    El orden es alfabético y no por frecuencia -están empatadas, no hay
    frecuencia que las ordene-. Importa que sea DETERMINISTA: si dependiera del
    orden de inserción del diccionario, la misma racha podría contarse de dos
    maneras distintas según qué día se mirara, y una frase que cambia sin que
    cambien los datos es una frase en la que no se puede confiar.
    """
    if not cuenta:
        return []
    tope = max(cuenta.values())
    return sorted(k for k, v in cuenta.items() if v == tope)


def _dominante(cuenta: dict[str, int]) -> str | None:
    """La clave con más apariciones, solo si gana en solitario.

    Un empate devuelve `None` A PROPÓSITO, y eso es carga estructural del
    detector de motivo: una semana empatada no dice que el tema haya cambiado,
    dice que esa semana no dice nada, y por eso se atraviesa como neutra en vez
    de romper la racha (ver `NEUTRAS_SEGUIDAS_MAX`). Si el empate eligiera un
    tema cualquiera, media racha real se partiría en dos por una semana que no
    tenía opinión.

    Esto NO es lo mismo que callar el empate en el mensaje. Dentro de una semana
    el empate es ausencia de señal; dentro de una racha ya contada es una señal
    que hay que decir, y para eso está `_dominantes`.
    """
    solos = _dominantes(cuenta)
    return solos[0] if len(solos) == 1 else None


def _enumera(cosas: list[str]) -> str:
    """«A», «A y B», «A, B y C». Ninguno de los `TEMAS` empieza por i- ni hi-,
    así que la `y` nunca tiene que volverse `e`."""
    if len(cosas) == 1:
        return cosas[0]
    return f"{', '.join(cosas[:-1])} y {cosas[-1]}"


@dataclass
class _Semana:
    inicio: date
    cubierta: int
    dominante: str | None  # un TEMA, no el nombre de una regla
    reglas: dict[str, int] = field(default_factory=dict)


def _semanas(
    day: date, por_dia: dict[date, DecisionDia], primera: date, raw: dict[str, Any]
) -> list[_Semana]:
    """Las semanas ISO COMPLETAS anteriores a hoy, de la más reciente hacia atrás.

    La semana en curso se descarta entera. Contar una semana a medias es lo que
    haría que un lunes malo pareciera "una semana mala" y que la frase del jueves
    contradijese a la del lunes sin que hubiera pasado nada nuevo.

    El dominante se busca por TEMA y no por nombre de regla, y no es cosmética:
    `sueno_corto` y `sueno_muy_corto` son el mismo problema con dos intensidades,
    igual que `hrv_baja_1d` y `hrv_hundida_2d`. Contarlas por separado parte en
    dos una semana que habla de una sola cosa y deja sin dominante -o con el
    dominante equivocado- semanas que lo tenían clarísimo.
    """
    out: list[_Semana] = []
    inicio = week_start(day) - timedelta(days=7)
    tope = week_start(primera)
    while inicio >= tope and len(out) < MAX_SEMANAS:
        dias = [por_dia[inicio + timedelta(days=i)]
                for i in range(7)
                if inicio + timedelta(days=i) in por_dia]
        temas: dict[str, int] = {}
        reglas: dict[str, int] = {}
        for dec in dias:
            if dec.verde or not dec.trigger_rule:
                continue
            tema = tema_de_regla(raw, dec.trigger_rule)
            temas[tema] = temas.get(tema, 0) + 1
            reglas[dec.trigger_rule] = reglas.get(dec.trigger_rule, 0) + 1
        out.append(_Semana(inicio, len(dias), _dominante(temas), reglas))
        inicio -= timedelta(days=7)
    return out


def _detecta_motivo(
    day: date,
    por_dia: dict[date, DecisionDia],
    primera: date,
    raw: dict[str, Any],
    minimo: int,
) -> tuple[Aviso | None, SinMuestra | None, str | None]:
    """El mismo tema manda semana tras semana.

    Es el detector que dice QUÉ está pasando, y el único que sobrevive a que el
    reparto de colores no se mueva: entre mayo y agosto el porcentaje de ámbares
    fue casi el mismo y lo que los producía cambió por completo.

    Una semana sin dominante -empate, o ni un día no verde- no rompe la racha:
    la atraviesa. Ver `NEUTRAS_SEGUIDAS_MAX`, que lleva escrito lo que costó
    medir esa decisión.
    """
    semanas = _semanas(day, por_dia, primera, raw)
    minimo_dias = COBERTURA_MINIMA * 7

    if len(semanas) < minimo:
        return None, SinMuestra(
            "motivo",
            f"hacen falta {minimo} semanas completas y hay {len(semanas)}",
        ), None

    flojas = [s for s in semanas[:minimo] if s.cubierta < minimo_dias]
    if flojas:
        return None, SinMuestra(
            "motivo",
            f"la semana del {_dm(flojas[0].inicio)} solo tiene "
            f"{flojas[0].cubierta} "
            f"{'día' if flojas[0].cubierta == 1 else 'días'} con decisión de 7",
        ), None

    cabeza: str | None = None
    n = 0
    neutras = 0
    huecas = 0
    contadas: list[_Semana] = []
    for s in semanas:
        if s.cubierta < minimo_dias:
            break
        if s.dominante is None:
            neutras += 1
            if neutras > NEUTRAS_SEGUIDAS_MAX:
                break
            huecas += 1
            continue
        if cabeza is None:
            cabeza = s.dominante
        elif s.dominante != cabeza:
            break
        neutras = 0
        n += 1
        contadas.append(s)

    if cabeza is None or n < minimo:
        return None, None, None

    # El tramo va de la PRIMERA a la ÚLTIMA semana que de verdad contaron. Las
    # neutras que quedaran colgando por delante o por detrás no alargan el rango.
    ultima, primera_sem = contadas[0], contadas[-1]
    fin = ultima.inicio + timedelta(days=6)
    abarcadas = ((ultima.inicio - primera_sem.inicio).days // 7) + 1
    hueco = ""
    if abarcadas > n:
        sueltas = abarcadas - n
        hueco = (f", con {sueltas} semana{'s' if sueltas > 1 else ''} sin "
                 f"dominante por medio")
    detalle = ", ".join(
        f"{k}×{v}" for k, v in sorted(
            {r: sum(s.reglas.get(r, 0) for s in contadas)
             for s in contadas for r in s.reglas}.items(),
            key=lambda kv: (-kv[1], kv[0]),
        )
    )
    texto = (
        f"{cabeza} manda por {_ordinal(n)} semana ({_dm(primera_sem.inicio)} → "
        f"{_dm(fin)}{hueco}). Reglas: {detalle}"
    )
    return (
        Aviso("motivo", texto, n,
              f"{primera_sem.inicio.isoformat()}..{fin.isoformat()}"),
        None,
        cabeza,
    )


def _detecta_ventana(
    day: date,
    por_dia: dict[date, DecisionDia],
    corta: int,
    larga: int,
    delta_min: float,
) -> tuple[Aviso | None, SinMuestra | None]:
    """El último mes comparado con los meses anteriores. Llega tarde y lo dice.

    Es el detector más lento de los tres y se queda a propósito: un indicador que
    confirma "el último mes ha ido peor que lo de antes" vale aunque no avise
    pronto. Lo que no puede es leerse como una alerta temprana, así que el texto
    se etiqueta como retrospectivo y no se deja al lector deducirlo.

    LAS VENTANAS NO SE SOLAPAN
    --------------------------
    La primera versión comparaba los 30 últimos días contra los 90 últimos, con
    los 30 metidos dentro. El comentario de entonces defendía el solape diciendo
    que un tramo disjunto se queda sin días en cuanto hay un hueco. Eso se
    arregla con el mínimo de cobertura, que ya existía; lo que no se arreglaba
    era el otro lado: el último mes pesaba un tercio de su propia referencia y
    la arrastraba hacia sí, de modo que la diferencia impresa era siempre menor
    que la real. El mismo defecto destrozaba al cualificador de sueño, donde se
    midió: allí una caída real de unos 50 minutos nunca llegó a verse como más
    de 22.

    Aquí el solape no llegó a callar ningún aviso -el empeoramiento de agosto
    fue brusco y no una deriva, así que cruzó el umbral igual-, pero los puntos
    que imprimía eran menores que los verdaderos. Un número que se lee cada
    mañana tiene que ser el número.
    """
    ref = larga - corta

    def mide(hasta: int, desde: int = 0) -> tuple[int, int]:
        vistos = [por_dia[day - timedelta(days=i)]
                  for i in range(desde, hasta)
                  if day - timedelta(days=i) in por_dia]
        return len(vistos), sum(1 for d in vistos if not d.verde)

    n_corta, malos_corta = mide(corta)
    n_larga, malos_larga = mide(larga, desde=corta)

    if n_corta < COBERTURA_MINIMA * corta or n_larga < COBERTURA_MINIMA * ref:
        return None, SinMuestra(
            "ventana",
            f"{n_corta}/{corta} días en el último mes y {n_larga}/{ref} en los "
            f"{ref} días anteriores; hace falta la mitad de cada tramo",
        )

    pct_corta = 100.0 * malos_corta / n_corta
    pct_larga = 100.0 * malos_larga / n_larga
    delta = pct_corta - pct_larga
    if delta < delta_min:
        return None, None

    texto = (
        f"RETROSPECTIVO — el último mes ha ido peor que los {ref} días "
        f"anteriores: {_pct(pct_corta)} de días no verdes en {corta} días "
        f"frente a {_pct(pct_larga)} en los {ref} de antes ({delta:+.0f} "
        f"puntos). Es una lectura hacia atrás: confirma lo que ya ha pasado, no "
        f"avisa de lo que viene"
    )
    return Aviso("ventana", texto, round(delta), f"{corta}d vs {ref}d previos"), None


# ---------------------------------------------------------------------------
# Cualificador de sueño
# ---------------------------------------------------------------------------


def _media(
    serie: dict[date, float | None] | None,
    day: date,
    hasta: int,
    desde: int = 0,
) -> tuple[float | None, int]:
    """Media de la serie en el tramo [desde, hasta) días hacia atrás desde `day`.

    `desde` existe para poder pedir un tramo que NO incluya los días recientes.
    Con `desde=0` es la ventana de siempre; con `desde=30, hasta=90` son los
    sesenta días ANTERIORES al último mes, sin solaparse con él.
    """
    if not serie:
        return None, 0
    vals = [
        v for i in range(desde, hasta)
        if (v := serie.get(day - timedelta(days=i))) is not None
    ]
    return (sum(vals) / len(vals) if vals else None), len(vals)


def _cualifica_sueno(
    day: date,
    sleep_score: dict[date, float | None] | None,
    sleep_min: dict[date, float | None] | None,
    corta: int,
    larga: int,
    caida_score: float,
    caida_min: float,
) -> tuple[str | None, SinMuestra | None]:
    """Cantidad o calidad: qué se ha movido del sueño en el último mes.

    `sleep_score` no dispara ninguna regla del semáforo y no va a dispararla. Su
    sitio es este: cuando un aviso de tendencia ya ha señalado al sueño, decir si
    lo que ha bajado es el tiempo en la cama o lo que Garmin puntúa de ese tiempo.
    Son dos problemas distintos con dos arreglos distintos, y sin separarlos el
    aviso solo repite el nombre de la regla que ya se ha leído.

    LAS DOS VENTANAS NO SE SOLAPAN, Y ESO SE PAGÓ CARO POR APRENDERLO
    ----------------------------------------------------------------
    La primera versión comparaba los últimos 30 días contra los últimos 90, con
    los 30 metidos dentro de los 90. Medido sobre el replay, la diferencia de
    minutos no pasó nunca de 22 mientras el sueño real caía unos 50 minutos de
    mayo a agosto; y el 01/09, tras dos meses con el peor sueño del registro,
    reportaba la diferencia MÁS PEQUEÑA de toda la serie -4,6 minutos- y
    concluía "lo que se mueve es el umbral, no el descanso".

    Era el filtro de paso alto que este módulo entero existe para evitar, dentro
    de la pieza construida para evitarlo. Dos causas, las dos estructurales y
    ninguna arreglable moviendo el umbral: el último mes era un tercio de su
    propia referencia, lo que amortigua la diferencia mecánicamente; y como las
    dos ventanas se deslizan, una deriva lenta nunca abre hueco entre ellas por
    lejos que llegue.

    Ahora la referencia es el tramo [corta, larga): los días anteriores al
    último mes, sin un solo día compartido. Por eso `corta < larga` no es una
    validación cosmética del cargador -es lo que impide que la referencia se
    quede vacía-.
    """
    ref = larga - corta
    sc_corta, n_sc_c = _media(sleep_score, day, corta)
    sc_larga, n_sc_l = _media(sleep_score, day, larga, desde=corta)
    mn_corta, n_mn_c = _media(sleep_min, day, corta)
    mn_larga, n_mn_l = _media(sleep_min, day, larga, desde=corta)

    falta_score = sc_corta is None or sc_larga is None or \
        n_sc_c < COBERTURA_MINIMA * corta or n_sc_l < COBERTURA_MINIMA * ref
    falta_min = mn_corta is None or mn_larga is None or \
        n_mn_c < COBERTURA_MINIMA * corta or n_mn_l < COBERTURA_MINIMA * ref

    if falta_score and falta_min:
        return None, SinMuestra(
            "sueño",
            f"el aviso señala al sueño y no hay serie suficiente para separar "
            f"cantidad de calidad ({n_mn_c} min y {n_sc_c} score en {corta} días)",
        )
    if falta_score:
        return None, SinMuestra(
            "sueño",
            f"hay minutos pero no `sleep_score` suficiente ({n_sc_c} de {corta} "
            f"días): no se puede decir si además duermes peor",
        )
    if falta_min:
        return None, SinMuestra(
            "sueño",
            f"hay `sleep_score` pero no minutos suficientes ({n_mn_c} de {corta} "
            f"días): no se puede decir si además duermes menos",
        )

    baja_min = (mn_larga - mn_corta) >= caida_min
    baja_score = (sc_larga - sc_corta) >= caida_score
    d_min = mn_larga - mn_corta
    d_sc = sc_larga - sc_corta

    # El texto nombra el tramo de comparación por lo que ES -los `ref` días
    # ANTERIORES al último mes- y no por la ventana larga entera. Decir "que en
    # 90 días" cuando la referencia excluye los 30 últimos sería describir mal
    # la cuenta justo en la frase que la justifica.
    if baja_min and baja_score:
        return (
            f"y es las dos cosas: {d_min:.0f} min menos y {d_sc:.0f} puntos "
            f"menos de calidad que en los {ref} días anteriores"
        ), None
    if baja_min:
        return (
            f"y es cantidad: {d_min:.0f} min menos que en los {ref} días "
            f"anteriores, con la calidad igual"
        ), None
    if baja_score:
        return (
            f"y es calidad, no cantidad: el mismo tiempo en la cama y "
            f"{d_sc:.0f} puntos menos de sueño que en los {ref} días anteriores"
        ), None
    return (
        f"pero el sueño no ha empeorado respecto a los {ref} días anteriores ni "
        f"en tiempo ni en calidad: lo que se mueve es el umbral, no el descanso"
    ), None


# ---------------------------------------------------------------------------
# Entrada principal
# ---------------------------------------------------------------------------


def _config_trend(config: Any) -> dict[str, Any]:
    raw = config.raw if hasattr(config, "raw") else config
    trend = (raw or {}).get("trend")
    if not isinstance(trend, dict):
        raise TendenciaError(
            "falta la sección 'trend' en config.yaml. No hay defectos para estos "
            "umbrales a propósito: un valor inventado aquí decide durante meses "
            "qué se considera una mala racha y nadie lo notaría"
        )
    return trend


def evaluar_tendencia(
    config: Any,
    day: date,
    decisiones: Iterable[DecisionDia],
    *,
    sleep_score: dict[date, float | None] | None = None,
    sleep_min: dict[date, float | None] | None = None,
) -> Tendencia:
    """Los tres detectores sobre el histórico de decisiones. Función pura.

    `decisiones` incluye la de HOY: el llamante la añade en memoria en vez de
    leerla de la base, porque a las 06:30 todavía no está escrita y porque en el
    recálculo de las 09:40 la que está escrita es la anterior.
    """
    trend = _config_trend(config)
    raw = config.raw if hasattr(config, "raw") else config

    por_dia: dict[date, DecisionDia] = {}
    for dec in decisiones:
        if dec.light not in LUCES:
            raise TendenciaError(
                f"semáforo desconocido {dec.light!r} el {dec.day}. Válidos: "
                f"{sorted(LUCES)}. Contarlo como no verde -o como verde- sería "
                f"inventarse el histórico sobre el que se mide la tendencia"
            )
        if dec.day in por_dia:
            raise TendenciaError(
                f"el día {dec.day} aparece dos veces en el histórico. La serie "
                f"tiene que venir ya resuelta a una decisión vigente por día"
            )
        if dec.day > day:
            raise TendenciaError(
                f"el día {dec.day} es posterior al día evaluado ({day}): la "
                f"tendencia no puede mirar hacia adelante"
            )
        por_dia[dec.day] = dec

    if not bool(trend.get("enabled", False)):
        return Tendencia(day=day, n=len(por_dia), activa=False)

    t = Tendencia(day=day, n=len(por_dia))
    if not por_dia:
        t.sin_muestra.append(SinMuestra("todos", "no hay ni una decisión previa"))
        return t

    primera = min(por_dia)
    corta = int(trend["ventana_corta_dias"])
    larga = int(trend["ventana_larga_dias"])

    a_motivo, sm_motivo, tema_motivo = _detecta_motivo(
        day, por_dia, primera, raw, int(trend["motivo_semanas_min"])
    )
    a_racha, sm_racha, temas_racha = _detecta_racha(
        day, por_dia, primera, raw, int(trend["racha_min"])
    )
    a_ventana, sm_ventana = _detecta_ventana(
        day, por_dia, corta, larga, float(trend["delta_pp_min"])
    )

    # El cualificador se engancha al primer aviso que señale al sueño, y el orden
    # es el mismo en que se leen: si el motivo ya dice "el sueño manda por quinta
    # semana", el matiz va ahí y no repetido en la racha.
    #
    # Los temas van en lista porque la racha puede tener varios empatados. Si el
    # sueño es uno de ellos, el matiz se engancha igual: el aviso ya lo ha
    # nombrado en voz alta, así que separar cantidad de calidad sigue cambiando
    # lo que hay que hacer. Exigir que mandara en solitario dejaría sin explicar
    # justo la mitad de los casos en que el sueño está metido.
    sueno = trend.get("sueno") or {}
    for aviso, temas in ((a_motivo, [tema_motivo] if tema_motivo else []), (a_racha, temas_racha)):
        if aviso is None or "el sueño" not in temas:
            continue
        matiz, sm = _cualifica_sueno(
            day, sleep_score, sleep_min, corta, larga,
            float(sueno["caida_score_min"]), float(sueno["caida_min_min"]),
        )
        if matiz:
            aviso.texto = f"{aviso.texto} — {matiz}"
        if sm:
            t.sin_muestra.append(sm)
        break

    t.avisos = [a for a in (a_motivo, a_racha, a_ventana) if a is not None]
    t.sin_muestra.extend(s for s in (sm_motivo, sm_racha, sm_ventana) if s is not None)
    return t
