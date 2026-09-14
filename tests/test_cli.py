"""Interfaz de línea de órdenes: solo las partes puras.

Lo que se prueba aquí es el parseo del check-in, que es donde estaba el fallo
más traicionero del proyecto: una errata (`fatige=5`) se descartaba en
silencio y el resultado solo se notaba como "sin datos para evaluar", sin que
nada apuntase a la causa. Un check-in que se ignora sin decirlo es peor que
uno que no se escribe.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.cli import CHECKIN_ALIAS, _completitud, checkin_help, parse_checkin
from app.engine.signals import DayMetrics

from tests.conftest import LUNES, dias
from tests.dobles import doble_de
from app.integrations.garmin import GarminClient


@pytest.fixture
def claves(cfg) -> set[str]:
    return set(cfg.slider_keys())


# ---------------------------------------------------------------------------
# Parseo
# ---------------------------------------------------------------------------


def test_sin_texto_no_hay_checkin(claves):
    assert parse_checkin(None, LUNES, claves) is None
    assert parse_checkin("", LUNES, claves) is None


def test_se_parsean_los_nombres_largos(claves):
    c = parse_checkin("lower_discomfort=2,fatigue=5", LUNES, claves)
    assert c is not None
    assert c.date == LUNES
    assert c.values == {"lower_discomfort": 2, "fatigue": 5}


def test_los_alias_cortos_se_traducen(claves):
    c = parse_checkin("lower=2,ganas=8,rpe=6", LUNES, claves)
    assert c.values == {
        "lower_discomfort": 2,
        "training_desire": 8,
        "yesterday_rpe": 6,
    }


def test_los_espacios_sobrantes_no_estorban(claves):
    c = parse_checkin("  lower = 2 , fatiga=5 ,, ", LUNES, claves)
    assert c.values == {"lower_discomfort": 2, "fatigue": 5}


def test_una_clave_desconocida_es_un_error_duro_no_un_valor_ignorado(claves):
    with pytest.raises(SystemExit) as exc:
        parse_checkin("fatige=5", LUNES, claves)
    mensaje = str(exc.value)
    assert "fatige" in mensaje
    assert "Válidos:" in mensaje, "el error tiene que decir qué SÍ vale"


def test_un_alias_de_una_señal_que_no_existe_en_este_config_tambien_falla():
    """Los alias no son un pase libre: la clave resultante se valida igual."""
    with pytest.raises(SystemExit):
        parse_checkin("lower=2", LUNES, valid_keys={"fatigue"})


def test_un_par_sin_igual_es_un_error(claves):
    with pytest.raises(SystemExit, match="mal escrito"):
        parse_checkin("lower", LUNES, claves)


def test_un_valor_que_no_es_entero_es_un_error(claves):
    with pytest.raises(SystemExit, match="no es un número entero"):
        parse_checkin("lower=mucho", LUNES, claves)


def test_los_siete_deslizadores_se_aceptan_a_la_vez(cfg, claves):
    """La sintaxis completa que documenta `--checkin-help`.

    Son SIETE, no seis: fatiga, ánimo, molestia superior, molestia lumbar,
    calidad del sueño, ganas de entrenar y RPE de ayer.
    """
    assert len(claves) == 7
    c = parse_checkin(
        "fatigue=4,mood=7,upper=1,lower=2,sleep=7,desire=8,rpe=6", LUNES, claves
    )
    assert set(c.values) == claves


def test_la_sintaxis_larga_tambien_cubre_los_siete(cfg, claves):
    texto = ",".join(f"{k}=5" for k in sorted(claves))
    c = parse_checkin(texto, LUNES, claves)
    assert set(c.values) == claves


# ---------------------------------------------------------------------------
# Ayuda
# ---------------------------------------------------------------------------


def test_la_ayuda_se_genera_del_yaml_no_de_una_lista_a_mano(cfg):
    """Una ayuda escrita a mano se queda obsoleta el día que se añade un
    deslizador, y entonces enseña a escribir check-ins que el motor ignora."""
    texto = checkin_help(cfg)
    for k in cfg.slider_keys():
        assert k in texto


def test_la_ayuda_incluye_una_linea_de_ejemplo_valida(cfg, claves):
    """El ejemplo que se le enseña al usuario tiene que funcionar de verdad."""
    texto = checkin_help(cfg)
    ejemplo = next(
        l.strip().split('"')[1]
        for l in texto.splitlines()
        if l.strip().startswith('--checkin "')
    )
    c = parse_checkin(ejemplo, LUNES, claves)
    assert c is not None and c.values


def test_todos_los_alias_apuntan_a_deslizadores_reales(claves):
    """Un alias hacia una señal inexistente sería una trampa de erratas.

    Es exactamente lo que había: la tabla contenía `stress` y `soreness`, que
    no son deslizadores de este config.
    """
    huerfanos = sorted(v for v in set(CHECKIN_ALIAS.values()) if v not in claves)
    assert huerfanos == [], f"alias que no llevan a ningún deslizador: {huerfanos}"


def test_todos_los_deslizadores_tienen_al_menos_una_forma_de_escribirse(claves):
    alcanzables = set(CHECKIN_ALIAS.values()) | claves
    assert claves <= alcanzables


# ---------------------------------------------------------------------------
# Completitud del wellness
# ---------------------------------------------------------------------------
#
# El aviso saltaba SOLO cuando una métrica venía a cero. Con 4 de 7 días de HRV
# no decía nada, y sin embargo por debajo de `baseline.min_days_required` no
# hay línea base: `hrv_ratio` no existe y las reglas que lo usan no se evalúan.
# El informe se leía igual que el de una semana completa.


def ventana(n: int, con_hrv: int, **resto) -> list[DayMetrics]:
    """`n` días de los cuales `con_hrv` traen HRV."""
    return [
        DayMetrics(date=LUNES, hrv=100.0 if i < con_hrv else None, **resto)
        for i in range(n)
    ]


def test_una_metrica_a_cero_sigue_avisando(cfg):
    _, avisos = _completitud(ventana(7, 0), cfg)
    assert any("NINGÚN día trajo HRV" in a for a in avisos)


def test_por_debajo_del_minimo_de_la_linea_base_tambien_avisa(cfg):
    """El caso que faltaba: hay dato, pero no hay contra qué compararlo."""
    _, avisos = _completitud(ventana(7, 3), cfg)
    hrv = [a for a in avisos if "HRV" in a]
    assert hrv, f"3/7 días de HRV pasó sin aviso: {avisos}"
    assert "3/7" in hrv[0]
    assert "línea base" in hrv[0]


def test_el_minimo_sale_del_yaml_no_de_una_constante(cfg_copia):
    """Si se sube `min_days_required`, el aviso tiene que subir con él."""
    cfg_copia.raw["baseline"]["min_days_required"] = 6
    _, avisos = _completitud(ventana(7, 5), cfg_copia)
    assert any("necesita 6" in a for a in avisos)


def test_un_hueco_suelto_se_menciona_sin_alarmar(cfg):
    _, avisos = _completitud(ventana(7, 6), cfg)
    hrv = [a for a in avisos if "HRV" in a]
    assert hrv and "falta 1 de 7" in hrv[0]
    assert "línea base" not in hrv[0], "6/7 no rompe la línea base"


def test_una_semana_completa_no_genera_ningun_aviso(cfg):
    metrics = dias(LUNES, 7, hrv=100.0, rhr=50.0, sleep_min=420,
                   sleep_score=80, body_battery=70)
    detalle, avisos = _completitud(metrics, cfg)
    assert avisos == []
    assert "HRV 7/7" in detalle[0]


def test_sin_config_no_revienta(cfg):
    """`_completitud` se llama antes de que el config esté garantizado."""
    _, avisos = _completitud(ventana(7, 3), None)
    assert any("HRV" in a for a in avisos)


# ---------------------------------------------------------------------------
# El ensayo predice sobre la base que va a existir, no sobre la que hay
# ---------------------------------------------------------------------------
#
# `estado_para_el_ensayo` leía la base tal cual y, si la lectura fallaba, seguía
# EN FRÍO diciéndolo. Decirlo no arreglaba nada: en frío no hay reglas activas y
# todas las rachas valen cero, o sea que el ensayo predice DE MENOS, que es el
# error que el propio módulo señala como el más difícil de detectar porque nunca
# sorprende.
#
# Y el motivo por el que la lectura fallaba no era una avería: era una columna
# nueva en `models.py` que todavía no estaba en `data/app.db`. La aplicación la
# añade al arrancar (`init_db` -> `ensure_schema`), así que el ensayo estaba
# prediciendo sobre una base que iba a dejar de existir en cuanto se levantara
# el contenedor. Pasó de verdad, con las dos columnas de la persistencia de la
# progresión.


@pytest.fixture
def base_desfasada(tmp_path, monkeypatch):
    """Una base real a la que le falta una columna, como la de antes de migrar."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import db as appdb
    from app.models import Base
    from app.settings import settings

    ruta = tmp_path / "vieja.db"
    eng = create_engine(f"sqlite:///{ruta}", future=True)
    Base.metadata.create_all(eng)

    # SQLite no sabe borrar una columna: se rehace la tabla sin ella.
    with eng.begin() as c:
        c.exec_driver_sql("ALTER TABLE exercise_targets RENAME TO viejo")
        c.exec_driver_sql(
            "CREATE TABLE exercise_targets ("
            "id INTEGER PRIMARY KEY, routine_key VARCHAR, exercise_key VARCHAR)"
        )
        c.exec_driver_sql("DROP TABLE viejo")

    monkeypatch.setattr(appdb, "engine", eng)
    monkeypatch.setattr(appdb, "SessionLocal", sessionmaker(bind=eng, future=True))
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{ruta}")
    return ruta


