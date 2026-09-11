"""Vista 3: qué le hace al cuerpo cada cosa que entrena, uno, dos y tres días después.

Las vistas 1 y 2 preguntan si se conoce. Esta pregunta algo distinto y más útil:
qué le pasa DESPUÉS. Cruza lo que hizo un día -la rutina, los ejercicios
concretos, la salida y cómo la clasificó Garmin, el volumen- contra cómo estaba
uno, dos y tres días más tarde.

Las tres preguntas que se pidieron explícitamente, y dónde se contestan:

  - ¿sube la molestia lumbar tras el Día 2? -> contraste de la rutina `dia_2`
    contra `lower_discomfort` a +1, +2 y +3;
  - ¿cuántos días de HRV cuesta una salida INTENSA? -> contraste de
    `bici_intensa` contra `hrv`, mirando en qué retardo deja de haber diferencia;
  - ¿el ánimo mejora tras la bici larga? -> contraste de `bici_larga` contra
    `mood`. Y "larga" no es un número inventado: es el cuarto superior de SUS
    salidas, que es lo único que significa algo para él.

LA TRAMPA DE ESTA VISTA
-----------------------
Es la vista peligrosa de las cinco. No porque calcule mal, sino porque calcula
bien muchas veces: treinta ejercicios contra la lumbar del día siguiente, con un
umbral del 5%, dan un ejercicio y medio "significativo" aunque ninguno tenga nada
que ver. Y no aparecería en el puesto quince, aparecería EL PRIMERO, porque el
ranking está ordenado justo por eso.

Contra eso van tres cosas, y ninguna es opcional:

  - la p corregida por Benjamini-Hochberg, que viaja al lado de la cruda;
  - la n de cada casilla siempre delante, porque "correlación de -0.8" sobre
    cuatro días es una anécdota con decimales;
  - la advertencia de confusión escrita en la respuesta. Los ejercicios no se
    hacen sueltos: se hacen dentro de una rutina, el mismo día que otros nueve.
    Un ranking de ejercicios NO puede separar el peso muerto del día en que toca
    peso muerto, y decirlo es parte del resultado, no una nota al pie.

Lo que esta vista no hace, otra vez, es tocar nada. Ninguna regla del motor mira
aquí. Si un ejercicio sale mal parado, la decisión de quitarlo la toma una
persona editando el `config.yaml`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import fmean, median
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis import series as S
from app.analysis.stats import (
    corregir_tanda,
    correlacion,
    emparejar,
    percentil,
)
from app.models import Activity, WorkoutLog

DIAS_DESPUES = (1, 2, 3)

# Cuántos días de exposición hacen falta para que una casilla se calcule. Es más
# exigente que el mínimo general de `stats` a propósito: allí `n` son días
# emparejados, y aquí un `n` de 30 puede esconder que solo 2 de esos 30 son días
# en que de verdad hizo el ejercicio. Un contraste entre 2 días y 28 no es un
# contraste.
N_MINIMO_EXPUESTOS = 3

# El cuarto superior y el inferior de sus propias salidas. "Larga" y "corta" no
# son 90 y 40 minutos: son lo que para él es largo y corto, que es lo único
# comparable consigo mismo y lo único que sigue significando lo mismo dentro de
# un año, cuando aguante más.
CUARTIL_ALTO = 75.0
CUARTIL_BAJO = 25.0


@dataclass(frozen=True)
class Exposicion:
    """Algo que pasó un día y de lo que se quiere saber la resaca."""

    clave: str
    etiqueta: str
    tipo: str  # binaria | continua
    familia: str  # rutina | bici | fuerza | ejercicio

    def como_dict(self) -> dict[str, Any]:
        return {
            "clave": self.clave,
            "etiqueta": self.etiqueta,
            "tipo": self.tipo,
            "familia": self.familia,
        }


# ---------------------------------------------------------------------------
# De la base a series de exposición
# ---------------------------------------------------------------------------


def _dias_con_fuerza(session: Session, desde: date, hasta: date) -> list[WorkoutLog]:
    return list(
        session.scalars(
            select(WorkoutLog).where(WorkoutLog.date >= desde, WorkoutLog.date <= hasta)
        )
    )


def rutinas_por_dia(
    session: Session, desde: date, hasta: date
) -> dict[date, set[str]]:
    """Qué rutina(s) se entrenaron cada día."""
    salida: dict[date, set[str]] = {}
    for w in _dias_con_fuerza(session, desde, hasta):
        if w.routine_key:
            salida.setdefault(S.a_fecha(w.date), set()).add(w.routine_key)
    return salida


def ejercicios_por_dia(
    session: Session, desde: date, hasta: date
) -> tuple[dict[date, set[str]], dict[str, str]]:
    """Qué ejercicios se hicieron cada día, sacados del crudo de Hevy.

    Del crudo y no de `exercise_targets` a propósito: los `targets` dicen lo que
    el motor PLANEA, y esta vista pregunta por lo que el cuerpo AGUANTÓ. Un
    ejercicio que estaba en el plan y no se hizo no puede contar como hecho, que
    es justo el error que haría que un ejercicio saliera correlacionado con una
    molestia los días que se saltó por esa molestia.

    Devuelve también los nombres, que salen del propio crudo. Se completan
    después con los del `config.yaml` cuando se conoce la plantilla.
    """
    por_dia: dict[date, set[str]] = {}
    nombres: dict[str, str] = {}
    for w in _dias_con_fuerza(session, desde, hasta):
        if not w.raw_json:
            continue
        try:
            crudo = json.loads(w.raw_json)
        except (ValueError, TypeError):
            # Un crudo ilegible es un entrenamiento que no se puede desglosar. Se
            # salta, y no se cuenta como día sin ejercicios: eso diría que ese
            # día no hizo nada.
            continue
        dia = S.a_fecha(w.date)
        for ex in crudo.get("exercises") or []:
            clave = ex.get("exercise_template_id") or ex.get("title")
            if not clave:
                continue
            clave = str(clave)
            por_dia.setdefault(dia, set()).add(clave)
            if ex.get("title"):
                nombres.setdefault(clave, str(ex["title"]))
    return por_dia, nombres


def _nombres_del_config(cfg: Any) -> dict[str, str]:
    """`template_id` -> nombre legible, del YAML.

    El crudo de Hevy trae el título que Hevy le pone al ejercicio; el YAML trae
    el que él le puso. Gana el del YAML cuando existe, porque es el nombre con
    el que se piensa la rutina.
    """
    raw = cfg.raw if hasattr(cfg, "raw") else (cfg or {})
    salida: dict[str, str] = {}
    for rutina in (raw.get("routines") or {}).values():
        for ex in rutina.get("exercises") or []:
            if ex.get("template_id") and ex.get("name"):
                salida[str(ex["template_id"])] = str(ex["name"])
    return salida


def _binaria(
    presentes: dict[date, set[str]],
    clave: str,
    desde: date,
    hasta: date,
    ventana: tuple[date, date] | None,
    sin_dato: set[date] | None = None,
) -> dict[date, float | None]:
    """1 los días que pasó, 0 los que no, y nada fuera de lo observado.

    El cero es la mitad del contraste: sin los días en que NO se hizo el
    ejercicio no hay con qué comparar los días en que sí. Pero solo dentro de la
    ventana observada -antes de que Hevy estuviera conectado, un cero diría "ese
    día no lo hizo" cuando lo que pasa es que no hay registro-.

    `sin_dato` es ese mismo argumento a nivel de día suelto en vez de a nivel de
    ventana entera. Un día con salida pero sin duración conocida no es un 0 en
    `bici_larga`: el 0 diría "ese día no rodó largo" cuando lo que pasa es que no
    consta cuánto rodó. Se queda fuera del contraste, igual que los días de antes
    de la ventana, y por la misma razón exacta.
    """
    fuera = sin_dato or frozenset()
    salida: dict[date, float | None] = {}
    d = desde
    while d <= hasta:
        if ventana is None or d < ventana[0] or d > ventana[1] or d in fuera:
            salida[d] = None
        else:
            salida[d] = 1.0 if clave in presentes.get(d, ()) else 0.0
        d += timedelta(days=1)
    return salida


def _salidas_por_dia(
    session: Session, desde: date, hasta: date
) -> dict[date, list[Activity]]:
    filas = session.scalars(
        select(Activity).where(
            Activity.date >= desde,
            Activity.date <= hasta,
            Activity.is_cycling.is_(True),
        )
    )
    salida: dict[date, list[Activity]] = {}
    for a in filas:
        salida.setdefault(S.a_fecha(a.date), []).append(a)
    return salida


def _minutos(acts: list[Activity]) -> float | None:
    """Los minutos que pedaleó ese día, o `None` si no se sabe.

    Basta que UNA de las salidas del día no traiga duración para que el total del
    día sea desconocido. Sumar solo las que sí la traen no es una aproximación
    prudente: es afirmar "ese día rodó 60 minutos" cuando rodó 60 y algo más.

    Era `sum((a.duration_s or 0.0) for a in acts) / 60.0`, y ese `or 0.0` hacía
    las dos cosas que más daño hacen en esta vista a la vez. Bajaba el umbral del
    cuartil superior metiendo ceros en la distribución de la que sale, y además
    marcaba el día como `bici_corta` -una salida sin duración caía siempre en el
    cuarto inferior-. El resultado era una frase concreta y falsa sobre su propio
    histórico: "los días de salida corta duermes peor", construida con días en los
    que la salida pudo ser la más larga del mes.
    """
    total = 0.0
    for a in acts:
        if a.duration_s is None:
            return None
        total += a.duration_s
    return total / 60.0


def exposiciones_de_bici(
    session: Session, desde: date, hasta: date, cob: S.Cobertura
) -> tuple[list[Exposicion], dict[str, dict[date, float | None]]]:
    """Las salidas por intensidad de Garmin y por duración propia.

    `intensity_level` sale de la clasificación por zonas que ya hace el sistema:
    no se vuelve a clasificar aquí. Si se clasificara otra vez, esta vista podría
    llamar intensa a una salida que el motor llamó media, y las dos cosas
    estarían en la misma pantalla contradiciéndose.
    """
    por_dia = _salidas_por_dia(session, desde, hasta)
    ventana = cob.bici

    # El umbral de "larga" sale de SUS salidas de la ventana, no de una constante.
    # Los días sin duración conocida no entran en la distribución: un cero ahí
    # tiraría del cuartil superior hacia abajo y convertiría en "larga" una salida
    # que no lo es, para todos los demás días.
    minutos = [
        m for m in (_minutos(acts) for acts in por_dia.values()) if m is not None
    ]
    p_alto = percentil(minutos, CUARTIL_ALTO) if minutos else None
    p_bajo = percentil(minutos, CUARTIL_BAJO) if minutos else None

    niveles = {"suave", "media", "intensa"}
    presentes: dict[date, set[str]] = {}
    # Los días con salida pero sin duración conocida. No van a `presentes` -no
    # son largos ni cortos- pero tampoco pueden quedarse en el 0 por omisión, así
    # que se apuntan aparte y salen del contraste de duración. Solo de ese: la
    # intensidad de Garmin sí se sabe, y esos contrastes siguen contando el día.
    sin_duracion: set[date] = set()
    for d, acts in por_dia.items():
        marcas = presentes.setdefault(d, set())
        marcas.add("bici_cualquiera")
        for a in acts:
            if a.intensity_level in niveles:
                marcas.add(f"bici_{a.intensity_level}")
        mins = _minutos(acts)
        if mins is None:
            sin_duracion.add(d)
        elif p_alto is not None:
            if mins >= p_alto:
                marcas.add("bici_larga")
            # `p_bajo < p_alto` porque si todas sus salidas duran lo mismo los
            # dos cuartiles coinciden, y entonces cada salida sería a la vez
            # larga y corta: dos exposiciones idénticas con nombres contrarios.
            if p_bajo is not None and p_bajo < p_alto and mins <= p_bajo:
                marcas.add("bici_corta")

    defs = [
        Exposicion("bici_cualquiera", "Cualquier salida", "binaria", "bici"),
        Exposicion("bici_suave", "Salida suave (Garmin)", "binaria", "bici"),
        Exposicion("bici_media", "Salida media (Garmin)", "binaria", "bici"),
        Exposicion("bici_intensa", "Salida intensa (Garmin)", "binaria", "bici"),
    ]
    if p_alto is not None:
        defs.append(
            Exposicion(
                "bici_larga",
                f"Salida larga (tu cuarto superior, {p_alto:.0f} min o más)",
                "binaria",
                "bici",
            )
        )
    if p_bajo is not None:
        defs.append(
            Exposicion(
                "bici_corta",
                f"Salida corta (tu cuarto inferior, {p_bajo:.0f} min o menos)",
                "binaria",
                "bici",
            )
        )

    por_duracion = {"bici_larga", "bici_corta"}
    sers = {
        e.clave: _binaria(
            presentes,
            e.clave,
            desde,
            hasta,
            ventana,
            sin_duracion if e.clave in por_duracion else None,
        )
        for e in defs
    }
    return defs, sers


# ---------------------------------------------------------------------------
# El contraste
# ---------------------------------------------------------------------------


def contraste(
    exposicion: dict[date, float | None],
    respuesta: dict[date, float | None],
    *,
    dias: int,
    metodo: str = "spearman",
    binaria: bool = True,
) -> dict[str, Any]:
    """Una casilla: qué le pasa a `respuesta` `dias` después de `exposicion`.

    Devuelve la correlación de siempre -con su n, su p, su motivo cuando no se
    puede- y, si la exposición es de sí-o-no, además las dos medias.

    Las medias no son adorno ni redundancia. "r = 0.31, p = 0.04" no contesta
    "¿sube la molestia lumbar tras el Día 2?"; "2.1 los días después de otra cosa,
    2.8 los días después del Día 2" sí la contesta, y es el mismo dato. El número
    que se puede leer y el número que se puede defender tienen que ir juntos,
    porque si solo va el primero se exagera y si solo va el segundo no se mira.
    """
    pares = emparejar(exposicion, respuesta, desfase=dias)
    res = correlacion(pares, metodo=metodo)
    salida: dict[str, Any] = {"dias_despues": dias, **res.como_dict()}

    if not binaria:
        return salida

    con = [y for x, y in zip(pares.x, pares.y) if x >= 0.5]
    sin = [y for x, y in zip(pares.x, pares.y) if x < 0.5]
    salida["n_expuesto"] = len(con)
    salida["n_no_expuesto"] = len(sin)
    salida["media_expuesto"] = round(fmean(con), 3) if con else None
    salida["media_no_expuesto"] = round(fmean(sin), 3) if sin else None
    salida["mediana_expuesto"] = round(median(con), 3) if con else None
    salida["mediana_no_expuesto"] = round(median(sin), 3) if sin else None
    salida["diferencia"] = (
        round(fmean(con) - fmean(sin), 3) if con and sin else None
    )

    # Un `n` de sesenta con dos días expuestos no es una muestra de sesenta: es
    # una de dos. Si la correlación se calculó igual, se tacha aquí con su motivo
    # en vez de dejar un número que parece sólido.
    #
    # Pero solo se escribe el motivo si no había ya otro. Cuando no hay NINGÚN
    # par -porque falta la otra serie entera- los dos grupos salen a cero y este
    # mensaje diría "0 días con esto y 0 sin ello", señalando a la exposición
    # cuando el que falta es el otro lado. El motivo de `correlacion` es el de
    # más abajo en la cadena, y ese es el que hay que arreglar primero.
    if len(con) < N_MINIMO_EXPUESTOS or len(sin) < N_MINIMO_EXPUESTOS:
        salida["r"] = None
        salida["p"] = None
        salida["suficiente"] = False
        if not salida.get("na"):
            salida["na"] = (
                f"solo {len(con)} día(s) con esto y {len(sin)} sin ello; hacen falta "
                f"al menos {N_MINIMO_EXPUESTOS} de cada para poder compararlos"
            )
    return salida


def _lectura_recuperacion(casillas: list[dict[str, Any]], sentido: str) -> str | None:
    """"A los dos días ya no se nota", que es la pregunta de la salida intensa.

    "¿Cuántos días de HRV cuesta una salida INTENSA?" no se contesta con tres
    correlaciones en una fila: se contesta con un número de días. Así que se
    recorren los retardos en orden y se busca el primero donde el efecto cambia
    de signo o se queda en la cuarta parte.

    Si al final de la ventana todavía se nota, lo dice tal cual. No se extrapola
    un "se recupera en cuatro días" que no se ha medido: la ventana llega hasta
    donde llega, y decir hasta dónde se ha mirado es parte de la respuesta.
    """
    utiles = [
        c
        for c in casillas
        if c.get("diferencia") is not None and c.get("r") is not None
    ]
    if not utiles or utiles[0]["dias_despues"] != casillas[0]["dias_despues"]:
        # Si el retardo más corto no se pudo calcular, no hay "al día siguiente"
        # con el que empezar la frase, y empezarla en el +2 diría otra cosa.
        return None

    primero = utiles[0]
    if abs(primero["diferencia"]) < 1e-9:
        return None
    sube = primero["diferencia"] > 0

    direccion = "sube" if sube else "baja"
    if sentido == "alto_peor":
        valencia = " (peor)" if sube else " (mejor)"
    elif sentido == "alto_mejor":
        valencia = " (mejor)" if sube else " (peor)"
    else:
        valencia = ""

    for c in utiles[1:]:
        se_da_la_vuelta = (c["diferencia"] > 0) != sube
        se_apaga = abs(c["diferencia"]) < abs(primero["diferencia"]) * 0.25
        if se_da_la_vuelta or se_apaga:
            return (
                f"al día siguiente {direccion}{valencia}, y al día "
                f"+{c['dias_despues']} ya está como siempre"
            )

    ultimo = utiles[-1]["dias_despues"]
    return (
        f"{direccion}{valencia} al día siguiente, y al día +{ultimo} -hasta donde "
        f"llega esta ventana- todavía se nota"
    )


# ---------------------------------------------------------------------------
# La vista
# ---------------------------------------------------------------------------


def _respuestas() -> dict[str, S.Definicion]:
    """Contra qué se mide la resaca: los siete deslizadores y las cinco métricas."""
    return {**S.SLIDERS, **S.GARMIN}


def vista_impacto(
    session: Session,
    *,
    dias: int = 180,
    hoy: date | None = None,
    metodo: str = "spearman",
    retardos: tuple[int, ...] = DIAS_DESPUES,
) -> dict[str, Any]:
    """La rejilla entera: cada exposición contra cada respuesta, a +1, +2 y +3."""
    hoy = hoy or date.today()
    desde, hasta = hoy - timedelta(days=dias - 1), hoy
    cob = S.cobertura(session)

    # Las respuestas se piden con margen por el mismo motivo que en la vista 2:
    # el retardo +3 necesita días POSTERIORES al final de la ventana, y sin ellos
    # los últimos días de exposición se perderían siempre.
    margen = max(retardos) if retardos else 0
    respuestas = {
        clave: S.serie(session, clave, desde, hasta + timedelta(days=margen), cob=cob)
        for clave in _respuestas()
    }

    exposiciones: list[Exposicion] = []
    sers: dict[str, dict[date, float | None]] = {}

    # Rutinas de fuerza.
    rutinas = rutinas_por_dia(session, desde, hasta)
    for clave in sorted({r for v in rutinas.values() for r in v}):
        e = Exposicion(f"rutina_{clave}", f"Rutina {clave}", "binaria", "rutina")
        exposiciones.append(e)
        sers[e.clave] = _binaria(rutinas, clave, desde, hasta, cob.fuerza)

    # Bici.
    defs_bici, sers_bici = exposiciones_de_bici(session, desde, hasta, cob)
    exposiciones.extend(defs_bici)
    sers.update(sers_bici)

    # Continuas: volumen, series, carga, minutos y desnivel.
    for clave in ("volumen_fuerza", "series_fuerza", "carga_bici", "minutos_bici", "desnivel_bici"):
        d = S.DEFINICIONES[clave]
        e = Exposicion(
            clave,
            d.etiqueta,
            "continua",
            "fuerza" if clave.endswith("fuerza") else "bici",
        )
        exposiciones.append(e)
        sers[clave] = S.serie(session, clave, desde, hasta, cob=cob)

    rejilla = []
    for exp in exposiciones:
        for clave_r, d_r in _respuestas().items():
            casillas = [
                contraste(
                    sers[exp.clave],
                    respuestas[clave_r],
                    dias=k,
                    metodo=metodo,
                    binaria=exp.tipo == "binaria",
                )
                for k in retardos
            ]
            rejilla.append(
                {
                    "exposicion": exp.como_dict(),
                    "respuesta": {"clave": clave_r, **d_r.como_dict()},
                    "por_dia": casillas,
                    "lectura": (
                        _lectura_recuperacion(casillas, d_r.sentido)
                        if exp.tipo == "binaria"
                        else None
                    ),
                }
            )

    corregir_tanda([fila["por_dia"] for fila in rejilla])

    return {
        "vista": "impacto",
        "metodo": metodo,
        "retardos": list(retardos),
        "ventana": {"desde": desde.isoformat(), "hasta": hasta.isoformat(), "dias": dias},
        "cobertura": cob.como_dict(),
        "advertencia": ADVERTENCIA_CONFUSION,
        "rejilla": rejilla,
    }


ADVERTENCIA_CONFUSION = (
    "Los ejercicios no se hacen sueltos: se hacen dentro de una rutina, el mismo "
    "día que otros ocho o nueve. Esta vista NO puede separar un ejercicio del día "
    "en que toca ese ejercicio. Si el peso muerto sale correlacionado con la "
    "lumbar, lo que ha salido es que el día de peso muerto se nota en la lumbar, "
    "y el culpable puede ser cualquiera de los ejercicios de ese día -o el propio "
    "volumen del día-. Sirve para saber por dónde mirar, no para sentenciar."
)


def ranking_ejercicios(
    session: Session,
    cfg: Any = None,
    *,
    respuesta: str = "lower_discomfort",
    dias: int = 180,
    hoy: date | None = None,
    metodo: str = "spearman",
    retardos: tuple[int, ...] = DIAS_DESPUES,
) -> dict[str, Any]:
    """Los ejercicios ordenados por cómo se relacionan con una molestia después.

    Por defecto, la lumbar al día siguiente, que es lo que se pidió y lo que más
    importa con una hernia L4-L5 de por medio.

    El orden lo pone el retardo más corto -el +1-, y los otros van al lado sin
    reordenar: un ejercicio que se nota a los tres días y no al día siguiente
    existe, pero mezclarlo en el mismo orden convertiría el ranking en una lista
    donde el primer puesto no significa lo mismo para todas las filas.
    """
    if respuesta not in _respuestas():
        raise ValueError(
            f"respuesta desconocida: {respuesta!r}. Las que hay: "
            f"{sorted(_respuestas())}"
        )
    hoy = hoy or date.today()
    desde, hasta = hoy - timedelta(days=dias - 1), hoy
    cob = S.cobertura(session)
    d_r = _respuestas()[respuesta]

    margen = max(retardos) if retardos else 0
    serie_r = S.serie(session, respuesta, desde, hasta + timedelta(days=margen), cob=cob)

    por_dia, nombres = ejercicios_por_dia(session, desde, hasta)
    nombres = {**nombres, **_nombres_del_config(cfg)} if cfg is not None else nombres
    claves = sorted({c for v in por_dia.values() for c in v})

    filas = []
    for clave in claves:
        exp = _binaria(por_dia, clave, desde, hasta, cob.fuerza)
        casillas = [
            contraste(exp, serie_r, dias=k, metodo=metodo, binaria=True)
            for k in retardos
        ]
        veces = sum(1 for v in exp.values() if v == 1.0)
        filas.append(
            {
                "clave": clave,
                "etiqueta": nombres.get(clave, clave),
                "veces_hecho": veces,
                "por_dia": casillas,
            }
        )

    corregir_tanda([f["por_dia"] for f in filas])

    primero = retardos[0] if retardos else 1

    def orden(f: dict[str, Any]) -> tuple[int, float]:
        c = next((c for c in f["por_dia"] if c["dias_despues"] == primero), None)
        if c is None or c.get("r") is None:
            return (1, 0.0)  # las incalculables, al final, pero SIN esconderlas
        # Peor primero: lo que más sube la molestia arriba del todo si la
        # respuesta es de las que alto es peor, y al revés si alto es mejor.
        signo = -1 if d_r.sentido == "alto_mejor" else 1
        return (0, -signo * c["r"])

    filas.sort(key=orden)

    return {
        "vista": "ranking_ejercicios",
        "metodo": metodo,
        "respuesta": {"clave": respuesta, **d_r.como_dict()},
        "retardos": list(retardos),
        "ordenado_por": f"correlación a +{primero} día(s)",
        "ventana": {"desde": desde.isoformat(), "hasta": hasta.isoformat(), "dias": dias},
        "cobertura": cob.como_dict(),
        "advertencia": ADVERTENCIA_CONFUSION,
        "n_ejercicios": len(filas),
        "ranking": filas,
    }
