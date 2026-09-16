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
from app.engine.tendencia import senales_de_regla
from app.integrations.activity_cache import dias_adaptativos

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
# Erratas: la lección del `fatige=5` aplicada al YAML
# ---------------------------------------------------------------------------
#
# Una clave mal escrita aquí no daba error. Se leía con `.get(clave, defecto)`
# y el defecto decidía en su lugar, sin decir nada. Los tres casos de abajo son
# reales, no hipotéticos: los tres cambian el entrenamiento y ninguno avisaba.


def _regla(data, nombre) -> dict:
    return next(r for r in data["special_rules"] if r["name"] == nombre)


# --- la errata de nivel de arriba, que era la única que no se miraba -------


@pytest.mark.parametrize(
    "buena,errata",
    [
        ("special_rules", "special_rule"),
        ("adaptive_thresholds", "adaptative_thresholds"),
        ("recovery_blocks", "recovery_block"),
        ("set_types", "sets_types"),
        ("notifications", "notification"),
        ("safety", "safty"),
    ],
)
def test_una_seccion_mal_escrita_no_se_ignora_entera(cfg_copia, buena, errata):
    """Escribir mal el nombre de una sección la borraba, en silencio.

    `special_rule:` en vez de `special_rules:` arrancaba limpio y con cero
    reglas especiales: el sistema decidía todos los días sin mirar ninguna. No
    hay forma de notarlo desde fuera salvo echar de menos un ámbar que nunca
    llega, y eso tarda semanas y se confunde con estar recuperado.

    Es el fallo más caro de la familia porque una sección es exactamente donde
    se escribe lo que NO es el comportamiento por defecto.
    """
    cfg_copia.raw[errata] = cfg_copia.raw.pop(buena)
    msg = errores(cfg_copia.raw)
    assert errata in msg
    assert buena in msg, "el error tiene que decir cuál era la buena"


def test_la_seccion_rules_se_rechaza_por_no_leerla_nadie(cfg_copia):
    """`rules` tenía validación propia y ningún lector.

    Escribirla en vez de `special_rules` pasaba la validación con nota -se le
    revisaban hasta los operadores de sus `when`- y no hacía nada. Un validador
    que revisa a conciencia una sección muerta es peor que uno que no la mira:
    certifica por escrito que está bien puesta.
    """
    cfg_copia.raw["rules"] = cfg_copia.raw["special_rules"]
    assert "rules" in errores(cfg_copia.raw)


def test_cold_start_se_rechaza_en_vez_de_ignorarse(cfg_copia):
    """Prometía por escrito un aviso que nadie daba.

    `notify: true` se lee como una garantía. El aviso existe -y además es
    incondicional-, pero no porque lo dijera esta clave. Que coincidieran era
    suerte: poner `notify: false` no habría callado nada.
    """
    cfg_copia.raw["cold_start"] = {"backfill_days": 30, "notify": True}
    msg = errores(cfg_copia.raw)
    assert "cold_start" in msg
    assert "baseline.window_days" in msg, "hay que decir dónde vive ahora"


def test_una_version_futura_no_se_lee_con_las_claves_de_la_vieja(cfg_copia):
    cfg_copia.raw["version"] = 2
    assert "version" in errores(cfg_copia.raw)


def test_una_errata_en_duration_days_no_pasa_desapercibida(cfg_copia):
    """`durantion_days` dejaba la retirada de peso muerto en 1 día en vez de 14.

    Catorce días sin peso muerto es una decisión de seguridad; un día es no
    hacer nada. La diferencia era una letra.
    """
    accion = _regla(cfg_copia.raw, "retirada_peso_muerto")["action"]
    accion["durantion_days"] = accion.pop("duration_days")
    msg = errores(cfg_copia.raw)
    assert "durantion_days" in msg
    assert "duration_days" in msg, "el error tiene que decir cuál era la buena"


def test_una_errata_en_every_n_weeks_no_apaga_la_descarga_en_silencio(cfg_copia):
    """Mismo desenlace que un `program.start` vacío: la descarga no se programa
    nunca. Ese ya era error duro; este se colaba."""
    trig = _regla(cfg_copia.raw, "semana_de_descarga")["trigger"]
    trig["every_n_week"] = trig.pop("every_n_weeks")
    assert "every_n_week" in errores(cfg_copia.raw)


def test_una_errata_en_el_factor_de_reduccion_no_deja_el_recorte_en_nada(cfg_copia):
    """Sin `factor`, `session_builder` usa 1.0: la regla dice que reduce carga
    y no reduce nada."""
    rl = _regla(cfg_copia.raw, "descarga_press_hombro")["action"]["reduce_load"]
    rl["factorr"] = rl.pop("factor")
    assert "factorr" in errores(cfg_copia.raw)


@pytest.mark.parametrize("operador", ["gtee", "mayor_que", "=>", "gte_adaptativo"])
def test_un_operador_desconocido_se_rechaza_al_arrancar(cfg_copia, operador):
    """En un freno no saltaba nunca; en un disparador saltaba todos los días.
    Dos desenlaces opuestos para la misma errata, ninguno visible."""
    cfg_copia.raw["progression"]["brakes"][1]["when"] = {operador: 4}
    assert "operador desconocido" in errores(cfg_copia.raw)


def test_los_operadores_adaptativos_siguen_siendo_validos(cfg_copia):
    """`gte_adaptive` es legítimo: compara contra la distribución propia."""
    cfg_copia.raw["progression"]["brakes"][1]["when"] = {"gte_adaptive": "load_2d_p90"}
    assert "operador desconocido" not in errores(cfg_copia.raw)


def test_un_when_vacio_no_compara_nada_y_se_rechaza(cfg_copia):
    cfg_copia.raw["progression"]["brakes"][1]["when"] = {}
    assert "no compara nada" in errores(cfg_copia.raw)


def test_una_clave_inventada_en_un_freno_se_rechaza(cfg_copia):
    cfg_copia.raw["progression"]["brakes"][1]["bloks"] = "all"
    assert "bloks" in errores(cfg_copia.raw)


def test_on_missing_solo_admite_block_o_skip(cfg_copia):
    cfg_copia.raw["progression"]["brakes"][0]["on_missing"] = "ignorar"
    assert "on_missing" in errores(cfg_copia.raw)


# ---------------------------------------------------------------------------
# Nombres de señal que no fabrica nadie
#
# El motor resuelve cada señal con `signals.get(nombre)`, así que una errata no
# revienta: devuelve `None`, que todo el sistema lee como «hoy no hay dato». El
# freno queda escrito, comentado y validado, y se salta todas las mañanas por
# falta de datos. En el log eso es indistinguible de un día sin sincronizar el
# reloj.
#
# Pasó de verdad, por el otro lado del `get`: `yesterday_routine` se nombraba
# en el motor, no la producía nadie, y `last_session_only` bloqueó las tres
# rutinas durante toda la vida del sistema sin decirlo.
# ---------------------------------------------------------------------------


def test_una_errata_en_la_señal_de_un_freno_no_arranca(cfg_copia):
    cfg_copia.raw["progression"]["brakes"][0]["source"] = "yesterday_rpee"
    assert "no la produce nadie" in errores(cfg_copia.raw)


def test_el_error_de_señal_propone_los_nombres_parecidos(cfg_copia):
    """Una errata se arregla sola si el mensaje enseña el nombre bueno."""
    cfg_copia.raw["progression"]["brakes"][0]["source"] = "yesterday_rpee"
    assert "yesterday_rpe" in errores(cfg_copia.raw)


def test_una_errata_en_la_señal_de_una_regla_del_semáforo_no_arranca(cfg_copia):
    regla = cfg_copia.raw["thresholds"]["amber"][0]
    regla["when"] = {"hrv_ratioo": {"lt": 0.9}}
    msg = errores(cfg_copia.raw)
    assert "no la produce nadie" in msg
    assert "hrv_ratioo" in msg


def test_una_errata_en_el_disparador_de_una_regla_especial_no_arranca(cfg_copia):
    _regla(cfg_copia.raw, "retirada_peso_muerto")["trigger"]["source"] = "lower_discomfor"
    assert "no la produce nadie" in errores(cfg_copia.raw)


def test_last_session_only_sin_la_señal_que_lo_acota_no_arranca(cfg_copia, monkeypatch):
    """El estado exacto en el que estuvo el sistema hasta que se produjo
    `yesterday_routine`: el YAML pedía acotar el freno a una rutina y el motor
    no tenía con qué, así que bloqueaba las tres. Si la señal se vuelve a
    perder, que no arranque en vez de volver a bloquear de más en silencio."""
    from app.engine import signals as mod

    monkeypatch.setattr(mod, "SENALES_FIJAS", mod.SENALES_FIJAS - {"yesterday_routine"})
    freno = next(
        b for b in cfg_copia.raw["progression"]["brakes"]
        if b.get("blocks") == "last_session_only"
    )
    msg = errores(cfg_copia.raw)
    assert "bloquearía TODAS" in msg
    assert freno["name"] in msg


def test_el_recuento_de_intensas_se_puede_nombrar_mientras_esté_encendido(cfg_copia):
    cfg_copia.raw["thresholds"]["amber"][0]["when"] = {"intense_count_7d": {"gte": 3}}
    assert "no la produce nadie" not in errores(cfg_copia.raw)


