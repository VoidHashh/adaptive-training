"""La portada del panel: lo que ya se sabe, antes del formulario de elegir.

POR QUÉ EXISTE ESTE MÓDULO
--------------------------
El panel de métricas tenía cinco vistas y las cinco eran el volcado fiel de un
módulo de `app/analysis/`. Abrirlo te enseñaba el APARATO DE MEDIR -un
desplegable de variables, una tabla de coeficientes- y no la medida. La primera
pantalla pedía elegir antes de contar nada, porque el formulario era en realidad
el índice del código.

Medido el 2026-09-13 sobre los datos reales: había 27 relaciones fiables
calculadas, serializadas y enviadas al navegador, y la pantalla de entrada no
enseñaba ninguna. Las cuatro más fuertes -carga, minutos y desnivel contra la
variabilidad y el Body Battery- ni siquiera tenían frase, porque la capa de
lenguaje solo cubría las exposiciones de sí-o-no.

Este módulo invierte quién pregunta. En vez de "elige dos variables y te digo su
correlación", dice "esto es lo que sé de ti, y esto es lo que todavía no puedo
saber y por qué".

LAS DOS MITADES, Y LA SEGUNDA NO ES DE RELLENO
----------------------------------------------
`lo_que_se_sabe` y `lo_que_no_se_puede_saber` tienen el mismo rango a propósito.
Una vista vacía que no explica su vacío es un fallo silencioso de interfaz:
quien la abre no puede distinguir "aquí no pasa nada" de "aquí falta un dato que
nadie está trayendo", y las dos cosas se leen igual de bien. Es exactamente el
criterio que ya gobierna el motor -`sin_muestra`, `skipped` frente a
`not_fired`, el `na` de cada casilla- aplicado por fin a la pantalla.

El día que se escribe esto, dos de los tres bloques de la portada están vacíos y
lo dicen con todas las letras. Eso es el comportamiento correcto, no un estado
transitorio que haya que disimular.

DÓNDE VIVE LA PROSA
-------------------
Aquí, en el servidor, y nunca en `static/`. Las bandas de |r| y la agrupación de
exposiciones salen de `config.yaml`; las frases de cada serie salen de
`Definicion.en_frase`. Si esto estuviera en JavaScript, añadir una exposición
nueva al análisis la dejaría fuera de la portada sin un solo error -se
calcularía, se enviaría por la red y no la vería nadie-, que es palabra por
palabra el fallo del desplegable de Impacto que dio origen a este rediseño.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from statistics import fmean
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analysis import series as S
from app.analysis.impacto import vista_impacto
from app.analysis.preguntas import tabla_discordancia
from app.analysis.stats import percentil_de
from app.analysis.texto import cuantos, plural
from app.engine.luces import LUCES as _LUCES
from app.models import Activity, Checkin, Decision, WorkoutLog

# Cuántos días mira "cómo voy". Una semana: es el tramo más corto que tiene
# sentido comparar contra un histórico -un día suelto es ruido- y el más largo
# que todavía describe CÓMO VAS y no cómo ibas.
DIAS_RECIENTES = 7

# Días con dato que hacen falta en esa semana para decir algo. Con tres de siete
# la media ya no es de la semana: es de tres días que resultaron tener reloj.
MINIMO_RECIENTES = 4


# LOS RECUENTOS VAN POR SEMANA DE CALENDARIO, Y LAS MEDIAS NO (25/09/2026).
#
# Las líneas de fuerza y bici y el bloque «qué ha cambiado» contaban los
# últimos `DIAS_RECIENTES` días y lo llamaban «esta semana». Un viernes eso es
# del sábado anterior a hoy, y el usuario lo leyó como es natural leerlo: «1
# salida esta semana» cuando desde el lunes no había salido -la salida era del
# domingo-. El número estaba bien; la frase decía otra cosa.
#
# Se arregla la cuenta y no la frase, porque «esta semana» es lo que se quiere
# saber: desde el lunes. Y la anterior se corta EN EL MISMO DÍA, para que un
# martes no se compare dos días contra siete y salga siempre «menos».
#
# Las medias de bienestar siguen en ventana móvil y ya lo dicen: «Tus últimos 7
# días». Una media de dos días un martes no sería una media de nada.
def _semana_en_curso(hoy: date) -> tuple[date, date]:
    """Del lunes de esta semana a hoy, los dos incluidos."""
    return hoy - timedelta(days=hoy.weekday()), hoy


def _la_anterior_hasta_el_mismo_dia(hoy: date) -> tuple[date, date]:
    """Del lunes de la semana pasada al mismo día de la semana que hoy."""
    lunes, _ = _semana_en_curso(hoy)
    return lunes - timedelta(days=7), hoy - timedelta(days=7)

# Cuántas ventanas de referencia hacen falta para situar la semana. Por debajo
# de esto el percentil no significa nada: "estás en el percentil 30" sobre seis
# ventanas es "hay dos peores que esta".
MINIMO_REFERENCIA = 20


# ---------------------------------------------------------------------------
# La configuración del lenguaje
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Grupo:
    """Un grupo de exposiciones que son la misma DECISIÓN.

    No agrupa por parecido temático sino por lo que el usuario puede mover. Y no
    es una comodidad de presentación: medido sobre sus 173 días, `minutos_bici`
    y `desnivel_bici` correlacionan a 0,9986 entre ellas. Enseñar "los minutos
    te bajan la variabilidad" y "el desnivel te baja la variabilidad" como dos
    hallazgos sugiere dos pruebas independientes donde hay una sola, y eso no es
    repetitivo: es una exageración de la evidencia.
    """

    clave: str
    titulo: str
    decision: str
    exposiciones: frozenset[str]
    familias: frozenset[str]


@dataclass(frozen=True)
class Lenguaje:
    """Lo que hace falta para contar un cálculo en castellano, sacado del YAML."""

    hallazgos_en_portada: int
    se_nota_poco: float
    se_nota: float
    se_nota_mucho: float
    grupos: tuple[Grupo, ...]

    def banda(self, r: float) -> str | None:
        """La palabra que sustituye al coeficiente. `None` si no llega a contar.

        El número no desaparece: viaja al lado, en el mismo hallazgo. Lo que
        hace la banda es PRECEDERLO, para que la primera lectura sea una frase y
        no una cifra que hay que saber interpretar.
        """
        a = abs(r)
        if a >= self.se_nota_mucho:
            return "se nota mucho"
        if a >= self.se_nota:
            return "se nota"
        if a >= self.se_nota_poco:
            return "se nota poco"
        return None


def lenguaje_de(cfg: Any) -> Lenguaje | None:
    """Lee `metrics` del config. `None` si la sección no está.

    Que sea opcional no es dejadez: el panel funcionaba antes de existir la
    portada y tiene que poder seguir funcionando sin ella. Lo que NO se admite
    es una sección a medias, y de eso se encarga el validador de
    `config_loader.py`, que la revisa entera si está.
    """
    raw = cfg.raw if hasattr(cfg, "raw") else (cfg or {})
    m = (raw or {}).get("metrics")
    if not m:
        return None
    bandas = m.get("fuerza_relacion") or {}
    grupos = tuple(
        Grupo(
            clave=str(g.get("clave")),
            titulo=str(g.get("titulo")),
            decision=str(g.get("decision")),
            exposiciones=frozenset(str(v) for v in (g.get("exposiciones") or [])),
            familias=frozenset(str(v) for v in (g.get("familias") or [])),
        )
        for g in (m.get("grupos_exposicion") or [])
    )
    return Lenguaje(
        hallazgos_en_portada=int(m.get("hallazgos_en_portada")),
        se_nota_poco=float(bandas.get("se_nota_poco")),
        se_nota=float(bandas.get("se_nota")),
        se_nota_mucho=float(bandas.get("se_nota_mucho")),
        grupos=grupos,
    )


def grupo_de(exposicion: dict[str, Any], lenguaje: Lenguaje) -> Grupo:
    """A qué grupo pertenece una exposición. REVIENTA si no pertenece a ninguno.

    El error duro es el punto entero de la función. Una exposición huérfana no
    daría ningún fallo: se calcularía como siempre, viajaría en la rejilla como
    siempre, y sencillamente no aparecería nunca en la portada. Nadie echa de
    menos un hallazgo que no sabe que existe.

    Es literalmente el fallo que originó este rediseño -45 correlaciones
    calculadas y descartadas en la última línea del JavaScript- así que la
    pieza que lo sustituye no puede poder repetirlo.

    Nombrarla explícitamente gana a reclamar su familia. Así `bici_intensa` cae
    en "apretar" aunque "salir en bici" también sea de familia `bici`.
    """
    clave = str(exposicion.get("clave"))
    for g in lenguaje.grupos:
        if clave in g.exposiciones:
            return g
    familia = str(exposicion.get("familia"))
    for g in lenguaje.grupos:
        if familia in g.familias:
            return g
    raise ValueError(
        f"la exposición '{clave}' (familia '{familia}') no cae en ningún grupo de "
        f"`metrics.grupos_exposicion`. Sin grupo no puede salir en la portada, y "
        f"quedarse fuera en silencio es justo lo que esta portada existe para "
        f"impedir: añádela a un grupo existente o crea uno nuevo en config.yaml"
    )


# ---------------------------------------------------------------------------
# Bloque 3: lo que ya se sabe
# ---------------------------------------------------------------------------


def _mejor_casilla(fila: dict[str, Any]) -> dict[str, Any] | None:
    """De los tres retardos de una celda, el que más dice. Solo si es fiable.

    "Fiable" es las dos cosas a la vez: que haya pasado el filtro de azar de
    Benjamini-Hochberg Y que el efecto sea de un tamaño que se pueda notar. Con
    173 días, un |r| de 0,15 sale significativo sin despeinarse y no significa
    nada que cambie ninguna decisión. Contarlo sería verdad estadística y
    mentira práctica, y la portada se lee como si fuera práctica.

    El corte de tamaño lo pone el que llama, con la banda del config.
    """
    candidatas = [
        c
        for c in fila.get("por_dia") or []
        if c.get("r") is not None and c.get("significativa")
    ]
    if not candidatas:
        return None
    return max(candidatas, key=lambda c: abs(c["r"]))


def _mayuscula(frase: str) -> str:
    """La primera en mayúscula y el resto INTACTO.

    `str.capitalize()` no vale: baja todo lo demás, y «hacer el Día 1» se
    convertía en «Hacer el día 1». La clave del config es `dia_1` y la
    ortografía se la pone `_rutina_en_frase`; deshacerla aquí sería tirar ese
    trabajo en la última línea.
    """
    return frase[:1].upper() + frase[1:] if frase else frase


def _verbo(sube: bool, plural: bool) -> str:
    raiz = "sube" if sube else "baja"
    return raiz + "n" if plural else raiz


def hallazgos(
    rejilla: list[dict[str, Any]], lenguaje: Lenguaje
) -> list[dict[str, Any]]:
    """Lo que se sabe, en frases, agrupado por decisión y ordenado por fuerza.

    La unidad de un hallazgo es (GRUPO x RESPUESTA), no (exposición x
    respuesta). Dentro del par se coge la relación más fuerte y las demás del
    mismo grupo se reparten según SU SIGNO.

    Y ese `confirmada_por` NO es una nota al pie de cortesía: es la diferencia
    entre "lo mismo sale por otros tres caminos" -información- y cuatro
    hallazgos separados que parecen cuatro pruebas -exageración-. Con r = 0,9986
    entre los minutos y el desnivel, los otros caminos no son independientes, y
    la portada tiene que poder decir eso sin esconderlo ni inflarlo.

    EL SIGNO SE COMPRUEBA, y esto costó un fallo real el 2026-09-13. La primera
    versión metía en `confirmada_por` a toda compañera del grupo sin mirar hacia
    dónde apuntaba, y la portada salió diciendo que «las salidas medias»
    confirmaban que las intensas bajan la variabilidad. Era falso: la casilla
    más fuerte de las medias es un +0,25 a tres días, del signo contrario. Una
    frase inventada en la pantalla de entrada, construida a partir de números
    todos correctos, sin un solo error por ningún lado.

    Las del signo contrario no se tiran: van en `discrepa`. Tirarlas sería
    volver a hacer lo mismo de siempre -calcular algo, no enseñarlo y que nadie
    pueda echarlo de menos- solo que ahora con la excusa de que estropea el
    titular. Que dos caras de la misma decisión apunten al revés es de las cosas
    más informativas que puede haber en esta pantalla.
    """
    # Primero se juntan TODAS las candidatas de cada par, y solo después se
    # elige. Ordenar mientras se acumula -ir arrastrando la mejor y empujando la
    # perdedora a una lista de compañeras- da el mismo resultado con el doble de
    # estados intermedios que comprobar, y este es el sitio donde una compañera
    # perdida se convierte en un hallazgo que aparenta más solidez de la que
    # tiene. Se prefiere la versión aburrida.
    por_par: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for fila in rejilla:
        casilla = _mejor_casilla(fila)
        if casilla is None:
            continue
        if lenguaje.banda(casilla["r"]) is None:
            continue
        grupo = grupo_de(fila["exposicion"], lenguaje)
        llave = (grupo.clave, fila["respuesta"]["clave"])
        por_par.setdefault(llave, []).append(
            {"fila": fila, "casilla": casilla, "grupo": grupo}
        )

    salida = []
    for candidatas in por_par.values():
        candidatas.sort(key=lambda c: -abs(c["casilla"]["r"]))
        m, resto = candidatas[0], candidatas[1:]
        fila, casilla, grupo = m["fila"], m["casilla"], m["grupo"]
        exp, resp = fila["exposicion"], fila["respuesta"]
        sube = casilla["r"] > 0
        # El signo del coeficiente dice si suben juntos; `sentido` dice si eso
        # es bueno o malo. Sin las dos cosas no hay frase posible: un -0,4 entre
        # salir y la variabilidad es malo, y entre salir y el pulso en reposo
        # sería bueno.
        if resp.get("sentido") == "alto_peor":
            valencia = "peor" if sube else "mejor"
        elif resp.get("sentido") == "alto_mejor":
            valencia = "mejor" if sube else "peor"
        else:
            valencia = "neutro"

        companeras = [
            c["fila"]["exposicion"]["en_frase"]
            for c in resto
            if c["fila"]["exposicion"].get("en_frase")
            and (c["casilla"]["r"] > 0) == sube
        ]
        discrepan = [
            {
                "en_frase": c["fila"]["exposicion"]["en_frase"],
                "plural": bool(c["fila"]["exposicion"].get("plural")),
                "r": c["casilla"]["r"],
                "dias_despues": c["casilla"]["dias_despues"],
            }
            for c in resto
            if c["fila"]["exposicion"].get("en_frase")
            and (c["casilla"]["r"] > 0) != sube
        ]
        salida.append(
            {
                "grupo": {
                    "clave": grupo.clave,
                    "titulo": grupo.titulo,
                    "decision": grupo.decision,
                },
                # El verbo concuerda con la exposición porque es el sujeto:
                # «salir en bici te BAJA la variabilidad», «las salidas largas
                # te SUBEN el pulso». Con `.capitalize()` a secas se comía las
                # mayúsculas de dentro -«el Día 1» acababa en «el día 1»- así
                # que solo se toca la primera letra.
                "frase": (
                    f"{_mayuscula(exp['en_frase'])} te "
                    f"{_verbo(sube, exp.get('plural', False))} {resp['en_frase']} "
                    f"al día siguiente"
                ),
                "valencia": valencia,
                "matiz": fila.get("lectura"),
                "fuerza": lenguaje.banda(casilla["r"]),
                "confirmada_por": companeras,
                "nota_confirmacion": (
                    _nota_confirmacion(companeras) if companeras else None
                ),
                "discrepa": discrepan,
                "nota_discrepancia": (
                    _nota_discrepancia(discrepan, resp) if discrepan else None
                ),
                "exposicion": exp,
                "respuesta": resp,
                # La ficha. El número no se esconde, se subordina: va aquí
                # entero -con su p, su p corregida y su n- para que la banda de
                # arriba lo PRECEDA en vez de sustituirlo.
                "ficha": {
                    "dias_despues": casilla["dias_despues"],
                    "r": casilla["r"],
                    "p": casilla.get("p"),
                    "p_corregida": casilla.get("p_corregida"),
                    "n": casilla.get("n"),
                    "metodo": casilla.get("metodo"),
                    "desde": casilla.get("desde"),
                    "hasta": casilla.get("hasta"),
                    "descartados": casilla.get("descartados"),
                },
                "dias_de_datos": casilla.get("n"),
            }
        )

    salida.sort(key=lambda h: -abs(h["ficha"]["r"]))
    return salida


def _nota_confirmacion(companeras: list[str]) -> str:
    """Qué significa que lo mismo salga por varios caminos del mismo grupo.

    Se dice que son "la misma cosa medida de otra manera" y no "otras pruebas",
    porque en este histórico lo primero es literalmente cierto y lo segundo
    sería falso. Ver la medida en el comentario de `grupos_exposicion`.
    """
    if len(companeras) == 1:
        return f"Lo mismo sale con {companeras[0]}, que aquí es la misma cosa medida de otra manera."
    return (
        "Lo mismo sale con "
        + ", ".join(companeras[:-1])
        + f" y {companeras[-1]}, que aquí son la misma cosa medida de otras maneras."
    )


def _nota_discrepancia(
    discrepan: list[dict[str, Any]], resp: dict[str, Any]
) -> str:
    """Que otra cara de la misma decisión apunte al revés. Se dice, no se tapa.

    Se dice SIN resolverla, que es lo honesto: con estos datos no se puede saber
    si las salidas medias de verdad hacen algo distinto de las intensas o si es
    que hay pocas y el número baila. Lo que sí se puede saber es que el titular
    no es toda la historia, y eso cabe en una línea.
    """
    nombres = [d["en_frase"] for d in discrepan]
    quien = (
        nombres[0]
        if len(nombres) == 1
        else ", ".join(nombres[:-1]) + f" y {nombres[-1]}"
    )
    # Dos cosas hacen plural el verbo: que haya más de una compañera, o que la
    # única que hay YA sea plural. Contar solo lo primero daba «las salidas
    # medias apunta al revés», que es el mismo despiste de concordancia que
    # `Exposicion.plural` existe para impedir, colado por la puerta de al lado.
    varios = len(discrepan) > 1 or discrepan[0]["plural"]
    verbo = "apuntan" if varios else "apunta"
    return (
        f"Ojo: dentro de esta misma decisión, {quien} {verbo} al revés sobre "
        f"{resp['en_frase']}. No es bastante para saber por qué."
    )


NOTA_AZAR = (
    "Miro más de cien relaciones a la vez. Con tantas, unas cuantas salen "
    "fuertes por puro azar. Lo que se cuenta aquí ya tiene descontado ese azar."
)


# ---------------------------------------------------------------------------
# Bloque 1: cómo voy
# ---------------------------------------------------------------------------


def _ventanas(valores: list[float], ancho: int) -> list[float]:
    """Todas las medias de `ancho` valores consecutivos de la lista."""
    if len(valores) < ancho:
        return []
    return [fmean(valores[i : i + ancho]) for i in range(len(valores) - ancho + 1)]


def _nivel(pct: float) -> tuple[str, str]:
    """Del percentil a una palabra. Devuelve (nivel, frase sin valencia).

    Cinco escalones y no tres porque el de en medio tiene que ser ancho: la
    mayoría de las semanas son normales, y una portada que llama "por debajo de
    lo tuyo" a cualquier cosa bajo la mediana estaría avisando siempre. Un aviso
    que salta siempre no se lee.
    """
    if pct < 15:
        return "bajo", "por debajo de lo tuyo"
    if pct < 35:
        return "algo_bajo", "un poco por debajo de lo tuyo"
    if pct <= 65:
        return "normal", "en tu rango de siempre"
    if pct <= 85:
        return "algo_alto", "un poco por encima de lo tuyo"
    return "alto", "por encima de lo tuyo"


def _linea(
    *,
    clave: str,
    etiqueta: str,
    lectura: str | None = None,
    na: str | None = None,
    nivel: str | None = None,
    valencia: str | None = None,
    media: float | None = None,
    unidad: str | None = None,
    percentil: float | None = None,
    n_reciente: int | None = None,
    n_referencia: int | None = None,
    tabla: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Una línea de «Cómo voy», SIEMPRE con las mismas doce claves.

    Antes había cinco formas distintas de esta fila esparcidas por el módulo, y
    las cinco eran correctas por separado: la de Garmin con percentil llevaba
    `media`, `unidad` y `percentil`; la de Garmin sin datos suficientes no; la de
    bici y la de fuerza tampoco, y encima ni siquiera traían `n_referencia`.
    Cada una decía la verdad sobre sí misma.

    El problema es lo que eso obliga a hacer al que las lee. En JavaScript
    `l.media` sobre la fila de la bici no da ningún error: da `undefined`, y de
    ahí salen los dos finales de siempre -«—» impreso en medio de una frase, o
    una rama elegida al revés sin dejar rastro-. El renderizador lo cazó el
    andamio de Node a la primera, pero lo habría cazado igual de bien un móvil a
    las siete de la mañana, y allí no hay quien lo lea.

    Así que la ausencia de una clave deja de significar nada, porque no hay
    ausencias: `None` es «esta línea no tiene media», que es una afirmación, y la
    hace el servidor, que es quien lo sabe. Es la misma regla que `_lista` en
    `encabezados.py` mirada desde el otro lado: allí la clave que falta revienta,
    aquí no puede faltar.

    `unidad` LLEVA UN SUFIJO, NO UN RANGO. La clave se sigue llamando igual
    porque el nombre siempre fue el correcto; lo que estaba mal era lo que se
    metía dentro. Aquí llegaba `Definicion.unidad` tal cual, y ese campo vale
    "ms" para la HRV pero "0-100" para la nota de sueño, así que la portada
    escribía «85,50 0-100» en la primera pantalla, encima del número que más se
    mira. Ahora llega `Definicion.sufijo`, que es `None` cuando lo que había era
    un rango: la nota de sueño sale como «85,50», y la escala -que sigue entera
    en `unidad` y en `rango`- se queda donde sirve, que es en los ejes.

    `tabla` es la duodécima, y nace `None` en once de las doce líneas. Solo la
    lleva la discordancia, porque es la única de estas cifras que MIENTE leída
    sola: un 30 % de días discordantes no dice si fue que te apetecía y no
    fuiste o al revés. Y se añade a la firma -en vez de colgarla solo donde
    hace falta- por lo que dicen los tres párrafos de arriba: la clave que falta
    no se distingue de la clave que vale `None`, y el que lo lee es un navegador
    a las siete de la mañana.
    """
    return {
        "clave": clave,
        "etiqueta": etiqueta,
        "nivel": nivel,
        "valencia": valencia,
        "lectura": lectura,
        "na": na,
        "media": media,
        "unidad": unidad,
        "percentil": percentil,
        "n_reciente": n_reciente,
        "n_referencia": n_referencia,
        "tabla": tabla,
    }


