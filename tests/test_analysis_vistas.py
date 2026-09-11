"""Vistas 1 y 2 de punta a punta, con datos sintéticos de resultado conocido.

Aquí no se vuelve a probar la estadística -eso está en `test_analysis_stats`- ni
la contabilidad de las series -eso está en `test_analysis_series`-. Lo que se
prueba es el montaje: que las dos piezas, ya probadas por separado, den el
resultado que se sabe que tienen que dar cuando se atornillan.

Se siembran relaciones EXACTAS -una inversa perfecta, un retardo limpio de dos
días- porque son las únicas cuyo resultado se puede escribir en el `assert` sin
haberlo sacado antes de ejecutar el código. Un test cuyo número esperado sale de
correr la implementación no prueba nada: certifica lo que hay, incluido el fallo.

Y se prueba lo que se pidió que no pasara nunca: que ninguna pareja desaparezca
por falta de datos. Con la base vacía tienen que salir las siete, cada una con su
motivo escrito.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.analysis import series as S
from app.analysis.concordancia import PARES, vista_concordancia, vista_desfase
from app.models import Activity, Base, Checkin, DailyMetrics

HOY = date(2026, 9, 11)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def dia(i: int) -> date:
    """El día i-ésimo contando hacia atrás desde hoy, en orden natural."""
    return HOY - timedelta(days=59 - i)


def sembrar(db, *, n=60, checkins=None, wellness=None):
    """Escribe n días seguidos con lo que devuelvan las dos funciones."""
    for i in range(n):
        d = dia(i)
        if checkins:
            campos = checkins(i)
            if campos:
                db.add(Checkin(date=d, **campos))
        if wellness:
            campos = wellness(i)
            if campos:
                db.add(DailyMetrics(date=d, fetch_status="ok", **campos))
    db.commit()


def par_de(vista, x, y):
    for p in vista["pares"]:
        if p["x"] == x and p["y"] == y:
            return p
    raise AssertionError(f"la pareja {x}/{y} no está en la vista")


def casilla(vista, x, y):
    for c in vista["rejilla"]:
        if c["x"] == x and c["y"] == y:
            return c
    raise AssertionError(f"la casilla {x}/{y} no está en la rejilla")


# ---------------------------------------------------------------------------
# Vista 1
# ---------------------------------------------------------------------------


def test_una_inversa_perfecta_sale_como_inversa_perfecta(db):
    """Cansancio y HRV movidos a la vez y al revés: r = -1 exacto.

    El signo negativo es aquí lo CORRECTO, no lo malo: más cansancio con menos
    variabilidad es que el reloj y él dicen lo mismo. Que la lectura diga
    "coincide" y no "va al revés" es la mitad del test.
    """
    sembrar(
        db,
        checkins=lambda i: {"fatigue": 1 + (i % 5)},
        wellness=lambda i: {"hrv": 70.0 - (i % 5) * 6},
    )
    v = vista_concordancia(db, dias=90, hoy=HOY)
    p = par_de(v, "fatigue", "hrv")

    assert p["r"] == -1.0
    assert p["n"] == 60
    assert p["na"] is None
    assert p["suficiente"] is True
    assert p["signo_esperado"] == -1
    assert "coincide" in p["lectura"]
    assert "REVÉS" not in p["lectura"]


def test_cuando_va_al_reves_lo_dice(db):
    """Más cansancio con MÁS variabilidad. Estadísticamente impecable, humanamente raro.

    Este es el caso que justifica mandar `signo_esperado` desde el servidor: un
    r de +1 entre cansancio y HRV es una correlación perfecta y una señal de que
    algo no cuadra, y las dos cosas a la vez no se leen en el número.
    """
    sembrar(
        db,
        checkins=lambda i: {"fatigue": 1 + (i % 5)},
        wellness=lambda i: {"hrv": 40.0 + (i % 5) * 6},
    )
    p = par_de(vista_concordancia(db, dias=90, hoy=HOY), "fatigue", "hrv")

    assert p["r"] == 1.0
    assert "AL REVÉS" in p["lectura"]


def test_el_cansancio_contra_la_fc_en_reposo_espera_el_signo_contrario(db):
    """Misma percepción, métrica que va al otro lado: ahora lo que coincide es +1.

    FC en reposo es la única métrica de Garmin donde alto es peor. Si el signo
    esperado se hubiera escrito a mano pareja por pareja, esta es la que se
    habría copiado mal, y saldría "va al revés" precisamente los días que mejor
    se percibe.
    """
    sembrar(
        db,
        checkins=lambda i: {"fatigue": 1 + (i % 5)},
        wellness=lambda i: {"rhr": 45.0 + (i % 5) * 2},
    )
    p = par_de(vista_concordancia(db, dias=90, hoy=HOY), "fatigue", "rhr")

    assert p["signo_esperado"] == 1
    assert p["r"] == 1.0
    assert "coincide" in p["lectura"]


def test_el_esfuerzo_percibido_contra_la_carga_sale_casi_identidad(db):
    """La pareja que sirve de alarma de incendios del desplazamiento.

    Se siembra la carga del día D y el RPE contestado el día D+1, que es
    exactamente como llegan en la vida real. Si el desplazamiento de
    `yesterday_rpe` se rompiera, esta correlación -que es casi una identidad- se
    hundiría, y sería el primer sitio donde se vería.
    """
    for i in range(40):
        d = dia(i)
        carga = 40.0 + (i % 5) * 45
        db.add(
            Activity(
                garmin_activity_id=1000 + i,
                date=d,
                is_cycling=True,
                training_load=carga,
            )
        )
        # Contestado al día siguiente, hablando de `d`.
        db.add(Checkin(date=d + timedelta(days=1), yesterday_rpe=1 + (i % 5)))
    db.commit()

    p = par_de(vista_concordancia(db, dias=90, hoy=HOY), "yesterday_rpe", "carga_bici")
    assert p["r"] == 1.0
    assert p["signo_esperado"] == 1
    assert "coincide" in p["lectura"]


def test_con_la_base_vacia_salen_las_siete_parejas_con_su_motivo(db):
    """Nada de medias tintas: ninguna pareja se esconde por falta de datos."""
    v = vista_concordancia(db, dias=90, hoy=HOY)

    assert len(v["pares"]) == len(PARES) == 7
    for p in v["pares"]:
        assert p["r"] is None
        assert p["p"] is None
        assert p["na"], f"{p['titulo']} sale sin motivo escrito"
        assert p["n"] == 0
        assert p["suficiente"] is False


def test_una_muestra_corta_se_calcula_y_se_marca_pero_no_se_esconde(db):
    """Cinco días dan un número. Lo que no dan es confianza, y eso se dice aparte."""
    sembrar(
        db,
        n=5,
        checkins=lambda i: {"fatigue": 1 + i},
        wellness=lambda i: {"hrv": 70.0 - i * 5},
    )
    p = par_de(vista_concordancia(db, dias=90, hoy=HOY), "fatigue", "hrv")

    assert p["n"] == 5
    assert p["r"] == -1.0
    assert p["na"] is None
    assert p["suficiente"] is False
    assert p["aviso"], "una muestra corta tiene que venir con su aviso"


def test_las_series_viajan_crudas_y_normalizadas(db):
    """El navegador pinta la escala y enseña el número. Las dos cosas o ninguna."""
    sembrar(
        db,
        n=10,
        checkins=lambda i: {"fatigue": 1 + (i % 5)},
        wellness=lambda i: {"hrv": 40.0 + i},
    )
    v = vista_concordancia(db, dias=90, hoy=HOY)
    hrv = next(s for s in v["series"] if s["clave"] == "hrv")

    assert hrv["etiqueta"] == "Variabilidad (HRV)"
    assert hrv["unidad"] == "ms"
    assert hrv["sentido"] == "alto_mejor"
    assert hrv["n"] == 10
    assert len(hrv["puntos"]) == 10

    primero, ultimo = hrv["puntos"][0], hrv["puntos"][-1]
    assert primero["valor"] == 40.0 and ultimo["valor"] == 49.0
    assert primero["escala"] < ultimo["escala"]
    assert 0.0 <= primero["escala"] <= 100.0


def test_estan_los_siete_deslizadores_y_las_cinco_metricas_aunque_esten_vacios(db):
    """Ningún gráfico oculto: la serie sin datos sale con n=0, no desaparece."""
    claves = {s["clave"] for s in vista_concordancia(db, dias=30, hoy=HOY)["series"]}
    assert set(S.SLIDERS) <= claves
    assert set(S.GARMIN) <= claves
    # Y las de entreno que participan en alguna pareja, que si no el gráfico de
    # esa pareja saldría con una sola línea.
    assert {"carga_bici", "volumen_fuerza"} <= claves


def test_la_ventana_y_la_cobertura_viajan_siempre(db):
    """Y dicen cosas distintas a propósito.

    `ventana` es lo que se PIDIÓ mirar; `cobertura` es lo que de verdad hay. Aquí
    se siembran tres días de julio y se piden cuarenta y cinco hasta hoy: la
    ventana llega hasta hoy y la cobertura se queda en julio. Esa diferencia es
    el dato -la HRV dejó de llegar hace dos meses-, y si la cobertura se
    calculara a partir de la ventana en vez de a partir de las filas, un silencio
    de dos meses se leería como falta de histórico.
    """
    sembrar(db, n=3, wellness=lambda i: {"hrv": 50.0})
    v = vista_concordancia(db, dias=45, hoy=HOY)

    assert v["ventana"] == {
        "desde": (HOY - timedelta(days=44)).isoformat(),
        "hasta": HOY.isoformat(),
        "dias": 45,
    }
    assert v["cobertura"]["garmin"] == {
        "desde": dia(0).isoformat(),
        "hasta": dia(2).isoformat(),
    }
    assert v["cobertura"]["checkin"] is None
    assert v["metodo"] == "spearman"


def test_una_serie_plana_da_motivo_y_no_un_cero(db):
    """Contestó 3 de cansancio sesenta días seguidos. Eso no es "no hay relación"."""
    sembrar(
        db,
        checkins=lambda i: {"fatigue": 3},
        wellness=lambda i: {"hrv": 40.0 + (i % 7)},
    )
    p = par_de(vista_concordancia(db, dias=90, hoy=HOY), "fatigue", "hrv")

    assert p["r"] is None
    assert p["na"] and "3" in p["na"]


# ---------------------------------------------------------------------------
# Vista 2
# ---------------------------------------------------------------------------


def test_un_retardo_sembrado_de_dos_dias_se_encuentra(db):
    """Lo que nota hoy es lo que el reloj marcará pasado mañana: desfase +2.

    Es la pregunta entera de la Vista 2. Se siembra el caso limpio -la HRV de
    D+2 es función exacta del cansancio de D- y el barrido tiene que señalar el
    +2 y no el 0, que es donde miraría cualquiera que no supiera que hay retardo.
    """
    for i in range(60):
        d = dia(i)
        db.add(Checkin(date=d, fatigue=1 + (i % 5)))
        # La HRV de d+2 responde al cansancio de d.
        db.add(
            DailyMetrics(
                date=d + timedelta(days=2), fetch_status="ok", hrv=70.0 - (i % 5) * 6
            )
        )
    db.commit()

    c = casilla(vista_desfase(db, dias=90, hoy=HOY + timedelta(days=2)), "fatigue", "hrv")
    assert c["mejor_desfase"] == 2
    assert c["por_desfase"]["2"]["r"] == -1.0
    assert "ADELANTA 2" in c["lectura"]


def test_sin_retardo_gana_el_cero(db):
    sembrar(
        db,
        checkins=lambda i: {"fatigue": 1 + (i % 5)},
        wellness=lambda i: {"hrv": 70.0 - (i % 5) * 6},
    )
    c = casilla(vista_desfase(db, dias=90, hoy=HOY), "fatigue", "hrv")

    assert c["mejor_desfase"] == 0
    assert "a la vez" in c["lectura"]


def test_la_rejilla_trae_las_treinta_y_cinco_casillas_aunque_no_haya_nada(db):
    """Siete deslizadores por cinco métricas, sin filtrar por las que salen bien.

    Filtrar dejaría en pantalla justo las que sobrevivieron al azar. Con treinta
    y cinco intentos, unas cuantas pasan cualquier filtro por casualidad, y
    esconder las otras treinta convierte la rejilla en una máquina de confirmar
    lo que uno ya creía.
    """
    v = vista_desfase(db, dias=90, hoy=HOY)

    assert len(v["rejilla"]) == len(S.SLIDERS) * len(S.GARMIN) == 35
    for c in v["rejilla"]:
        assert c["mejor_desfase"] is None
        assert c["na"], f"{c['x']}/{c['y']} sale sin motivo"


def test_el_barrido_entero_viaja_no_solo_el_ganador(db):
    """Un pico aislado es ruido; una curva que sube y baja es un retardo.

    Sin los vecinos no hay forma de distinguirlos, y el ganador solo sería un
    número sin contexto que invita a creérselo.
    """
    sembrar(
        db,
        checkins=lambda i: {"fatigue": 1 + (i % 5)},
        wellness=lambda i: {"hrv": 70.0 - (i % 5) * 6},
    )
    c = casilla(vista_desfase(db, dias=90, hoy=HOY), "fatigue", "hrv")

    assert sorted(int(k) for k in c["por_desfase"]) == [-3, -2, -1, 0, 1, 2, 3]
    for k, r in c["por_desfase"].items():
        assert r["n"] >= 0 and r["metodo"] == "spearman"


def test_los_extremos_del_barrido_no_pierden_pares_por_el_corte_de_la_consulta(db):
    """El margen: en -3 hacen falta métricas de tres días ANTES de la ventana.

    Si las métricas se leyeran con las mismas fechas que se piden, los desfases
    de los extremos tendrían menos pares que los del centro SIEMPRE, por
    construcción. Y como menos pares es menos correlación significativa, el
    barrido estaría sesgado hacia el cero: encontraría "van a la vez" en casos
    donde hay retardo de verdad.
    """
    sembrar(
        db,
        checkins=lambda i: {"fatigue": 1 + (i % 5)},
        wellness=lambda i: {"hrv": 50.0 + (i % 7)},
    )
    # Tres días de métricas ANTES del primer check-in, que es lo que el desfase
    # -3 necesita y la ventana pedida no incluye.
    for k in (1, 2, 3):
        db.add(DailyMetrics(date=dia(0) - timedelta(days=k), fetch_status="ok", hrv=44.0))
    db.commit()

    c = casilla(vista_desfase(db, dias=60, hoy=HOY), "fatigue", "hrv")
    assert c["por_desfase"]["-3"]["n"] == c["por_desfase"]["0"]["n"] == 60


def test_el_convenio_de_signos_viaja_escrito(db):
    v = vista_desfase(db, dias=30, hoy=HOY)
    assert v["rango_desfase"] == [-3, 3]
    assert "adelanta" in v["convenio"]