def test_nombrar_el_recuento_de_intensas_apagado_no_arranca(cfg_copia):
    """Apagar el bloque deja de escribir la señal. Una regla que la nombre
    seguiría en el fichero, sin dar error, sin saltar nunca."""
    cfg_copia.raw["thresholds"]["amber"][0]["when"] = {"intense_count_7d": {"gte": 3}}
    cfg_copia.raw["cycling"]["recommendation"]["intensity_count"]["enabled"] = False
    assert "no la produce nadie" in errores(cfg_copia.raw)


def test_cambiar_la_ventana_del_recuento_invalida_el_nombre_viejo(cfg_copia):
    """La ventana va dentro del nombre de la señal. Cambiarla en el config y
    dejar la regla apuntando a `intense_count_7d` es la misma avería."""
    cfg_copia.raw["thresholds"]["amber"][0]["when"] = {"intense_count_7d": {"gte": 3}}
    cfg_copia.raw["cycling"]["recommendation"]["intensity_count"]["window_days"] = 10
    assert "no la produce nadie" in errores(cfg_copia.raw)


def test_un_disparador_no_puede_ser_por_señal_y_por_calendario_a_la_vez(cfg_copia):
    _regla(cfg_copia.raw, "semana_de_descarga")["trigger"]["source"] = "fatigue"
    assert "solo uno de los dos" in errores(cfg_copia.raw)


def test_un_disparador_sin_señal_ni_calendario_se_rechaza(cfg_copia):
    trig = _regla(cfg_copia.raw, "retirada_peso_muerto")["trigger"]
    trig.pop("source")
    assert "por señal" in errores(cfg_copia.raw)


def test_una_regla_especial_sin_duration_days_se_rechaza(cfg_copia):
    """El defecto silencioso era 1 día."""
    _regla(cfg_copia.raw, "retirada_peso_muerto")["action"].pop("duration_days")
    assert "duration_days" in errores(cfg_copia.raw)


@pytest.mark.parametrize("factor", [0, 1.5, -0.5])
def test_una_regla_de_recorte_no_puede_subir_la_carga(cfg_copia, factor):
    _regla(cfg_copia.raw, "semana_de_descarga")["action"]["load_factor"] = factor
    assert "recortan carga" in errores(cfg_copia.raw)


def test_sin_seccion_set_types_no_se_arranca(cfg_copia):
    """Faltando la sección, la heurística se activaba con su defecto y convertía
    en calentamiento la primera serie de todo ejercicio de 4+ series. Eso mueve
    el cumplimiento, el recorte del ámbar y la progresión de volumen a la vez.
    """
    cfg_copia.raw.pop("set_types")
    assert "set_types" in errores(cfg_copia.raw)


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
        pytest.param({"start": "2026-09-14"}, "entre comillas", id="entrecomillada"),
        pytest.param({"start": "el lunes"}, "no es una fecha", id="texto_libre"),
        pytest.param({"start": "2026-13-45"}, "no es una fecha", id="fecha_imposible"),
        pytest.param(
            {"start": datetime(2026, 9, 14, 7, 30)}, "lleva hora", id="con_hora"
        ),
        # Fechas válidas y bien escritas, pero a media semana. `_deload` cuenta
        # desde el lunes de esa semana, así que el origen de verdad no sería el
        # que pone el YAML. Los siete días, para que quede fijado que el único
        # que pasa es el lunes y no "cualquiera menos el martes".
        pytest.param({"start": date(2026, 9, 15)}, "tiene que ser LUNES", id="martes"),
        pytest.param(
            {"start": date(2026, 9, 16)}, "tiene que ser LUNES", id="miercoles"
        ),
        pytest.param({"start": date(2026, 9, 17)}, "tiene que ser LUNES", id="jueves"),
        pytest.param({"start": date(2026, 9, 18)}, "tiene que ser LUNES", id="viernes"),
        pytest.param({"start": date(2026, 9, 19)}, "tiene que ser LUNES", id="sabado"),
        pytest.param({"start": date(2026, 9, 20)}, "tiene que ser LUNES", id="domingo"),
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
    assert cfg.program_start == date(2026, 9, 14)
    assert isinstance(cfg.program_start, date)


def test_el_program_start_del_config_real_es_lunes(cfg):
    """Control negativo de los seis casos de arriba.

    Si la regla del lunes estuviera mal escrita al revés -rechazando lunes en
    vez de aceptarlo- aquellos seis seguirían en verde y nadie se enteraría
    hasta el arranque.
    """
    assert cfg.program_start.weekday() == 0, cfg.program_start
    data = copy.deepcopy(cfg.raw)
    assert "tiene que ser LUNES" not in errores(data)


def test_el_error_del_lunes_dice_qué_día_es_y_cuál_sería_el_lunes(cfg):
    """El mensaje tiene que traer la fecha que hay que escribir, ya calculada.

    Un error que solo dice "tiene que ser lunes" obliga a mirar un calendario
    para arreglarlo, y quien lo mire puede contar mal justo como contó mal al
    escribir la fecha. El sábado 2026-09-19 pertenece a la semana del lunes
    2026-09-14: ese es el número que el sistema va a usar de verdad.

    Se busca dentro de LA LÍNEA del lunes y no en el texto entero. Escrito
    contra el texto entero pasaba sin comprobar nada: mover `start` al 19
    dispara además el cruce con `recalibrado_el`, cuyo mensaje ya menciona el
    14 por su cuenta, así que la aserción se cumplía sola aunque el cálculo del
    lunes estuviera roto. Lo cazó `scripts/check_lunes.py`.
    """
    data = copy.deepcopy(cfg.raw)
    data["program"] = dict(data["program"], start=date(2026, 9, 19))
    linea = next(p for p in _validate(data) if "tiene que ser LUNES" in p)
    assert "es sábado" in linea
    assert "2026-09-14" in linea


def test_program_start_acepta_una_cadena_bien_escrita_en_el_accesor():
    """El accesor no debe depender de que PyYAML haya hecho la conversión."""
    c = Config({"program": {"start": "2026-01-05"}}, "x")
    assert c.program_start == date(2026, 1, 5)


# ---------------------------------------------------------------------------
# Referencias cruzadas
# ---------------------------------------------------------------------------


def test_rutina_inexistente_en_la_rotacion(cfg):
    data = copy.deepcopy(cfg.raw)
    data["rotation"]["order"][1] = "dia_fantasma"
    assert "no existe" in errores(data)


def test_regla_especial_sobre_un_ejercicio_inexistente(cfg):
    data = copy.deepcopy(cfg.raw)
    data["special_rules"][0]["action"]["remove_exercises"] = ["ejercicio_fantasma"]
    assert "'ejercicio_fantasma' no existe" in errores(data)


def test_umbral_adaptativo_referenciado_pero_no_definido(cfg):
    data = copy.deepcopy(cfg.raw)
    data["thresholds"]["amber"].append(
        {"name": "inventada", "when": {"load_2d": {"gt_adaptive": "no_existe"}}}
    )
    assert "'no_existe', que no está definido" in errores(data)


def test_umbral_adaptativo_definido_pero_sin_lector(cfg):
    data = copy.deepcopy(cfg.raw)
    data["adaptive_thresholds"]["huerfano_p90"] = {
        "signal": "load_2d",
        "stat": "p90",
        "window_days": 90,
        "min_samples": 10,
    }
    assert "no lo referencia ninguna regla" in errores(data)


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
# adopt_executed_load: tres frenos, y un freno mal puesto no da error
# ---------------------------------------------------------------------------
# Un `down_after_sessions: 0` bajaría el objetivo con la primera serie mal
# apuntada; un `max_jump_pct: 5` dejaría pasar un 600 por un 60; un
# `max_jump_kg: 0` desconectaría el mecanismo entero. En los tres casos el
# sistema seguiría arrancando y escribiendo la rutina cada mañana.


def _ael(cfg, **cambios) -> dict:
    data = copy.deepcopy(cfg.raw)
    data["progression"]["adopt_executed_load"].update(cambios)
    return data


@pytest.mark.parametrize("valor", [0, -1, "tres", None, True])
def test_bajar_a_la_primera_sesion_floja_se_rechaza(cfg, valor):
    """`True` está en la lista a propósito: `isinstance(True, int)` es `True` en
    Python, así que un `down_after_sessions: yes` mal escrito pasaría por entero
    y valdría 1 -bajar a la primera- si nadie mira el tipo."""
    assert "down_after_sessions" in errores(_ael(cfg, down_after_sessions=valor))


def test_un_salto_maximo_de_cero_kilos_se_rechaza(cfg):
    """Con 0 no se adoptaría ningún cambio: la opción seguiría diciendo
    `enabled: true` y no haría absolutamente nada."""
    assert "max_jump_kg" in errores(_ael(cfg, max_jump_kg=0))


@pytest.mark.parametrize("valor", [0, 1.5, -0.2, "20%"])
def test_un_porcentaje_de_salto_fuera_de_rango_se_rechaza(cfg, valor):
    """Por encima de 1 el tope dejaría pasar más del doble del peso actual, que
    es justo lo que este límite existe para frenar."""
    assert "max_jump_pct" in errores(_ael(cfg, max_jump_pct=valor))


