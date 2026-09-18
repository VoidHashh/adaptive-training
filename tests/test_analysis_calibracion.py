"""Vista 7 con desacuerdos sembrados de resultado conocido.

Las tres medidas que se pidieron por su nombre tienen aquí tests propios, y cada
test siembra el caso de forma que la respuesta se puede escribir en el `assert`
sin haber ejecutado antes la implementación.

LO QUE SE PRUEBA AQUÍ NO SON LOS NÚMEROS, SON LAS NEGATIVAS
-----------------------------------------------------------
Contar filas lo hace bien cualquiera. Lo que distingue esta vista de una tabla
de recuentos son los sitios donde se NIEGA a contestar, y son casi todos los
tests de este archivo:

  - el porcentaje va sobre las opinadas, no sobre el total: mirar la tarjeta y
    cerrar el móvil no es estar de acuerdo;
  - sin denominador no hay 0,0, hay un motivo escrito;
  - un desacuerdo sin anulación no se reparte entre «más dura» y «más suave»;
  - la lectura de la dirección se calla con menos de tres;
  - un empate en cabeza no nombra regla;
  - el veredicto se calla por debajo de cinco Y se calla sin grupo de contraste,
    que son dos silencios distintos y el segundo es el que se olvida;
  - los casos que no juzgan dicen CUÁL de las cuatro cosas les falta.

Un fallo en cualquiera de esos sitios no rompe la pantalla: la deja diciendo un
número redondo y falso, que es de lo que esta vista existe para protegerse.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.analysis.calibracion import (
    ETIQUETA_JUEZ,
    MAS_DURA,
    MAS_SUAVE,
    MINIMO_JUICIOS,
    SIN_PEDIR,
    SIN_REGLA,
    vista_calibracion,
)
from app.analysis.stats import N_MINIMO_CALCULABLE
from app.models import Base, Decision, Preview, SessionPerformance

HOY = date(2026, 9, 11)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def dia(i: int) -> date:
    """Hace `i` días. `dia(0)` es hoy."""
    return HOY - timedelta(days=i)


# ---------------------------------------------------------------------------
# Sembradores
# ---------------------------------------------------------------------------


def decision(db, i: int, *, ejecutada: str, luz: str = "green") -> Decision:
    """La decisión que se acabó guardando ese día, con la sesión que salió."""
    fila = Decision(
        date=dia(i),
        light=luz,
        planned_session_json=json.dumps({"kind": ejecutada}),
    )
    db.add(fila)
    db.flush()
    return fila


def previsualizacion(
    db,
    i: int,
    *,
    seq: int = 1,
    luz: str = "green",
    propuesta: str = "full",
    regla: str | None = None,
    discrepa: bool | None = None,
    pediste: str | None = None,
    forzada: bool = False,
    enviada: Decision | None = None,
) -> Preview:
    fila = Preview(
        date=dia(i),
        seq=seq,
        light=luz,
        session_type=propuesta,
        decision_json=json.dumps({"trigger_rule": regla}) if regla else None,
        disagreed=discrepa,
        override_session_type=pediste,
        forced_on_red=forzada,
        decision_id=enviada.id if enviada is not None else None,
    )
    db.add(fila)
    db.flush()
    return fila


def resultado(db, i: int, pct: float, *, kind: str = "strength") -> None:
    """Cómo salió la sesión de ese día, que es el juez de la medida (b)."""
    db.add(
        SessionPerformance(
            date=dia(i),
            kind=kind,
            source_key=f"hevy:{kind}:{i}:{pct}",
            performance_pct=pct,
        )
    )
    db.flush()


def vista(db, **kwargs):
    return vista_calibracion(db, hasta=HOY, **kwargs)


def celda(direcciones: dict, clave: str) -> dict:
    return next(c for c in direcciones["celdas"] if c["clave"] == clave)


def grupo(agrupado: list[dict], clave: str) -> dict:
    return next(g for g in agrupado if g["clave"] == clave)


def caso_de(quien_acerto: dict, i: int) -> dict:
    fecha = dia(i).isoformat()
    return next(c for c in quien_acerto["casos"] if c["fecha"] == fecha)


# ---------------------------------------------------------------------------
# (a) Cuántas veces, y sobre qué denominador
# ---------------------------------------------------------------------------


def test_el_porcentaje_va_sobre_las_opinadas_y_no_sobre_el_total(db):
    """Mirar la tarjeta y no decir nada NO es estar de acuerdo.

    Este es el número que más fácil sale mal y peor se nota: con el total como
    denominador, el porcentaje de desacuerdo baja cada vez que se abre la
    previsualización por curiosidad y se cierra el móvil. Acabaría midiendo con
    qué frecuencia se pulsa un botón, y bajando solo con el uso.
    """
    for i in (1, 2):
        previsualizacion(db, i, discrepa=True)
    for i in (3, 4):
        previsualizacion(db, i, discrepa=False)
    for i in (5, 6, 7, 8, 9, 10):
        previsualizacion(db, i, discrepa=None)

    c = vista(db)["cuantas"]
    assert (c["discrepadas"], c["conformes"], c["sin_opinar"]) == (2, 2, 6)
    assert c["opinadas"] == 4
    assert c["total"] == 10
    # 2 de 4 opinadas, no 2 de 10.
    assert c["pct"] == 50.0
    assert c["na"] is None


def test_sin_ninguna_opinion_el_porcentaje_no_es_cero_sino_un_motivo(db):
    """Un 0,0 % sin denominador dice «nunca discrepas», que es falso y distinto
    de «todavía no has dicho nada»."""
    for i in (1, 2, 3):
        previsualizacion(db, i, discrepa=None)

    c = vista(db)["cuantas"]
    assert c["opinadas"] == 0
    assert c["pct"] is None
    assert c["na"] and "no es un cero" in c["na"]


def test_dos_previsualizaciones_del_mismo_dia_cuentan_las_dos(db):
    """La revisión es el dato, y por eso no se deduplica por día.

    Si la segunda tapara a la primera, la tabla entera sobraba: para guardar la
    última respuesta ya estaba `checkins`.
    """
    previsualizacion(db, 2, seq=1, discrepa=True)
    previsualizacion(db, 2, seq=2, discrepa=False)

    c = vista(db)["cuantas"]
    assert c["total"] == 2
    assert c["opinadas"] == 2
    assert c["pct"] == 50.0


def test_la_ventana_deja_fuera_lo_de_antes(db):
    previsualizacion(db, 5, discrepa=True)
    previsualizacion(db, 40, discrepa=True)

    c = vista(db, dias=30)["cuantas"]
    assert c["total"] == 1
    assert c["discrepadas"] == 1


# ---------------------------------------------------------------------------
# (a) Hacia dónde
# ---------------------------------------------------------------------------


def test_la_direccion_sale_de_la_dureza_pedida_y_no_del_motivo_escrito(db):
    """Pedir `full` sobre `reduced` es más dura; `recovery` sobre `full`, más suave."""
    previsualizacion(db, 1, discrepa=True, propuesta="reduced", pediste="full")
    previsualizacion(db, 2, discrepa=True, propuesta="full", pediste="recovery")
    previsualizacion(db, 3, discrepa=True, propuesta="recovery", pediste="reduced")

    d = vista(db)["direcciones"]
    assert d["n"] == 3
    assert celda(d, MAS_DURA)["n"] == 2
    assert celda(d, MAS_SUAVE)["n"] == 1
    assert celda(d, SIN_PEDIR)["n"] == 0


def test_discrepar_sin_pedir_otra_sesion_es_una_casilla_propia(db):
    """«Dije que no y aun así hice lo que ponía» es una respuesta entera.

    Es además la más frecuente, y repartirla entre las otras dos -o dejarla
    fuera- fabricaría una tendencia que no existe. Se comprueba de las dos
    formas: que cae donde tiene que caer y que las tres celdas suman el total.
    """
    previsualizacion(db, 1, discrepa=True, propuesta="full", pediste=None)
    previsualizacion(db, 2, discrepa=True, propuesta="full", pediste="recovery")

    d = vista(db)["direcciones"]
    assert celda(d, SIN_PEDIR)["n"] == 1
    assert celda(d, MAS_DURA)["n"] == 0
    assert sum(c["n"] for c in d["celdas"]) == d["n"] == 2


def test_pedir_la_misma_dureza_que_la_propuesta_no_tiene_direccion(db):
    """Anular por rutina sin cambiar de dureza no es ni más dura ni más suave."""
    previsualizacion(db, 1, discrepa=True, propuesta="full", pediste="full")

    d = vista(db)["direcciones"]
    assert celda(d, SIN_PEDIR)["n"] == 1
    assert celda(d, MAS_DURA)["n"] == 0
    assert celda(d, MAS_SUAVE)["n"] == 0


def test_una_dureza_que_el_motor_ya_no_conoce_no_se_reparte_a_ojo(db):
    """Una fila vieja con un tipo de sesión renombrado.

    No se sabe si era más dura o más suave, así que no se elige: elegir a ojo
    estropearía justo el número que esta vista da.
    """
    previsualizacion(db, 1, discrepa=True, propuesta="full", pediste="maraton")

    d = vista(db)["direcciones"]
    assert celda(d, SIN_PEDIR)["n"] == 1
    assert celda(d, MAS_DURA)["n"] + celda(d, MAS_SUAVE)["n"] == 0


def test_las_forzadas_en_rojo_no_son_una_cuarta_casilla(db):
    """Van sueltas porque se pidió poder mirarlas, pero son un SUBCONJUNTO.

    Si sumaran, el total de direcciones saldría mayor que el de desacuerdos y la
    tabla parecería haber perdido la cuenta.
    """
    previsualizacion(
        db, 1, discrepa=True, luz="red", propuesta="recovery",
        pediste="full", forzada=True,
    )
    previsualizacion(db, 2, discrepa=True, propuesta="reduced", pediste="full")

    d = vista(db)["direcciones"]
    assert d["forzadas_en_rojo"] == 1
    assert celda(d, MAS_DURA)["n"] == 2  # la forzada YA está contada aquí
    assert sum(c["n"] for c in d["celdas"]) == d["n"] == 2


def test_la_lectura_de_la_direccion_se_calla_con_menos_de_tres(db):
    """Con dos desacuerdos «siempre pides más dura» es cierto y vacío."""
    for i in range(1, N_MINIMO_CALCULABLE):
        previsualizacion(db, i, discrepa=True, propuesta="reduced", pediste="full")

    d = vista(db)["direcciones"]
    assert d["n"] == N_MINIMO_CALCULABLE - 1
    assert d["lectura"] is None

    previsualizacion(db, 9, discrepa=True, propuesta="reduced", pediste="full")
    d = vista(db)["direcciones"]
    assert d["n"] == N_MINIMO_CALCULABLE
    assert d["lectura"] and "más dura" in d["lectura"]


def test_sin_ningun_desacuerdo_no_hay_direccion_sino_motivo(db):
    previsualizacion(db, 1, discrepa=False)

    d = vista(db)["direcciones"]
    assert d["n"] == 0
    assert d["lectura"] is None
    assert d["na"]


# ---------------------------------------------------------------------------
# (c) Dónde se agolpa
# ---------------------------------------------------------------------------


def test_los_dos_porcentajes_del_agolpamiento_contestan_cosas_distintas(db):
    """La razón por la que ninguno de los dos viaja solo.

    Se siembran dos reglas opuestas:

      - `fatiga_alta` decide diez días y se discrepa de tres. Es la que más
        desacuerdos acumula -el 75 % de todos- y a la vez de la que MENOS se
        discrepa cuando aparece (30 %). Leyendo solo el primer porcentaje, es la
        culpable; y no lo es, es solo la que más sale.
      - `lumbar_alta` decide una vez y se discrepa esa vez: 100 % cuando
        aparece, y un solo desacuerdo. Leyendo solo el segundo porcentaje, es la
        culpable; y tampoco lo es, porque ha disparado una vez.

    Ninguna de las dos lecturas sola señala un sitio donde tocar un umbral. Las
    dos juntas sí.
    """
    for i in range(1, 4):
        previsualizacion(db, i, regla="fatiga_alta", discrepa=True)
    for i in range(4, 11):
        previsualizacion(db, i, regla="fatiga_alta", discrepa=False)
    previsualizacion(db, 11, regla="lumbar_alta", discrepa=True)

    por_regla = vista(db)["donde"]["por_regla"]
    fatiga = grupo(por_regla, "fatiga_alta")
    lumbar = grupo(por_regla, "lumbar_alta")

    assert fatiga["discrepadas"] == 3
    assert fatiga["pct_de_los_desacuerdos"] == 75.0
    assert fatiga["pct_cuando_aparece"] == 30.0

    assert lumbar["discrepadas"] == 1
    assert lumbar["pct_de_los_desacuerdos"] == 25.0
    assert lumbar["pct_cuando_aparece"] == 100.0


def test_el_porcentaje_del_grupo_tampoco_cuenta_las_que_no_opinaron(db):
    """Mismo denominador que arriba, y aquí es donde se olvida.

    En el test de los dos porcentajes todas las filas llevan opinión, así que
    `opinadas` y `vistas` valen lo mismo y la cuenta sale bien aunque se use la
    columna equivocada. Este siembra las tres cosas a la vez -una discrepada, una
    conforme y cuatro miradas sin decir nada- para que los dos denominadores se
    separen: 1 de 2 es 50 %, y 1 de 6 sería 16,7 %.

    Las `vistas` viajan igualmente, porque saber que se opinó dos de seis veces
    es lo que impide leer ese 50 % como si describiera las mañanas.
    """
    previsualizacion(db, 1, regla="fatiga_alta", discrepa=True)
    previsualizacion(db, 2, regla="fatiga_alta", discrepa=False)
    for i in (3, 4, 5, 6):
        previsualizacion(db, i, regla="fatiga_alta", discrepa=None)

    fatiga = grupo(vista(db)["donde"]["por_regla"], "fatiga_alta")
    assert (fatiga["opinadas"], fatiga["vistas"]) == (2, 6)
    assert fatiga["pct_cuando_aparece"] == 50.0


def test_una_previsualizacion_sin_regla_se_agrupa_con_nombre_propio(db):
    """Un día verde sin regla disparada no desaparece de la tabla."""
    previsualizacion(db, 1, regla=None, discrepa=True)

    por_regla = vista(db)["donde"]["por_regla"]
    assert grupo(por_regla, SIN_REGLA)["discrepadas"] == 1


def test_el_agolpamiento_tambien_se_mira_por_color(db):
    """El tercer ámbar, el conforme, es el que hace que este test pueda fallar.

    Con dos ámbares y los dos discrepados, `pct_cuando_aparece` y
    `pct_de_los_desacuerdos` valen los dos 100 y el test pasa aunque se calculen
    con el mismo denominador. Ese tercero separa las dos cuentas -2 de 3 opinadas
    es 66,7, 2 de 2 desacuerdos es 100- y es lo único que convierte este test en
    una guarda en vez de en una coincidencia.
    """
    previsualizacion(db, 1, luz="amber", discrepa=True)
    previsualizacion(db, 2, luz="amber", discrepa=True)
    previsualizacion(db, 3, luz="amber", discrepa=False)
    previsualizacion(db, 4, luz="green", discrepa=False)

    por_luz = vista(db)["donde"]["por_luz"]
    ambar = grupo(por_luz, "amber")
    assert ambar["discrepadas"] == 2
    assert ambar["pct_de_los_desacuerdos"] == 100.0
    assert ambar["pct_cuando_aparece"] == 66.7
    assert grupo(por_luz, "green")["discrepadas"] == 0
    assert grupo(por_luz, "green")["pct_de_los_desacuerdos"] == 0.0


def test_un_empate_en_cabeza_no_nombra_ninguna_regla(db):
    """Nombrar la primera de dos iguales sería un hallazgo de orden alfabético."""
    for i in (1, 2):
        previsualizacion(db, i, regla="aaa_primera", discrepa=True)
    for i in (3, 4):
        previsualizacion(db, i, regla="zzz_ultima", discrepa=True)

    donde = vista(db)["donde"]
    assert donde["lectura"] == "los desacuerdos no se agolpan en ninguna regla concreta"


def test_cuando_una_regla_destaca_la_lectura_la_nombra(db):
    for i in (1, 2, 3):
        previsualizacion(db, i, regla="fatiga_alta", discrepa=True)
    previsualizacion(db, 4, regla="lumbar_alta", discrepa=True)

    donde = vista(db)["donde"]
    assert donde["lectura"] and "fatiga_alta" in donde["lectura"]


def test_la_tabla_del_agolpamiento_no_se_reordena_sola_entre_recargas(db):
    """Dos grupos empatados se ordenan por nombre, no por el capricho del dict.

    Una tabla que cambia de orden entre dos recargas sin que haya pasado nada no
    se puede comparar consigo misma, y esta es de mirar dos veces con semanas de
    diferencia.

    OJO CON EL ORDEN DEL SEMBRADO: es lo único que hace fallar a este test.
    Los grupos se construyen recorriendo las filas ordenadas por fecha, así que
    el orden de inserción del diccionario es el de las fechas. Si la regla que va
    primera por alfabeto es además la más antigua, el `sort` sin el segundo
    criterio la deja donde ya estaba -la ordenación de Python es estable- y el
    test pasa sin haber comprobado nada. Se descubrió mutando: escrito al revés,
    quitar `d["clave"]` del `key` no lo ponía rojo.

    Por eso `zzz_ultima` es la ANTIGUA: sin el segundo criterio sale primera.
    """
    previsualizacion(db, 9, regla="zzz_ultima", discrepa=True)
    previsualizacion(db, 1, regla="aaa_primera", discrepa=True)

    claves = [g["clave"] for g in vista(db)["donde"]["por_regla"]]
    assert claves == ["aaa_primera", "zzz_ultima"]


# ---------------------------------------------------------------------------
# (b) Quién acertó: los cuatro motivos por los que un caso no juzga
# ---------------------------------------------------------------------------


def test_un_desacuerdo_que_no_se_llego_a_enviar_no_juzga_nada(db):
    previsualizacion(db, 1, discrepa=True, propuesta="reduced", pediste="full")
    resultado(db, 1, 80.0)

    q = vista(db)["quien_acerto"]
    caso = caso_de(q, 1)
    assert caso["se_envio"] is False
    assert caso["na"] and "no llegaste a enviarlo" in caso["na"]
    assert q["n"] == 0


def test_discrepar_sin_pedir_otra_sesion_no_juzga_nada(db):
    """Salió la sesión del motor, así que el resultado no dice quién tenía razón.

    Este caso es el que más fácil se cuela en el recuento: hay desacuerdo, hay
    decisión enviada y hay rendimiento medido. Lo que no hay es una sesión
    distinta que comparar, y contarlo mediría la opinión contra un desenlace que
    la opinión no tocó.
    """
    d = decision(db, 1, ejecutada="full")
    previsualizacion(db, 1, discrepa=True, propuesta="full", pediste=None, enviada=d)
    resultado(db, 1, 80.0)

    q = vista(db)["quien_acerto"]
    caso = caso_de(q, 1)
    assert caso["se_hizo_lo_que_pediste"] is None
    assert caso["na"] and "no pediste otra sesión" in caso["na"]
    assert q["n"] == 0


def test_pedir_una_sesion_y_que_se_ejecute_otra_no_juzga_nada(db):
    d = decision(db, 1, ejecutada="reduced")
    previsualizacion(db, 1, discrepa=True, propuesta="reduced", pediste="full", enviada=d)
    resultado(db, 1, 80.0)

    q = vista(db)["quien_acerto"]
    caso = caso_de(q, 1)
    assert caso["se_ejecuto"] == "reduced"
    assert caso["se_hizo_lo_que_pediste"] is False
    assert caso["na"] and "no es lo que pedías" in caso["na"]
    assert q["n"] == 0


def test_una_sesion_sin_resultado_medido_todavia_no_juzga_nada(db):
    d = decision(db, 1, ejecutada="full")
    previsualizacion(db, 1, discrepa=True, propuesta="reduced", pediste="full", enviada=d)

    q = vista(db)["quien_acerto"]
    caso = caso_de(q, 1)
    assert caso["se_hizo_lo_que_pediste"] is True
    assert caso["rendimiento_pct"] is None
    assert caso["na"] and "todavía no tiene resultado" in caso["na"]
    assert q["n"] == 0


def test_el_caso_que_si_juzga_sale_sin_motivo_y_con_su_percentil(db):
    d = decision(db, 1, ejecutada="full")
    previsualizacion(db, 1, discrepa=True, propuesta="reduced", pediste="full", enviada=d)
    resultado(db, 1, 72.0)

    q = vista(db)["quien_acerto"]
    caso = caso_de(q, 1)
    assert caso["na"] is None
    assert caso["direccion"] == MAS_DURA
    assert caso["rendimiento_pct"] == 72.0
    assert q["n"] == 1


def test_un_dia_con_fuerza_y_bici_no_elige_una_de_las_dos_a_ojo(db):
    """Dos resultados el mismo día: la mediana, no el primero que llegue."""
    d = decision(db, 1, ejecutada="full")
    previsualizacion(db, 1, discrepa=True, propuesta="reduced", pediste="full", enviada=d)
    resultado(db, 1, 60.0, kind="strength")
    resultado(db, 1, 80.0, kind="bike")

    caso = caso_de(vista(db)["quien_acerto"], 1)
    assert caso["rendimiento_pct"] == 70.0


# ---------------------------------------------------------------------------
# (b) Los dos silencios del veredicto
# ---------------------------------------------------------------------------


def sembrar_juzgables(db, cuantos: int, pct: float = 80.0, desde: int = 1) -> None:
    """`cuantos` desacuerdos completos: pedidos, ejecutados y con resultado."""
    for k in range(cuantos):
        i = desde + k
        d = decision(db, i, ejecutada="full")
        previsualizacion(
            db, i, discrepa=True, propuesta="reduced", pediste="full", enviada=d
        )
        resultado(db, i, pct)


def test_el_veredicto_se_calla_por_debajo_del_minimo_y_dice_cuanto_falta(db):
    sembrar_juzgables(db, MINIMO_JUICIOS - 1)
    resultado(db, 50, 40.0)  # grupo de contraste, para aislar el motivo

    q = vista(db)["quien_acerto"]
    assert q["n"] == MINIMO_JUICIOS - 1
    assert q["veredicto"] is None
    assert q["na"] and f"van {MINIMO_JUICIOS - 1} de los {MINIMO_JUICIOS}" in q["na"]


def test_el_veredicto_se_calla_sin_grupo_de_contraste(db):
    """El silencio que se olvida: muestra de sobra y nada contra qué comparar.

    Con los cinco juicios y ningún día normal medido, la mediana de los cinco es
    un número perfectamente calculado que no dice nada: no se sabe en qué
    percentil sale un día cualquiera, así que no se sabe si 80 es bueno.
    """
    sembrar_juzgables(db, MINIMO_JUICIOS)

    q = vista(db)["quien_acerto"]
    assert q["n"] == MINIMO_JUICIOS
    assert q["n_el_resto"] == 0
    assert q["mediana_discrepando"] == 80.0
    assert q["veredicto"] is None


def test_los_dias_discrepados_no_entran_en_su_propio_grupo_de_contraste(db):
    """Compararse contra uno mismo daría siempre «igual que las demás»."""
    sembrar_juzgables(db, MINIMO_JUICIOS)
    resultado(db, 50, 40.0)

    q = vista(db)["quien_acerto"]
    assert q["n_el_resto"] == 1
    assert q["mediana_el_resto"] == 40.0


def test_con_muestra_y_contraste_el_veredicto_compara_las_dos_medianas(db):
    sembrar_juzgables(db, MINIMO_JUICIOS, pct=80.0)
    for i in range(50, 55):
        resultado(db, i, 40.0)

    q = vista(db)["quien_acerto"]
    assert q["mediana_discrepando"] == 80.0
    assert q["mediana_el_resto"] == 40.0
    assert q["veredicto"] and "salieron mejor" in q["veredicto"]


def test_el_veredicto_dice_hacia_donde_y_los_numeros_viajan_aparte(db):
    """La regla 2, atada por los dos lados: ni en la frase, ni perdidos.

    La frase es lo que se lee al ABRIR la pantalla, y los percentiles van detrás
    de «ver detalle». Aquí ponía «salieron mejor que las demás: percentil 80.0
    frente a 40.0», y ese trozo es exactamente lo que la regla 2 manda plegar.

    Las dos mitades del `assert` tienen que estar las dos. Sin la primera, nada
    impide volver a meter el número en la frase. Sin la segunda, la forma más
    fácil de aprobar la primera es dejar de calcular las medianas, y entonces el
    detalle que se abre cuando la frase no basta se queda sin el dato que lo
    justifica: esconder no es borrar.
    """
    sembrar_juzgables(db, MINIMO_JUICIOS, pct=80.0)
    for i in range(50, 55):
        resultado(db, i, 40.0)

    q = vista(db)["quien_acerto"]
    assert "percentil" not in q["veredicto"]
    assert "80.0" not in q["veredicto"] and "40.0" not in q["veredicto"]
    assert q["mediana_discrepando"] == 80.0 and q["mediana_el_resto"] == 40.0


def test_el_veredicto_dice_a_donde_salieron_las_sesiones_no_quien_tenia_razon(db):
    """La distancia entre las dos frases es lo que el `comp_rpe` no deja recorrer.

    Un veredicto que dijera «tenías razón» estaría afirmando algo que este juez
    no puede sostener: dentro del índice va el esfuerzo percibido que apunta el
    propio usuario.
    """
    sembrar_juzgables(db, MINIMO_JUICIOS, pct=30.0)
    for i in range(50, 55):
        resultado(db, i, 70.0)

    q = vista(db)["quien_acerto"]
    assert q["veredicto"] and "salieron peor" in q["veredicto"]
    assert "razón" not in q["veredicto"]


def test_la_etiqueta_del_juez_viaja_siempre_incluso_sin_un_solo_caso(db):
    """Sobre todo sin casos: es cuando la pantalla está más vacía y más se lee.

    Si la etiqueta apareciera solo junto al veredicto, el día que el veredicto
    dijera «salieron mejor» sería la primera vez que se lee, y ya no habría forma
    de distinguir entre acertar y ser indulgente al puntuarse.
    """
    q = vista(db)["quien_acerto"]
    assert q["casos"] == []
    assert q["etiqueta_del_juez"] == ETIQUETA_JUEZ
    assert "comp_rpe" not in q["etiqueta_del_juez"]  # se explica, no se cita
    assert q["na"] and str(MINIMO_JUICIOS) in q["na"]


# ---------------------------------------------------------------------------
# La vista entera
# ---------------------------------------------------------------------------


def test_abrir_la_vista_no_escribe_nada(db):
    """Una pantalla que al abrirse tocara `previews` cambiaría el dato por mirarlo.

    La tabla de previsualizaciones es el registro de lo que se pensó cada mañana.
    Es el sitio donde un `UPDATE` accidental no daría error nunca y falsearía las
    tres medidas a la vez.
    """
    d = decision(db, 1, ejecutada="full")
    previsualizacion(db, 1, discrepa=True, propuesta="reduced", pediste="full", enviada=d)
    resultado(db, 1, 72.0)
    db.commit()

    antes = [
        (p.id, p.seq, p.disagreed, p.answers_json, p.light, p.override_session_type)
        for p in db.scalars(select(Preview).order_by(Preview.id))
    ]
    n_decisiones = db.scalar(select(func.count()).select_from(Decision))

    vista(db)
    db.expire_all()

    despues = [
        (p.id, p.seq, p.disagreed, p.answers_json, p.light, p.override_session_type)
        for p in db.scalars(select(Preview).order_by(Preview.id))
    ]
    assert despues == antes
    assert db.scalar(select(func.count()).select_from(Decision)) == n_decisiones


def test_con_la_base_vacia_la_vista_sale_entera_y_con_los_motivos(db):
    """Ninguna vista se esconde: se pinta toda, con el porqué en cada casilla."""
    v = vista(db)

    assert v["vista"] == "calibracion"
    assert set(v) >= {"cuantas", "direcciones", "donde", "quien_acerto"}
    assert v["cuantas"]["pct"] is None and v["cuantas"]["na"]
    assert v["direcciones"]["na"] and len(v["direcciones"]["celdas"]) == 3
    assert v["donde"]["na"] and v["donde"]["por_regla"] == []
    assert v["quien_acerto"]["veredicto"] is None and v["quien_acerto"]["na"]


def test_la_ventana_se_anuncia_con_sus_dos_extremos(db):
    """Y con la misma forma que las otras siete, que la PWA pinta con una sola
    función: `ventana.desde`, `ventana.hasta`, `ventana.dias`. Publicadas en la
    raíz del payload se pintarían igual de bien hasta que alguien mirara el
    encabezado de ésta al lado del de cualquier otra.
    """
    v = vista(db, dias=30)
    assert v["ventana"]["dias"] == 30
    assert v["ventana"]["hasta"] == HOY.isoformat()
    assert v["ventana"]["desde"] == (HOY - timedelta(days=29)).isoformat()
