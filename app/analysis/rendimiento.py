"""Vista 5: la percepción de la mañana frente a lo que de verdad salió.

QUÉ ES ESTO Y QUÉ NO ES
-----------------------
Es un espejo. No calibra el semáforo, no mueve una carga, no cambia la sesión
del día, no entra en ninguna regla. Ninguna función de este módulo escribe en
`exercise_targets`, ni en `rule_states`, ni en `decisions`. La única tabla que
toca es `session_performance`, y de esa tabla no lee nadie más que esta vista y
la línea de Telegram de la mañana siguiente.

Esa frontera no es estilo: es el requisito. Un espejo que empujara sería otra
cosa, y un contador que además decidiera dejaría de ser creíble justo el día en
que hace falta que lo sea.

POR QUÉ EXISTE
--------------
La distorsión va siempre en la misma dirección: sentirse incapaz de cosas que
luego se hacen perfectamente. Los números del entreno no opinan. Si la sesión
del martes se completó entera, con la carga que tocaba y con un esfuerzo
percibido normal, eso pasó, y lo que la mañana del martes anunciaba no lo
cambia. Este módulo se limita a poner las dos cosas una al lado de la otra y a
contar cuántas veces han ido en direcciones opuestas.

LOS COMPONENTES VAN SUELTOS, SIEMPRE
------------------------------------
`performance_index` es una mezcla y toda mezcla esconde de dónde viene. Un 75
puede ser cumplimiento perfecto con un esfuerzo altísimo, o cumplimiento
regular con un esfuerzo bajo, y son dos sesiones que no se parecen en nada. Por
eso cada pieza se guarda antes de promediarla y los números crudos de los que
sale cada pieza viajan en `components_json`: dentro de seis meses se tiene que
poder rehacer la cuenta sin depender de que este archivo siga existiendo tal
cual.

EL PERCENTIL ES CONTRA SU PROPIA HISTORIA
-----------------------------------------
No hay constantes de "buen rendimiento". Un índice de 62 no significa nada; lo
que significa algo es que 62 caiga por encima del 70% de sus sesiones. Por eso
todo lo que se compara se compara con percentiles sobre su propio histórico, y
por eso cada fila guarda CUÁNTAS sesiones había detrás cuando se calculó: la
misma fila con 18 sesiones detrás y con 200 dice cosas distintas, y recalcular
todas las filas cada noche daría un contador que cambia de valor sin que haya
pasado nada nuevo.

LA BICI NO TIENE POTENCIÓMETRO
------------------------------
Así que el esfuerzo se mide por frecuencia cardiaca relativa a SUS zonas, por
velocidad y por desnivel. Es peor que unos vatios y hay que decirlo en vez de
disimularlo, que es por lo que son tres columnas y no una.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis import series as S
from app.analysis.stats import percentil_de
from app.analysis.texto import cuantos, plural
from app.engine.sets import volume_kg
from app.models import Activity, Checkin, Decision, SessionPerformance, WorkoutLog

# Los deslizadores van de 0 a 10 (`static/app.js`: min="0" max="10"), y la API
# los valida con `ge=0, le=10`. Se escribe aquí porque de este rango depende la
# normalización entera: usar 1-10 -que es lo que parece a simple vista- metería
# un sesgo del 11% en todos los índices de percepción y nadie lo vería.
ESCALA_MIN = 0
ESCALA_MAX = 10

# Los seis del formulario que describen CÓMO SE ENCUENTRA. `yesterday_rpe` no
# está y no puede estarlo: habla del entreno de ayer, no del estado de hoy, y
# además es el ingrediente del componente de esfuerzo del OTRO lado de la
# comparación. Meterlo aquí sería correlacionar una cosa consigo misma.
PERCEPCION = (
    "fatigue",
    "mood",
    "upper_discomfort",
    "lower_discomfort",
    "sleep_quality",
    "training_desire",
)

# Con menos de cuatro de los seis, la media deja de describir la mañana y pasa
# a describir el trozo que se contestó. El formulario los pide todos salvo el
# RPE, así que que falten es una anomalía y no el caso normal.
MINIMO_SLIDERS = 4

# ---------------------------------------------------------------------------
# El umbral de la disociación
# ---------------------------------------------------------------------------
#
# Aprobado tal cual: "1 de cada 10-12 sesiones es la frecuencia correcta:
# bastante para que el contador crezca, raro para que el mensaje no se vuelva
# ruido".
#
# Las cuatro condiciones se exigen a la vez y cada una tapa un agujero distinto:
#
#   PERCEPCION_MALA   la mañana tiene que haber sido de las malas DE VERDAD,
#                     no simplemente peor que la media. Sin esto, cualquier día
#                     mediocre con una sesión buena dispararía el mensaje;
#   RENDIMIENTO_OK    la sesión tiene que haber salido al menos normal. "Me
#                     sentí fatal y entrené fatal" no es una distorsión: es una
#                     percepción acertada, y contarla como acierto del cuerpo
#                     sería exactamente la clase de mentira amable que no se
#                     quiere aquí;
#   HUECO_MINIMO      cuarenta puntos de percentil. Dos posiciones que se tocan
#                     no son una contradicción, son ruido de medida;
#   BASE_MINIMA       quince sesiones detrás. Un percentil sobre cinco sesiones
#                     es una ordenación de cinco cosas, no una distribución, y
#                     el contador arrancaría lleno de falsos positivos que luego
#                     no se pueden borrar porque la tabla es append-only.
PERCEPCION_MALA = 25.0
RENDIMIENTO_OK = 50.0
HUECO_MINIMO = 40.0
BASE_MINIMA = 15

FUERZA = "strength"
BICI = "bike"
# El HIIT es un tipo PROPIO y no fuerza con otro nombre. `_previas` reparte las
# distribuciones por tipo, así que meter un bloque de wall balls y burpees en la
# muestra de las sesiones de fuerza movería el percentil de la prensa por algo
# que no se parece en nada: otro volumen, otros pesos, otra duración. El
# percentil dejaría de decir "comparado con tus sesiones de fuerza" para decir
# "comparado con una mezcla", que es un número sin pregunta detrás.
HIIT = "hiit"

PERCEPCION_PEOR = "perception_worse"
PERCEPCION_MEJOR = "perception_better"
ALINEADO = "aligned"
SIN_DATO = "na"


def _limitar(v: float, bajo: float = 0.0, alto: float = 100.0) -> float:
    return max(bajo, min(alto, v))


def _media(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


# ---------------------------------------------------------------------------
# La percepción
# ---------------------------------------------------------------------------


def indice_percepcion(valores: dict[str, Any]) -> dict[str, Any]:
    """Los seis deslizadores de la mañana en un 0-100 donde 100 es "de lujo".

    El sentido de cada uno lo manda `series.DEFINICIONES`, que es la misma tabla
    que usan las vistas 1 a 3. Duplicarla aquí -aunque fuera un diccionario de
    seis líneas- dejaría abierta la puerta a que un día alguien cambie el
    sentido del ánimo en un sitio y no en el otro, y ese fallo no daría ningún
    error: daría una correlación con el signo cambiado.

    Los seis pesan igual. La alternativa -ponderar las ganas de entrenar por
    encima de las molestias, por ejemplo- sería decidir cuáles de sus síntomas
    cuentan y cuáles no, y eso no lo decide un archivo de código.
    """
    piezas: dict[str, Any] = {}
    usados: list[float] = []
    faltan: list[str] = []

    for clave in PERCEPCION:
        crudo = valores.get(clave)
        if crudo is None:
            faltan.append(clave)
            piezas[clave] = None
            continue
        v = float(crudo)
        sentido = S.DEFINICIONES[clave].sentido
        bruto = (v - ESCALA_MIN) / (ESCALA_MAX - ESCALA_MIN) * 100.0
        # `alto_peor` se invierte para que el índice signifique SIEMPRE lo mismo:
        # más alto, mejor mañana. Sin invertir, un día de mucho dolor sumaría
        # como un día de mucho ánimo.
        bueno = bruto if sentido == "alto_mejor" else 100.0 - bruto
        piezas[clave] = {"valor": v, "sentido": sentido, "normalizado": round(bueno, 2)}
        usados.append(bueno)

    if len(usados) < MINIMO_SLIDERS:
        return {
            "indice": None,
            "piezas": piezas,
            "n_sliders": len(usados),
            "na": (
                f"solo {len(usados)} de los {len(PERCEPCION)} deslizadores están "
                f"contestados (faltan: {', '.join(faltan)}); con menos de "
                f"{MINIMO_SLIDERS} la media describe el trozo que se rellenó, no "
                f"la mañana"
            ),
        }

    return {
        "indice": round(sum(usados) / len(usados), 2),
        "piezas": piezas,
        "n_sliders": len(usados),
        "na": None,
    }


# ---------------------------------------------------------------------------
# El rendimiento en fuerza
# ---------------------------------------------------------------------------


@dataclass
class _Plan:
    """`_emparejar` pide un objeto con `.exercises`, y aquí hay un diccionario.

    Se usa el emparejador del motor y no uno propio a propósito: de él cuelga
    también lo que el motor considera sesión limpia, y dos emparejamientos
    distintos podrían decir "este ejercicio no aparece" en una vista y "se hizo
    a 70 kg" en la otra. Esa contradicción, en la vista que existe para ser un
    dato objetivo, sería el peor sitio posible donde tenerla.
    """

    exercises: list[dict[str, Any]]


def cumplimiento(
    entreno: dict[str, Any], plan: dict[str, Any], cfg: Any
) -> dict[str, Any]:
    """Qué parte de lo PRESCRITO ESE DÍA se completó, serie a serie.

    Relativo a lo prescrito y nunca a un volumen absoluto. Una semana de
    descarga bien hecha es un cumplimiento del 100%: el plan pedía menos y se
    hizo lo que pedía. Medirlo contra el volumen de una semana normal
    convertiría el descanso programado en un fracaso, que es justo al revés de
    lo que esta vista tiene que decir.

    El criterio de "serie alcanzada" es el del motor (`_alcanza`): reps, peso y
    segundos, y pasarse cuenta como cumplir. Lo que cambia aquí es que el motor
    contesta sí/no por ejercicio -porque lo suyo es abrir o no la puerta de la
    carga- y esto contesta una fracción: para un índice, "seis de siete series"
    y "cero de siete" no pueden valer lo mismo.
    """
    from app.integrations.hevy import _alcanza, _emparejar

    parejas = _emparejar(entreno, _Plan(plan.get("exercises") or []), cfg)

    prescritas = 0
    logradas = 0
    detalle: dict[str, Any] = {}

    for clave, (objetivo, reales) in parejas.items():
        hechas = 0
        if reales is not None:
            for i, pedida in enumerate(objetivo):
                if i < len(reales) and _alcanza(reales[i], pedida):
                    hechas += 1
        prescritas += len(objetivo)
        logradas += hechas
        detalle[clave] = {
            "prescritas": len(objetivo),
            "logradas": hechas,
            # `None` es "el ejercicio no aparece en el entreno", que no es lo
            # mismo que "aparece y no se completó ninguna serie".
            "registrado": reales is not None,
        }

    if prescritas == 0:
        return {
            "valor": None,
            "detalle": detalle,
            "prescritas": 0,
            "logradas": 0,
            "na": (
                "la sesión prescrita de ese día no tenía ninguna serie efectiva: "
                "sin nada que cumplir no hay cumplimiento que medir"
            ),
        }

    return {
        "valor": round(100.0 * logradas / prescritas, 2),
        "detalle": detalle,
        "prescritas": prescritas,
        "logradas": logradas,
        "na": None,
    }


def pesos_topes(entreno: dict[str, Any], plan: dict[str, Any], cfg: Any) -> dict[str, float]:
    """El peso de la serie efectiva más pesada de cada ejercicio, tal como se hizo.

    Se guarda en `components_json` de cada fila para que la progresión de la
    sesión siguiente tenga con qué compararse sin volver a abrir el crudo de
    Hevy ni reconstruir el plan de aquel día.
    """
    from app.integrations.hevy import pesos_ejecutados

    crudos = pesos_ejecutados(entreno, _Plan(plan.get("exercises") or []), cfg)
    return {k: float(v) for k, v in crudos.items() if v is not None}


def progresion(
    pesos_hoy: dict[str, float], pesos_antes: dict[str, float]
) -> dict[str, Any]:
    """¿Se movió más peso que la vez anterior en esta misma rutina?

    La escala se ancla en +-10%: un 10% más de media es un 100, el mismo peso es
    un 50 y un 10% menos es un 0. El 50 del centro es deliberado y no un
    apaño para que quede bonito: repetir carga NO es fracasar. En una espalda
    con hernia L4-L5, sostener es el resultado normal y bueno de la mayoría de
    las sesiones, y una escala donde "igual que la semana pasada" puntuara cero
    convertiría el plan entero en una acusación semanal.
    """
    comunes = [
        (k, pesos_hoy[k], pesos_antes[k])
        for k in pesos_hoy
        if k in pesos_antes and pesos_antes[k] > 0
    ]
    if not comunes:
        return {
            "valor": None,
            "n_ejercicios": 0,
            "detalle": {},
            "na": (
                "no hay ninguna sesión anterior de esta rutina con pesos apuntados "
                "que compartan ejercicio con esta: no hay contra qué comparar"
            ),
        }

    detalle = {
        k: {"hoy": hoy, "anterior": antes, "variacion_pct": round(100 * (hoy / antes - 1), 2)}
        for k, hoy, antes in comunes
    }
    # Sin `or 0.0`: `comunes` no está vacío -lo garantiza el `return` de arriba-,
    # así que `_media` devuelve un float y ese defecto es inalcanzable. Importa
    # quitarlo porque este es el SITIO QUE ESCRIBE `variacion_media_pct`, y un
    # cero de relleno aquí sale por el otro extremo como "la misma carga que la
    # vez anterior": la frase concreta y falsa que ya se arregló en la punta que
    # lee. Taparlo en un lado y dejarlo en el otro es no haberlo arreglado.
    medio = _media([hoy / antes - 1 for _, hoy, antes in comunes])
    return {
        "valor": round(_limitar(50.0 + medio * 500.0), 2),
        # El porcentaje crudo, antes de meterlo en la escala y antes de
        # recortarlo. El índice es un 0-100 comparable entre sesiones; esto es
        # lo que hay que poder decir en una frase -"dos kilos y medio más en el
        # press"- sin obligar a deshacer mentalmente la escala.
        "variacion_media_pct": round(medio * 100.0, 2),
        "n_ejercicios": len(comunes),
        "detalle": detalle,
        "na": None,
    }


def esfuerzo(
    rpe: float | None, volumen: float, volumenes_previos: list[float]
) -> dict[str, Any]:
    """El RPE del día siguiente, puesto EN RELACIÓN con la carga que se movió.

    Un RPE de 7 no dice nada por sí solo. Un 7 moviendo el volumen más alto del
    semestre es una sesión excelente; el mismo 7 en la sesión más floja es una
    señal de que algo no va. Por eso el componente es la DISTANCIA entre lo que
    costó y lo que se movió, y no ninguna de las dos cosas por separado.

    El RPE se contesta a la mañana siguiente (`checkins.yesterday_rpe`). Esa es
    la razón de fondo de que el aviso salga en el mensaje del día siguiente y no
    en el del día: antes de esa respuesta, la sesión no se puede terminar de
    evaluar. No es una preferencia de formato, es que el dato no existe todavía.
    """
    if rpe is None:
        return {
            "valor": None,
            "rpe": None,
            "volumen_kg": round(volumen, 1),
            "carga_pct": None,
            "na": (
                "todavía no ha contestado el esfuerzo percibido de esta sesión; se "
                "pregunta en el formulario de la mañana siguiente"
            ),
        }

    carga_pct = percentil_de(volumen, volumenes_previos)
    if carga_pct is None:
        return {
            "valor": None,
            "rpe": rpe,
            "volumen_kg": round(volumen, 1),
            "carga_pct": None,
            "na": (
                "no hay sesiones anteriores con las que comparar el volumen, así "
                "que el esfuerzo no se puede poner en relación con nada"
            ),
        }

    coste = (rpe - ESCALA_MIN) / (ESCALA_MAX - ESCALA_MIN) * 100.0
    return {
        # Mover mucho con poco coste sube; mover poco con mucho coste baja. Se
        # divide entre dos para que el componente se quede en 0-100 sin recortar
        # en cuanto los dos extremos se separan del todo.
        "valor": round(_limitar(50.0 + (carga_pct - coste) / 2.0), 2),
        "rpe": rpe,
        "volumen_kg": round(volumen, 1),
        "carga_pct": round(carga_pct, 2),
        "coste_pct": round(coste, 2),
        "na": None,
    }


def volumen_efectivo(entreno: dict[str, Any], cfg: Any) -> float:
    """Kilos por repetición de todo el entreno, sin el calentamiento.

    Usa `volume_kg` del motor, que es el mismo que decide qué serie es
    calentamiento en el cumplimiento y en el recorte del ámbar. Un tercer
    criterio de calentamiento, solo para esta vista, acabaría discrepando.
    """
    raw = (cfg.raw if hasattr(cfg, "raw") else cfg) or {}
    set_cfg = raw.get("set_types") or {}
    total = 0.0
    for ex in entreno.get("exercises") or []:
        clave = ex.get("exercise_template_id") or ex.get("title")
        total += volume_kg(ex.get("sets") or [], set_cfg, str(clave) if clave else None)
    return total


def rendimiento_fuerza(
    entreno: dict[str, Any],
    plan: dict[str, Any],
    cfg: Any,
    *,
    rpe: float | None,
    pesos_antes: dict[str, float],
    volumenes_previos: list[float],
) -> dict[str, Any]:
    """Los tres componentes de una sesión de fuerza, y su mezcla.

    El cumplimiento es el esqueleto: si no se puede calcular, no hay índice. Los
    otros dos se suman si están. Eso hace que el índice de una sesión recién
    terminada -sin RPE todavía- sea comparable con el de una sesión antigua, y
    por eso `componentes_usados` viaja en la respuesta: dos índices de 70 hechos
    con distinto número de piezas no son el mismo 70, y quien lo lea tiene que
    poder verlo.
    """
    cump = cumplimiento(entreno, plan, cfg)
    pesos_hoy = pesos_topes(entreno, plan, cfg)
    prog = progresion(pesos_hoy, pesos_antes)
    volumen = volumen_efectivo(entreno, cfg)
    esf = esfuerzo(rpe, volumen, volumenes_previos)

    componentes = {"cumplimiento": cump, "progresion": prog, "esfuerzo": esf}
    usados = [k for k, v in componentes.items() if v["valor"] is not None]

    if cump["valor"] is None:
        return {
            "indice": None,
            "componentes": componentes,
            "componentes_usados": usados,
            "pesos": pesos_hoy,
            "volumen_kg": round(volumen, 1),
            "na": cump["na"],
        }

    valores = [componentes[k]["valor"] for k in usados]
    return {
        "indice": round(sum(valores) / len(valores), 2),
        "componentes": componentes,
        "componentes_usados": usados,
        "pesos": pesos_hoy,
        "volumen_kg": round(volumen, 1),
        "na": None,
    }


# ---------------------------------------------------------------------------
# El rendimiento en bici
# ---------------------------------------------------------------------------


def coste_cardiaco(act: Activity) -> dict[str, Any]:
    """El esfuerzo del corazón en una escala de 1 a 5, usando SUS zonas.

    "Frecuencia cardiaca relativa a las zonas" es literalmente esto: no los
    pulsos, que dependen del día y de la cafeína, sino en qué zonas suyas estuvo
    y cuánto tiempo. Las zonas se las calcula Garmin con su máxima y su umbral,
    así que ya vienen normalizadas a él.

    Sin zonas se cae a la media de pulsaciones, y se DICE que se ha caído
    (`fuente`). Un número que unos días significa "zona media ponderada" y otros
    "pulsaciones por minuto" y no avisa de cuál es, es peor que no tenerlo.
    """
    segundos = [
        act.hr_zone_1_s or 0.0,
        act.hr_zone_2_s or 0.0,
        act.hr_zone_3_s or 0.0,
        act.hr_zone_4_s or 0.0,
        act.hr_zone_5_s or 0.0,
    ]
    total = sum(segundos)
    if total > 0:
        ponderado = sum(s * (i + 1) for i, s in enumerate(segundos)) / total
        return {
            "valor": round(ponderado, 3),
            "fuente": "zonas",
            "reparto": [round(s / total * 100, 1) for s in segundos],
            "na": None,
        }
    if act.avg_hr:
        return {"valor": float(act.avg_hr), "fuente": "fc_media", "reparto": None, "na": None}
    return {
        "valor": None,
        "fuente": None,
        "reparto": None,
        "na": "la salida no trae ni tiempo en zonas ni pulsaciones medias",
    }


def _metricas_bici(act: Activity) -> dict[str, Any]:
    """Velocidad, desnivel por kilómetro y velocidad corregida por el terreno."""
    metros = float(act.distance_m or 0.0)
    segundos = float(act.moving_duration_s or act.duration_s or 0.0)
    if metros <= 0 or segundos <= 0:
        return {
            "na": (
                "la salida no trae distancia o duración, así que no hay velocidad "
                "que calcular"
            )
        }
    km = metros / 1000.0
    velocidad = km / (segundos / 3600.0)

    # "Llano" y "no lo sé" no son el mismo dato, y un `or 0.0` los convertía en
    # el mismo número. El desnivel sale por Telegram como una afirmación -"42 km
    # a 25,2 km/h con 0 m/km de desnivel"- y de ahí a decirle a alguien que rodó
    # por el llano un día que subió un puerto hay un paso. Además contamina dos
    # cosas más sin avisar: la velocidad ajustada se queda sin corregir pero
    # sigue llamándose ajustada, y la salida entra en el histórico del desnivel
    # como la más llana de todas.
    #
    # Falta de verdad cuando la salida es de rodillo o el dispositivo no lleva
    # altímetro, así que no es un caso raro de laboratorio.
    # `na` significa "esta salida no se puede juzgar en absoluto" y corta arriba.
    # Sin desnivel sí se puede juzgar, solo que con menos piezas: quedan los
    # kilómetros, la velocidad cruda y todo el bloque de pulso. Por eso el motivo
    # va en `na_desnivel` y no en `na`; meterlo en `na` tiraría la salida entera
    # -y su componente de corazón, que estaba perfecto- por un dato que solo
    # afecta a dos de las tres piezas.
    if act.elevation_gain_m is None:
        return {
            "km": round(km, 2),
            "velocidad_kmh": round(velocidad, 2),
            "desnivel_m_km": None,
            "velocidad_ajustada": None,
            "na": None,
            "na_desnivel": (
                "la salida no trae desnivel acumulado, así que no se puede "
                "corregir la velocidad por el terreno ni decir cuánto se subió"
            ),
        }

    desnivel_km = float(act.elevation_gain_m) / km
    return {
        "km": round(km, 2),
        "velocidad_kmh": round(velocidad, 2),
        "desnivel_m_km": round(desnivel_km, 1),
        # Cien metros de desnivel por kilómetro es terreno muy duro y vale como
        # unidad: a ese ritmo de subida, la velocidad cuenta el doble. El ajuste
        # es tosco y se deja a la vista precisamente por eso; sin potenciómetro
        # no hay forma de hacerlo fino, y fingir precisión sería peor.
        "velocidad_ajustada": round(velocidad * (1 + desnivel_km / 100.0), 2),
        "na": None,
        "na_desnivel": None,
    }


def rendimiento_bici(act: Activity, historico: list[dict[str, Any]]) -> dict[str, Any]:
    """Los tres componentes de una salida, y cuál de ellos NO entra en el índice.

    `desnivel` se calcula, se guarda y se enseña, pero NO se promedia: no es
    rendimiento, es dificultad del terreno. Meterlo en la media diría que salir
    a rodar por el llano es rendir peor, y el día que encadenara tres salidas
    llanas el índice bajaría sin que hubiera pasado nada. Donde sí entra el
    desnivel es dentro de la velocidad, corrigiéndola, que es el sitio donde de
    verdad significa algo.

    Eso hay que decirlo aquí y en la pantalla, no esconderlo: los tres se piden
    y los tres salen, pero dos construyen el índice y el tercero lo explica.
    """
    m = _metricas_bici(act)
    if m.get("na"):
        return {"indice": None, "componentes": {}, "componentes_usados": [], "na": m["na"]}

    fc = coste_cardiaco(act)
    # La eficiencia es velocidad AJUSTADA por unidad de coste cardiaco, así que
    # sin desnivel tampoco hay eficiencia. No se sustituye por la velocidad
    # cruda: el histórico de eficiencias está hecho de ajustadas, y mezclar las
    # dos daría un percentil que no significa lo que dice.
    #
    # El precio es que una salida de rodillo se queda sin índice. Es el precio
    # correcto: sin nota y con el motivo escrito, en vez de con una nota
    # calculada sobre dos números que no son comparables entre sí.
    eficiencia = (
        m["velocidad_ajustada"] / fc["valor"]
        if m["velocidad_ajustada"] is not None and fc["valor"]
        else None
    )

    previos_vel = [h["velocidad_ajustada"] for h in historico if h.get("velocidad_ajustada")]
    previos_efi = [h["eficiencia"] for h in historico if h.get("eficiencia")]
    previos_des = [h["desnivel_m_km"] for h in historico if h.get("desnivel_m_km") is not None]

    # Sin desnivel no hay velocidad ajustada, y la cruda NO sirve de sustituta:
    # el histórico contra el que se compara está lleno de velocidades corregidas
    # por el terreno, así que meter una sin corregir daría un percentil que no
    # significa lo que dice. Es el mismo motivo por el que el coste cardiaco
    # lleva `fuente`: un número que unos días mide una cosa y otros otra, y no
    # avisa de cuál, es peor que no tenerlo.
    velocidad = {
        "valor": (
            percentil_de(m["velocidad_ajustada"], previos_vel)
            if m["velocidad_ajustada"] is not None
            else None
        ),
        "velocidad_kmh": m["velocidad_kmh"],
        "velocidad_ajustada": m["velocidad_ajustada"],
        "n_previas": len(previos_vel),
        "na": m["na_desnivel"]
        or (
            None
            if previos_vel
            else "no hay salidas anteriores con las que comparar la velocidad"
        ),
    }
    corazon = {
        "valor": percentil_de(eficiencia, previos_efi) if eficiencia else None,
        "eficiencia": round(eficiencia, 3) if eficiencia else None,
        "coste_cardiaco": fc["valor"],
        "fuente_fc": fc["fuente"],
        "reparto_zonas": fc["reparto"],
        "n_previas": len(previos_efi),
        # El motivo del desnivel también cuenta aquí: la eficiencia depende de
        # la velocidad ajustada. Sin él, decir solo "no hay salidas anteriores"
        # mandaría a buscar el fallo al sitio equivocado.
        "na": fc["na"]
        or m["na_desnivel"]
        or (None if previos_efi else "no hay salidas anteriores con las que comparar"),
    }
    desnivel = {
        "valor": (
            percentil_de(m["desnivel_m_km"], previos_des)
            if m["desnivel_m_km"] is not None
            else None
        ),
        "desnivel_m_km": m["desnivel_m_km"],
        "n_previas": len(previos_des),
        # La bandera que impide que alguien lo promedie por error más adelante.
        "entra_en_el_indice": False,
        "nota": (
            "el desnivel mide lo duro que era el terreno, no lo bien que se "
            "rodó; entra corrigiendo la velocidad, no sumando por su cuenta"
        ),
        "na": m["na_desnivel"]
        or (
            None
            if previos_des
            else "no hay salidas anteriores con las que comparar el desnivel"
        ),
    }

    componentes = {"velocidad": velocidad, "corazon": corazon, "desnivel": desnivel}
    usados = [k for k in ("velocidad", "corazon") if componentes[k]["valor"] is not None]

    if not usados:
        return {
            "indice": None,
            "componentes": componentes,
            "componentes_usados": [],
            "metricas": m,
            "eficiencia": eficiencia,
            "na": (
                "no hay salidas anteriores con las que comparar esta: el percentil "
                "de una muestra de una sola salida sería siempre el mismo número"
            ),
        }

    return {
        "indice": round(sum(componentes[k]["valor"] for k in usados) / len(usados), 2),
        "componentes": componentes,
        "componentes_usados": usados,
        "metricas": m,
        "eficiencia": eficiencia,
        "na": None,
    }


# ---------------------------------------------------------------------------
# El cruce
# ---------------------------------------------------------------------------


def cruzar(
    percepcion_pct: float | None, rendimiento_pct: float | None, base: int
) -> dict[str, Any]:
    """Percepción contra rendimiento, y si el hueco es lo bastante grande.

    `gap` positivo = la sesión salió MEJOR de lo que anunciaba la mañana.

    La disociación se marca en las DOS direcciones, con el mismo umbral. El
    contador que se mira luego solo cuenta una de ellas, pero registrar solo esa
    daría un contador que no se puede creer: un marcador que apunta los aciertos
    y no los fallos no es un marcador, es un cartel. Que la otra dirección esté
    ahí, guardada y sin destacar, es lo que hace que el número signifique algo.
    """
    if percepcion_pct is None or rendimiento_pct is None:
        que_falta = []
        if percepcion_pct is None:
            que_falta.append("la percepción")
        if rendimiento_pct is None:
            que_falta.append("el rendimiento")
        return {
            "gap": None,
            "direccion": SIN_DATO,
            "disociacion": False,
            "na": f"no se pudo situar {' ni '.join(que_falta)} en su histórico",
        }

    gap = rendimiento_pct - percepcion_pct

    if base < BASE_MINIMA:
        return {
            "gap": round(gap, 2),
            # La dirección SÍ se apunta -es una resta, no necesita muestra-, pero
            # no se marca disociación: con pocas sesiones detrás el percentil es
            # una ordenación de cuatro cosas, y la tabla no se puede corregir
            # después.
            "direccion": _direccion(gap),
            "disociacion": False,
            "na": (
                f"solo hay {cuantos(base, 'sesión', 'sesiones')} "
                f"{plural(base, 'anterior', 'anteriores')}; hacen falta "
                f"{BASE_MINIMA} para que un percentil signifique algo, así que "
                f"esta no cuenta todavía para el contador"
            ),
        }

    clara = (
        abs(gap) >= HUECO_MINIMO
        and (
            (percepcion_pct <= PERCEPCION_MALA and rendimiento_pct >= RENDIMIENTO_OK)
            or (rendimiento_pct <= PERCEPCION_MALA and percepcion_pct >= RENDIMIENTO_OK)
        )
    )
    return {
        "gap": round(gap, 2),
        "direccion": _direccion(gap),
        "disociacion": bool(clara),
        "na": None,
    }


def _direccion(gap: float) -> str:
    if gap >= HUECO_MINIMO:
        return PERCEPCION_PEOR
    if gap <= -HUECO_MINIMO:
        return PERCEPCION_MEJOR
    return ALINEADO


# ---------------------------------------------------------------------------
# Persistencia: una fila por sesión, escrita una vez y nunca reescrita
# ---------------------------------------------------------------------------


def _previas(session: Session, kind: str, antes_de: date) -> list[SessionPerformance]:
    """Las sesiones ya evaluadas del mismo tipo, ANTES de esta.

    Estrictamente anteriores, y por eso el filtro está aquí y no en la llamada:
    incluir la sesión que se está evaluando dentro de su propia muestra la
    empujaría hacia el centro -una muestra de una sola sesión da siempre el
    percentil 50- y las primeras filas saldrían todas "normales" por
    construcción, que es la conclusión que esta vista existe para no dar por
    descontada.

    Solo entran las que tienen LOS DOS índices. Situar la percepción entre
    treinta sesiones y el rendimiento entre doce sería restar dos números
    medidos con reglas distintas, y el hueco que sale de esa resta -que es
    justo el número del contador- no significaría nada.
    """
    return list(
        session.scalars(
            select(SessionPerformance)
            .where(
                SessionPerformance.kind == kind,
                SessionPerformance.date < antes_de,
                SessionPerformance.perception_index.is_not(None),
                SessionPerformance.performance_index.is_not(None),
            )
            .order_by(SessionPerformance.date)
        )
    )


def _checkin(session: Session, dia: date) -> Checkin | None:
    return session.scalars(select(Checkin).where(Checkin.date == dia)).first()


def _decision(session: Session, dia: date) -> Decision | None:
    return session.scalars(
        select(Decision)
        .where(Decision.date == dia, Decision.is_current.is_(True))
        .order_by(Decision.id.desc())
    ).first()


def _valor(bloque: Any) -> float | None:
    return bloque.get("valor") if isinstance(bloque, dict) else None


def _dato(fila: SessionPerformance, *camino: str) -> Any:
    """Saca un valor de `components_json` sin reventar si no está."""
    try:
        actual: Any = json.loads(fila.components_json or "{}")
    except (TypeError, ValueError):
        return None
    for paso in camino:
        if not isinstance(actual, dict):
            return None
        actual = actual.get(paso)
    return actual


def evaluar_sesion(
    session: Session,
    cfg: Any,
    *,
    dia: date,
    kind: str,
    source_key: str,
    entreno: dict[str, Any] | None = None,
    actividad: Activity | None = None,
) -> SessionPerformance:
    """Escribe la fila de una sesión. Si ya estaba, la devuelve SIN TOCARLA.

    Append-only en el sentido fuerte. La fila guarda el juicio que se pudo hacer
    ese día con el histórico que había ese día, y no se recalcula nunca. Los
    percentiles no son verdades absolutas -son la posición dentro de una
    distribución que sigue creciendo-, así que rehacerlos cada noche daría un
    contador que cambia de valor sin que haya pasado nada nuevo.
    """
    ya = session.scalars(
        select(SessionPerformance).where(SessionPerformance.source_key == source_key)
    ).first()
    if ya is not None:
        return ya

    # La percepción es la del check-in de ESE día, la de antes de entrenar. Si
    # esa mañana no se rellenó el formulario no hay percepción que comparar, y
    # `indice_percepcion` lo dirá con el motivo escrito en vez de inventarse un
    # cincuenta -que es lo que saldría de un diccionario vacío tratado como
    # "todo normal"-.
    manana_de = _checkin(session, dia)
    valores = (
        {k: getattr(manana_de, k, None) for k in PERCEPCION} if manana_de else {}
    )
    per = indice_percepcion(valores)

    previas = _previas(session, kind, dia)
    base = len(previas)

    if kind in (FUERZA, HIIT):
        rend = _rendimiento_de_fuerza(
            session, cfg, dia, entreno or {}, previas, hiit=(kind == HIIT)
        )
    else:
        rend = _rendimiento_de_bici(actividad, previas)

    per_pct = (
        percentil_de(per["indice"], [p.perception_index for p in previas])
        if per["indice"] is not None
        else None
    )
    rend_pct = (
        percentil_de(rend["indice"], [p.performance_index for p in previas])
        if rend["indice"] is not None
        else None
    )
    cruce = cruzar(per_pct, rend_pct, base)

    comp = rend.get("componentes") or {}
    fila = SessionPerformance(
        date=dia,
        kind=kind,
        source_key=source_key,
        routine_key=rend.get("rutina"),
        garmin_activity_id=actividad.garmin_activity_id if actividad else None,
        perception_index=per["indice"],
        perception_pct=per_pct,
        performance_index=rend["indice"],
        performance_pct=rend_pct,
        comp_compliance=_valor(comp.get("cumplimiento")),
        comp_progression=_valor(comp.get("progresion")),
        comp_rpe=_valor(comp.get("esfuerzo")),
        comp_bike_hr=_valor(comp.get("corazon")),
        comp_bike_speed=_valor(comp.get("velocidad")),
        comp_bike_elevation=_valor(comp.get("desnivel")),
        components_json=json.dumps(
            {"percepcion": per, "rendimiento": rend}, ensure_ascii=False, default=str
        ),
        gap_pct=cruce["gap"],
        direction=cruce["direccion"],
        dissociation=cruce["disociacion"],
        n_sessions_base=base,
        # El motivo del eslabón de MÁS ABAJO. Si no hay percepción, dar el motivo
        # del cruce -"no se pudo situar la percepción"- mandaría a mirar el
        # histórico cuando lo que falta es el formulario de esa mañana.
        na_reason=per["na"] or rend.get("na") or cruce["na"],
    )
    for clave in PERCEPCION:
        setattr(fila, f"perceived_{clave}", valores.get(clave))

    session.add(fila)
    session.flush()
    return fila


def _rendimiento_de_fuerza(
    session: Session,
    cfg: Any,
    dia: date,
    entreno: dict[str, Any],
    previas: list[SessionPerformance],
    *,
    hiit: bool = False,
) -> dict[str, Any]:
    """Reúne de la base lo que `rendimiento_fuerza` necesita, y lo llama.

    `hiit` elige CONTRA QUÉ PLAN se mide. La decisión del día guarda dos
    sesiones: la de fuerza en la raíz y el bloque HIIT anidado bajo `hiit`. Sin
    este interruptor, un entreno de wall balls se comparaba con la lista de la
    prensa y el remo: ninguno de sus ejercicios aparecía, el índice salía por
    los suelos y la fila decía que la sesión había sido malísima cuando se
    había hecho entera.
    """
    dec = _decision(session, dia)
    plan = json.loads(dec.planned_session_json) if dec and dec.planned_session_json else {}
    if hiit:
        plan = plan.get("hiit") or {}
    if not plan.get("exercises"):
        return {
            "indice": None,
            "componentes": {},
            "componentes_usados": [],
            "rutina": plan.get("routine"),
            "na": (
                "no hay sesión prescrita guardada para ese día: sin saber lo que "
                "tocaba hacer, lo que se hizo no es ni mucho ni poco"
            ),
        }

    # El esfuerzo se contesta a la MAÑANA SIGUIENTE, sobre el entreno de ayer.
    manana = _checkin(session, dia + timedelta(days=1))
    rpe = getattr(manana, "yesterday_rpe", None) if manana else None

    rutina = plan.get("routine")
    salida = rendimiento_fuerza(
        entreno,
        plan,
        cfg,
        rpe=float(rpe) if rpe is not None else None,
        pesos_antes=_ultimos_pesos(previas, rutina),
        volumenes_previos=[
            v
            for v in (_dato(p, "rendimiento", "volumen_kg") for p in previas)
            if v is not None
        ],
    )
    salida["rutina"] = rutina
    return salida


def _ultimos_pesos(previas: list[SessionPerformance], rutina: str | None) -> dict[str, float]:
    """Los pesos de la última sesión de la MISMA rutina, para comparar.

    De la misma rutina y no simplemente de la anterior: comparar los pesos del
    Día 2 con los del Día 1 mediría variaciones entre ejercicios que no tienen
    nada que ver, y la progresión se volvería ruido con forma de número.
    """
    for p in reversed(previas):
        if rutina and p.routine_key != rutina:
            continue
        pesos = _dato(p, "rendimiento", "pesos")
        if pesos:
            return {k: float(v) for k, v in pesos.items()}
    return {}


def _rendimiento_de_bici(
    act: Activity | None, previas: list[SessionPerformance]
) -> dict[str, Any]:
    if act is None:
        return {
            "indice": None,
            "componentes": {},
            "componentes_usados": [],
            "na": "no hay actividad de Garmin guardada para esa salida",
        }
    historico = [
        {
            "velocidad_ajustada": _dato(p, "rendimiento", "metricas", "velocidad_ajustada"),
            "eficiencia": _dato(p, "rendimiento", "eficiencia"),
            "desnivel_m_km": _dato(p, "rendimiento", "metricas", "desnivel_m_km"),
        }
        for p in previas
    ]
    return rendimiento_bici(act, historico)


# ---------------------------------------------------------------------------
# El barrido: qué sesiones se pueden evaluar ya
# ---------------------------------------------------------------------------

# Cuántos días se espera al check-in de la mañana siguiente antes de dar la
# sesión por evaluada sin esfuerzo percibido.
ESPERA_MAXIMA = 3


def _toca_evaluar(session: Session, dia: date, hoy: date) -> bool:
    if dia >= hoy:
        return False  # todavía no ha llegado la mañana siguiente
    if _checkin(session, dia + timedelta(days=1)) is not None:
        return True
    return (hoy - dia).days >= ESPERA_MAXIMA


def evaluar_pendientes(
    session: Session, cfg: Any, *, hasta: date, dias: int = 30
) -> list[SessionPerformance]:
    """Evalúa las sesiones que ya se pueden evaluar y todavía no lo están.

    UNA SESIÓN NO SE EVALÚA EL MISMO DÍA, y no es una decisión de formato sino
    el calendario del dato: el esfuerzo percibido se pregunta en el formulario
    de la mañana siguiente. Evaluarla por la noche daría una fila sin ese
    componente y, como las filas no se reescriben, ese hueco sería para siempre.

    Y si el check-in del día siguiente no llega nunca -una mañana que no se
    rellenó-, se evalúa igual pasados `ESPERA_MAXIMA` días, con el esfuerzo a
    nulo y el motivo escrito. Sin esa salida, una sesión sin check-in detrás se
    quedaría fuera de la tabla para siempre y el denominador del contador
    dejaría de ser el total de sesiones, que es precisamente lo que lo hace
    creíble.
    """
    from app.integrations.hevy import claves_hiit

    escritas: list[SessionPerformance] = []
    desde = hasta - timedelta(days=dias - 1)
    hiit = claves_hiit(cfg)

    for w in session.scalars(
        select(WorkoutLog)
        .where(WorkoutLog.date >= desde, WorkoutLog.date <= hasta)
        .order_by(WorkoutLog.date)
    ):
        if not _toca_evaluar(session, w.date, hasta):
            continue
        try:
            crudo = json.loads(w.raw_json or "{}")
        except ValueError:
            crudo = {}
        # El tipo sale de la RUTINA DE ORIGEN, nunca del título. Todas las filas
        # de `workout_log` entraban aquí como `strength`, así que el bloque HIIT
        # -que en Hevy es un entrenamiento suelto- se juzgaba contra el plan de
        # fuerza y encima ensuciaba la distribución con la que se sitúan las
        # sesiones de fuerza de verdad. Un entreno sin rutina conocida sigue
        # contando como fuerza: es lo que casi siempre es, y `_rendimiento_de_
        # fuerza` ya dice que no hay plan contra el que medirlo.
        escritas.append(
            evaluar_sesion(
                session,
                cfg,
                dia=w.date,
                kind=HIIT if (w.routine_key or "") in hiit else FUERZA,
                source_key=f"hevy:{w.hevy_workout_id}",
                entreno=crudo,
            )
        )

    for a in session.scalars(
        select(Activity)
        .where(
            Activity.date >= desde,
            Activity.date <= hasta,
            Activity.is_cycling.is_(True),
        )
        .order_by(Activity.date)
    ):
        if not _toca_evaluar(session, a.date, hasta):
            continue
        escritas.append(
            evaluar_sesion(
                session,
                cfg,
                dia=a.date,
                kind=BICI,
                source_key=f"garmin:{a.garmin_activity_id}",
                actividad=a,
            )
        )

    return escritas


# ---------------------------------------------------------------------------
# El contador, que es para lo que existe todo lo anterior
# ---------------------------------------------------------------------------


def _resumen(f: SessionPerformance) -> dict[str, Any]:
    """Una fila tal como se guardó. NADA se recalcula aquí.

    Es la diferencia entre un histórico y una reconstrucción. Los percentiles de
    cada fila se calcularon con el histórico que había ESE día; volver a
    calcularlos ahora daría otros números, y el listado de "los días en que
    pasó" dejaría de coincidir con los días en que de verdad pasó.
    """
    return {
        "fecha": f.date.isoformat(),
        "tipo": f.kind,
        "rutina": f.routine_key,
        "percepcion": f.perception_index,
        "percepcion_pct": f.perception_pct,
        "rendimiento": f.performance_index,
        "rendimiento_pct": f.performance_pct,
        "gap": f.gap_pct,
        "direccion": f.direction,
        "disociacion": bool(f.dissociation),
        "n_base": f.n_sessions_base,
        "componentes": {
            "cumplimiento": f.comp_compliance,
            "progresion": f.comp_progression,
            "esfuerzo": f.comp_rpe,
            "corazon": f.comp_bike_hr,
            "velocidad": f.comp_bike_speed,
            "desnivel": f.comp_bike_elevation,
        },
        "na": f.na_reason,
    }


def _juzgable(f: SessionPerformance) -> bool:
    """¿Esta sesión PUDO haber contado para el contador?

    Es la condición exacta con la que `cruzar` decide si marca disociación: los
    dos percentiles y suficientes sesiones detrás. Se escribe así, mirando lo
    guardado, y no repitiendo las reglas: si un día cambia el umbral, el
    denominador de las filas viejas tiene que seguir siendo el que era cuando se
    escribieron.
    """
    return f.gap_pct is not None and (f.n_sessions_base or 0) >= BASE_MINIMA


def _cuenta(
    filas: list[SessionPerformance],
) -> tuple[list[SessionPerformance], list[SessionPerformance], list[SessionPerformance]]:
    """Reparte las filas en juzgables, peores y mejores.

    Existe para que la ventana, el acumulado y el mensaje de Telegram cuenten
    con el MISMO código. Tres sitios repartiendo a mano es la forma barata de
    que un día la pantalla diga "9 de 84" y el Telegram de esa misma mañana diga
    "8 de 83", y de que nadie sepa cuál de los dos miente.
    """
    juzgadas = [f for f in filas if _juzgable(f)]
    return (
        juzgadas,
        [f for f in juzgadas if f.dissociation and f.direction == PERCEPCION_PEOR],
        [f for f in juzgadas if f.dissociation and f.direction == PERCEPCION_MEJOR],
    )


def _pct(veces: int, de: int) -> float | None:
    return round(100.0 * veces / de, 1) if de else None


def contador_historico(
    session: Session, *, hasta: date | None = None
) -> dict[str, Any]:
    """El contador sobre TODAS las sesiones juzgadas, sin ventana.

    Es el que se manda por Telegram y el que no puede bajar. El de la ventana
    sirve para mirar si la cosa mejora; este sirve para la frase "van nueve de
    ochenta y cuatro", que solo significa algo si las ochenta y cuatro son todas
    las que ha habido y no las de los últimos seis meses.
    """
    hasta = hasta or date.today()
    filas = list(
        session.scalars(
            select(SessionPerformance)
            .where(SessionPerformance.date <= hasta)
            .order_by(SessionPerformance.date, SessionPerformance.id)
        )
    )
    juzgadas, peores, _ = _cuenta(filas)
    return {
        "veces": len(peores),
        "de": len(juzgadas),
        "pct": _pct(len(peores), len(juzgadas)),
        "total_sesiones": len(filas),
        "na": None
        if juzgadas
        else (
            "todavía no hay ninguna sesión que se haya podido juzgar, así que el "
            "acumulado no tiene denominador: no es un cero, es que aún no se "
            "puede contar"
        ),
        "que_es": (
            "todas las sesiones registradas hasta la fecha, sin ventana; este "
            "número no baja"
        ),
    }


def vista_percepcion(
    session: Session, *, dias: int = 180, hasta: date | None = None
) -> dict[str, Any]:
    """El contador, el listado y las dos direcciones.

    EL DENOMINADOR VA EXPLICADO, y no por escrúpulo de estadístico. "Nueve de
    ochenta y cuatro" solo significa algo si se sabe qué son las ochenta y
    cuatro. Aquí salen los dos números -las sesiones que se pudieron juzgar y
    todas las sesiones registradas- y el porcentaje se calcula sobre el primero,
    que es el único sobre el que la cuenta tiene sentido: una sesión sin
    check-in esa mañana no es una sesión en la que la percepción acertara, es
    una sesión en la que no se preguntó.

    Meterlas en el denominador haría bajar el contador cada vez que se olvidara
    un formulario, que sería premiar el olvido. Dejarlas fuera y no decirlo sería
    peor. Así que salen las dos cifras y sale el reparto de motivos.

    Y aparte del contador de la ventana sale `historico`, que es el mismo cálculo
    sobre TODAS las filas. No es un extra: el contador de la ventana puede BAJAR
    con el tiempo aunque ninguna fila se reescriba, porque una disociación de
    hace siete meses sale de la ventana y deja de contarse. Para mirar una
    tendencia eso está bien; para el número que se mira una mañana mala, no -era
    justamente lo de "que no pueda reescribir su pasado"-. El acumulado no baja
    nunca, y es el que se manda por Telegram.
    """
    hasta = hasta or date.today()
    desde = hasta - timedelta(days=dias - 1)

    filas = list(
        session.scalars(
            select(SessionPerformance)
            .where(SessionPerformance.date >= desde, SessionPerformance.date <= hasta)
            .order_by(SessionPerformance.date, SessionPerformance.id)
        )
    )
    juzgadas, peores, mejores = _cuenta(filas)
    acumulado = contador_historico(session, hasta=hasta)

    motivos: dict[str, int] = {}
    for f in filas:
        if _juzgable(f):
            continue
        clave = f.na_reason or (
            f"todavía no hay {BASE_MINIMA} sesiones anteriores de este tipo con "
            f"las que comparar"
        )
        motivos[clave] = motivos.get(clave, 0) + 1

    return {
        # Se declara la vista como todas las demás. Va sin `metodo`, y eso está
        # razonado en el endpoint: aquí no se correlaciona nada, se restan dos
        # percentiles ya guardados.
        "vista": "percepcion",
        "ventana": {
            "desde": desde.isoformat(),
            "hasta": hasta.isoformat(),
            "dias": dias,
        },
        # El número que se mira una mañana mala. Va primero a propósito.
        "contador": {
            "veces": len(peores),
            "de": len(juzgadas),
            "pct": _pct(len(peores), len(juzgadas)),
            "total_sesiones": len(filas),
            "sin_juicio": len(filas) - len(juzgadas),
            "motivos": motivos,
            "na": None
            if juzgadas
            else (
                "todavía no hay ninguna sesión que se haya podido juzgar en esta "
                "ventana, así que el contador no tiene denominador: no es un cero, "
                "es que aún no se puede contar"
            ),
            "nota": (
                "el porcentaje es sobre las sesiones que se pudieron juzgar, no "
                "sobre todas las registradas; las que no se pudieron juzgar salen "
                "aparte con su motivo"
            ),
        },
        # El mismo cálculo sin ventana. Es el que se manda por Telegram, así que
        # tiene que estar aquí para que la pantalla pueda enseñar EXACTAMENTE el
        # número que llegó al móvil esa mañana. Si el mensaje citara una cuenta
        # que la vista no sabe hacer, comprobarlo sería imposible.
        "historico": acumulado,
        # La otra dirección: registrada, contada y sin destacar. Un marcador que
        # apunta los aciertos y no los fallos es un cartel, no un marcador.
        #
        # Lleva su propio `na` aunque comparta denominador con el contador de
        # arriba. No es repetirse: los dos bloques se pintan por separado, y el
        # día que la PWA saque este solo -en una pestaña, en un resumen- un "0 de
        # 0" sin motivo al lado se lee como "nunca ha pasado" en vez de como "aún
        # no hay con qué contarlo". Que el motivo viaje pegado al número que
        # explica es lo único que garantiza que lleguen juntos.
        "contraria": {
            "veces": len(mejores),
            "de": len(juzgadas),
            "pct": _pct(len(mejores), len(juzgadas)),
            "na": None
            if juzgadas
            else (
                "todavía no hay ninguna sesión que se haya podido juzgar en esta "
                "ventana, así que esta dirección tampoco tiene denominador: no es "
                "un cero, es que aún no se puede contar"
            ),
            "que_es": (
                "las veces que la mañana prometía más de lo que luego salió; se "
                "guardan para que el contador de arriba se pueda creer"
            ),
        },
        "alineadas": len(juzgadas) - len(peores) - len(mejores),
        # El listado que se pidió: esos días, con sus números.
        "disociaciones": [_resumen(f) for f in reversed(peores)],
        "contrarias": [_resumen(f) for f in reversed(mejores)],
        "sesiones": [_resumen(f) for f in filas],
        "por_tipo": {
            tipo: {
                "sesiones": sum(1 for f in filas if f.kind == tipo),
                "juzgadas": sum(1 for f in juzgadas if f.kind == tipo),
                "veces": sum(1 for f in peores if f.kind == tipo),
            }
            for tipo in (FUERZA, HIIT, BICI)
        },
        "componentes": _medias_componentes(juzgadas),
        "ultima": _resumen(peores[-1]) if peores else None,
        # Las cifras de la frase son las del ACUMULADO, no las de la ventana, y
        # son exactamente las mismas que salieron por Telegram esa mañana. Un
        # mensaje en pantalla que dijera "van 5 de 40" junto a un histórico de
        # "9 de 84" obligaría a elegir cuál de los dos creerse.
        "mensaje": (
            mensaje_disociacion(
                peores[-1], veces=acumulado["veces"], de=acumulado["de"]
            )
            if peores
            else None
        ),
    }


def _medias_componentes(filas: list[SessionPerformance]) -> dict[str, Any]:
    """La media de cada pieza por separado, con su n.

    Un índice medio de 70 no dice de dónde sale. Esto sí: enseña si el 70 lo
    sostiene el cumplimiento mientras la progresión lleva meses plana, que es
    una lectura distinta y que lleva a hacer cosas distintas.
    """
    # La etiqueta viaja al lado del número, y no se deja que la escriba la PWA.
    # Sin ella, el móvil solo tiene la clave -`progresion`, `corazon`- y las
    # pinta tal cual: en pantalla quedan seis palabras sin tilde con pinta de
    # nombre de variable, justo en la vista que existe para leerse de un vistazo
    # una mañana mala. La alternativa sería un diccionario de nombres escrito a
    # mano en JavaScript, que es otra copia de esta lista y se quedaría vieja el
    # día que se añada una pieza.
    #
    # Y la etiqueta dice qué MIDE cada pieza, no cómo se llama la columna:
    # "corazón" no significa nada suelto, "el corazón, frente a tus salidas de
    # siempre" sí.
    # La corta es para la tira de una sola línea de cada sesión, donde no caben
    # seis frases. Van las dos y no se recorta la larga en el móvil: cortar
    # "Corazón, frente a tus salidas de siempre" por el ancho da "Corazón,
    # frente a tus…", que promete una comparación sin decir contra qué.
    columnas = {
        "cumplimiento": ("comp_compliance", "Cumplimiento de lo prescrito", "cumplimiento"),
        "progresion": ("comp_progression", "Progresión de la carga", "progresión"),
        "esfuerzo": ("comp_rpe", "Esfuerzo percibido frente al volumen", "esfuerzo"),
        "corazon": ("comp_bike_hr", "Corazón, frente a tus salidas de siempre", "corazón"),
        "velocidad": (
            "comp_bike_speed", "Velocidad, frente a tus salidas de siempre", "velocidad"
        ),
        "desnivel": ("comp_bike_elevation", "Desnivel de la salida", "desnivel"),
    }
    salida: dict[str, Any] = {}
    for nombre, (columna, etiqueta, corta) in columnas.items():
        valores = [
            float(getattr(f, columna))
            for f in filas
            if getattr(f, columna) is not None
        ]
        media = _media(valores)
        salida[nombre] = {
            "etiqueta": etiqueta,
            "corta": corta,
            "media": round(media, 2) if media is not None else None,
            "n": len(valores),
            "na": None
            if valores
            else "ninguna sesión de la ventana trae esta pieza calculada",
            # El desnivel se promedia AQUÍ -es una media de contexto, para poder
            # decir por qué terreno se ha rodado- pero sigue sin entrar en
            # ningún índice. La bandera viaja para que nadie lo confunda.
            "entra_en_el_indice": nombre != "desnivel",
        }
    return salida


# ---------------------------------------------------------------------------
# La frase
# ---------------------------------------------------------------------------


def _frase_fuerza(comp: dict[str, Any]) -> list[str]:
    from app.engine.message import fmt_num

    trozos: list[str] = []
    cump = comp.get("cumplimiento") or {}
    if cump.get("valor") is not None:
        trozos.append(
            f"{cump['logradas']} de {cump['prescritas']} series efectivas completadas"
        )

    prog = comp.get("progresion") or {}
    if prog.get("valor") is not None:
        # Indexado directo, y no `.get(..., 0.0)`, por la misma razón que sus dos
        # hermanos de aquí al lado. `progresion` solo tiene dos formas: o `valor`
        # es None y trae su `na` -y entonces no llegamos aquí-, o `valor` existe y
        # `variacion_media_pct` es un float. No hay una tercera.
        #
        # Un `or 0.0` ahí sería inalcanzable con datos legítimos, y lo único que
        # podría hacer es convertir una clave que se ha renombrado en la punta que
        # ESCRIBE en la frase "la misma carga que la vez anterior": una afirmación
        # concreta sobre el entreno, dicha con aplomo, sin tener ni idea. Un
        # mensaje cuyo valor entero es ser verificable no puede inventarse un dato
        # para no quedarse corto; que reviente y que mañana se vuelva a intentar,
        # que para eso `reported_at` solo se pone si el envío sale bien.
        var = prog["variacion_media_pct"]
        if abs(var) < 0.5:
            trozos.append("la misma carga que la vez anterior")
        else:
            signo = "+" if var > 0 else ""
            trozos.append(f"{signo}{fmt_num(var)}% de carga sobre la vez anterior")

    esf = comp.get("esfuerzo") or {}
    if esf.get("valor") is not None:
        trozos.append(
            f"RPE {fmt_num(esf['rpe'])} moviendo más volumen que el "
            f"{fmt_num(esf['carga_pct'])}% de tus sesiones"
        )
    return trozos


def _frase_bici(rend: dict[str, Any]) -> list[str]:
    from app.engine.message import fmt_num

    trozos: list[str] = []
    m = rend.get("metricas") or {}
    if m.get("velocidad_kmh") is not None:
        # El desnivel se añade solo si se sabe. Sin él la frase se queda en los
        # kilómetros y la velocidad, que son ciertos; con un `fmt_num(None)`
        # saldría "con — m/km de desnivel", que es ruido con pinta de dato.
        trozo = f"{fmt_num(m['km'])} km a {fmt_num(m['velocidad_kmh'])} km/h"
        if m.get("desnivel_m_km") is not None:
            trozo += f" con {fmt_num(m['desnivel_m_km'])} m/km de desnivel"
        trozos.append(trozo)
    corazon = (rend.get("componentes") or {}).get("corazon") or {}
    if corazon.get("fuente_fc") == "zonas" and corazon.get("coste_cardiaco"):
        trozos.append(f"zona media {fmt_num(corazon['coste_cardiaco'])} de 5")
    elif corazon.get("fuente_fc") == "fc_media" and corazon.get("coste_cardiaco"):
        trozos.append(f"{fmt_num(corazon['coste_cardiaco'])} ppm de media")
    return trozos


def mensaje_disociacion(
    fila: SessionPerformance, *, veces: int, de: int
) -> str:
    """Lo que se dice cuando la mañana y la sesión se contradijeron.

    SIN CONDESCENDENCIA Y SIN TONO DE AUTOAYUDA. No dice "eres más capaz de lo
    que crees", no felicita, no anima y no interpreta. Dice qué puntuó esa
    mañana, dónde cayó dentro de sus mañanas, qué salió en la sesión, con qué
    números concretos, y cuántas veces van sobre cuántas.

    La razón es práctica, no de estilo: una frase que consuela se aprende a
    descontar en dos semanas -"ya está la app diciéndome que soy fuerte"- y a
    partir de ahí no sirve de nada. Una cifra no se descuenta. El valor entero
    de esto está en que sea verificable y aburrido.

    Tampoco propone nada. No sugiere entrenar, no sugiere descansar, no dice qué
    hacer con el dato. Esta vista es un espejo; la sesión de hoy la decide el
    motor con sus reglas, y mezclar las dos cosas convertiría el contador en un
    argumento, que es exactamente lo que no puede ser.
    """
    from app.engine.message import fmt_date, fmt_num

    que = {
        FUERZA: "La sesión de fuerza",
        HIIT: "El bloque de HIIT",
        BICI: "La salida en bici",
    }.get(fila.kind, "La sesión")
    lineas = [
        f"{fmt_date(fila.date).capitalize()}: esa mañana te puntuaste "
        f"{fmt_num(fila.perception_index)} de 100 — percentil "
        f"{fmt_num(fila.perception_pct)} de tus mañanas, o sea que solo el "
        f"{fmt_num(fila.perception_pct)}% fueron peores.",
        f"{que} salió en el percentil {fmt_num(fila.performance_pct)} de las tuyas.",
    ]

    try:
        comp = json.loads(fila.components_json or "{}")
    except (TypeError, ValueError):
        comp = {}
    rend = comp.get("rendimiento") or {}
    trozos = (
        _frase_fuerza(rend.get("componentes") or {})
        if fila.kind in (FUERZA, HIIT)
        else _frase_bici(rend)
    )
    if trozos:
        lineas.append(f"Los números: {'; '.join(trozos)}.")

    lineas.append(
        f"Van {veces} de {de} sesiones en las que se ha podido comparar."
    )
    return "\n".join(lineas)


# ---------------------------------------------------------------------------
# Lo que falta por decir
# ---------------------------------------------------------------------------


def pendientes_de_avisar(
    session: Session, *, hasta: date | None = None, dias: int = 30
) -> list[SessionPerformance]:
    """Las disociaciones que todavía no se han contado en ningún Telegram.

    Solo la dirección que se destaca. La contraria se guarda y se cuenta en la
    pantalla, pero no se manda: el mensaje existe para una mañana concreta -la
    de levantarse pensando que no se puede entrenar- y avisar también de los
    días en que la mañana prometió de más convertiría el aviso en un comentario
    diario sobre el estado de ánimo. Eso no es lo que se pidió y no es lo que
    hace falta.

    La ventana es corta a propósito. Una disociación de hace tres semanas que no
    se avisó en su momento ya no es una noticia de esta mañana, y mandarla tarde
    solo enseñaría que el aviso no es de fiar.
    """
    hasta = hasta or date.today()
    desde = hasta - timedelta(days=dias - 1)
    return list(
        session.scalars(
            select(SessionPerformance)
            .where(
                SessionPerformance.reported_at.is_(None),
                SessionPerformance.dissociation.is_(True),
                SessionPerformance.direction == PERCEPCION_PEOR,
                SessionPerformance.date >= desde,
                SessionPerformance.date <= hasta,
            )
            .order_by(SessionPerformance.date)
        )
    )


def marcar_reportadas(
    session: Session, filas: list[SessionPerformance], *, cuando: Any
) -> None:
    """Apunta que ya se dijeron. Es lo ÚNICO que se reescribe de una fila.

    Y no es una excepción al append-only, es otra cosa: `reported_at` no forma
    parte del juicio de la sesión -no entra en ningún índice, ni en el contador,
    ni en el listado-, es la marca de "esto ya se ha contado". Sin ella, el
    mismo día saldría en el mensaje cada mañana hasta que se cambiara de
    ventana.

    Se escribe DESPUÉS de que el envío haya salido bien. Al revés -marcar y
    luego enviar- un fallo de red borraría el aviso sin haberlo dado, y nadie se
    enteraría de que faltó.
    """
    for f in filas:
        f.reported_at = cuando
