"""Vista 4: auditoría del semáforo. Qué decidió, por qué, y qué no decidió nunca.

Las vistas 1, 2 y 3 miran su cuerpo. Esta mira el MOTOR. Es la única de las cinco
cuyo sujeto es el propio sistema, y la única que puede terminar en un cambio del
`config.yaml`.

Contesta cuatro cosas:

  - qué color salió cada día y con qué deslizadores delante;
  - cuántas veces disparó cada regla, y cuántas de esas fue LA que mandó. Las
    dos cifras por separado, porque una regla que dispara a diario pero nunca
    decide nada es ruido con nombre;
  - qué reglas no han disparado NUNCA. Es lo que se pidió explícitamente, y lo
    que más cuesta ver de otra forma: una regla que no salta no aparece en
    ningún sitio, no da ningún error, y puede llevar meses sin hacer nada;
  - cómo ha ido subiendo cada ejercicio, con la marca de cuándo subió y por qué,
    y de cuándo se frenó y por qué.

LA DISTINCIÓN QUE SALVA ESTA VISTA
----------------------------------
"Nunca ha disparado" son dos cosas distintas que se parecen mucho en una tabla:

  - la regla se evaluó ciento ochenta días y nunca se cumplió. Esa es la que él
    describió: o está mal calibrada o sobra, y la decisión es suya;
  - la regla no se pudo evaluar nunca porque le faltaba un dato. Esa no está mal
    calibrada: está CIEGA, y tocarle el umbral no la arreglaría, solo la movería
    de sitio sin que siguiera sin dispararse jamás.

Juntarlas en un "nunca disparó" mandaría a recalibrar una regla que no ha llegado
a ejecutarse ni una vez. Por eso `skipped_rules_json` -que el motor ya guardaba-
es la columna más importante de esta vista.

Y una tercera, que solo se ve mirando el catálogo de hoy contra el histórico: una
regla que disparó en su momento y que ya no está declarada. No es que no
dispare; es que se la llevaron por delante en alguna edición del YAML, y las
veces que disparó siguen contando en un total que ya no significa lo mismo.

Nada de aquí toca nada. Si una regla sobra, la quita una persona editando el
`config.yaml`.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis import series as S
from app.engine.sets import warmup_flags
from app.models import Decision, RuleState

LUCES = ("green", "amber", "red")

# Cómo se llama cada color en la pantalla. Va desde el servidor por lo mismo que
# las etiquetas de las series: si la PWA lleva su propia tabla, el día que se
# añada un cuarto color la pantalla dirá "undefined" y no fallará nada.
NOMBRE_LUZ = {"green": "Verde", "amber": "Ámbar", "red": "Rojo"}


# ---------------------------------------------------------------------------
# Los días
# ---------------------------------------------------------------------------


def _decisiones(session: Session, desde: date, hasta: date) -> dict[date, Decision]:
    """La decisión vigente de cada día. `is_current` manda.

    La tabla es append-only: un día recalculado tiene varias filas y solo una
    vigente. Contar todas inflaría los contadores de reglas con los ensayos.
    """
    filas = session.scalars(
        select(Decision).where(
            Decision.date >= desde,
            Decision.date <= hasta,
            Decision.is_current.is_(True),
        )
    )
    return {S.a_fecha(f.date): f for f in filas}


def _carga(crudo: str | None, por_defecto: Any) -> Any:
    if not crudo:
        return por_defecto
    try:
        return json.loads(crudo)
    except (ValueError, TypeError):
        return por_defecto


def dias_de_luz(session: Session, desde: date, hasta: date) -> list[dict[str, Any]]:
    """Un registro por día NATURAL, incluidos los días en que no decidió nadie.

    Los días sin fila salen con la luz a `None` y el motivo escrito. Es la
    diferencia entre "ese día estuvo en verde" y "ese día el PC estaba apagado",
    y en una auditoría del semáforo confundirlas sería contar como aciertos los
    días en que no hubo semáforo.

    Los deslizadores que se devuelven son los del SNAPSHOT, no los de la tabla de
    check-ins: son los que el motor tenía delante cuando decidió. Un check-in
    rellenado por la tarde, después de una decisión de las nueve, no estuvo en
    esa decisión, y pintarlo encima diría que sí.
    """
    filas = _decisiones(session, desde, hasta)
    salida: list[dict[str, Any]] = []
    d = desde
    while d <= hasta:
        fila = filas.get(d)
        if fila is None:
            salida.append(
                {
                    "fecha": d.isoformat(),
                    "luz": None,
                    "nombre_luz": None,
                    "na": "no hay decisión guardada para este día",
                    "regla_determinante": None,
                    "reglas_disparadas": [],
                    "reglas_saltadas": [],
                    "fuente": None,
                    "config_hash": None,
                    "sliders": {},
                }
            )
            d += timedelta(days=1)
            continue

        snapshot = _carga(fila.inputs_snapshot_json, {}) or {}
        valores = snapshot.get("values") or {}
        salida.append(
            {
                "fecha": d.isoformat(),
                "luz": fila.light,
                "nombre_luz": NOMBRE_LUZ.get(fila.light, fila.light),
                "na": None,
                "regla_determinante": fila.trigger_rule,
                "reglas_disparadas": _carga(fila.fired_rules_json, []) or [],
                "reglas_saltadas": _carga(fila.skipped_rules_json, []) or [],
                "fuente": fila.source,
                "config_hash": fila.config_hash,
                # Solo los deslizadores, que es lo que se pinta encima del
                # calendario. El resto del snapshot son señales derivadas.
                "sliders": {
                    k: valores[k] for k in S.SLIDERS if valores.get(k) is not None
                },
            }
        )
        d += timedelta(days=1)
    return salida


def distribucion(dias: list[dict[str, Any]]) -> dict[str, Any]:
    """Verde, ámbar y rojo, en total y por semana.

    Los días sin decisión se cuentan aparte y NO entran en el denominador de los
    porcentajes: un mes con el PC apagado la mitad del tiempo daría un "60% de
    días verdes" que en realidad es "60% de los días que se miraron".
    """

    def contar(grupo: list[dict[str, Any]]) -> dict[str, Any]:
        c = Counter(d["luz"] for d in grupo if d["luz"])
        n = sum(c.values())
        return {
            "n": n,
            "sin_decision": sum(1 for d in grupo if d["luz"] is None),
            **{luz: c.get(luz, 0) for luz in LUCES},
            "porcentaje": (
                {luz: round(100.0 * c.get(luz, 0) / n, 1) for luz in LUCES}
                if n
                else None
            ),
        }

    semanas: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for d in dias:
        f = date.fromisoformat(d["fecha"])
        iso = f.isocalendar()
        semanas.setdefault((iso.year, iso.week), []).append(d)

    return {
        "global": contar(dias),
        "por_semana": [
            {
                "anio": anio,
                "semana": semana,
                "desde": min(d["fecha"] for d in grupo),
                "hasta": max(d["fecha"] for d in grupo),
                **contar(grupo),
            }
            for (anio, semana), grupo in sorted(semanas.items())
        ],
    }


# ---------------------------------------------------------------------------
# Las reglas
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Regla:
    nombre: str
    luz: str | None
    descripcion: str | None
    declarada: bool


def _catalogo(cfg: Any) -> dict[str, Regla]:
    """Las reglas que el `config.yaml` declara HOY."""
    salida: dict[str, Regla] = {}
    for r in cfg.all_rules():
        nombre = r.get("name")
        if not nombre:
            continue
        salida[nombre] = Regla(
            nombre=nombre,
            luz=r.get("light"),
            descripcion=r.get("description"),
            declarada=True,
        )
    return salida


def auditoria_reglas(
    cfg: Any, dias: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Cada regla con sus dos contadores, su estado y su frase.

    Devuelve la lista entera y, aparte, las que no han disparado nunca, que es
    la que se pidió por su nombre. La segunda es un subconjunto de la primera y
    no una lista distinta: nada se calcula dos veces y nada puede discrepar.
    """
    catalogo = _catalogo(cfg)

    disparos: Counter[str] = Counter()
    determinantes: Counter[str] = Counter()
    evaluadas: Counter[str] = Counter()
    saltadas: Counter[str] = Counter()
    faltas: dict[str, Counter[str]] = {}
    ultima: dict[str, str] = {}

    dias_con_decision = 0
    for d in dias:
        if d["luz"] is None:
            continue
        dias_con_decision += 1
        nombres_saltados = set()
        for s in d["reglas_saltadas"]:
            nombre = s.get("name") if isinstance(s, dict) else str(s)
            if not nombre:
                continue
            nombres_saltados.add(nombre)
            saltadas[nombre] += 1
            for m in (s.get("missing") or []) if isinstance(s, dict) else []:
                faltas.setdefault(nombre, Counter())[str(m)] += 1

        # Una regla se evaluó ese día si existía y no se saltó. Las del catálogo
        # se recorren siempre; las que solo aparecen en el histórico no se pueden
        # contar como evaluadas los días en que ni siquiera estaban declaradas.
        for nombre in catalogo:
            if nombre not in nombres_saltados:
                evaluadas[nombre] += 1

        for nombre in d["reglas_disparadas"]:
            disparos[str(nombre)] += 1
            ultima[str(nombre)] = d["fecha"]
        if d["regla_determinante"]:
            determinantes[str(d["regla_determinante"])] += 1

    # El histórico puede nombrar reglas que ya no están declaradas.
    vistas = set(catalogo) | set(disparos) | set(determinantes) | set(saltadas)

    filas: list[dict[str, Any]] = []
    for nombre in sorted(vistas):
        r = catalogo.get(nombre) or Regla(nombre, None, None, declarada=False)
        n_disparos = disparos.get(nombre, 0)
        n_evaluada = evaluadas.get(nombre, 0)
        n_saltada = saltadas.get(nombre, 0)

        if dias_con_decision == 0:
            # Ni un día con decisión guardada. Ninguna regla ha llegado a
            # ejecutarse, así que no se puede decir de ninguna que esté mal
            # calibrada ni que esté ciega: no ha habido ocasión. Decir "le falta
            # un dato" aquí mandaría a revisar el reloj cuando lo que pasa es que
            # el motor todavía no ha decidido nada.
            estado = "sin_historico"
            lectura = (
                "no hay ni un día con decisión guardada en esta ventana: el motor "
                "no ha llegado a evaluar esta regla ninguna vez"
            )
        elif not r.declarada:
            estado = "retirada"
            lectura = (
                f"disparó {n_disparos} vez/veces en esta ventana, pero YA NO está "
                f"declarada en el config.yaml: el contador habla de una regla que "
                f"ya no existe"
            )
        elif n_disparos:
            estado = "dispara"
            mandas = determinantes.get(nombre, 0)
            if mandas == 0:
                lectura = (
                    f"disparó {n_disparos} día(s) y no mandó en ninguno: siempre "
                    f"hubo otra por delante"
                )
            else:
                lectura = f"disparó {n_disparos} día(s) y mandó en {mandas}"
        elif n_evaluada == 0:
            estado = "nunca_evaluada"
            que_falta = ", ".join(
                f"{k} ({v} día(s))" for k, v in faltas.get(nombre, Counter()).most_common(3)
            )
            lectura = (
                f"NO se ha podido evaluar ni un solo día: le falta "
                f"{que_falta or 'algún dato'}. No es calibración, es que está ciega"
            )
        else:
            estado = "nunca_disparo"
            lectura = (
                f"se evaluó {n_evaluada} día(s) y no disparó ninguno: o está mal "
                f"calibrada o sobra"
            )

        filas.append(
            {
                "nombre": nombre,
                "luz": r.luz,
                "nombre_luz": NOMBRE_LUZ.get(r.luz or "", None),
                "descripcion": r.descripcion,
                "declarada": r.declarada,
                "veces_disparada": n_disparos,
                "veces_determinante": determinantes.get(nombre, 0),
                "dias_evaluada": n_evaluada,
                "veces_saltada": n_saltada,
                "le_falto": dict(faltas.get(nombre, Counter())),
                "ultima_vez": ultima.get(nombre),
                "estado": estado,
                "lectura": lectura,
            }
        )

    # Las que no dispararon nunca, separando las dos formas de no disparar.
    #
    # `sin_historico` NO entra aquí, y esa exclusión es el punto: esta lista se
    # lee bajo el rótulo "o están mal calibradas o sobran", y con la base recién
    # estrenada metería las trece reglas debajo de esa frase. Seguirían todas en
    # `reglas` con su estado, así que no se esconde ninguna; lo que no se hace es
    # acusarlas de un defecto que todavía no puede saberse.
    nunca = [f for f in filas if f["estado"] in ("nunca_disparo", "nunca_evaluada")]
    return filas, nunca


