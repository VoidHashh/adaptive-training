"""Las tres medidas del desacuerdo: cuántas veces, hacia dónde, y contra qué regla.

PARA QUÉ EXISTE ESTA VISTA
--------------------------
No para llevar la cuenta de quién gana. El objetivo escrito cuando se pidió el
botón de previsualizar era CALIBRAR: que las respuestas de la mañana y las
decisiones del motor converjan con el tiempo hasta que lo que dice el sistema
coincida con lo que uno sabe de sí mismo. Un marcador no hace eso; lo que hace
eso es saber EN QUÉ SITIO concreto discrepan, y ese sitio es una regla y su
umbral.

De ahí las tres medidas, que son tres preguntas distintas y ninguna sustituye a
otra:

  (a) cuántas veces se discrepa, y en qué dirección -¿pidiendo más o pidiendo
      menos?-. Sin la dirección, "discrepo el 30 %" junta a dos personas
      opuestas: la que siempre quiere entrenar más de lo que le dicen y la que
      siempre quiere menos.
  (b) quién acertó, medido contra cómo salió la sesión. Es la única de las tres
      que no se puede contestar con la tabla de previsualizaciones sola, y la
      única que se calla hasta tener muestra.
  (c) si el desacuerdo se agolpa en un umbral. Es la que sirve para ARREGLAR
      algo: doce desacuerdos repartidos entre nueve reglas no dicen qué tocar;
      nueve de doce el día que disparó `fatiga_alta` sí.

LA VISTA NO SE ESCONDE; EL VEREDICTO SÍ SE CALLA
------------------------------------------------
`metricas.js` lleva escrito que ninguna vista se esconde: con la base vacía se
pintan todas, enteras, con el motivo en cada casilla. Esta no es la excepción, y
conviene decir por qué no se contradice con el "callada hasta tener muestra" que
se pidió.

Lo que se calla es el VEREDICTO de (b) -"tenías razón", "la tenía el motor"-, y
solo ése. Los recuentos salen desde el primer día, la lista de casos sale con un
caso, y cuando no hay bastante para pronunciarse se dice cuánto falta y por qué.
Esconder la vista entera convertiría la falta de muestra en una pantalla que no
existe, y entonces no habría forma de saber si es que no hay datos o si es que
la funcionalidad no se hizo. Callar el veredicto es lo contrario: se ve todo lo
que hay, y encima se ve que todavía no alcanza.

EL JUEZ NO ES INDEPENDIENTE, Y ESO VA ESCRITO EN LA PANTALLA
------------------------------------------------------------
La medida (b) compara los desacuerdos contra `session_performance`, y el índice
de rendimiento de esa tabla lleva dentro `comp_rpe`: el esfuerzo percibido que
uno mismo apunta a la mañana siguiente. O sea que la nota de la sesión que va a
decidir si uno tenía razón la pone, en parte, uno mismo.

Eso no lo invalida -es la única medida del resultado que hay, y una medida
imperfecta y etiquetada vale más que ninguna- pero SÍ tiene que viajar con la
etiqueta puesta. Un veredicto que se presentara como neutral estaría fingiendo
una independencia que no tiene, y el día que dijera "casi siempre tenías razón"
no habría forma de distinguir entre acertar y ser indulgente al puntuarse.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.stats import N_MINIMO_CALCULABLE
from app.analysis.texto import cuantos
from app.engine.session_builder import DUREZA
from app.models import Decision as DecisionRow
from app.models import Preview as PreviewRow
from app.models import SessionPerformance

# Cuántos desacuerdos EJECUTADOS hacen falta antes de decir quién acertó.
#
# No es un umbral estadístico: con cinco casos no hay contraste que aguante
# nada, y por eso lo que sale nunca es una p. Es el punto por debajo del cual
# una frase comparativa -"salen mejor los días que discrepas"- se sostiene sobre
# tan poca cosa que una noche mala la da la vuelta entera. Con dos casos, uno
# malo mueve la mediana cincuenta puntos.
#
# Y hay un motivo para que sea más alto que el `N_MINIMO_EXPUESTOS = 3` de
# impacto, donde también se comparan dos grupos: allí el desenlace lo mide el
# reloj, y aquí lo mide en parte el propio usuario. Un juez que no es
# independiente pide más muestra antes de dejarle hablar, no menos.
MINIMO_JUICIOS = 5

# Las tres direcciones posibles de un desacuerdo declarado, en el orden en que se
# leen. La tercera no es un hueco: es el caso más frecuente -decir que no y no
# pedir nada distinto- y esconderlo dejaría la suma de las dos primeras por
# debajo del total sin explicación.
MAS_DURA = "mas_dura"
MAS_SUAVE = "mas_suave"
SIN_PEDIR = "sin_pedir"

# Cada dirección va con DOS rótulos y no uno, y el corto no es un capricho de
# maquetación: la etiqueta larga ocupa cincuenta caracteres y encima de una barra
# de 340 píxeles -el ancho del móvil más estrecho, regla 5- se sale por el lado.
# Escrito el corto en la PWA, esta lista tendría dos mitades en dos ficheros y
# añadir una cuarta dirección dejaría una barra rotulada con su clave cruda.
DIRECCIONES: tuple[tuple[str, str, str], ...] = (
    (MAS_DURA, "Pediste una sesión más dura de la propuesta", "Más dura"),
    (MAS_SUAVE, "Pediste una sesión más suave de la propuesta", "Más suave"),
    (SIN_PEDIR, "Dijiste que no lo compartías, sin pedir otra sesión", "Sin pedir nada"),
)

SIN_REGLA = "(ninguna regla lo decidió)"


def _pct(veces: int, de: int) -> float | None:
    """Nunca un 0,0 sin denominador. Sin denominador no hay porcentaje."""
    return round(100.0 * veces / de, 1) if de else None


def _json(texto: str | None) -> dict[str, Any]:
    """El JSON guardado, o un diccionario vacío si no hay o no se puede leer.

    Se traga el `JSONDecodeError` a propósito, y es el único sitio de este módulo
    donde se traga algo. `decision_json` es un bloque de texto escrito hace meses
    por una versión anterior del motor: una fila vieja ilegible tiene que costar
    esa fila, no la vista entera. Lo que no se traga es la AUSENCIA de una clave
    que se espera, que va por `_lista` en `encabezados.py` y revienta.
    """
    if not texto:
        return {}
    try:
        cargado = json.loads(texto)
    except (ValueError, TypeError):
        return {}
    return cargado if isinstance(cargado, dict) else {}


def _direccion(fila: PreviewRow) -> str:
    """Hacia dónde tiraba el desacuerdo, leyendo lo que se pidió en vez de lo dado.

    Sale de comparar la dureza pedida con la propuesta, y NO del motivo escrito a
    mano: el texto libre es lo que explica el desacuerdo a un humano dentro de
    seis meses, y es lo peor posible para contar categorías. Una medida que
    dependiera de si uno escribió "me encuentro bien" o "estoy fresco" no sería
    una medida.

    Sin anulación no se inventa dirección. «Dije que no y no pedí nada distinto»
    es una respuesta entera, y la más común: uno mira la tarjeta, no la comparte,
    y aun así hace lo que pone. Meterla en cualquiera de las otras dos sería
    rellenar un hueco con la opción que más se le parece, que es como se fabrican
    las tendencias que no existen.
    """
    pedida = fila.override_session_type
    propuesta = fila.session_type
    if not pedida or not propuesta:
        return SIN_PEDIR
    if pedida not in DUREZA or propuesta not in DUREZA:
        # Una dureza que el motor de hoy no conoce. Pasa si se renombra un tipo
        # de sesión y quedan filas viejas con el nombre de antes. No se cuenta
        # como ninguna de las dos direcciones porque no se sabe cuál es, y
        # elegir una a ojo estropearía justo el número que esta vista da.
        return SIN_PEDIR
    if DUREZA[pedida] > DUREZA[propuesta]:
        return MAS_DURA
    if DUREZA[pedida] < DUREZA[propuesta]:
        return MAS_SUAVE
    return SIN_PEDIR


# ---------------------------------------------------------------------------
# (a) Cuántas veces se discrepa, y hacia dónde
# ---------------------------------------------------------------------------


def _cuantas(filas: list[PreviewRow]) -> dict[str, Any]:
    """El recuento, con los TRES estados de `disagreed` separados.

    El porcentaje se calcula sobre las OPINADAS y no sobre el total, por lo mismo
    que el contador de percepción se calcula sobre las sesiones juzgables: una
    previsualización que se miró sin decir nada no es un acuerdo. Meterla en el
    denominador haría bajar el porcentaje cada vez que se mira la tarjeta y se
    cierra el móvil, que es la mayoría de las veces, y el número acabaría midiendo
    con qué frecuencia se pulsa un botón.

    Los dos denominadores viajan igualmente, porque el segundo dice otra cosa que
    hace falta saber: si se opina una de cada veinte veces, el porcentaje de
    arriba describe a las que opinan, no a las mañanas.
    """
    discrepadas = sum(1 for f in filas if f.disagreed is True)
    conformes = sum(1 for f in filas if f.disagreed is False)
    sin_opinar = sum(1 for f in filas if f.disagreed is None)
    opinadas = discrepadas + conformes
    return {
        "discrepadas": discrepadas,
        "conformes": conformes,
        "sin_opinar": sin_opinar,
        "opinadas": opinadas,
        "total": len(filas),
        "pct": _pct(discrepadas, opinadas),
        "que_es": (
            "el porcentaje va sobre las previsualizaciones en las que dijiste "
            "algo; mirar la tarjeta y no decir nada no cuenta como estar de "
            "acuerdo"
        ),
        "na": None
        if opinadas
        else (
            "todavía no has dicho ni que sí ni que no en ninguna "
            "previsualización, así que esto no tiene denominador: no es un cero, "
            "es que aún no se puede contar"
        ),
    }


def _direcciones(filas: list[PreviewRow]) -> dict[str, Any]:
    """Las tres casillas de la dirección, más las forzadas en rojo aparte.

    `forzadas_en_rojo` no es una cuarta dirección: es un subconjunto de
    `mas_dura`, y va suelto porque es el caso que se pidió poder mirar por
    separado -subir de dureza con el semáforo en rojo, confirmado a mano-.
    Sumarlo a las tres celdas daría un total mayor que el número de desacuerdos y
    haría pensar que se ha perdido la cuenta.
    """
    desacuerdos = [f for f in filas if f.disagreed is True]
    n = len(desacuerdos)
    cuenta: dict[str, int] = {}
    for f in desacuerdos:
        clave = _direccion(f)
        cuenta[clave] = cuenta.get(clave, 0) + 1

    celdas = [
        {
            "clave": clave,
            "etiqueta": etiqueta,
            "corta": corta,
            "n": cuenta.get(clave, 0),
            "pct": _pct(cuenta.get(clave, 0), n),
        }
        for clave, etiqueta, corta in DIRECCIONES
    ]
    return {
        "n": n,
        "celdas": celdas,
        "forzadas_en_rojo": sum(1 for f in desacuerdos if f.forced_on_red),
        "que_es": (
            "«forzadas en rojo» son un subconjunto de las que pedían más dura, no "
            "una cuarta casilla: por eso no suman"
        ),
        "lectura": _lectura_direcciones(celdas, n),
        "na": None
        if n
        else "todavía no has marcado ningún desacuerdo, así que no hay dirección",
    }


def _lectura_direcciones(celdas: list[dict[str, Any]], n: int) -> str | None:
    """Hacia dónde tira, en una frase, o `None` si todavía no se puede decir.

    Se calla por debajo de `N_MINIMO_CALCULABLE` porque con dos desacuerdos
    «siempre pides más dura» es literalmente cierto y completamente vacío.
    """
    if n < N_MINIMO_CALCULABLE:
        return None
    dura = next(c for c in celdas if c["clave"] == MAS_DURA)["n"]
    suave = next(c for c in celdas if c["clave"] == MAS_SUAVE)["n"]
    if dura == 0 and suave == 0:
        return (
            "cuando no lo compartes no pides otra sesión: aceptas la propuesta y "
            "dejas apuntado que no la compartías"
        )
    if dura == suave:
        return (
            f"va igualado: {dura} hacia más dura y {suave} hacia más suave de "
            f"{cuantos(n, 'desacuerdo', 'desacuerdos')}"
        )
    if dura > suave:
        return (
            f"cuando discrepas es sobre todo porque el motor se queda corto: "
            f"{dura} de {n} pedían una sesión más dura"
        )
    return (
        f"cuando discrepas es sobre todo porque el motor se pasa: {suave} de {n} "
        f"pedían una sesión más suave"
    )


# ---------------------------------------------------------------------------
# (c) Dónde se agolpa: la regla que decidió el día, y el color
# ---------------------------------------------------------------------------


def _agrupar(
    filas: list[PreviewRow], clave_de, sin_valor: str
) -> list[dict[str, Any]]:
    """Agrupa y devuelve LOS DOS porcentajes, que contestan preguntas distintas.

    `pct_de_los_desacuerdos` es cuánto pesa este grupo dentro de todo lo que se
    discrepa. `pct_cuando_aparece` es con qué frecuencia se discrepa cuando este
    grupo sale. Uno solo de los dos miente en el caso normal:

      - una regla que dispara todos los días se lleva el primer porcentaje por
        ser la más frecuente, aunque se discrepe de ella menos que de ninguna;
      - una regla que ha disparado dos veces y se discrepó las dos tiene un
        100 % en el segundo, y no es donde hay que mirar.

    Leídos juntos sí señalan un sitio: el grupo que pesa mucho Y en el que se
    discrepa mucho. Por eso van los dos y por eso ninguno viaja solo.
    """
    total_desacuerdos = sum(1 for f in filas if f.disagreed is True)
    grupos: dict[str, dict[str, int]] = {}
    for f in filas:
        clave = clave_de(f) or sin_valor
        g = grupos.setdefault(clave, {"discrepadas": 0, "opinadas": 0, "vistas": 0})
        g["vistas"] += 1
        if f.disagreed is not None:
            g["opinadas"] += 1
        if f.disagreed is True:
            g["discrepadas"] += 1

    salida = [
        {
            "clave": clave,
            "discrepadas": g["discrepadas"],
            "opinadas": g["opinadas"],
            "vistas": g["vistas"],
            "pct_de_los_desacuerdos": _pct(g["discrepadas"], total_desacuerdos),
            "pct_cuando_aparece": _pct(g["discrepadas"], g["opinadas"]),
        }
        for clave, g in grupos.items()
    ]
    # Por desacuerdos y después por nombre: el segundo criterio no es cosmético.
    # Sin él, dos grupos empatados se ordenan como los devuelva el diccionario, y
    # una tabla que se reordena sola entre dos recargas no se puede comparar
    # consigo misma.
    salida.sort(key=lambda d: (-d["discrepadas"], d["clave"]))
    return salida


def _donde_se_agolpa(filas: list[PreviewRow]) -> dict[str, Any]:
    """Por regla que decidió el día y por color, con la lectura si da la muestra."""
    por_regla = _agrupar(
        filas, lambda f: _json(f.decision_json).get("trigger_rule"), SIN_REGLA
    )
    por_luz = _agrupar(filas, lambda f: f.light, "(sin color)")
    n = sum(1 for f in filas if f.disagreed is True)
    return {
        "n": n,
        "por_regla": por_regla,
        "por_luz": por_luz,
        "que_es": (
            "la regla es la que decidió el color de ese día, leída de la decisión "
            "que se previsualizó; no es «la regla con la que no estás de acuerdo», "
            "que eso no se pregunta en ninguna parte"
        ),
        "lectura": _lectura_agolpe(por_regla, n),
        "na": None
        if n
        else (
            "todavía no has marcado ningún desacuerdo, así que no hay nada que "
            "agrupar"
        ),
    }


def _lectura_agolpe(por_regla: list[dict[str, Any]], n: int) -> str | None:
    """La regla que se lleva más desacuerdos, si es que alguna destaca.

    «Destaca» es tener más que la siguiente. Con un empate en cabeza no se nombra
    ninguna: nombrar la primera de dos iguales sería inventarse un hallazgo a
    partir del orden alfabético.
    """
    if n < N_MINIMO_CALCULABLE or not por_regla:
        return None
    primera = por_regla[0]
    if not primera["discrepadas"]:
        return None
    segunda = por_regla[1]["discrepadas"] if len(por_regla) > 1 else 0
    if primera["discrepadas"] == segunda:
        return "los desacuerdos no se agolpan en ninguna regla concreta"
    return (
        f"{primera['discrepadas']} de {n} salieron el día que decidió "
        f"`{primera['clave']}`; de las {primera['opinadas']} veces que decidió y "
        f"dijiste algo, discrepaste {primera['discrepadas']}"
    )


# ---------------------------------------------------------------------------
# (b) Quién acertó. La única que se calla.
# ---------------------------------------------------------------------------

ETIQUETA_JUEZ = (
    "El resultado de la sesión lo mide `session_performance`, y dentro de ese "
    "índice va el esfuerzo percibido que apuntas tú a la mañana siguiente. O sea "
    "que la nota que decide quién tenía razón la pones en parte tú. No es un juez "
    "independiente y no se presenta como tal."
)


def _rendimiento_por_dia(
    session: Session, desde: date, hasta: date
) -> dict[date, list[float]]:
    """Los percentiles de rendimiento de cada día, en una sola consulta.

    Una lista por día y no un número: un día puede tener fuerza y bici, y quedarse
    con una de las dos elegiría a ojo cuál cuenta.
    """
    filas = session.execute(
        select(SessionPerformance.date, SessionPerformance.performance_pct).where(
            SessionPerformance.date >= desde,
            SessionPerformance.date <= hasta,
            SessionPerformance.performance_pct.is_not(None),
        )
    ).all()
    por_dia: dict[date, list[float]] = {}
    for dia, pct in filas:
        por_dia.setdefault(dia, []).append(float(pct))
    return por_dia


def _mediana(xs: list[float]) -> float | None:
    if not xs:
        return None
    ord_ = sorted(xs)
    m = len(ord_) // 2
    if len(ord_) % 2:
        return round(ord_[m], 1)
    return round((ord_[m - 1] + ord_[m]) / 2.0, 1)


def _sesion_ejecutada(session: Session, decision_id: int | None) -> str | None:
    """Qué dureza se ejecutó de verdad ese día, leída de la decisión guardada.

    Hace falta para separar «discrepé y se hizo lo que pedí» de «discrepé y se
    hizo lo propuesto igual». Solo el primero puede juzgar nada: en el segundo la
    sesión que salió es la del motor, así que el resultado no dice si el usuario
    tenía razón, dice lo mismo que cualquier otro día. Contarlos juntos mediría
    la opinión contra un desenlace que la opinión no tocó.
    """
    if decision_id is None:
        return None
    fila = session.get(DecisionRow, decision_id)
    if fila is None:
        return None
    return _json(fila.planned_session_json).get("kind")


def _quien_acerto(
    session: Session, filas: list[PreviewRow], desde: date, hasta: date
) -> dict[str, Any]:
    """Los casos uno a uno, y el veredicto solo si hay muestra.

    Los CASOS salen siempre, desde el primero. Es la parte que no se esconde: son
    hechos -este día pediste esto, se ejecutó aquello, la sesión salió en el
    percentil tal- y un hecho no necesita muestra para poder mirarse. Lo que
    necesita muestra es la frase que compara, y ésa es la que se calla.
    """
    rendimiento = _rendimiento_por_dia(session, desde, hasta)
    desacuerdos = [f for f in filas if f.disagreed is True]

    casos: list[dict[str, Any]] = []
    for f in desacuerdos:
        ejecutada = _sesion_ejecutada(session, f.decision_id)
        pcts = rendimiento.get(f.date, [])
        casos.append({
            "fecha": f.date.isoformat(),
            "preview_id": f.id,
            "luz": f.light,
            "propuesta": f.session_type,
            "pediste": f.override_session_type,
            "direccion": _direccion(f),
            "forzada_en_rojo": bool(f.forced_on_red),
            "se_envio": f.decision_id is not None,
            "se_ejecuto": ejecutada,
            # Se hizo lo que pedías: hay anulación pedida y la decisión guardada
            # acabó siendo esa. Sin anulación no es que no se hiciera: es que no
            # había nada distinto que hacer, y por eso es `None` y no `False`.
            "se_hizo_lo_que_pediste": (
                None
                if not f.override_session_type
                else (ejecutada == f.override_session_type)
            ),
            "rendimiento_pct": _mediana(pcts),
            "na": _na_del_caso(f, ejecutada, pcts),
        })
    casos.sort(key=lambda c: (c["fecha"], c["preview_id"]))

    juzgables = [
        c for c in casos if c["se_hizo_lo_que_pediste"] and c["rendimiento_pct"] is not None
    ]
    # El grupo de contraste: los días de la ventana con rendimiento medido en los
    # que NO se llevó la contraria al motor. Sin él, decir "los días que discrepas
    # salen en el percentil 62" no dice nada, porque no se sabe en qué percentil
    # sale un día cualquiera.
    dias_discrepados = {f.date for f in desacuerdos}
    resto = [
        p
        for dia, pcts in rendimiento.items()
        if dia not in dias_discrepados
        for p in pcts
    ]

    return {
        "n": len(juzgables),
        "hacen_falta": MINIMO_JUICIOS,
        "casos": casos,
        "mediana_discrepando": _mediana([c["rendimiento_pct"] for c in juzgables]),
        "mediana_el_resto": _mediana(resto),
        "n_el_resto": len(resto),
        "veredicto": _veredicto(juzgables, resto),
        "etiqueta_del_juez": ETIQUETA_JUEZ,
        "na": _na_del_veredicto(casos, juzgables),
    }


def _na_del_caso(
    fila: PreviewRow, ejecutada: str | None, pcts: list[float]
) -> str | None:
    """Por qué este caso no juzga nada, cuando no juzga. `None` cuando sí.

    Cada motivo por separado y no un "no se puede": la diferencia entre «no lo
    enviaste», «pediste otra cosa y se hizo la propuesta» y «no hay resultado de
    esa sesión» es la diferencia entre tres cosas distintas que arreglar.
    """
    if fila.decision_id is None:
        return "lo miraste y no llegaste a enviarlo, así que no hay sesión que juzgar"
    if not fila.override_session_type:
        return (
            "dijiste que no lo compartías y no pediste otra sesión, así que salió "
            "la del motor: el resultado no dice quién tenía razón"
        )
    if ejecutada != fila.override_session_type:
        return (
            f"pediste «{fila.override_session_type}» y acabó ejecutándose "
            f"«{ejecutada or 'nada que conste'}», así que lo que salió no es lo "
            f"que pedías"
        )
    if not pcts:
        return (
            "esa sesión todavía no tiene resultado medido; suele faltarle el "
            "esfuerzo percibido de la mañana siguiente"
        )
    return None


def _veredicto(
    juzgables: list[dict[str, Any]], resto: list[float]
) -> str | None:
    """La frase comparativa, o `None` mientras no haya con qué.

    Devuelve `None` en los dos casos en que callar es lo correcto: cuando no se
    llega a `MINIMO_JUICIOS`, y cuando no hay grupo de contraste. El segundo es
    más fácil de olvidar: con quince desacuerdos ejecutados y ningún día normal
    medido, la mediana de los quince es un número perfectamente calculado que no
    se puede comparar contra nada.

    Y no dice "tenías razón". Dice hacia dónde salieron las sesiones, que es lo
    que se ha medido. La distancia entre las dos frases es justo lo que el
    `comp_rpe` de dentro del índice no permite recorrer.

    NI UN NÚMERO EN LA FRASE, y eso es la regla 2, no una manía de redacción.
    Aquí ponía «... salieron mejor que las demás: percentil 62 frente a 55», y
    eso es lo que esta frase tiene prohibido: es la que se lee al abrir la
    pantalla, y los percentiles van detrás de «ver detalle». Las dos medianas
    viajan igual -`mediana_discrepando` y `mediana_el_resto` van al lado- y la
    pantalla las pinta abajo, plegadas. O sea que no se pierde el número: se
    pierde de la primera línea, que es donde molestaba. Lo caza
    `render_pwa.mjs`, que cuenta las palabras prohibidas de la vista principal.

    Las dos medianas NO se comprueban contra `None`, y no es un descuido: aquí
    `_mediana` no puede devolverlo. `juzgables` viene ya filtrado por
    `rendimiento_pct is not None`, y de `resto` se acaba de exigir arriba que no
    esté vacía. La guarda estuvo escrita y se quitó al ver que ninguna mutación
    la ponía roja: no podía dispararse. Si algún día se afloja cualquiera de los
    dos filtros esto revienta con un TypeError en la comparación, que es
    preferible a callarse por una rama que nadie sabía que existía.
    """
    if len(juzgables) < MINIMO_JUICIOS or not resto:
        return None
    mio = _mediana([c["rendimiento_pct"] for c in juzgables])
    otros = _mediana(resto)
    n = len(juzgables)
    if mio > otros:
        return (
            f"las {cuantos(n, 'sesión', 'sesiones')} que hiciste llevándole la "
            f"contraria al motor salieron mejor que las demás"
        )
    if mio < otros:
        return (
            f"las {cuantos(n, 'sesión', 'sesiones')} que hiciste llevándole la "
            f"contraria al motor salieron peor que las demás"
        )
    return (
        f"las {cuantos(n, 'sesión', 'sesiones')} que hiciste llevándole la "
        f"contraria al motor salieron igual que las demás"
    )


def _na_del_veredicto(
    casos: list[dict[str, Any]], juzgables: list[dict[str, Any]]
) -> str | None:
    """Cuánto falta para poder decir algo, y de qué. `None` si ya se puede.

    Dice el número que falta, no "faltan datos". Es la diferencia entre una
    pantalla que se puede esperar -"van 2 de 5"- y una que parece rota.
    """
    if len(juzgables) >= MINIMO_JUICIOS:
        return None
    if not casos:
        return (
            f"todavía no has marcado ningún desacuerdo. Hacen falta "
            f"{MINIMO_JUICIOS} en los que además pidieras otra sesión, se "
            f"ejecutara la que pediste y esa sesión acabara con resultado medido"
        )
    return (
        f"van {len(juzgables)} de los {MINIMO_JUICIOS} que hacen falta. Solo "
        f"cuentan los desacuerdos en los que pediste otra sesión, se ejecutó la "
        f"que pediste y esa sesión tiene resultado medido: los demás salen abajo "
        f"con el motivo de por qué no juzgan nada"
    )


# ---------------------------------------------------------------------------
# La vista
# ---------------------------------------------------------------------------


def vista_calibracion(
    session: Session, *, dias: int = 180, hasta: date | None = None
) -> dict[str, Any]:
    """Las tres medidas sobre las previsualizaciones de la ventana.

    SOLO LEE. Igual que la vista de percepción, y por un motivo más fuerte aquí:
    la tabla de previsualizaciones es el registro de lo que se pensó cada mañana,
    y una pantalla que al abrirse escribiera en ella estaría cambiando el dato por
    mirarlo. Abrir las métricas no puede mover ninguno de los tres números.

    NO lleva `metodo`. Aquí no se correlaciona nada: se cuenta, se agrupa y se
    comparan dos medianas. Ofrecer un desplegable de Pearson o Spearman sugeriría
    una prueba estadística que esta vista no hace, y el sitio donde se decide qué
    creerse no es un desplegable, es la etiqueta del juez.
    """
    hasta = hasta or date.today()
    desde = hasta - timedelta(days=dias - 1)

    filas = list(
        session.scalars(
            select(PreviewRow)
            .where(PreviewRow.date >= desde, PreviewRow.date <= hasta)
            .order_by(PreviewRow.date, PreviewRow.seq, PreviewRow.id)
        )
    )

    return {
        "vista": "calibracion",
        # `ventana` con la misma forma que en las otras siete vistas, y no tres
        # claves sueltas. La PWA pinta los dos extremos con `pintarCobertura`,
        # que recibe la ventana entera: una vista que publicara `desde` y
        # `hasta` en la raíz tendría que pintarlos a mano y se quedaría con un
        # encabezado distinto al de sus hermanas sin que nada fallara.
        "ventana": {
            "desde": desde.isoformat(),
            "hasta": hasta.isoformat(),
            "dias": dias,
        },
        # NO lleva `cobertura`. Las otras vistas miran check-ins, Garmin, bici y
        # fuerza, y ahí saber de qué fuente falta qué es la mitad de la lectura.
        # Ésta mira una sola tabla, `previews`, que la escribe el propio botón
        # de previsualizar: su cobertura es el recuento del encabezado -cuántas
        # previsualizaciones y en cuántas dijiste algo- y repetirla como si
        # fueran cuatro fuentes sería inventarse tres.
        "cuantas": _cuantas(filas),
        "direcciones": _direcciones(filas),
        "donde": _donde_se_agolpa(filas),
        "quien_acerto": _quien_acerto(session, filas, desde, hasta),
    }
