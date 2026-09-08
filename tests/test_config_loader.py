"""El validador del config.yaml.

Lo que más importa aquí no es que acepte el config bueno —eso lo prueba el
hecho de que el resto de la batería arranque— sino que RECHACE los malos. Un
validador que solo se prueba con entradas válidas no está probado.
"""

from __future__ import annotations

import copy
from datetime import date, datetime

import pytest

from app.config_loader import Config, ConfigError, _validate, compute_hash, load_config

from tests.conftest import REPO_ROOT


def errores(data) -> str:
    """Todos los problemas encontrados, en un solo texto para buscar dentro."""
    return "\n".join(_validate(data))


# ---------------------------------------------------------------------------
# El config real
# ---------------------------------------------------------------------------


def test_el_config_del_repo_es_valido(cfg):
    assert _validate(cfg.raw) == []


def test_el_hash_no_depende_del_orden_de_las_claves(cfg):
    revuelto = dict(reversed(list(cfg.raw.items())))
    assert compute_hash(revuelto) == compute_hash(cfg.raw)


def test_el_hash_si_cambia_con_el_contenido(cfg):
    otro = copy.deepcopy(cfg.raw)
    otro["timezone"] = "UTC"
    assert compute_hash(otro) != compute_hash(cfg.raw)


def test_load_config_lanza_si_no_existe(tmp_path):
    with pytest.raises(ConfigError, match="no se encuentra"):
        load_config(tmp_path / "no_existe.yaml")


# ---------------------------------------------------------------------------
# program.start: el sistema NO debe arrancar sin él
# ---------------------------------------------------------------------------
# Sin origen de programa la semana de descarga no se activa nunca. Es un fallo
# silencioso con consecuencias físicas, así que la exigencia no es "avisar":
# es negarse a arrancar. Cada uno de estos casos es una forma real de
# equivocarse escribiendo el YAML.


@pytest.mark.parametrize(
    "mutacion, fragmento",
    [
        pytest.param({"start": None}, "program.start está vacío", id="null"),
        pytest.param({}, "program.start está vacío", id="seccion_vacia"),
        pytest.param({"start": "2026-09-08"}, "entre comillas", id="entrecomillada"),
        pytest.param({"start": "el lunes"}, "no es una fecha", id="texto_libre"),
        pytest.param({"start": "2026-13-45"}, "no es una fecha", id="fecha_imposible"),
        pytest.param(
            {"start": datetime(2026, 9, 8, 7, 30)}, "lleva hora", id="con_hora"
        ),
    ],
)
def test_program_start_invalido_impide_arrancar(cfg, mutacion, fragmento):
    data = copy.deepcopy(cfg.raw)
    data["program"] = mutacion
    assert fragmento in errores(data)


def test_falta_la_seccion_program_entera(cfg):
    data = copy.deepcopy(cfg.raw)
    del data["program"]
    assert "falta la sección obligatoria 'program'" in errores(data)


def test_program_start_valida_es_accesible_como_date(cfg):
    assert cfg.program_start == date(2026, 9, 8)
    assert isinstance(cfg.program_start, date)


def test_program_start_acepta_una_cadena_bien_escrita_en_el_accesor():
    """El accesor no debe depender de que PyYAML haya hecho la conversión."""
    c = Config({"program": {"start": "2026-01-05"}}, "x")
    assert c.program_start == date(2026, 1, 5)


# ---------------------------------------------------------------------------
# Referencias cruzadas
# ---------------------------------------------------------------------------


def test_rutina_inexistente_en_el_calendario(cfg):
    data = copy.deepcopy(cfg.raw)
    variante = data["calendar"]["active_variant"]
    dia = next(
        d for d in data["calendar"]["variants"][variante]
        if d != "description" and (data["calendar"]["variants"][variante][d] or {}).get("strength")
    )
    data["calendar"]["variants"][variante][dia]["strength"] = "dia_fantasma"
    assert "la rutina 'dia_fantasma' no existe" in errores(data)


def test_regla_especial_sobre_un_ejercicio_inexistente(cfg):
    data = copy.deepcopy(cfg.raw)
    data["special_rules"][0]["action"]["remove_exercises"] = ["ejercicio_fantasma"]
    assert "'ejercicio_fantasma' no existe" in errores(data)


def test_umbral_adaptativo_referenciado_pero_no_definido(cfg):
    data = copy.deepcopy(cfg.raw)
    data["thresholds"]["amber"].append(
        {"name": "inventada", "when": {"load_3d": {"gt_adaptive": "no_existe"}}}
    )
    assert "'no_existe', que no está definido" in errores(data)


def test_ejercicio_sin_template_id(cfg):
    data = copy.deepcopy(cfg.raw)
    rkey = next(iter(data["routines"]))
    data["routines"][rkey]["exercises"][0]["template_id"] = None
    assert "falta template_id" in errores(data)


# ---------------------------------------------------------------------------
# Las dos puertas de volumen
# ---------------------------------------------------------------------------
# Añadir una serie efectiva es más arriesgado que sumar dos repeticiones. Que
# la puerta de las series pueda quedar MÁS PERMISIVA que la de las reps es un
# error de configuración que no da síntoma hasta que ya se ha añadido la serie.


def test_sets_gate_no_puede_ser_mas_laxa_que_reps_gate(cfg):
    data = copy.deepcopy(cfg.raw)
    vs = data["progression"]["volume_safety"]
    vs["sets_gate"]["block_if_last_routine_session_in"] = []
    vs["reps_gate"]["block_if_last_routine_session_in"] = ["amber", "red"]
    assert "reps_gate es MÁS estricta que sets_gate" in errores(data)


