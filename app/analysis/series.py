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

import re
from dataclasses import dataclass, field
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
    # De qué otras series sale esta, si no tiene columna propia.
    #
    # Vacío para todo lo que se mide o se contesta. Lo lleva `discordancia`, que
    # es la única serie de aquí que no existe en ninguna tabla: se calcula, día a
    # día, de las dos respuestas de las que habla. Tenerlo como campo y no como
    # un `if clave == "discordancia"` escondido en la lectura es lo que hace que
    # la siguiente derivada no tenga que tocar la función que lee.
    deriva_de: tuple[str, ...] = ()
    # Cómo se llama esta serie DENTRO DE UNA FRASE, con su artículo puesto.
    #
    # "Variabilidad (HRV)" es un buen encabezado de columna y una frase
    # imposible: "salir en bici te baja Variabilidad (HRV) al día siguiente" no
    # lo dice nadie. La portada escribe en castellano, y para escribir en
    # castellano hace falta saber el género y el número de cada cosa.
    #
    # Es OBLIGATORIA y de palabra clave a propósito. Si tuviera un defecto -la
    # etiqueta, por ejemplo- añadir una serie nueva daría frases rotas sin un
    # solo error, y el fallo aparecería en la portada del usuario y en ningún
    # log. Sin defecto, una serie sin frase no llega ni a importarse.
    en_frase: str = field(kw_only=True)

    @property
    def sufijo(self) -> str | None:
        """Lo que se escribe DETRÁS de un valor suelto, o `None` si no hay nada.

        `unidad` hace dos trabajos distintos con la misma palabra. Para la HRV
        vale "ms" y es una unidad de verdad: se pega detrás del número y la frase
        queda bien -"44,8 ms"-. Para la nota de sueño vale "0-100" y eso NO es
        una unidad, es el rango de la escala: sirve para poner los topes de un
        eje, y pegado detrás de un valor da "85,5 0-100", que no lo escribiría
        nadie y que en la portada salía tal cual.

        No se arregla en la portada porque no es un problema de la portada: es
        que el campo mezcla dos cosas, y el que lo lee no tiene forma de saber
        cuál le ha tocado. Aquí se separan, y quien quiera el rango sigue
        teniendo `unidad` y `rango` intactos.

        Lo que parece un rango -dos números con un guión en medio- no es sufijo.
        Todo lo demás sí. La regla se escribe por la FORMA y no por una lista de
        claves a mano: una serie nueva con escala 0-10 acertaría sola, y una
        lista se habría quedado vieja en silencio el día que se añadiera.
        """
        if re.fullmatch(r"\d+(?:[.,]\d+)?-\d+(?:[.,]\d+)?", self.unidad):
            return None
        return self.unidad

    def como_dict(self) -> dict[str, Any]:
        return {
            "clave": self.clave,
            "etiqueta": self.etiqueta,
            "fuente": self.fuente,
            "unidad": self.unidad,
            "sufijo": self.sufijo,
            "sentido": self.sentido,
            "rango": list(self.rango) if self.rango else None,
            "desplazamiento_dias": self.desplazamiento,
            "en_frase": self.en_frase,
        }


# Los siete deslizadores. La lista de CLAVES vive en `config.yaml` y esta de aquí
# no la sustituye: `comprobar_sliders()` revienta si las dos dejan de coincidir.
# Lo que se añade aquí es lo que el YAML no sabe -el sentido y el desplazamiento-,
# que son propiedades del significado de cada deslizador, no de su formulario.
SLIDERS: dict[str, Definicion] = {
    d.clave: d
    for d in (
        Definicion(
            "fatigue", "Cansancio general", "checkin", "1-5", "alto_peor", (1, 5),
            en_frase="el cansancio",
        ),
        Definicion(
            "mood", "Ánimo", "checkin", "1-5", "alto_mejor", (1, 5),
            en_frase="el ánimo",
        ),
        Definicion(
            "upper_discomfort",
            "Molestias tronco superior",
            "checkin",
            "1-5",
            "alto_peor",
            (1, 5),
            en_frase="las molestias del tronco superior",
        ),
        Definicion(
            "lower_discomfort",
            "Molestias lumbares",
            "checkin",
            "1-5",
            "alto_peor",
            (1, 5),
            en_frase="las molestias lumbares",
        ),
        Definicion(
            "sleep_quality",
            "Calidad del sueño percibida",
            "checkin",
            "1-5",
            "alto_mejor",
            (1, 5),
            en_frase="lo bien que sientes que has dormido",
        ),
        Definicion(
            "training_desire", "Ganas de entrenar", "checkin", "1-5", "alto_mejor", (1, 5),
            en_frase="las ganas de entrenar",
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
            en_frase="lo duro que se te hizo el entreno",
        ),
    )
}

