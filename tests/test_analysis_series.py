"""Que cada número acabe en el día que le toca, y que un hueco no se vuelva un cero.

Las correlaciones ya están probadas aparte con números de resultado conocido. Lo
que se prueba aquí es lo de antes: la contabilidad. Una correlación impecable
sobre pares mal alineados da un resultado impecablemente falso, y no hay forma de
verlo mirando la salida -sale un número, con su n y su p, perfectamente creíble-.

Los tres sitios donde eso puede pasar:

  - `yesterday_rpe`, que se contesta el día D y habla del día D-1. Si no se
    desplaza, TODAS las parejas de esfuerzo salen corridas un día;
  - los días sin actividad, que dentro de la ventana observada son descanso -un
    cero de verdad- y fuera de ella no son nada. Rellenar de ceros fuera fabrica
    meses de descanso que nunca existieron, y devolver `None` dentro tira a la
    basura los días de descanso, que son justo los que dan contraste;
  - la normalización a escala común, que tiene que dejar los huecos como huecos.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.analysis import series as S
from app.models import Activity, Base, Checkin, DailyMetrics, WorkoutLog

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


def wellness(session, dia: date, **campos) -> None:
    session.add(DailyMetrics(date=dia, fetch_status="ok", **campos))
    session.commit()


def salida(session, dia: date, *, carga=100.0, minutos=60, desnivel=300.0, ident=None):
    session.add(
        Activity(
            garmin_activity_id=ident if ident is not None else int(dia.strftime("%Y%m%d")),
            date=dia,
            is_cycling=True,
            training_load=carga,
            duration_s=minutos * 60,
            elevation_gain_m=desnivel,
        )
    )
    session.commit()


def entreno(session, dia: date, *, volumen=5000.0, series=20, ident=None):
    session.add(
        WorkoutLog(
            hevy_workout_id=ident or dia.isoformat(),
            date=dia,
            total_volume_kg=volumen,
            total_sets=series,
        )
    )
    session.commit()


# ---------------------------------------------------------------------------
# El deslizador que habla de ayer
# ---------------------------------------------------------------------------


def test_el_rpe_se_guarda_en_el_dia_del_entreno_no_en_el_de_la_respuesta(db):
    """Es el desplazamiento, y es el fallo más caro de los que caben aquí.

    Se contesta el martes que el entreno del lunes fue un 5. Si la serie lo
    coloca en el martes, la pareja `yesterday_rpe` vs `carga_bici` compara el
    esfuerzo del lunes con la carga del martes. No falla nada: sale una
    correlación baja y creíble, y la conclusión sería "no percibe bien el
    esfuerzo" cuando lo percibe perfectamente y el que suma mal es el programa.
    """
    lunes, martes = date(2026, 9, 7), date(2026, 9, 8)
    checkin(db, martes, yesterday_rpe=5, fatigue=2)

    rpe = S.serie(db, "yesterday_rpe", lunes - timedelta(days=3), martes)
    assert rpe[lunes] == 5.0
    assert martes not in rpe

    # Y el resto de deslizadores NO se mueven: el cansancio del martes es del
    # martes. Un desplazamiento global sería el mismo error con otro signo.
    cansancio = S.serie(db, "fatigue", lunes - timedelta(days=3), martes)
    assert cansancio[martes] == 2.0
    assert lunes not in cansancio


def test_el_rpe_del_ultimo_dia_de_la_ventana_no_se_pierde(db):
    """La ventana pedida acaba hoy, pero el RPE de hoy vive en el check-in de mañana.

    Si la consulta leyera los check-ins con las mismas fechas que se piden, el
    último día de cada ventana se quedaría siempre sin RPE, y como sería siempre
    el último nadie lo notaría: la serie tendría un día menos y ya está.
    """
    ayer, hoy = HOY - timedelta(days=1), HOY
    checkin(db, hoy, yesterday_rpe=4)

    rpe = S.serie(db, "yesterday_rpe", HOY - timedelta(days=10), ayer)
    assert rpe[ayer] == 4.0


def test_el_desplazamiento_esta_declarado_y_es_el_unico(db):
    """Si algún día otro deslizador empieza a hablar del pasado, que se vea aquí."""
    desplazados = {k: d.desplazamiento for k, d in S.SLIDERS.items() if d.desplazamiento}
    assert desplazados == {"yesterday_rpe": 1}


# ---------------------------------------------------------------------------
# El cero que es descanso y el hueco que no es nada
# ---------------------------------------------------------------------------


def test_un_dia_sin_salida_dentro_de_la_ventana_es_un_cero_de_verdad(db):
    """Los días de descanso son datos. Sin ellos la carga no tiene con qué contrastar.

    Si los días sin bici se devolvieran como huecos, `emparejar` los descartaría
    y la correlación entre carga y cualquier cosa se calcularía SOLO sobre los
    días que entrenó. Justo los días que más dicen -entrenó fuerte / no entrenó-
    desaparecerían de la comparación.
    """
    salida(db, date(2026, 9, 1), carga=200.0)
    salida(db, date(2026, 9, 5), carga=50.0)

    s = S.serie(db, "carga_bici", date(2026, 9, 1), date(2026, 9, 5))
    assert s == {
        date(2026, 9, 1): 200.0,
        date(2026, 9, 2): 0.0,
        date(2026, 9, 3): 0.0,
        date(2026, 9, 4): 0.0,
        date(2026, 9, 5): 50.0,
    }


def test_fuera_de_la_ventana_observada_no_hay_ceros_hay_nada(db):
    """Antes de la primera salida guardada nadie miró. Eso no es descanso."""
    salida(db, date(2026, 9, 1))
    salida(db, date(2026, 9, 3))

    s = S.serie(db, "carga_bici", date(2026, 8, 28), date(2026, 9, 6))
    for d in (date(2026, 8, 28), date(2026, 8, 31)):
        assert s[d] is None, f"{d} es anterior a lo observado y sale como cero"
    for d in (date(2026, 9, 4), date(2026, 9, 6)):
        assert s[d] is None, f"{d} es posterior a lo observado y sale como cero"
    assert s[date(2026, 9, 2)] == 0.0  # este SÍ: está dentro y fue descanso


def test_dos_salidas_el_mismo_dia_se_suman(db):
    """Dos salidas son un día de más carga, no un día de carga media."""
    dia = date(2026, 9, 2)
    salida(db, dia, carga=120.0, minutos=45, desnivel=400.0, ident=1)
    salida(db, dia, carga=80.0, minutos=30, desnivel=150.0, ident=2)

    assert S.serie(db, "carga_bici", dia, dia)[dia] == 200.0
    assert S.serie(db, "minutos_bici", dia, dia)[dia] == 75.0
    assert S.serie(db, "desnivel_bici", dia, dia)[dia] == 550.0


def test_una_salida_sin_carga_estimada_no_vale_cero(db):
    """La fila existe, la columna viene nula. Eso es una salida sin ese dato.

    Contarla como cero diría que ese día pedaleó sin esfuerzo, que es
    literalmente lo contrario de lo que pasó.
    """
    dia = date(2026, 9, 2)
    salida(db, dia, carga=None)
    assert S.serie(db, "carga_bici", dia, dia)[dia] is None


def test_lo_que_no_es_bici_no_cuenta_como_carga_de_bici(db):
    """La vista habla de salidas. Un paseo registrado no es una salida."""
    dia = date(2026, 9, 2)
    db.add(
        Activity(
            garmin_activity_id=999, date=dia, is_cycling=False, training_load=300.0
        )
    )
    salida(db, dia, carga=100.0, ident=1)
    assert S.serie(db, "carga_bici", dia, dia)[dia] == 100.0


def test_el_volumen_de_fuerza_sigue_las_mismas_reglas(db):
    entreno(db, date(2026, 9, 1), volumen=4000.0, series=18)
    entreno(db, date(2026, 9, 3), volumen=6000.0, series=22)

    s = S.serie(db, "volumen_fuerza", date(2026, 8, 30), date(2026, 9, 4))
    assert s[date(2026, 8, 30)] is None  # fuera
    assert s[date(2026, 9, 1)] == 4000.0
    assert s[date(2026, 9, 2)] == 0.0  # dentro, sin entreno
    assert s[date(2026, 9, 3)] == 6000.0
    assert s[date(2026, 9, 4)] is None  # fuera


def test_la_bici_y_la_fuerza_tienen_cada_una_su_ventana(db):
    """Llevan meses distintos registrados, y mezclar las ventanas inventa datos.

    Si la cobertura fuera una sola para todo "entreno", los días con bici pero
    sin histórico de fuerza dirían "ese día no levantó nada" cuando lo que pasa
    es que Hevy todavía no estaba conectado.
    """
    salida(db, date(2026, 7, 1))
    salida(db, date(2026, 9, 1))
    entreno(db, date(2026, 9, 1))
    entreno(db, date(2026, 9, 5))

    cob = S.cobertura(db)
    assert cob.bici == (date(2026, 7, 1), date(2026, 9, 1))
    assert cob.fuerza == (date(2026, 9, 1), date(2026, 9, 5))

    julio = date(2026, 7, 1)
    assert S.serie(db, "carga_bici", julio, julio)[julio] == 100.0
    assert S.serie(db, "volumen_fuerza", julio, julio)[julio] is None


def test_la_cobertura_vacia_no_revienta(db):
    """Una base recién creada. Todo `None`, nada de fechas inventadas."""
    cob = S.cobertura(db)
    assert cob.como_dict() == {
        "checkin": None,
        "garmin": None,
        "bici": None,
        "fuerza": None,
    }


# ---------------------------------------------------------------------------
# Bienestar: aquí la ausencia SIEMPRE es hueco
# ---------------------------------------------------------------------------


def test_un_dia_de_bienestar_sin_fila_no_aparece(db):
    """Sin reloj no hay cero: el corazón latió igual, el dato se perdió."""
    wellness(db, date(2026, 9, 1), hrv=50.0)
    wellness(db, date(2026, 9, 3), hrv=60.0)

    s = S.serie(db, "hrv", date(2026, 9, 1), date(2026, 9, 3))
    assert s == {date(2026, 9, 1): 50.0, date(2026, 9, 3): 60.0}
    assert date(2026, 9, 2) not in s


def test_una_metrica_nula_dentro_de_una_fila_que_existe_sale_nula(db):
    """La fila del 2 existe -Garmin contestó- pero sin body battery.

    Sale como `None` y no como ausente, y las dos cosas acaban igual en
    `emparejar`. Lo que importa es que no salga como cero, que sería "acabó el
    día sin batería".
    """
    wellness(db, date(2026, 9, 2), hrv=45.0, body_battery=None)
    s = S.serie(db, "body_battery", date(2026, 9, 2), date(2026, 9, 2))
    assert s[date(2026, 9, 2)] is None


# ---------------------------------------------------------------------------
# Escala común
# ---------------------------------------------------------------------------


def test_normalizar_lleva_el_mayor_arriba_y_el_menor_abajo():
    vals = {
        date(2026, 9, 1): 10.0,
        date(2026, 9, 2): 20.0,
        date(2026, 9, 3): 30.0,
        date(2026, 9, 4): 40.0,
    }
    n = S.normalizar(vals)
    assert n[date(2026, 9, 1)] < n[date(2026, 9, 2)] < n[date(2026, 9, 3)]
    assert n[date(2026, 9, 4)] == max(v for v in n.values() if v is not None)
    assert all(0.0 <= v <= 100.0 for v in n.values() if v is not None)


def test_normalizar_deja_los_huecos_como_huecos():
    vals = {
        date(2026, 9, 1): 10.0,
        date(2026, 9, 2): None,
        date(2026, 9, 3): 30.0,
    }
    n = S.normalizar(vals)
    assert n[date(2026, 9, 2)] is None


def test_normalizar_una_serie_entera_de_huecos_no_revienta():
    vals = {date(2026, 9, 1): None, date(2026, 9, 2): None}
    assert S.normalizar(vals) == vals


def test_normalizar_no_aplasta_la_serie_contra_el_techo_por_un_dia_raro():
    """La razón de usar percentil y no regla de tres entre el mínimo y el máximo.

    Treinta días entre 40 y 60, y una noche de gripe con la HRV a 8. Con una
    escala lineal, los treinta días normales se quedarían todos apretados entre
    el 80 y el 100 y el gráfico sería una línea recta con un pico. Con
    percentiles, los treinta días normales siguen repartidos por todo el rango,
    que es lo que hace falta para poder compararlos con otra serie.
    """
    vals = {date(2026, 8, 1) + timedelta(days=i): 40.0 + i * 0.7 for i in range(30)}
    vals[date(2026, 7, 30)] = 8.0

    n = S.normalizar(vals)
    normales = [n[d] for d in vals if vals[d] != 8.0]
    assert min(normales) < 20.0, "los días normales están todos pegados al techo"
    assert max(normales) > 90.0
    assert n[date(2026, 7, 30)] < min(normales)


# ---------------------------------------------------------------------------
# Que la tabla de deslizadores no se quede atrás
# ---------------------------------------------------------------------------


def test_el_config_y_la_tabla_de_sentidos_dicen_lo_mismo(cfg):
    """Con el `config.yaml` de verdad, no con uno de mentira."""
    S.comprobar_sliders(cfg)


def test_un_deslizador_nuevo_en_el_yaml_revienta_en_vez_de_desaparecer(cfg_copia):
    """Es el interruptor conectado a nada, otra vez.

    Añadir un deslizador al formulario y no añadirlo aquí no rompería nada: la
    PWA lo pintaría, la base lo guardaría, y la sección de métricas seguiría
    enseñando las siete correlaciones de siempre sin mencionarlo. El dato nuevo
    estaría recogido y no analizado, que es la peor combinación.
    """
    cfg_copia.raw["checkin_sliders"].append({"key": "dolor_rodilla", "label": "Rodilla"})
    with pytest.raises(ValueError, match="dolor_rodilla"):
        S.comprobar_sliders(cfg_copia)


def test_una_serie_desconocida_revienta_con_la_lista_delante(db):
    with pytest.raises(ValueError, match="serie desconocida"):
        S.serie(db, "hrv_verdadera", HOY, HOY)


def test_todas_las_definiciones_tienen_un_sentido_valido():
    for clave, d in S.DEFINICIONES.items():
        assert d.sentido in {"alto_peor", "alto_mejor", "neutro"}, clave
        assert d.fuente in {"checkin", "garmin", "entreno"}, clave
        assert d.etiqueta and d.unidad, clave


def test_un_rango_no_se_escribe_detras_de_un_numero():
    """«85,50 0-100» estuvo escrito en la primera pantalla del panel.

    `unidad` hace dos trabajos con la misma palabra: para la HRV es una unidad de
    verdad -"ms"- y para la nota de sueño es el rango de la escala -"0-100"-. La
    portada la pegaba detrás del valor sin mirar cuál le había tocado, y de ahí
    salía una frase que no escribiría nadie, en el sitio de la interfaz que más
    se lee y con el aspecto de un número mal formateado.

    No daba ningún error, y por eso hace falta este test: lo único que lo delató
    fue pintar las seis vistas contra la base de verdad y leerlas. El andamio de
    Node no lo veía porque todas las claves existían y todos los valores eran
    cadenas; era correcto y decía una tontería.

    Se comprueba por la FORMA y no contra una lista de claves: una serie nueva
    con escala 0-10 tiene que acertar sola. Y se comprueba en los dos sentidos
    -que los rangos desaparezcan y que las unidades de verdad sigan enteras-,
    porque un `sufijo` que devolviera `None` siempre también haría pasar la
    mitad de arriba.
    """
    rangos = {c: d for c, d in S.DEFINICIONES.items()
              if re.fullmatch(r"\d+(?:[.,]\d+)?-\d+(?:[.,]\d+)?", d.unidad)}
    assert rangos, (
        "no queda ni una serie con la unidad puesta como rango, así que este "
        "test ya no vigila nada: o se han arreglado en el catálogo -y entonces "
        "sobra `sufijo`- o alguien ha cambiado el formato y hay que mirarlo"
    )
    for clave, d in rangos.items():
        assert d.sufijo is None, (
            f"{clave} tiene la unidad '{d.unidad}', que es un rango, y `sufijo` "
            f"lo devuelve igualmente: la portada escribirá "
            f"'85,50 {d.unidad}' detrás del valor"
        )

    for clave, d in S.DEFINICIONES.items():
        if clave in rangos:
            continue
        assert d.sufijo == d.unidad, (
            f"{clave} mide en '{d.unidad}' y `sufijo` se lo ha comido: el valor "
            f"saldrá desnudo y sin decir en qué está"
        )
