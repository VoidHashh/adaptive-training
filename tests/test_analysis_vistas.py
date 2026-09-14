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
from app.analysis.concordancia import (
    AVISO_INTERNAS,
    MISMO_ORIGEN,
    PARES,
    pares_internos,
    vista_concordancia,
    vista_desfase,
)
from app.models import Activity, Base, Checkin, DailyMetrics
from tests.dobles import doble_de
from app.analysis.stats import Resultado

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


def interna_de(vista, x, y):
    """La casilla del reloj consigo mismo, buscada sin depender del orden.

    Por pareja no ordenada: `pares_internos` las genera en el orden de
    `S.GARMIN`, y un test que pidiera `hrv`/`rhr` en ese orden exacto se rompería
    al reordenar el diccionario sin que hubiera cambiado nada de fondo.
    """
    for c in vista["internas"]:
        if {c["x"], c["y"]} == {x, y}:
            return c
    raise AssertionError(f"la pareja interna {x}/{y} no está en la vista")


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
# Vista 1, el bloque interno: el reloj cruzado consigo mismo
# ---------------------------------------------------------------------------


def test_estan_las_diez_parejas_del_reloj_y_salen_de_las_cinco_metricas(db):
    """C(5,2) = 10, generadas y no escritas a mano.

    Se comprueba contra `S.GARMIN` y no contra un 10 escrito aquí: si mañana se
    añade una sexta métrica al reloj, este test tiene que pedir quince solo, y no
    seguir dando por buenas las diez de siempre mientras la métrica nueva se
    queda sin cruzar con nada.
    """
    v = vista_concordancia(db, dias=90, hoy=HOY)
    n = len(S.GARMIN)
    esperadas = n * (n - 1) // 2

    assert len(v["internas"]) == esperadas
    assert len(PARES_INTERNOS := pares_internos()) == esperadas
    # Ni una pareja repetida, ni una consigo misma.
    vistas = {frozenset((c["x"], c["y"])) for c in v["internas"]}
    assert len(vistas) == esperadas
    assert all(len(par) == 2 for par in vistas)
    assert all(p.x in S.GARMIN and p.y in S.GARMIN for p in PARES_INTERNOS)


def test_con_la_base_vacia_salen_las_diez_internas_con_su_motivo(db):
    """Nada de medias tintas, también aquí: ninguna se esconde por falta de datos."""
    v = vista_concordancia(db, dias=90, hoy=HOY)

    assert len(v["internas"]) == 10
    for c in v["internas"]:
        assert c["r"] is None
        assert c["na"], f"{c['titulo']} sale sin motivo escrito"
        assert c["n"] == 0
        assert c["lectura"] is None
        assert c["al_reves"] is None
    assert v["resumen_internas"]["calculadas"] == 0
    assert v["resumen_internas"]["significativas"] == 0


def test_el_signo_esperado_de_las_internas_sale_del_sentido_de_cada_metrica(db):
    """HRV contra FC en reposo espera -1, y las dos "alto es mejor" esperan +1.

    Es la pareja donde un signo escrito a mano se habría copiado mal: en el reloj
    solo hay una métrica donde alto es peor, y es la que aparece en cuatro de las
    diez parejas. Si el signo se invirtiera, cuatro tarjetas dirían "se
    contradicen" justo cuando el reloj está siendo coherente.
    """
    v = vista_concordancia(db, dias=90, hoy=HOY)
    por_clave = {frozenset((c["x"], c["y"])): c for c in v["internas"]}

    assert por_clave[frozenset(("hrv", "rhr"))]["signo_esperado"] == -1
    assert por_clave[frozenset(("rhr", "sleep_score"))]["signo_esperado"] == -1
    assert por_clave[frozenset(("hrv", "sleep_score"))]["signo_esperado"] == 1
    assert por_clave[frozenset(("sleep_min", "body_battery"))]["signo_esperado"] == 1
    # Ninguna sale sin signo: eso solo pasaría con una métrica neutra, y en el
    # reloj no hay ninguna.
    assert all(c["signo_esperado"] in (1, -1) for c in v["internas"])


def test_una_hrv_alta_con_pulsaciones_bajas_es_coincidir_aunque_la_r_sea_negativa(db):
    """El caso que obliga a que la frase no hable del signo.

    Se siembra el buen día perfecto -variabilidad arriba, reposo abajo- y sale
    r = -1. Ese menos uno es el reloj siendo COHERENTE, y una frase que dijera
    "van en sentidos opuestos" al lado de un -1 correcto sería exactamente al
    revés de la verdad.
    """
    sembrar(
        db,
        wellness=lambda i: {"hrv": 40.0 + (i % 5) * 6, "rhr": 60.0 - (i % 5) * 2},
    )
    c = interna_de(vista_concordancia(db, dias=90, hoy=HOY), "hrv", "rhr")

    assert c["r"] == -1.0
    assert c["signo_esperado"] == -1
    assert c["al_reves"] is False
    assert "coinciden" in c["lectura"]
    assert "contradicen" not in c["lectura"]