def test_los_valores_del_config_real_pasan(cfg):
    assert "adopt_executed_load" not in errores(cfg.raw)


def test_una_clave_inventada_en_la_adopcion_no_se_ignora(cfg):
    """El error recurrente de esta casa: `max_jump_percent` en vez de
    `max_jump_pct` arrancaría limpio y con el defecto del 20% decidiendo en su
    lugar. El usuario habría escrito un tope que nadie lee."""
    data = _ael(cfg, max_jump_percent=0.5)
    assert "max_jump_percent" in errores(data)


def test_apagar_la_adopcion_no_exige_el_resto_de_numeros(cfg):
    """Un bloque a `enabled: false` sigue teniendo que validar lo que ponga,
    pero no puede exigir claves que no están: el defecto ya las cubre."""
    data = copy.deepcopy(cfg.raw)
    data["progression"]["adopt_executed_load"] = {"enabled": False}
    assert "adopt_executed_load" not in errores(data)


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
    assert c.program_start == date(2026, 9, 14)


# ---------------------------------------------------------------------------
# Interruptores decorativos
# ---------------------------------------------------------------------------
#
# La peor clave del YAML no es la que está mal escrita: es la que está bien
# escrita y no la lee nadie. `notifications.telegram` declaraba un `enabled:
# true` y tres `send_on_*` que ningún módulo consultaba. El único freno real es
# `integrations.telegram.send_enabled`, comprobado dentro de `send()`.
#
# Quien pusiera `enabled: false` para callar el bot habría seguido recibiendo
# mensajes, y el archivo de configuración le habría dado la razón por escrito.
# Un interruptor desconectado es peor que no tener interruptor: promete un
# control que no existe.


@pytest.mark.parametrize(
    "seccion, clave",
    [
        ("notifications", "enabled"),
        ("notifications", "send_on_decision"),
        ("notifications", "send_on_error"),
        ("integrations", "notify_enabled"),
    ],
)
def test_un_interruptor_que_no_lee_nadie_no_arranca(cfg_copia, seccion, clave):
    cfg_copia.raw[seccion]["telegram"][clave] = False
    msg = errores(cfg_copia.raw)
    assert clave in msg
    assert "ignorando en silencio" in msg


def test_el_unico_freno_de_telegram_sigue_estando_permitido(cfg_copia):
    """El otro lado del filo: la validación no puede prohibir el freno bueno."""
    cfg_copia.raw["integrations"]["telegram"]["send_enabled"] = False
    assert "telegram" not in errores(cfg_copia.raw)


def test_el_config_del_repo_no_declara_interruptores_muertos():
    """Sobre el archivo real, no sobre uno inventado en el test."""
    c = load_config(REPO_ROOT / "config.yaml")
    notif = (c.raw.get("notifications") or {}).get("telegram") or {}
    assert set(notif) == {"include_reasoning"}, (
        f"claves que no lee nadie en notifications.telegram: "
        f"{set(notif) - {'include_reasoning'}}"
    )


# ---------------------------------------------------------------------------
# El reintento de Garmin
# ---------------------------------------------------------------------------


def test_una_lista_de_esperas_corta_no_arranca(cfg_copia):
    """Entre N intentos hay N-1 esperas.

    Con la lista corta la última se repetiría y nadie lo vería: el margen real
    no sería el que dice el archivo, que es justo el número que alguien mira
    cuando quiere saber si un 429 se sobrevive.
    """
    cfg_copia.raw["schedule"]["garmin_retry"] = {
        "attempts": 5, "backoff_seconds": [60, 300]
    }
    msg = errores(cfg_copia.raw)
    assert "4 esperas" in msg and "solo hay 2" in msg


def test_cero_intentos_no_arranca(cfg_copia):
    """Con 0 no se llama a Garmin ni una vez, y el día se queda sin datos."""
    cfg_copia.raw["schedule"]["garmin_retry"] = {"attempts": 0}
    assert "attempts" in errores(cfg_copia.raw)


def test_una_hora_ilegible_no_arranca(cfg_copia):
    """Reventaría al montar el scheduler, a las seis de la mañana del día del
    despliegue y con nadie delante."""
    cfg_copia.raw["schedule"]["garmin_fetch_time"] = "06h30"
    assert "HH:MM" in errores(cfg_copia.raw)


def test_una_hora_fuera_de_rango_no_arranca(cfg_copia):
    cfg_copia.raw["schedule"]["fallback_decision_time"] = "25:00"
    assert "HH:MM" in errores(cfg_copia.raw)


def test_una_hora_sin_dos_puntos_no_arranca(cfg_copia):
    """El caso que se cuela si solo se comprueba que los trozos sean números.

    `"0630"` parte en una sola pieza: `int("0630")` vale 630 y no protesta, así
    que la validación pasaría y el fallo saltaría después, al montar el
    scheduler, buscando el segundo trozo que no existe.
    """
    cfg_copia.raw["schedule"]["garmin_fetch_time"] = "0630"
    assert "HH:MM" in errores(cfg_copia.raw)


def test_una_seccion_entera_que_no_lee_nadie_no_arranca(cfg_copia):
    """No solo las claves de dentro: también un canal que no existe.

    `notifications.email` se escribiría con toda la buena fe y no mandaría
    jamás un correo. El silencio es el peor fallo de este sistema, y aquí
    vendría acompañado de un archivo de configuración que promete lo contrario.
    """
    cfg_copia.raw["notifications"]["email"] = {"to": "yo@ejemplo.com"}
    assert "email" in errores(cfg_copia.raw)


def test_una_integracion_que_no_existe_no_arranca(cfg_copia):
    cfg_copia.raw["integrations"]["strava"] = {"write_enabled": True}
    assert "strava" in errores(cfg_copia.raw)


def test_no_se_puede_pedir_quedarse_sin_ninguna_copia(cfg_copia):
    """La copia es lo único que permite deshacer una escritura en Hevy."""
    cfg_copia.raw["integrations"]["hevy"]["backup"]["keep_last"] = 0
    assert "keep_last" in errores(cfg_copia.raw)


@pytest.mark.parametrize("valor", [-1, "treinta", 30.5, True, None])
def test_un_keep_last_que_no_es_un_entero_util_no_arranca(cfg_copia, valor):
    """`True` está en la lista a propósito: en Python es un `int` que vale 1,
    así que `keep_last: yes` habría pasado por «guarda una copia» sin que nadie
    lo escribiera con esa intención.

    Y `None` también: es lo que deja YAML al escribir `keep_last:` y no poner
    nada detrás. Acaba donde acaba no escribir la clave -se guardan todas-, o
    sea que solo sirve para aparentar que dice un número.
    """
    cfg_copia.raw["integrations"]["hevy"]["backup"]["keep_last"] = valor
    assert "keep_last" in errores(cfg_copia.raw)


def test_no_poner_el_limite_es_valido_y_significa_guardarlas_todas(cfg_copia):
    del cfg_copia.raw["integrations"]["hevy"]["backup"]["keep_last"]
    assert _validate(cfg_copia.raw) == []


def test_las_copias_no_se_pueden_apagar(cfg_copia):
    """`backup.enabled` no existe y no puede existir: sin copia verificada no se
    escribe, y eso vive en el código. Aceptar la clave sería prometer un apagado
    que no hay."""
    cfg_copia.raw["integrations"]["hevy"]["backup"]["enabled"] = False
    assert "enabled" in errores(cfg_copia.raw)


# ---------------------------------------------------------------------------
# La rotación
#
# Aquí estaban los nueve tests del calendario fijo: un día mal escrito, un día
# que falta, un día de más, un día vacío, dos cosas en el mismo día, una bandera
# en `false`, una variante inactiva. Miraban tan de cerca porque el constructor
# de sesiones caía a `rest` en cuanto no encontraba `strength`, y todo lo que
# saliera mal salía mal hacia el mismo lado: el día de fuerza se convertía en
# descanso sin error, sin log y sin forma de sospecharlo.
#
# Y con los nueve en verde, el calendario que había activo no nombraba `dia_3`
# en ningún día de la semana. Estaba bien formado y era falso. Ninguno de los
# nueve podía verlo, porque todos comprobaban la FORMA y el fallo era del
# CONTENIDO: una semana declarada que no era mi semana.
#
# `rotation` tiene una superficie mucho más pequeña -una lista- y por eso se
# comprueba entera. Lo que sigue no es la traducción de los nueve: es lo que de
# verdad se puede escribir mal aquí.
# ---------------------------------------------------------------------------


def test_el_calendario_se_rechaza_con_nombre_propio(cfg_copia):
    """Y no como "sección desconocida".

    Lo que hay que entender no es que la clave sobre, sino que lo que lleva
    dentro YA NO SE APLICA. Un `monday: {strength: dia_1}` que sobreviva a una
    fusión de ramas no programaría nada, y un error genérico dejaría pensando
    que basta con moverlo de sitio.
    """
    cfg_copia.raw["calendar"] = {"active_variant": "with_pool", "variants": {}}
    err = errores(cfg_copia.raw)
    assert "calendar ya no existe" in err
    assert "rotation.order" in err