# Las dos preguntas de Sí/No, y la que sale de las dos.
#
# EN SU PROPIO DICCIONARIO, Y ESO ES LA MITAD DEL DISEÑO
# ------------------------------------------------------
# Meterlas en `SLIDERS` habría sido una línea menos y el error de siempre: hay
# sitios que recorren `SLIDERS` para decidir qué PUEDE mirar el semáforo, qué se
# pinta encima del calendario y qué sale en el desplegable de percepción. Un
# `will_train` colado ahí se habría ganado esos tres permisos sin que nadie los
# concediera, y el primero es justo el que el proyecto entero se ha dedicado a
# negar: «no me apetece» es una decisión, no una medida, y no puede pintar el
# semáforo de rojo.
#
# Separadas, cada sitio que las quiera tiene que nombrarlas. Eso es más trabajo
# hoy y es la razón de que mañana no se cuelen donde no deben.
#
# Y SE MIDEN EN 0-1, QUE NO ES UNA TRAMPA
# ---------------------------------------
# Un booleano como serie es una recta de ceros y unos, y su media es una
# proporción: 0,43 es «dijiste que sí el 43 % de los días». Eso se puede
# correlacionar -es lo que hace Spearman con cualquier variable de dos valores- y
# se puede pintar.
#
# Lo que NO se puede es dejar que un `None` entre en esa cuenta como 0. Ahí está
# toda la diferencia entre «contestaste que no» y «no contestaste», y es la misma
# distinción que el formulario, la API y el mensaje del día defienden cada uno por
# su lado. Aquí la defiende `_serie_checkin`, que convierte con `float(v)` y NUNCA
# con `bool(v)`, y que deja el `None` pasar de largo antes de convertir nada.
PREGUNTAS: dict[str, Definicion] = {
    d.clave: d
    for d in (
        Definicion(
            "wants_to_train",
            "Apetece entrenar (sí/no)",
            "checkin",
            "0-1",
            # Igual que `training_desire`, del que esto es la versión de dos
            # valores: que apetezca es la parte buena de la escala.
            "alto_mejor",
            (0, 1),
            en_frase="el apetito de entrenar",
        ),
        Definicion(
            "will_train",
            "Va a entrenar (sí/no)",
            "checkin",
            "0-1",
            # NEUTRO, y no es una duda: entrenar no es mejor que no entrenar. El
            # día que toca descanso, un «no» es el plan cumpliéndose. Ponerle
            # `alto_mejor` sería que el análisis escribiera en cada frase que
            # entrenar más es estar mejor, que es precisamente la creencia por la
            # que este sistema existe para no tenerla.
            "neutro",
            (0, 1),
            en_frase="la intención de entrenar",
        ),
        # La derivada. Sin columna: se calcula de las dos de arriba.
        Definicion(
            "discordancia",
            "Discordancia (apetece ≠ voy)",
            "checkin",
            "0-1",
            # NEUTRO otra vez, y aquí es lo importante de toda esta entrada.
            #
            # La discordancia junta dos cosas que no se parecen en nada: «me
            # apetecía y no fui» y «no me apetecía y fui». La primera es un
            # obstáculo; la segunda es disciplina, o es empeñarse, según el día.
            # Un solo bit no distingue cuál de las dos fue, así que declararla
            # «alto_peor» sería que el sistema diera por malo entrenar sin ganas
            # sin haberlo demostrado nunca.
            #
            # Por eso el binario viaja SIEMPRE con la tabla de las cuatro
            # casillas al lado: el binario es lo que se puede correlacionar, y
            # las cuatro casillas son lo que dice en qué dirección pasó.
            "neutro",
            (0, 1),
            deriva_de=("wants_to_train", "will_train"),
            en_frase="la distancia entre lo que te apetece y lo que haces",
        ),
    )
}