def _linea_de_serie(
    d: S.Definicion,
    serie: dict[date, float | None],
    *,
    corte: date,
    escala: float = 1.0,
    unidad: str | None = None,
    decimales: int = 1,
    tabla: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Una serie cualquiera situada dentro de su propio histórico.

    Era el cuerpo del bucle de `como_voy`, escrito una sola vez para las cinco
    métricas del reloj. Se saca fuera porque ahora lo usan también las tres
    preguntas, y copiarlo habría dejado dos sitios donde decidir qué es "poca
    muestra": el día que uno de los dos cambiara, la portada tendría dos
    criterios distintos para la misma frase y ninguno de los dos sería el
    equivocado a simple vista.

    `tabla` se pasa TAL CUAL por las tres salidas, incluidas las dos de «no hay
    bastante muestra». Habla de la ventana entera y no de la última semana, así
    que una semana floja no es motivo para esconderla: al contrario, es cuando
    más falta hace saber de qué lado cayeron los días que sí hubo.

    `escala` y `unidad` son lo único que las preguntas necesitan distinto, y son
    presentación pura: multiplican el número que se enseña y nada más. La cuenta
    -la media, el percentil, el nivel- es idéntica, que es justamente lo que
    había que conservar.
    """
    recientes = [v for f, v in sorted(serie.items()) if f >= corte and v is not None]
    previos = [v for f, v in sorted(serie.items()) if f < corte and v is not None]
    sufijo = unidad if unidad is not None else d.sufijo

    if len(recientes) < MINIMO_RECIENTES:
        return _linea(
            clave=d.clave,
            etiqueta=d.etiqueta,
            na=(
                f"solo {len(recientes)} de los últimos {DIAS_RECIENTES} días "
                f"traen este dato; hacen falta {MINIMO_RECIENTES} para que la "
                f"media sea de la semana y no de los días sueltos que hubo"
            ),
            unidad=sufijo,
            n_reciente=len(recientes),
            n_referencia=len(previos),
            tabla=tabla,
        )

    referencia = _ventanas(previos, min(DIAS_RECIENTES, len(recientes)))
    if len(referencia) < MINIMO_REFERENCIA:
        return _linea(
            clave=d.clave,
            etiqueta=d.etiqueta,
            na=(
                f"hay {len(referencia)} semanas anteriores con las que "
                f"comparar y hacen falta {MINIMO_REFERENCIA}: con menos, "
                f"decir si esta semana es alta o baja sería inventárselo"
            ),
            unidad=sufijo,
            n_reciente=len(recientes),
            n_referencia=len(referencia),
            tabla=tabla,
        )

    media = fmean(recientes)
    pct = percentil_de(media, referencia)
    nivel, frase = _nivel(pct or 0.0)
    if nivel == "normal":
        valencia = "normal"
    elif d.sentido == "alto_mejor":
        valencia = "peor" if nivel.endswith("bajo") else "mejor"
    elif d.sentido == "alto_peor":
        valencia = "mejor" if nivel.endswith("bajo") else "peor"
    else:
        valencia = "neutro"

    return _linea(
        clave=d.clave,
        etiqueta=d.etiqueta,
        nivel=nivel,
        valencia=valencia,
        lectura=frase,
        media=round(media * escala, decimales),
        unidad=sufijo,
        percentil=round(pct, 0) if pct is not None else None,
        n_reciente=len(recientes),
        n_referencia=len(referencia),
        tabla=tabla,
    )


def como_voy(
    session: Session, *, hoy: date, dias: int, cob: S.Cobertura
) -> dict[str, Any]:
    """La última semana frente al propio histórico, nunca frente a constantes.

    LAS DOS POBLACIONES SON DISJUNTAS, y eso ya costó caro una vez en
    `engine/tendencia.py`: la semana reciente NO entra en su propia referencia.
    Metida dentro, una mala semana se sube ella sola el listón y se tapa; medido
    allí, el solapamiento amortiguaba la señal alrededor de un tercio.

    Y se compara una media de siete días contra OTRAS MEDIAS de siete días, no
    contra los días sueltos. Una media es menos variable que un dato aislado:
    situarla en la distribución de días sueltos la mandaría siempre al centro y
    la portada diría "normal" pasara lo que pasara.
    """
    desde = hoy - timedelta(days=dias - 1)
    corte = hoy - timedelta(days=DIAS_RECIENTES - 1)
    lineas: list[dict[str, Any]] = []

    for clave, d in S.GARMIN.items():
        lineas.append(
            _linea_de_serie(
                d, S.serie(session, clave, desde, hoy, cob=cob), corte=corte
            )
        )

    # Las tres del formulario que se contestan con un Sí o un No.
    #
    # Aquí es donde «se cuentan en la tendencia» deja de ser una frase. La media
    # de una serie de ceros y unos es una PROPORCIÓN, así que la línea dice «has
    # dicho que sí el 43 % de los últimos siete días» y el percentil la sitúa
    # contra todas tus semanas anteriores. Eso responde a la única pregunta que
    # justifica contestarlas cada mañana durante meses: ¿me está bajando el
    # apetito, o es que esta semana ha sido rara?
    #
    # Los deslizadores NO están en esta lista, y no es un olvido: no lo estaban
    # antes de existir las preguntas y meterlos ahora sería otra decisión, de
    # otro día, con su propio motivo. Lo que se pidió es que las respuestas de
    # Sí/No se contaran en la tendencia, y se cuentan.
    tabla = tabla_discordancia(session, desde, hoy)
    for clave, d in S.PREGUNTAS.items():
        lineas.append(
            _linea_de_serie(
                d,
                S.serie(session, clave, desde, hoy, cob=cob),
                corte=corte,
                # Solo la discordancia. Las dos preguntas crudas se leen solas
                # -«has dicho que sí el 43 % de los días» no tiene doble
                # lectura-; la que sale de restarlas, no.
                tabla=tabla if clave == "discordancia" else None,
                # De proporción a porcentaje, y con el "%" puesto. Sin esto la
                # portada imprimiría «0,4» encima de la palabra "apetece", que
                # no significa nada a las siete de la mañana. `d.sufijo` no
                # sirve aquí porque la unidad declarada es "0-1", que es un
                # rango -bueno para un eje- y no un sufijo.
                escala=100.0,
                unidad="%",
                decimales=0,
            )
        )

    lineas.append(_linea_bici(session, hoy=hoy))
    lineas.append(_linea_fuerza(session, hoy=hoy))

    con_algo = [ln for ln in lineas if ln.get("lectura")]
    # CUÁNTAS SEÑALES VAN MEJOR Y CUÁNTAS PEOR, contado aquí.
    #
    # Ocho líneas que dicen "por debajo de lo tuyo" una detrás de otra son ocho
    # lecturas que hay que sumar con la cabeza para saber si la semana va bien o
    # mal. Esa suma es un quesito de tres trozos, y los tres números salen de
    # aquí porque en la PWA no se cuenta nada que luego se lea.
    #
    # Solo entran las líneas que COMPARAN con el histórico, que son las que
    # tienen `nivel`. Bici y fuerza traen `valencia` "neutro" y no comparan nada
    # -son recuentos de la semana-, así que meterlas engordaría el trozo de "como
    # siempre" con dos señales que nunca van a decir otra cosa.
    comparadas = [ln for ln in lineas if ln.get("nivel")]
    resumen = {
        "senales": len(comparadas),
        "mejor": sum(1 for ln in comparadas if ln["valencia"] == "mejor"),
        "peor": sum(1 for ln in comparadas if ln["valencia"] == "peor"),
        "normal": sum(
            1 for ln in comparadas if ln["valencia"] in ("normal", "neutro")
        ),
    }
    return {
        "titulo": "Cómo voy",
        "resumen": resumen,
        # «frente a tus últimos 180» obligaba a adivinar 180 qué. Ahora dice lo
        # que es la referencia -lo normal para ti- y cuánto pasado la forma,
        # que desde el 25/09/2026 es lo único que dice cuánto se mira: la
        # portada ya no tiene selector de ventana.
        "subtitulo": (
            f"Tus últimos {DIAS_RECIENTES} días, frente a lo que es normal para "
            f"ti en los últimos {dias}."
        ),
        "estado": "con_datos" if con_algo else "vacio",
        "na": (
            None
            if con_algo
            else (
                "todavía no hay bastante histórico para situar esta semana dentro "
                "de lo tuyo; hacen falta unas semanas más de reloj"
            )
        ),
        "lineas": lineas,
    }


def _hace(dias: int) -> str:
    """Cuánto hace, en castellano. Una sola copia para bici y para fuerza."""
    if dias == 0:
        return "hoy"
    if dias == 1:
        return "ayer"
    if dias == 2:
        return "anteayer"
    return f"hace {dias} días"


def _linea_bici(session: Session, *, hoy: date) -> dict[str, Any]:
    """Salidas de la semana -desde el lunes- y cuánto hace de la última."""
    desde, _ = _semana_en_curso(hoy)
    dias_con_salida = set(
        session.scalars(
            select(Activity.date).where(Activity.date >= desde, Activity.date <= hoy)
        )
    )
    ultima = session.scalar(select(func.max(Activity.date)))
    if ultima is None:
        return _linea(
            clave="bici",
            etiqueta="Bici",
            na="no hay ninguna salida registrada todavía",
            n_reciente=0,
        )
    ultima = S.a_fecha(ultima)
    hace = (hoy - ultima).days
    n = len({S.a_fecha(d) for d in dias_con_salida})
    cuando = _hace(hace)
    return _linea(
        clave="bici",
        etiqueta="Bici",
        valencia="neutro",
        lectura=(
            f"{n} salida{'s' if n != 1 else ''} esta semana, la última {cuando}"
            if n
            else f"ninguna salida esta semana, la última {cuando}"
        ),
        n_reciente=n,
    )


def _linea_fuerza(session: Session, *, hoy: date) -> dict[str, Any]:
    """Sesiones de fuerza de la semana, según lo que el sistema haya APUNTADO.

    Distingue dos vacíos que no son el mismo y que sin esto se leerían igual:
    "no has entrenado esta semana" y "el sistema no ha apuntado nunca nada". El
    segundo no habla del usuario, habla del sistema, y confundirlos sería
    exactamente el reproche sin fundamento que esta portada no puede permitirse.
    """
    desde, _ = _semana_en_curso(hoy)
    total = session.scalar(select(func.count()).select_from(WorkoutLog)) or 0
    if not total:
        return _linea(
            clave="fuerza",
            etiqueta="Fuerza",
            na="el sistema todavía no ha apuntado ninguna sesión de fuerza",
            n_reciente=0,
        )
    # DÍAS con entreno, no filas de `workout_log`, y con la misma función que
    # «qué ha cambiado». Contaba filas, y el 25/09/2026 la misma pantalla decía
    # «4 sesiones esta semana» arriba y «3 esta semana» abajo: el Día 2 del 22
    # y su HIIT eran dos entrenos en Hevy y una sola sesión para quien los hizo.
    n = _cuenta_dias(session, WorkoutLog, WorkoutLog.date, desde, hoy)
    ultima = session.scalar(select(func.max(WorkoutLog.date)))
    hace = (hoy - S.a_fecha(ultima)).days if ultima is not None else None
    cola = "" if hace is None else f", la última {_hace(hace)}"
    # Las dos formas se escriben enteras porque «sesión» + «es» da «sesiónes»:
    # en castellano el plural mueve la tilde. El motivo entero está en
    # `app/analysis/texto.py`, que es donde vive ya esta regla.
    cuantas = cuantos(n, "sesión", "sesiones")
    return _linea(
        clave="fuerza",
        etiqueta="Fuerza",
        valencia="neutro",
        lectura=f"{cuantas} esta semana{cola}",
        n_reciente=n,
    )


# ---------------------------------------------------------------------------
# Bloque 2: qué ha cambiado
# ---------------------------------------------------------------------------


# Lo que se lee encima del bloque. Dice las dos fronteras porque ninguna es la
# que se supondría sin decirla: la semana empieza el lunes, y la anterior se
# corta en el mismo día.
SUBTITULO_CAMBIOS = "Desde el lunes, frente a los mismos días de la semana pasada."


def que_ha_cambiado(session: Session, *, hoy: date) -> dict[str, Any]:
    """Esta semana contra la anterior. Dos ventanas DISJUNTAS, otra vez.

    Hoy está vacío entero y dice por qué. No es un placeholder: es la respuesta
    correcta mientras el motor no lleve dos semanas guardando decisiones. Un
    bloque que se inventara un "sin cambios" con cero datos estaría diciendo que
    ha mirado, y no ha mirado.
    """
    ini_esta, _ = _semana_en_curso(hoy)
    ini_previa, fin_previa = _la_anterior_hasta_el_mismo_dia(hoy)
    lineas: list[dict[str, Any]] = []

    colores_esta = _colores(session, ini_esta, hoy)
    colores_previa = _colores(session, ini_previa, fin_previa)
    if sum(colores_esta.values()) or sum(colores_previa.values()):
        lineas.append(
            {
                "clave": "semaforo",
                "etiqueta": "El semáforo",
                "lectura": _lectura_semaforo(colores_esta, colores_previa),
                "esta_semana": colores_esta,
                "semana_anterior": colores_previa,
                # El total de días con decisión, sumado AQUÍ y no en el móvil.
                #
                # Es el número que va en el agujero del quesito de la portada, y
                # el que marca el tamaño de cada trozo. Sumar tres enteros en el
                # navegador no tiene ningún misterio, y ése es justamente el
                # problema: cada vez que un número que se lee sale de una cuenta
                # hecha en la PWA, deja de haber un test que lo respalde. La
                # frontera está escrita en la cabecera de `static/graficos.js` y
                # la vigila `test_la_pwa_no_calcula_estadistica`; esto es lo que
                # cuesta respetarla, y cuesta una línea.
                "dias_con_decision": sum(colores_esta.values()),
            }
        )

    for clave, etiqueta, modelo, columna in (
        ("fuerza", "Sesiones de fuerza", WorkoutLog, WorkoutLog.date),
        ("bici", "Salidas de bici", Activity, Activity.date),
    ):
        a = _cuenta_dias(session, modelo, columna, ini_esta, hoy)
        b = _cuenta_dias(session, modelo, columna, ini_previa, fin_previa)
        if not a and not b:
            continue
        lineas.append(
            {
                "clave": clave,
                "etiqueta": etiqueta,
                "lectura": _lectura_conteo(a, b),
                "esta_semana": a,
                "semana_anterior": b,
            }
        )

    if lineas:
        return {
            "titulo": "Qué ha cambiado",
            "subtitulo": SUBTITULO_CAMBIOS,
            "estado": "con_datos",
            "na": None,
            "lineas": lineas,
        }
    return {
        "titulo": "Qué ha cambiado",
        "subtitulo": SUBTITULO_CAMBIOS,
        "estado": "vacio",
        "na": (
            "todavía no hay nada que comparar. Este bloque mira la semana contra "
            "la anterior, y para eso el sistema tiene que llevar al menos dos "
            "semanas guardando decisiones y apuntando entrenos"
        ),
        "lineas": [],
    }


def _colores(session: Session, desde: date, hasta: date) -> dict[str, int]:
    filas = session.execute(
        select(Decision.light, func.count())
        .where(
            Decision.date >= desde,
            Decision.date <= hasta,
            Decision.is_current.is_(True),
        )
        .group_by(Decision.light)
    ).all()
    cuenta = {"green": 0, "amber": 0, "red": 0}
    for luz, n in filas:
        if luz in cuenta:
            cuenta[luz] = int(n)
    return cuenta


def _cuenta_dias(
    session: Session, modelo: Any, columna: Any, desde: date, hasta: date
) -> int:
    return int(
        session.scalar(
            select(func.count(func.distinct(columna)))
            .select_from(modelo)
            .where(columna >= desde, columna <= hasta)
        )
        or 0
    )


# El nombre de cada color EN SUS DOS FORMAS, y por eso esta tabla sigue
# existiendo aparte de la de `app/engine/luces.py` en vez de derivarse de ella:
# «verde» → «verdes» pero «ámbar» → «ámbares», y pegar sufijos en castellano es
# justo el fallo que `app/analysis/texto.py` documenta entero.
#
# El singular hacía falta y no estaba. Con solo el plural, la primera línea de
# la primera pantalla del panel decía «3 verdes, 1 ámbares esta semana»: la
# frase que se lee a las siete de la mañana, mal escrita, en la puerta. Pasó
# desapercibida porque el comentario de aquí arriba ya hablaba de plurales y
# parecía que el problema estaba resuelto -tenía la tabla, le faltaba la mitad-.
#
# Lo que sí se comprueba es que hable de los mismos tres colores que el motor:
# si mañana aparece un cuarto, esto revienta al importar y no cuatro semanas
# después con un `KeyError` en la portada de un martes por la mañana.
NOMBRE_LUZ = {
    "green": ("verde", "verdes"),
    "amber": ("ámbar", "ámbares"),
    "red": ("rojo", "rojos"),
}
assert set(NOMBRE_LUZ) == set(_LUCES), (
    "los plurales de la portada y los colores del motor no hablan de lo mismo: "
    f"portada {sorted(NOMBRE_LUZ)}, motor {sorted(_LUCES)}"
)


def _lectura_semaforo(esta: dict[str, int], previa: dict[str, int]) -> str:
    partes = [f"{n} {plural(n, *NOMBRE_LUZ[c])}" for c, n in esta.items() if n]
    ahora = ", ".join(partes) if partes else "ningún día con decisión"
    antes = sum(previa.values())
    if not antes:
        return f"{ahora} esta semana. La anterior no hay con qué compararlo."
    verdes_a, verdes_b = esta.get("green", 0), previa.get("green", 0)
    if verdes_a > verdes_b:
        return f"{ahora} esta semana: más verdes que la anterior."
    if verdes_a < verdes_b:
        return f"{ahora} esta semana: menos verdes que la anterior."
    return f"{ahora} esta semana, los mismos verdes que la anterior."


def _lectura_conteo(a: int, b: int) -> str:
    if a == b:
        return f"{a} esta semana, igual que la anterior"
    if a > b:
        return f"{a} esta semana, {a - b} más que la anterior"
    return f"{a} esta semana, {b - a} menos que la anterior"


# ---------------------------------------------------------------------------
# La otra mitad del bloque 3: lo que todavía no se puede saber
# ---------------------------------------------------------------------------


def _falta_para(n: int, cuantos: str) -> str:
    """Lo que le falta a un contador para llegar a `MINIMO_REFERENCIA`.

    Frase ENTERA y no un trozo. El navegador la imprime tal cual porque aquí es
    donde se sabe el número, y las dos formas que hacen falta -«todavía no hay»
    y «llevas N»- no se distinguen por un sufijo: son dos noticias distintas.
    Empezar de cero es algo que hacer; ir por la mitad es una cuenta atrás.

    Se evita a propósito meter el número delante del sustantivo («18 check-ins
    más»), que obligaría a concordar el verbo de la frase que lo envuelve. Es la
    misma razón por la que `LIGHT_ES` escribe los femeninos enteros en vez de
    pegarle una letra al color.
    """
    if not n:
        return f"Todavía no hay {cuantos}. Hacen falta {MINIMO_REFERENCIA}."
    return (
        f"Llevas {n} y hacen falta {MINIMO_REFERENCIA} para que el número "
        f"signifique algo: {cuantos}, {MINIMO_REFERENCIA - n} más."
    )


def lo_que_falta(session: Session) -> list[dict[str, Any]]:
    """Qué preguntas no tienen respuesta todavía, y qué dato exacto las abriría.

    Es la pieza que convierte "el panel está vacío" en "el panel te está
    diciendo por qué está vacío y qué lo llenaría". Sin esto, las cuatro vistas
    sin datos son indistinguibles de cuatro vistas rotas.

    Se calcula CONTANDO FILAS, no escribiendo el estado a mano. Escrito a mano
    seguiría diciendo "hacen falta check-ins" el día que haya doscientos, que es
    la forma que tiene un texto de envejecer hacia falso en vez de hacia viejo.

    EL UMBRAL ERA CERO, Y CERO NO ES EL NÚMERO QUE DECIDE NADA

    Esto preguntaba `if not n_checkin`. O sea que la pregunta desaparecía de la
    lista al llegar el check-in NÚMERO UNO, y con las cuatro fuera la sección
    entera se quedaba vacía y el bloque no se pintaba. Medido el 2026-09-17
    sobre la base real: 2 check-ins, 4 decisiones, 16 sesiones. La portada no
    tenía nada pendiente que contar mientras ninguna de las cuatro preguntas
    podía contestarse todavía.

    Y el número bueno estaba en este mismo fichero, cien líneas más arriba:
    `MINIMO_REFERENCIA`, que es el que `_linea_de_serie` ya usa para decidir si
    un percentil significa algo, con su frase «hacen falta 20» y todo. Dos varas
    de medir en el mismo módulo, y gobernaba la floja justo la sección que
    existe para explicar la ausencia. Un contador que se lee y no es el que
    decide es la misma avería de siempre, esta vez en la primera pantalla.
    """
    n_checkin = session.scalar(select(func.count()).select_from(Checkin)) or 0
    n_decision = session.scalar(select(func.count()).select_from(Decision)) or 0
    n_fuerza = session.scalar(select(func.count()).select_from(WorkoutLog)) or 0

    faltan: list[dict[str, Any]] = []
    if n_checkin < MINIMO_REFERENCIA:
        faltan.append(
            {
                "que": "Cómo se relaciona lo que NOTAS con lo que mide el reloj",
                "falta": _falta_para(n_checkin, "check-ins por la mañana"),
                "vistas": ["concordancia", "desfase"],
            }
        )
    if n_fuerza < MINIMO_REFERENCIA:
        faltan.append(
            {
                "que": "Qué te hace cada rutina de fuerza",
                "falta": _falta_para(n_fuerza, "sesiones de Hevy apuntadas"),
                "vistas": ["impacto"],
            }
        )
    if n_decision < MINIMO_REFERENCIA:
        faltan.append(
            {
                "que": "Si el motor acierta con el color del día",
                "falta": _falta_para(n_decision, "decisiones guardadas, una por mañana"),
                "vistas": ["auditoria"],
            }
        )
    if n_checkin < MINIMO_REFERENCIA or n_fuerza < MINIMO_REFERENCIA:
        # Esta necesita las DOS cosas, así que se nombra la que vaya más
        # atrasada. Decir "faltan check-ins y sesiones" cuando las sesiones ya
        # están sería mandar a hacer algo que no desbloquea nada.
        peor = "check-ins" if n_checkin <= n_fuerza else "sesiones apuntadas"
        faltan.append(
            {
                "que": "Si lo que la mañana prometía se parece a lo que sale",
                "falta": (
                    f"Hacen falta las dos cosas a la vez, y lo que va más corto "
                    f"son los {peor}. "
                    + _falta_para(min(n_checkin, n_fuerza), peor)
                ),
                "vistas": ["percepcion"],
            }
        )
    return faltan


# ---------------------------------------------------------------------------
# La vista
# ---------------------------------------------------------------------------


def vista_portada(
    session: Session,
    cfg: Any = None,
    *,
    dias: int = 180,
    hoy: date | None = None,
    metodo: str = "spearman",
) -> dict[str, Any]:
    """Los tres bloques, en el orden en que se leen."""
    hoy = hoy or date.today()
    cob = S.cobertura(session)
    lenguaje = lenguaje_de(cfg)

    impacto = vista_impacto(session, dias=dias, hoy=hoy, metodo=metodo)
    if lenguaje is None:
        # Sin la sección `metrics` no hay bandas ni grupos, y sin ellos no se
        # puede contar un hallazgo sin inventarse los cortes. Se dice, no se
        # rellena con un defecto: un corte inventado decide qué se le enseña al
        # usuario y qué no, y eso no puede salir de un `.get(clave, 0.3)`.
        encontrados: list[dict[str, Any]] = []
        na_hallazgos = (
            "falta la sección `metrics` en config.yaml, que es donde se declara "
            "cuándo una relación se puede contar y cómo se agrupan las "
            "exposiciones. Sin eso no se puede redactar ningún hallazgo"
        )
        tope = 0
    else:
        encontrados = hallazgos(impacto["rejilla"], lenguaje)
        na_hallazgos = None
        tope = lenguaje.hallazgos_en_portada

    calculadas = sum(
        1
        for fila in impacto["rejilla"]
        for c in fila["por_dia"]
        if c.get("r") is not None
    )

    return {
        "vista": "portada",
        "ventana": impacto["ventana"],
        "cobertura": cob.como_dict(),
        "como_voy": como_voy(session, hoy=hoy, dias=dias, cob=cob),
        "que_ha_cambiado": que_ha_cambiado(session, hoy=hoy),
        "lo_que_se_sabe": {
            "titulo": "Lo que ya sé de ti",
            # Los tres bloques traen `subtitulo` porque los tres se pintan con la
            # misma cabecera, y a este le faltaba: el renderizador leía
            # `b.subtitulo` sobre un diccionario que no la tenía y en JavaScript
            # eso no es un error, es un `undefined` que no imprime nada. La
            # cabecera salía a medias y no había forma de notarlo desde el móvil.
            # Decía «De las {calculadas} relaciones que hoy se pueden calcular,
            # éstas son las que aguantan». Es verdad y es método: el número de
            # relaciones probadas es el denominador de la corrección, y quien lo
            # necesita lo tiene en `relaciones_calculadas` y en la vista de
            # impacto. En la primera pantalla la pregunta es qué se sabe, no
            # cuántas cosas se probaron para saberlo. La advertencia sobre el
            # azar NO se va: sigue debajo, en `nota_azar`, que es la que dice en
            # llano por qué se puede creer lo de encima (25/09/2026).
            "subtitulo": "Lo que se repite en tus datos lo bastante como para contártelo.",
            "estado": "con_datos" if encontrados else "vacio",
            "na": na_hallazgos
            or (
                None
                if encontrados
                else (
                    "de las relaciones que se pueden calcular hoy, ninguna es lo "
                    "bastante fuerte ni lo bastante fiable para contarla. No es un "
                    "fallo: es que todavía no hay señal que destaque del ruido"
                )
            ),
            "portada": encontrados[:tope],
            "resto": max(0, len(encontrados) - tope),
            "total": len(encontrados),
            "relaciones_calculadas": calculadas,
            "nota_azar": NOTA_AZAR,
        },
        "lo_que_no_se_puede_saber": lo_que_falta(session),
    }