def test_sin_rotacion_no_arranca(cfg_copia):
    """Sin ciclo no hay ninguna sesión que escribir en Hevy. El sistema tiene
    que negarse, no amanecer en blanco."""
    del cfg_copia.raw["rotation"]
    assert "rotation" in errores(cfg_copia.raw)


def test_un_ciclo_vacio_no_arranca(cfg_copia):
    cfg_copia.raw["rotation"]["order"] = []
    err = errores(cfg_copia.raw)
    assert "rotation.order" in err


def test_un_ciclo_que_no_es_una_lista_no_arranca(cfg_copia):
    """`order: dia_1` en vez de una lista. En Python eso es una cadena, y una
    cadena es iterable: el ciclo pasaría a ser `['d','i','a','_','1']`."""
    cfg_copia.raw["rotation"]["order"] = "dia_1"
    assert "rotation.order" in errores(cfg_copia.raw)


def test_una_rutina_repetida_en_el_ciclo_no_arranca(cfg_copia):
    """El puntero sale de la última sesión ejecutada que esté en la lista.

    Con `[dia_1, dia_2, dia_1, dia_3]`, haber hecho `dia_1` no dice en cuál de
    las dos posiciones estoy: el ciclo salta siempre a la primera y el tramo de
    después del segundo `dia_1` no se visita jamás. `dia_3` desaparecería del
    programa sin que nada fallara, que es exactamente lo que ya pasó una vez.
    """
    cfg_copia.raw["rotation"]["order"] = ["dia_1", "dia_2", "dia_1", "dia_3"]
    err = errores(cfg_copia.raw)
    assert "repite" in err
    assert "dia_1" in err


def test_una_clave_inventada_dentro_de_rotation_no_arranca(cfg_copia):
    """El caso de quien escribe `start_with:` esperando que signifique algo."""
    cfg_copia.raw["rotation"]["start_with"] = "dia_2"
    assert "start_with" in errores(cfg_copia.raw)


def test_sacar_una_rutina_del_ciclo_y_dejarla_declarada_no_arranca(cfg_copia):
    """LA comprobación. La que le habría salvado los meses al Día 3.

    Una rutina que está escrita en `routines`, con sus ejercicios, sus cargas y
    su `focus`, pero que no aparece en el ciclo, no se programa nunca. Es
    exactamente la situación en la que el Día 3 estuvo meses: declarado,
    hecho, y fuera del plan. El resultado era que cada sesión entraba como
    entreno suelto, sin racha, sin adopción de carga y sin progresión.

    Sacarla del ciclo sigue siendo legal -a lo mejor es lo que se quiere-, pero
    hay que quererlo a propósito y borrar lo que la anunciaba. Callado, no.
    """
    cfg_copia.raw["rotation"]["order"] = ["dia_1", "dia_2"]
    err = errores(cfg_copia.raw)
    assert "dia_3" in err
    assert "rotation.order" in err


def test_y_si_ademas_se_borra_lo_que_la_anunciaba_entonces_si_arranca(cfg_copia):
    """El contraste: sin esto, el de arriba pasaría con un validador que
    rechazara cualquier `rotation.order` más corto que `routines`."""
    cfg_copia.raw["rotation"]["order"] = ["dia_1", "dia_2"]
    cfg_copia.raw["routines"]["dia_3"].pop("focus", None)

    assert errores(cfg_copia.raw) == ""
    assert "dia_3" in cfg_copia.raw["routines"], "sigue declarada, y no se escribe"
    assert Config(cfg_copia.raw, "x").rotation_order() == ["dia_1", "dia_2"]


# ---------------------------------------------------------------------------
# Cómo suben las reps
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("modo", ["double", "volume"])
def test_un_rep_apply_to_mal_escrito_no_arranca(cfg_copia, modo):
    """`rep_apply_to` es la opción que estuvo años sin que nadie la leyera.

    Ahora sí se lee, y por eso mismo hay que validarla: un `lowest-first` con
    guion en vez de guion bajo caería al defecto sin decir nada, y el defecto
    NO es el mismo en los dos modos -`double` sube una serie, `volume` sube
    todas-, así que la errata no se vería como un error sino como un esquema
    que se aplana solo al cabo de unas semanas.
    """
    cfg_copia.raw["progression"]["modes"][modo]["rep_apply_to"] = "lowest-first"
    err = errores(cfg_copia.raw)
    assert f"progression.modes.{modo}.rep_apply_to" in err
    assert "lowest_first" in err, "el error tiene que decir cuáles valen"


def test_un_default_apply_to_mal_escrito_no_arranca(cfg_copia):
    """Lo mismo para la carga: `top_sets` en plural es la errata natural, y
    caería en el `else` que sube TODAS las series."""
    cfg_copia.raw["progression"]["default_apply_to"] = "top_sets"
    assert "default_apply_to" in errores(cfg_copia.raw)


@pytest.mark.parametrize("modo,esperado", [("double", "lowest_first"),
                                           ("volume", "all_sets")])
def test_el_config_real_declara_como_suben_las_reps(cfg, modo, esperado):
    """Guarda del `config.yaml` de verdad: los dos modos declaran su
    `rep_apply_to` a mano en vez de depender del defecto del código, porque son
    defectos distintos y confundirlos no da ningún error."""
    assert cfg.raw["progression"]["modes"][modo].get("rep_apply_to") == esperado


# ---------------------------------------------------------------------------
# cycling.fetch: la ventana que alimenta los percentiles de carga
# ---------------------------------------------------------------------------
#
# Estas comprobaciones nacen de conectar `cycling.fetch`, que llevaba desde el
# principio declarado y sin leer mientras el código pedía 190 días a pelo. Una
# opción conectada hay que validarla, y aquí la validación no es cosmética: el
# backfill es lo ÚNICO que llena la caché sobre la que se calculan
# `load_2d_p90` y `load_7d_p90`. Si se queda corto, las dos reglas que usan
# esos umbrales no se evalúan ningún día. No fallan: no se evalúan. Y el
# mensaje de la mañana sale igual de bonito con una señal menos.


def test_un_backfill_mas_corto_que_el_percentil_no_arranca(cfg_copia):
    cfg_copia.raw["cycling"]["fetch"]["backfill_days"] = 30
    err = errores(cfg_copia.raw)
    assert "backfill_days" in err
    # La ventana contra la que se compara no se escribe a mano aquí: es la que
    # decida `dias_adaptativos`, y el día que aparezca una tercera cosa que se
    # calibre contra el histórico esta prueba tiene que seguir el cambio en vez
    # de fallar por un número viejo. Estaba puesto un "60" -el percentil de
    # carga- y la ventana de 180 de la bici lo dejó obsoleto al instante.
    assert str(dias_adaptativos(cfg_copia.raw)) in err, (
        "hay que decir contra qué ventana se está comparando"
    )
    assert "sin dar error" in err, "y por qué importa: el fallo sería mudo"


def test_el_backfill_del_config_real_cubre_la_ventana_de_la_bici(cfg):
    """Guarda del `config.yaml` de verdad, no de una copia manipulada: los 180
    días de huecos entre intensas solo existen si la caché los tiene. Con un
    backfill corto `_baseline_gaps` no falla, calcula OTRA COSA -menos huecos,
    percentiles más estrechos- y nadie se entera."""
    assert cfg.raw["cycling"]["fetch"]["backfill_days"] >= dias_adaptativos(cfg.raw)


def test_una_ventana_corta_mayor_que_el_backfill_no_arranca(cfg_copia):
    """Con los nombres al revés el 'backfill' dejaría huecos, que es justo lo
    contrario de lo que promete."""
    cfg_copia.raw["cycling"]["fetch"]["lookback_days"] = (
        cfg_copia.raw["cycling"]["fetch"]["backfill_days"] + 1
    )
    assert "lookback_days" in errores(cfg_copia.raw)


def test_borrar_cycling_fetch_no_desactiva_la_comprobacion(cfg_copia):
    """El `if fetch:` que había aquí convertía borrar la sección en la manera
    de saltarse el validador. Y es el caso peor: sin números escritos manda el
    defecto del código, que nadie va a revisar cuando cambie la ventana de la
    bici. Se comprueba el valor efectivo, no el declarado."""
    cfg_copia.raw["cycling"].pop("fetch", None)
    cfg_copia.raw["cycling"]["recommendation"]["baseline_from_gaps"][
        "window_days"
    ] = 5000
    err = errores(cfg_copia.raw)
    assert "backfill_days" in err and "5000" in err


@pytest.mark.parametrize("clave", ["lookback_days", "backfill_days"])
@pytest.mark.parametrize("valor", [0, -1, "diez", 10.5])
def test_una_ventana_que_no_es_un_entero_positivo_no_arranca(cfg_copia, clave, valor):
    """Un 0 no llama a Garmin ni una vez y la caché se quedaría congelada."""
    cfg_copia.raw["cycling"]["fetch"][clave] = valor
    assert f"cycling.fetch.{clave}" in errores(cfg_copia.raw)


