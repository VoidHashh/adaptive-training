"""La portada, sembrada para que cada frase se pueda escribir antes de ejecutarla.

Lo que se prueba aquí no es la estadística -eso es `test_analysis_stats`- ni el
montaje de la rejilla -eso es `test_analysis_impacto`-. Es la capa que convierte
números en castellano, que es justo la que no da ningún error cuando se equivoca:
una frase mal construida se sirve con un 200, se pinta perfecta y se lee como si
fuera verdad.

Los tres fallos que esta capa cometió de verdad el 2026-09-13, cada uno con su
test, porque los tres salieron de código que funcionaba:

  - «las salidas largas te SUBE el pulso», con el verbo en singular y el sujeto
    en plural;
  - «lo mismo sale con las salidas medias» dicho de una compañera cuya casilla
    más fuerte apunta al REVÉS;
  - `p = 0.0` servido como si un contraste pudiera ser imposible por azar.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.analysis.portada import (
    DIAS_RECIENTES,
    Grupo,
    Lenguaje,
    grupo_de,
    hallazgos,
    lenguaje_de,
    lo_que_falta,
    que_ha_cambiado,
    vista_portada,
)
from app.models import Activity, Base, Checkin, DailyMetrics, Decision, WorkoutLog

HOY = date(2026, 9, 11)
N = 120


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def dia(i: int) -> date:
    return HOY - timedelta(days=N - 1 - i)


# ---------------------------------------------------------------------------
# El lenguaje: bandas y grupos
# ---------------------------------------------------------------------------


LENGUAJE = Lenguaje(
    hallazgos_en_portada=3,
    se_nota_poco=0.20,
    se_nota=0.35,
    se_nota_mucho=0.50,
    grupos=(
        Grupo(
            clave="salir",
            titulo="Salir en bici",
            decision="si sales",
            exposiciones=frozenset({"bici_cualquiera", "minutos_bici"}),
            familias=frozenset(),
        ),
        Grupo(
            clave="apretar",
            titulo="Apretar",
            decision="cómo de fuerte",
            exposiciones=frozenset({"bici_intensa", "bici_media"}),
            familias=frozenset(),
        ),
        Grupo(
            clave="fuerza",
            titulo="Fuerza",
            decision="qué rutina",
            exposiciones=frozenset(),
            familias=frozenset({"rutina"}),
        ),
    ),
)


def exposicion(clave, *, familia="bici", en_frase="salir en bici", plural=False):
    return {
        "clave": clave,
        "etiqueta": clave,
        "tipo": "binaria",
        "familia": familia,
        "en_frase": en_frase,
        "plural": plural,
    }


def respuesta(clave="hrv", *, sentido="alto_mejor", en_frase="la variabilidad"):
    return {
        "clave": clave,
        "etiqueta": clave,
        "fuente": "garmin",
        "unidad": "ms",
        "sentido": sentido,
        "en_frase": en_frase,
    }


def fila(exp, resp, *casillas):
    return {"exposicion": exp, "respuesta": resp, "por_dia": list(casillas), "lectura": None}


def casilla(dias, r, *, significativa=True, n=100):
    return {
        "dias_despues": dias,
        "r": r,
        "p": 0.001,
        "p_corregida": 0.004,
        "significativa": significativa,
        "n": n,
        "metodo": "spearman",
        "desde": "2026-01-01",
        "hasta": "2026-09-11",
        "descartados": 0,
    }


def test_las_bandas_parten_donde_dice_el_config():
    assert LENGUAJE.banda(0.55) == "se nota mucho"
    assert LENGUAJE.banda(-0.55) == "se nota mucho"
    assert LENGUAJE.banda(0.50) == "se nota mucho"
    assert LENGUAJE.banda(0.36) == "se nota"
    assert LENGUAJE.banda(0.21) == "se nota poco"
    assert LENGUAJE.banda(0.19) is None
    assert LENGUAJE.banda(-0.19) is None


def test_una_exposicion_sin_grupo_revienta_en_vez_de_desaparecer():
    """El error duro es el punto de la función, y por eso tiene test propio.

    Sin él, una exposición nueva se calcularía, viajaría en la rejilla y no
    saldría jamás en la portada, sin un solo aviso. Es palabra por palabra el
    fallo del desplegable de Impacto que originó todo este rediseño: nadie echa
    de menos un hallazgo que no sabe que existe.
    """
    huerfana = exposicion("bici_nueva", familia="marciana")
    with pytest.raises(ValueError, match="no cae en ningún grupo"):
        grupo_de(huerfana, LENGUAJE)


def test_el_nombre_explicito_gana_a_la_familia():
    """`bici_intensa` es de familia `bici` y cae en "apretar", no en "salir"."""
    g = grupo_de(exposicion("bici_intensa"), LENGUAJE)
    assert g.clave == "apretar"

    # Y una que solo reclama su familia sigue cayendo donde debe.
    g2 = grupo_de(exposicion("rutina_dia_2", familia="rutina"), LENGUAJE)
    assert g2.clave == "fuerza"


def test_sin_seccion_metrics_no_hay_lenguaje_y_no_se_inventa():
    """Sin bandas en el config no se rellenan con un defecto.

    Un corte inventado decide qué se le enseña al usuario y qué se le esconde.
    Esa decisión no puede salir de un `.get(clave, 0.3)` que nadie ha escrito a
    propósito.
    """
    assert lenguaje_de(None) is None
    assert lenguaje_de({}) is None
    assert lenguaje_de({"metrics": {}}) is None


# ---------------------------------------------------------------------------
# Las frases
# ---------------------------------------------------------------------------


def test_el_verbo_concuerda_con_el_sujeto_en_plural():
    """«Las salidas largas te SUBEN», no «te sube».

    Salió mal en la primera versión y no dio ningún error: una frase mal
    conjugada se sirve con un 200 y se lee como escrita por una máquina.
    """
    r = hallazgos(
        [
            fila(
                exposicion(
                    "bici_intensa", en_frase="las salidas largas", plural=True
                ),
                respuesta("rhr", sentido="alto_peor", en_frase="el pulso en reposo"),
                casilla(1, 0.40),
            )
        ],
        LENGUAJE,
    )
    assert r[0]["frase"] == (
        "Las salidas largas te suben el pulso en reposo al día siguiente"
    )


def test_el_verbo_va_en_singular_con_un_sujeto_singular():
    r = hallazgos(
        [
            fila(
                exposicion("bici_cualquiera", en_frase="salir en bici"),
                respuesta(),
                casilla(1, -0.40),
            )
        ],
        LENGUAJE,
    )
    assert r[0]["frase"] == "Salir en bici te baja la variabilidad al día siguiente"


def test_la_mayuscula_inicial_no_se_come_las_de_dentro():
    """«hacer el Día 1» -> «Hacer el Día 1», no «Hacer el día 1».

    `str.capitalize()` baja todo lo demás. La ortografía de «Día 1» la pone
    `_rutina_en_frase` a partir de la clave `dia_1` del config, y deshacerla en
    la última línea sería tirar ese trabajo.
    """
    r = hallazgos(
        [
            fila(
                exposicion(
                    "rutina_dia_1", familia="rutina", en_frase="hacer el Día 1"
                ),
                respuesta("lower_discomfort", sentido="alto_peor",
                          en_frase="las molestias lumbares"),
                casilla(1, 0.40),
            )
        ],
        LENGUAJE,
    )
    assert r[0]["frase"].startswith("Hacer el Día 1 te suben") is False
    assert r[0]["frase"] == (
        "Hacer el Día 1 te sube las molestias lumbares al día siguiente"
    )


def test_la_valencia_sale_del_sentido_y_no_del_signo():
    """Un -0,4 es malo contra la HRV y bueno contra el pulso en reposo."""
    contra_hrv = hallazgos(
        [fila(exposicion("bici_cualquiera"), respuesta("hrv", sentido="alto_mejor"),
              casilla(1, -0.40))],
        LENGUAJE,
    )
    contra_rhr = hallazgos(
        [fila(exposicion("bici_cualquiera"),
              respuesta("rhr", sentido="alto_peor", en_frase="el pulso"),
              casilla(1, -0.40))],
        LENGUAJE,
    )
    assert contra_hrv[0]["valencia"] == "peor"
    assert contra_rhr[0]["valencia"] == "mejor"


# ---------------------------------------------------------------------------
# La agrupación por decisión
# ---------------------------------------------------------------------------


def test_dos_exposiciones_del_mismo_grupo_dan_UN_hallazgo():
    """La unidad es (grupo x respuesta), no (exposición x respuesta).

    Con r = 0,9986 entre los minutos y el desnivel, enseñarlos como dos
    hallazgos sugiere dos pruebas independientes donde hay una sola. No es
    repetitivo: es una exageración de la evidencia.
    """
    r = hallazgos(
        [
            fila(exposicion("bici_cualquiera", en_frase="salir en bici"),
                 respuesta(), casilla(1, -0.30)),
            fila(exposicion("minutos_bici", en_frase="acumular minutos"),
                 respuesta(), casilla(1, -0.45)),
        ],
        LENGUAJE,
    )
    assert len(r) == 1
    # Gana la más fuerte y la otra queda citada, no tirada.
    assert r[0]["exposicion"]["clave"] == "minutos_bici"
    assert r[0]["confirmada_por"] == ["salir en bici"]
    assert "la misma cosa medida de otra manera" in r[0]["nota_confirmacion"]


def test_dos_grupos_distintos_dan_DOS_hallazgos():
    r = hallazgos(
        [
            fila(exposicion("bici_cualquiera"), respuesta(), casilla(1, -0.30)),
            fila(exposicion("bici_intensa", en_frase="las salidas intensas",
                            plural=True), respuesta(), casilla(1, -0.45)),
        ],
        LENGUAJE,
    )
    assert len(r) == 2
    assert {h["grupo"]["clave"] for h in r} == {"salir", "apretar"}


def test_una_companera_del_signo_contrario_NO_confirma():
    """El fallo real del 2026-09-13, con su test.

    La portada salió diciendo que «las salidas medias» confirmaban que las
    intensas bajan la variabilidad, cuando la casilla más fuerte de las medias
    es del signo opuesto. Una frase falsa montada con números todos correctos,
    sin un solo error por ningún lado.
    """
    r = hallazgos(
        [
            fila(exposicion("bici_intensa", en_frase="las salidas intensas",
                            plural=True), respuesta(), casilla(1, -0.40)),
            fila(exposicion("bici_media", en_frase="las salidas medias",
                            plural=True), respuesta(), casilla(3, 0.25)),
        ],
        LENGUAJE,
    )
    assert len(r) == 1
    assert r[0]["confirmada_por"] == []
    assert r[0]["nota_confirmacion"] is None


def test_la_companera_que_discrepa_se_DICE_en_vez_de_tirarse():
    """Que dos caras de la misma decisión apunten al revés es información.

    Tirarla sería volver a lo de siempre -calcular algo, no enseñarlo y que
    nadie pueda echarlo de menos- con la excusa de que estropea el titular.
    """
    r = hallazgos(
        [
            fila(exposicion("bici_intensa", en_frase="las salidas intensas",
                            plural=True), respuesta(), casilla(1, -0.40)),
            fila(exposicion("bici_media", en_frase="las salidas medias",
                            plural=True), respuesta(), casilla(3, 0.25)),
        ],
        LENGUAJE,
    )
    assert [d["en_frase"] for d in r[0]["discrepa"]] == ["las salidas medias"]
    nota = r[0]["nota_discrepancia"]
    # Plural porque el nombre ya es plural, aunque solo haya uno.
    assert "las salidas medias apuntan al revés" in nota
    assert "No es bastante para saber por qué" in nota


def test_una_sola_discrepante_en_singular_lleva_el_verbo_en_singular():
    r = hallazgos(
        [
            fila(exposicion("bici_intensa", en_frase="las salidas intensas",
                            plural=True), respuesta(), casilla(1, -0.40)),
            fila(exposicion("bici_media", en_frase="apretar de más"),
                 respuesta(), casilla(3, 0.25)),
        ],
        LENGUAJE,
    )
    assert "apretar de más apunta al revés" in r[0]["nota_discrepancia"]


def test_lo_flojo_y_lo_no_significativo_no_llegan_a_frase():
    """Dos filtros distintos, y hacen falta los dos.

    Con 173 días un |r| de 0,15 sale significativo sin despeinarse y no cambia
    ninguna decisión. Contarlo sería verdad estadística y mentira práctica, y la
    portada se lee como si fuera práctica.
    """
    flojo = hallazgos(
        [fila(exposicion("bici_cualquiera"), respuesta(), casilla(1, -0.10))],
        LENGUAJE,
    )
    ruido = hallazgos(
        [fila(exposicion("bici_cualquiera"), respuesta(),
              casilla(1, -0.60, significativa=False))],
        LENGUAJE,
    )
    assert flojo == []
    assert ruido == []


def test_se_ordenan_por_fuerza_y_el_resto_se_cuenta():
    r = hallazgos(
        [
            fila(exposicion("bici_cualquiera"), respuesta("hrv"), casilla(1, -0.25)),
            fila(exposicion("bici_intensa", en_frase="las salidas intensas",
                            plural=True), respuesta("rhr", sentido="alto_peor",
                                                    en_frase="el pulso"),
                 casilla(1, 0.55)),
        ],
        LENGUAJE,
    )
    assert [abs(h["ficha"]["r"]) for h in r] == [0.55, 0.25]
    assert r[0]["fuerza"] == "se nota mucho"


def test_la_ficha_lleva_el_numero_entero_al_lado_de_la_banda():
    """Lo que se pidió: que la palabra PRECEDA al dato, no que lo sustituya."""
    r = hallazgos(
        [fila(exposicion("bici_cualquiera"), respuesta(), casilla(2, -0.42, n=173))],
        LENGUAJE,
    )
    f = r[0]["ficha"]
    assert r[0]["fuerza"] == "se nota"
    assert f["r"] == -0.42
    assert f["n"] == 173
    assert f["dias_despues"] == 2
    assert f["metodo"] == "spearman"
    assert f["p_corregida"] == 0.004
    assert f["desde"] == "2026-01-01" and f["hasta"] == "2026-09-11"


# ---------------------------------------------------------------------------
# Los bloques que cuentan filas
# ---------------------------------------------------------------------------


def test_lo_que_falta_se_cuenta_y_no_se_escribe(db):
    """Escrito a mano seguiría diciendo "hacen falta check-ins" con doscientos.

    Esa es la forma que tiene un texto de envejecer hacia falso en vez de hacia
    viejo, que es el mismo fallo que este proyecto lleva meses cazando.
    """
    vacio = lo_que_falta(db)
    assert {f["vistas"][0] for f in vacio} == {
        "concordancia", "impacto", "auditoria", "percepcion"
    }

    db.add(Checkin(date=dia(0), fatigue=3))
    db.add(WorkoutLog(hevy_workout_id="w1", date=dia(0), routine_key="dia_1"))
    db.add(
        Decision(
            date=dia(0), light="green", is_current=True,
            fired_rules_json="[]", skipped_rules_json="[]",
        )
    )
    db.commit()
    assert lo_que_falta(db) == []


def test_que_ha_cambiado_vacio_dice_por_que(db):
    b = que_ha_cambiado(db, hoy=HOY)
    assert b["estado"] == "vacio"
    assert b["lineas"] == []
    assert "dos semanas" in b["na"]


def test_que_ha_cambiado_compara_ventanas_DISJUNTAS(db):
    """La semana de en medio no puede contarse en las dos mitades.

    Es el mismo error que costó un tercio de señal en `engine/tendencia.py`: una
    ventana metida dentro de su propia referencia se compara en parte consigo
    misma.
    """
    # Tres salidas esta semana, una la anterior. Nada más viejo.
    for i in range(3):
        db.add(
            Activity(
                garmin_activity_id=100 + i,
                date=HOY - timedelta(days=i),
                is_cycling=True,
                intensity_level="media",
            )
        )
    db.add(
        Activity(
            garmin_activity_id=200,
            date=HOY - timedelta(days=DIAS_RECIENTES + 1),
            is_cycling=True,
            intensity_level="media",
        )
    )
    db.commit()

    b = que_ha_cambiado(db, hoy=HOY)
    linea = next(ln for ln in b["lineas"] if ln["clave"] == "bici")
    assert linea["esta_semana"] == 3
    assert linea["semana_anterior"] == 1
    assert linea["lectura"] == "3 esta semana, 2 más que la anterior"


def test_la_fuerza_distingue_no_entrenar_de_no_haber_apuntado_nunca(db):
    """Dos vacíos que se leerían igual y no son el mismo.

    "No has entrenado esta semana" habla del usuario; "el sistema no ha apuntado
    nada" habla del sistema. Confundirlos sería un reproche sin fundamento, que
    es exactamente lo que esta portada no puede permitirse.
    """
    v = vista_portada(db, None, dias=N, hoy=HOY)
    linea = next(ln for ln in v["como_voy"]["lineas"] if ln["clave"] == "fuerza")
    assert linea["lectura"] is None
    assert "el sistema todavía no ha apuntado" in linea["na"]

    # Con una sesión vieja pero ninguna esta semana, el mensaje cambia de sujeto.
    db.add(WorkoutLog(hevy_workout_id="w1", date=dia(0), routine_key="dia_1"))
    db.commit()
    v2 = vista_portada(db, None, dias=N, hoy=HOY)
    linea2 = next(ln for ln in v2["como_voy"]["lineas"] if ln["clave"] == "fuerza")
    assert linea2["na"] is None
    assert linea2["lectura"].startswith("0 sesiones esta semana")


def test_como_voy_no_compara_la_semana_contra_si_misma(db):
    """La media reciente NO entra en su propia distribución de referencia.

    Metida dentro, una mala semana se sube ella sola el listón y se tapa.
    """
    # 113 días de HRV a 50 y los últimos 7 a 30: la semana tiene que salir baja.
    for i in range(N):
        db.add(
            DailyMetrics(
                date=dia(i),
                fetch_status="ok",
                hrv=30.0 if i >= N - DIAS_RECIENTES else 50.0,
            )
        )
    db.commit()

    v = vista_portada(db, None, dias=N, hoy=HOY)
    hrv = next(ln for ln in v["como_voy"]["lineas"] if ln["clave"] == "hrv")
    assert hrv["media"] == 30.0
    assert hrv["nivel"] == "bajo"
    # HRV es alto_mejor, así que estar baja es "peor".
    assert hrv["valencia"] == "peor"
    assert hrv["lectura"] == "por debajo de lo tuyo"

    # Y la disjunción se comprueba CONTANDO, no por el veredicto. Sembrado así
    # de exagerado, la semana sale "baja" también con solapamiento -es el
    # mínimo de todas formas- y el test pasaría con el fallo dentro. Lo que solo
    # puede salir de una referencia limpia es su tamaño: 113 días previos dan
    # 113-7+1 = 107 ventanas, y meter los 7 recientes daría 114.
    #
    # Esto es exactamente el fallo de `engine/tendencia.py`, donde el
    # solapamiento amortiguaba la señal alrededor de un tercio sin dar ningún
    # error y sin cambiar el veredicto la mayoría de los días.
    assert hrv["n_reciente"] == DIAS_RECIENTES
    assert hrv["n_referencia"] == (N - DIAS_RECIENTES) - DIAS_RECIENTES + 1


def test_con_poca_semana_no_se_inventa_un_nivel(db):
    """Tres días con reloj no son una media de la semana: son tres días."""
    for i in range(N - 2, N):
        db.add(DailyMetrics(date=dia(i), fetch_status="ok", hrv=40.0))
    for i in range(N - 40, N - 10):
        db.add(DailyMetrics(date=dia(i), fetch_status="ok", hrv=50.0))
    db.commit()

    v = vista_portada(db, None, dias=N, hoy=HOY)
    hrv = next(ln for ln in v["como_voy"]["lineas"] if ln["clave"] == "hrv")
    assert hrv["nivel"] is None
    assert "hacen falta" in hrv["na"]


# ---------------------------------------------------------------------------
# Los campos obligatorios, que son la defensa de verdad
# ---------------------------------------------------------------------------


def test_una_exposicion_sin_frase_o_sin_numero_NO_SE_PUEDE_CONSTRUIR():
    """El valor de `en_frase` y `plural` sin defecto está en que no hay defecto.

    Con `default=""` o `default=False` todo seguiría pasando -los ocho sitios de
    hoy los pasan- y la trampa se armaría para el noveno: una exposición nueva
    entraría muda en la portada, o mal conjugada, sin un solo error. La garantía
    no la da que hoy estén puestos, la da que el módulo no importe sin ellos.

    Se prueba con `TypeError` porque es lo único que distingue "obligatorio" de
    "obligatorio de palabra". Sin este test, quitarle el candado al dataclass no
    rompe nada y nadie se entera.
    """
    from app.analysis.impacto import Exposicion

    with pytest.raises(TypeError):
        Exposicion("x", "X", "binaria", "bici", plural=False)  # falta en_frase
    with pytest.raises(TypeError):
        Exposicion("x", "X", "binaria", "bici", en_frase="x")  # falta plural

    # Y con las dos puestas se construye sin más.
    e = Exposicion("x", "X", "binaria", "bici", en_frase="salir", plural=False)
    assert e.como_dict()["en_frase"] == "salir"
    assert e.como_dict()["plural"] is False


def test_una_serie_sin_frase_NO_SE_PUEDE_CONSTRUIR():
    """Lo mismo para `series.Definicion`, que es de donde salen las respuestas.

    Una serie nueva sin frase saldría en la portada como «te baja » y a nadie le
    fallaría nada.
    """
    from app.analysis.series import Definicion

    with pytest.raises(TypeError):
        Definicion("x", "X", "garmin", "ms", "alto_mejor")

    d = Definicion("x", "X", "garmin", "ms", "alto_mejor", en_frase="la equis")
    assert d.como_dict()["en_frase"] == "la equis"


def test_con_poco_historico_no_se_situa_la_semana(db):
    """La semana entera con reloj, pero sin nada contra lo que compararla.

    Es el otro mínimo y hace falta por separado: aquí la media SÍ es buena -son
    los siete días- y lo que falta es la referencia. "Estás en el percentil 30"
    sobre cuatro ventanas quiere decir "hay una peor que esta", y eso no es un
    percentil, es una anécdota con el disfraz puesto.

    Sin este test, subir `MINIMO_REFERENCIA` a cero no rompe nada y la portada
    empieza a repartir veredictos desde el tercer día de vida del sistema, que
    es cuando más se los cree quien los lee.
    """
    # 23 días: 7 recientes y 16 previos, que dan 16-7+1 = 10 ventanas. El número
    # está elegido para caer DENTRO del umbral, no muy por debajo: con una sola
    # ventana el test pasaría también con `MINIMO_REFERENCIA` bajado a 2, y
    # entonces no estaría probando el umbral sino la ausencia de datos. Diez
    # distingue un 20 de un 2, que es lo que hay que distinguir.
    for i in range(N - 23, N):
        db.add(DailyMetrics(date=dia(i), fetch_status="ok", hrv=40.0 + i % 3))
    db.commit()

    v = vista_portada(db, None, dias=N, hoy=HOY)
    hrv = next(ln for ln in v["como_voy"]["lineas"] if ln["clave"] == "hrv")
    assert hrv["n_reciente"] == DIAS_RECIENTES
    assert hrv["n_referencia"] == 10
    assert hrv["nivel"] is None
    assert hrv["lectura"] is None
    assert "semanas anteriores" in hrv["na"]
    assert str(hrv["n_referencia"]) in hrv["na"]


def test_sin_config_la_portada_dice_que_le_falta_el_config(db):
    """No se rellena con defectos: se explica el hueco."""
    v = vista_portada(db, None, dias=N, hoy=HOY)
    bloque = v["lo_que_se_sabe"]
    assert bloque["estado"] == "vacio"
    assert "metrics" in bloque["na"]
    assert bloque["portada"] == []
