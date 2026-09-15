"""Que las dos respuestas de Sí/No lleguen enteras al análisis, y la derivada con ellas.

Lo que se prueba aquí no es una cuenta difícil -la discordancia es un `!=`- sino
las tres formas en que una respuesta de dos valores se pierde por el camino sin
dar un error:

  - el `None` que se convierte en `False`. «No contestaste» y «contestaste que
    no» son dos cosas distintas y las dos caben en la misma columna nula. En
    cuanto una de ellas se cuela en la otra, la serie entera se lee como una fila
    de noes y ningún número sale raro;

  - la serie derivada que no existe en ninguna tabla. `discordancia` no tiene
    columna, así que cualquier lectura que vaya directa a `Checkin` revienta o
    -peor- devuelve un hueco silencioso;

  - la pregunta que se guarda y no se cruza con nada. Es el interruptor conectado
    a nada de siempre: el formulario la pide todas las mañanas, la base la
    guarda, y no aparece en una sola correlación. No da error nunca.

Y una cuarta, que es la que justifica el módulo `preguntas.py`: el binario solo
-«el 30 % de los días no coincidieron»- junta dos situaciones opuestas. Que la
tabla de las cuatro casillas viaje pegada al número es lo que impide leerlo mal.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.analysis import series as S
from app.analysis.preguntas import tabla_discordancia
from app.models import Base, Checkin, DailyMetrics

HOY = date(2026, 9, 11)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def checkin(session, dia: date, **campos) -> None:
    session.add(Checkin(date=dia, **campos))
    session.commit()


# ---------------------------------------------------------------------------
# Las dos preguntas como serie
# ---------------------------------------------------------------------------


def test_un_si_vale_uno_y_un_no_vale_cero(db):
    """Un booleano se convierte con `float`, que es lo que lo deja comparable.

    Una serie de ceros y unos tiene media, y su media es una proporción: 0,43 es
    «dijiste que sí el 43 % de los días». Eso es lo que permite correlacionarla y
    situarla en su histórico como cualquier otra.
    """
    checkin(db, HOY, wants_to_train=True)
    checkin(db, HOY - timedelta(days=1), wants_to_train=False)

    s = S.serie(db, "wants_to_train", HOY - timedelta(days=1), HOY)
    assert s[HOY] == 1.0
    assert s[HOY - timedelta(days=1)] == 0.0


def test_no_contestar_no_es_contestar_que_no(db):
    """La distinción que defienden el formulario, la API y el mensaje del día.

    Aquí la defiende `float(v)` con el guardia del `None` delante. Con `bool(v)`
    -que no da error ninguno- el día sin contestar valdría 0.0 y sería
    indistinguible de un «no» de verdad: la media bajaría, el percentil se
    movería, y en la pantalla saldría un número perfectamente creíble.
    """
    checkin(db, HOY, wants_to_train=False)
    checkin(db, HOY - timedelta(days=1), wants_to_train=None, fatigue=3)

    s = S.serie(db, "wants_to_train", HOY - timedelta(days=1), HOY)
    assert s[HOY] == 0.0
    assert s[HOY - timedelta(days=1)] is None, (
        "un día sin contestar se está contando como un «no»"
    )


def test_la_discordancia_sale_de_las_dos_y_no_de_ninguna_columna(db):
    """La única serie del sistema que no está en ninguna tabla.

    Las cuatro combinaciones, en cuatro días seguidos: coinciden en dos y
    discrepan en las otras dos, y la serie tiene que decir exactamente eso sin
    que exista un `Checkin.discordancia` en ninguna parte.
    """
    dias = [HOY - timedelta(days=i) for i in range(4)]
    checkin(db, dias[0], wants_to_train=True, will_train=True)
    checkin(db, dias[1], wants_to_train=True, will_train=False)
    checkin(db, dias[2], wants_to_train=False, will_train=True)
    checkin(db, dias[3], wants_to_train=False, will_train=False)

    assert not hasattr(Checkin, "discordancia")
    s = S.serie(db, "discordancia", dias[3], dias[0])
    assert s[dias[0]] == 0.0
    assert s[dias[1]] == 1.0
    assert s[dias[2]] == 1.0
    assert s[dias[3]] == 0.0


def test_con_media_pregunta_contestada_no_hay_discordancia_que_contar(db):
    """Un `False` aquí diría «contestaste lo mismo a las dos» sobre medio día.

    Es la misma trampa que el `None` de arriba, un piso más arriba: la mitad de
    las combinaciones posibles de dos preguntas son «no contestó», y darles un
    cero fabricaría días de coherencia cada vez que el formulario se envía a
    medias.
    """
    checkin(db, HOY, wants_to_train=True, will_train=None)
    checkin(db, HOY - timedelta(days=1), wants_to_train=None, will_train=True)

    s = S.serie(db, "discordancia", HOY - timedelta(days=1), HOY)
    assert s[HOY] is None
    assert s[HOY - timedelta(days=1)] is None


def test_la_discordancia_del_analisis_dice_lo_mismo_que_la_del_motor(db, cfg):
    """Las dos copias de la cuenta, enfrentadas de verdad.

    `signals.py` la calcula para el mensaje de la mañana y `series.py` la calcula
    sobre meses de histórico para las vistas. Están escritas dos veces a
    propósito -tienen formas distintas- y lo único que no puede pasar es que
    difieran: el mensaje diría una cosa y la pantalla otra sobre el mismo martes,
    y las dos serían defendibles mirándolas por separado.

    Aquí este test escribía la regla por TERCERA vez y la comparaba contra
    `series.py`, sin llegar a tocar `signals.py`. O sea que decía comprobar que
    las dos copias coinciden y lo que comprobaba era que una de ellas coincide
    con una copia nueva escrita en el propio test. La batería de mutaciones lo
    enseñó: invertir el `!=` del motor dejaba esto verde. Así que ahora se llama
    a `build_signals` de verdad, con las nueve combinaciones metidas a la vez en
    la base y en el histórico que recibe el motor, y se comparan los dos
    diccionarios día a día.
    """
    # El `Checkin` del motor no es el de `app.models`: aquel es la fila de la
    # tabla y este el dato suelto con un `values`. Que se llamen igual es de
    # siempre y no se toca hoy, pero aquí conviven los dos y hay que nombrarlos.
    from app.engine.signals import Checkin as CheckinMotor
    from app.engine.signals import CLAVE_APETECE, CLAVE_VOY_A_ENTRENAR, build_signals

    combinaciones = [(a, v) for a in (True, False, None) for v in (True, False, None)]
    dias = [HOY - timedelta(days=i) for i in range(len(combinaciones))]
    historial = []
    for dia, (apetece, voy) in zip(dias, combinaciones):
        checkin(db, dia, wants_to_train=apetece, will_train=voy)
        historial.append(
            CheckinMotor(
                date=dia,
                values={
                    k: v
                    for k, v in (
                        (CLAVE_APETECE, apetece),
                        (CLAVE_VOY_A_ENTRENAR, voy),
                    )
                    if v is not None
                },
            )
        )

    sig = build_signals(
        cfg, HOY, metrics=[], rides=[], sessions=[], checkin_history=historial
    )
    del_motor = sig.history["discordancia"]
    del_analisis = S.serie(db, "discordancia", dias[-1], dias[0])

    # El motor deja fuera los días sin las dos -su histórico solo lleva valores
    # sabidos, por convenio del módulo- y el análisis los deja dentro con `None`.
    # Esa asimetría es a propósito y está documentada en los dos sitios; lo que
    # se compara aquí es que donde los dos hablan, digan lo mismo, y que ninguno
    # se calle un día del que el otro sí tiene respuesta.
    assert del_motor, "el motor no ha dejado histórico de discordancia ninguno"
    assert set(del_motor) == {d for d, v in del_analisis.items() if v is not None}
    for dia, valor in del_motor.items():
        assert del_analisis[dia] == float(valor), dia


# ---------------------------------------------------------------------------
# Que la tabla no se quede atrás
# ---------------------------------------------------------------------------


def test_una_pregunta_nueva_en_el_yaml_revienta_en_vez_de_desaparecer(cfg_copia):
    """El interruptor conectado a nada, aplicado a las preguntas.

    Una tercera pregunta -«¿has dormido fuera de casa?»- se contestaría todas las
    mañanas, se guardaría en su columna y no saldría en una sola correlación. El
    dato recogido y no analizado es la peor combinación de las tres posibles.
    """
    cfg_copia.raw["checkin_preguntas"].append(
        {"key": "dormido_fuera", "label": "¿Has dormido fuera?"}
    )
    with pytest.raises(ValueError, match="dormido_fuera"):
        S.comprobar_preguntas(cfg_copia)


def test_el_config_y_la_tabla_de_preguntas_dicen_lo_mismo(cfg):
    """Con el `config.yaml` de verdad."""
    S.comprobar_preguntas(cfg)
    S.comprobar_series(cfg)


def test_la_derivada_no_se_exige_en_el_yaml(cfg):
    """`discordancia` no es una pregunta que nadie conteste.

    Si `comprobar_preguntas` la exigiera en `checkin_preguntas`, el formulario
    tendría que hacer una pregunta que no se puede hacer: «¿estás en desacuerdo
    contigo mismo?».
    """
    from app.repository import preguntas_del_config

    assert "discordancia" not in set(preguntas_del_config(cfg))
    assert "discordancia" in S.PREGUNTAS
    assert "discordancia" in S.DERIVADAS
    S.comprobar_preguntas(cfg)


def test_ninguna_pregunta_se_cuela_entre_los_deslizadores():
    """La separación ES la garantía, y por eso hay un test que la mira.

    Varios sitios recorren `SLIDERS` para conceder permisos: qué puede mirar el
    semáforo, qué se pinta sobre el calendario, qué sale en el desplegable de
    percepción. Una pregunta metida ahí se ganaría los tres sin que nadie los
    concediera, y el primero es el que este proyecto entero se dedica a negar.
    """
    assert not (set(S.PREGUNTAS) & set(S.SLIDERS))
    from app.analysis.rendimiento import PERCEPCION

    assert not (set(S.PREGUNTAS) & set(PERCEPCION))


def test_toda_derivada_tiene_escrita_su_cuenta():
    """Sin esto el fallo sale en la pantalla del usuario, no al arrancar."""
    from app.analysis.series import _COMBINA

    assert set(_COMBINA) == set(S.DERIVADAS)


def test_la_guardia_del_registro_avisa_de_verdad_y_dice_lo_que_falta():
    """La comprobación, no los datos que hoy la pasan.

    El test de arriba mira que `_COMBINA` y `DERIVADAS` coincidan HOY, y eso está
    bien, pero deja sin vigilar la guardia misma: cambiarla por un `if False:` no
    ponía nada rojo, porque los datos seguían coincidiendo. Una guardia que
    ningún test toca es una guardia que alguien quita por inútil, y justo el día
    que empieza a hacer falta.

    Así que aquí se llama a `cuadran` con dos diccionarios que NO cuadran, en los
    dos sentidos, y se exige que reviente diciendo cuál sobra y cuál falta. El
    mensaje importa tanto como la excepción: quien lo lea estará arrancando el
    contenedor, sin depurador, y lo único que va a tener es esa línea.
    """
    S.cuadran({"a": 1}, {"a": 1}, nombre="X", nombre_contra="Y", falta="da igual")

    with pytest.raises(ValueError) as e:
        S.cuadran({"a": 1, "b": 2}, {"a": 1}, nombre="X", nombre_contra="Y", falta="ponla")
    assert "sobran: ['b']" in str(e.value)
    assert "faltan: []" in str(e.value)
    assert "ponla" in str(e.value)

    with pytest.raises(ValueError) as e:
        S.cuadran({"a": 1}, {"a": 1, "c": 3}, nombre="X", nombre_contra="Y", falta="ponla")
    assert "faltan: ['c']" in str(e.value)


# ---------------------------------------------------------------------------
# Las cuatro casillas
# ---------------------------------------------------------------------------


def _reparto(db, *, apetecia_y_no=0, sin_ganas_y_si=0, coinciden_si=0, coinciden_no=0):
    """Días con las dos contestadas, repartidos como se pida."""
    i = 0

    def mete(n, apetece, voy):
        nonlocal i
        for _ in range(n):
            checkin(db, HOY - timedelta(days=i), wants_to_train=apetece, will_train=voy)
            i += 1

    mete(apetecia_y_no, True, False)
    mete(sin_ganas_y_si, False, True)
    mete(coinciden_si, True, True)
    mete(coinciden_no, False, False)


def test_las_cuatro_casillas_salen_siempre_las_cuatro(db):
    """Incluidas las que valen cero, y ese es el punto entero.

    Una casilla vacía que no se pinta se lee como «esto no pasa». Una casilla
    vacía pintada con su total al lado se lee como lo que es: «de los veinte días
    contados, cero cayeron aquí».
    """
    _reparto(db, apetecia_y_no=6, coinciden_si=14)
    t = tabla_discordancia(db, HOY - timedelta(days=60), HOY)

    assert len(t["celdas"]) == 4
    por_llave = {(c["apetece"], c["voy"]): c for c in t["celdas"]}
    assert por_llave[(True, False)]["n"] == 6
    assert por_llave[(True, True)]["n"] == 14
    assert por_llave[(False, True)]["n"] == 0
    assert por_llave[(False, False)]["n"] == 0
    assert t["n"] == 20
    assert t["discordantes"] == 6
    assert t["pct_discordantes"] == 30.0


def test_las_cuatro_salen_siempre_en_el_mismo_orden(db):
    """Primero las dos que coinciden, después las dos que no.

    Una tabla de dos por dos que se reordena cada vez que se abre no se puede
    comparar consigo misma de un día para otro: la casilla que ayer estaba arriba
    a la izquierda hoy está abajo, y la lectura de un vistazo -que es la única
    que se hace a las siete de la mañana- deja de existir. Por eso el orden se
    escribe una vez en `CASILLAS` y no se deja al `dict` ni al navegador.

    Con dos repartos distintos, porque un orden que dependa de los datos -las más
    llenas primero, por ejemplo- pasaría igual de bien un test con un reparto
    solo.
    """
    esperado = [(True, True), (False, False), (True, False), (False, True)]

    _reparto(db, apetecia_y_no=6, coinciden_si=14)
    t = tabla_discordancia(db, HOY - timedelta(days=60), HOY)
    assert [(c["apetece"], c["voy"]) for c in t["celdas"]] == esperado

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as otra:
        _reparto(otra, sin_ganas_y_si=9, coinciden_no=2)
        t = tabla_discordancia(otra, HOY - timedelta(days=60), HOY)
    assert [(c["apetece"], c["voy"]) for c in t["celdas"]] == esperado


def test_el_cero_viaja_siempre_con_su_denominador(db):
    """Un cero sin total al lado dice lo mismo con cinco días que con ciento ochenta."""
    _reparto(db, coinciden_si=5)
    t = tabla_discordancia(db, HOY - timedelta(days=60), HOY)

    assert t["discordantes"] == 0
    assert "5 días" in t["ficha"]
    assert t["n"] == 5


def test_los_dos_repartos_opuestos_no_se_pintan_iguales(db):
    """El binario los confunde; la tabla es lo que los distingue.

    Treinta días discordantes por «me apetecía y no fui» y treinta por «no me
    apetecía y fui» dan el mismo 0,30 de media y describen a dos personas
    distintas. La lectura de la tabla tiene que decir cuál de las dos fue.
    """
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as otra:
        _reparto(otra, sin_ganas_y_si=9, coinciden_si=21)
        b = tabla_discordancia(otra, HOY - timedelta(days=60), HOY)

    _reparto(db, apetecia_y_no=9, coinciden_si=21)
    a = tabla_discordancia(db, HOY - timedelta(days=60), HOY)

    assert a["pct_discordantes"] == b["pct_discordantes"] == 30.0
    assert a["lectura"] != b["lectura"]
    assert "no entrenaste" in a["lectura"]
    assert "sin que te apeteciera" in b["lectura"]


def test_un_dia_con_media_pregunta_se_cuenta_aparte_y_no_dentro(db):
    """Meterlo en la casilla que "parece" sería inventarse la mitad del dato."""
    _reparto(db, coinciden_si=4)
    checkin(db, HOY - timedelta(days=40), wants_to_train=True)
    checkin(db, HOY - timedelta(days=41), will_train=False)

    t = tabla_discordancia(db, HOY - timedelta(days=60), HOY)
    assert t["n"] == 4
    assert t["sin_las_dos"] == 2
    assert "2 días con solo una" in t["ficha"]


def test_con_menos_de_tres_dias_no_hay_reparto_que_ensenar(db):
    _reparto(db, coinciden_si=2)
    t = tabla_discordancia(db, HOY - timedelta(days=60), HOY)

    assert t["na"], "dos días se están pintando como si fueran una distribución"
    assert t["lectura"] is None
    # Pero las casillas siguen ahí: lo que se marca es la muestra, no se esconde
    # la tabla.
    assert len(t["celdas"]) == 4


def test_la_base_vacia_lo_dice_y_no_se_cae(db):
    t = tabla_discordancia(db, HOY - timedelta(days=60), HOY)
    assert t["n"] == 0
    assert t["na"] and "todavía no hay ningún día" in t["na"]
    assert all(c["n"] == 0 and c["pct"] is None for c in t["celdas"])
    assert t["pct_discordantes"] is None


def test_entre_tres_y_veinte_dias_la_tabla_sale_marcada_no_escondida(db):
    _reparto(db, apetecia_y_no=2, coinciden_si=8)
    t = tabla_discordancia(db, HOY - timedelta(days=60), HOY)

    assert t["na"] is None
    assert t["aviso"], "diez días se están pintando como si fueran un histórico"
    assert t["lectura"]


# ---------------------------------------------------------------------------
# Que lleguen a las vistas
# ---------------------------------------------------------------------------


def _sembrar_para_las_vistas(db, n=40):
    for i in range(n):
        d = HOY - timedelta(days=i)
        db.add(
            Checkin(
                date=d,
                fatigue=1 + (i % 5),
                wants_to_train=(i % 3 != 0),
                will_train=(i % 4 != 0),
            )
        )
        db.add(DailyMetrics(date=d, fetch_status="ok", hrv=70.0 - (i % 5) * 6))
    db.commit()


def test_la_vista_1_trae_las_tres_series_y_la_tabla_pegada_a_la_derivada(db):
    """«Se cuentan en todas las correlaciones» tiene que poder comprobarse."""
    from app.analysis.concordancia import vista_concordancia

    _sembrar_para_las_vistas(db)
    v = vista_concordancia(db, dias=90, hoy=HOY)

    por_clave = {s["clave"]: s for s in v["series"]}
    assert {"wants_to_train", "will_train", "discordancia"} <= set(por_clave)
    assert por_clave["discordancia"]["n"] > 0

    # La tabla va pegada a la serie que no se puede leer sin ella...
    assert por_clave["discordancia"]["tabla"]["n"] > 0
    assert len(por_clave["discordancia"]["tabla"]["celdas"]) == 4
    # ...y la clave existe en TODAS, con valor `None`. Una clave que falta y una
    # clave que vale `None` son lo mismo en JavaScript, y de ahí sale una rama
    # elegida al revés sin dejar rastro.
    assert all("tabla" in s for s in v["series"])
    assert por_clave["hrv"]["tabla"] is None


def test_la_vista_2_cruza_las_preguntas_con_el_reloj(db):
    from app.analysis.concordancia import vista_desfase

    _sembrar_para_las_vistas(db)
    v = vista_desfase(db, dias=90, hoy=HOY)

    ejes = {c["x"] for c in v["rejilla"]}
    assert {"wants_to_train", "will_train", "discordancia"} <= ejes
    casilla = next(
        c for c in v["rejilla"] if c["x"] == "wants_to_train" and c["y"] == "hrv"
    )
    assert sorted(int(k) for k in casilla["por_desfase"]) == [-3, -2, -1, 0, 1, 2, 3]


def test_la_vista_3_las_usa_de_respuesta_y_nunca_de_exposicion(db):
    """La flecha tiene una dirección y no se puede invertir sin decirlo.

    «Salir largo en bici te baja el apetito de entrenar» es lo que esta vista
    existe para descubrir. «No tener ganas te baja la variabilidad» es la misma
    correlación leída al revés y no hay nada detrás que la sostenga.
    """
    from app.analysis.impacto import vista_impacto

    _sembrar_para_las_vistas(db)
    v = vista_impacto(db, dias=90, hoy=HOY)

    respuestas = {f["respuesta"]["clave"] for f in v["rejilla"]}
    exposiciones = {f["exposicion"]["clave"] for f in v["rejilla"]}
    assert set(S.PREGUNTAS) <= respuestas
    assert not (set(S.PREGUNTAS) & exposiciones)


def test_la_tendencia_pinta_las_preguntas_en_porcentaje(db):
    """0,43 encima de la palabra «apetece» no significa nada a las siete.

    La media de una serie de ceros y unos es una proporción, así que la línea
    dice «43 %» y no «0,4». Y la unidad no puede salir de `Definicion.sufijo`,
    que para estas vale `None` porque su `unidad` declarada -"0-1"- es un rango.
    """
    from app.analysis.portada import como_voy

    _sembrar_para_las_vistas(db, n=180)
    v = como_voy(db, hoy=HOY, dias=180, cob=S.cobertura(db))

    por_clave = {ln["clave"]: ln for ln in v["lineas"]}
    apetece = por_clave["wants_to_train"]
    assert apetece["unidad"] == "%"
    assert apetece["media"] is not None and 0.0 <= apetece["media"] <= 100.0

    # Y la tabla solo en la derivada, que es la única que se lee mal sola.
    assert por_clave["discordancia"]["tabla"]["n"] > 0
    assert apetece["tabla"] is None
    assert all("tabla" in ln for ln in v["lineas"])


def test_la_intencion_de_entrenar_no_emite_juicio(db):
    """`will_train` es `neutro` y tiene que llegar `neutro` hasta la pantalla.

    Entrenar no es mejor que no entrenar: el día que toca descanso, un «no» es el
    plan cumpliéndose. Una valencia de «peor» en esa línea sería el sistema
    escribiendo cada semana que entrenar más es estar mejor, que es exactamente
    la creencia por la que existe.
    """
    from app.analysis.portada import como_voy

    _sembrar_para_las_vistas(db, n=180)
    v = como_voy(db, hoy=HOY, dias=180, cob=S.cobertura(db))
    por_clave = {ln["clave"]: ln for ln in v["lineas"]}

    assert S.PREGUNTAS["will_train"].sentido == "neutro"
    assert S.PREGUNTAS["discordancia"].sentido == "neutro"
    for clave in ("will_train", "discordancia"):
        assert por_clave[clave]["valencia"] in {"neutro", "normal", None}
