"""Que los números salgan bien, comprobados contra resultados que se saben.

Un módulo de estadística es de los pocos sitios donde se puede probar de verdad:
hay datos cuyo resultado exacto se conoce de antemano, y contra esos se compara.
Lo demás -"parece razonable"- no es una prueba, es una impresión.

Aquí se comprueban tres familias de cosas y la tercera es la importante:

  1. Que las fórmulas dan lo que dan. Correlación perfecta = 1, monótona no
     lineal = 1 en Spearman y menos en Pearson, valores calculados a mano.
  2. Que el emparejamiento por fecha no desliza series. Un `zip` sobre dos listas
     de valores correlaciona el cansancio del martes con el HRV del jueves y no
     chirría en ninguna parte: sale un número perfectamente calculado de dos
     cosas que no se corresponden.
  3. Que lo que NO se puede calcular sale como motivo escrito y nunca como cero.
     Es el requisito explícito: un `0.0` afirma "no hay relación", que es una
     conclusión, y afirmarla sin datos es peor que no decir nada.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from app.analysis import (
    N_MINIMO_FIABLE,
    correlacion,
    emparejar,
    mejor_desfase,
    percentil,
    percentil_de,
    rangos,
)
from app.analysis.stats import _beta_incompleta, _p_valor

D0 = date(2026, 3, 1)


def dias(n: int, desde: date = D0) -> list[date]:
    return [desde + timedelta(days=i) for i in range(n)]


def serie(vals, desde: date = D0) -> dict[date, float | None]:
    return dict(zip(dias(len(vals), desde), vals))


def pares(xs, ys, desde: date = D0):
    return emparejar(serie(xs, desde), serie(ys, desde))


# ---------------------------------------------------------------------------
# Resultados que se saben de antemano
# ---------------------------------------------------------------------------


def test_una_recta_perfecta_da_uno():
    x = list(range(30))
    y = [3 * v + 7 for v in x]
    for metodo in ("pearson", "spearman"):
        r = correlacion(pares(x, y), metodo=metodo)
        assert r.r == pytest.approx(1.0), metodo


def test_una_recta_descendente_da_menos_uno():
    x = list(range(30))
    y = [100 - 2 * v for v in x]
    assert correlacion(pares(x, y), metodo="pearson").r == pytest.approx(-1.0)


def test_pearson_a_mano():
    """Cinco pares con el resultado calculado aparte.

    x = 1..5, y = 2,4,5,4,5.  media x=3, media y=4.
    Sxy = (-2)(-2)+(-1)(0)+(0)(1)+(1)(0)+(2)(1) = 4+0+0+0+2 = 6
    Sxx = 4+1+0+1+4 = 10 ; Syy = 4+0+1+0+1 = 6
    r = 6 / sqrt(60) = 0.7745966692...
    """
    r = correlacion(pares([1, 2, 3, 4, 5], [2, 4, 5, 4, 5]), metodo="pearson")
    assert r.r == pytest.approx(6 / math.sqrt(60), abs=1e-12)
    assert r.n == 5


def test_spearman_sube_donde_pearson_se_queda_corto():
    """Una relación monótona pero curva: Spearman la ve entera, Pearson no.

    Es exactamente el caso de los deslizadores. Un 1-5 de cansancio no está en
    la misma escala que los milisegundos de HRV, y forzar una recta entre los
    dos subestima una relación que sí existe.
    """
    x = list(range(1, 21))
    y = [v ** 3 for v in x]
    assert correlacion(pares(x, y), metodo="spearman").r == pytest.approx(1.0)
    assert correlacion(pares(x, y), metodo="pearson").r < 0.95


def test_los_empates_se_promedian():
    """1,2,2,3 tiene rangos 1, 2.5, 2.5, 4 y no 1,2,3,4.

    Con deslizadores enteros de 1 a 5 sobre seis meses hay empates por docenas.
    Repartirlos por orden de llegada inventa un orden entre días idénticos: mete
    ruido con estructura, que no se promedia hasta cero.
    """
    assert rangos([1, 2, 2, 3]) == [1.0, 2.5, 2.5, 4.0]
    assert rangos([5, 5, 5, 5]) == [2.5, 2.5, 2.5, 2.5]
    assert rangos([3, 1, 2]) == [3.0, 1.0, 2.0]


def test_dos_series_independientes_no_dan_correlacion_alta():
    """Control negativo: sin relación, |r| tiene que quedarse pequeño."""
    x = [((i * 37) % 101) for i in range(120)]
    y = [((i * 53) % 97) for i in range(120)]
    r = correlacion(emparejar(serie(x), serie(y)), metodo="spearman")
    assert abs(r.r) < 0.25, f"r={r.r} sobre ruido"


# ---------------------------------------------------------------------------
# Lo que no se puede calcular sale con motivo, nunca con cero
# ---------------------------------------------------------------------------


def test_una_serie_plana_no_da_cero_da_motivo():
    """El requisito central. Un `0.0` aquí sería una conclusión inventada.

    "No hay relación" y "no se puede saber si hay relación" son afirmaciones
    distintas, y la primera cierra la pregunta. Si el usuario puso un 3 de
    cansancio los ciento ochenta días, lo que hay que decirle es eso, no que su
    cansancio no tiene nada que ver con su HRV.

    El motivo dice **3**, que es el número que él puso, y no el 15.5 en que lo
    convierten los rangos de Spearman. Un motivo que manda a buscar un valor que
    no existe en ninguna parte es peor que no dar motivo.
    """
    r = correlacion(pares([3] * 30, list(range(30))))
    assert r.r is None, "una constante no puede producir un número"
    assert r.p is None
    assert r.na and "siempre 3" in r.na, (
        f"el motivo tiene que hablar del dato original, no del rango: {r.na}"
    )
    assert r.n == 30, "el n se dice igual: se miraron 30 días"
    assert r.aviso == r.na


def test_el_motivo_no_cambia_entre_pearson_y_spearman():
    """El valor que se nombra es el del usuario, lo transforme quien lo transforme."""
    for metodo in ("pearson", "spearman"):
        r = correlacion(pares([7] * 25, list(range(25))), metodo=metodo)
        assert "siempre 7" in r.na, f"{metodo}: {r.na}"


def test_se_dice_CUAL_de_las_dos_series_es_la_plana():
    """"No varía" a secas obliga a ir a mirar la base de datos."""
    assert "primera" in correlacion(pares([2] * 10, list(range(10)))).na
    assert "segunda" in correlacion(pares(list(range(10)), [9] * 10)).na
    assert "ninguna de las dos" in correlacion(pares([1] * 10, [4] * 10)).na


def test_con_dos_dias_no_se_calcula_y_se_dice_cuantos_faltan():
    """Con dos puntos la recta pasa por los dos: r sale 1 siempre, y es mentira."""
    r = correlacion(pares([1, 2], [5, 9]))
    assert r.r is None
    assert "falta 1" in r.na and "2 días" in r.na


def test_sin_ningun_dia_tambien_se_explica():
    r = correlacion(emparejar({}, {}))
    assert r.n == 0 and r.r is None
    assert r.na and "0 días" in r.na
    assert r.desde is None and r.hasta is None


def test_el_motivo_distingue_no_tengo_historico_de_me_faltan_datos():
    """Un n bajo tiene dos causas y solo una se arregla.

    "Llevo dos semanas" se arregla esperando. "De la mitad de los días falta el
    HRV" se arregla mirando por qué, y para eso hay que saber que está pasando.
    """
    xs = {d: 1.0 * i for i, d in enumerate(dias(10))}
    ys = {d: (2.0 * i if i % 5 == 0 else None) for i, d in enumerate(dias(10))}
    r = correlacion(emparejar(xs, ys))
    assert r.n == 2 and r.descartados == 8
    assert "se descartaron 8 por huecos" in r.na


# ---------------------------------------------------------------------------
# La muestra insuficiente se marca, no se esconde
# ---------------------------------------------------------------------------


def test_con_pocos_dias_el_numero_se_da_igual_pero_avisado():
    """Lo que se pidió explícitamente: marcarla sin ocultarla."""
    x = list(range(8))
    r = correlacion(pares(x, [2 * v for v in x]))
    assert r.r == pytest.approx(1.0), "el número se calcula y se entrega"
    assert r.suficiente is False
    assert "muestra insuficiente" in r.aviso and "8 pares" in r.aviso
    assert str(N_MINIMO_FIABLE) in r.aviso, "y se dice cuántos harían falta"


def test_con_bastantes_dias_no_hay_aviso():
    x = list(range(N_MINIMO_FIABLE + 5))
    r = correlacion(pares(x, [2 * v + 1 for v in x]))
    assert r.suficiente is True and r.aviso is None


def test_el_dict_para_el_navegador_lleva_todo_lo_que_hace_falta():
    """La PWA pinta esto y no calcula nada, así que tiene que venir todo."""
    x = list(range(25))
    d = correlacion(pares(x, [3 * v for v in x])).como_dict()
    assert d["n"] == 25 and d["r"] == 1.0 and d["na"] is None
    assert d["suficiente"] is True and d["metodo"] == "spearman"
    assert d["desde"] == D0.isoformat()
    assert d["hasta"] == (D0 + timedelta(days=24)).isoformat()
    assert d["p"] is not None and d["descartados"] == 0


def test_la_correccion_viaja_siempre_aunque_no_se_haya_corregido():
    """`significativa` tiene tres valores, y el tercero no es la ausencia.

    `null` significa "sobre esta vista no se pasó ninguna corrección por
    comparaciones múltiples", que es un hecho de la vista y no del número. No
    mandar la clave significa lo mismo para un humano y otra cosa muy distinta
    para JavaScript: la PWA dibuja la barra hueca con `c.significativa !== false`
    y, sin la clave, `undefined !== false` da `true` y la barra sale sólida.

    Funcionaba. Ese es el problema: funcionaba por accidente, nadie lo decidió, y
    el día que alguien invierta la comprobación la vista entera cambia de
    significado sin dar un solo error. Concordancia y desfase se apoyaban en ese
    accidente hasta que el andamio de render lo cazó.
    """
    d = correlacion(pares(list(range(25)), [3 * v for v in range(25)])).como_dict()

    assert "significativa" in d and d["significativa"] is None
    assert "p_corregida" in d and d["p_corregida"] is None


def test_la_ventana_es_la_de_los_pares_que_entraron_y_no_la_pedida():
    """Si los cinco primeros días no tienen HRV, la ventana empieza el sexto.

    Decir "del 1 de marzo al 9 de septiembre" cuando los datos empiezan en mayo
    es una ventana falsa, y con ella el `n` parece un agujero enorme en vez de
    un histórico corto.
    """
    xs = {d: float(i) for i, d in enumerate(dias(10))}
    ys = {d: (None if i < 4 else float(i)) for i, d in enumerate(dias(10))}
    r = correlacion(emparejar(xs, ys))
    assert r.desde == D0 + timedelta(days=4)
    assert r.hasta == D0 + timedelta(days=9)
    assert r.n == 6


def test_un_metodo_inventado_revienta_en_vez_de_elegir_uno():
    with pytest.raises(ValueError, match="método desconocido"):
        correlacion(pares([1, 2, 3], [1, 2, 3]), metodo="kendall")


# ---------------------------------------------------------------------------
# Emparejar por fecha
# ---------------------------------------------------------------------------


def test_un_hueco_descarta_el_par_entero_y_no_desliza_la_serie():
    """El fallo que este test existe para impedir.

    Si el día 3 no tiene HRV y se comprime la lista en vez de descartar el par,
    a partir de ahí el cansancio del día 4 se compara con el HRV del día 5 y
    todo lo demás va corrido. La correlación sale perfectamente calculada y es
    de dos cosas que no se corresponden.
    """
    xs = {d: float(i) for i, d in enumerate(dias(6))}
    ys = dict(xs)
    ys[D0 + timedelta(days=3)] = None

    p = emparejar(xs, ys)
    assert p.dias == [D0 + timedelta(days=i) for i in (0, 1, 2, 4, 5)]
    assert p.x == p.y, "cada día sigue emparejado consigo mismo"
    assert p.descartados == 1


def test_un_dia_que_solo_esta_en_una_serie_no_entra():
    xs = {D0: 1.0, D0 + timedelta(days=1): 2.0, D0 + timedelta(days=9): 3.0}
    ys = {D0: 5.0, D0 + timedelta(days=1): 6.0}
    p = emparejar(xs, ys)
    assert len(p) == 2 and p.descartados == 1


def test_los_dias_salen_ordenados_aunque_el_diccionario_no_lo_este():
    xs = {D0 + timedelta(days=i): float(i) for i in (3, 0, 2, 1)}
    p = emparejar(xs, dict(xs))
    assert p.dias == dias(4)
    assert p.x == [0.0, 1.0, 2.0, 3.0]


# ---------------------------------------------------------------------------
# El desfase
# ---------------------------------------------------------------------------


def test_una_senal_retrasada_dos_dias_se_encuentra_en_el_desfase_dos():
    """`y` es `x` movida dos días hacia el futuro: el pico tiene que estar en +2.

    Con el convenio de `mejor_desfase`, +2 significa que lo que él anota hoy se
    parece a lo que el reloj marcará dentro de dos días: su percepción se
    adelanta.
    """
    base = [1, 5, 2, 8, 3, 9, 4, 7, 2, 6, 1, 8, 3, 5, 9, 2, 7, 4, 6, 1, 8, 5, 3, 9]
    xs = serie(base)
    ys = {D0 + timedelta(days=i + 2): float(v) for i, v in enumerate(base)}

    d = mejor_desfase(xs, ys)
    assert d.mejor == 2, {k: v.r for k, v in d.por_desfase.items()}
    assert d.resultado.r == pytest.approx(1.0)
    assert "ADELANTA 2" in d.lectura()


def test_una_senal_adelantada_se_encuentra_en_el_desfase_negativo():
    base = [1, 5, 2, 8, 3, 9, 4, 7, 2, 6, 1, 8, 3, 5, 9, 2, 7, 4, 6, 1, 8, 5, 3, 9]
    xs = {D0 + timedelta(days=i + 3): float(v) for i, v in enumerate(base)}
    ys = serie(base)

    d = mejor_desfase(xs, ys)
    assert d.mejor == -3
    assert "DETRÁS 3" in d.lectura()


def test_sin_retardo_gana_el_cero():
    x = [1, 5, 2, 8, 3, 9, 4, 7, 2, 6, 1, 8, 3, 5, 9, 2, 7, 4, 6, 1]
    d = mejor_desfase(serie(x), serie(x))
    assert d.mejor == 0
    assert "a la vez" in d.lectura()


def test_gana_la_relacion_mas_FUERTE_aunque_sea_negativa():
    """Quedarse con el `r` más grande confunde la dirección con la fuerza.

    Una correlación de -0.9 es una relación más fuerte que una de +0.3, y en
    estas parejas las negativas son las esperables: más cansancio, menos HRV.
    Un criterio que prefiriera las positivas se perdería justo las que importan.
    """
    base = [1, 5, 2, 8, 3, 9, 4, 7, 2, 6, 1, 8, 3, 5, 9, 2, 7, 4, 6, 1, 8, 5]
    xs = serie(base)
    ys = {D0 + timedelta(days=i + 1): float(-v) for i, v in enumerate(base)}

    d = mejor_desfase(xs, ys)
    assert d.mejor == 1
    assert d.resultado.r == pytest.approx(-1.0)


def test_el_barrido_entero_se_devuelve_y_no_solo_el_ganador():
    """Un pico aislado es ruido; una curva con hombros es una relación.

    Sin los vecinos no se puede distinguir, y distinguirlo es la mitad de lo que
    la vista 2 sirve para decidir.
    """
    x = list(range(30))
    d = mejor_desfase(serie(x), serie([2 * v for v in x]))
    assert set(d.por_desfase) == set(range(-3, 4))
    assert all(isinstance(v.n, int) for v in d.por_desfase.values())
    assert set(d.como_dict()["por_desfase"]) == {"-3", "-2", "-1", "0", "1", "2", "3"}


def test_si_ningun_desfase_se_puede_calcular_se_dice_una_sola_vez():
    d = mejor_desfase(serie([2] * 12), serie(list(range(12))))
    assert d.mejor is None and d.resultado is None
    assert d.na and "siempre 2" in d.na
    assert d.lectura() is None
    assert d.como_dict()["mejor_desfase"] is None


def test_el_desfase_recorta_la_ventana_y_el_n_lo_refleja():
    """Correlacionar con retardo cuesta días en los extremos, y se ve en el n."""
    x = list(range(20))
    d = mejor_desfase(serie(x), serie(x))
    assert d.por_desfase[0].n == 20
    assert d.por_desfase[3].n == 17
    assert d.por_desfase[-3].n == 17


# ---------------------------------------------------------------------------
# El valor p
# ---------------------------------------------------------------------------


def test_la_misma_correlacion_con_mas_dias_es_mas_creible():
    """Es lo que el p aporta y el r no puede: r=0.5 con n=8 no es r=0.5 con n=150."""
    poco = _p_valor(0.5, 8)
    mucho = _p_valor(0.5, 150)
    assert poco > 0.1, poco
    assert mucho < 0.001, mucho


def test_valores_p_contra_referencia():
    """Contrastados contra la integral de la t, calculada aparte.

    Los valores no salen de la memoria ni de una tabla redondeada: se obtuvieron
    integrando numéricamente (Simpson, cuatro millones de puntos) la densidad de
    la t de Student, que es un camino completamente distinto del de la beta
    incompleta que usa el módulo. Dos implementaciones independientes que
    coinciden en diez decimales es la única garantía disponible sin scipy.

        r=0.5, n=12 -> t=1.825742, gl=10 -> p = 0.0978546143
        r=0.8, n=10 -> t=3.771236, gl=8  -> p = 0.0054560000
    """
    assert _p_valor(0.5, 12) == pytest.approx(0.0978546143, abs=1e-9)
    assert _p_valor(0.8, 10) == pytest.approx(0.0054560000, abs=1e-8)
    assert _p_valor(-0.8, 10) == pytest.approx(0.0054560000, abs=1e-8), (
        "el signo indica la dirección, no la credibilidad"
    )


def test_sin_relacion_el_p_ronda_uno():
    assert _p_valor(0.0, 50) == pytest.approx(1.0, abs=1e-9)


def test_la_beta_incompleta_cumple_sus_identidades():
    """Sin scipy, la única garantía es comprobar las propiedades que la definen."""
    assert _beta_incompleta(0.0, 2, 3) == 0.0
    assert _beta_incompleta(1.0, 2, 3) == 1.0
    for a, b, x in ((2, 3, 0.3), (0.5, 4, 0.7), (10, 10, 0.45), (1, 1, 0.25)):
        assert _beta_incompleta(x, a, b) + _beta_incompleta(1 - x, b, a) == pytest.approx(1.0)
    # I_x(1,1) = x, que es la uniforme.
    assert _beta_incompleta(0.25, 1, 1) == pytest.approx(0.25)
    # Simétrica en el centro.
    assert _beta_incompleta(0.5, 3, 3) == pytest.approx(0.5)


def test_una_correlacion_perfecta_no_revienta_el_p():
    """r=1 mete un cero en el denominador de la t si nadie lo sujeta."""
    x = list(range(25))
    r = correlacion(pares(x, [2 * v for v in x]), metodo="pearson")
    assert r.r == pytest.approx(1.0)
    assert r.p == 0.0


# ---------------------------------------------------------------------------
# Percentiles
# ---------------------------------------------------------------------------


def test_el_percentil_de_un_valor_cuenta_la_mitad_de_los_empates():
    """Con deslizadores enteros el empate es la norma, no la excepción.

    Contando solo los estrictamente menores, TODOS los 1 caerían en el percentil
    0 y todos los 5 en el 80: la escala entera se desplaza hacia abajo y un día
    normal parece malo. En la vista 5 eso es justo el error que no se puede
    cometer, porque el sesgo iría en la misma dirección que la distorsión que la
    vista existe para contrapesar.
    """
    muestra = [1, 1, 2, 2, 2, 3, 3, 4, 5, 5]
    # Dos por debajo, tres iguales: (2 + 1.5) / 10 = 35%
    assert percentil_de(2, muestra) == pytest.approx(35.0)
    assert percentil_de(1, muestra) == pytest.approx(10.0)
    assert percentil_de(5, muestra) == pytest.approx(90.0)


def test_el_percentil_de_una_muestra_vacia_es_None_y_no_cero():
    assert percentil_de(3, []) is None
    assert percentil([], 50) is None


def test_percentiles_conocidos():
    assert percentil([1, 2, 3, 4, 5], 50) == 3.0
    assert percentil([1, 2, 3, 4], 50) == 2.5
    assert percentil([10, 20, 30, 40, 50], 25) == 20.0
    assert percentil([10, 20, 30, 40, 50], 0) == 10.0
    assert percentil([10, 20, 30, 40, 50], 100) == 50.0
    assert percentil([7], 42) == 7.0


def test_el_percentil_se_sujeta_al_rango():
    assert percentil([1, 2, 3], 150) == 3.0
    assert percentil([1, 2, 3], -10) == 1.0


def test_una_p_positiva_no_viaja_nunca_como_cero_exacto():
    """`round(3e-12, 5)` da `0.0`, y `p = 0` se lee como "imposible por azar".

    Ningún contraste dice eso jamás. Dice "más improbable de lo que sé medir con
    la precisión con la que lo cuento", que no es lo mismo ni de lejos. Es el
    fallo de siempre con otro disfraz: el valor que se lee no es el que se
    calculó, y la diferencia la mete la presentación sin avisar.

    Sale de verdad: un Spearman de 173 días con r = 0,4 -que es lo que tiene
    este histórico contra la HRV- da p del orden de 1e-12.
    """
    from app.analysis.stats import P_MINIMA, redondear_p

    assert redondear_p(3e-12) == P_MINIMA
    assert redondear_p(1e-300) == P_MINIMA
    assert redondear_p(None) is None

    # Lo que ya se ve con cinco decimales pasa tal cual.
    assert redondear_p(0.04321) == 0.04321
    assert redondear_p(0.5) == 0.5
    # Y el suelo no sube nada que ya estuviera por encima.
    assert redondear_p(0.000123) == 0.00012


def test_el_suelo_de_la_p_no_toca_la_significacion(db_no_hace_falta=None):
    """El suelo es de PRESENTACIÓN: no puede cambiar quién pasa el corte.

    `corregir_tanda` decide con la p que le llega y redondea después. Si el
    orden de las casillas dependiera del redondeo, el suelo estaría moviendo
    resultados en vez de escribirlos.
    """
    from app.analysis.stats import corregir_tanda

    casillas = [
        {"p": 3e-12},
        {"p": 0.0004},
        {"p": 0.5},
        {"p": 0.9},
    ]
    corregir_tanda([casillas])
    assert [c["significativa"] for c in casillas] == [True, True, False, False]
    assert casillas[0]["p_corregida"] > 0.0
