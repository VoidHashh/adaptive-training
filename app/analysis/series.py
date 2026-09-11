"""De la base de datos a `dict[date, float | None]`, que es lo único que come `stats`.

`stats` no sabe nada de SQLAlchemy ni de deslizadores: recibe dos series
indexadas por fecha y devuelve una correlación. Este módulo es el puente, y casi
todo lo delicado del análisis vive aquí y no allí, porque las decisiones difíciles
no son estadísticas, son de contabilidad: qué día le toca a cada número, y qué
significa que un día no tenga fila.

EL AUSENTE Y EL CERO, OTRA VEZ
------------------------------
Es la misma distinción que en `backfill`, pero al revés de como suele plantearse.
En bienestar, la ausencia de fila es SIEMPRE un hueco: el reloj se lleva puesto
todas las noches, y si no hay HRV de un martes es que el dato se perdió, no que
ese martes el corazón no latiera. En entreno es al contrario: la ausencia de
actividad un martes normalmente significa que ese martes no se entrenó, y eso es
un cero de verdad, un dato tan bueno como un 180 de carga.

Pero solo DENTRO de la ventana observada. Fuera de ella -antes de la primera
actividad guardada, o después de la última- la ausencia no dice nada: puede ser
descanso o puede ser que el sistema todavía no existiera. Por eso cada serie de
entreno se recorta a `cobertura()` y devuelve `None` fuera, en vez de rellenar de
ceros hasta el infinito y fabricar así una racha de descanso de meses que nunca
ocurrió.

EL DESLIZADOR QUE HABLA DE AYER
-------------------------------
`yesterday_rpe` se contesta el día D y describe el entreno del día D-1. Si se
emparejara por la fecha de la fila, el esfuerzo del lunes saldría comparado con
la carga del martes, y la correlación -la más fácil de todas, porque es casi una
identidad- saldría cercana a cero. Con la ventana de desfase de la Vista 2
encima, el error sería peor que un cero: aparecería un desfase de +1 día
perfectamente nítido que no es una propiedad de su percepción, sino del
formulario.

Así que la serie se desplaza al escribirla: el valor va guardado en la fecha DEL
ENTRENO QUE DESCRIBE, no en la del día que se contestó. Todo lo que pregunte por
`yesterday_rpe` recibe ya la serie corregida, y no hay ningún sitio donde se
pueda olvidar hacerlo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Activity, Checkin, DailyMetrics, WorkoutLog

# ---------------------------------------------------------------------------
# Qué series existen
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Definicion:
    """Lo que hay que saber de una serie para pintarla y para leer su signo.

    `sentido` es lo que convierte un `r = -0.4` en una frase. Un número negativo
    entre cansancio y HRV significa que coinciden -más cansancio, menos
    variabilidad-, y entre cansancio y FC en reposo significa lo contrario. Sin
    saber para cada serie si lo alto es bueno o malo, el signo no se puede
    traducir y la PWA tendría que llevar la tabla en JavaScript, que es justo lo
    que no puede llevar.
    """

    clave: str
    etiqueta: str
    fuente: str  # checkin | garmin | entreno
    unidad: str
    sentido: str  # alto_peor | alto_mejor | neutro
    rango: tuple[float, float] | None = None
    # Días que hay que RESTAR a la fecha de la fila para colocar el valor en el
    # día del que habla. Hoy solo `yesterday_rpe` lo usa; ver la cabecera.
    desplazamiento: int = 0

    def como_dict(self) -> dict[str, Any]:
        return {
            "clave": self.clave,
            "etiqueta": self.etiqueta,
            "fuente": self.fuente,
            "unidad": self.unidad,
            "sentido": self.sentido,
            "rango": list(self.rango) if self.rango else None,
            "desplazamiento_dias": self.desplazamiento,
        }


# Los siete deslizadores. La lista de CLAVES vive en `config.yaml` y esta de aquí
# no la sustituye: `comprobar_sliders()` revienta si las dos dejan de coincidir.
# Lo que se añade aquí es lo que el YAML no sabe -el sentido y el desplazamiento-,
# que son propiedades del significado de cada deslizador, no de su formulario.
SLIDERS: dict[str, Definicion] = {
    d.clave: d
    for d in (
        Definicion("fatigue", "Cansancio general", "checkin", "1-5", "alto_peor", (1, 5)),
        Definicion("mood", "Ánimo", "checkin", "1-5", "alto_mejor", (1, 5)),
        Definicion(
            "upper_discomfort",
            "Molestias tronco superior",
            "checkin",
            "1-5",
            "alto_peor",
            (1, 5),
        ),
        Definicion(
            "lower_discomfort",
            "Molestias lumbares",
            "checkin",
            "1-5",
            "alto_peor",
            (1, 5),
        ),
        Definicion(
            "sleep_quality",
            "Calidad del sueño percibida",
            "checkin",
            "1-5",
            "alto_mejor",
            (1, 5),
        ),
        Definicion(
            "training_desire", "Ganas de entrenar", "checkin", "1-5", "alto_mejor", (1, 5)
        ),
        # Neutro a propósito: entrenar duro no es ni bueno ni malo, depende de lo
        # que tocara ese día. Y desplazado uno, que es lo importante.
        Definicion(
            "yesterday_rpe",
            "Esfuerzo percibido del entreno",
            "checkin",
            "1-5",
            "neutro",
            (1, 5),
            desplazamiento=1,
        ),
    )
}

GARMIN: dict[str, Definicion] = {
    d.clave: d
    for d in (
        Definicion("hrv", "Variabilidad (HRV)", "garmin", "ms", "alto_mejor"),
        Definicion("rhr", "FC en reposo", "garmin", "ppm", "alto_peor"),
        Definicion("sleep_min", "Sueño medido", "garmin", "min", "alto_mejor"),
        Definicion("sleep_score", "Nota de sueño", "garmin", "0-100", "alto_mejor", (0, 100)),
        Definicion("body_battery", "Body Battery", "garmin", "0-100", "alto_mejor", (0, 100)),
    )
}

ENTRENO: dict[str, Definicion] = {
    d.clave: d
    for d in (
        Definicion("carga_bici", "Carga de la bici", "entreno", "carga", "neutro"),
        Definicion("minutos_bici", "Minutos de bici", "entreno", "min", "neutro"),
        Definicion("desnivel_bici", "Desnivel acumulado", "entreno", "m", "neutro"),
        Definicion("volumen_fuerza", "Volumen de fuerza", "entreno", "kg", "neutro"),
        Definicion("series_fuerza", "Series de fuerza", "entreno", "series", "neutro"),
    )
}

DEFINICIONES: dict[str, Definicion] = {**SLIDERS, **GARMIN, **ENTRENO}

# Las columnas reales detrás de cada serie de entreno, y de qué tabla salen.
_COLUMNAS_ENTRENO: dict[str, tuple[Any, Any]] = {
    "carga_bici": (Activity, Activity.training_load),
    "minutos_bici": (Activity, Activity.duration_s),
    "desnivel_bici": (Activity, Activity.elevation_gain_m),
    "volumen_fuerza": (WorkoutLog, WorkoutLog.total_volume_kg),
    "series_fuerza": (WorkoutLog, WorkoutLog.total_sets),
}


def comprobar_sliders(config: Any) -> None:
    """Revienta si el YAML y `SLIDERS` dejan de decir lo mismo.

    El día que se añada un octavo deslizador al formulario, esta tabla se
    quedaría con siete y el deslizador nuevo no aparecería en ninguna vista. No
    saldría ningún error: saldrían las mismas seis correlaciones de siempre y el
    dato nuevo no estaría, que es exactamente la clase de fallo callado que en
    este proyecto se convierte en error duro.
    """
    from app.repository import sliders_del_config

    del_yaml = set(sliders_del_config(config))
    de_aqui = set(SLIDERS)
    if del_yaml == de_aqui:
        return
    faltan = sorted(del_yaml - de_aqui)
    sobran = sorted(de_aqui - del_yaml)
    partes = []
    if faltan:
        partes.append(f"en el config pero no en `SLIDERS`: {faltan}")
    if sobran:
        partes.append(f"en `SLIDERS` pero no en el config: {sobran}")
    raise ValueError(
        "los deslizadores del análisis no coinciden con `checkin_sliders` del "
        "config (" + "; ".join(partes) + "). Añádelo a `app/analysis/series.py` "
        "con su sentido, o quítalo del YAML."
    )


# ---------------------------------------------------------------------------
# Lectura
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cobertura:
    """El primer y el último día con datos de cada fuente.

    Sale en todos los endpoints. No es adorno: es la única forma de que un "no
    hay muestra suficiente" se pueda distinguir de "esta métrica lleva rota
    desde marzo". Con los extremos delante, un hueco al final se lee como lo que
    es -algo dejó de llegar- y no como falta de histórico.
    """

    checkin: tuple[date, date] | None = None
    garmin: tuple[date, date] | None = None
    bici: tuple[date, date] | None = None
    fuerza: tuple[date, date] | None = None

    def como_dict(self) -> dict[str, Any]:
        def par(v: tuple[date, date] | None) -> dict[str, str] | None:
            return {"desde": v[0].isoformat(), "hasta": v[1].isoformat()} if v else None

        return {
            "checkin": par(self.checkin),
            "garmin": par(self.garmin),
            "bici": par(self.bici),
            "fuerza": par(self.fuerza),
        }


def cobertura(session: Session) -> Cobertura:
    """Los extremos reales de cada tabla, de una sola pasada por tabla."""

    def extremos(columna: Any) -> tuple[date, date] | None:
        fila = session.execute(
            select(func.min(columna), func.max(columna))
        ).first()
        if fila is None or fila[0] is None or fila[1] is None:
            return None
        return (a_fecha(fila[0]), a_fecha(fila[1]))

    return Cobertura(
        checkin=extremos(Checkin.date),
        garmin=extremos(DailyMetrics.date),
        bici=extremos(Activity.date),
        fuerza=extremos(WorkoutLog.date),
    )


def a_fecha(v: Any) -> date:
    """SQLite devuelve a veces la fecha como texto según por dónde se lea."""
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def serie(
    session: Session, clave: str, desde: date, hasta: date, *, cob: Cobertura | None = None
) -> dict[date, float | None]:
    """La serie de una métrica entre dos fechas, ambas incluidas.

    Devuelve SOLO los días que tienen algo que decir. Un día que no está en el
    diccionario y un día que está con `None` significan lo mismo para `emparejar`
    -se descarta el par-, así que no se rellena el hueco con nada.
    """
    if clave not in DEFINICIONES:
        raise ValueError(
            f"serie desconocida: {clave!r}. Las que hay: {sorted(DEFINICIONES)}"
        )
    d = DEFINICIONES[clave]
    if d.fuente == "checkin":
        return _serie_checkin(session, d, desde, hasta)
    if d.fuente == "garmin":
        return _serie_garmin(session, d, desde, hasta)
    return _serie_entreno(session, d, desde, hasta, cob or cobertura(session))


def _serie_checkin(
    session: Session, d: Definicion, desde: date, hasta: date
) -> dict[date, float | None]:
    # Se pide desplazada para que al restar el desplazamiento el resultado caiga
    # dentro de la ventana que pidió quien llama. Con `yesterday_rpe` y una
    # ventana que acaba hoy, esto lee el check-in de mañana -que no existe- sin
    # que pase nada, y lee el de hoy para colocarlo en ayer, que es lo que hace
    # falta y se habría perdido si la ventana se leyera tal cual.
    col = getattr(Checkin, d.clave)
    filas = session.execute(
        select(Checkin.date, col).where(
            Checkin.date >= desde + timedelta(days=d.desplazamiento),
            Checkin.date <= hasta + timedelta(days=d.desplazamiento),
        )
    ).all()
    salida: dict[date, float | None] = {}
    for f, v in filas:
        salida[a_fecha(f) - timedelta(days=d.desplazamiento)] = (
            None if v is None else float(v)
        )
    return salida


def _serie_garmin(
    session: Session, d: Definicion, desde: date, hasta: date
) -> dict[date, float | None]:
    col = getattr(DailyMetrics, d.clave)
    filas = session.execute(
        select(DailyMetrics.date, col).where(
            DailyMetrics.date >= desde, DailyMetrics.date <= hasta
        )
    ).all()
    return {a_fecha(f): (None if v is None else float(v)) for f, v in filas}


def _serie_entreno(
    session: Session, d: Definicion, desde: date, hasta: date, cob: Cobertura
) -> dict[date, float | None]:
    """Suma por día, con ceros dentro de la ventana observada y nada fuera.

    Suma y no media porque dos salidas el mismo día son un día de más carga, no
    un día de carga media. Y los días sin fila valen `0.0` -son descanso- salvo
    fuera de la cobertura, donde valen `None` porque allí nadie miró.
    """
    modelo, columna = _COLUMNAS_ENTRENO[d.clave]
    filtros = [modelo.date >= desde, modelo.date <= hasta]
    if modelo is Activity:
        filtros.append(Activity.is_cycling.is_(True))

    filas = session.execute(
        select(modelo.date, func.sum(columna)).where(*filtros).group_by(modelo.date)
    ).all()
    crudo = {a_fecha(f): v for f, v in filas}

    ventana = cob.bici if modelo is Activity else cob.fuerza
    salida: dict[date, float | None] = {}
    dia = desde
    while dia <= hasta:
        if ventana is None or dia < ventana[0] or dia > ventana[1]:
            salida[dia] = None
        elif dia in crudo:
            # La fila existe pero la columna puede venir nula (una salida sin
            # carga estimada). Eso NO es un cero: es una salida sin ese dato.
            v = crudo[dia]
            salida[dia] = None if v is None else float(v)
        else:
            salida[dia] = 0.0
        dia += timedelta(days=1)

    if d.clave == "minutos_bici":
        salida = {k: (None if v is None else v / 60.0) for k, v in salida.items()}
    return salida


# ---------------------------------------------------------------------------
# Escala común
# ---------------------------------------------------------------------------


def normalizar(valores: dict[date, float | None]) -> dict[date, float | None]:
    """Lleva una serie a 0-100 por su propio percentil histórico.

    Es lo que permite pintar el cansancio -que va de 1 a 5- encima de la HRV
    -que va de 20 a 90- en el mismo eje sin que una de las dos sea una línea
    plana pegada al suelo. Y se hace por percentil, no por regla de tres entre
    el mínimo y el máximo, por dos razones. La primera es que un solo día raro
    -una HRV de 12 la noche de una gripe- aplastaría el resto de la serie contra
    el techo. La segunda es la que vale: un percentil dice "este martes fue de
    los peores que has tenido", que es la frase que de verdad se quiere leer, y
    no depende de si un 45 de HRV es alto o bajo para otra persona.

    Los nulos siguen siendo nulos. Un hueco normalizado sigue siendo un hueco.
    """
    from app.analysis.stats import percentil_de

    muestra = [v for v in valores.values() if v is not None]
    if not muestra:
        return {k: None for k in valores}
    return {
        k: (None if v is None else percentil_de(v, muestra)) for k, v in valores.items()
    }
