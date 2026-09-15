"""Vista 6 con un escalón sembrado a mano, de resultado conocido de antemano.

La vista del umbral existe para contestar con un NÚMERO DE CARGA, así que la
única forma de probarla que vale algo es sembrar un número y exigir que salga
ése. Todo lo de aquí abajo está calculado ANTES de ejecutar la implementación:

  - Veinte salidas, una cada cinco días. Doce suaves de 20 a 75 de carga (de
    cinco en cinco) y ocho duras de 180 a 250 (de diez en diez). Entre 75 y 180
    no hay NADA, y ese hueco es todo el diseño: el escalón de verdad está ahí
    dentro, así que cualquier corte que la implementación encuentre dentro del
    hueco separa exactamente los dos grupos sembrados.
  - La HRV vale 50 todas las mañanas, y 40 la mañana siguiente a una salida
    dura. O sea: −10 ms exactos de factura, que dura UN día.
  - El decil 60 de esas veinte cargas cae en 117,0 -dentro del hueco-, y es el
    candidato que más separa. Por eso el test puede exigir 117,0 y no "algo
    entre 75 y 180": si la regla de percentiles cambiara, este test tiene que
    ponerse rojo y obligar a mirar, no adaptarse solo.

Las salidas van cada CINCO días y no cada tres por un motivo que costó ver: la
curva llega al día +4, y con tres días de separación el día +4 de una salida cae
encima de la siguiente. La mitad de las barras del día +4 medirían la salida de
al lado en vez de la recuperación de la propia. Con cinco días, los cuatro días
de después de cada salida son siempre días de descanso.

Lo que NO se prueba aquí es la estadística (`test_analysis_stats`) ni la
contabilidad de las series (`test_analysis_series`). Esto prueba el montaje: que
las piezas ya probadas, atornilladas, contesten lo que se sembró.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.analysis.stats import percentil
from app.analysis.umbral import (
    DIAS_AISLAMIENTO,
    MINIMO_SUAVIZADO,
    N_MINIMO_FIABLE,
    N_MINIMO_TRAMO,
    SUAVIZADO,
    _escalon_o_cuesta,
    _media_movil,
    _na_grafica,
    _resumen,
    _resumen_descartes,
    curva,
    frontera,
    grafica,
    recoger,
    tramos,
    vista_umbral,
)
from app.models import Activity, Base, DailyMetrics

HOY = date(2026, 9, 11)
N = 100

# Cada cuántos días hay salida, y las cargas sembradas. El hueco entre 75 y 180
# es el escalón: lo que el análisis tiene que encontrar es que está ahí dentro.
CADA = 5
SUAVES = [20.0 + 5 * k for k in range(12)]  # 20, 25, ... 75
DURAS = [180.0 + 10 * k for k in range(8)]  # 180, 190, ... 250

# Qué salidas son duras, por número de salida y no por día. Van repartidas -no
# las ocho al final- para que el hallazgo no se pueda explicar por el paso del
# tiempo: si las duras fueran todas de septiembre, una HRV que baja en
# septiembre por cualquier otro motivo daría exactamente el mismo resultado.
#
# Y la primera y la última salida quedan SUAVES a propósito. Ésas dos son las
# únicas que no pueden salir aisladas -los días de al lado caen fuera de la
# cobertura, y de un día sin cobertura no se sabe si hubo salida o no-, así que
# dejándolas suaves las ocho duras entran enteras en la curva de recuperación.
DURAS_EN = (2, 3, 7, 8, 12, 13, 17, 18)

HRV_BASE = 50.0
HRV_TRAS_DURA = 40.0
FACTURA = HRV_TRAS_DURA - HRV_BASE  # -10.0

# El corte que tiene que salir: el decil 60 de las veinte cargas sembradas.
# `percentil` interpola linealmente, así que con veinte valores la posición es
# 19 * 0,60 = 11,4, o sea 75 + 0,4 * (180 - 75) = 117,0.
CORTE = 117.0


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def dia(i: int) -> date:
    """El día i-ésimo de la ventana, en orden natural: `dia(N - 1)` es hoy."""
    return HOY - timedelta(days=N - 1 - i)


def cargas_sembradas() -> dict[int, float]:
    """Qué carga lleva cada salida, indexada por número de salida (no por día)."""
    suaves, duras = iter(SUAVES), iter(DURAS)
    return {
        j: next(duras) if j in DURAS_EN else next(suaves)
        for j in range(len(SUAVES) + len(DURAS))
    }


def sembrar(db, *, cargas: dict[int, float] | None = None) -> dict[int, float]:
    """El escalón entero: las salidas, y la HRV que reacciona a ellas.

    Devuelve el reparto de cargas para que el test pueda contar con él sin
    volver a calcularlo, que sería tener la misma cuenta en dos sitios.
    """
    cargas = cargas_sembradas() if cargas is None else cargas
    duras_el_dia = set()
    for j, carga in cargas.items():
        i = j * CADA
        db.add(
            Activity(
                garmin_activity_id=1000 + i,
                date=dia(i),
                is_cycling=True,
                intensity_level="intensa" if carga >= 100 else "suave",
                duration_s=3600.0,
                training_load=carga,
            )
        )
        if carga >= 100:
            duras_el_dia.add(i)

    for i in range(N):
        db.add(
            DailyMetrics(
                date=dia(i),
                fetch_status="ok",
                # La mañana DESPUÉS de una dura, y solo ésa. El resto de días
                # valen lo mismo, así que cualquier diferencia que encuentre el
                # análisis viene de las salidas y no de una tendencia de fondo.
                hrv=HRV_TRAS_DURA if (i - 1) in duras_el_dia else HRV_BASE,
            )
        )
    db.commit()
    return cargas


def salidas_de(db):
    return recoger(db, desde=dia(0), hasta=dia(N - 1))


def tramo_marcado(u):
    marcados = [t for t in u["tramos"] if t["parte_la_frontera"]]
    assert len(marcados) == 1, f"tramos partidos por la frontera: {len(marcados)}"
    return marcados[0]


def barra(c, dias):
    for d in c["por_dia"]:
        if d["dia"] == dias:
            return d
    raise AssertionError(f"no está el día +{dias}")


# ---------------------------------------------------------------------------
# La frontera: el número que la vista existe para dar
# ---------------------------------------------------------------------------


def test_el_corte_cae_dentro_del_hueco_sembrado(db):
    """La pregunta entera, con la respuesta escrita antes de ejecutar nada.

    Doce salidas por debajo con factura cero, ocho por encima con −10 ms
    exactos. Si el montaje estuviera mal -el `antes` cogido del día equivocado,
    el `despues` desplazado un día, las dos mitades del corte solapadas- estos
    cuatro números saldrían distintos y ninguno de ellos parecería un error.
    """
    sembrar(db)
    salidas, _, _ = salidas_de(db)
    f = frontera(salidas)

    assert f["na"] is None
    assert f["carga"] == CORTE

    c = f["corte"]
    assert c["n_debajo"] == len(SUAVES)
    assert c["n_encima"] == len(DURAS)
    assert c["debajo"]["media"] == 0.0
    assert c["encima"]["media"] == FACTURA
    assert c["diferencia"] == FACTURA


def test_el_corte_viene_con_cuantas_veces_se_ha_mirado_y_con_sus_vecinos(db):
    """El número solo no es honesto: se ha elegido el mejor de una tanda.

    Nueve candidatos -los nueve deciles- más la relación continua son diez
    miradas a los mismos cien días, y las diez van a la misma corrección. Sin
    ese recuento al lado, la `p` del ganador se lee como si el corte se hubiera
    elegido antes de mirar, que es la forma más limpia de fabricar un hallazgo.

    Y los vecinos son la resolución de la regla: el escalón está ENTRE 67,5 y
    193, no exactamente en 117. Que la frase lo diga es la diferencia entre un
    dato y una cifra con tres decimales fingidos.
    """
    sembrar(db)
    salidas, _, _ = salidas_de(db)
    f = frontera(salidas)

    assert len(f["candidatos"]) == 9
    assert f["miradas"] == 10
    assert f["vecinos"] == {"debajo": 67.5, "encima": 193.0}

    # Todos los candidatos salen de la tanda corregidos, incluidos los que
    # pierden: si solo se corrigiera el ganador, la corrección no se estaría
    # enterando de cuántas veces se ha buscado.
    assert all(c["p_corregida"] is not None for c in f["candidatos"])
    assert f["continua"]["p_corregida"] is not None

    assert "9 candidatos" in f["lectura"]
    assert "193" in f["lectura"]
    assert "117" in f["lectura"]


def test_con_el_escalon_sembrado_el_corte_aguanta_la_correccion(db):
    """Una separación perfecta de veinte salidas tiene que sobrevivir a diez miradas.

    No es una obviedad que merezca la pena escribir por el resultado, sino por
    el contrario: si este `assert` se pusiera rojo, querría decir que la tanda de
    corrección está castigando de más y que ningún hallazgo real de esta
    aplicación llegaría nunca a la pantalla.
    """
    sembrar(db)
    salidas, _, _ = salidas_de(db)
    f = frontera(salidas)

    assert f["corte"]["significativa"] is True


def test_sin_salidas_bastantes_no_hay_corte_sino_un_motivo_escrito(db):
    """Dos salidas no son un escalón, y la vista lo dice en vez de devolver cero."""
    sembrar(db, cargas={0: 30.0, 1: 200.0})
    salidas, _, _ = salidas_de(db)
    f = frontera(salidas)

    assert f["carga"] is None
    assert f["na"]
    assert f["lectura"] is None
    assert f["escalon"] is None


# ---------------------------------------------------------------------------
# Los tramos, y la banda que el corte parte por la mitad
# ---------------------------------------------------------------------------


def test_los_tramos_marcan_la_banda_que_el_corte_parte_y_esa_banda_miente_a_medias(db):
    """El caso que se vio con datos de verdad y casi deja la pantalla contradiciéndose.

    Los cuartiles de carga y la búsqueda del corte son dos reglas
    INDEPENDIENTES, así que el corte cae casi siempre en mitad de un cuartil. Esa
    banda promedia salidas de los dos lados, y su media no se parece ni a la de
    abajo ni a la de arriba: aquí sale −6,0 entre un 0,0 y un −10,0. Leída sin
    contexto, justo debajo de un hallazgo que dice −10, parece que la tabla
    desmiente al titular.

    No lo desmiente: está contando otra cosa. Por eso la banda se marca, y por
    eso el marcado tiene un test: es lo único que evita que la pantalla se
    contradiga sola.
    """
    sembrar(db)
    salidas, _, _ = salidas_de(db)
    u = tramos(salidas, frontera_en=CORTE)

    assert u["na"] is None
    assert len(u["tramos"]) == 4

    medias = [t["media"] for t in u["tramos"]]
    assert medias == [0.0, 0.0, -6.0, FACTURA]

    t = tramo_marcado(u)
    assert t["media"] == -6.0
    assert t["desde_carga"] < CORTE < t["hasta_carga"]
    # Y la mitad central de esa banda va de −10 a 0, que es la forma que tiene
    # una banda con salidas de los dos lados dentro. La media sola no lo dice.
    assert t["p25"] == FACTURA
    assert t["p75"] == 0.0


def test_un_corte_que_cae_justo_en_el_borde_no_parte_ninguna_banda(db):
    """Alineado con un borde no es partido, y decir que lo parte sería mentir.

    La comparación es estrictamente DENTRO. Con `<=` en un extremo, la pantalla
    avisaría de una contradicción que no existe, y un aviso que salta cuando no
    pasa nada es la forma de enseñar a ignorarlo.

    El caso del medio no es de laboratorio: `CORTES_FRONTERA` son deciles y
    `CORTES_TRAMOS` cuartiles, y el decil 50 y el cuartil 50 son el MISMO
    número. O sea que cada vez que gane el corte de en medio, la frontera cae
    exactamente sobre un borde de la tabla de abajo.
    """
    sembrar(db)
    salidas, _, _ = salidas_de(db)
    u = tramos(salidas, frontera_en=None)

    assert all(not t["parte_la_frontera"] for t in u["tramos"])

    # La mediana de lo sembrado, sin redondear: el borde de verdad del tramo del
    # medio. Es el único cruce exacto que el reparto decil/cuartil permite.
    mediana = percentil(list(cargas_sembradas().values()), 50.0)
    assert mediana == u["tramos"][2]["desde_carga"]
    u2 = tramos(salidas, frontera_en=mediana)
    assert all(not t["parte_la_frontera"] for t in u2["tramos"])


def test_la_marca_del_corte_es_verdad_sobre_la_tabla_tal_como_sale_escrita(db):
    """Un borde que se imprime redondeado se compara redondeado.

    El cuartil 25 de lo sembrado es 43,75, y sale publicado como 43,8. Si la
    frontera fuese 43,8 y se comparase contra el borde crudo, la pantalla
    marcaría "el corte cae aquí" en una banda cuyo `desde_carga` impreso ES
    43,8. Los dos números estarían bien por separado y se desmentirían el uno al
    otro en la misma fila, que es la peor forma de tener razón.

    Esto puede parecer una esquina, pero es exactamente el fallo silencioso de
    siempre: `vista_umbral` NO le pasa a `tramos` la frontera de dentro, le pasa
    la que `frontera` publica, y esa viene por `round(carga, 1)`.
    """
    sembrar(db)
    salidas, _, _ = salidas_de(db)

    crudo = percentil(list(cargas_sembradas().values()), 25.0)
    assert crudo == 43.75  # el de dentro
    publicado = tramos(salidas)["tramos"][1]["desde_carga"]
    assert publicado == 43.8  # el que él lee

    u = tramos(salidas, frontera_en=publicado)
    partidos = [t["etiqueta"] for t in u["tramos"] if t["parte_la_frontera"]]
    assert partidos == [], f"marcada como partida una banda que empieza en el corte: {partidos}"


def test_una_media_de_una_sola_salida_no_es_una_media():
    """Debajo de `N_MINIMO_TRAMO` sale el motivo escrito, nunca un número.

    Lo encontró la batería de mutación: bajar `N_MINIMO_TRAMO` de 3 a 1 dejaba
    la suite entera en verde. O sea que NADA sujetaba la regla, y un tramo con
    una salida dentro habría publicado su `media` -que es esa salida, otra vez,
    con otro nombre- con toda naturalidad, en la misma columna y con la misma
    pinta que una media de treinta. La tabla de tramos es justo donde más duele:
    los bordes son cuartiles de carga, no de cuántas salidas caen en cada uno,
    así que un tramo escuchimizado no es raro, es lo normal en los extremos.

    Se recorre el tramo entero por debajo del mínimo -incluido el cero, que es
    el caso de la banda vacía- y se comprueba la otra mitad de la regla: en
    cuanto hay media, el `na` se va y aparece el aviso de muestra corta. Los dos
    a la vez no salen nunca.

    El mínimo se AFIRMA antes de usarlo, y eso tiene su propia historia: la
    primera versión de este test leía la constante y se paraba ahí, así que la
    mutación siguió viva. Un test escrito solo contra la constante es cómplice de
    la constante: bajándola a 1, el bucle recorre un caso, `_resumen([1.0])`
    publica su media tan contento, y el test sigue verde certificando la regla
    que acaban de quitar. Es la misma trampa que el `.env.example` que era la
    única lista de ajustes: la pieza que tenía que avisar del hueco es la que
    dice que no lo hay.
    """
    assert N_MINIMO_TRAMO >= 3, (
        "con dos valores la media es el punto medio de dos salidas, y con uno es "
        "la salida otra vez con otro nombre: hacen falta tres para que la palabra "
        "'media' no prometa más de lo que hay"
    )

    for n in range(N_MINIMO_TRAMO):
        r = _resumen([1.0] * n)
        assert r["n"] == n
        assert r["media"] is None, f"con {n} salida(s) se ha publicado una media"
        assert r["p25"] is None and r["p75"] is None and r["frase"] is None
        assert r["na"] and str(N_MINIMO_TRAMO) in r["na"], r["na"]
        assert r["aviso"] is None  # `na` y `aviso` no coinciden nunca

    r = _resumen([1.0] * N_MINIMO_TRAMO)
    assert r["media"] == 1.0 and r["na"] is None
    assert r["aviso"] and str(N_MINIMO_FIABLE) in r["aviso"], r["aviso"]


# ---------------------------------------------------------------------------
# La recuperación: cuántos días dura
# ---------------------------------------------------------------------------


def test_la_factura_de_una_salida_dura_dura_un_dia(db):
    """La segunda pregunta del enunciado, sembrada para que la respuesta sea "uno".

    Se siembra un bajón que dura exactamente una noche. Lo que tiene que salir
    NO es una correlación: es el número de días. Si la curva dijera "hasta el
    día +4 todavía se nota", el fallo estaría en `_cuando_vuelve` y no en la
    estadística, y con datos ruidosos no se distinguiría una cosa de la otra.
    """
    sembrar(db)
    salidas, _, _ = salidas_de(db)
    c = curva(salidas, titulo="duras", desde_carga=CORTE)

    assert c["na"] is None
    assert c["n_salidas"] == len(DURAS)
    assert barra(c, 1)["media"] == FACTURA
    assert barra(c, 2)["media"] == 0.0
    assert barra(c, 3)["media"] == 0.0
    assert barra(c, 4)["media"] == 0.0

    assert c["vuelve_el_dia"] == 2
    assert "1 día" in c["lectura"]
    assert "no más" in c["lectura"]


def test_la_escala_del_dibujo_la_decide_el_servidor(db):
    """Elegir el denominador de un dibujo es decidir cuánto parece que se mueve.

    Si el navegador buscara el máximo de lo que le llega, dos curvas de la misma
    pantalla saldrían con ejes distintos y las barras de una parecerían el doble
    que las de la otra sin que nada lo dijera.
    """
    sembrar(db)
    salidas, _, _ = salidas_de(db)
    c = curva(salidas, titulo="duras", desde_carga=CORTE)

    assert c["escala"] == abs(FACTURA)


def test_la_curva_avisa_de_que_son_ocho_salidas_en_vez_de_esconderlas(db):
    """La regla de la casa: por debajo de veinte se marca, no se esconde.

    Y el aviso de la curva entera va aparte del de cada barra a propósito. Las
    cuatro barras salen de las MISMAS ocho salidas: no son cuatro medidas de
    cuatro grupos, son una foto de ocho mirada cuatro veces. Cuatro avisos
    sueltos dejan leer "bueno, son cuatro avisos distintos".
    """
    sembrar(db)
    salidas, _, _ = salidas_de(db)
    c = curva(salidas, titulo="duras", desde_carga=CORTE)

    assert c["aviso"] and "8 salidas" in c["aviso"]
    assert all(d["aviso"] for d in c["por_dia"])
    # `na` y `aviso` no coinciden nunca: o no hay número, o lo hay y va marcado.
    assert all(d["na"] is None for d in c["por_dia"])


def test_la_curva_solo_cuenta_salidas_aisladas_y_las_de_los_bordes_no_lo_son(db):
    """Con otra salida en medio, el día +2 ya no mide lo que duró la primera.

    De las veinte sembradas hay dieciocho aisladas, y las dos que se caen son la
    primera y la última: sus días vecinos quedan FUERA de la cobertura de bici, y
    de un día sin cobertura no se sabe si hubo salida o no. Un `None` vecino
    rompe el aislamiento igual que una salida, y es lo correcto: lo que rompe la
    medida no es que hubiera otra salida, es no poder descartarlo.
    """
    sembrar(db)
    salidas, _, _ = salidas_de(db)

    assert len(salidas) == len(SUAVES) + len(DURAS)
    aisladas = [s for s in salidas if s.aislada]
    assert len(aisladas) == len(salidas) - 2

    primera, ultima = salidas[0], salidas[-1]
    assert not primera.aislada and not ultima.aislada
    assert "no se sabe si hubo salida" in primera.porque_no
    assert "no se sabe si hubo salida" in ultima.porque_no

    # Y la curva sin filtro de carga cuenta esas dieciocho, no las veinte.
    c = curva(salidas, titulo="todas")
    assert c["n_salidas"] == len(aisladas)
    assert f"{DIAS_AISLAMIENTO} días" in curva(salidas, titulo="t", desde_carga=1e9)["na"]


def test_la_curva_de_todas_las_salidas_diluye_la_factura_sin_borrarla(db):
    """Ocho duras y diez suaves aisladas: −80 ms repartidos entre dieciocho.

    Esta curva existe justo para eso, para poderse comparar con la de arriba. La
    de las duras dice −10; ésta dice −4,44 y sigue volviendo el día +2. Las dos
    son verdad y dicen cosas distintas, que es lo que se quiere enseñar.
    """
    sembrar(db)
    salidas, _, _ = salidas_de(db)
    c = curva(salidas, titulo="todas")

    esperada = round(FACTURA * len(DURAS) / c["n_salidas"], 2)
    assert barra(c, 1)["media"] == esperada
    assert barra(c, 2)["media"] == 0.0
    assert c["vuelve_el_dia"] == 2


# ---------------------------------------------------------------------------
# Escalón o cuesta: la frase que compara dos banderas, no dos magnitudes
# ---------------------------------------------------------------------------


def _con(sig):
    return {"significativa": sig}


def test_el_corte_aguanta_y_la_continua_no_es_un_escalon():
    frase = _escalon_o_cuesta(_con(True), _con(False))
    assert frase.startswith("Esto es un escalón")
    assert "no una cuesta" in frase


def test_la_continua_aguanta_y_el_corte_no_es_una_cuesta():
    """Y entonces el número de arriba es un sitio por donde partir, no una frontera.

    Publicarlo como umbral convertiría en hallazgo lo que solo es una elección de
    dónde cortar, y es el error que esta frase existe para no cometer.
    """
    frase = _escalon_o_cuesta(_con(False), _con(True))
    assert frase.startswith("Esto es una cuesta")
    assert "sitio por donde partir" in frase


def test_si_aguantan_las_dos_se_dice_que_no_se_pueden_separar():
    frase = _escalon_o_cuesta(_con(True), _con(True))
    assert "las dos" in frase
    assert "no se puede separar" in frase


def test_si_no_aguanta_ninguna_se_dice_que_lo_de_arriba_es_solo_lo_que_mas_separa():
    """La diferencia entre "separa más que los demás" y "separa de verdad".

    Es el caso peligroso: hay un número en grande en la pantalla y no hay
    hallazgo. Callarlo dejaría el número solo, y un número solo se lee como un
    hallazgo.
    """
    frase = _escalon_o_cuesta(_con(False), _con(False))
    assert "No aguanta ninguna" in frase
    assert "MÁS separa" in frase


def test_sin_correccion_aplicada_no_se_dice_nada():
    """`significativa` a `None` no es "no aguanta": es "no se ha corregido".

    Los tres valores dicen cosas distintas y confundir el tercero con el segundo
    escribiría un veredicto donde no hay ni prueba.
    """
    assert _escalon_o_cuesta(_con(None), _con(True)) is None
    assert _escalon_o_cuesta(_con(True), _con(None)) is None


def test_la_frase_de_escalon_no_mete_ningun_numero_dentro():
    """Compara dos "aguanta / no aguanta", no dos magnitudes.

    Meter aquí la `r` de la continua invitaría a leerla como la FUERZA del
    efecto cuando lo que se está diciendo es de qué FORMA es. La `r` va al lado,
    con su barra, donde se puede comparar con las demás `r` de la aplicación.
    """
    for corte in (True, False):
        for suave in (True, False):
            frase = _escalon_o_cuesta(_con(corte), _con(suave))
            assert not any(ch.isdigit() for ch in frase), frase


# ---------------------------------------------------------------------------
# Los huecos que se dicen en vez de callarse
# ---------------------------------------------------------------------------


def test_los_dias_del_final_sin_bici_no_se_llaman_de_antes_de_la_bici(db):
    """El fallo que encontraron los datos de verdad, convertido en test.

    La cobertura de bici acaba en la última salida, y la ventana acaba hoy. Los
    días de en medio son el reloj sin sincronizar -o sea lo único de todo el
    resumen sobre lo que se puede hacer algo hoy- y durante un tiempo se
    escribían como "días de antes de la bici". Decía lo contrario de la verdad y
    además escondía el único caso arreglable de los dos.
    """
    sembrar(db)
    _, _, sin_cobertura = salidas_de(db)

    ultimo = (len(SUAVES) + len(DURAS) - 1) * CADA
    assert sin_cobertura == {"antes": 0, "despues": N - 1 - ultimo}

    v = vista_umbral(db, dias=N, hoy=HOY)
    resumen = v["salidas"]["resumen"]
    assert "no tienen bici apuntada todavía" in resumen
    assert "antes de la bici" not in resumen


def test_los_de_antes_y_los_de_despues_se_cuentan_por_separado(db):
    """Dos huecos que no son la misma noticia y no se suman.

    Antes de la primera salida es historia que no existe y no va a existir
    nunca. Después de la última es una cola que se llena sola en cuanto
    sincronice el reloj. Contarlos juntos borra esa diferencia, y con ella el
    único de los dos sobre el que se puede actuar.
    """
    sembrar(db, cargas={10: 200.0, 11: 40.0})
    _, _, sin_cobertura = salidas_de(db)

    assert sin_cobertura["antes"] == 10 * CADA
    assert sin_cobertura["despues"] == N - 1 - 11 * CADA

    frase = _resumen_descartes({}, 2, sin_cobertura)
    assert "antes de la bici" in frase
    assert "no tienen bici apuntada todavía" in frase


def test_con_un_solo_dia_de_cola_la_frase_va_en_singular(db):
    """"los 1 último día" es de las cosas que nadie mira dos veces y chirrían siempre."""
    frase = _resumen_descartes({}, 5, {"antes": 0, "despues": 1})
    assert "el último día de la ventana no tiene bici apuntada todavía" in frase
    assert " 1 último" not in frase


def test_una_salida_sin_carga_estimada_es_un_descarte_y_no_un_cero(db):
    """La distinción de la que depende todo lo demás de este módulo.

    Una salida con `training_load` nulo NO es un día de descanso: es un día en
    que se salió y no se sabe cuánto. Contarla como cero la metería en el grupo
    de "por debajo del corte" y arrastraría la media de las suaves hacia el lado
    de las duras, sin que saltara nada.
    """
    for i in (0, 20, 40):
        db.add(
            Activity(
                garmin_activity_id=2000 + i,
                date=dia(i),
                is_cycling=True,
                duration_s=3600.0,
                training_load=200.0 if i != 20 else None,
            )
        )
    for i in range(N):
        db.add(DailyMetrics(date=dia(i), fetch_status="ok", hrv=HRV_BASE))
    db.commit()

    salidas, fuera, _ = salidas_de(db)

    assert fuera["sin_carga"] == 1
    assert [s.carga for s in salidas] == [200.0, 200.0]
    assert all(s.carga > 0 for s in salidas)


def test_la_grafica_dice_por_que_no_hay_linea_cuando_la_hrv_no_se_mueve(db):
    """El hueco que ningún test de "no se pinta undefined" habría visto.

    El dibujo escala el eje a lo medido. Con todas las noches iguales no hay alto
    contra el que dibujar, el navegador devuelve una cadena vacía y en la
    pantalla queda un hueco sin motivo en mitad de la vista. Es exactamente el
    fallo silencioso que este panel persigue, y se calla en el único sitio donde
    nadie lo está mirando.
    """
    for i in range(N):
        db.add(DailyMetrics(date=dia(i), fetch_status="ok", hrv=HRV_BASE))
    db.commit()

    g = grafica(db, desde=dia(0), hasta=dia(N - 1))
    assert g["n"] == N
    assert g["na"] and "raya recta" in g["na"]

    # Y el otro motivo, que es el de siempre: no hay con qué.
    assert "no se dibuja una línea" in (_na_grafica([50.0, 51.0]) or "")
    assert _na_grafica([50.0, 51.0, 52.0]) is None


def test_la_grafica_manda_la_altura_del_corte_ya_calculada(db):
    """Que el navegador dividiera `corte` entre `carga_maxima` daría el mismo
    píxel hoy y se despegaría el día que aquí se cambie el denominador. Y se
    despegaría en silencio: la raya seguiría saliendo, un poco mal puesta.
    """
    sembrar(db)
    g = grafica(db, desde=dia(0), hasta=dia(N - 1), corte=CORTE)

    assert g["na"] is None
    assert len(g["salidas"]) == len(SUAVES) + len(DURAS)
    assert g["carga_maxima"] == max(DURAS)
    assert g["corte"] == CORTE
    assert g["altura_corte"] == round(CORTE / max(DURAS), 4)
    assert g["rango"] == [HRV_TRAS_DURA, HRV_BASE]
    # La más dura va a altura 1 y ninguna se sale del dibujo.
    assert max(s["altura"] for s in g["salidas"]) == 1.0
    assert all(0 < s["altura"] <= 1 for s in g["salidas"])
    # Y cada palo sabe de qué lado del corte cae, calculado aquí y no allí.
    assert sum(1 for s in g["salidas"] if s["dura"]) == len(DURAS)


def test_el_techo_del_dibujo_es_el_suyo_y_no_una_cifra_escrita(db):
    """Se mueve la salida más dura y la altura del corte tiene que moverse con ella.

    Este test existe porque la batería de mutación de `out/mutar_umbral.py`
    encontró viva la mutación que cambia `corte / techo` por `corte / 250.0`. Y
    sobrevivía por una tontería con mala sombra: la salida más dura del sembrado
    vale justo 250, así que el denominador escrito a mano daba exactamente el
    mismo número que el de verdad y el test de arriba lo aprobaba.

    Es el fallo de siempre en su versión más barata: una constante que coincide
    con el dato el día que se escribe el test y deja de coincidir el día que
    cambian los datos, sin avisar. Aquí el techo se mueve a 300 a propósito para
    que las dos cuentas ya no puedan dar lo mismo.
    """
    cargas = cargas_sembradas()
    cargas[DURAS_EN[-1]] = 300.0
    sembrar(db, cargas=cargas)
    g = grafica(db, desde=dia(0), hasta=dia(N - 1), corte=CORTE)

    assert g["carga_maxima"] == 300.0
    assert g["altura_corte"] == round(CORTE / 300.0, 4)
    assert g["altura_corte"] != round(CORTE / max(DURAS), 4)
    # Y el palo más alto sigue siendo exactamente el borde del dibujo: el techo
    # que escala el corte y el que escala las barras son el mismo.
    assert max(s["altura"] for s in g["salidas"]) == 1.0


def test_la_banda_del_dibujo_es_la_mitad_central_y_no_el_recorrido_entero(db):
    """La banda dice "esto es lo normal", y para eso tiene que dejar algo fuera.

    Lo encontró la batería de mutación: abrir `BANDA` de (25, 75) a (0, 100)
    dejaba la suite entera en verde. Y no es un cambio de cosmética. Una banda que
    va del mínimo al máximo contiene, por construcción, TODAS las noches: la
    franja de detrás de la línea pasaría a decir "todo lo que has visto entra
    dentro de lo normal", que es justo lo contrario de lo que una banda sirve para
    decir. Del mismo color, en el mismo sitio y un poco más ancha: no hay nada en
    la pantalla que permita notarlo.

    Ninguno de los tests de la gráfica la miraba. Comprobaban el techo, la altura
    del corte, el rango y el motivo de que no haya línea, y la banda se coló por
    el hueco que dejaban entre todos ellos.

    Se siembran cien noches con veinte valores distintos, cinco veces cada uno,
    para que los cuartiles caigan donde se pueden calcular a mano: 44,75 y 54,25.
    Pero lo que se exige no son esos dos números sino la PROPIEDAD que los hace
    una banda -que una de cada cuatro noches quede fuera por cada lado-, porque
    los números solos los cumpliría cualquier par de percentiles y la propiedad no
    la puede fingir un (0, 100).
    """
    for i in range(N):
        db.add(DailyMetrics(date=dia(i), fetch_status="ok", hrv=40.0 + (i % 20)))
    db.commit()

    g = grafica(db, desde=dia(0), hasta=dia(N - 1))
    assert g["na"] is None and g["n"] == N
    assert g["rango"] == [40.0, 59.0]
    assert g["banda"] == {"desde": 44.8, "hasta": 54.2}

    # Acota: los dos bordes caen ESTRICTAMENTE dentro del recorrido medido.
    assert g["rango"][0] < g["banda"]["desde"] < g["banda"]["hasta"] < g["rango"][1]

    # Y acota por donde dice: un cuarto de las noches fuera por cada lado.
    medidos = [p["valor"] for p in g["puntos"] if p["valor"] is not None]
    assert len(medidos) == N
    assert sum(v < g["banda"]["desde"] for v in medidos) == N // 4
    assert sum(v > g["banda"]["hasta"] for v in medidos) == N // 4

    # Y por debajo del mínimo calculable la banda no se estrecha: se va.
    assert grafica(db, desde=dia(0), hasta=dia(1))["banda"] is None


def test_un_punto_suavizado_que_sale_de_una_sola_noche_es_una_cola_inventada():
    """El mínimo de la media móvil, y por qué el sembrado no podía verlo.

    Bajar `MINIMO_SUAVIZADO` de 4 a 1 dejaba la suite entera en verde, y el motivo
    es estructural, no un descuido de nadie: `sembrar` pone HRV los cien días, sin
    un solo hueco. Sobre una serie sin huecos los dos valores dan exactamente lo
    mismo, punto por punto, porque nunca hay menos de cuatro noches dentro de la
    ventana. Los tests no eran flojos: el sembrado hacía la pregunta imposible, y
    ésa es una forma de agujero que no se encuentra leyendo los tests, porque en
    los tests no hay nada escrito que esté mal.

    Con huecos, la diferencia es la que va de medir a rellenar. El día de una
    noche suelta publicaría un punto "suavizado" que es esa noche otra vez, con el
    mismo trazo que uno salido de siete. En una gráfica eso no se distingue: no
    hay manera de mirar la línea y saber qué parte es medida y qué parte es una
    cola inventada.

    Por eso el mínimo se AFIRMA además de comprobarse: más de media ventana tiene
    que ser noche de verdad. Es la lección de `N_MINIMO_TRAMO`, que sobrevivió a
    la primera batería porque el test leía la constante para construir el caso y
    encogía con ella.
    """
    assert MINIMO_SUAVIZADO > SUAVIZADO // 2, (
        "más de media ventana tiene que ser noche de verdad, o el punto dibujado "
        "dice más de lo que sabe"
    )

    dias = [dia(i) for i in range(12)]
    # Una noche suelta al principio y cuatro seguidas al final: el mínimo parte
    # justo entre las dos cosas.
    valores = {dia(0): 50.0, dia(6): 40.0, dia(7): 42.0, dia(8): 44.0, dia(9): 46.0}
    suave = _media_movil(valores, dias)

    assert suave[dia(0)] is None  # una sola noche en toda la ventana
    assert suave[dia(3)] is None  # dos, y cogidas de los dos extremos
    assert suave[dia(5)] is None  # tres: sigue sin llegar

    # Con las cuatro dentro sí, y el punto es la media de las cuatro.
    assert suave[dia(6)] == suave[dia(9)] == 43.0

    # Y en cuanto se sale una, se acaba la línea. No se estira: se corta.
    assert suave[dia(10)] is None


# ---------------------------------------------------------------------------
# La vista montada: que las dos preguntas lleguen juntas y en orden
# ---------------------------------------------------------------------------


def test_la_vista_entera_contesta_las_dos_preguntas_con_los_numeros_sembrados(db):
    """De punta a punta: la segunda pregunta se calcula SOBRE la respuesta de la primera.

    La curva de las duras se filtra por el corte que acaba de encontrar la
    frontera, no por una constante. Si ese cable se soltara, la vista seguiría
    pintando dos curvas plausibles y nadie lo notaría: por eso el test comprueba
    que el título de la curva lleva el corte dentro.
    """
    sembrar(db)
    v = vista_umbral(db, dias=N, hoy=HOY)

    assert v["umbral"]["frontera"]["carga"] == CORTE
    assert tramo_marcado(v["umbral"])["media"] == -6.0

    duras, todas = v["recuperacion"]["curvas"]
    assert f"{CORTE:.0f}" in duras["titulo"]
    assert duras["desde_carga"] == CORTE
    assert duras["n_salidas"] == len(DURAS)
    assert duras["vuelve_el_dia"] == 2
    assert todas["desde_carga"] is None
    assert todas["n_salidas"] == len(SUAVES) + len(DURAS) - 2

    assert v["salidas"]["medidas"] == len(SUAVES) + len(DURAS)
    assert v["salidas"]["fuera"] == {
        "sin_carga": 0,
        "sin_hrv_antes": 0,
        "sin_hrv_despues": 0,
    }
    assert v["grafica"]["altura_corte"] == round(CORTE / max(DURAS), 4)
    assert v["ventana"] == {
        "desde": dia(0).isoformat(),
        "hasta": HOY.isoformat(),
        "dias": N,
    }


def test_la_vista_con_la_base_vacia_explica_el_vacio_en_vez_de_devolver_ceros(db):
    """El primer día de un sistema que arranca no es un hallazgo de que no pasa nada.

    Ni un cero de relleno en toda la vista: cada hueco trae escrito por qué está
    vacío. Un 0,0 aquí se leería como "la bici no te afecta", que es justamente
    la conclusión que esta vista existe para no dar por descontada.
    """
    v = vista_umbral(db, dias=N, hoy=HOY)

    assert v["salidas"]["medidas"] == 0
    assert v["umbral"]["na"]
    assert v["umbral"]["tramos"] == []
    assert v["umbral"]["frontera"]["carga"] is None
    assert v["umbral"]["frontera"]["na"]
    assert v["grafica"]["na"]
    assert v["cobertura"]["bici"] is None
    for c in v["recuperacion"]["curvas"]:
        assert c["na"]
        assert c["vuelve_el_dia"] is None
        assert all(d["media"] is None for d in c["por_dia"])
