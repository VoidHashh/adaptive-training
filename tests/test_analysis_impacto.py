"""Vista 3 con datos sintéticos de resultado conocido.

Las tres preguntas que se pidieron por su nombre tienen aquí un test cada una, y
están sembradas de forma que la respuesta se puede escribir en el `assert` sin
haber ejecutado antes la implementación:

  - ¿sube la molestia lumbar tras el Día 2? -> se siembra que sí, exactamente
    tres puntos, y el test exige 5.0 contra 2.0;
  - ¿cuántos días de HRV cuesta una salida INTENSA? -> se siembra un bajón que
    dura UN día, y el test exige que la lectura diga "al día +2 ya está como
    siempre". Que salga el número de días, no tres correlaciones;
  - ¿el ánimo mejora tras la bici larga? -> se siembra que sí, y "larga" se
    define sola desde el cuarto superior de las salidas sembradas.

Y el ranking de ejercicios, que es el que se pidió como el más importante: se
siembra un ejercicio culpable, uno inocente y uno que se hace TODOS los días. El
tercero es el que importa de verdad, porque no tiene con qué compararse y la
tentación es esconderlo. Tiene que salir en la lista, el último, con el motivo
escrito.

Lo que NO se prueba aquí es la estadística (`test_analysis_stats`) ni la
contabilidad de las series (`test_analysis_series`). Esto prueba el montaje.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.analysis.impacto import (
    ADVERTENCIA_CONFUSION,
    _binaria,
    catalogo_de_respuestas,
    ejercicios_por_dia,
    exposiciones_de_bici,
    por_defecto,
    ranking_ejercicios,
    vista_impacto,
)
from app.analysis.series import cobertura
from app.models import Activity, Base, Checkin, DailyMetrics, WorkoutLog

HOY = date(2026, 9, 11)
N = 60


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def dia(i: int) -> date:
    """El día i-ésimo de una ventana de 60 que termina hoy, en orden natural."""
    return HOY - timedelta(days=N - 1 - i)


# ---------------------------------------------------------------------------
# Sembradores
# ---------------------------------------------------------------------------


def entreno(db, i, *, rutina=None, ejercicios=(), volumen=None, series=None, crudo=None):
    if crudo is None:
        crudo = json.dumps(
            {
                "exercises": [
                    {"exercise_template_id": c, "title": t} for c, t in ejercicios
                ]
            }
        )
    db.add(
        WorkoutLog(
            hevy_workout_id=f"w{i}",
            date=dia(i),
            routine_key=rutina,
            raw_json=crudo,
            total_volume_kg=volumen,
            total_sets=series,
        )
    )


def salida(db, i, *, nivel="media", minutos=60.0, carga=None, desnivel=None):
    db.add(
        Activity(
            garmin_activity_id=1000 + i,
            date=dia(i),
            is_cycling=True,
            intensity_level=nivel,
            duration_s=None if minutos is None else minutos * 60.0,
            training_load=carga,
            elevation_gain_m=desnivel,
        )
    )


def checkins(db, campos, *, hasta=N):
    for i in range(hasta):
        valores = campos(i)
        if valores:
            db.add(Checkin(date=dia(i), **valores))


def wellness(db, campos):
    for i in range(N):
        valores = campos(i)
        if valores:
            db.add(DailyMetrics(date=dia(i), fetch_status="ok", **valores))


def fila_de(vista, exposicion, respuesta):
    for f in vista["rejilla"]:
        if f["exposicion"]["clave"] == exposicion and f["respuesta"]["clave"] == respuesta:
            return f
    raise AssertionError(f"no está la fila {exposicion} x {respuesta}")


def a(fila, dias):
    for c in fila["por_dia"]:
        if c["dias_despues"] == dias:
            return c
    raise AssertionError(f"no está el retardo +{dias}")


# ---------------------------------------------------------------------------
# Las tres preguntas que se pidieron por su nombre
# ---------------------------------------------------------------------------


def test_sube_la_molestia_lumbar_tras_el_dia_2(db):
    """La primera pregunta del enunciado, sembrada para que la respuesta sea que sí.

    El Día 2 cae uno de cada cuatro, y el día siguiente la lumbar vale 5 en vez
    de 2. No hay ruido a propósito: si con una relación perfecta el montaje no
    devolviera 5.0 contra 2.0, el fallo estaría en el emparejado y no en la
    estadística, y con datos ruidosos no se distinguiría una cosa de la otra.

    Las medias importan tanto como la r. "r = 1.0" no contesta la pregunta;
    "2.0 los días normales, 5.0 después del Día 2" la contesta.
    """
    for i in range(N):
        entreno(db, i, rutina="dia_2" if i % 4 == 0 else "dia_1")
    checkins(db, lambda i: {"lower_discomfort": 5 if i % 4 == 1 else 2})
    db.commit()

    v = vista_impacto(db, dias=N, hoy=HOY)
    c = a(fila_de(v, "rutina_dia_2", "lower_discomfort"), 1)

    assert c["n"] == 59  # el último día no tiene un "día siguiente" que mirar
    assert c["n_expuesto"] == 15
    assert c["n_no_expuesto"] == 44
    assert c["media_expuesto"] == 5.0
    assert c["media_no_expuesto"] == 2.0
    assert c["diferencia"] == 3.0
    assert c["r"] == 1.0
    assert c["na"] is None

    lectura = fila_de(v, "rutina_dia_2", "lower_discomfort")["lectura"]
    assert "sube" in lectura
    assert "(peor)" in lectura


def test_cuantos_dias_de_hrv_cuesta_una_salida_intensa(db):
    """La segunda pregunta. Se siembra un bajón que dura exactamente un día.

    Lo que se exige no es una correlación: es una FRASE con un número de días.
    La pregunta era "cuántos días cuesta", y tres celdas de una rejilla no la
    contestan aunque contengan la respuesta.

    Se siembra el bajón solo en D+1 para que en D+2 la diferencia cambie de
    signo, que es la señal que busca `_lectura_recuperacion`.
    """
    for i in range(0, N, 5):
        salida(db, i, nivel="intensa", minutos=90.0)
    salida(db, N - 1, nivel="suave", minutos=45.0)  # estira la cobertura hasta el final
    wellness(db, lambda i: {"hrv": 40.0 if i % 5 == 1 else 60.0})
    db.commit()

    v = vista_impacto(db, dias=N, hoy=HOY)
    fila = fila_de(v, "bici_intensa", "hrv")

    uno = a(fila, 1)
    assert uno["media_expuesto"] == 40.0
    assert uno["media_no_expuesto"] == 60.0
    assert uno["diferencia"] == -20.0
    assert uno["r"] == -1.0

    dos = a(fila, 2)
    assert dos["media_expuesto"] == 60.0  # a los dos días ya está arriba otra vez

    assert fila["lectura"] == (
        "al día siguiente baja (peor), y al día +2 ya está como siempre"
    )


def test_el_animo_mejora_tras_la_bici_larga(db):
    """La tercera pregunta. Y "larga" no es un número inventado.

    Se siembran salidas de 120 y de 30 minutos a partes iguales, así que el
    cuarto superior de SUS salidas cae en 120 exacto. Nadie escribe "90 minutos"
    en ningún sitio: el umbral sale de lo que él hace, que es lo que se pidió y
    lo que seguirá significando lo mismo dentro de un año cuando aguante más.
    """
    for i in range(0, N, 2):
        salida(db, i, minutos=120.0 if i % 4 == 0 else 30.0)
    checkins(db, lambda i: {"mood": 5 if i % 4 == 1 else 2})
    db.commit()

    v = vista_impacto(db, dias=N, hoy=HOY)
    fila = fila_de(v, "bici_larga", "mood")

    assert fila["exposicion"]["etiqueta"] == "Salida larga (tu cuarto superior, 120 min o más)"

    uno = a(fila, 1)
    assert uno["n_expuesto"] == 15
    assert uno["media_expuesto"] == 5.0
    assert uno["media_no_expuesto"] == 2.0
    assert uno["r"] == 1.0
    assert "sube" in fila["lectura"]
    assert "(mejor)" in fila["lectura"]


# ---------------------------------------------------------------------------
# El ranking de ejercicios
# ---------------------------------------------------------------------------


def test_el_ranking_pone_primero_al_ejercicio_culpable(db):
    """El que se pidió como el más importante, sembrado con un culpable evidente.

    Tres ejercicios: uno que precede siempre a la molestia, uno que nunca, y uno
    que se hace TODOS los días. El tercero es el interesante: no tiene días sin
    él con los que compararse, así que no se puede calcular nada. Tiene que salir
    igualmente, el último y con el motivo escrito, porque esconderlo dejaría un
    ranking de dos ejercicios que parecería completo.
    """
    for i in range(N):
        ejercicios = [("neutro", "Plancha")]
        if i % 3 == 0:
            ejercicios.append(("malo", "Peso muerto"))
        if i % 3 == 1:
            ejercicios.append(("bueno", "Curl"))
        entreno(db, i, rutina="dia_1", ejercicios=ejercicios)
    checkins(db, lambda i: {"lower_discomfort": 5 if i % 3 == 1 else 2})
    db.commit()

    r = ranking_ejercicios(db, dias=N, hoy=HOY)

    assert r["n_ejercicios"] == 3
    assert r["respuesta"]["clave"] == "lower_discomfort"
    assert r["ordenado_por"] == "correlación a +1 día(s)"

    primero = r["ranking"][0]
    assert primero["clave"] == "malo"
    assert primero["etiqueta"] == "Peso muerto"
    assert primero["veces_hecho"] == 20
    c = a(primero, 1)
    assert c["media_expuesto"] == 5.0
    assert c["media_no_expuesto"] == 2.0
    assert c["r"] == 1.0

    # El inocente va detrás, y con el signo contrario: los días que lo hace, la
    # molestia del día siguiente es más baja que la media.
    segundo = r["ranking"][1]
    assert segundo["clave"] == "bueno"
    assert a(segundo, 1)["r"] < 0

    # Y el de todos los días, el último, calculado a N/A y con su porqué. El
    # porqué exacto importa: no es "faltan días", es que se hace SIEMPRE, y una
    # serie constante no tiene con qué correlacionarse. Dicho así se entiende que
    # esperar no lo va a arreglar.
    ultimo = r["ranking"][-1]
    assert ultimo["clave"] == "neutro"
    c = a(ultimo, 1)
    assert c["r"] is None
    assert c["p"] is None
    assert c["suficiente"] is False
    assert "vale siempre 1" in c["na"]


def test_un_ejercicio_hecho_dos_veces_no_se_calcula_pero_se_ve(db):
    """Dos días con y cincuenta sin no es una muestra: es una anécdota.

    Sin este corte saldría una r perfecta -con dos puntos SIEMPRE sale perfecta-
    encabezando el ranking de lo que le hace daño a la espalda, que es justo la
    lista en la que un falso positivo tiene consecuencias reales.
    """
    for i in range(N):
        ejercicios = [("base", "Sentadilla")]
        if i in (10, 20):
            ejercicios.append(("raro", "Buenos días"))
        entreno(db, i, rutina="dia_1", ejercicios=ejercicios)
    checkins(db, lambda i: {"lower_discomfort": 5 if i in (11, 21) else 2})
    db.commit()

    r = ranking_ejercicios(db, dias=N, hoy=HOY)
    raro = next(f for f in r["ranking"] if f["clave"] == "raro")

    assert raro["veces_hecho"] == 2
    c = a(raro, 1)
    assert c["r"] is None
    assert "solo 2 día(s) con esto" in c["na"]
    assert "al menos 3 de cada" in c["na"]


def test_el_motivo_que_sale_es_el_del_eslabon_que_falta_de_verdad(db):
    """Cuando falta la serie de respuesta entera, la culpa no es de la exposición.

    Si no hay ni un solo par, los dos grupos salen a cero y el mensaje de "0 días
    con esto y 0 sin ello" señalaría al ejercicio o a la salida, cuando lo que
    pasa es que no hay check-ins. Mandaría a mirar exactamente al sitio
    equivocado, que en una pantalla de diagnóstico es peor que no decir nada.

    Gana el motivo de más abajo en la cadena, que es el que hay que arreglar
    primero.
    """
    for i in range(0, N, 3):
        salida(db, i, nivel="intensa", minutos=90.0)
    db.commit()  # ni un check-in, ni una métrica de reloj

    v = vista_impacto(db, dias=N, hoy=HOY)
    c = a(fila_de(v, "bici_intensa", "fatigue"), 1)

    assert c["r"] is None
    assert "las dos cosas medidas" in c["na"]
    assert "0 día(s) con esto" not in c["na"]

    # Y con la respuesta presente y variando, el motivo sí pasa a ser el del
    # reparto de grupos: hay pares de sobra, pero solo dos días con la exposición.
    checkins(db, lambda i: {"fatigue": 1 + (i % 5)})
    salida(db, 1, nivel="suave", minutos=40.0)
    salida(db, 2, nivel="suave", minutos=40.0)
    db.commit()

    v = vista_impacto(db, dias=N, hoy=HOY)
    c = a(fila_de(v, "bici_suave", "fatigue"), 1)

    assert c["n"] > 20  # pares hay: lo que falta es reparto
    assert "2 día(s) con esto" in c["na"]
    assert "al menos 3 de cada" in c["na"]


def test_el_nombre_del_yaml_le_gana_al_de_hevy(db):
    """El crudo trae el título de Hevy; el YAML trae el que él le puso.

    Gana el del YAML, porque es el nombre con el que piensa la rutina. Un ranking
    que dice "Barbell Romanian Deadlift" le obliga a traducir mentalmente cada
    fila para saber de qué le están hablando.
    """

    class Cfg:
        raw = {
            "routines": {
                "dia_1": {
                    "exercises": [
                        {"template_id": "abc", "name": "Peso muerto rumano (el mío)"}
                    ]
                }
            }
        }

    for i in range(N):
        entreno(db, i, rutina="dia_1", ejercicios=[("abc", "Barbell Romanian Deadlift")])
    checkins(db, lambda i: {"lower_discomfort": 2})
    db.commit()

    sin_cfg = ranking_ejercicios(db, dias=N, hoy=HOY)
    assert sin_cfg["ranking"][0]["etiqueta"] == "Barbell Romanian Deadlift"

    con_cfg = ranking_ejercicios(db, Cfg(), dias=N, hoy=HOY)
    assert con_cfg["ranking"][0]["etiqueta"] == "Peso muerto rumano (el mío)"


def test_una_respuesta_que_no_existe_revienta_con_la_lista(db):
    """Un nombre mal escrito en la llamada no puede devolver un ranking vacío.

    Vacío parecería "ningún ejercicio se relaciona con nada", que es una
    conclusión, y sería mentira.
    """
    with pytest.raises(ValueError) as e:
        ranking_ejercicios(db, respuesta="lumbago", dias=N, hoy=HOY)
    assert "lumbago" in str(e.value)
    assert "lower_discomfort" in str(e.value)


# ---------------------------------------------------------------------------
# El ausente y el cero, otra vez
# ---------------------------------------------------------------------------


def test_fuera_de_lo_observado_no_hay_ceros(db):
    """Antes de que Hevy estuviera conectado, "no lo hizo" no se puede afirmar.

    Dentro de la ventana observada un día sin ese ejercicio es un cero de verdad
    y hace falta: es la mitad del contraste. Fuera, un cero diría que no entrenó
    cuando lo que pasa es que nadie miró.
    """
    for i in range(30, 41):
        entreno(db, i, rutina="dia_1", ejercicios=[("x", "X")] if i % 2 == 0 else [])
    db.commit()

    cob = cobertura(db)
    por_dia, _ = ejercicios_por_dia(db, dia(0), dia(N - 1))
    serie = _binaria(por_dia, "x", dia(0), dia(N - 1), cob.fuerza)

    assert serie[dia(0)] is None  # nadie miró
    assert serie[dia(29)] is None
    assert serie[dia(30)] == 1.0  # observado y hecho
    assert serie[dia(31)] == 0.0  # observado y no hecho: un cero legítimo
    assert serie[dia(40)] == 1.0
    assert serie[dia(41)] is None
    assert serie[dia(N - 1)] is None


def test_un_crudo_ilegible_se_salta_sin_inventar_un_dia_vacio(db):
    """Un JSON roto es un entreno que no se puede desglosar, no un día sin ejercicios.

    Contarlo como día sin ejercicios metería un cero en todas las series de
    ejercicio a la vez, y ese día pasaría a ser evidencia en contra de todos
    ellos.
    """
    entreno(db, 10, rutina="dia_1", ejercicios=[("a", "A")])
    entreno(db, 11, rutina="dia_1", crudo="{esto no es json")
    entreno(db, 12, rutina="dia_1", ejercicios=[("a", "A")])
    db.commit()

    por_dia, nombres = ejercicios_por_dia(db, dia(0), dia(N - 1))

    assert set(por_dia) == {dia(10), dia(12)}
    assert dia(11) not in por_dia
    assert nombres == {"a": "A"}


def test_si_todas_las_salidas_duran_lo_mismo_ninguna_es_larga_y_corta_a_la_vez(db):
    """Los dos cuartiles coinciden, y sin el corte cada salida sería las dos cosas.

    Dos exposiciones idénticas con nombres contrarios en la misma pantalla, cada
    una con su correlación, diciendo que lo largo y lo corto le sientan
    exactamente igual de mal.
    """
    for i in range(0, N, 6):
        salida(db, i, minutos=60.0)
    db.commit()

    cob = cobertura(db)
    defs, sers = exposiciones_de_bici(db, dia(0), dia(N - 1), cob)
    claves = {d.clave for d in defs}

    assert "bici_larga" in claves
    assert "bici_corta" in claves  # la definición existe...
    # ...pero ningún día la cumple, así que no hay contradicción en pantalla.
    assert all(v == 0.0 for v in sers["bici_corta"].values() if v is not None)
    assert any(v == 1.0 for v in sers["bici_larga"].values())


def test_una_salida_sin_duracion_no_se_marca_como_corta(db):
    """Sin duración no se sabe si fue larga o corta, y eso no es "fue corta".

    Era `sum((a.duration_s or 0.0) for a in acts) / 60.0`: cero minutos cae
    siempre en el cuarto inferior, así que TODA salida sin duración se marcaba
    corta. La frase que salía de ahí -"los días de salida corta duermes peor"-
    es concreta, se lee como un hecho sobre su propio histórico, y estaría
    construida con días en los que la salida pudo ser la más larga del mes.

    Tampoco vale el cero por omisión en la otra dirección: ese día no es un "no
    rodó largo", es un "no consta cuánto rodó". Sale del contraste de duración
    igual que los días de antes de la ventana, por la misma razón.
    """
    for i in range(0, 48, 6):
        salida(db, i, minutos=30.0 + i)
    salida(db, 50, minutos=None)
    db.commit()

    cob = cobertura(db)
    _, sers = exposiciones_de_bici(db, dia(0), dia(N - 1), cob)

    assert sers["bici_corta"][dia(50)] is None
    assert sers["bici_larga"][dia(50)] is None
    # Y los días de al lado, que sí tienen dato, siguen siendo ceros legítimos.
    assert sers["bici_corta"][dia(49)] == 0.0
    assert sers["bici_larga"][dia(49)] == 0.0


def test_una_salida_sin_duracion_no_tira_del_umbral_de_los_demas_dias(db):
    """El cero de relleno no solo mentía sobre su día: contaminaba el corte.

    El umbral de "larga" es el cuartil superior de SUS salidas. Metiendo ceros en
    esa distribución el corte baja, y entonces salidas normales de los demás días
    pasan a llamarse largas. Un dato que falta en un día no puede cambiar la
    etiqueta de los otros cincuenta y nueve.
    """
    for i in range(0, 48, 6):
        salida(db, i, minutos=30.0 + i)
    db.commit()
    cob = cobertura(db)
    defs_limpio, _ = exposiciones_de_bici(db, dia(0), dia(N - 1), cob)

    salida(db, 50, minutos=None)
    db.commit()
    cob = cobertura(db)
    defs_con_hueco, _ = exposiciones_de_bici(db, dia(0), dia(N - 1), cob)

    etiqueta = {d.clave: d.etiqueta for d in defs_limpio}
    con_hueco = {d.clave: d.etiqueta for d in defs_con_hueco}
    assert etiqueta["bici_larga"] == con_hueco["bici_larga"]
    assert etiqueta["bici_corta"] == con_hueco["bici_corta"]


def test_una_salida_sin_duracion_sigue_contando_donde_si_hay_dato(db):
    """Lo que falta es la duración, no la salida entera.

    La intensidad la clasificó Garmin por zonas y está ahí. Tirar el día de todos
    los contrastes por un campo que solo le hace falta a dos sería el error
    contrario: perder evidencia buena por prudencia mal puesta.
    """
    for i in range(0, 48, 6):
        salida(db, i, minutos=30.0 + i)
    salida(db, 50, nivel="intensa", minutos=None)
    db.commit()

    cob = cobertura(db)
    _, sers = exposiciones_de_bici(db, dia(0), dia(N - 1), cob)

    assert sers["bici_cualquiera"][dia(50)] == 1.0
    assert sers["bici_intensa"][dia(50)] == 1.0
    assert sers["bici_intensa"][dia(49)] == 0.0


# ---------------------------------------------------------------------------
# Nada se esconde, y las trampas de la vista
# ---------------------------------------------------------------------------


def test_con_la_base_vacia_sale_la_rejilla_entera_con_sus_motivos(db):
    """Nada de medias tintas: ningún gráfico oculto, ninguna vista aplazada.

    Con la base vacía siguen saliendo las cuatro exposiciones de bici y las cinco
    continuas contra las doce respuestas, con el motivo escrito en cada casilla.
    Lo que no sale son las rutinas y los ejercicios, y eso es correcto: no son
    una lista fija, se descubren de lo que se ha entrenado.
    """
    v = vista_impacto(db, dias=N, hoy=HOY)

    exposiciones = {f["exposicion"]["clave"] for f in v["rejilla"]}
    assert exposiciones == {
        "bici_cualquiera",
        "bici_suave",
        "bici_media",
        "bici_intensa",
        "volumen_fuerza",
        "series_fuerza",
        "carga_bici",
        "minutos_bici",
        "desnivel_bici",
    }
    assert len(v["rejilla"]) == 9 * 12

    for fila in v["rejilla"]:
        for c in fila["por_dia"]:
            assert c["r"] is None
            assert c["na"], f"{fila['exposicion']['clave']} sin motivo escrito"
            assert c["n"] == 0


def test_la_p_corregida_viaja_en_todas_las_casillas(db):
    """La cruda sola invita a leer la rejilla como si fuera una sola pregunta.

    Son cientos de casillas: con el 5% de siempre, unas cuantas salen
    "significativas" aunque no haya absolutamente nada que encontrar. La
    corregida va al lado, nunca en lugar de la cruda, y nunca por debajo.
    """
    for i in range(N):
        entreno(db, i, rutina="dia_2" if i % 4 == 0 else "dia_1")
    checkins(db, lambda i: {"lower_discomfort": 5 if i % 4 == 1 else 2})
    db.commit()

    v = vista_impacto(db, dias=N, hoy=HOY)
    con_p = 0
    for fila in v["rejilla"]:
        for c in fila["por_dia"]:
            assert "p_corregida" in c
            assert "significativa" in c
            if c["p"] is not None:
                con_p += 1
                assert c["p_corregida"] >= c["p"] - 1e-9
                assert c["significativa"] == (c["p_corregida"] < 0.05)
            else:
                assert c["p_corregida"] is None
                assert c["significativa"] is False
    assert con_p > 0


def test_la_advertencia_de_confusion_viaja_en_la_respuesta(db):
    """No es un comentario en el código: es parte del resultado.

    Un ranking de ejercicios NO puede separar el peso muerto del día en que toca
    peso muerto, y quien mira la pantalla tiene que leer eso en la pantalla.
    """
    v = vista_impacto(db, dias=N, hoy=HOY)
    r = ranking_ejercicios(db, dias=N, hoy=HOY)

    assert v["advertencia"] == ADVERTENCIA_CONFUSION
    assert r["advertencia"] == ADVERTENCIA_CONFUSION
    assert "no puede separar" in ADVERTENCIA_CONFUSION.lower()


def test_el_retardo_mas_largo_no_pierde_dias_por_el_borde_de_la_ventana(db):
    """El +3 necesita días POSTERIORES al final de la ventana, y se piden.

    Sin margen, los tres últimos días de exposición se caerían siempre, y la
    fila del +3 tendría menos n que la del +1 por un motivo puramente
    administrativo. Parecería que hay menos datos cuanto más lejos se mira, que
    es exactamente lo que NO está pasando.
    """
    for i in range(N):
        entreno(db, i, rutina="dia_2" if i % 4 == 0 else "dia_1")
    # Tres días de respuestas más allá del final de la ventana.
    for extra in range(1, 4):
        db.add(Checkin(date=HOY + timedelta(days=extra), lower_discomfort=3))
    checkins(db, lambda i: {"lower_discomfort": 5 if i % 4 == 1 else 2})
    db.commit()

    fila = fila_de(vista_impacto(db, dias=N, hoy=HOY), "rutina_dia_2", "lower_discomfort")

    assert a(fila, 1)["n"] == 60
    assert a(fila, 2)["n"] == 60
    assert a(fila, 3)["n"] == 60


def test_la_ventana_y_la_cobertura_van_siempre_juntas(db):
    """Lo que se pidió mirar y lo que existe no son lo mismo, y la diferencia es el dato.

    Se piden 60 días y solo hay entrenos en tres. La ventana tiene que seguir
    diciendo 60 -es lo que se preguntó- y la cobertura tiene que decir la verdad.
    """
    for i in (10, 11, 12):
        entreno(db, i, rutina="dia_1")
    db.commit()

    v = vista_impacto(db, dias=N, hoy=HOY)

    assert v["ventana"] == {
        "desde": dia(0).isoformat(),
        "hasta": HOY.isoformat(),
        "dias": N,
    }
    assert v["cobertura"]["fuerza"] == {
        "desde": dia(10).isoformat(),
        "hasta": dia(12).isoformat(),
    }
    assert v["cobertura"]["bici"] is None
    assert v["retardos"] == [1, 2, 3]


def test_las_exposiciones_continuas_no_traen_medias_de_grupo(db):
    """El volumen no es un sí o un no, así que no hay "los días con" y "los días sin".

    Inventar dos grupos partiendo el volumen por la mitad daría dos medias
    perfectamente presentables y un umbral que no ha decidido nadie.
    """
    for i in range(N):
        entreno(db, i, rutina="dia_1", volumen=1000.0 + i * 10, series=12)
    checkins(db, lambda i: {"fatigue": 1 + (i % 5)})
    db.commit()

    v = vista_impacto(db, dias=N, hoy=HOY)
    c = a(fila_de(v, "volumen_fuerza", "fatigue"), 1)

    assert "media_expuesto" not in c
    assert "n_expuesto" not in c
    assert c["n"] > 0

    # Pero SÍ trae frase, y en la forma de dosis. Hasta el 2026-09-13 aquí se
    # comprobaba que `lectura` fuese `None`, porque la capa de lenguaje solo
    # cubría las exposiciones de sí-o-no. El efecto era que las cuatro
    # relaciones más fuertes de todo el histórico -carga, minutos y desnivel
    # contra la HRV y el Body Battery, las cuatro continuas- eran justo las
    # únicas que llegaban al navegador sin una sola palabra que las explicara.
    #
    # No hay medias de grupo porque no hay grupos, y eso sigue igual. Lo que no
    # se sostiene es que de "no hay dos grupos" se siga "no hay nada que
    # contar": una continua se cuenta como dosis -cuanto más, más- en vez de
    # como contraste.
    lectura = fila_de(v, "volumen_fuerza", "fatigue")["lectura"]
    assert lectura is not None
    assert lectura.startswith("cuanto más acumulas")


# ---------------------------------------------------------------------------
# El catálogo de respuestas: que el desplegable sepa qué hay dentro de cada
# opción ANTES de que haya que elegirla para averiguarlo.
# ---------------------------------------------------------------------------


def test_el_catalogo_cuenta_las_casillas_vivas_de_cada_respuesta(db):
    """Cada opción del desplegable declara cuánto tiene dentro.

    Se siembra la bici contra el reloj -que sí deja correlacionar- y NO se
    siembra ningún check-in, que es exactamente el estado del sistema el día que
    esto se escribió: doce respuestas ofrecidas, cinco con datos.
    """
    for i in range(N):
        salida(db, i, minutos=60.0 + i, carga=100.0 + i)
    wellness(db, lambda i: {"hrv": 50.0 + (i % 7)})
    db.commit()

    v = vista_impacto(db, dias=N, hoy=HOY)
    por_clave = {r["clave"]: r for r in v["respuestas"]}

    assert por_clave["hrv"]["n"] > 0
    assert por_clave["hrv"]["vacia"] is False
    assert por_clave["fatigue"]["n"] == 0
    assert por_clave["fatigue"]["vacia"] is True


def test_el_catalogo_trae_todas_las_respuestas_de_la_rejilla_sin_repetir(db):
    for i in range(N):
        salida(db, i, minutos=60.0 + i)
    wellness(db, lambda i: {"hrv": 50.0 + (i % 7)})
    db.commit()

    v = vista_impacto(db, dias=N, hoy=HOY)
    del_catalogo = [r["clave"] for r in v["respuestas"]]
    de_la_rejilla = {f["respuesta"]["clave"] for f in v["rejilla"]}

    assert len(del_catalogo) == len(set(del_catalogo)), "hay respuestas repetidas"
    assert set(del_catalogo) == de_la_rejilla


def test_el_denominador_del_catalogo_cuenta_TODAS_las_casillas_de_esa_respuesta(db):
    for i in range(N):
        salida(db, i, minutos=60.0 + i)
    wellness(db, lambda i: {"hrv": 50.0 + (i % 7)})
    db.commit()

    v = vista_impacto(db, dias=N, hoy=HOY)
    hrv = next(r for r in v["respuestas"] if r["clave"] == "hrv")
    esperadas = sum(
        len(f["por_dia"]) for f in v["rejilla"] if f["respuesta"]["clave"] == "hrv"
    )
    assert hrv["de"] == esperadas
    assert hrv["n"] <= hrv["de"]


def test_la_vista_no_se_estrena_en_una_respuesta_vacia(db):
    """El fallo concreto que esto arregla.

    El cliente abría en la primera opción de la rejilla, que es el cansancio.
    Con el sistema recién arrancado el cansancio tiene cero casillas, así que la
    vista de Impacto se estrenaba vacía teniendo las de la bici calculadas a dos
    clics de distancia.
    """
    for i in range(N):
        salida(db, i, minutos=60.0 + i)
    wellness(db, lambda i: {"hrv": 50.0 + (i % 7)})
    db.commit()

    v = vista_impacto(db, dias=N, hoy=HOY)
    elegida = v["respuesta_por_defecto"]

    assert elegida is not None
    escogida = next(r for r in v["respuestas"] if r["clave"] == elegida)
    assert escogida["vacia"] is False
    assert escogida["n"] > 0


def test_sin_ningun_dato_no_se_finge_una_respuesta_por_defecto(db):
    """Que no haya con qué abrir es un estado real y hay que poder decirlo.

    Devolver la primera de la lista aquí daría un desplegable que promete doce
    vistas y abre en una vacía sin explicar por qué, que es justo el fallo
    silencioso de interfaz que este rediseño viene a quitar.
    """
    v = vista_impacto(db, dias=N, hoy=HOY)

    assert v["respuesta_por_defecto"] is None
    assert all(r["vacia"] for r in v["respuestas"])


def test_una_correlacion_de_cero_clavado_cuenta_como_calculada():
    """r = 0.0 es un RESULTADO, no un hueco.

    Significa "no se parecen en nada", que es una respuesta perfectamente
    buena a la pregunta de la vista. Un filtro por verdad-falsedad -`if
    c.get("r")`- lo tiraría junto a los `None`, y entonces la opción del
    desplegable se marcaría vacía teniendo dentro justamente el hallazgo más
    rotundo: que ahí no hay nada que ver.

    Va con una rejilla montada a mano en vez de sembrando la base porque
    ninguna siembra razonable da un cero clavado, y una mutación que solo se
    ve con un cero clavado no se caza sembrando.
    """
    rejilla = [
        {
            "respuesta": {"clave": "hrv", "etiqueta": "Variabilidad (HRV)"},
            "por_dia": [
                {"dias_despues": 1, "r": 0.0},
                {"dias_despues": 2, "r": None},
            ],
        }
    ]
    cat = catalogo_de_respuestas(rejilla)

    assert cat[0]["n"] == 1, "el cero clavado se ha perdido por falsy"
    assert cat[0]["de"] == 2
    assert cat[0]["vacia"] is False
    # Y por lo tanto sirve para abrir la vista.
    assert por_defecto(cat) == "hrv"