GARMIN: dict[str, Definicion] = {
    d.clave: d
    for d in (
        Definicion(
            "hrv", "Variabilidad (HRV)", "garmin", "ms", "alto_mejor",
            en_frase="la variabilidad",
        ),
        Definicion(
            "rhr", "FC en reposo", "garmin", "ppm", "alto_peor",
            en_frase="el pulso en reposo",
        ),
        Definicion(
            "sleep_min", "Sueño medido", "garmin", "min", "alto_mejor",
            en_frase="lo que duermes",
        ),
        Definicion(
            "sleep_score", "Nota de sueño", "garmin", "0-100", "alto_mejor", (0, 100),
            en_frase="la nota de sueño",
        ),
        Definicion(
            "body_battery", "Body Battery", "garmin", "0-100", "alto_mejor", (0, 100),
            en_frase="el Body Battery",
        ),
    )
}

ENTRENO: dict[str, Definicion] = {
    d.clave: d
    for d in (
        Definicion(
            "carga_bici", "Carga de la bici", "entreno", "carga", "neutro",
            en_frase="la carga de la bici",
        ),
        Definicion(
            "minutos_bici", "Minutos de bici", "entreno", "min", "neutro",
            en_frase="los minutos de bici",
        ),
        Definicion(
            "desnivel_bici", "Desnivel acumulado", "entreno", "m", "neutro",
            en_frase="el desnivel",
        ),
        Definicion(
            "volumen_fuerza", "Volumen de fuerza", "entreno", "kg", "neutro",
            en_frase="el volumen de fuerza",
        ),
        Definicion(
            "series_fuerza", "Series de fuerza", "entreno", "series", "neutro",
            en_frase="las series de fuerza",
        ),
    )
}

DEFINICIONES: dict[str, Definicion] = {**SLIDERS, **PREGUNTAS, **GARMIN, **ENTRENO}

# Lo que no sale de ninguna columna. Se calcula al leer, de las series que diga
# su `deriva_de`.
DERIVADAS: dict[str, Definicion] = {
    k: d for k, d in DEFINICIONES.items() if d.deriva_de
}

# Las cinco de `ENTRENO` hacen doble papel: son RESPUESTA -"¿qué pasa con el
# desnivel?"- y son EXPOSICIÓN -"¿qué te hace acumular desnivel?"-, y en cada
# papel se nombran distinto. Como respuesta es "el desnivel"; como exposición
# tiene que ser un infinitivo, porque la frase es "acumular desnivel te baja la
# variabilidad" y no "el desnivel te baja la variabilidad", que suena a que el
# desnivel actúa solo.
COMO_EXPOSICION: dict[str, str] = {
    "carga_bici": "acumular carga en la bici",
    "minutos_bici": "acumular minutos de bici",
    "desnivel_bici": "acumular desnivel",
    "volumen_fuerza": "acumular volumen de fuerza",
    "series_fuerza": "acumular series",
}


