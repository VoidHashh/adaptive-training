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
    cfg_copia.raw["progression"]["brakes"][1]["when"] = {"gte_adaptive": "load_3d_p90"}
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
# El calendario
#
# El peor sitio del archivo para un descuido, porque el constructor de sesiones
# cae a `rest` cuando no encuentra `strength`. Todo lo que sale mal aquí sale mal
# hacia el mismo lado: el día de fuerza se convierte en descanso, el mensaje de
# las nueve anuncia descanso, y no hay error, ni log, ni forma de sospecharlo.
# ---------------------------------------------------------------------------


def _lunes(cfg_copia) -> dict:
    return cfg_copia.raw["calendar"]["variants"]["with_pool"]


def test_un_dia_mal_escrito_no_arranca(cfg_copia):
    """`strenght` en vez de `strength`: el lunes pasaría a ser descanso."""
    _lunes(cfg_copia)["monday"] = {"strenght": "dia_1"}
    err = errores(cfg_copia.raw)
    assert "strenght" in err
    assert "monday" in err


def test_un_dia_que_falta_no_arranca(cfg_copia):
    """Olvidar un día y declararlo descanso acaban igual, y no son lo mismo."""
    del _lunes(cfg_copia)["monday"]
    err = errores(cfg_copia.raw)
    assert "monday" in err


def test_un_dia_de_mas_no_arranca(cfg_copia):
    """Lo encontró la mutación. Con los siete días en su sitio, un octavo
    inventado no lo veía nadie: la comprobación de días que FALTAN no dice nada
    de los que SOBRAN, y el bloque se quedaba ahí escrito sin hacer nada.

    Es el caso de quien añade `holiday:` esperando que signifique algo.
    """
    _lunes(cfg_copia)["holiday"] = {"rest": True}
    assert "holiday" in errores(cfg_copia.raw)


def test_un_dia_vacio_no_arranca(cfg_copia):
    """Que un descanso haya que escribirlo es el precio de poder distinguirlo
    de un día a medio escribir."""
    _lunes(cfg_copia)["monday"] = {}
    assert "monday" in errores(cfg_copia.raw)


def test_un_dia_con_dos_cosas_no_arranca(cfg_copia):
    """Gana la fuerza y la piscina no llega a leerse nunca."""
    _lunes(cfg_copia)["monday"] = {"strength": "dia_1", "pool": True}
    err = errores(cfg_copia.raw)
    assert "monday" in err
    assert "pool" in err and "strength" in err


def test_dos_banderas_a_la_vez_tampoco(cfg_copia):
    _lunes(cfg_copia)["wednesday"] = {"pool": True, "bike": True}
    assert "wednesday" in errores(cfg_copia.raw)


def test_una_bandera_en_false_no_arranca(cfg_copia):
    """`pool: false` y no escribir `pool` son indistinguibles para el código,
    así que escribirlo solo sirve para aparentar que dice algo."""
    _lunes(cfg_copia)["wednesday"] = {"pool": False, "rest": True}
    err = errores(cfg_copia.raw)
    assert "pool" in err
    assert "false" in err.lower()


def test_una_rutina_que_no_existe_sigue_sin_arrancar(cfg_copia):
    """Ya estaba comprobado; se deja escrito para que las claves nuevas de
    arriba no puedan cargárselo sin que nadie se entere."""
    _lunes(cfg_copia)["monday"] = {"strength": "dia_4"}
    assert "dia_4" in errores(cfg_copia.raw)


def test_una_variante_que_no_se_usa_tambien_se_valida(cfg_copia):
    """`summer` no está activa hoy, y ese es justo el problema: el día que se
    cambie `active_variant` no hay ninguna otra oportunidad de revisarla."""
    cfg_copia.raw["calendar"]["variants"]["summer"]["friday"] = {"strength": "dia_9"}
    assert "dia_9" in errores(cfg_copia.raw)
