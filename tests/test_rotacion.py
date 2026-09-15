"""Qué lleva más de una vuelta sin hacerse.

Todo lo de aquí es sobre una función pura: se le da el ciclo y la lista de lo
ejecutado, y contesta. No hay base de datos ni configuración, y por eso los
casos se pueden escribir como lo que son -historias de entrenamiento- en vez de
como montajes.

La lista va SIEMPRE de más reciente a más antigua, igual que la consulta que la
produce (`repository.ejecutadas_del_ciclo`). Escribirla al revés en un test
haría pasar cosas que en producción no pasan, así que el helper `historia` la
recibe en el orden en que se entrenó -que es como uno lo piensa- y la invierte.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.engine.rotacion import CADUCA_TRAS, Pendiente, pendientes, to_dict

ORDEN = ["dia_1", "dia_2", "dia_3"]
INICIO = date(2026, 8, 3)


def historia(*claves: str) -> list[tuple[str, date]]:
    """Lo entrenado en orden cronológico, devuelto como lo lee el repositorio.

    Una sesión por día y días consecutivos: las fechas no entran en ningún
    umbral -la unidad es la sesión- y separarlas solo añadiría ruido a los
    casos. Lo único que se usa es el orden.
    """
    con_fecha = [(k, INICIO + timedelta(days=i)) for i, k in enumerate(claves)]
    return list(reversed(con_fecha))


def claves(lista) -> list[str]:
    return [p.clave for p in lista]


# ---------------------------------------------------------------------------
# El ciclo corriente no marca nada
# ---------------------------------------------------------------------------
#
# Es la mitad más importante del módulo. Una lista de pendientes que se llena
# sola cuando todo va bien no es una alarma: es decorado, y además convierte la
# línea del mensaje en algo que se deja de leer a la semana.


def test_el_ciclo_perfecto_no_deja_nada_pendiente():
    assert pendientes(ORDEN, historia("dia_1", "dia_2", "dia_3")) == []
    assert pendientes(
        ORDEN, historia(*(ORDEN * 4))
    ) == [], "cuatro vueltas limpias han marcado algo"


def test_a_media_vuelta_tampoco():
    """Que el Día 3 no se haya hecho todavía no es que lleve sin hacerse."""
    assert pendientes(ORDEN, historia("dia_1", "dia_2")) == []


def test_una_rutina_que_no_se_ha_hecho_nunca_no_esta_pendiente():
    """El día que se añade una rutina al config no es el día de avisar de ella.

    `sesiones_desde` no tiene valor posible aquí: no hay desde cuándo contar.
    Inventarle un cero diría "recién hecha" y un infinito diría "abandonada", y
    las dos son afirmaciones que nadie ha comprobado.
    """
    orden = [*ORDEN, "dia_4_recien_anadido"]
    assert claves(pendientes(orden, historia(*(ORDEN * 3)))) == []


def test_sin_ciclo_no_hay_nada_que_contar():
    assert pendientes([], historia("dia_1")) == []


def test_sin_nada_entrenado_no_hay_nada_pendiente():
    """Un sistema recién arrancado no tiene deudas."""
    assert pendientes(ORDEN, []) == []


# ---------------------------------------------------------------------------
# El desvío puntual, que es el caso de uso que lo motivó todo
# ---------------------------------------------------------------------------


def test_la_manana_de_despues_del_desvio_no_dice_nada():
    """La vuelta EXACTA todavía no es "lleva sin hacerse". Es el umbral entero.

    Tocaba el Día 1, se hizo el Día 2, y esto es la mañana siguiente. El Día 1
    está a tres sesiones, que con un ciclo de tres es una vuelta clavada. Es el
    único caso en el que `> len(orden)` y `>= len(orden)` contestan distinto -el
    ciclo corriente no llega nunca a tres, se queda en dos-, así que este test
    es lo único que sujeta esa desigualdad.

    Y contesta que no, porque decirlo aquí sería avisar de algo que pasó ayer,
    que se hizo aposta y que el propio ciclo va a devolver en dos sesiones. La
    condición para hablar no es que te la saltaras: es que el ciclo te la haya
    vuelto a ofrecer y tampoco la hicieras.
    """
    desvio = historia("dia_1", "dia_2", "dia_3", "dia_2")
    assert pendientes(ORDEN, desvio) == []

    # Y no es que la cuenta no se lleve: es que tres no pasa de tres.
    assert pendientes(ORDEN, desvio, caduca_tras=0) == []


def test_saltarse_una_vez_el_dia_1_lo_marca_justo_cuando_ya_toca_otra_vez():
    """Y por eso el mensaje calla la que está proponiendo hoy.

    Tocaba el Día 1, se hizo el Día 2, y desde ahí el ciclo siguió solo: Día 3
    y vuelta al Día 1. En el momento en que el Día 1 vuelve a ser la propuesta,
    han pasado cuatro sesiones desde la última vez que se hizo -una vuelta
    entera y una más-, así que esta función lo marca.

    No está mal marcado: es verdad que lleva cuatro. Lo que pasa es que decirlo
    esa mañana no informa de nada, porque es justo lo que el sistema está
    proponiendo. De eso se encarga el mensaje, que calla la pendiente que
    coincide con la propuesta del día; aquí se deja marcada a propósito para que
    el siguiente caso -saltársela DOS veces- tenga de dónde salir.
    """
    hecho = historia("dia_1", "dia_2", "dia_3", "dia_2", "dia_3")
    assert claves(pendientes(ORDEN, hecho)) == ["dia_1"]


def test_elegir_siempre_la_misma_es_lo_que_de_verdad_se_cuenta():
    """El caso que la rotación no puede ver sola.

    Una vuelta limpia y a partir de ahí el Día 2 cuatro veces. La rotación solo
    mira la ÚLTIMA sesión ejecutada, así que no se entera: después de un Día 2
    propone el Día 3, se hace otro Día 2, vuelve a proponer el Día 3, y el
    puntero no tiene dónde anotar que lleva cuatro veces sin que le hagan caso.
    La cuenta hacia atrás sí lo ve.
    """
    hecho = historia("dia_1", "dia_2", "dia_3", "dia_2", "dia_2", "dia_2", "dia_2")
    p = pendientes(ORDEN, hecho)
    assert claves(p) == ["dia_1", "dia_3"]
    assert p[0].sesiones_desde == 6
    assert p[0].ultima_vez == INICIO


def test_se_nombra_primero_lo_que_mas_lleva_parado():
    """El mensaje nombra una sola, y tiene que ser la que más falta hace decir."""
    hecho = historia(
        "dia_1", "dia_3", "dia_2", "dia_2", "dia_2", "dia_2", "dia_2",
    )
    p = pendientes(ORDEN, hecho)
    assert claves(p) == ["dia_1", "dia_3"]
    assert p[0].sesiones_desde > p[1].sesiones_desde


def test_hacerla_borra_la_cuenta():
    """No hay deuda acumulada: se hace y se acabó.

    Es lo que separa esto de un contador de sesiones perdidas. El sistema no
    lleva la cuenta de lo que debes, lleva la cuenta de lo que hace que no
    haces; en cuanto se hace, deja de tener nada que decir.
    """
    parado = historia(*(ORDEN + ["dia_2"] * 4))
    assert claves(pendientes(ORDEN, parado)) == ["dia_1", "dia_3"]

    al_dia = historia(*(ORDEN + ["dia_2"] * 4 + ["dia_1", "dia_3"]))
    assert claves(pendientes(ORDEN, al_dia)) == []


# ---------------------------------------------------------------------------
# La caducidad
# ---------------------------------------------------------------------------
#
# No es un plazo para hacer nada: al agotarse no pasa absolutamente nada salvo
# que el mensaje deja de repetir la línea. Se mide en sesiones y no en días
# porque con dos entrenos por semana catorce días son dos sesiones y con cuatro
# son ocho, y un umbral en días diría cosas distintas según la semana.


def test_la_pendiente_caduca_a_las_tres_sesiones_de_estarlo():
    hecho = historia(*(["dia_1"] + ["dia_2", "dia_3"] * 3))
    p = next(x for x in pendientes(ORDEN, hecho) if x.clave == "dia_1")
    assert p.sesiones_desde == 6
    assert not p.caducada, "seis sesiones son la vuelta más tres justas"

    una_mas = historia(*(["dia_1"] + ["dia_2", "dia_3"] * 3 + ["dia_2"]))
    p = next(x for x in pendientes(ORDEN, una_mas) if x.clave == "dia_1")
    assert p.sesiones_desde == 7
    assert p.caducada


def test_una_caducada_sigue_en_la_lista():
    """Deja de decirse, no deja de saberse.

    Quitarla aquí sería borrar el dato justo en el caso en que más dice de la
    práctica real: una rutina que se lleva meses sin tocar. Lo que se apaga es
    el aviso de la mañana, y eso lo decide el mensaje leyendo `caducada`.
    """
    hecho = historia(*(ORDEN + ["dia_2"] * 20))
    p = pendientes(ORDEN, hecho)
    assert "dia_1" in claves(p)
    assert all(x.caducada for x in p if x.clave == "dia_1")


def test_el_umbral_de_caducidad_se_puede_mover_sin_tocar_el_de_pendiente():
    hecho = historia(*(["dia_1"] + ["dia_2", "dia_3"] * 3 + ["dia_2"]))
    assert next(
        x for x in pendientes(ORDEN, hecho, caduca_tras=99) if x.clave == "dia_1"
    ).caducada is False
    assert CADUCA_TRAS == 3


# ---------------------------------------------------------------------------
# El volcado
# ---------------------------------------------------------------------------


def test_el_volcado_lleva_las_cuatro_claves_y_ninguna_va_a_none():
    """Misma regla que el resto de los payloads: una clave ausente es undefined.

    Lo lee el JSON de la decisión y de ahí la ficha. Y aquí ninguna va vacía,
    que es una propiedad del filtro y no una casualidad: lo que no tiene fecha
    ni cuenta -lo que no se ha hecho nunca- no llega a entrar en la lista.
    """
    hecho = historia(*(ORDEN + ["dia_2"] * 4))
    volcado = to_dict(pendientes(ORDEN, hecho))
    assert volcado
    for fila in volcado:
        assert set(fila) == {"clave", "sesiones_desde", "ultima_vez", "caducada"}
        assert isinstance(fila["ultima_vez"], str)
        assert isinstance(fila["sesiones_desde"], int)


def test_el_volcado_de_la_lista_vacia_es_una_lista_vacia():
    assert to_dict([]) == []


def test_pendiente_es_comparable_por_valor():
    """Se usa en los `assert ... == []` de arriba y en la igualdad de estados."""
    a = Pendiente(clave="dia_1", sesiones_desde=4, ultima_vez=INICIO, caducada=False)
    b = Pendiente(clave="dia_1", sesiones_desde=4, ultima_vez=INICIO, caducada=False)
    assert a == b