def test_una_errata_en_cycling_fetch_no_se_ignora(cfg_copia):
    """`lookback_dias` en castellano es la errata natural, y caería al defecto
    del código dejando el YAML diciendo otra cosa: exactamente la avería que
    esta sección acaba de cerrar."""
    cfg_copia.raw["cycling"]["fetch"]["lookback_dias"] = 10
    err = errores(cfg_copia.raw)
    assert "cycling.fetch" in err and "lookback_dias" in err


def test_el_config_real_declara_las_dos_ventanas(cfg):
    """Guarda del `config.yaml` de verdad. Sin estas dos claves el código usa
    sus defectos y vuelve a haber dos sitios donde mirar el mismo número."""
    fetch = cfg.raw["cycling"]["fetch"]
    assert isinstance(fetch.get("lookback_days"), int)
    assert isinstance(fetch.get("backfill_days"), int)
    # La ventana se le pregunta a `dias_adaptativos` y no se recorre
    # `adaptive_thresholds` a mano. Cuando la sección se quedó vacía -su único
    # lector era `carga_acumulada`- el `max()` de aquí reventó con un
    # ValueError, y un test que revienta en vez de fallar no dice qué pasó.
    assert fetch["backfill_days"] >= dias_adaptativos(cfg.raw)


# ---------------------------------------------------------------------------
# `cycling.hr_zones`: un interruptor que ofrecía una alternativa inexistente
# ---------------------------------------------------------------------------
#
# El bloque era:
#
#     hr_zones:
#       source: garmin          # garmin | computed
#       max_hr: null            # solo necesario si source: computed
#
# `computed` no está implementado en ninguna parte. Las zonas salen de
# `timeInZones` de cada actividad, tal cual las da Garmin, y `classify_ride`
# lee `ride.zones` sin mirar el config. Lo peligroso no es la clave sobrante:
# es que anuncia una alternativa. Quien escribiera `source: computed` y
# rellenara su `max_hr` creería haber cambiado el criterio con el que se
# decide si una salida fue intensa -y por tanto si el lunes se frena- y no
# habría cambiado nada.


def test_hr_zones_no_se_ignora_se_rechaza(cfg_copia):
    cfg_copia.raw["cycling"]["hr_zones"] = {"source": "garmin", "max_hr": None}
    err = errores(cfg_copia.raw)
    assert "hr_zones" in err


def test_el_rechazo_de_hr_zones_explica_por_que(cfg_copia):
    """Un 'clave desconocida' a secas invita a volver a escribirla."""
    cfg_copia.raw["cycling"]["hr_zones"] = {"source": "computed", "max_hr": 185}
    err = errores(cfg_copia.raw)
    assert "computed" in err, "el error tiene que nombrar la alternativa que no existe"
    assert "Bórralo" in err


def test_el_config_real_ya_no_lo_lleva(cfg):
    assert "hr_zones" not in cfg.raw["cycling"]


def test_una_errata_en_cycling_no_se_ignora(cfg_copia):
    """La sección entera queda cerrada, no solo `hr_zones`."""
    cfg_copia.raw["cycling"]["clasification"] = []
    err = errores(cfg_copia.raw)
    assert "cycling" in err and "clasification" in err


@pytest.mark.parametrize(
    "clave",
    ["activity_types", "classification", "classification_fallback",
     "fetch", "load", "recommendation"],
)
def test_las_claves_vivas_de_cycling_siguen_pasando(cfg, clave):
    """Guarda de la lista blanca: si alguien la recorta, esto lo dice.

    Las seis se leen de verdad -`signals.py`, `bike_advisor.py`,
    `activity_cache.py`-, así que ninguna puede caer en el rechazo.

    Eran siete. `weekend` se ha caído de la lista porque la sección se ha
    borrado entera al pasar el recuento a ventana rodante: sus dos últimos
    lectores, `weekend_summary` y `_intense_rides_this_weekend`, ya no existen.
    Que no vuelva lo vigila `test_reponer_el_bloque_del_fin_de_semana_tampoco_pasa`
    en `tests/test_opciones_en_reglas.py`, que es el lado contrario de esta
    guarda: esta comprueba que lo vivo pasa, aquella que lo muerto no.
    """
    assert clave in cfg.raw["cycling"]
    assert "clave desconocida" not in errores(cfg.raw)


# ---------------------------------------------------------------------------
# Rutinas: la lista blanca que no existía y la rutina vacía que pasaba
# ---------------------------------------------------------------------------
#
# `check_keys` vivía a la altura de `progression.brakes`, o sea DESPUÉS del
# bucle de rutinas, así que las secciones de más arriba no podían llamarla
# aunque quisieran. Ahí se quedaron dos claves muertas: `standalone: false` en
# los dos bloques HIIT y `focus` en los tres días.


def test_standalone_ya_no_esta_en_el_config_real(cfg):
    for rkey, rutina in cfg.raw["routines"].items():
        assert "standalone" not in rutina, f"{rkey} sigue llevando standalone"


def test_standalone_no_puede_volver(cfg_copia):
    """No era una opción: repetía como interruptor lo que deciden `calendar` y
    `hiit.blocks`. Un `standalone: true` no habría programado nada."""
    cfg_copia.raw["routines"]["hiit_dia_1"]["standalone"] = True
    err = errores(cfg_copia.raw)
    assert "standalone" in err and "hiit_dia_1" in err


def test_una_errata_en_una_clave_de_rutina_no_se_ignora(cfg_copia):
    cfg_copia.raw["routines"]["dia_1"]["hevy_routine_di"] = "xxx"
    err = errores(cfg_copia.raw)
    assert "hevy_routine_di" in err


@pytest.mark.parametrize("vacio", [[], None, {}])
def test_una_rutina_sin_ejercicios_no_arranca(cfg_copia, vacio):
    """La mañana saldría sin sesión, que es indistinguible de un día de
    descanso: el fallo silencioso más caro de este sistema."""
    cfg_copia.raw["routines"]["dia_1"]["exercises"] = vacio
    err = errores(cfg_copia.raw)
    assert "dia_1" in err and "exercises" in err


def test_todas_las_rutinas_reales_traen_ejercicios(cfg):
    for rkey, rutina in cfg.raw["routines"].items():
        assert rutina.get("exercises"), f"{rkey} está vacía"


# `focus` ya no es decorativo: es el subtítulo del encabezado del mensaje.


def test_una_rutina_de_fuerza_sin_foco_no_arranca(cfg_copia):
    del cfg_copia.raw["routines"]["dia_1"]["focus"]
    err = errores(cfg_copia.raw)
    assert "dia_1" in err and "focus" in err


@pytest.mark.parametrize("vacio", ["", "   ", None])
def test_un_foco_en_blanco_tampoco_vale(cfg_copia, vacio):
    """Un `focus: ""` acortaría el encabezado igual que no ponerlo, pero
    dejando el fichero con pinta de tenerlo."""
    cfg_copia.raw["routines"]["dia_1"]["focus"] = vacio
    assert "focus" in errores(cfg_copia.raw)


def test_un_foco_en_un_bloque_hiit_se_rechaza(cfg_copia):
    """Los bloques HIIT se añaden al final de otra sesión y usan su
    encabezado, así que ahí `focus` volvería a ser una clave muerta."""
    cfg_copia.raw["routines"]["hiit_dia_1"]["focus"] = "Metabólico"
    err = errores(cfg_copia.raw)
    assert "hiit_dia_1" in err and "focus" in err


def test_se_exige_en_LAS_TRES_y_no_solo_en_las_que_salen_pronto(cfg_copia):
    """Era "en las rutinas de TODAS las variantes", cuando había variantes.

    El motivo de entonces sigue valiendo con otra forma: el `dia_3` puede
    tardar cinco días en salir por el ciclo, y un fichero que arranca hoy y
    falla el jueves es peor que uno que no arranca. Todas las del ciclo se
    validan al cargar, salgan cuándo salgan.
    """
    assert cfg_copia.rotation_order()[0] != "dia_3", "el escenario pide que no sea la primera"
    del cfg_copia.raw["routines"]["dia_3"]["focus"]
    assert "dia_3" in errores(cfg_copia.raw)


def test_el_config_real_trae_foco_en_las_tres(cfg):
    for rkey in ("dia_1", "dia_2", "dia_3"):
        assert str(cfg.raw["routines"][rkey].get("focus") or "").strip()


# ---------------------------------------------------------------------------
# `safety.condition`: documentación disfrazada de interruptor
# ---------------------------------------------------------------------------
#
# `condition: "Hernia discal L4-L5"` no lo leía nadie. En una sección llamada
# `safety` eso es peor que en cualquier otra: una clave se lee como algo que el
# código consulta, y quien la viera podría creer que cambiarla -o quitarla-
# cambia lo que el sistema permite. Lo que manda es la lista de `template_ids`.
# Ahora la condición está escrita como comentario del YAML, que es lo que era.


def test_condition_ya_no_esta_en_el_config_real(cfg):
    assert "condition" not in cfg.raw["safety"]


def test_condition_no_puede_volver(cfg_copia):
    cfg_copia.raw["safety"]["condition"] = "Hernia discal L4-L5"
    err = errores(cfg_copia.raw)
    assert "safety" in err and "condition" in err