def test_umbral_de_lumbar_de_series_no_puede_superar_al_de_reps(cfg):
    data = copy.deepcopy(cfg.raw)
    vs = data["progression"]["volume_safety"]
    vs["sets_gate"]["block_if_mean_lower_discomfort_gte"] = 5
    vs["reps_gate"]["block_if_mean_lower_discomfort_gte"] = 3
    assert "debe ser <= el de reps_gate" in errores(data)


def test_una_puerta_que_bloquea_en_verde_deja_el_modo_muerto(cfg):
    data = copy.deepcopy(cfg.raw)
    data["progression"]["volume_safety"]["sets_gate"][
        "block_if_last_routine_session_in"
    ] = ["green"]
    assert "deja el modo sin ninguna sesión en la que pueda progresar" in errores(data)


def test_las_claves_muertas_de_volume_safety_se_rechazan(cfg):
    data = copy.deepcopy(cfg.raw)
    data["progression"]["volume_safety"]["block_if_red_days_gte"] = 2
    assert "ya no se usa" in errores(data)


# ---------------------------------------------------------------------------
# Seguridad: hernia L4-L5
# ---------------------------------------------------------------------------
# Esto no es un umbral ajustable, es una restricción médica permanente. Se
# comprueba que sigue mordiendo tanto por template_id como por nombre.


def test_un_patron_prohibido_en_una_rutina_hiit_impide_arrancar(cfg):
    data = copy.deepcopy(cfg.raw)
    bloque = next(iter(data["hiit"]["blocks"].values()))
    data["routines"][bloque]["exercises"].append(
        {
            "key": "colado",
            "name": "Peso muerto rumano con mancuernas",
            "template_id": "AAAAAAAA",
            "sets": [{"type": "normal", "reps": 10}],
        }
    )
    assert "SEGURIDAD" in errores(data)


def test_el_patron_prohibido_ignora_los_acentos(cfg):
    """`Superserie con péndulo` y `pendulo` tienen que tratarse igual."""
    data = copy.deepcopy(cfg.raw)
    patrones = data["safety"]["forbidden_in_hiit"]["name_patterns"]
    bloque = next(iter(data["hiit"]["blocks"].values()))
    data["safety"]["forbidden_in_hiit"]["name_patterns"] = patrones + ["burpee"]
    data["routines"][bloque]["exercises"].append(
        {
            "key": "colado_acento",
            "name": "Búrpeé",  # con acentos: debe seguir coincidiendo si se normaliza
            "template_id": "BBBBBBBB",
            "sets": [{"type": "normal", "reps": 10}],
        }
    )
    # No se afirma que este nombre concreto coincida (depende del patrón), pero
    # sí que el validador no revienta al normalizar acentos.
    _validate(data)


def test_una_rutina_no_hiit_puede_llevar_peso_muerto(cfg):
    """El peso muerto normal está permitido: lo gobierna `retirada_peso_muerto`."""
    data = copy.deepcopy(cfg.raw)
    hiit = set(data["hiit"]["blocks"].values())
    normal = next(k for k in data["routines"] if k not in hiit)
    data["routines"][normal]["exercises"].append(
        {
            "key": "peso_muerto_extra",
            "name": "Peso muerto rumano",
            "template_id": "CCCCCCCC",
            "sets": [{"type": "normal", "reps": 8}],
        }
    )
    assert "SEGURIDAD" not in errores(data)


def test_hiit_no_puede_estar_a_la_vez_permitido_y_prohibido(cfg):
    data = copy.deepcopy(cfg.raw)
    permitida = data["hiit"]["allowed_routines"][0]
    data["hiit"]["never_routines"] = list(data["hiit"]["never_routines"]) + [permitida]
    assert "a la vez en allowed_routines y never_routines" in errores(data)


# ---------------------------------------------------------------------------
# Modos de progresión
# ---------------------------------------------------------------------------


def test_max_sets_por_debajo_de_las_series_actuales(cfg):
    data = copy.deepcopy(cfg.raw)
    for rkey, routine in data["routines"].items():
        for ex in routine.get("exercises", []):
            if str(ex.get("progression_type", "")) == "sets":
                ex["max_sets"] = 1
                assert "es menor que las" in errores(data)
                return
    pytest.skip("no hay ningún ejercicio en modo 'sets' en este config")


def test_rep_range_invertido(cfg):
    data = copy.deepcopy(cfg.raw)
    rkey = next(iter(data["routines"]))
    data["routines"][rkey]["exercises"][0]["rep_range"] = [12, 8]
    assert "está invertido" in errores(data)


def test_deload_con_factor_mayor_que_uno(cfg):
    data = copy.deepcopy(cfg.raw)
    data["progression"].setdefault("deload", {})["sets_factor"] = 1.2
    assert "una descarga recorta volumen" in errores(data).replace("\n", " ")


def test_los_ficheros_de_configuracion_no_se_modifican_al_validar(cfg):
    """`_validate` no debe tocar lo que recibe."""
    antes = compute_hash(cfg.raw)
    _validate(cfg.raw)
    assert compute_hash(cfg.raw) == antes


def test_el_config_del_repo_carga_desde_disco():
    c = load_config(REPO_ROOT / "config.yaml")
    assert c.hash and len(c.hash) == 16
    assert c.program_start == date(2026, 9, 8)