def reglas_especiales(
    session: Session, cfg: Any, desde: date, hasta: date
) -> list[dict[str, Any]]:
    """Las de efecto prolongado, que no son un color del día sino un estado.

    Se cuentan por ACTIVACIONES y no por días activos: una retirada de peso
    muerto de catorce días es un suceso, no catorce. Contarla por días la
    pondría catorce veces por delante de una regla que saltó tres veces en tres
    meses, y la lectura sería que es catorce veces más importante.
    """
    declaradas = [
        r.get("name") for r in (cfg.raw.get("special_rules") or []) if r.get("name")
    ]
    filas = list(
        session.scalars(
            select(RuleState).where(
                RuleState.active_from <= hasta,
                # Una regla sin fin sigue activa: no se le puede pedir que haya
                # terminado antes del principio de la ventana para contarla.
                (RuleState.active_until.is_(None)) | (RuleState.active_until >= desde),
            )
        )
    )

    por_nombre: dict[str, list[RuleState]] = {}
    for f in filas:
        por_nombre.setdefault(f.rule_name, []).append(f)

    salida: list[dict[str, Any]] = []
    for nombre in sorted(set(declaradas) | set(por_nombre)):
        activaciones = por_nombre.get(nombre, [])
        declarada = nombre in declaradas
        if activaciones:
            lectura = (
                f"se activó {len(activaciones)} vez/veces; la última el "
                f"{max(S.a_fecha(a.active_from) for a in activaciones).isoformat()}"
            )
        elif declarada:
            lectura = "no se ha activado ni una vez en esta ventana"
        else:
            lectura = "se activó alguna vez pero ya no está declarada en el config.yaml"
        salida.append(
            {
                "nombre": nombre,
                "declarada": declarada,
                "veces_activada": len(activaciones),
                "activaciones": [
                    {
                        "desde": S.a_fecha(a.active_from).isoformat(),
                        "hasta": (
                            S.a_fecha(a.active_until).isoformat()
                            if a.active_until
                            else None
                        ),
                        "entidad": a.entity,
                        "motivo": a.reason,
                    }
                    for a in sorted(activaciones, key=lambda a: a.active_from)
                ],
                "lectura": lectura,
            }
        )
    return salida