def test_cuando_dos_metricas_del_reloj_se_contradicen_lo_dice(db):
    """Variabilidad y pulsaciones subiendo juntas: r = +1 y algo no cuadra.

    No es un caso de laboratorio. Es lo que se vería si el reloj estuviera
    leyendo mal las noches, y es la única forma de enterarse sin abrir la app de
    Garmin y mirar noche por noche.
    """
    sembrar(
        db,
        wellness=lambda i: {"hrv": 40.0 + (i % 5) * 6, "rhr": 45.0 + (i % 5) * 2},
    )
    c = interna_de(vista_concordancia(db, dias=90, hoy=HOY), "hrv", "rhr")

    assert c["r"] == 1.0
    assert c["al_reves"] is True
    assert "contradicen" in c["lectura"]
    assert vista_concordancia(db, dias=90, hoy=HOY)["resumen_internas"][
        "en_sentido_contrario"
    ] == 1


def test_un_signo_contrario_pero_diminuto_no_cuenta_como_contradiccion(db):
    """Un r de casi cero al otro lado es cero, no un hallazgo al revés.

    `al_reves` tiene tres valores por esto. Si un -0.02 donde se esperaba +1
    contara como contradicción, el resumen de la pantalla diría "3 de 10 van en
    sentido contrario" cualquier día con ruido, y esa frase se lee como que el
    reloj está roto.
    """
    from app.analysis.concordancia import Par, _al_reves

    @doble_de(Resultado)
    class Res:
        r = -0.04
        suficiente = True

    par = Par("hrv", "sleep_min", "da igual")
    assert par.signo_esperado == 1
    assert _al_reves(par, Res()) is None


def test_las_internas_se_corrigen_y_las_de_percepcion_no(db):
    """Dos bloques en la misma pantalla con contratos distintos, y a propósito.

    Las siete de percepción son hipótesis declaradas de antemano y llegan con
    `significativa: None` -no hay corrección que aguantar-. Las diez internas son
    la rejilla completa de lo que se puede cruzar y llegan con `True` o `False`.
    Que los dos bloques convivan es lo que hace que el tercer estado de la barra
    sea una distinción real y no un comentario en el código.
    """
    sembrar(
        db,
        checkins=lambda i: {"fatigue": 1 + (i % 5)},
        wellness=lambda i: {
            "hrv": 70.0 - (i % 5) * 6,
            "rhr": 45.0 + (i % 5) * 2,
            "sleep_min": 400.0 + (i % 7) * 11,
        },
    )
    v = vista_concordancia(db, dias=90, hoy=HOY)

    for p in v["pares"]:
        assert p["significativa"] is None, f"{p['titulo']} no debería corregirse"
        assert p["p_corregida"] is None

    calculadas = [c for c in v["internas"] if c["r"] is not None]
    assert calculadas, "el sembrado tenía que dar internas calculables"
    for c in calculadas:
        assert c["significativa"] in (True, False)
        assert c["p_corregida"] is not None
        # La corrección solo puede subir la p, nunca bajarla.
        assert c["p_corregida"] >= c["p"]


def test_la_correccion_de_las_internas_no_depende_de_los_checkins(db):
    """Diez casillas en la tanda, siempre diez, haya o no mañanas contestadas.

    Si las diecisiete se corrigieran juntas, la dureza de la corrección de las
    internas -que solo dependen del reloj- cambiaría según cuántos check-ins
    llevara contestados. Un mismo histórico de Garmin daría dos veredictos
    distintos, y el motivo no aparecería por ninguna parte.
    """
    wellness = lambda i: {  # noqa: E731
        "hrv": 70.0 - (i % 5) * 6,
        "rhr": 45.0 + (i % 5) * 2,
        "sleep_min": 400.0 + (i % 7) * 11,
        "sleep_score": 60.0 + (i % 6) * 5,
        "body_battery": 30.0 + (i % 8) * 6,
    }
    sembrar(db, wellness=wellness)
    sin_checkins = vista_concordancia(db, dias=90, hoy=HOY)["internas"]

    for i in range(60):
        db.add(Checkin(date=dia(i), fatigue=1 + (i % 5), mood=1 + (i % 4)))
    db.commit()
    con_checkins = vista_concordancia(db, dias=90, hoy=HOY)["internas"]

    antes = {(c["x"], c["y"]): c["p_corregida"] for c in sin_checkins}
    despues = {(c["x"], c["y"]): c["p_corregida"] for c in con_checkins}
    assert antes == despues