def test_la_condicion_sigue_documentada_en_el_yaml():
    """Quitar la clave no es quitar la información: sin ella nadie entiende
    por qué hay veinte plantillas prohibidas."""
    import io

    from app.settings import REPO_ROOT

    texto = io.open(REPO_ROOT / "config.yaml", encoding="utf-8").read()
    assert "L4-L5" in texto


def test_una_errata_dentro_de_forbidden_in_hiit_no_se_ignora(cfg_copia):
    """`template_id` en singular dejaría la lista de bloqueos VACÍA y el
    validador daría por buena una rutina HIIT con un sit up dentro."""
    cfg_copia.raw["safety"]["forbidden_in_hiit"]["template_id"] = []
    err = errores(cfg_copia.raw)
    assert "safety.forbidden_in_hiit" in err and "template_id" in err


def test_la_prohibicion_de_verdad_sigue_en_pie(cfg_copia):
    """Guarda de que este arreglo no ha aflojado nada: el sit up retirado por
    la hernia sigue sin poder entrar en un bloque HIIT."""
    cfg_copia.raw["routines"]["hiit_dia_1"]["exercises"].append(
        {"key": "sit_up", "name": "Sit Up", "template_id": "022DF610",
         "progression_type": "none", "sets": [{"reps": 20}]}
    )
    err = errores(cfg_copia.raw)
    assert "022DF610" in err or "Sit Up" in err


# ---------------------------------------------------------------------------
# `progression.modes.*`: la lista blanca que faltaba donde más caro sale
# ---------------------------------------------------------------------------
#
# `rep_aply_to` con una pe se leería como ausente, el `.get()` devolvería el
# defecto `all`, y la rampa 12/10/10 se aplanaría a 13/11/11 en vez de subir
# solo la serie baja. El YAML seguiría diciendo `lowest_first`. Es la misma
# avería que se arregló en el código; ahora está cerrada por los dos lados.


def test_la_errata_de_rep_apply_to_no_se_ignora(cfg_copia):
    modo = cfg_copia.raw["progression"]["modes"]["volume"]
    modo["rep_aply_to"] = modo.pop("rep_apply_to")
    err = errores(cfg_copia.raw)
    assert "progression.modes.volume" in err and "rep_aply_to" in err


@pytest.mark.parametrize("modo", ["double", "volume", "sets"])
def test_cada_modo_tiene_su_lista_blanca(cfg_copia, modo):
    cfg_copia.raw["progression"]["modes"][modo]["incremento_reps"] = 1
    err = errores(cfg_copia.raw)
    assert f"progression.modes.{modo}" in err


def test_un_modo_inventado_no_se_ignora(cfg_copia):
    """Un modo que el motor no conoce no progresaría nada y no lo diría."""
    cfg_copia.raw["progression"]["modes"]["tiempo_bajo_tension"] = {}
    err = errores(cfg_copia.raw)
    assert "progression.modes" in err and "tiempo_bajo_tension" in err


def test_una_errata_en_el_nivel_de_progression_tampoco(cfg_copia):
    cfg_copia.raw["progression"]["defualt_increment_kg"] = 2.5
    err = errores(cfg_copia.raw)
    assert "defualt_increment_kg" in err


@pytest.mark.parametrize(
    "modo,clave",
    [("double", "deduced_span"), ("double", "rep_apply_to"),
     ("double", "rep_increment"),
     ("volume", "max_reps"), ("volume", "max_seconds"),
     ("volume", "on_ceiling_notify"), ("volume", "rep_apply_to"),
     ("volume", "reps_increment"), ("volume", "seconds_increment"),
     ("sets", "clean_sessions_required"), ("sets", "max_sets"), ("sets", "then")],
)
def test_las_claves_vivas_de_cada_modo_siguen_pasando(cfg, modo, clave):
    """Guarda de la lista blanca: las doce se leen de verdad en
    `app/engine/progression.py`, así que ninguna puede caer en el rechazo."""
    assert clave in cfg.raw["progression"]["modes"][modo]
    assert "clave desconocida" not in errores(cfg.raw)


# ---------------------------------------------------------------------------
# La sección `trend`
# ---------------------------------------------------------------------------
#
# Los cinco umbrales de la capa de tendencia son ABSOLUTOS, no adaptativos, y
# eso está razonado largo en el propio YAML. La contrapartida de esa decisión es
# que nada los corrige solo: si alguien pone `racha_min: 2` el sistema no se
# rompe, simplemente empieza a avisar todos los días y en un mes el aviso deja
# de leerse. Por eso el validador es aquí más duro que en otras secciones.


def test_trend_es_obligatoria(cfg_copia):
    """Omitirla no puede equivaler a apagarla.

    `enabled: false` deja constancia de que alguien decidió apagarla; que falte
    la sección entera es una actualización a medias, y con defectos por dentro
    nadie se enteraría de que la capa lleva meses callada.
    """
    del cfg_copia.raw["trend"]
    assert "trend" in errores(cfg_copia.raw)


def test_una_clave_inventada_en_trend_no_se_ignora(cfg_copia):
    cfg_copia.raw["trend"]["racha_minima"] = 5
    err = errores(cfg_copia.raw)
    assert "trend" in err and "racha_minima" in err


def test_una_clave_inventada_en_trend_sueno_tampoco(cfg_copia):
    cfg_copia.raw["trend"]["sueno"]["caida_puntos_min"] = 5
    err = errores(cfg_copia.raw)
    assert "trend.sueno" in err and "caida_puntos_min" in err


def test_enabled_tiene_que_ser_booleano(cfg_copia):
    """`enabled: "false"` es una cadena y las cadenas no vacías son verdaderas.

    Es el fallo silencioso clásico de YAML: se escribe entre comillas por
    costumbre y la capa queda encendida creyendo el autor que la apagó.
    """
    cfg_copia.raw["trend"]["enabled"] = "false"
    assert "enabled" in errores(cfg_copia.raw)


@pytest.mark.parametrize(
    "clave,valor",
    [("racha_min", 1), ("ventana_corta_dias", 6), ("ventana_larga_dias", 13),
     ("delta_pp_min", 0), ("motivo_semanas_min", 1)],
)
def test_los_minimos_de_trend_se_respetan(cfg_copia, clave, valor):
    cfg_copia.raw["trend"][clave] = valor
    assert clave in errores(cfg_copia.raw)


def test_la_ventana_corta_tiene_que_ser_mas_corta(cfg_copia):
    """Con corta >= larga el detector compararía un tramo consigo mismo."""
    cfg_copia.raw["trend"]["ventana_corta_dias"] = 90
    cfg_copia.raw["trend"]["ventana_larga_dias"] = 90
    assert "ventana" in errores(cfg_copia.raw)


@pytest.mark.parametrize("clave", ["caida_score_min", "caida_min_min"])
def test_las_caidas_de_sueno_tienen_que_ser_positivas(cfg_copia, clave):
    """Una caída de 0 dispararía el veredicto con cualquier ruido de medición."""
    cfg_copia.raw["trend"]["sueno"][clave] = 0
    assert clave in errores(cfg_copia.raw)


# ---------------------------------------------------------------------------
# El interruptor del recuento: el validador y el motor miraban a lados distintos
# ---------------------------------------------------------------------------
#
# La validación del bloque colgaba de `budget.get("enabled")` y quien lo aplicaba
# -`bike_advisor`- leía `budget_cfg.get("enabled", True)`. Sin la clave, el
# validador se saltaba el bloque entero y el motor lo aplicaba igual con los
# valores que se inventara: el sábado decidido con un límite que no estaba
# escrito en ninguna parte y un config.yaml que pasaba la validación.
#
# El bloque ya no recorta nada, así que el precio de perderlo en silencio ha
# cambiado: no es un sábado decidido a ciegas, es el número que desaparece del
# mensaje sin una sola queja. Menos grave y exactamente igual de callado, o sea
# que las guardas se quedan donde estaban.


def test_un_recuento_sin_la_clave_enabled_no_pasa(cfg_copia):
    b = cfg_copia.raw["cycling"]["recommendation"]["intensity_count"]
    del b["enabled"]
    assert "intensity_count.enabled" in errores(cfg_copia.raw)


def test_enabled_mal_escrito_se_caza_por_la_clave_que_falta(cfg_copia):
    """`enable:` por `enabled:` es el error real, no el hipotético."""
    b = cfg_copia.raw["cycling"]["recommendation"]["intensity_count"]
    b["enable"] = b.pop("enabled")
    assert "intensity_count.enabled" in errores(cfg_copia.raw)


def test_un_enabled_que_no_es_booleano_tampoco_pasa(cfg_copia):
    """`enabled: "true"` entre comillas es verdadero para Python y no para YAML."""
    b = cfg_copia.raw["cycling"]["recommendation"]["intensity_count"]
    b["enabled"] = "true"
    assert "intensity_count.enabled" in errores(cfg_copia.raw)


def test_apagar_el_recuento_a_proposito_sigue_siendo_valido(cfg_copia):
    """La guarda exige que la decisión esté escrita, no que sea una en concreto."""
    b = cfg_copia.raw["cycling"]["recommendation"]["intensity_count"]
    b["enabled"] = False
    assert "intensity_count" not in errores(cfg_copia.raw)


