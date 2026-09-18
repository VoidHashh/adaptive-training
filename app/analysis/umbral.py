"""Cuánta bici te pasa factura, y cuánto dura la factura.

LAS DOS PREGUNTAS, Y POR QUÉ VAN JUNTAS
---------------------------------------
Son las dos mitades de la misma decisión de la mañana. "¿Me paso con esta
salida?" no se puede contestar sin saber a partir de qué se nota, y "¿puedo
entrenar mañana?" no se puede contestar sin saber cuánto dura lo que se notó.
Separadas en dos vistas, cada una enseñaría media respuesta.

Todo lo de aquí mide UNA cosa: la HRV de la mañana siguiente a una salida,
comparada con la de la mañana de la salida. Es decir, la noche de después menos
la noche de antes.

    delta(d, k) = hrv[d + k] - hrv[d]

`hrv[d]` es la noche que ACABA la mañana del día `d`, o sea la noche anterior a
la salida de ese día. Así que `delta(d, 1)` es exactamente lo que cambió la
noche de después respecto de la de antes, y la resta se lleva por delante la
deriva lenta -la forma, la estación, el sueño de esa semana- sin tener que
modelarla. Es la comparación más corta posible, y la más difícil de discutir.

POR QUÉ UNA RESTA Y NO UNA CORRELACIÓN
--------------------------------------
La vista de Impacto ya correlaciona la carga con la HRV del día siguiente, y
devuelve una `r`. Una `r` contesta "¿van juntas?" y no contesta "¿a partir de
cuánto?", que es literalmente la pregunta. Una relación puede ser monótona y
perfectamente lineal y no tener ningún umbral, o ser casi plana hasta un punto y
caerse a plomo después: la misma `r` para las dos.

Aquí se parte la carga en tramos y se mira la media de cada uno. Es más tosco y
contesta lo que se pregunta.

DE DÓNDE SALEN LOS CORTES: DE SU PROPIA DISTRIBUCIÓN
-----------------------------------------------------
Los cortes son percentiles de SUS PROPIAS cargas, no números redondos. Un tramo
"de 0 a 50" es un número inventado por quien escribe el código; el percentil de
sus salidas es un número que dice algo de él. La consecuencia que importa: el
umbral SALE del dato en vez de entrar por el código. Si en este módulo estuviera
escrito un 150, el panel encontraría 150 con cualquier base de datos del mundo,
incluida una vacía.

Hay DOS reglas de percentiles y no una, y la diferencia no es un descuido:

- `CORTES_TRAMOS`, los cuartiles, para la TABLA. Cuatro tramos con una docena de
  salidas cada uno se leen; nueve tramos con cinco salidas cada uno son ruido
  con pinta de tabla.
- `CORTES_FRONTERA`, los deciles, para BUSCAR EL ESCALÓN. Aquí no se lee cada
  candidato: se compara entre ellos y gana uno.

Separarlas costó una medición, y merece la pena contarla porque la intuición
decía lo contrario. Con cuartiles la frontera salía en 174; con quintiles -una
sola marca más- saltaba a 191; con deciles, 147. Un número que se mueve treinta
puntos según cuántas marcas tenga la regla no está midiendo el cuerpo, está
midiendo la regla. Las dos reglas finas coinciden entre ellas, y eso es lo que
hace pensar que 147 es el sitio y 174 era el cuartil más cercano al sitio.

El miedo razonable a afinar la regla es el sobreajuste: con más candidatos
siempre gana alguno, y gana más fuerte. Lo que descarta esa lectura aquí es que
va AL REVÉS. Al pasar de siete candidatos a nueve -corrección de Benjamini-
Hochberg más dura, porque hay más miradas- los cortes de al lado se caen y el de
147 aguanta, y aguanta con la `p` más pequeña de toda la tabla. El sobreajuste
se deshace cuando lo aprietas; esto se queda solo en pie.

Aun así, la frontera se publica como lo que es: el mejor de una lista, no un
número medido. Por eso salen TODOS los candidatos con su `n` y su `p` al lado,
y por eso va la relación continua de control junto a ellos.

Y no se supone el signo. Si el tramo duro sale con la HRV MÁS ALTA, la frase lo
dice tal cual. Suponer la dirección fisiológica y quedarse solo con los cortes
que van "hacia donde tienen que ir" es la forma más limpia de no enterarse nunca
de lo contrario -que es justo lo que pasó con el sueño invertido-.

LAS SALIDAS AISLADAS, PARA LA DURACIÓN
--------------------------------------
Para el umbral valen todas las salidas. Para la DURACIÓN no: si al día siguiente
hubo otra salida, el día +2 ya no mide lo que duró la primera, mide la suma de
las dos. Así que la curva de recuperación solo usa salidas sin ninguna otra en
los dos días de antes ni en los dos de después.

Un día cuya carga es `None` -salió sin carga estimada, o está fuera de la
cobertura- CUENTA COMO QUE ROMPE EL AISLAMIENTO. No es lo mismo "sé que no
salió" que "no sé si salió", y llamar aislada a una salida por un hueco de datos
es inventarse el aislamiento. El precio es que las salidas de los bordes de la
ventana nunca son aisladas, y es el precio correcto.

LAS BARRAS DE LA CURVA NO LLEVAN p, Y ESO ESTÁ ESCRITO
-------------------------------------------------------
Cada barra es una media contra cero, y aquí no hay ningún contraste de una sola
muestra: `stats.py` sabe correlacionar dos series y nada más. Poner una `p` de
otra cosa al lado sería peor que no ponerla. Así que cada barra lleva su `n` y
su recorrido entre cuartiles -que es lo que dice cuánto se mueve de una salida a
otra- y `significativa` viene en `None`, que en este panel significa "a esto no
se le ha aplicado ninguna corrección", no "no ha pasado el filtro".

Los cortes del umbral SÍ llevan `p`, porque ahí sí hay dos grupos que comparar,
y van corregidos por Benjamini-Hochberg junto con la relación continua: diez
miradas a los mismos datos son diez oportunidades de encontrar algo por azar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from statistics import fmean
from typing import Any

from sqlalchemy.orm import Session

from app.analysis import series as S
from app.analysis.stats import (
    N_MINIMO_CALCULABLE,
    N_MINIMO_FIABLE,
    correlacion,
    corregir_tanda,
    emparejar,
    percentil,
)
from app.analysis.texto import cuantos

# La respuesta que se mira y la exposición que la mueve. Están aquí arriba y no
# repartidas por el módulo porque el día que se quiera hacer lo mismo con el
# pulso en reposo, lo que hay que cambiar tiene que estar en un sitio.
RESPUESTA = "hrv"
EXPOSICION = "carga_bici"

# Hasta dónde se sigue la recuperación. Cuatro días y no tres como en Impacto:
# la pregunta de esta vista es CUÁNDO VUELVE, y una ventana que acaba justo
# cuando el efecto se está deshaciendo contesta siempre "todavía se nota".
DIAS_DESPUES = (1, 2, 3, 4)

# Cuántos días a cada lado tienen que estar limpios para llamar aislada a una
# salida. Dos y no uno porque la curva llega a +4: con un solo día de margen,
# una salida el día +2 se comería la mitad de la curva que se está midiendo.
DIAS_AISLAMIENTO = 2

# Mínimos. `N_MINIMO_TRAMO` es el de una media -tres valores ya dicen algo, uno
# no dice nada- y es a propósito más bajo que `N_MINIMO_FIABLE`: con cuarenta y
# nueve salidas repartidas en cuatro tramos, exigir veinte por tramo dejaría la
# vista entera en blanco para siempre. Lo que se hace en vez de esconderlo es
# publicar el `n` de cada tramo pegado a su media.
N_MINIMO_TRAMO = 3

# Las dos reglas de percentiles, en percentiles de su propia distribución de
# cargas. La de la tabla es gorda porque cada tramo se lee suelto y necesita
# salidas dentro; la de la búsqueda es fina porque ahí los candidatos no se leen
# sueltos, se comparan. Ver la cabecera del módulo: el número que se publica
# cambia según cuál se use, y ésa es justamente la razón de haberlo medido.
CORTES_TRAMOS = (25.0, 50.0, 75.0)
CORTES_FRONTERA = (10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0)

# La media móvil de la gráfica y cuántos días de verdad hacen falta dentro de la
# ventana para publicar un punto suavizado. Sin el mínimo, los extremos de la
# serie saldrían calculados sobre tres días y dibujados igual que el centro, que
# es una cola plana inventada con pinta de medida.
SUAVIZADO = 7
MINIMO_SUAVIZADO = 4

# La banda de "lo normal" que va detrás de la línea: sus propios cuartiles.
BANDA = (25.0, 75.0)


# ---------------------------------------------------------------------------
# Recoger las salidas con su antes y su después
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Salida:
    """Una salida, con lo que la HRV hizo antes y después.

    `antes` es la HRV de la mañana de la salida, o sea la noche ANTERIOR: es el
    punto de partida contra el que se mide todo lo demás. `despues[k]` es la
    diferencia respecto de ese punto, no el valor absoluto, porque el valor
    absoluto no se puede comparar entre una salida de marzo y una de septiembre.
    """

    fecha: date
    carga: float
    antes: float
    aislada: bool
    porque_no: str | None
    despues: dict[int, float | None]


def _aislamiento(
    cargas: dict[date, float | None], dia: date, *, margen: int = DIAS_AISLAMIENTO
) -> tuple[bool, str | None]:
    """¿Está sola esta salida? Y si no, por qué no.

    Un `None` en un día vecino NO es un día de descanso: es un día del que no se
    sabe nada. Rompe el aislamiento igual que una salida, y el motivo lo dice
    con otras palabras para que se pueda distinguir una salida real de un hueco.
    """
    for paso in range(1, margen + 1):
        for vecino in (dia - timedelta(days=paso), dia + timedelta(days=paso)):
            v = cargas.get(vecino)
            if v is None:
                return False, f"no se sabe si hubo salida el {vecino.isoformat()}"
            if v > 0:
                return False, f"hubo otra salida el {vecino.isoformat()}"
    return True, None


def _en_cobertura(cob: S.Cobertura, dia: date) -> bool:
    """¿Este día cae dentro del tramo del que se sabe algo de bici?"""
    if cob.bici is None:
        return False
    return cob.bici[0] <= dia <= cob.bici[1]


def recoger(
    session: Session,
    *,
    desde: date,
    hasta: date,
    cob: S.Cobertura | None = None,
    retardos: tuple[int, ...] = DIAS_DESPUES,
) -> tuple[list[Salida], dict[str, int], dict[str, int]]:
    """Todas las salidas de la ventana que tienen con qué medirse.

    Devuelve también el recuento de lo que se ha quedado fuera y por qué, y
    aparte los días de la ventana que caen fuera de la cobertura de bici. Ese
    recuento es la mitad del encabezado de la vista: sin él, una ventana con
    cuarenta salidas y dos medibles se lee igual que una con dos salidas.

    Y los de fuera de cobertura van partidos en ANTES y DESPUÉS, que no son la
    misma noticia. Antes es histórico que no existe y no va a existir nunca.
    Después es la cola de la ventana sin nada apuntado todavía, o sea casi
    siempre que el reloj lleva unos días sin sincronizar; contarlos juntos
    escribía "3 días de la ventana son de antes de la bici" para tres días de
    hace una semana, que es decir lo contrario de lo que pasa y además esconder
    lo único de los dos que se puede arreglar.
    """
    cob = cob or S.cobertura(session)
    margen = max(retardos) if retardos else 0

    # Las cargas se piden con margen POR LOS DOS LADOS: el aislamiento mira dos
    # días hacia atrás desde la primera salida de la ventana, y sin ese margen
    # esos días vendrían ausentes -no `None`, ausentes- y `dict.get` los daría
    # como `None`, que rompe el aislamiento por un motivo falso.
    cargas = S.serie(
        session,
        EXPOSICION,
        desde - timedelta(days=DIAS_AISLAMIENTO),
        hasta + timedelta(days=DIAS_AISLAMIENTO),
        cob=cob,
    )
    hrv = S.serie(
        session, RESPUESTA, desde, hasta + timedelta(days=margen), cob=cob
    )

    salidas: list[Salida] = []
    fuera = {"sin_carga": 0, "sin_hrv_antes": 0, "sin_hrv_despues": 0}
    sin_cobertura = {"antes": 0, "despues": 0}

    dia = desde
    while dia <= hasta:
        carga = cargas.get(dia)
        if carga is None:
            # Un `None` puede ser dos cosas MUY distintas, y meterlas en el
            # mismo saco rompe el denominador del encabezado. Fuera de la
            # cobertura de bici no es una salida que se haya perdido: es un día
            # anterior a que hubiera bici que contar, y contarlo como descarte
            # haría que una ventana de dos años dijera "54 salidas medidas de
            # 598" siendo 540 de esos días tiempo antes de empezar. Dentro de la
            # cobertura sí es un descarte de verdad: salió y no se sabe cuánto.
            #
            # Lo que NO se hace en ninguno de los dos casos es tratarlo como un
            # cero, que es el fallo que `_serie_entreno` tiene documentado.
            #
            # Y fuera de cobertura hay todavía dos casos. Por detrás del primer
            # día con bici es historia que no existe. Por delante del último es
            # la cola de la ventana: el reloj no ha sincronizado todavía, y eso
            # sí se arregla. La base sin una sola actividad cae entera en
            # `antes`, que es lo correcto: no hay un "último día con bici" del
            # que estar por delante.
            if _en_cobertura(cob, dia):
                fuera["sin_carga"] += 1
            elif cob.bici is not None and dia > cob.bici[1]:
                sin_cobertura["despues"] += 1
            else:
                sin_cobertura["antes"] += 1
            dia += timedelta(days=1)
            continue
        if carga <= 0:
            dia += timedelta(days=1)
            continue

        antes = hrv.get(dia)
        if antes is None:
            fuera["sin_hrv_antes"] += 1
            dia += timedelta(days=1)
            continue

        despues: dict[int, float | None] = {}
        for k in retardos:
            v = hrv.get(dia + timedelta(days=k))
            despues[k] = None if v is None else v - antes
        if all(v is None for v in despues.values()):
            fuera["sin_hrv_despues"] += 1
            dia += timedelta(days=1)
            continue

        aislada, porque_no = _aislamiento(cargas, dia)
        salidas.append(
            Salida(
                fecha=dia,
                carga=carga,
                antes=antes,
                aislada=aislada,
                porque_no=porque_no,
                despues=despues,
            )
        )
        dia += timedelta(days=1)

    # `sin_cobertura` va aparte y no dentro de `fuera` a propósito: `fuera` es
    # "lo que se ha perdido" y esto es "lo que nunca hubo". Quien sume `fuera`
    # para hacer un denominador -el encabezado lo hace- tiene que sumar solo lo
    # primero, y la forma de garantizarlo es que lo segundo no esté ahí dentro.
    return salidas, fuera, sin_cobertura


# ---------------------------------------------------------------------------
# Lo que se dice de un montón de deltas
# ---------------------------------------------------------------------------


def _frase_delta(media: float | None) -> str:
    """Cómo se dice un delta de HRV en castellano, con el signo puesto.

    La HRV alta es mejor, así que un delta negativo es "baja". Escribir "−5,8 de
    variación" obligaría a saber el sentido de la métrica para leer la frase, y
    el sentido de la métrica es justo lo que este panel no quiere que haya que
    saberse.
    """
    if media is None:
        return "no se puede decir"
    if abs(media) < 0.05:
        return "no se mueve"
    verbo = "baja" if media < 0 else "sube"
    return f"{verbo} {abs(media):.1f} ms".replace(".", ",")


def _resumen(deltas: list[float], *, minimo: int = N_MINIMO_TRAMO) -> dict[str, Any]:
    """La media de unos deltas, con el recorrido que la rodea y su motivo si no hay.

    El recorrido entre cuartiles va SIEMPRE que haya media, y no es adorno: una
    media de −9 ms con las salidas repartidas entre −10 y −8 y una media de −9 ms
    con las salidas repartidas entre −40 y +20 son el mismo número contando dos
    cosas que no se parecen. Sin el recorrido, la segunda se lee como la primera.
    """
    n = len(deltas)
    if n < minimo:
        return {
            "n": n,
            "media": None,
            "p25": None,
            "p75": None,
            "frase": None,
            "aviso": None,
            "na": (
                f"solo hay {cuantos(n, 'salida medida', 'salidas medidas')} aquí, "
                f"y hacen falta {minimo} para que una media diga algo"
            ),
        }
    media = fmean(deltas)
    return {
        "n": n,
        "media": round(media, 2),
        "p25": round(percentil(deltas, 25.0) or 0.0, 2),
        "p75": round(percentil(deltas, 75.0) or 0.0, 2),
        "frase": _frase_delta(media),
        # La media se publica desde tres salidas, pero entre tres y veinte va
        # marcada. Es la regla de `stats.aviso` aplicada a una media en vez de a
        # una `r`: quien pidió ver sus números desde el primer día los ve desde
        # el primer día, sabiendo lo que valen. `na` y `aviso` no coinciden
        # nunca: o no hay número, o lo hay y puede llevar advertencia.
        "aviso": (
            None
            if n >= N_MINIMO_FIABLE
            else (
                f"muestra insuficiente: {cuantos(n, 'salida', 'salidas')}, hacen "
                f"falta {N_MINIMO_FIABLE} para tomárselo en serio"
            )
        ),
        "na": None,
    }


# ---------------------------------------------------------------------------
# Análisis 1: el umbral
# ---------------------------------------------------------------------------


def _bordes(cargas: list[float], cortes: tuple[float, ...]) -> list[float]:
    """Los cortes de los tramos, sin repetidos y en orden.

    Se quitan los repetidos porque con pocas salidas dos cuartiles pueden caer
    en el mismo valor, y dos cortes iguales dan un tramo vacío entre ellos que
    saldría en la pantalla como un tramo de verdad con cero salidas dentro.
    """
    vistos: list[float] = []
    for q in cortes:
        v = percentil(cargas, q)
        if v is None:
            continue
        if not vistos or v > vistos[-1]:
            vistos.append(v)
    return vistos


def tramos(
    salidas: list[Salida],
    *,
    cortes: tuple[float, ...] = CORTES_TRAMOS,
    retardo: int = 1,
    frontera_en: float | None = None,
) -> dict[str, Any]:
    """La carga partida en tramos, y la HRV del día siguiente en cada uno.

    `frontera_en` NO parte los tramos: solo marca cuál de ellos se lleva dentro
    el escalón. Es a propósito que no los parta -las dos reglas son
    independientes, ver la cabecera-, pero callarlo dejaría la tabla diciendo
    "de 92 a 174 la HRV sube" justo al lado de una frase que dice "de 147 para
    arriba baja 7 ms", y las dos cosas serían verdad y parecerían reñidas.
    """
    usables = [s for s in salidas if s.despues.get(retardo) is not None]
    if len(usables) < N_MINIMO_CALCULABLE:
        return {
            "retardo": retardo,
            "tramos": [],
            "na": (
                f"solo hay {cuantos(len(usables), 'salida', 'salidas')} con la HRV "
                f"medida la mañana de la salida y la de después. Hacen falta "
                f"{N_MINIMO_CALCULABLE} para partir nada en tramos"
            ),
        }

    cargas = [s.carga for s in usables]
    bordes = _bordes(cargas, cortes)
    suelo, techo = min(cargas), max(cargas)

    # La frontera se compara REDONDEADA contra bordes REDONDEADOS, y esto no es
    # una manía: `frontera` publica su carga con `round(carga, 1)` y
    # `vista_umbral` mete en `frontera_en` ese número ya redondeado, no el de
    # dentro. Comparándolo contra el borde crudo se puede marcar como partido un
    # tramo cuyo borde publicado ES la frontera publicada -borde 43,75 que sale
    # impreso como 43,8, frontera 43,8-: en la pantalla se leería "el corte cae
    # aquí" sobre una banda que empieza justo en el corte. El aviso tiene que ser
    # verdad sobre la tabla TAL COMO ESTÁ ESCRITA, porque es la única tabla que
    # él ve; si no, el número de al lado desmiente a la marca.
    corte = None if frontera_en is None else round(frontera_en, 1)

    filas: list[dict[str, Any]] = []
    limites = [suelo, *bordes, techo]
    for i in range(len(limites) - 1):
        bajo, alto = limites[i], limites[i + 1]
        ultimo = i == len(limites) - 2
        dentro = [
            s
            for s in usables
            if s.carga >= bajo and (s.carga <= alto if ultimo else s.carga < alto)
        ]
        # El último tramo se rotula con su borde de ABAJO -"de 148 para
        # arriba"-, que es el dato que se quiere leer, y no con el máximo
        # observado, que es una anécdota de la salida más dura que hizo nunca y
        # además se movería sola en cuanto hiciera otra peor.
        desde, hasta = round(bajo, 1), round(alto, 1)
        filas.append(
            {
                "desde_carga": desde,
                "hasta_carga": hasta,
                "ultimo": ultimo,
                "etiqueta": (
                    f"de {bajo:.0f} de carga para arriba"
                    if ultimo and len(limites) > 2
                    else f"de {bajo:.0f} a {alto:.0f} de carga"
                ),
                # Estrictamente DENTRO: si la frontera cae justo en el borde, el
                # tramo no está partido, está alineado con ella, y decir que la
                # parte sería mentir en el sentido incómodo -avisar de una
                # contradicción que no hay-.
                "parte_la_frontera": corte is not None and desde < corte < hasta,
                **_resumen([s.despues[retardo] for s in dentro]),  # type: ignore[misc]
            }
        )

    return {"retardo": retardo, "tramos": filas, "na": None}


def _corte(
    usables: list[Salida], carga: float, *, retardo: int, metodo: str
) -> dict[str, Any]:
    """Partir las salidas por una carga y comparar las dos mitades.

    La `p` sale de correlacionar el binario "es dura" con el delta, que es lo
    mismo que hace `contraste` en `impacto.py`. No se inventa aquí ningún
    contraste nuevo: se reutiliza el que ya está probado.
    """
    encima = [s for s in usables if s.carga >= carga]
    debajo = [s for s in usables if s.carga < carga]

    dura = {s.fecha: (1.0 if s.carga >= carga else 0.0) for s in usables}
    delta = {s.fecha: s.despues[retardo] for s in usables}
    res = correlacion(emparejar(dura, delta), metodo=metodo)

    fila: dict[str, Any] = {
        "carga": round(carga, 1),
        "n_encima": len(encima),
        "n_debajo": len(debajo),
        "encima": _resumen([s.despues[retardo] for s in encima]),  # type: ignore[misc]
        "debajo": _resumen([s.despues[retardo] for s in debajo]),  # type: ignore[misc]
        **res.como_dict(),
    }
    arriba, abajo = fila["encima"]["media"], fila["debajo"]["media"]
    fila["diferencia"] = (
        None if arriba is None or abajo is None else round(arriba - abajo, 2)
    )
    # «De cada 10 salidas, 3 son de las que te cuestan una noche».
    #
    # Es una proporción, y una proporción es un número que se lee: por eso se
    # calcula aquí y no en el navegador. La maqueta aprobada lo hacía en el
    # cliente con una nota al margen que decía «al backend si esto se aprueba»,
    # y se aprobó.
    #
    # Va sobre diez y no en porcentaje a propósito. «El 30 % de tus salidas» es
    # la misma cuenta dicha en el idioma que el encargo pedía quitar de la vista
    # principal; «3 de cada 10» se entiende sin traducir nada, que era el punto.
    total = fila["n_encima"] + fila["n_debajo"]
    fila["de_cada_diez"] = (
        None if not total else round(fila["n_encima"] / total * 10)
    )
    return fila


def frontera(
    salidas: list[Salida],
    *,
    cortes: tuple[float, ...] = CORTES_FRONTERA,
    retardo: int = 1,
    metodo: str = "spearman",
) -> dict[str, Any]:
    """Cuál de los cortes separa más, con los que perdió al lado.

    "El que más separa" es en VALOR ABSOLUTO, sin suponer hacia dónde. Quedarse
    solo con los cortes que van hacia donde la fisiología dice que tienen que ir
    es la manera más limpia de no encontrarse nunca una sorpresa, y las sorpresas
    son lo único que este panel puede aportar que no supiera ya.
    """
    usables = [s for s in salidas if s.despues.get(retardo) is not None]
    vacio: dict[str, Any] = {
        "carga": None,
        "candidatos": [],
        "continua": None,
        "lectura": None,
        "escalon": None,
    }
    if len(usables) < N_MINIMO_CALCULABLE:
        return {
            **vacio,
            "na": (
                f"con {cuantos(len(usables), 'salida medida', 'salidas medidas')} no "
                f"se puede buscar ningún corte"
            ),
        }

    candidatos = [
        _corte(usables, c, retardo=retardo, metodo=metodo)
        for c in _bordes([s.carga for s in usables], cortes)
    ]

    # Y la relación continua al lado de los cortes, que es el control: si la
    # carga y el delta van juntos de forma suave, el "umbral" que salga es un
    # corte arbitrario en una cuesta, no un escalón. Las dos cosas se leen
    # juntas o no se leen.
    continua = correlacion(
        emparejar(
            {s.fecha: s.carga for s in usables},
            {s.fecha: s.despues[retardo] for s in usables},
        ),
        metodo=metodo,
    ).como_dict()

    # Corregidos todos a la vez: cada candidato y la continua son otras tantas
    # miradas a los mismos días. Y es a propósito que la tanda incluya los
    # cortes que van a perder: si solo se corrigiera el ganador, la corrección
    # no estaría enterándose de cuántas veces se ha buscado, que es justo de lo
    # que tiene que enterarse.
    corregir_tanda([candidatos, [continua]])

    elegibles = [
        c
        for c in candidatos
        if c["diferencia"] is not None
        and c["n_encima"] >= N_MINIMO_TRAMO
        and c["n_debajo"] >= N_MINIMO_TRAMO
    ]
    if not elegibles:
        return {
            **vacio,
            "candidatos": candidatos,
            "continua": continua,
            "na": (
                f"ninguno de los {len(candidatos)} cortes deja "
                f"{N_MINIMO_TRAMO} salidas a cada lado, así que no hay dos grupos "
                f"que comparar"
            ),
        }

    mejor = max(elegibles, key=lambda c: abs(c["diferencia"]))
    vecinos = _vecinos(candidatos, mejor)
    return {
        "carga": mejor["carga"],
        "corte": mejor,
        "candidatos": candidatos,
        "continua": continua,
        # La resolución con la que está medido el número, dicha en voz alta. Sin
        # esto, "147" se lee como si el 146 fuera seguro y el 148 también, y lo
        # único que dice el dato es "entre el candidato de abajo y el de arriba".
        "vecinos": vecinos,
        "miradas": len(candidatos) + 1,
        "lectura": _lectura_frontera(mejor, vecinos, len(candidatos) + 1),
        "escalon": _escalon_o_cuesta(mejor, continua),
        "na": None,
    }


def _vecinos(
    candidatos: list[dict[str, Any]], mejor: dict[str, Any]
) -> dict[str, float | None]:
    """Los candidatos de al lado del que gana, que son su margen de error."""
    cargas = sorted(c["carga"] for c in candidatos)
    i = cargas.index(mejor["carga"])
    return {
        "debajo": cargas[i - 1] if i > 0 else None,
        "encima": cargas[i + 1] if i + 1 < len(cargas) else None,
    }


def _escalon_o_cuesta(
    mejor: dict[str, Any], continua: dict[str, Any]
) -> str | None:
    """Si lo que hay es un escalón o una cuesta, que no es un matiz.

    Un corte que aguanta la corrección mientras la relación suave NO la aguanta
    dice algo muy concreto: que por debajo del corte el cuerpo no distingue una
    salida de otra, y que lo que cuenta es pasarlo, no cuánto se pase. Al revés
    -la suave aguanta y el corte no- el "umbral" es un punto cualquiera de una
    cuesta, y publicarlo como umbral convertiría en hallazgo lo que solo es una
    elección de dónde partir.

    Las dos salen de la MISMA tanda de corrección, así que la comparación es
    limpia: no es un contraste enfrentado a otro con distinto castigo por el
    número de veces que se ha mirado.

    Sin ningún número dentro, y no por estilo: lo que esta frase compara son dos
    banderas de "aguanta o no aguanta", no dos magnitudes. Meter aquí la `r` de
    la relación suave invitaría a leerla como la fuerza del efecto cuando lo que
    se está diciendo es de qué FORMA es. La `r` va al lado, con su barra, donde
    se puede comparar con las demás `r` de la aplicación.
    """
    corte, suave = mejor.get("significativa"), continua.get("significativa")
    if corte is None or suave is None:
        return None
    if corte and not suave:
        return (
            "Esto es un escalón, no una cuesta. El corte aguanta la corrección y "
            "la relación suave entre carga y HRV no la aguanta: por debajo del "
            "corte el cuerpo no distingue una salida de otra, y lo que cuenta es "
            "pasarlo, no cuánto lo pases."
        )
    if suave and not corte:
        return (
            "Esto es una cuesta, no un escalón. La relación suave entre carga y "
            "HRV aguanta la corrección y el corte no: cuanta más carga, peor, sin "
            "un punto donde cambie la cosa. El número de arriba es entonces un "
            "sitio por donde partir, no una frontera."
        )
    if corte and suave:
        return (
            "Aguantan las dos: hay cuesta y encima hay escalón. Cuanta más carga "
            "peor, y además pasar el corte se nota aparte. Con estas salidas no "
            "se puede separar lo uno de lo otro."
        )
    return (
        "No aguanta ninguna de las dos: ni el corte ni la relación suave entre "
        "carga y HRV sobreviven a la corrección por las veces que se han mirado "
        "estos mismos días. Lo de arriba es lo que MÁS separa de todo lo que se "
        "ha probado, que no es lo mismo que separar de verdad."
    )


def _lectura_frontera(
    c: dict[str, Any], vecinos: dict[str, float | None], miradas: int
) -> str:
    """La frase del hallazgo. El número va dentro, no debajo."""
    arriba, abajo = c["encima"], c["debajo"]
    frase = (
        f"Por debajo de {c['carga']:.0f} de carga la HRV de la mañana siguiente "
        f"{abajo['frase']} "
        f"({cuantos(abajo['n'], 'salida', 'salidas')}). "
        f"De {c['carga']:.0f} para arriba {arriba['frase']} "
        f"({cuantos(arriba['n'], 'salida', 'salidas')})."
    )
    if c.get("significativa") is False:
        frase += (
            f" Corrigiendo por las {miradas} veces que se han mirado estos mismos "
            f"días, la diferencia no aguanta: puede ser casualidad."
        )
    # El número no se afina más de lo que da de sí la regla. Decir "147" a secas
    # invita a creer que el 140 está a salvo, y el dato no dice eso: dice que el
    # escalón cae entre el candidato de abajo y el de arriba.
    d, e = vecinos.get("debajo"), vecinos.get("encima")
    if d is not None and e is not None:
        frase += (
            f" El corte se ha buscado entre {miradas - 1} candidatos sacados de tus "
            f"propias salidas, y los de al lado de éste son {d:.0f} y {e:.0f}: el "
            f"escalón está por ahí, no exactamente en {c['carga']:.0f}."
        )
    return frase


# ---------------------------------------------------------------------------
# Análisis 2: cuánto dura
# ---------------------------------------------------------------------------


def curva(
    salidas: list[Salida],
    *,
    titulo: str,
    desde_carga: float | None = None,
    retardos: tuple[int, ...] = DIAS_DESPUES,
) -> dict[str, Any]:
    """La HRV de los días siguientes a una salida aislada, día a día.

    Solo aisladas: ver la cabecera del módulo. Y solo por encima de
    `desde_carga` cuando se pasa, porque la duración de lo que no se nota no es
    una pregunta.
    """
    dentro = [s for s in salidas if s.aislada]
    if desde_carga is not None:
        dentro = [s for s in dentro if s.carga >= desde_carga]

    por_dia = [
        {
            "dia": k,
            **_resumen([s.despues[k] for s in dentro if s.despues.get(k) is not None]),  # type: ignore[misc]
            # Explícito y no deducido en el navegador: estas barras no llevan
            # contraste, y `None` en este panel significa "no se le ha aplicado
            # ninguna corrección", que es distinto de "no la ha pasado".
            "significativa": None,
        }
        for k in retardos
    ]

    vuelve, na_vuelve = _cuando_vuelve(por_dia)
    cuales = "" if desde_carga is None else f" de {desde_carga:.0f} de carga para arriba"

    # AQUÍ SE MANDABA `escala`, el denominador del dibujo, y ya no.
    #
    # El argumento para calcularlo en el servidor era bueno y ha dejado de
    # aplicar por una razón concreta: valía cuando la pantalla pintaba LAS DOS
    # curvas -la de las salidas duras y la de todas- una debajo de otra, y dos
    # ejes distintos hacían que las barras de una parecieran el doble que las de
    # la otra sin que nada lo dijera. Ahora se dibuja una sola; la segunda vive
    # en «ver detalle» y es una tabla, que no tiene eje que confundir.
    #
    # Y había un segundo motivo para el cálculo de aquí: se tomaba el borde del
    # recorrido y no la media, porque las barras llevaban bigotes entre
    # cuartiles y escalando con las medias los bigotes se salían de la caja. Los
    # bigotes se han ido con la regla 2 -la mitad central no sale en la vista
    # principal-, así que ya no hay nada que meter en la caja aparte de las
    # cuatro medias, y de eso se encarga `escalaY` en el navegador igual que en
    # los otros seis gráficos del panel.
    #
    # Se borra en vez de dejarlo puesto porque una clave que el servidor calcula
    # y nadie lee es una promesa de que algo se decide aquí cuando se decide
    # allí, y ese es exactamente el patrón decorativo que hay que cazar.
    #
    # El aviso de la curva ENTERA va aparte del de cada barra, y no sobra. Las
    # cuatro barras salen de las MISMAS salidas, así que no son cuatro medidas
    # de cuatro salidas: son una foto de cuatro salidas mirada cuatro veces. Un
    # aviso por barra deja leer "bueno, son cuatro avisos distintos"; éste dice
    # cuántas personas-salida hay detrás del dibujo completo.
    na = (
        None
        if any(d["media"] is not None for d in por_dia)
        else (
            f"no hay bastantes salidas aisladas{cuales} con la HRV medida "
            f"después. Una salida cuenta como aislada cuando no hay otra en los "
            f"{DIAS_AISLAMIENTO} días de antes ni en los {DIAS_AISLAMIENTO} de "
            f"después, y de ésas hoy hay {len(dentro)}"
        )
    )
    # `na` y `aviso` son excluyentes en todo el panel, y aquí se colaban los dos
    # a la vez: sin ninguna salida salía el motivo escrito Y, debajo, "toda esta
    # curva sale de 0 salidas". Avisar de que una muestra es corta cuando no hay
    # muestra es hablar de un dibujo que no está: deja leer que hay una curva
    # floja donde lo que hay es nada, que son dos situaciones distintas y se
    # arreglan de maneras distintas.
    aviso = (
        None
        if na is not None or len(dentro) >= N_MINIMO_FIABLE
        else (
            f"toda esta curva sale de {cuantos(len(dentro), 'salida', 'salidas')}"
            f"{cuales}. Es la forma que tienen esas, no una media de muchas"
        )
    )
    return {
        "titulo": titulo,
        "desde_carga": None if desde_carga is None else round(desde_carga, 1),
        "n_salidas": len(dentro),
        "por_dia": por_dia,
        "vuelve_el_dia": vuelve,
        "aviso": aviso,
        "lectura": _lectura_curva(por_dia, vuelve, na_vuelve),
        "na": na,
    }


def _cuando_vuelve(por_dia: list[dict[str, Any]]) -> tuple[int | None, str | None]:
    """El primer día cuya media ya no va en contra del primero.

    Se compara con el SIGNO del primer día medido y no con "menor que cero" a
    secas: si la primera noche la HRV sube -que puede pasar-, "ha vuelto" es que
    deje de subir, no que baje.
    """
    medidos = [d for d in por_dia if d["media"] is not None]
    if not medidos:
        return None, "no hay ni un día con media"
    primero = medidos[0]
    if abs(primero["media"]) < 0.05:
        return primero["dia"], None
    signo = -1 if primero["media"] < 0 else 1
    for d in medidos[1:]:
        if d["media"] * signo <= 0:
            return d["dia"], None
    return None, (
        f"hasta el día +{medidos[-1]['dia']}, que es hasta donde llega esta "
        f"ventana, todavía se nota"
    )


def _lectura_curva(
    por_dia: list[dict[str, Any]], vuelve: int | None, na_vuelve: str | None
) -> str | None:
    medidos = [d for d in por_dia if d["media"] is not None]
    if not medidos:
        return None
    primero = medidos[0]
    cabeza = (
        f"La noche de después de una salida así, la HRV {primero['frase']} "
        f"({cuantos(primero['n'], 'salida', 'salidas')})."
    )
    if vuelve is None:
        return f"{cabeza} Y {na_vuelve}."
    if vuelve == primero["dia"]:
        return f"{cabeza} O sea que no llega a notarse ni un día."
    dias = vuelve - 1
    return (
        f"{cabeza} El día +{vuelve} ya está de vuelta, así que lo que cuesta es "
        f"{cuantos(dias, 'día', 'días')}, no más."
    )


# ---------------------------------------------------------------------------
# La gráfica: la HRV en el tiempo con las salidas marcadas
# ---------------------------------------------------------------------------


def _media_movil(
    valores: dict[date, float | None], dias: list[date], *, ancho: int = SUAVIZADO
) -> dict[date, float | None]:
    """Media móvil centrada, y `None` donde no hay con qué.

    El mínimo de días reales es lo que impide que los dos extremos de la línea
    salgan calculados sobre tres valores y dibujados con el mismo trazo que el
    centro. Una cola suavizada sobre nada es una cola inventada, y en una
    gráfica no se distingue de una medida.
    """
    lado = ancho // 2
    salida: dict[date, float | None] = {}
    for d in dias:
        trozo = [
            valores.get(d + timedelta(days=k))
            for k in range(-lado, lado + 1)
        ]
        reales = [v for v in trozo if v is not None]
        salida[d] = round(fmean(reales), 2) if len(reales) >= MINIMO_SUAVIZADO else None
    return salida


def grafica(
    session: Session,
    *,
    desde: date,
    hasta: date,
    cob: S.Cobertura | None = None,
    corte: float | None = None,
) -> dict[str, Any]:
    """Lo que hace falta para dibujar la HRV con las salidas encima.

    Todo lo que se LEE viene calculado de aquí -la media móvil, la banda, la
    altura de cada palo-. El navegador multiplica por píxeles y nada más. La
    altura va ya en 0-1 y no como "carga partido por el máximo" a propósito:
    elegir el denominador es una decisión, y las decisiones se toman aquí.
    """
    cob = cob or S.cobertura(session)
    hrv = S.serie(session, RESPUESTA, desde, hasta, cob=cob)
    cargas = S.serie(session, EXPOSICION, desde, hasta, cob=cob)

    dias: list[date] = []
    d = desde
    while d <= hasta:
        dias.append(d)
        d += timedelta(days=1)

    suave = _media_movil(hrv, dias)
    medidos = [v for v in hrv.values() if v is not None]

    salidas = [
        {
            "fecha": f.isoformat(),
            "carga": round(c, 1),
            "dura": None if corte is None else c >= corte,
        }
        for f, c in sorted(cargas.items())
        if c is not None and c > 0
    ]
    techo = max((s["carga"] for s in salidas), default=0.0)
    for s in salidas:
        # Con una sola salida en la ventana el techo es ella misma y saldría a
        # altura 1. Es lo correcto: no hay contra qué compararla, y dibujarla
        # bajita fingiría una escala que no existe.
        s["altura"] = round(s["carga"] / techo, 4) if techo > 0 else 0.0

    return {
        "puntos": [
            {
                "fecha": f.isoformat(),
                "valor": None if hrv.get(f) is None else round(hrv[f], 1),  # type: ignore[index]
                "suave": suave.get(f),
            }
            for f in dias
        ],
        "salidas": salidas,
        "suavizado": SUAVIZADO,
        "carga_maxima": round(techo, 1) if techo > 0 else None,
        "corte": None if corte is None else round(corte, 1),
        # La raya del umbral, ya en la misma escala 0-1 que los palos. Que el
        # navegador dividiera `corte` entre `carga_maxima` daría el mismo píxel
        # hoy y se despegaría el día que aquí se cambie el denominador, y se
        # despegaría en silencio: la raya seguiría saliendo, un poco mal puesta.
        "altura_corte": (
            round(corte / techo, 4) if corte is not None and techo > 0 else None
        ),
        "banda": (
            {
                "desde": round(percentil(medidos, BANDA[0]) or 0.0, 1),
                "hasta": round(percentil(medidos, BANDA[1]) or 0.0, 1),
            }
            if len(medidos) >= N_MINIMO_CALCULABLE
            else None
        ),
        # El eje sale de lo MEDIDO y no de un rango declarado, al revés que en
        # `serieTemporal`. Allí la escala es 0-100 porque las series vienen
        # normalizadas; aquí son milisegundos de verdad y un eje de 0 a 200
        # dejaría toda la variación aplastada en una franja de dos píxeles.
        "rango": (
            [round(min(medidos), 1), round(max(medidos), 1)] if medidos else None
        ),
        # TU MEDIA Y DÓNDE ANDAS AHORA. Los dos números de la frase de la línea.
        #
        # «Sin banda de percentiles ni mitad central, solo la línea y mi media»:
        # la media pasa de ser un adorno del dibujo a ser la única referencia
        # que queda, así que se calcula donde hay tests con resultado conocido.
        # La maqueta la promediaba en el navegador con una nota que decía «al
        # backend si esto se aprueba», y se aprobó.
        #
        # `media` sale de lo MEDIDO en crudo y `ahora` del último punto SUAVE, y
        # la diferencia importa: la media de la ventana entera contra una sola
        # noche suelta compara un mes con un día, y un día de HRV se mueve lo
        # que le da la gana. El último suavizado es la última semana, que es lo
        # que la frase quiere decir con «ahora andas por».
        "media": round(fmean(medidos), 1) if medidos else None,
        "ahora": next(
            (suave[f] for f in reversed(dias) if suave.get(f) is not None), None
        ),
        "n": len(medidos),
        # Los DOS motivos por los que no hay línea, y el segundo no es teórico:
        # el dibujo escala el eje a lo medido, así que una ventana con todas las
        # noches iguales no tiene alto contra el que dibujar y el navegador
        # devolvería una cadena vacía. Un hueco sin motivo en mitad de la
        # pantalla es exactamente el fallo silencioso que este panel persigue, y
        # que el navegador se callara aquí no lo notaría ningún test de los que
        # comprueban que no se pinta `undefined`.
        "na": _na_grafica(medidos),
    }


def _na_grafica(medidos: list[float]) -> str | None:
    if len(medidos) < N_MINIMO_CALCULABLE:
        return (
            f"solo hay {cuantos(len(medidos), 'noche medida', 'noches medidas')} "
            f"de HRV en esta ventana, y con eso no se dibuja una línea"
        )
    if min(medidos) == max(medidos):
        return (
            f"las {cuantos(len(medidos), 'noche medida', 'noches medidas')} de esta "
            f"ventana marcan todas el mismo valor, así que no hay nada que dibujar: "
            f"saldría una raya recta"
        )
    return None


# ---------------------------------------------------------------------------
# La vista
# ---------------------------------------------------------------------------


def _resumen_descartes(
    fuera: dict[str, int], medidas: int, sin_cobertura: dict[str, int] | None = None
) -> str:
    sin_cobertura = sin_cobertura or {}
    partes = [f"{cuantos(medidas, 'salida medida', 'salidas medidas')}"]
    if fuera.get("sin_carga"):
        partes.append(f"{fuera['sin_carga']} días sin carga conocida")
    if fuera.get("sin_hrv_antes"):
        partes.append(
            f"{fuera['sin_hrv_antes']} sin la HRV de la mañana de la salida"
        )
    if fuera.get("sin_hrv_despues"):
        partes.append(f"{fuera['sin_hrv_despues']} sin ninguna HRV después")
    # Los dos de fuera de cobertura se dicen con otras palabras y al final: no
    # son descartes, son ventana pedida de más. Sin ninguna de las dos frases,
    # pedir dos años y ver los mismos números que con seis meses parece que el
    # panel ignora el selector.
    if sin_cobertura.get("antes"):
        partes.append(
            f"{sin_cobertura['antes']} días de la ventana son de antes de la bici"
        )
    if sin_cobertura.get("despues"):
        # Esta frase es la que importa de las dos, y es la que durante un tiempo
        # se escribió al revés. Los días del final sin nada apuntado no son
        # historia que falte: son el reloj sin sincronizar, o sea lo único de
        # todo este resumen sobre lo que se puede hacer algo hoy. Decirlos como
        # "de antes de la bici" no solo mentía, escondía justo eso.
        n = sin_cobertura["despues"]
        partes.append(
            "el último día de la ventana no tiene bici apuntada todavía"
            if n == 1
            else f"los {n} últimos días de la ventana no tienen bici apuntada todavía"
        )
    return " · ".join(partes)


def vista_umbral(
    session: Session,
    cfg: Any = None,
    *,
    dias: int = 180,
    hoy: date | None = None,
    metodo: str = "spearman",
) -> dict[str, Any]:
    """El umbral, la duración y la gráfica: las dos preguntas y su dibujo.

    `cfg` no se usa todavía y está en la firma a propósito, para que la llamada
    del endpoint sea la misma que la de las demás vistas. El día que las bandas
    salgan del YAML, no hay que tocar `api.py`.
    """
    hoy = hoy or date.today()
    desde, hasta = hoy - timedelta(days=dias - 1), hoy
    cob = S.cobertura(session)

    salidas, fuera, sin_cobertura = recoger(session, desde=desde, hasta=hasta, cob=cob)
    fr = frontera(salidas, retardo=1, metodo=metodo)
    corte = fr.get("carga")

    aisladas = [s for s in salidas if s.aislada]
    curvas = [
        curva(
            salidas,
            titulo=(
                f"Las salidas de {corte:.0f} de carga para arriba"
                if corte is not None
                else "Las salidas duras"
            ),
            desde_carga=corte,
        ),
        curva(salidas, titulo="Todas las salidas, duras y suaves"),
    ]

    return {
        # Las dos etiquetas que llevan todas las vistas. `metodo` sobre todo: el
        # endpoint lo acepta y lo valida, así que si no viajara de vuelta habría
        # un mando en el panel que se puede mover, que cambia el cálculo, y que
        # no se puede comprobar desde fuera que lo haya cambiado.
        "vista": "umbral",
        "metodo": metodo,
        "ventana": {"desde": desde.isoformat(), "hasta": hasta.isoformat(), "dias": dias},
        "cobertura": cob.como_dict(),
        # El recuento en la moneda de esta vista: la salida con su antes y su
        # después. Lo lee el encabezado, y lo lee la pantalla.
        "salidas": {
            "medidas": len(salidas),
            "aisladas": len(aisladas),
            "fuera": fuera,
            "sin_cobertura": sin_cobertura,
            "resumen": _resumen_descartes(fuera, len(salidas), sin_cobertura),
        },
        "umbral": {
            **tramos(salidas, retardo=1, frontera_en=corte),
            "frontera": fr,
        },
        "recuperacion": {
            "curvas": curvas,
            "aislamiento": DIAS_AISLAMIENTO,
            "nota": (
                f"Solo cuentan las salidas sin ninguna otra en los "
                f"{DIAS_AISLAMIENTO} días de antes ni en los {DIAS_AISLAMIENTO} de "
                f"después. Con otra salida en medio, el día +2 ya no mide lo que "
                f"duró la primera."
            ),
        },
        "grafica": grafica(session, desde=desde, hasta=hasta, cob=cob, corte=corte),
        "convenio": (
            "Todo se mide contra la noche ANTERIOR a cada salida, no contra tu "
            "media: así la comparación no arrastra la forma que tuvieras ese mes."
        ),
        "sin_p": (
            "Las barras de la curva no llevan p: cada una es una media contra "
            "cero, y aquí no hay ningún contraste de una sola muestra con el que "
            "calcularla. Llevan en su lugar cuántas salidas hay detrás y el "
            "recorrido de la mitad central, que es lo que dice cuánto varían de "
            "una salida a otra."
        ),
    }
