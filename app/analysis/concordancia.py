"""Vistas 1 y 2: si lo que nota coincide con lo que mide el reloj, y con cuánto retraso.

Son la misma pregunta dos veces. La Vista 1 la hace el mismo día -¿el martes que
me sentí reventado tenía la HRV baja?- y la Vista 2 la hace corriendo la ventana
de -3 a +3 días, porque la respuesta interesante casi nunca cae en el cero: un
cuerpo que se rompe el lunes suele dar la cara el miércoles, y una percepción que
se adelanta es tan informativa como una que va por detrás.

Van juntas en un módulo porque comparten las parejas y comparten la trampa. Si el
emparejado se hiciera de dos maneras distintas, la Vista 1 y la Vista 2 podrían
discrepar en el desfase 0 y no habría forma de saber cuál miente.

Y EL RELOJ, ¿COINCIDE CONSIGO MISMO?
------------------------------------
La Vista 1 lleva desde el principio una pregunta debajo que no se hacía en voz
alta. Cuando una pareja de percepción sale plana -cansancio contra HRV, r = 0.05-
hay dos explicaciones y son muy distintas: o él no nota lo que le pasa al cuerpo,
o esa métrica del reloj no mide gran cosa. El número es idéntico en los dos casos
y la primera lectura es la que hace daño.

Las correlaciones internas de Garmin son el suelo contra el que se lee todo lo
demás. Las cinco métricas del reloj describen el mismo día y el mismo cuerpo, así
que entre ellas tiene que haber señal; si tampoco la hay, el problema no está en
la percepción. Por eso viven en esta vista y no en una pantalla aparte: separadas
serían una curiosidad, y aquí son el control.

Tienen además una virtud práctica que no hay que disimular: no dependen del
check-in. El día que se enciende el sistema, con cero mañanas contestadas y seis
meses de reloj detrás, este bloque es lo único de esta pantalla que dice algo.

LO QUE ESTE MÓDULO NO HACE
--------------------------
No decide nada. Ni una regla del motor mira aquí, ni debe. Estas dos vistas son
para MIRARLAS, y lo que se haga con lo que digan lo decide una persona editando
el `config.yaml`, no el sistema ajustándose solo. Un sistema que se recalibra con
su propia salida se persigue la cola.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from itertools import combinations
from typing import Any

from sqlalchemy.orm import Session

from app.analysis import series as S
from app.analysis.preguntas import tabla_discordancia
from app.analysis.stats import corregir_tanda, correlacion, emparejar, mejor_desfase

# Cuántos días mira por defecto. Coincide a propósito con la ventana del
# histórico de Garmin: pedir más sería pedir días que no existen, y pedir menos
# sería tirar a la basura el backfill que se gastó en traerlos.
DIAS_POR_DEFECTO = 180

# El barrido de la Vista 2, tal cual se pidió.
RANGO_DESFASE = range(-3, 4)


@dataclass(frozen=True)
class Par:
    """Una pareja que merece la pena mirar, y qué se espera de ella.

    `espera` es el signo que tendría la correlación SI la percepción y el reloj
    dijeran lo mismo. Casi siempre sale solo de los `sentido` de las dos series
    -si una es "alto peor" y la otra "alto mejor", coincidir significa correlación
    negativa-, y solo hay que escribirlo a mano cuando alguna de las dos es
    neutra. Tenerlo permite mandar al navegador la frase "esto va al revés de lo
    que debería", que es la única lectura de un signo que de verdad importa.
    """

    x: str
    y: str
    titulo: str
    espera: int | None = None  # 1, -1 o None = dedúcelo

    @property
    def signo_esperado(self) -> int | None:
        if self.espera is not None:
            return self.espera
        a, b = S.DEFINICIONES[self.x], S.DEFINICIONES[self.y]
        if a.sentido == "neutro" or b.sentido == "neutro":
            return None
        return 1 if a.sentido == b.sentido else -1


# Las parejas de la Vista 1. Son las cinco que se pidieron, con dos abiertas en
# dos: "sueño medido" son los minutos Y la nota, que no siempre van juntos -una
# noche larga y mala existe-, y "carga del entreno" son la bici Y la fuerza, que
# se miden en unidades distintas y no se pueden sumar. Abrirlas es lo contrario
# de las medias tintas: es no elegir por él cuál de las dos era la que valía.
PARES: tuple[Par, ...] = (
    Par("fatigue", "hrv", "Cansancio frente a variabilidad"),
    Par("fatigue", "rhr", "Cansancio frente a FC en reposo"),
    Par("sleep_quality", "sleep_min", "Sueño percibido frente a minutos dormidos"),
    Par("sleep_quality", "sleep_score", "Sueño percibido frente a nota de sueño"),
    Par("training_desire", "body_battery", "Ganas de entrenar frente a Body Battery"),
    # Neutras las dos, así que el signo va escrito: más carga debería ser más
    # esfuerzo percibido. Es la pareja más parecida a una identidad de todas, y
    # por eso es la mejor para detectar que algo está mal emparejado: si ESTA
    # sale plana, el que está roto es el desplazamiento de `yesterday_rpe`, no él.
    Par("yesterday_rpe", "carga_bici", "Esfuerzo percibido frente a carga de la bici", 1),
    Par(
        "yesterday_rpe",
        "volumen_fuerza",
        "Esfuerzo percibido frente a volumen de fuerza",
        1,
    ),
)


# ---------------------------------------------------------------------------
# Las correlaciones internas del reloj
# ---------------------------------------------------------------------------

# Qué parejas NO son dos medidas independientes del mismo día.
#
# Esto es lo primero que hay que saber para leer el bloque, y por eso va antes
# que el bloque. Garmin publica -a grandes rasgos, la fórmula exacta es suya y
# está cerrada- de qué se alimenta cada número: la nota de sueño se construye con
# los minutos dormidos, las fases y las constantes de la noche, y el Body Battery
# con la variabilidad, el estrés, el sueño y la actividad. Es decir: dos de las
# cinco métricas son RESÚMENES de las otras tres.
#
# Así que una correlación alta entre la nota de sueño y los minutos dormidos no
# dice nada del cuerpo de nadie: dice que Garmin usa los minutos para calcular la
# nota. Sin este aviso al lado, ese 0.7 se lee como un hallazgo, y es aritmética
# del reloj.
#
# Lo que sí dice algo -y mucho- es que una de estas parejas salga PLANA o al
# revés. Si la nota de sueño no sigue a los minutos, es que en su caso la nota la
# está dominando otra cosa, y entonces "he dormido mal" según el reloj no
# significa "he dormido poco".
MISMO_ORIGEN: dict[frozenset[str], str] = {
    frozenset(("hrv", "rhr")): (
        "las dos salen de la misma grabación latido a latido de la noche: no son "
        "dos medidas, son dos lecturas de una"
    ),
    frozenset(("hrv", "sleep_score")): (
        "la variabilidad de la noche es uno de los ingredientes de la nota de sueño"
    ),
    frozenset(("rhr", "sleep_score")): (
        "la frecuencia cardiaca de la noche es uno de los ingredientes de la nota "
        "de sueño"
    ),
    frozenset(("sleep_min", "sleep_score")): (
        "los minutos dormidos entran directamente en el cálculo de la nota: parte "
        "de esta relación es la fórmula del reloj, no el cuerpo"
    ),
    frozenset(("hrv", "body_battery")): (
        "el Body Battery se construye sobre la variabilidad y el estrés derivado "
        "de ella"
    ),
    frozenset(("rhr", "body_battery")): (
        "el Body Battery se construye sobre las constantes de la noche, la de "
        "reposo entre ellas"
    ),
    frozenset(("sleep_min", "body_battery")): (
        "dormir es lo que recarga el Body Battery de la mañana: la relación está "
        "puesta a mano en el modelo del reloj"
    ),
    frozenset(("sleep_score", "body_battery")): (
        "los dos son resúmenes que Garmin calcula, y calculados con casi los "
        "mismos ingredientes"
    ),
}

# Las parejas que NO están arriba -sueño medido contra variabilidad y contra FC
# en reposo- son las únicas donde el reloj mide dos cosas de verdad distintas:
# cuánto duró la noche, y cómo estuvo el corazón durante ella. Son las que pueden
# decir algo del cuerpo sin que la fórmula conteste antes.
#
# El aviso no lleva ni una cifra escrita, y es deliberado. Decir aquí "ocho de
# las diez" habría durado exactamente hasta la primera métrica nueva en
# `S.GARMIN`: la pantalla pasaría a tener quince parejas y este párrafo seguiría
# hablando de diez, con toda la seguridad del mundo y sin dar un solo error. Los
# números los cuenta `resumen_internas` sobre las casillas que de verdad hay.
AVISO_INTERNAS = (
    "Estas parejas cruzan el reloj consigo mismo, y sirven de suelo para leer "
    "las de arriba: si dos métricas de Garmin tampoco se parecen entre ellas, "
    "que una percepción no las siga deja de ser un fallo de la percepción. La "
    "mayoría llevan aviso porque el reloj calcula una de las dos a partir de la "
    "otra -la nota de sueño y el Body Battery no son medidas, son resúmenes que "
    "Garmin construye con las demás-, y en esas lo informativo no es que la "
    "correlación salga alta, es que salga baja o al revés."
)


def pares_internos() -> tuple[Par, ...]:
    """Todas las parejas posibles de las métricas del reloj, generadas.

    Generadas y no escritas a mano. Una sexta métrica en `S.GARMIN` -temperatura
    de la piel, saturación- se cruza sola con las cinco que ya hay. Escrita la
    lista a mano, esa métrica nueva entraría en las series, se pintaría su
    gráfico y no se cruzaría con nada; y eso no daría ningún error, daría una
    pantalla aparentemente completa a la que le faltarían cinco casillas.

    El signo esperado sale del `sentido` de cada una y no se escribe nunca: las
    cinco métricas de Garmin tienen sentido declarado -ninguna es neutra-, así
    que las diez parejas salen con su signo. Dos "alto es mejor" tienen que subir
    juntas; HRV contra FC en reposo tiene que ir al revés, porque un buen día es
    variabilidad alta y pulsaciones bajas.
    """
    claves = list(S.GARMIN)
    return tuple(
        Par(a, b, f"{S.GARMIN[a].etiqueta} frente a {S.GARMIN[b].etiqueta}")
        for a, b in combinations(claves, 2)
    )


def _al_reves(par: Par, res: Any) -> bool | None:
    """¿La correlación contradice el signo que se esperaba? `None` si no se puede decir.

    `None` cubre dos casos que no hay que confundir con un "no": que no haya
    número, y que lo haya pero sea tan pequeño que su signo es una moneda al
    aire. Un r de -0.02 donde se esperaba +1 no es una contradicción, es cero
    escrito con mala suerte, y contarlo como contradicción llenaría el resumen
    de hallazgos inventados.

    Una sola definición para la frase y para el recuento del resumen. Con dos, la
    pantalla podría decir "1 de 10 va al revés" y no marcar ninguna.
    """
    if res.r is None or par.signo_esperado is None or abs(res.r) < 0.1:
        return None
    return (res.r > 0) != (par.signo_esperado > 0)


def _lectura_interna(par: Par, res: Any) -> str | None:
    """El signo entre dos métricas del reloj, traducido. No habla de percepción.

    Es otra función y no la de arriba porque `_lectura` dice "coincide con lo que
    marca el reloj", y aquí las dos series SON el reloj: la frase se quedaría sin
    referente y diría que el reloj coincide consigo mismo, que es la conclusión
    que este bloque tiene que dejar mirar, no dar por hecha.
    """
    contra = _al_reves(par, res)
    if contra is None:
        if res.r is None:
            return None
        return "van cada una por su lado: saber una no dice casi nada de la otra"
    fuerza = "claramente" if abs(res.r) >= 0.4 else "débilmente" if abs(res.r) < 0.25 else ""
    cola = "" if res.suficiente else " (muestra corta todavía)"
    # La frase no menciona el signo, y es a propósito. La pareja HRV contra FC en
    # reposo sale en -0.66 y eso es COINCIDIR: variabilidad alta con pulsaciones
    # bajas es un buen día por partida doble. Cualquier redacción que hable de
    # "el mismo sentido" al lado de un r negativo se lee mal, así que se habla de
    # días buenos y malos, que es lo que el signo significa una vez traducido.
    if contra:
        return (
            f"se contradicen {fuerza}: los días buenos para una tienden a ser "
            f"malos para la otra{cola}"
        ).replace("  ", " ")
    return (
        f"coinciden {fuerza} en qué días fueron buenos y cuáles malos{cola}"
    ).replace("  ", " ")


def _ventana(dias: int, hoy: date | None) -> tuple[date, date]:
    hoy = hoy or date.today()
    return hoy - timedelta(days=dias - 1), hoy


def _claves_de_la_vista() -> list[str]:
    """Deslizadores, preguntas, métricas de Garmin y lo que pidan las parejas.

    Se calcula, no se escribe. Añadir una pareja nueva con una serie de entreno
    que no estuviera aquí dejaría la correlación calculada pero el gráfico sin
    una de sus dos líneas, y nadie se daría cuenta hasta abrirlo en el móvil.

    `S.PREGUNTAS` va NOMBRADO, y esa es la mitad del diseño de `series.py`: las
    dos respuestas de Sí/No y la discordancia no viven en `SLIDERS` justamente
    para que llegar hasta aquí cueste escribirlo. Se escribe, porque se pidió
    que se contaran en todas las correlaciones, y se escribe en este sitio y no
    en la lista que consulta el semáforo.
    """
    claves = list(S.SLIDERS) + list(S.PREGUNTAS) + list(S.GARMIN)
    for p in PARES:
        for c in (p.x, p.y):
            if c not in claves:
                claves.append(c)
    return claves


def _lectura(par: Par, res: Any) -> str | None:
    """El signo traducido a una frase, o `None` si todavía no se puede decir nada."""
    if res.r is None:
        return None
    esperado = par.signo_esperado
    if esperado is None or abs(res.r) < 0.1:
        return "no se parecen lo bastante como para decir nada del sentido"
    va_como_toca = (res.r > 0) == (esperado > 0)
    fuerza = "claramente" if abs(res.r) >= 0.4 else "débilmente" if abs(res.r) < 0.25 else ""
    cola = "" if res.suficiente else " (muestra corta todavía)"
    if va_como_toca:
        return f"coincide {fuerza} con lo que marca el reloj{cola}".replace("  ", " ")
    return (
        f"va {fuerza} AL REVÉS de lo que marca el reloj{cola}".replace("  ", " ")
    )


def vista_concordancia(
    session: Session,
    *,
    dias: int = DIAS_POR_DEFECTO,
    hoy: date | None = None,
    metodo: str = "spearman",
) -> dict[str, Any]:
    """Vista 1: las series en un eje común y la correlación de cada pareja.

    Manda los valores CRUDOS y los normalizados a la vez. El navegador pinta los
    normalizados -que es lo único que permite superponer un 1-5 con una HRV en
    milisegundos- y enseña los crudos al tocar un punto. Mandar solo los
    normalizados obligaría a la PWA a deshacer la cuenta para enseñar el número
    de verdad, que es exactamente la cuenta que no puede hacer.
    """
    desde, hasta = _ventana(dias, hoy)
    cob = S.cobertura(session)

    crudas: dict[str, dict[date, float | None]] = {
        c: S.serie(session, c, desde, hasta, cob=cob) for c in _claves_de_la_vista()
    }

    salida_series = []
    for clave, valores in crudas.items():
        d = S.DEFINICIONES[clave]
        escala = S.normalizar(valores)
        puntos = [
            {
                "fecha": f.isoformat(),
                "valor": valores[f],
                "escala": escala[f],
            }
            for f in sorted(valores)
        ]
        salida_series.append(
            {
                **d.como_dict(),
                "n": sum(1 for v in valores.values() if v is not None),
                "puntos": puntos,
                # La tabla de las cuatro casillas, pegada a la serie que no se
                # puede leer sin ella. Va en TODAS las series con valor `None`
                # -y no solo en la que la tiene- por la misma razón que `_linea`
                # de la portada tiene siempre las mismas claves: en JavaScript,
                # una clave que falta no da error, da `undefined`, y de ahí sale
                # una rama elegida al revés que nadie ve hasta que la ve el
                # usuario.
                "tabla": (
                    tabla_discordancia(session, desde, hasta)
                    if clave == "discordancia"
                    else None
                ),
            }
        )

    salida_pares = []
    for par in PARES:
        pares = emparejar(crudas[par.x], crudas[par.y])
        res = correlacion(pares, metodo=metodo)
        salida_pares.append(
            {
                "x": par.x,
                "y": par.y,
                "etiqueta_x": S.DEFINICIONES[par.x].etiqueta,
                "etiqueta_y": S.DEFINICIONES[par.y].etiqueta,
                "titulo": par.titulo,
                "signo_esperado": par.signo_esperado,
                "lectura": _lectura(par, res),
                # El mismo `_al_reves` que las internas, y por el mismo motivo
                # que allí: es la única definición de "va por donde no debería".
                # Lo pide el gráfico de la vista, que pinta las siete barras de
                # azul o de naranja según esto. Deducirlo en el móvil comparando
                # el signo de `r` con `signo_esperado` daría otro criterio -sin
                # el umbral de 0.1- y la pantalla tendría dos: una barra naranja
                # al lado de una frase que dice que no contradice nada.
                "al_reves": _al_reves(par, res),
                **res.como_dict(),
            }
        )

    # Las internas reaprovechan las series que ya están leídas: las cinco de
    # Garmin están en `crudas` porque `_claves_de_la_vista` las mete siempre. Ni
    # una consulta más por el bloque entero.
    salida_internas = []
    for par in pares_internos():
        res = correlacion(emparejar(crudas[par.x], crudas[par.y]), metodo=metodo)
        salida_internas.append(
            {
                "x": par.x,
                "y": par.y,
                "etiqueta_x": S.GARMIN[par.x].etiqueta,
                "etiqueta_y": S.GARMIN[par.y].etiqueta,
                "titulo": par.titulo,
                "signo_esperado": par.signo_esperado,
                "lectura": _lectura_interna(par, res),
                "al_reves": _al_reves(par, res),
                "mismo_origen": MISMO_ORIGEN.get(frozenset((par.x, par.y))),
                **res.como_dict(),
            }
        )

    # Estas SÍ se corrigen, y las siete de arriba no, y la diferencia no es un
    # descuido. Las siete son hipótesis declaradas una a una, con su signo
    # esperado escrito de antemano: son siete preguntas, no siete intentos. Estas
    # diez son la rejilla COMPLETA de lo que se puede cruzar -nadie eligió cuáles
    # mirar, están todas porque existen todas-, y rastrear una rejilla a ver qué
    # sale es exactamente el caso para el que se escribió Benjamini-Hochberg.
    #
    # Se corrigen sobre sus diez y no sobre las diecisiete: meter las de
    # percepción en la tanda haría que la dureza de la corrección de las internas
    # dependiera de cuántas mañanas lleve contestadas, que no tiene nada que ver
    # con cuántas veces se ha mirado aquí.
    corregir_tanda([salida_internas])

    calculadas = [c for c in salida_internas if c["r"] is not None]
    compartidas = [c for c in salida_internas if c["mismo_origen"] is not None]
    independientes = [c for c in salida_internas if c["mismo_origen"] is None]
    return {
        "vista": "concordancia",
        "metodo": metodo,
        "ventana": {
            "desde": desde.isoformat(),
            "hasta": hasta.isoformat(),
            "dias": dias,
        },
        "cobertura": cob.como_dict(),
        "series": salida_series,
        "pares": salida_pares,
        "aviso_internas": AVISO_INTERNAS,
        "internas": salida_internas,
        # Los dos recuentos se hacen aquí porque la PWA no cuenta: son sumas
        # triviales, pero el día que una deje de serlo ya estaría escrita en
        # JavaScript.
        #
        # El de las siete declaradas es el más nuevo de los dos. No lo tenían
        # porque la vista las pintaba una a una y el lector sumaba con la vista;
        # ahora arriba hay un gráfico de siete barras y debajo UNA frase, y esa
        # frase necesita los números hechos.
        "resumen_pares": {
            "parejas": len(salida_pares),
            "calculadas": sum(1 for p in salida_pares if p["r"] is not None),
            # Los tres estados de `_al_reves`, contados por separado y sin
            # restar: `False` es "va por donde debía", `True` es "va al
            # contrario" y `None` es "no hay número, o lo hay y es tan pequeño
            # que su signo es una moneda al aire". Un recuento de dos casillas
            # obligaría a meter el tercero en alguna de las otras dos, y las dos
            # opciones mienten.
            "como_se_esperaba": sum(1 for p in salida_pares if p["al_reves"] is False),
            "al_reves": sum(1 for p in salida_pares if p["al_reves"] is True),
            "sin_signo_claro": sum(
                1 for p in salida_pares if p["al_reves"] is None and p["r"] is not None
            ),
        },
        # El reparto entre "comparten origen" e "independientes" es lo que
        # convierte este bloque en una lectura en vez de una tabla. Con las diez
        # juntas, "ocho de diez significativas" suena a que el reloj es
        # coherentísimo; abiertas en dos, se ve si esa coherencia está donde el
        # reloj calcula una métrica a partir de otra -en cuyo caso es su fórmula
        # asomando- o donde mide dos cosas de verdad distintas, que es el único
        # sitio donde puede estar diciendo algo del cuerpo.
        "resumen_internas": {
            "parejas": len(salida_internas),
            "calculadas": len(calculadas),
            "significativas": sum(1 for c in calculadas if c["significativa"]),
            # Cuenta el signo, no la significación: la barra hueca y la p van en
            # cada tarjeta. Aquí lo que se cuenta es cuántas relaciones no van
            # por donde deberían, que es una pregunta anterior a si aguantan.
            "en_sentido_contrario": sum(1 for c in salida_internas if c["al_reves"]),
            "comparten_origen": {
                "parejas": len(compartidas),
                "significativas": sum(1 for c in compartidas if c["significativa"]),
            },
            "independientes": {
                "parejas": len(independientes),
                "significativas": sum(1 for c in independientes if c["significativa"]),
            },
        },
    }


def vista_desfase(
    session: Session,
    *,
    dias: int = DIAS_POR_DEFECTO,
    hoy: date | None = None,
    metodo: str = "spearman",
    rango: range = RANGO_DESFASE,
) -> dict[str, Any]:
    """Vista 2: los siete deslizadores y las tres preguntas contra las cinco métricas.

    La rejilla entera, cincuenta casillas, sin filtrar por "las que salen bien".
    Una casilla que sale a cero también es una respuesta -ese deslizador y esa
    métrica no tienen nada que ver, ni a la vez ni con retraso- y esconderla
    dejaría la rejilla llena de las que sobrevivieron por azar, que con cincuenta
    intentos son unas cuantas. Por eso viaja también la `p` de cada una: la forma
    de no engañarse con esta tabla es mirarla entera.

    Eran treinta y cinco y ahora son cincuenta porque las dos preguntas de Sí/No
    y la discordancia entran por derecho: se pidió que se contaran en todas las
    correlaciones, y una respuesta que se guarda y no se cruza con nada es un
    campo de la base de datos, no una pregunta.

    Y ese aumento del 43 % en el número de casillas TIENE UN PRECIO, que se paga
    a sabiendas: con más intentos, más casillas cruzan el 0,05 por puro azar. El
    precio se paga aquí y no se disimula porque la alternativa -mirarlas sin
    meterlas en la tabla- sería el mismo número de intentos con la cuenta
    escondida.

    De las tres filas nuevas, dos son la misma pregunta: `discordancia` se
    calcula de las otras dos, así que si sale algo en ella conviene mirar cuál de
    las dos lo está trayendo antes de contarlo como un tercer hallazgo. Es el
    mismo aviso que `MISMO_ORIGEN` le pone a la nota de sueño y a los minutos.
    """
    desde, hasta = _ventana(dias, hoy)
    cob = S.cobertura(session)

    # Se piden con margen: un desfase de -3 empareja el día D del deslizador con
    # el D-3 de la métrica, y si la métrica solo se hubiera leído desde `desde`,
    # los tres primeros días del barrido perderían pares que SÍ existen en la
    # base. El margen es lo que hace que la n de los extremos baje por falta de
    # datos de verdad y no por el corte de la consulta.
    margen = max(abs(min(rango)), abs(max(rango)))
    # El eje de lo que se CONTESTA: los deslizadores y las preguntas. Las dos
    # listas se nombran por separado porque en `series.py` están separadas, y
    # están separadas para que nadie las junte por descuido en la lista que
    # decide qué puede mirar el semáforo.
    sliders = {
        c: S.serie(session, c, desde, hasta, cob=cob)
        for c in [*S.SLIDERS, *S.PREGUNTAS]
    }
    metricas = {
        c: S.serie(
            session, c, desde - timedelta(days=margen), hasta + timedelta(days=margen), cob=cob
        )
        for c in S.GARMIN
    }

    rejilla = []
    for cx, sx in sliders.items():
        for cy, sy in metricas.items():
            df = mejor_desfase(sx, sy, rango=rango, metodo=metodo)
            rejilla.append(
                {
                    "x": cx,
                    "y": cy,
                    "etiqueta_x": S.DEFINICIONES[cx].etiqueta,
                    "etiqueta_y": S.DEFINICIONES[cy].etiqueta,
                    **df.como_dict(),
                }
            )

    # DÓNDE PICAN LAS CINCUENTA, CONTADO AQUÍ.
    #
    # La vista contesta una sola pregunta -¿la percepción se adelanta al reloj o
    # va por detrás?- y la contesta con la forma del montón: si la masa de picos
    # cae a la derecha del cero, se adelanta. Eso es un histograma de siete
    # barras, y para dibujarlo hace falta contar cuántas casillas pican en cada
    # retardo.
    #
    # El recuento se hace aquí y no en el móvil por lo mismo que `dias_con_decision`
    # y `resumen_pares`: la PWA no calcula ninguna cifra que luego se lea, y un
    # `.filter().length` sobre la rejilla es una cifra que se lee. Además el
    # criterio de "esta casilla pica en -2" tiene que ser UNO: si el móvil lo
    # dedujera por su cuenta acabaría habiendo dos definiciones de pico, la de la
    # barra y la de la tarjeta de debajo, y el día que no coincidan nadie lo verá.
    #
    # `sin_pico` no se esconde ni se suma a los ceros. Una pareja sin bastantes
    # días no es una pareja que vaya a la vez: es una que no se ha podido mirar,
    # y meterla en la barra del cero engordaría justo la respuesta más cómoda.
    picos = [f["mejor_desfase"] for f in rejilla]
    reparto = {str(d): sum(1 for p in picos if p == d) for d in rango}
    return {
        "vista": "desfase",
        "metodo": metodo,
        "rango_desfase": [min(rango), max(rango)],
        "ventana": {
            "desde": desde.isoformat(),
            "hasta": hasta.isoformat(),
            "dias": dias,
        },
        "cobertura": cob.como_dict(),
        "convenio": (
            "desfase positivo = la percepción se adelanta al reloj; "
            "negativo = va por detrás"
        ),
        "reparto_desfases": {
            "parejas": len(rejilla),
            "con_pico": sum(1 for p in picos if p is not None),
            "sin_pico": sum(1 for p in picos if p is None),
            "por_desfase": reparto,
            "se_adelanta": sum(1 for p in picos if p is not None and p > 0),
            "a_la_vez": sum(1 for p in picos if p == 0),
            "va_detras": sum(1 for p in picos if p is not None and p < 0),
        },
        "rejilla": rejilla,
    }