def test_quitar_el_bloque_entero_tampoco_pasa(cfg_copia):
    """Ausente y desactivado se parecen mucho en el YAML y nada en el log.

    `intensity_cout:` mal tecleado deja el bloque ausente y el diccionario
    vacío, y el recuento -la única razón por la que el bloque sigue existiendo-
    desaparecería del mensaje sin ruido. Apagarlo se hace con `enabled: false`,
    que es una decisión escrita y se lee en el fichero un año después.
    """
    del cfg_copia.raw["cycling"]["recommendation"]["intensity_count"]
    assert "intensity_count" in errores(cfg_copia.raw)


# ---------------------------------------------------------------------------
# `window_days`: el ancho de la ventana, que sale escrito en el mensaje
# ---------------------------------------------------------------------------
#
# ESTA SECCIÓN LA ABRIÓ UNA MUTACIÓN, NO UNA IDEA.
#
# `scripts/mutar_bici.py` cambió la línea `ventana = conteo.get("window_days")`
# de `config_loader` por un `ventana = 7` -o sea: el validador deja de mirar la
# clave y se inventa el ancho- y sobrevivió. La guarda estaba escrita, con su
# comentario largo explicando por qué no puede haber un 7 por defecto, y no la
# tocaba ningún test. Una guarda sin test no es media guarda: es una guarda que
# alguien puede borrar limpiando código sin que nada se ponga rojo.
#
# El precio de perderla: el YAML arranca sin `window_days`, `intensity_count()`
# revienta a las 6:00 dentro del contenedor, y lo que tenía que fallar en la
# aduana falla en mitad del cálculo de la mañana.


def test_un_recuento_sin_window_days_no_pasa(cfg_copia):
    """El periodo del recuento sale escrito en el mensaje, así que se exige."""
    b = cfg_copia.raw["cycling"]["recommendation"]["intensity_count"]
    del b["window_days"]
    assert "intensity_count.window_days" in errores(cfg_copia.raw)


@pytest.mark.parametrize("malo", [True, False, 0, -3, 7.0, "7", None, [7]])
def test_un_window_days_que_no_es_un_entero_positivo_tampoco_pasa(cfg_copia, malo):
    """`window_days: yes` es el caso real, y en Python `True` es un `int`.

    `isinstance(True, int)` vale cierto, así que sin la exclusión explícita del
    booleano esa errata pasaría la validación y daría una ventana de UN día: el
    mensaje diría «ninguna sesión intensa hoy» mirando solo hoy, con toda la
    naturalidad del mundo. El resto de valores son la familia habitual -el cero,
    el negativo, el flotante, el número entre comillas- que convierten el ancho
    en algo que no se puede escribir en una frase.
    """
    b = cfg_copia.raw["cycling"]["recommendation"]["intensity_count"]
    b["window_days"] = malo
    assert "intensity_count.window_days" in errores(cfg_copia.raw)


def test_el_error_de_window_days_dice_que_no_se_supone_un_siete(cfg_copia):
    """Un error que solo prohíbe no explica por qué el defecto sería peor.

    Quien vea la queja va a pensar «pues ponle un 7 por defecto y a correr», que
    es exactamente lo que no se puede hacer: el número sale escrito en el
    mensaje de la mañana y suponerlo es publicar un dato con unidades que nadie
    eligió. El mensaje tiene que llevar esa razón dentro.
    """
    b = cfg_copia.raw["cycling"]["recommendation"]["intensity_count"]
    del b["window_days"]
    msg = errores(cfg_copia.raw)
    assert "mensaje" in msg, "hay que decir que el periodo se publica"
    assert "no se supone" in msg, "hay que decir por qué no vale un defecto"


def test_con_el_recuento_apagado_no_se_exige_el_ancho(cfg_copia):
    """El control negativo: la guarda cuelga de `enabled`, y eso es a propósito.

    Sin este test, invertir la condición -exigir `window_days` siempre, incluso
    con el bloque apagado- dejaría los cuatro de arriba en verde y nadie estaría
    comprobando que apagar el recuento sigue siendo una configuración válida.

    Y no es una hipótesis de laboratorio: apagado el bloque, `intensity_count()`
    devuelve `None` antes de mirar el ancho, así que exigirlo aquí sería el
    validador pidiendo un dato que el motor ya no va a usar. Los dos lados
    tienen que preguntar lo mismo.
    """
    b = cfg_copia.raw["cycling"]["recommendation"]["intensity_count"]
    b["enabled"] = False
    del b["window_days"]
    assert "window_days" not in errores(cfg_copia.raw)


# ---------------------------------------------------------------------------
# Las claves de cuando el recuento recortaba: lista negra
# ---------------------------------------------------------------------------
#
# Una clave BORRADA que se ignora en silencio es peor que una mal escrita. Quien
# escriba `weekly_limit: 2` va a creer que se ha puesto un tope, el fichero va a
# validar, y no va a pasar nada: el silencio cae del lado de sentirse protegido
# sin estarlo.


@pytest.mark.parametrize(
    "muerta",
    [
        "weekly_limit",
        "on_budget_exhausted",
        "max_intense_rides_per_weekend",
        "require_green_for_intense",
    ],
)
def test_las_claves_del_presupuesto_muerto_no_pasan(cfg_copia, muerta):
    cfg_copia.raw["cycling"]["recommendation"]["intensity_count"][muerta] = 2
    assert muerta in errores(cfg_copia.raw)


def test_el_bloque_con_el_nombre_viejo_tampoco_pasa(cfg_copia):
    """Renombrar `intensity_budget` a `intensity_count` deja huérfano el viejo.

    Un fichero que traiga los dos -el nuevo porque se copió de aquí y el viejo
    porque estaba antes- valida perfectamente y aplica solo uno. El nombre viejo
    tiene que doler.
    """
    rec = cfg_copia.raw["cycling"]["recommendation"]
    rec["intensity_budget"] = {"enabled": True, "weekly_limit": 4}
    assert "intensity_budget" in errores(cfg_copia.raw)


@pytest.mark.parametrize("muerta", ["no_consecutive_intense", "after_intense_downgrade_to"])
def test_los_recortes_por_intensa_de_ayer_ya_no_existen(cfg_copia, muerta):
    """Los dos que bajaban el nivel por haber apretado ayer.

    Ahora eso es una nota. Dejar la clave puesta y que no haga nada sería
    exactamente el fallo que estas listas negras existen para evitar.
    """
    cfg_copia.raw["cycling"]["recommendation"][muerta] = "suave"
    assert muerta in errores(cfg_copia.raw)


# ---------------------------------------------------------------------------
# Las dos preguntas de Sí/No
# ---------------------------------------------------------------------------
#
# «¿Te apetece entrenar hoy?» y «¿Vas a entrenar hoy?» se guardan, se cuentan y
# se correlacionan, pero NO deciden el color del día. Esa frontera no se sostiene
# sola: se sostiene porque están en una lista distinta a la de los deslizadores y
# porque hay un validador que lo comprueba. Estos tests son el validador del
# validador.


def test_las_preguntas_no_son_deslizadores(cfg):
    """Dos listas separadas, y `checkin_keys` las junta solo para la pantalla.

    Fundirlas en una sola lista con una bandera `decide: false` sería la versión
    frágil: la garantía pasaría a depender de que cada sitio que recorre las
    respuestas se acuerde de mirar la bandera. Estando en listas distintas, el
    sitio que solo puede ver deslizadores pide `slider_keys()` y ya está.
    """
    assert set(cfg.pregunta_keys()) == {"wants_to_train", "will_train"}
    assert not set(cfg.pregunta_keys()) & set(cfg.slider_keys())
    assert cfg.checkin_keys() == cfg.slider_keys() + cfg.pregunta_keys()


@pytest.mark.parametrize("clave", ["wants_to_train", "will_train"])
def test_borrar_una_pregunta_del_config_no_pasa_en_silencio(cfg_copia, clave):
    """Sin la clave, el sistema NO se rompe: vuelve a comportarse como antes.

    Eso es justo lo que lo hace peligroso. `sig.get("will_train")` devolvería
    `None`, el motor lee `None` como «no me lo han dicho» -que es el tercer
    estado legítimo, el de los días que no contestas- y el mensaje prescribiría
    la sesión todos los días. Ni una excepción, ni un log: el comportamiento
    anterior con cara de normal. Por eso la ausencia tiene que doler AQUÍ, que es
    el único momento en que alguien está mirando.
    """
    cfg_copia.raw["checkin_preguntas"] = [
        p for p in cfg_copia.raw["checkin_preguntas"] if p["key"] != clave
    ]
    assert clave in errores(cfg_copia.raw)


def test_una_pregunta_repetida_como_deslizador_se_rechaza(cfg_copia):
    """La misma clave en las dos listas: puede y no puede mover el semáforo.

    El formulario la pintaría dos veces -una como barra de 1 a 10 y otra como
    Sí/No, sobre la misma columna- y el validador de reglas la dejaría entrar en
    el semáforo por la puerta de los deslizadores mientras el resto del sistema
    la trata como una pregunta que no decide. No hay forma de que las dos
    lecturas sean ciertas a la vez.
    """
    cfg_copia.raw["checkin_sliders"].append(
        {"key": "will_train", "label": "¿Vas a entrenar hoy?", "min": 1, "max": 10}
    )
    fallo = errores(cfg_copia.raw)
    assert "will_train" in fallo
    assert "checkin_preguntas" in fallo