def cuadran(
    registro: dict[str, Any],
    contra: dict[str, Any],
    *,
    nombre: str,
    nombre_contra: str,
    falta: str,
) -> None:
    """Dos diccionarios que tienen que llevar las mismas claves, o `ValueError`.

    Es el mismo patrón dos veces -`COMO_EXPOSICION` contra `ENTRENO`, `_COMBINA`
    contra `DERIVADAS`- y las dos se comprueban al IMPORTAR, que es todo el
    punto: el fallo que evitan no se parece a un fallo. Una serie sin su entrada
    no revienta al arrancar; revienta -o peor, calla- el día que alguien abre la
    pantalla que la usa, con el usuario delante y sin nada en el log.

    Y está sacada a función, en vez de repetida en dos `if`, porque un `if` a
    nivel de módulo no se puede probar: se ejecuta una sola vez, al importar, con
    los datos buenos, y cualquier test que lo mire acaba comprobando los datos y
    no la comprobación. Eso ya pasó aquí -la batería de mutaciones cambió el `if`
    por un `if False:` y nadie se quejó-, y una guardia que nadie vigila es una
    guardia que alguien borrará por inútil.
    """
    if set(registro) == set(contra):
        return
    raise ValueError(
        f"`{nombre}` y `{nombre_contra}` han dejado de coincidir "
        f"(sobran: {sorted(set(registro) - set(contra))}; "
        f"faltan: {sorted(set(contra) - set(registro))}). {falta}"
    )


# Sin defecto y comprobado al importar. Una serie de entreno nueva sin su
# infinitivo no daría un error: daría una frase con la etiqueta de tabla metida
# a la fuerza -"Desnivel acumulado te baja la variabilidad"- o, peor, un hueco
# donde tendría que ir el sujeto. Y eso solo se vería en la pantalla del
# usuario, nunca en un log.
cuadran(
    COMO_EXPOSICION,
    ENTRENO,
    nombre="COMO_EXPOSICION",
    nombre_contra="ENTRENO",
    falta="Toda serie de entreno necesita cómo se nombra cuando es la causa y "
    "no el efecto.",
)


def _discordancia(partes: tuple[float | None, ...]) -> float | None:
    """1 si una de las dos respuestas dice sí y la otra no; 0 si van juntas.

    La primera línea es la única que importa: si falta CUALQUIERA de las dos, el
    día no vale. No hay discordancia de la que hablar cuando solo se sabe la
    mitad, y darle un 0 -"pues no hubo discordancia"- sería inventar un día de
    coherencia cada vez que el formulario se envió a medias.

    Es la misma cuenta, con las mismas palabras, que hace `signals.py` para el
    mensaje del día. Que esté escrita dos veces es a propósito: allí se decide
    con los valores de HOY y aquí se lee el histórico de meses, y juntarlas
    obligaría a uno de los dos a cargar con la forma del otro. Lo que no puede
    pasar es que difieran, y de eso hay test.
    """
    apetece, voy = partes
    if apetece is None or voy is None:
        return None
    return 1.0 if bool(apetece) != bool(voy) else 0.0


# Cómo se calcula cada derivada, al lado de la lista de derivadas y no dentro de
# la función que lee. Igual que `_COLUMNAS_ENTRENO`: quien añada una serie que
# sale de otras pone aquí su cuenta y no toca `_serie_derivada`.
_COMBINA: dict[str, Any] = {
    "discordancia": _discordancia,
}

# Y comprobado al importar, por lo mismo que `COMO_EXPOSICION`: una derivada sin
# su cuenta no daría un error al arrancar, daría un `KeyError` el día que alguien
# pidiera esa serie desde la portada, con el usuario delante.
cuadran(
    _COMBINA,
    DERIVADAS,
    nombre="_COMBINA",
    nombre_contra="DERIVADAS",
    falta="Toda serie con `deriva_de` necesita la cuenta que la saca de sus partes.",
)

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


