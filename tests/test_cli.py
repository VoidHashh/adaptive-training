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

from app.cli import CHECKIN_ALIAS, checkin_help, parse_checkin

from tests.conftest import LUNES


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
