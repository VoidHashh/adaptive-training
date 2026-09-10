"""Interfaz de línea de órdenes: solo las partes puras.

Lo que se prueba aquí es el parseo del check-in, que es donde estaba el fallo
más traicionero del proyecto: una errata (`fatige=5`) se descartaba en
silencio y el resultado solo se notaba como "sin datos para evaluar", sin que
nada apuntase a la causa. Un check-in que se ignora sin decirlo es peor que
uno que no se escribe.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.cli import CHECKIN_ALIAS, _completitud, checkin_help, parse_checkin
from app.engine.signals import DayMetrics

from tests.conftest import LUNES, dias


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