def test_el_ensayo_pone_al_dia_el_esquema_en_vez_de_salir_en_frio(base_desfasada, cfg):
    from app.cli import estado_para_el_ensayo

    _estado, nota = estado_para_el_ensayo(cfg)
    assert "NO SE PUDO LEER" not in nota, (
        "una columna que la aplicación añade al arrancar no puede dejar el "
        "ensayo prediciendo en frío"
    )
    assert "leído de la base de datos" in nota


def test_el_ensayo_no_se_inventa_una_base_de_datos_que_no_existe(tmp_path, cfg, monkeypatch):
    """Poner al día no es lo mismo que crear.

    Migrar un fichero que ya está es continuar lo que la aplicación hace al
    arrancar; fabricarlo por mirar un informe es dejar rastro donde no había
    nada.
    """
    from app.cli import estado_para_el_ensayo
    from app.settings import settings

    ruta = tmp_path / "no_existe.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{ruta}")

    _estado, nota = estado_para_el_ensayo(cfg)
    assert "sin base de datos" in nota
    assert not ruta.exists(), "el ensayo ha creado una base de datos"


@pytest.fixture
def base_sin_columnas_de_sueltos(tmp_path, monkeypatch):
    """Una base real anterior a las columnas de entrenamientos fuera del plan."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import db as appdb
    from app.models import Base
    from app.settings import settings

    ruta = tmp_path / "antes_de_los_sueltos.db"
    eng = create_engine(f"sqlite:///{ruta}", future=True)
    Base.metadata.create_all(eng)

    with eng.begin() as c:
        c.exec_driver_sql("ALTER TABLE workout_log RENAME TO viejo")
        c.exec_driver_sql(
            "CREATE TABLE workout_log ("
            "id INTEGER PRIMARY KEY, hevy_workout_id VARCHAR, date DATE, "
            "routine_key VARCHAR, title VARCHAR, all_sets_at_target BOOLEAN)"
        )
        c.exec_driver_sql("DROP TABLE viejo")

    monkeypatch.setattr(appdb, "engine", eng)
    monkeypatch.setattr(appdb, "SessionLocal", sessionmaker(bind=eng, future=True))
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{ruta}")
    return ruta


def test_el_ensayo_lee_lo_entrenado_sin_depender_de_quien_migre_antes(
    base_sin_columnas_de_sueltos, cfg
):
    """El presupuesto de intensas no puede salir a cero por el orden de dos líneas.

    `sesiones_para_el_ensayo` se apoyaba en que `estado_para_el_ensayo` hubiera
    puesto el esquema al día, y `main` la llama DESPUÉS. Con las columnas nuevas
    de `workout_log` el primer ensayo sobre la base antigua reventaba la lectura,
    la cazaba y anunciaba el presupuesto a cero: margen para una salida intensa
    que en producción ya estaba gastado. La segunda ejecución salía bien, que es
    la peor manera posible de fallar.
    """
    from app.cli import sesiones_para_el_ensayo

    _sesiones, nota = sesiones_para_el_ensayo(cfg, date(2026, 9, 14))
    assert "NO SE PUDO LEER" not in nota, (
        "una columna que la aplicación añade al arrancar no puede dejar el "
        f"presupuesto de intensas a cero: {nota}"
    )
    assert "sesión(es) en 14 días" in nota


def test_el_ensayo_no_se_inventa_una_base_para_mirar_lo_entrenado(
    tmp_path, cfg, monkeypatch
):
    from app.cli import sesiones_para_el_ensayo
    from app.settings import settings

    ruta = tmp_path / "no_existe.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{ruta}")

    sesiones, nota = sesiones_para_el_ensayo(cfg, date(2026, 9, 14))
    assert sesiones == []
    assert "sin base de datos" in nota
    assert not ruta.exists(), "el ensayo ha creado una base de datos"


# --- el histórico de check-ins del ensayo ----------------------------------
#
# Tercera vez la misma historia. El ensayo en seco es lo único que se mira antes
# de dejar que el sistema escriba solo, así que cada dato que el ensayo no lee
# es un sitio donde el ensayo dice una cosa y la mañana hace otra. Hoy la
# diferencia es cero porque ningún umbral adaptativo mira un deslizador; se
# conecta ahora precisamente por eso, porque conectarlo cuando ya importa
# significa descubrir el desajuste con las reglas nuevas puestas y no saber cuál
# de las dos cosas está mal.


def test_el_ensayo_lee_el_historico_de_checkins(base_sin_columnas_de_sueltos, cfg):
    from app.cli import historial_para_el_ensayo
    from app.db import SessionLocal
    from app.repository import upsert_checkin

    dia = date(2026, 9, 14)
    with SessionLocal() as s:
        for i in range(1, 6):
            upsert_checkin(s, dia - timedelta(days=i), {"fatigue": 3}, config=cfg)
        s.commit()

    hist, nota = historial_para_el_ensayo(dia)
    assert len(hist) == 5, nota
    assert "5 check-in(s)" in nota


def test_el_ensayo_no_mete_el_dia_de_hoy_en_el_historico(
    base_sin_columnas_de_sueltos, cfg
):
    """Igual que la mañana: hoy entra por `checkin`, no por la serie."""
    from app.cli import historial_para_el_ensayo
    from app.db import SessionLocal
    from app.repository import upsert_checkin

    dia = date(2026, 9, 14)
    with SessionLocal() as s:
        upsert_checkin(s, dia, {"fatigue": 9}, config=cfg)
        s.commit()

    hist, nota = historial_para_el_ensayo(dia)
    assert hist == []
    assert "sin check-ins anteriores" in nota


def test_el_ensayo_no_se_inventa_una_base_para_el_historico(tmp_path, monkeypatch):
    from app.cli import historial_para_el_ensayo
    from app.settings import settings

    ruta = tmp_path / "no_existe.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{ruta}")

    hist, nota = historial_para_el_ensayo(date(2026, 9, 14))
    assert hist == []
    assert "sin base de datos" in nota
    assert not ruta.exists(), "el ensayo ha creado una base de datos"


# ---------------------------------------------------------------------------
# La orden manual pide la misma ventana de salidas que el trabajo de madrugada
# ---------------------------------------------------------------------------
#
# `RIDE_HISTORY_DAYS = 190` estaba escrito DOS veces, aquí y en el planificador,
# mientras `cycling.fetch` declaraba en el YAML 10 y 90. Al conectar la opción
# había que conectar los dos sitios: si el planificador respeta la ventana y la
# orden manual sigue pidiendo 190, el usuario que compara la decisión a mano se
# come el 429 y luego el trabajo de las 06:30 se queda sin histórico. El coste
# de una constante duplicada no lo paga quien la escribe.


@pytest.fixture
def garmin_espiado(monkeypatch):
    """Sustituye Garmin y devuelve el diccionario con lo que se le pidió."""
    from app.integrations import garmin as gmod

    visto: dict = {}

    @doble_de(GarminClient)
    class ClienteFalso:
        # `rate_limit_events` y `fetch_errors` estaban aquí como listas DE
        # CLASE, compartidas por todas las instancias. `GarminClient` las
        # declara con `field(default_factory=list)`, o sea una por instancia.
        # Van en `__init__` por eso y porque una lista de clase en un doble
        # arrastra lo que hizo un test al siguiente.
        session_resumed = False

        def __init__(self):
            self.rate_limit_events: list = []
            self.fetch_errors: list = []

        def connect(self):
            return None

        def window(self, day, days=7, ride_days=None):
            visto.update(days=days, ride_days=ride_days)
            return dias(day, days, hrv=100.0, rhr=50.0), []

    monkeypatch.setattr(gmod, "build_client", lambda *a, **k: ClienteFalso())
    return visto


def cache_falsa(monkeypatch, cache):
    from app.integrations import activity_cache as ac

    monkeypatch.setattr(ac, "load_cached_rides", lambda *a, **k: cache)


def test_la_orden_manual_usa_la_ventana_corta_del_yaml(cfg, monkeypatch, garmin_espiado):
    from app.cli import fetch_garmin
    from tests.test_activity_cache import cache_de

    cache_falsa(monkeypatch, cache_de(180, day=LUNES))
    fetch_garmin(LUNES, 8, True, cfg)
    assert garmin_espiado["ride_days"] == cfg.raw["cycling"]["fetch"]["lookback_days"]


def test_sin_cache_la_orden_manual_se_trae_el_historico_entero(
    cfg, monkeypatch, garmin_espiado
):
    """`--no-cache` no deja el objeto de caché en None y ya está: sin caché no
    hay nada en disco que fusionar, así que esta petición es la única fuente."""
    from app.cli import fetch_garmin

    fetch_garmin(LUNES, 8, False, cfg)
    assert garmin_espiado["ride_days"] == cfg.raw["cycling"]["fetch"]["backfill_days"]


def test_el_informe_dice_por_que_se_pidio_esa_ventana(cfg, monkeypatch, garmin_espiado):
    """Un backfill que se repite cada mañana es un síntoma, no una casualidad.

    Si el motivo no se imprime, la única señal de que la caché no se está
    escribiendo es la factura de peticiones a Garmin.
    """
    from app.cli import fetch_garmin
    from tests.test_activity_cache import cache_de

    cache_falsa(monkeypatch, cache_de(180, day=LUNES))
    _m, _r, proc = fetch_garmin(LUNES, 8, True, cfg)
    linea = [d for d in proc.detalle if "ventana de salidas" in d]
    assert linea, f"el informe no dice qué ventana se pidió: {proc.detalle}"
    assert "10" in linea[0]