def recalibraciones(dias: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Los días en que cambió el `config.yaml`, sacados del hash de cada decisión.

    Sin esto, un contador de "disparó 4 veces en 180 días" mezcla cuatro disparos
    de una regla que a lo mejor tuvo tres umbrales distintos en ese tiempo. No se
    puede corregir automáticamente -no se guarda el YAML entero de cada día- pero
    sí se puede decir dónde está la costura, que es lo que hace que el número se
    lea con la cabeza puesta.
    """
    salida: list[dict[str, Any]] = []
    anterior: str | None = None
    for d in dias:
        actual = d["config_hash"]
        if actual is None:
            continue
        if anterior is not None and actual != anterior:
            salida.append({"fecha": d["fecha"], "de": anterior, "a": actual})
        anterior = actual
    return salida


# ---------------------------------------------------------------------------
# La progresión de cada ejercicio
# ---------------------------------------------------------------------------


def _series_del_ejercicio(ex: dict[str, Any], set_cfg: dict[str, Any]) -> dict[str, Any]:
    sets = ex.get("sets") or []
    flags = warmup_flags(sets, set_cfg, ex.get("key"))
    pesos = [s.get("weight_kg") for s in sets if s.get("weight_kg") is not None]
    return {
        "series_totales": len(sets),
        "series_efectivas": sum(1 for f in flags if not f),
        # El tope y no la media: la rampa 40/40/45 progresa cuando sube el 45, y
        # una media escondería esa subida detrás de dos series que no se movieron.
        "peso_max_kg": max(pesos) if pesos else None,
        "reps": [s.get("reps") for s in sets],
    }


def puertas_de_progresion(
    session: Session, desde: date, hasta: date
) -> list[dict[str, Any]]:
    """Los días en que NO progresó nadie, y por qué.

    Cuando la puerta se cierra, el motor le pone el motivo a cada ejercicio por
    separado (`blocked_by = gate_reason`), así que en la ficha de cada uno ya
    sale. Pero mirando ejercicio por ejercicio no se ve lo que de verdad pasó:
    que ese día no subió NINGUNO y por el mismo motivo. Doce fichas con doce
    frenos parecen doce problemas; son uno.

    Las dos puertas van separadas porque lo son en el motor: añadir una serie
    efectiva no es el mismo riesgo que sumar dos repeticiones.
    """
    salida: list[dict[str, Any]] = []
    for d, fila in sorted(_decisiones(session, desde, hasta).items()):
        prog = _carga(fila.progression_json, {}) or {}
        if not prog:
            continue
        if prog.get("gate_open", True) and prog.get("sets_allowed", True) and prog.get(
            "reps_allowed", True
        ):
            continue
        salida.append(
            {
                "fecha": d.isoformat(),
                "rutina": prog.get("routine"),
                "luz": fila.light,
                "puerta_abierta": bool(prog.get("gate_open", True)),
                "motivo": prog.get("gate_reason"),
                "series_permitidas": bool(prog.get("sets_allowed", True)),
                "motivo_series": prog.get("sets_reason"),
                "reps_permitidas": bool(prog.get("reps_allowed", True)),
                "motivo_reps": prog.get("reps_reason"),
            }
        )
    return salida


def progresion_ejercicios(
    session: Session, cfg: Any, desde: date, hasta: date
) -> list[dict[str, Any]]:
    """Cada ejercicio: su carga día a día, y las marcas de por qué se movió o no.

    La carga que se dibuja es LA QUE SE PRESCRIBIÓ ese día, no el objetivo
    guardado. Los dos números existen y no son el mismo: en un día ámbar o en
    semana de descarga se escribe menos de lo que manda el objetivo, y dibujar el
    objetivo taparía precisamente los frenos que esta vista existe para enseñar.

    Los hitos salen de `progression_json`, que el motor ya guardaba con el porqué
    dentro. No se reconstruyen comparando días consecutivos: una carga que baja y
    vuelve a subir puede ser un ámbar, una descarga o un cambio manual, y
    adivinar cuál de las tres fue es exactamente lo que produce una auditoría que
    se inventa la historia.
    """
    set_cfg = (cfg.raw.get("set_types") or {}) if hasattr(cfg, "raw") else {}
    filas = _decisiones(session, desde, hasta)

    puntos: dict[tuple[str, str], list[dict[str, Any]]] = {}
    hitos: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for d in sorted(filas):
        fila = filas[d]
        sesion = _carga(fila.planned_session_json, {}) or {}
        rutina = sesion.get("routine") or ""
        for ex in sesion.get("exercises") or []:
            clave = ex.get("key")
            if not clave:
                continue
            puntos.setdefault((rutina, str(clave)), []).append(
                {
                    "fecha": d.isoformat(),
                    "luz": fila.light,
                    **_series_del_ejercicio(ex, set_cfg),
                }
            )

        prog = _carga(fila.progression_json, {}) or {}
        rutina_prog = prog.get("routine") or rutina
        for ex in prog.get("exercises") or []:
            clave = ex.get("key")
            if not clave:
                continue
            destino = hitos.setdefault((rutina_prog, str(clave)), [])
            if ex.get("changed"):
                destino.append(
                    {
                        "fecha": d.isoformat(),
                        "tipo": "subida",
                        "que": ex.get("what"),
                        "por_que": ex.get("why"),
                        "modo": ex.get("mode"),
                    }
                )
                continue
            # Un ejercicio que no sube tiene SIEMPRE un motivo, y los motivos no
            # son intercambiables: un techo se arregla cambiando el ejercicio y
            # un dato que falta se arregla apuntando el peso en Hevy.
            if ex.get("at_ceiling"):
                freno, detalle = "techo", "techo alcanzado: toca cambiar el ejercicio"
            elif ex.get("needs_data"):
                freno, detalle = "falta_dato", "falta la carga apuntada en Hevy"
            elif ex.get("blocked_by"):
                freno, detalle = "puerta", str(ex["blocked_by"])
            elif ex.get("waiting"):
                freno, detalle = "racha", str(ex["waiting"])
            else:
                continue
            destino.append(
                {
                    "fecha": d.isoformat(),
                    "tipo": "freno",
                    "motivo": freno,
                    "por_que": detalle,
                }
            )

    salida = []
    for clave in sorted(set(puntos) | set(hitos)):
        rutina, ejercicio = clave
        marcas = sorted(hitos.get(clave, []), key=lambda h: h["fecha"])
        subidas = [h for h in marcas if h["tipo"] == "subida"]
        frenos = [h for h in marcas if h["tipo"] == "freno"]
        p = puntos.get(clave, [])
        salida.append(
            {
                "rutina": rutina,
                "ejercicio": ejercicio,
                "dias_prescrito": len(p),
                "puntos": p,
                "subidas": subidas,
                "frenos": frenos,
                "lectura": _lectura_progresion(p, subidas, frenos),
            }
        )
    return salida


def _lectura_progresion(
    puntos: list[dict[str, Any]],
    subidas: list[dict[str, Any]],
    frenos: list[dict[str, Any]],
) -> str:
    """La frase que dice si este ejercicio está vivo o lleva meses parado."""
    if not puntos:
        return "no se ha prescrito ni un día en esta ventana"
    if subidas:
        ultima = subidas[-1]["fecha"]
        return f"{len(subidas)} subida(s); la última el {ultima}"
    if frenos:
        motivos = Counter(f["motivo"] for f in frenos)
        principal, veces = motivos.most_common(1)[0]
        traduccion = {
            "techo": "está en su techo",
            "falta_dato": "le falta la carga apuntada en Hevy",
            "puerta": "la puerta de la progresión estaba cerrada",
            "racha": "todavía no ha completado la racha",
        }
        return (
            f"NO ha subido ni una vez en {len(puntos)} día(s) prescrito(s): "
            f"{traduccion.get(principal, principal)} ({veces} día(s))"
        )
    return (
        f"no ha subido ni una vez en {len(puntos)} día(s) prescrito(s), y no hay "
        f"ningún motivo apuntado: eso es un hueco del registro, no una explicación"
    )


def _lecturas(
    dias: list[dict[str, Any]],
    recalib: list[dict[str, Any]],
    puertas: list[dict[str, Any]],
    progresion: list[dict[str, Any]],
) -> dict[str, str | None]:
    """Por qué está vacía cada sección que puede salir vacía.

    Tres de estas listas se quedan a cero y una lista vacía no dice nada por sí
    sola: se pinta como un hueco en la pantalla y el que mira rellena el hueco
    con lo que se imagine. El problema es que aquí lo que se imagina uno y lo que
    pasa de verdad son OPUESTOS.

    "La puerta de la progresión no se cerró ningún día" es una buena noticia: el
    semáforo no ha frenado nada. "El motor no ha llegado a evaluar la puerta ni
    una vez" es que no hay nada que auditar. Las dos producen exactamente la
    misma lista vacía, y leer la primera cuando pasa la segunda es creerse que un
    sistema que no ha arrancado funciona perfectamente. Lo mismo con las
    recalibraciones y con la progresión de las fichas.

    Así que la lectura se escribe mirando primero si hubo decisiones, que es el
    eslabón de más abajo: si no las hubo, ninguna de las tres secciones puede
    decir nada de sí misma, y el motivo es ese y no el suyo propio.

    Devuelve `None` -y no una frase- cuando la sección SÍ trae contenido: ahí
    están los datos, que hablan solos.
    """
    con_decision = sum(1 for d in dias if d["luz"] is not None)

    def sin_motor(propio: str) -> str:
        if con_decision == 0:
            return (
                "no hay ni un día con decisión guardada en esta ventana: el motor "
                "no ha llegado a ejecutarse, así que esto no está vacío por lo que "
                "parece, sino porque todavía no hay nada que auditar"
            )
        return propio

    return {
        "recalibraciones": None
        if recalib
        else sin_motor(
            f"los {con_decision} día(s) con decisión se resolvieron con la misma "
            f"configuración: no ha habido ninguna recalibración en esta ventana"
        ),
        "puertas_cerradas": None
        if puertas
        else sin_motor(
            f"la puerta de la progresión no se cerró ni uno de los {con_decision} "
            f"día(s) con decisión: el semáforo no ha frenado la progresión"
        ),
        "progresion": None
        if progresion
        else sin_motor(
            f"hay {con_decision} día(s) con decisión pero ninguno guardó la sesión "
            f"prescrita: es un hueco del registro, no que no se haya entrenado"
        ),
    }


# ---------------------------------------------------------------------------
# La vista
# ---------------------------------------------------------------------------


def vista_auditoria(
    session: Session, cfg: Any, *, dias: int = 180, hoy: date | None = None
) -> dict[str, Any]:
    """Todo lo anterior en una respuesta, para que la PWA solo pinte."""
    hoy = hoy or date.today()
    desde, hasta = hoy - timedelta(days=dias - 1), hoy

    lista = dias_de_luz(session, desde, hasta)
    filas_reglas, nunca = auditoria_reglas(cfg, lista)
    recalib = recalibraciones(lista)
    puertas = puertas_de_progresion(session, desde, hasta)
    progresion = progresion_ejercicios(session, cfg, desde, hasta)

    return {
        "vista": "auditoria",
        "ventana": {"desde": desde.isoformat(), "hasta": hasta.isoformat(), "dias": dias},
        "cobertura": S.cobertura(session).como_dict(),
        "dias": lista,
        "distribucion": distribucion(lista),
        "reglas": filas_reglas,
        "nunca_dispararon": nunca,
        "reglas_especiales": reglas_especiales(session, cfg, desde, hasta),
        "recalibraciones": recalib,
        "puertas_cerradas": puertas,
        "progresion": progresion,
        # Lo que hay que pintar DEBAJO de cada sección que salga vacía, para que
        # un cero no se lea como la respuesta cuando es la ausencia de respuesta.
        "lecturas": _lecturas(lista, recalib, puertas, progresion),
    }