def test_las_parejas_que_el_reloj_calcula_de_las_otras_llevan_su_aviso(db):
    """Ocho de diez llevan aviso, y las dos que no son las que miden cosas distintas.

    Sin el aviso, el 0.53 entre los minutos dormidos y la nota de sueño se lee
    como un hallazgo sobre el cuerpo. Es la fórmula de Garmin: la nota se
    construye CON los minutos. Que el aviso viaje pegado a la casilla, y no en un
    párrafo suelto arriba, es lo que hace que una tarjeta leída sola no engañe.
    """
    v = vista_concordancia(db, dias=90, hoy=HOY)
    con_aviso = {
        frozenset((c["x"], c["y"])) for c in v["internas"] if c["mismo_origen"]
    }
    sin_aviso = {
        frozenset((c["x"], c["y"]))
        for c in v["internas"]
        if c["mismo_origen"] is None
    }

    assert sin_aviso == {
        frozenset(("hrv", "sleep_min")),
        frozenset(("rhr", "sleep_min")),
    }
    assert len(con_aviso) == 8
    assert con_aviso == set(MISMO_ORIGEN)
    assert v["resumen_internas"]["comparten_origen"]["parejas"] == 8
    assert v["resumen_internas"]["independientes"]["parejas"] == 2


def test_los_avisos_de_origen_hablan_de_parejas_que_existen(db):
    """Una clave mal escrita en `MISMO_ORIGEN` sería un aviso que no sale nunca.

    Y no daría error: la pareja se pintaría sin advertencia, con su correlación
    alta y con toda la pinta de ser un descubrimiento. Es el fallo callado de
    siempre, esta vez en forma de nota que no aparece.
    """
    posibles = {frozenset((p.x, p.y)) for p in pares_internos()}
    for clave in MISMO_ORIGEN:
        assert clave in posibles, f"{sorted(clave)} no es ninguna pareja del reloj"


def test_el_resumen_de_las_internas_cuadra_con_las_casillas(db):
    """El contador de la cabecera no puede decir algo distinto de las tarjetas.

    Se pinta arriba del todo y es lo único que muchas mañanas se va a leer. Si
    dijera "8 de 10" mientras abajo hay nueve barras sólidas, la pantalla estaría
    discutiendo consigo misma.
    """
    sembrar(
        db,
        wellness=lambda i: {
            "hrv": 70.0 - (i % 5) * 6,
            "rhr": 45.0 + (i % 5) * 2,
            "sleep_min": 400.0 + (i % 7) * 11,
            "sleep_score": 60.0 + (i % 6) * 5,
            "body_battery": 30.0 + (i % 8) * 6,
        },
    )
    v = vista_concordancia(db, dias=90, hoy=HOY)
    r = v["resumen_internas"]
    cas = v["internas"]

    assert r["parejas"] == len(cas)
    assert r["calculadas"] == sum(1 for c in cas if c["r"] is not None)
    assert r["significativas"] == sum(1 for c in cas if c["significativa"])
    assert r["en_sentido_contrario"] == sum(1 for c in cas if c["al_reves"])
    assert (
        r["comparten_origen"]["parejas"] + r["independientes"]["parejas"]
        == r["parejas"]
    )
    assert (
        r["comparten_origen"]["significativas"]
        + r["independientes"]["significativas"]
        == r["significativas"]
    )


def test_el_bloque_interno_no_hace_falta_ni_un_checkin(db):
    """La razón práctica de que exista, probada: cero mañanas contestadas.

    El día que se enciende el sistema esta pantalla está entera en N/A -las siete
    parejas necesitan check-ins que no hay- y estas diez casillas son lo único
    que dice algo. Si un día empezaran a depender del check-in, la pantalla
    volvería a estar vacía el primer día y nadie se enteraría hasta encenderla.
    """
    sembrar(
        db,
        wellness=lambda i: {"hrv": 70.0 - (i % 5) * 6, "rhr": 45.0 + (i % 5) * 2},
    )
    v = vista_concordancia(db, dias=90, hoy=HOY)

    assert v["cobertura"]["checkin"] is None
    assert all(p["r"] is None for p in v["pares"]), "sin check-ins no hay percepción"
    assert interna_de(v, "hrv", "rhr")["r"] is not None
    assert interna_de(v, "hrv", "rhr")["n"] == 60


def test_el_aviso_del_bloque_viaja_escrito_y_sin_cifras_dentro(db):
    """La PWA pinta la advertencia; no la lleva escrita en JavaScript.

    Y la advertencia no puede llevar cuentas escritas a mano. Un "ocho de las
    diez parejas" dentro del texto sobrevive intacto a que se añada una sexta
    métrica al reloj: la pantalla pasaría a tener quince casillas y el párrafo
    de arriba seguiría diciendo diez, sin dar ningún error. Las cifras las cuenta
    `resumen_internas` sobre las casillas que hay.
    """
    v = vista_concordancia(db, dias=30, hoy=HOY)
    assert v["aviso_internas"] == AVISO_INTERNAS

    numeros = ("ocho", "nueve", "diez", "quince", "8", "10", "15")
    escrito = v["aviso_internas"].lower()
    for n in numeros:
        assert n not in escrito, (
            f"el aviso lleva {n!r} escrito a mano: se queda viejo en cuanto "
            f"cambie `S.GARMIN` y nadie se entera"
        )


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
