"""El orquestador: `decide` y `advance_state` contra el config real.

Aquí se prueba lo que solo se puede romper en la juntura entre módulos: el
orden de las operaciones, la persistencia de las reglas especiales entre días,
el ámbito rutina+ejercicio del estado, y el avance del estado tras ejecutar la
sesión.

Es la traducción a pytest de `scripts/smoke_decision.py`, que se mantiene como
herramienta de diagnóstico manual (imprime la traza entera); lo que se ejecuta
en automático es esto.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from app.engine.decision import EngineState, advance_state, decide
from app.engine.signals import Signals

from tests.conftest import LUNES, sig

JUEVES = LUNES + timedelta(days=3)


# ---------------------------------------------------------------------------
# Semáforo -> sesión
# ---------------------------------------------------------------------------


def test_un_dia_sin_señales_malas_es_verde_y_entrena_entero(cfg):
    d = decide(cfg, LUNES, sig(LUNES), EngineState())
    assert d.light == "green"
    assert d.session.kind == "full"
    assert d.session.routine_key == "dia_1"
    assert d.calendar_routine == "dia_1"
    assert d.progression is not None
    assert d.bike is not None
    assert not d.deload.active


def test_una_molestia_cervical_de_6_baja_a_ambar_y_reduce(cfg):
    d = decide(cfg, LUNES, sig(LUNES, upper_discomfort=6), EngineState())
    assert d.light == "amber"
    assert d.session.kind == "reduced"
    assert d.progression is not None and not d.progression.gate_open


def test_una_molestia_lumbar_de_7_pone_el_dia_en_rojo(cfg):
    d = decide(cfg, LUNES, sig(LUNES, lower_discomfort=7), EngineState())
    assert d.light == "red"
    assert d.session.kind == "recovery"


# ---------------------------------------------------------------------------
# La sesión aplazada no se pierde
# ---------------------------------------------------------------------------


def test_la_fuerza_de_un_dia_rojo_queda_pendiente(cfg):
    d = decide(cfg, LUNES, sig(LUNES, lower_discomfort=7), EngineState())
    st = advance_state(EngineState(), d, executed={})
    assert st.pending_strength == ("dia_1", LUNES)


def test_lo_pendiente_se_recupera_en_el_siguiente_dia_libre_y_verde(cfg):
    rojo = decide(cfg, LUNES, sig(LUNES, lower_discomfort=7), EngineState())
    st = advance_state(EngineState(), rojo, executed={})

    martes = LUNES + timedelta(days=1)  # día de descanso en el calendario
    d2 = decide(cfg, martes, sig(martes), st)
    assert d2.session.routine_key == "dia_1"
    assert d2.session.deferred_from == LUNES

    st2 = advance_state(st, d2, executed={})
    assert st2.pending_strength is None, "el pendiente se consume al recuperarlo"


def test_entrenar_otra_rutina_no_se_lleva_por_delante_la_aplazada(cfg):
    """El aplazamiento existe para que un día malo no cueste una sesión.

    Esto lo borraba `advance_state` con un `pending_strength = None` a secas:
    un lunes rojo aplazaba `dia_1`, el jueves tocaba `dia_2` por calendario, y
    PLANIFICAR el `dia_2` -ni siquiera ejecutarlo- borraba el `dia_1`. La
    sesión que el rojo había protegido desaparecía por haber entrenado otra
    cosa. Costaba la sesión igual, pero tres días más tarde y sin decirlo.
    """
    rojo = decide(cfg, LUNES, sig(LUNES, lower_discomfort=7), EngineState())
    st = advance_state(EngineState(), rojo, executed={})
    assert st.pending_strength == ("dia_1", LUNES)

    jueves = LUNES + timedelta(days=3)
    d2 = decide(cfg, jueves, sig(jueves, lower_discomfort=1), st)
    assert d2.session.routine_key == "dia_2", "el escenario necesita OTRA rutina"

    st2 = advance_state(st, d2, executed={k["key"]: True for k in d2.session.exercises})
    assert st2.pending_strength == ("dia_1", LUNES), (
        "haber hecho dia_2 no es haber hecho el dia_1 que quedaba pendiente"
    )


def test_un_aplazamiento_que_caduca_se_borra_y_se_cuenta(cfg):
    """Antes no lo borraba nadie y no lo contaba nadie.

    Pasados los `defer_expires_days`, `decide` dejaba de mirar la fila y la
    sesión aplazada se evaporaba: ni se recuperaba, ni se limpiaba, ni se
    avisaba. Una sesión de fuerza menos esa semana, sin rastro.
    """
    rojo = decide(cfg, LUNES, sig(LUNES, lower_discomfort=7), EngineState())
    st = advance_state(EngineState(), rojo, executed={})

    tarde = LUNES + timedelta(days=9)  # defer_expires_days = 7
    d = decide(cfg, tarde, sig(tarde, lower_discomfort=1), st)
    assert d.expired_deferral == ("dia_1", LUNES)

    st2 = advance_state(st, d, executed=None)
    assert st2.pending_strength is None, "caducado y encima sin limpiar"


def test_la_caducidad_se_mira_aunque_el_dia_no_sea_verde_ni_libre(cfg):
    """La comprobación colgaba del `if` que recupera la sesión, que solo entra
    en días verdes y sin bici. O sea: se dejaba de comprobar exactamente
    cuando se arrastra una mala racha, que es cuando un aplazamiento caduca."""
    rojo = decide(cfg, LUNES, sig(LUNES, lower_discomfort=7), EngineState())
    st = advance_state(EngineState(), rojo, executed={})

    tarde = LUNES + timedelta(days=9)
    d = decide(cfg, tarde, sig(tarde, lower_discomfort=7), st)  # otro rojo
    assert d.light == "red"
    assert d.expired_deferral == ("dia_1", LUNES)


def test_dentro_de_plazo_no_caduca_nada(cfg):
    """El contraste: sin esto, un `expired_deferral` siempre activo pasaría los
    dos tests de arriba y borraría todos los aplazamientos al día siguiente."""
    rojo = decide(cfg, LUNES, sig(LUNES, lower_discomfort=7), EngineState())
    st = advance_state(EngineState(), rojo, executed={})

    pronto = LUNES + timedelta(days=2)
    d = decide(cfg, pronto, sig(pronto, lower_discomfort=1), st)
    assert d.expired_deferral is None
    st2 = advance_state(st, d, executed=None)
    assert st2.pending_strength is not None or d.session.routine_key == "dia_1"


# ---------------------------------------------------------------------------
# Reglas especiales: vigencia por calendario, no por síntoma de hoy
# ---------------------------------------------------------------------------


def hist_lumbar(dia: date, valor: int, dias: int = 2) -> dict:
    return {"lower_discomfort": {dia - timedelta(days=i): valor for i in range(dias)}}


def test_dos_dias_de_lumbar_a_5_retiran_el_peso_muerto(cfg):
    señales = Signals(
        day=JUEVES,
        values={"lower_discomfort": 5},
        history=hist_lumbar(JUEVES, 5),
    )
    d = decide(cfg, JUEVES, señales, EngineState())
    nombres = [r.name for r in d.active_rules]
    assert "retirada_peso_muerto" in nombres
    assert "peso_muerto_smith" not in [e.get("key") for e in d.session.exercises]


def test_la_retirada_dura_catorce_dias(cfg):
    señales = Signals(
        day=JUEVES, values={"lower_discomfort": 5}, history=hist_lumbar(JUEVES, 5)
    )
    d = decide(cfg, JUEVES, señales, EngineState())
    regla = next(r for r in d.active_rules if r.name == "retirada_peso_muerto")
    assert (regla.active_until - regla.active_from).days == 13  # 14 días inclusive


def test_la_regla_sobrevive_a_que_hoy_no_duela_nada(cfg):
    """Si caducara con el síntoma, la protección desaparecería el primer día
    bueno, que es justo cuando uno se anima a cargar."""
    señales = Signals(
        day=JUEVES, values={"lower_discomfort": 5}, history=hist_lumbar(JUEVES, 5)
    )
    st = advance_state(EngineState(), decide(cfg, JUEVES, señales, EngineState()), executed=None)

    despues = JUEVES + timedelta(days=7)
    d = decide(cfg, despues, sig(despues, lower_discomfort=0), st)
    assert "retirada_peso_muerto" in [r.name for r in d.active_rules]


def test_y_caduca_al_dia_quince(cfg):
    señales = Signals(
        day=JUEVES, values={"lower_discomfort": 5}, history=hist_lumbar(JUEVES, 5)
    )
    st = advance_state(EngineState(), decide(cfg, JUEVES, señales, EngineState()), executed=None)

    caducada = JUEVES + timedelta(days=14)
    d = decide(cfg, caducada, sig(caducada, lower_discomfort=0), st)
    assert "retirada_peso_muerto" not in [r.name for r in d.active_rules]


def test_el_recorte_de_carga_de_una_regla_aplica_su_factor_exacto(cfg_summer):
    """Y se ACUMULA con el recorte de series del ámbar, no lo sustituye."""
    viernes = date(2026, 9, 11)  # dia_3 en la variante summer
    antes = [
        s.get("weight_kg")
        for ex in cfg_summer.raw["routines"]["dia_3"]["exercises"]
        if ex["key"] == "press_hombro_maquina"
        for s in ex["sets"]
    ]

    d = decide(cfg_summer, viernes, sig(viernes, upper_discomfort=6), EngineState())
    despues = [
        s.get("weight_kg")
        for ex in d.session.exercises
        if ex.get("key") == "press_hombro_maquina"
        for s in ex["sets"]
    ]

    assert "descarga_press_hombro" in [r.name for r in d.active_rules]
    assert despues[0] == pytest.approx(antes[0] * 0.70)
    assert d.light == "amber"
    assert len(despues) < len(antes), "el ámbar recorta series ADEMÁS de la carga"


# ---------------------------------------------------------------------------
# Semana de descarga
# ---------------------------------------------------------------------------
# Es la parte del sistema con consecuencias a más largo plazo: 44 años, déficit
# calórico y hernia L4-L5. Que no se active nunca es un fallo silencioso, así
# que su calendario se prueba entero.


@pytest.fixture
def estado_en_descarga():
    return EngineState(program_start=LUNES - timedelta(weeks=7))


def test_a_las_siete_semanas_toca_descarga(cfg, estado_en_descarga):
    d = decide(cfg, LUNES, sig(LUNES), estado_en_descarga)
    assert d.deload.active
    assert d.deload.reason


def test_la_descarga_congela_la_progresion(cfg, estado_en_descarga):
    d = decide(cfg, LUNES, sig(LUNES), estado_en_descarga)
    assert d.progression is not None and not d.progression.gate_open
    assert not d.progression.changes


def test_la_semana_anterior_no_es_de_descarga(cfg, estado_en_descarga):
    antes = LUNES - timedelta(weeks=1)
    assert not decide(cfg, antes, sig(antes), estado_en_descarga).deload.active


def test_la_descarga_cubre_los_siete_dias_no_solo_el_que_dispara(cfg, estado_en_descarga):
    d = decide(cfg, LUNES, sig(LUNES), estado_en_descarga)
    st = advance_state(estado_en_descarga, d, executed=None)
    assert st.last_deload_start == LUNES

    for i in range(1, 7):
        dia = LUNES + timedelta(days=i)
        assert decide(cfg, dia, sig(dia), st).deload.active, f"el día +{i} se cayó"


def test_no_se_encadenan_dos_descargas_seguidas(cfg, estado_en_descarga):
    d = decide(cfg, LUNES, sig(LUNES), estado_en_descarga)
    st = advance_state(estado_en_descarga, d, executed=None)
    siguiente = LUNES + timedelta(weeks=1)
    assert not decide(cfg, siguiente, sig(siguiente), st).deload.active


def test_una_descarga_que_arranca_en_rojo_se_retrasa(cfg, estado_en_descarga):
    """Descargar sobre una semana ya frenada no descarga: no hay de qué."""
    d = decide(cfg, LUNES, sig(LUNES, lower_discomfort=7), estado_en_descarga)
    assert d.light == "red"
    assert not d.deload.active
    assert d.deload.shifted


def test_la_descarga_se_programa_cada_siete_semanas_y_siempre_en_lunes(cfg):
    """Un año simulado desde el `program_start` real del YAML.

    Se mide entre INICIOS DE SEMANA, no desde la fecha cruda: 2026-09-08 es
    martes y las descargas siempre empiezan en lunes, así que la primera cae
    legítimamente a 6,9 semanas del origen.
    """
    inicio = cfg.program_start
    st = EngineState(program_start=inicio)
    arranques: list[date] = []
    dias_en_descarga = 0

    dia = inicio
    for _ in range(370):
        d = decide(cfg, dia, sig(dia), st)
        if d.deload.active:
            dias_en_descarga += 1
            if not arranques or (dia - arranques[-1]).days > 7:
                arranques.append(dia)
        st = advance_state(st, d, executed=None)
        dia += timedelta(days=1)

    assert arranques, "en un año entero no se programó ni una descarga"
    assert all(a.weekday() == 0 for a in arranques), "las descargas empiezan en lunes"

    lunes_origen = inicio - timedelta(days=inicio.weekday())
    assert (arranques[0] - lunes_origen).days / 7 == pytest.approx(7.0, abs=0.2)

    separaciones = [
        (b - a).days / 7 for a, b in zip(arranques, arranques[1:])
    ]
    assert all(6 <= s <= 8 for s in separaciones), separaciones
    assert dias_en_descarga == len(arranques) * 7


# ---------------------------------------------------------------------------
# Avance del estado
# ---------------------------------------------------------------------------


def test_las_rachas_son_por_rutina_y_ejercicio(cfg_summer):
    """La plancha lateral aparece en varias rutinas y cada una lleva su racha.

    Con ámbito solo-ejercicio, hacerla bien en `dia_1` haría progresar la de
    `dia_2` sin haberla ejecutado.
    """
    st = EngineState()
    for dia in (date(2026, 9, 7), date(2026, 9, 9)):
        d = decide(cfg_summer, dia, sig(dia), st)
        st = advance_state(st, d, executed={e["key"]: True for e in d.session.exercises})

    planchas = {k: v for k, v in st.clean_sessions.items() if "plancha_lateral" in k[1]}
    assert len(planchas) >= 2
    assert all(isinstance(k, tuple) and len(k) == 2 for k in st.clean_sessions)
    assert st.last_routine_light.get("dia_1") == "green"


def test_una_serie_sin_completar_resetea_la_racha_entera(cfg_summer):
    st = EngineState()
    lunes = date(2026, 9, 7)
    d = decide(cfg_summer, lunes, sig(lunes), st)
    st = advance_state(st, d, executed={e["key"]: True for e in d.session.exercises})

    siguiente = lunes + timedelta(days=7)
    d2 = decide(cfg_summer, siguiente, sig(siguiente), st)
    falla = next(e["key"] for e in d2.session.exercises)
    ejecutado = {e["key"]: True for e in d2.session.exercises}
    ejecutado[falla] = False

    st2 = advance_state(st, d2, executed=ejecutado)
    assert st2.clean_sessions.get(("dia_1", falla)) == 0


def test_el_estado_no_avanza_si_la_sesion_aun_no_se_ha_ejecutado(cfg_summer):
    """A las siete de la mañana la decisión existe, pero la racha no."""
    st = EngineState()
    d = decide(cfg_summer, date(2026, 9, 7), sig(date(2026, 9, 7)), st)
    despues = advance_state(st, d, executed=None)
    assert despues.clean_sessions == {}
    assert despues.last_routine_light == {}


# ---------------------------------------------------------------------------
# "No lo sé" no es "sí": el cumplimiento de la sesión anterior
# ---------------------------------------------------------------------------
#
# `for_routine` proyectaba la ausencia de registro a `True`, es decir: "no
# tengo ni un apunte de este ejercicio" se convertía en "la última sesión se
# completó entera". La puerta general pide justo ese dato para subir carga, y
# `evaluate_gate` ya sabía tratar el `None` -lo cerraba nombrando el motivo-,
# pero nunca llegaba a verlo. Con una hernia L4-L5 la dirección del fallo
# importa: la puerta se abría, no se cerraba.


def test_un_ejercicio_sin_registro_llega_al_motor_como_no_lo_se(cfg):
    st = EngineState()
    comp, clean = st.for_routine("dia_1", ["prensa_horizontal"])
    assert comp["prensa_horizontal"] is None
    assert clean["prensa_horizontal"] == 0, (
        "la racha sí conserva el cero, y no es incoherente: cero sesiones "
        "limpias es un valor honesto que CIERRA la puerta; un True inventado "
        "la abre"
    )


def test_un_registro_de_verdad_sigue_pasando_tal_cual(cfg):
    st = EngineState(compliance={("dia_1", "prensa_horizontal"): False,
                                 ("dia_1", "gemelo_sentado"): True})
    comp, _ = st.for_routine("dia_1", ["prensa_horizontal", "gemelo_sentado"])
    assert comp["prensa_horizontal"] is False
    assert comp["gemelo_sentado"] is True


def test_el_registro_de_otra_rutina_no_vale_por_esta(cfg):
    """La clave es (rutina, ejercicio). Si no lo fuera, haber cumplido en
    `dia_3` abriría la puerta del `dia_1` sin haberlo entrenado."""
    st = EngineState(compliance={("dia_3", "prensa_horizontal"): True})
    comp, _ = st.for_routine("dia_1", ["prensa_horizontal"])
    assert comp["prensa_horizontal"] is None


def test_en_frio_la_puerta_se_cierra_en_vez_de_abrirse(cfg):
    """Instalación recién estrenada: no hay ni una sesión reconciliada."""
    d = decide(cfg, LUNES, sig(LUNES, lower_discomfort=1), EngineState())
    assert d.progression is not None
    assert not d.progression.gate_open
    assert "no hay registro" in d.progression.gate_reason
    assert not d.progression.changes, (
        "y no se mueve nada: la puerta gobierna también el volumen"
    )


def test_un_ejercicio_nuevo_en_una_rutina_en_marcha_frena_a_toda_la_rutina(cfg):
    """El caso de en medio, que es el que estaba peor.

    Ocho ejercicios con registro y uno estrenado hoy. `all(known)` devolvía
    True: la puerta se abría con la evidencia que había e ignoraba la que
    faltaba. Progresar con la evidencia elegida es progresar a ciegas.
    """
    keys = [e["key"] for e in cfg.raw["routines"]["dia_1"]["exercises"]]
    nuevo = keys[-1]
    st = EngineState(
        compliance={("dia_1", k): True for k in keys if k != nuevo},
        clean_sessions={("dia_1", k): 5 for k in keys},
    )
    d = decide(cfg, LUNES, sig(LUNES, lower_discomfort=1), st)

    assert not d.progression.gate_open
    assert nuevo in d.progression.gate_reason, "hay que decir CUÁL falta"
    assert not d.progression.changes


def test_con_todos_registrados_y_cumplidos_la_puerta_se_abre(cfg):
    """El contraste. Si esto no pasara, el arreglo habría congelado el motor
    entero en vez de tapar un agujero."""
    keys = [e["key"] for e in cfg.raw["routines"]["dia_1"]["exercises"]]
    st = EngineState(
        compliance={("dia_1", k): True for k in keys},
        clean_sessions={("dia_1", k): 5 for k in keys},
    )
    d = decide(cfg, LUNES, sig(LUNES, lower_discomfort=1), st)

    assert d.progression.gate_open, d.progression.gate_reason
    assert d.progression.changes, "con todo en regla algo tiene que subir"


def test_una_sola_sesion_reconciliada_desbloquea_la_rutina_entera(cfg):
    """La otra mitad del arreglo, y la que impide que sea un cierre permanente.

    Cerrar la puerta ante la falta de registro solo es aceptable si el registro
    se consigue. `advance_state` recorre TODOS los ejercicios de la sesión y a
    los que no aparecen en `executed` les pone False, no los deja en None: tras
    una sesión reconciliada no queda ni un `None` en la rutina.

    Si algún día un ejercicio del config dejara de llegar a la sesión, esa
    clave se quedaría en None para siempre y la rutina no volvería a progresar.
    Este test es la alarma de eso.
    """
    st = EngineState()
    d = decide(cfg, LUNES, sig(LUNES, lower_discomfort=1), st)
    st = advance_state(st, d, executed={e["key"]: True for e in d.session.exercises})

    keys_cfg = [e["key"] for e in cfg.raw["routines"][d.session.routine_key]["exercises"]]
    comp, _ = st.for_routine(d.session.routine_key, keys_cfg)
    sin_registro = [k for k, v in comp.items() if v is None]
    assert not sin_registro, (
        f"estos ejercicios están en config.yaml pero no llegan a la sesión, "
        f"así que no se reconcilian nunca y congelan la rutina: {sin_registro}"
    )


def test_un_incumplimiento_confirmado_manda_sobre_los_que_faltan(cfg):
    """Si lo confirmado ya decide, lo que falta da igual.

    Es el criterio de `weekend_summary`. Un False confirmado cierra la puerta
    por incumplimiento, no por falta de registro: el motivo tiene que decir la
    verdad, porque es lo que se lee en el móvil.
    """
    keys = [e["key"] for e in cfg.raw["routines"]["dia_1"]["exercises"]]
    st = EngineState(
        compliance={("dia_1", keys[0]): False, ("dia_1", keys[1]): None},
        clean_sessions={("dia_1", k): 5 for k in keys},
    )
    d = decide(cfg, LUNES, sig(LUNES, lower_discomfort=1), st)

    assert not d.progression.gate_open
    assert "no se completaron" in d.progression.gate_reason
    assert "no hay registro" not in d.progression.gate_reason


# ---------------------------------------------------------------------------
# Serialización
# ---------------------------------------------------------------------------


def test_la_decision_se_puede_guardar_como_json(cfg):
    d = decide(cfg, LUNES, sig(LUNES), EngineState())
    recargada = json.loads(json.dumps(d.to_dict(), ensure_ascii=False))
    assert recargada["light"] == "green"
    assert "inputs" in recargada, "sin la fotografía de señales no se puede auditar"