def test_una_regla_del_semaforo_no_puede_mirar_una_pregunta(cfg_copia):
    """El camino que estaba abierto: las reglas de `thresholds`.

    Los frenos de progresión y los disparadores de reglas especiales ya se
    validaban contra `slider_keys`, así que una pregunta no pasaba por ahí. Las
    reglas del semáforo, en cambio, no validaban sus señales contra NADA: bastaba
    con escribir `will_train: {eq: false}` en un `when` para que «hoy no voy» te
    pusiera el día en ámbar. Y el ámbar de ese día no sería una medida de cómo
    estás: sería el sistema dándote la razón y llamándolo fisiología.
    """
    cfg_copia.raw["thresholds"]["amber"].append(
        {
            "name": "no_me_apetece",
            "when": {"all": [{"wants_to_train": {"eq": False}}]},
        }
    )
    fallo = errores(cfg_copia.raw)
    assert "no_me_apetece" in fallo
    assert "wants_to_train" in fallo


def test_la_prohibicion_llega_hasta_el_fondo_del_arbol(cfg_copia):
    """Anidada bajo `any` y negada: sigue siendo una regla que la mira.

    Este es el test que hace falsable la decisión de reutilizar
    `senales_de_regla` en vez de copiar aquí un recorrido más simple. Una
    comprobación que solo mirase las claves de primer nivel del `when` aprobaría
    esta regla, y el permiso quedaría abierto exactamente para quien anide, que
    es lo que hace cualquiera en cuanto la condición tiene dos partes.
    """
    cfg_copia.raw["thresholds"]["red"].append(
        {
            "name": "escondida",
            "when": {
                "any": [
                    {"all": [{"fatigue": {"gte": 9}}]},
                    {"not": {"will_train": {"eq": True}}},
                ]
            },
        }
    )
    fallo = errores(cfg_copia.raw)
    assert "escondida" in fallo
    assert "will_train" in fallo


def test_un_umbral_adaptativo_tampoco_puede_calcularse_sobre_una_pregunta(cfg_copia):
    """La puerta trasera: la clave del `when` no es la métrica del umbral.

    En `{load_2d: {gt_adaptive: mi_umbral}}` la señal que se lee es `load_2d` y
    el umbral contra el que se compara viaja en el VALOR. El guardia que camina
    las claves del árbol no puede ver ahí dentro por construcción, así que un
    `metric: will_train` colaría el histórico de «hoy no voy» dentro del cálculo
    del color sin que ninguna regla nombrase la pregunta.
    """
    cfg_copia.raw["adaptive_thresholds"]["ganas_p50"] = {
        "metric": "will_train",
        "window_days": 60,
        "percentile": 50,
        "min_days_required": 30,
    }
    fallo = errores(cfg_copia.raw)
    assert "ganas_p50" in fallo
    assert "will_train" in fallo


def test_el_desliza_de_las_ganas_sigue_pudiendo_decidir(cfg):
    """Y esta es la otra mitad: la prohibición no se ha comido `training_desire`.

    Conviven a propósito. La INTENSIDAD de las ganas (1-10) es una medida de
    estado y sí es asunto del semáforo -`sin_ganas_y_reventado` la usa-. El sí o
    el no de si vas a ir es una intención, y esa es asunto tuyo. Un guardia
    demasiado ancho habría matado la regla que sí queremos.
    """
    assert "training_desire" in cfg.slider_keys()
    reglas = [
        r
        for nivel in ("red", "amber")
        for r in (cfg.raw["thresholds"].get(nivel) or [])
        if isinstance(r, dict)
    ]
    assert any("training_desire" in senales_de_regla(r) for r in reglas)
    assert _validate(cfg.raw) == []


# ---------------------------------------------------------------------------
# El selector de sesión
# ---------------------------------------------------------------------------
#
# La sección dice poco a propósito: los días del ciclo NO se enumeran en ella.
# Casi todo lo que se comprueba aquí es que no se pueda empezar a enumerarlos.


def test_sin_la_seccion_del_selector_no_se_arranca(cfg_copia):
    """Faltando, el formulario no preguntaría y nadie lo notaría.

    Es el mismo fallo de `checkin_preguntas`: `chosen_session` se quedaría nula
    todos los días, el sistema seguiría proponiendo como siempre y todo
    funcionaría. O sea, el comportamiento anterior con cara de normal, que es la
    avería que este fichero existe para impedir.
    """
    del cfg_copia.raw["checkin_selector"]
    assert "checkin_selector" in errores(cfg_copia.raw)


def test_la_clave_del_selector_no_se_puede_renombrar(cfg_copia):
    """La busca el código por su nombre, como `will_train`."""
    cfg_copia.raw["checkin_selector"]["key"] = "rutina_elegida"
    fallo = errores(cfg_copia.raw)
    assert "chosen_session" in fallo


@pytest.mark.parametrize("clave", ["bici", "otro"])
def test_borrar_una_opcion_sin_fuerza_no_pasa_en_silencio(cfg_copia, clave):
    """Sin ellas, un día de bici no se podría declarar.

    Y no se podría declarar en silencio: el selector ofrecería solo rutinas del
    ciclo, y un día de bici volvería a ser indistinguible de un día sin
    contestar, que es justo la distinción que el selector viene a añadir.
    """
    cfg_copia.raw["checkin_selector"]["sin_fuerza"] = [
        o for o in cfg_copia.raw["checkin_selector"]["sin_fuerza"] if o["key"] != clave
    ]
    fallo = errores(cfg_copia.raw)
    assert clave in fallo


def test_una_opcion_sin_fuerza_no_puede_llamarse_como_un_dia_del_ciclo(cfg_copia):
    """Estando en las dos listas, elegirla es rutina y no-rutina a la vez.

    Cuál gana dependería del orden en que se preguntara, que es la peor clase de
    respuesta: la que es estable hasta que alguien reordena dos `if`.
    """
    cfg_copia.raw["checkin_selector"]["sin_fuerza"].append(
        {"key": "dia_2", "label": "Día 2 pero en bici"}
    )
    fallo = errores(cfg_copia.raw)
    assert "dia_2" in fallo


def test_una_clave_inventada_en_el_selector_se_rechaza(cfg_copia):
    """Lista blanca, como en el resto del fichero."""
    cfg_copia.raw["checkin_selector"]["preseleccionar"] = "dia_1"
    assert "preseleccionar" in errores(cfg_copia.raw)


def test_una_regla_del_semaforo_no_puede_mirar_el_selector(cfg_copia):
    """No por lo que haría, sino por lo que NO haría.

    A diferencia de una pregunta de Sí/No -que sí llega a `values` y sí movería
    el color-, el selector no llega. Una regla que lo nombrara no daría error ni
    un ámbar injusto: se anotaría como saltada por falta de datos todas las
    mañanas, para siempre, y en el log eso se lee igual que un día en que el
    reloj no sincronizó. Una regla que nunca dispara y parece que podría es peor
    que una regla que falla.
    """
    cfg_copia.raw["thresholds"]["amber"].append(
        {
            "name": "dia_de_bici",
            "when": {"all": [{"chosen_session": {"eq": "bici"}}]},
        }
    )
    fallo = errores(cfg_copia.raw)
    assert "dia_de_bici" in fallo
    assert "chosen_session" in fallo


def test_las_opciones_son_el_ciclo_mas_las_dos_que_no_son_fuerza(cfg):
    """Y en el orden de pantalla: primero el ciclo tal cual gira."""
    assert cfg.opciones_selector() == [*cfg.rotation_order(), "bici", "otro"]


def test_anadir_un_dia_al_ciclo_lo_hace_elegible_sin_tocar_el_selector(cfg_copia):
    """La razón entera de no enumerar los días en la sección del selector.

    El fallo que esto evita ya se pagó una vez: el calendario fijo estaba
    impecable, con sus siete días escritos, y sencillamente no nombraba `dia_3`.
    Todas las sesiones del Día 3 entraron como entrenos sueltos y sus cargas no
    se movieron nunca. Con una segunda lista aquí, un `dia_4` nuevo se podría
    proponer y no se podría aceptar.
    """
    cfg_copia.raw["rotation"]["order"] = [*cfg_copia.rotation_order(), "dia_4"]
    assert "dia_4" in cfg_copia.opciones_selector()


def test_el_selector_no_entra_en_las_claves_del_checkin(cfg):
    """Lo que lo mantiene fuera del espacio de nombres de las reglas.

    `checkin_keys()` es lo que `build_signals` vuelca en `signals.values` y lo
    que recorre el análisis. Todo lo de esa lista es número o booleano; esto es
    una cadena.
    """
    assert cfg.selector()["key"] not in cfg.checkin_keys()
    assert cfg.checkin_keys() == cfg.slider_keys() + cfg.pregunta_keys()