def comprobar_preguntas(config: Any) -> None:
    """Lo mismo que `comprobar_sliders`, para las preguntas de Sí/No.

    No existía, y esa es la razón de que exista ahora. `comprobar_sliders` lleva
    escrito desde el principio por qué hace falta -un deslizador nuevo que no
    esté en la tabla no aparece en ninguna vista y no da ningún error-, y las dos
    preguntas nacieron con el mismo agujero abierto y sin nadie mirándolo. Una
    tercera pregunta -«¿has dormido fuera de casa?», la que sea- se habría
    contestado todas las mañanas, se habría guardado en su columna, y no habría
    salido en una sola correlación. El formulario la pediría, la base la
    guardaría y el análisis no la habría visto nunca.

    Las DERIVADAS no se comparan contra el YAML a propósito: `discordancia` no es
    una pregunta que nadie conteste, es lo que sale de restar dos que sí. Exigir
    que estuviera en `checkin_preguntas` obligaría a poner en el formulario una
    pregunta que el formulario no puede hacer.
    """
    from app.repository import preguntas_del_config

    del_yaml = set(preguntas_del_config(config))
    de_aqui = set(PREGUNTAS) - set(DERIVADAS)
    if del_yaml == de_aqui:
        return
    faltan = sorted(del_yaml - de_aqui)
    sobran = sorted(de_aqui - del_yaml)
    partes = []
    if faltan:
        partes.append(f"en el config pero no en `PREGUNTAS`: {faltan}")
    if sobran:
        partes.append(f"en `PREGUNTAS` pero no en el config: {sobran}")
    raise ValueError(
        "las preguntas del análisis no coinciden con `checkin_preguntas` del "
        "config (" + "; ".join(partes) + "). Añádela a `app/analysis/series.py` "
        "con su sentido y su frase, o quítala del YAML."
    )


def comprobar_series(config: Any) -> None:
    """Las dos comprobaciones, en una sola llamada.

    Existe porque `comprobar_sliders(cfg)` estaba copiado literal en las siete
    vistas de métricas, y añadir una segunda comprobación habría sido copiarla
    siete veces más y olvidarla en la octava vista que se escribiera. Con una
    sola puerta, la lista que se añada mañana queda vigilada en todas partes sin
    tocar ni un endpoint.
    """
    comprobar_sliders(config)
    comprobar_preguntas(config)


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
    # Antes que la fuente, porque una derivada TIENE fuente -`discordancia` es
    # del check-in, y así sale en el desplegable junto a las dos de las que
    # viene- pero no tiene columna. Preguntar primero por la fuente la mandaría a
    # `_serie_checkin` a buscar un `Checkin.discordancia` que no existe.
    if d.deriva_de:
        return _serie_derivada(session, d, desde, hasta, cob)
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
        # `float(v)` y no `bool(v)`, y la diferencia solo se nota con las dos
        # preguntas de Sí/No. `float` de un booleano da 1.0 o 0.0 y de un `None`
        # revienta -por eso el guardia va delante-; `bool` de un `None` da
        # `False` sin quejarse, y ahí se habría perdido para siempre la
        # diferencia entre "contestaste que no" y "no contestaste". Toda la
        # columna se leería como una fila de noes.
        salida[a_fecha(f) - timedelta(days=d.desplazamiento)] = (
            None if v is None else float(v)
        )
    return salida


def _serie_derivada(
    session: Session,
    d: Definicion,
    desde: date,
    hasta: date,
    cob: Cobertura | None,
) -> dict[date, float | None]:
    """La serie que no está en ninguna tabla: se saca de las que dice `deriva_de`.

    Los días son la UNIÓN de los días de sus partes, no la intersección. Parece
    lo contrario de lo que conviene -un día con una sola mitad no puede dar un
    valor- pero da igual: la cuenta devuelve `None` para ese día, y un día con
    `None` y un día que no está valen lo mismo para `emparejar`. Con la
    intersección el resultado sería el mismo diccionario menos unas claves
    nulas, y a cambio habría que decidir aquí qué significa que falte una parte,
    que es justo lo que decide la cuenta.
    """
    partes = [serie(session, k, desde, hasta, cob=cob) for k in d.deriva_de]
    combina = _COMBINA[d.clave]
    dias: set[date] = set()
    for p in partes:
        dias |= set(p)
    return {dia: combina(tuple(p.get(dia) for p in partes)) for dia in sorted(dias)}


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
