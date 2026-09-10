"""Lo que el mensaje de las 7 de la mañana tiene que decir sí o sí.

Este fichero existe por un fallo silencioso de los que no revientan nada: el
motor apuntaba cada degradación en `signals.notes` -sin check-in, sin línea
base de HRV, umbral adaptativo sin histórico suficiente, salidas del fin de
semana sin clasificar- y esas notas solo las imprimía `cli.py`. En Telegram,
que es el único sitio donde este sistema se lee de verdad, no aparecía
ninguna.

El resultado era un mensaje idéntico el día que se sabía todo y el día que no
se sabía casi nada. Un 🟢 con línea base de HRV y check-in relleno y un 🟢
decidido a ciegas se leían igual.

La norma que fija este fichero: si hoy se ha decidido con menos datos de los
que debería, el mensaje lo dice, y lo dice AUNQUE se haya apagado el
razonamiento. `include_reasoning: false` significa "no me cuentes por qué",
no "ocúltame que hoy has decidido a ciegas".
"""

from __future__ import annotations

import copy

import pytest

from app.engine.decision import EngineState, decide
from app.engine.message import render_plain, render_telegram

from tests.conftest import LUNES, sig, sig_completa


DEGRADACIONES = [
    "sin check-in: solo se evalúan las reglas objetivas",
    "hrv_baseline: sin línea base (hacen falta 4 días con dato en los 7 anteriores)",
    "carga_acumulada: histórico insuficiente: 12 días con dato de 30 necesarios",
]


@pytest.fixture
def cfg_sin_motivo(cfg):
    """El config real con el razonamiento apagado."""
    c = copy.deepcopy(cfg)
    c.raw.setdefault("notifications", {}).setdefault("telegram", {})
    c.raw["notifications"]["telegram"]["include_reasoning"] = False
    return c


def decision(cfg, *, notas: list[str] | None = None, estado=None, **valores):
    s = sig(LUNES, **valores)
    s.notes.extend(notas or [])
    return decide(cfg, LUNES, s, estado or EngineState())


def decision_completa(cfg, *, estado=None, **valores):
    """Como `decision`, pero sobre un día en el que no falta ningún dato."""
    s = sig_completa(LUNES, **valores)
    return decide(cfg, LUNES, s, estado or EngineState())


# ---------------------------------------------------------------------------
# Las degradaciones llegan al mensaje
# ---------------------------------------------------------------------------


def test_las_degradaciones_aparecen_en_el_mensaje(cfg):
    txt = render_plain(decision(cfg, notas=DEGRADACIONES), cfg)
    for nota in DEGRADACIONES:
        assert nota in txt


def test_el_bloque_lleva_un_titulo_que_se_entiende_de_un_vistazo(cfg):
    txt = render_plain(decision(cfg, notas=DEGRADACIONES), cfg)
    assert "Decidido con datos incompletos" in txt


def test_apagar_el_razonamiento_no_oculta_que_faltaban_datos(cfg_sin_motivo):
    """El caso que motiva el fichero.

    Con `include_reasoning: false` desaparece el "Por qué" -que es lo
    pedido- pero no puede desaparecer el aviso de que hoy se ha decidido con
    menos información de la debida.
    """
    txt = render_plain(decision(cfg_sin_motivo, notas=DEGRADACIONES), cfg_sin_motivo)
    assert "Por qué" not in txt
    for nota in DEGRADACIONES:
        assert nota in txt


def test_un_dia_con_todos_los_datos_no_añade_ruido(cfg):
    """El aviso solo vale si no sale todos los días.

    Este test pasaba antes sobre `sig(LUNES)` a secas, que es un día sin UN
    SOLO dato: pasaba porque el aviso de reglas sin evaluar vivía entonces en
    otra sección, no porque no hubiera nada que avisar. Afirmaba lo contrario
    de lo que comprobaba.
    """
    txt = render_plain(decision_completa(cfg), cfg)
    assert "Decidido con datos incompletos" not in txt


# ---------------------------------------------------------------------------
# Las reglas que no se pudieron evaluar son parte del aviso, no del motivo
# ---------------------------------------------------------------------------


def test_una_regla_sin_datos_se_dice_aunque_el_razonamiento_este_apagado(
    cfg_sin_motivo,
):
    """Un check-in a medias deja reglas sin evaluar, y eso no es "por qué".

    `cervicales_hombros` mira `upper_discomfort`. Si el check-in no lo trae, la
    regla no se evalúa -no es que no dispare- y el día sale verde sin haberla
    mirado. Ese aviso vivía dentro de `include_reasoning`, así que con el
    razonamiento apagado un verde a medio comprobar se leía como un verde
    entero.
    """
    d = decision(cfg_sin_motivo, upper_discomfort=None)
    assert any(r.name == "cervicales_hombros" for r in d.light_decision.skipped), (
        "el escenario ya no deja esa regla sin evaluar; el test hay que rehacerlo"
    )
    txt = render_plain(d, cfg_sin_motivo)
    assert "Por qué" not in txt
    assert "Decidido con datos incompletos" in txt
    assert "cervicales_hombros" in txt


def test_las_reglas_sin_datos_no_se_dicen_dos_veces(cfg):
    """Con el razonamiento encendido sigue apareciendo una sola vez: se ha
    movido de sección, no duplicado."""
    txt = render_plain(decision(cfg), cfg)
    assert txt.count("sin datos para evaluar") == 1


def test_un_dia_con_el_checkin_entero_no_dice_que_falten_reglas(cfg):
    txt = render_plain(decision_completa(cfg), cfg)
    assert "sin datos para evaluar" not in txt


def test_el_dia_completo_no_deja_ninguna_regla_sin_evaluar(cfg):
    """Guarda de `sig_completa`, y la razón de que exista.

    Si mañana una regla nueva pide una señal que no está en
    `SENALES_COMPLETAS`, los tests de "aquí no hay nada que avisar" pasarían a
    comprobar un día incompleto sin enterarse. Este falla primero y dice cuál
    falta.
    """
    d = decision_completa(cfg)
    faltan = sorted({s for r in d.light_decision.skipped for s in r.missing})
    assert not d.light_decision.skipped, (
        f"`sig_completa` se ha quedado corta: añade a SENALES_COMPLETAS {faltan}"
    )


# ---------------------------------------------------------------------------
# Orden: el aviso sobrevive al recorte de los 4096 caracteres
# ---------------------------------------------------------------------------


def test_el_aviso_va_antes_del_porque_para_no_perderlo_en_el_recorte(cfg):
    """`render_telegram` recorta por el final cuando pasa de 4096.

    Si el aviso fuera detrás del razonamiento, el día de mensaje largo -que
    es el día de muchas reglas y mucho que contar- se perdería justo el aviso.
    """
    txt = render_plain(decision(cfg, notas=DEGRADACIONES), cfg)
    assert txt.index("Decidido con datos incompletos") < txt.index("Por qué")


def test_el_aviso_va_despues_de_la_sesion_porque_primero_es_que_hacer(cfg):
    txt = render_plain(decision(cfg, notas=DEGRADACIONES), cfg)
    assert txt.index("Día 1") < txt.index("Decidido con datos incompletos")


def test_el_mensaje_nunca_pasa_del_limite_de_telegram(cfg):
    from app.engine.message import LIMIT

    txt = render_telegram(decision(cfg, notas=DEGRADACIONES * 40), cfg)
    assert len(txt) <= LIMIT


# ---------------------------------------------------------------------------
# Los apuntes del motor que tampoco se veían
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg_hiit(cfg_copia):
    """El config real con el HIIT encendido y ya dentro de su semana de inicio."""
    cfg_copia.raw["hiit"]["enabled"] = True
    cfg_copia.raw["hiit"]["start_week"] = 1
    return cfg_copia


@pytest.fixture
def estado_hiit():
    """El HIIT cuenta semanas desde `EngineState.program_start`, no desde el
    YAML: sin esto el motivo siempre sería "no hay fecha de inicio"."""
    return EngineState(program_start=LUNES)


def test_lo_que_el_motor_apunta_de_la_sesion_llega_al_mensaje(cfg_hiit, estado_hiit):
    """"sin HIIT: <motivo>" se notaba en la app y no se explicaba en ninguna
    parte: solo lo imprimía el CLI."""
    d = decision(cfg_hiit, upper_discomfort=6, estado=estado_hiit)  # ámbar
    assert d.light == "amber"
    assert any(n.startswith("sin HIIT") for n in d.session.notes), (
        "el escenario ya no produce esa nota; el test hay que rehacerlo"
    )
    txt = render_plain(d, cfg_hiit)
    assert "sin HIIT" in txt and "hoy es amber" in txt


def test_el_hiit_apagado_en_el_config_no_se_repite_cada_dia(cfg):
    """Con `hiit.enabled: false` el motivo es el mismo hoy y dentro de seis
    meses. Una línea que sale todos los días no se lee: se aprende a saltarla,
    y con ella se saltan las que sí cambian."""
    assert cfg.raw["hiit"]["enabled"] is False
    d = decision(cfg)
    assert not any(n.startswith("sin HIIT") for n in d.session.notes)
    assert "sin HIIT" not in render_plain(d, cfg)


def test_una_regla_que_quita_el_hiit_no_dice_el_motivo_al_reves(cfg_hiit, estado_hiit):
    """La rama `ok and permitido` metía en el `else` común el caso "tocaba
    HIIT pero algo lo ha quitado", y ahí `why` vale "semana 1, verde y dia_1
    lo admite": el motivo de que SÍ tocara, presentado como el de que no."""
    cfg_hiit.raw["actions"]["green"]["allow_hiit"] = False
    d = decision(cfg_hiit, estado=estado_hiit)
    nota = next((n for n in d.session.notes if n.startswith("sin HIIT")), None)
    assert nota is not None
    assert "lo admite" not in nota, f"el motivo está del revés: {nota!r}"
    assert "regla especial" in nota


def test_un_bloque_de_hiit_que_no_existe_se_dice_en_vez_de_desaparecer(
    cfg_hiit, estado_hiit
):
    """El peor de los tres: todo decía que tocaba HIIT, el bloque no estaba en
    `routines`, y la sesión salía sin él sin una sola línea en ninguna parte.
    Una errata en `hiit.blocks` borraba el HIIT del programa en silencio."""
    cfg_hiit.raw["hiit"]["blocks"]["dia_1"] = "bloque_que_no_existe"
    d = decision(cfg_hiit, estado=estado_hiit)
    assert d.session.hiit_block is None
    nota = next((n for n in d.session.notes if n.startswith("sin HIIT")), None)
    assert nota is not None, f"el bloque desapareció sin decir nada: {d.session.notes}"
    assert "bloque_que_no_existe" in nota
    assert "bloque_que_no_existe" in render_plain(d, cfg_hiit)


def test_la_fuerza_que_queda_pendiente_se_dice(cfg):
    """Un día rojo aplaza la fuerza. Sin decirlo, el usuario no sabe si esa
    sesión se ha perdido o vuelve."""
    d = decision(cfg, lower_discomfort=7)
    txt = render_plain(d, cfg)
    assert "queda pendiente" in txt


def test_la_sesion_que_caduca_se_dice_aunque_el_razonamiento_este_apagado(
    cfg_sin_motivo,
):
    """Perder una sesión es un hecho del programa, no la explicación de una
    decisión.

    Es la misma frontera que el bloque de "Decidido con datos incompletos":
    dentro de `include_reasoning` van los porqués, y quien lo apaga está
    diciendo "no me cuentes cómo lo has razonado", no "no me digas que esta
    semana has entrenado una vez menos". Un aplazamiento que caduca es lo
    segundo, y además es lo único que distingue una sesión perdida de una
    sesión que sigue esperando.
    """
    from datetime import timedelta

    from app.engine.decision import advance_state

    cfg = cfg_sin_motivo
    rojo = decide(cfg, LUNES, sig(LUNES, lower_discomfort=7), EngineState())
    st = advance_state(EngineState(), rojo, executed={})
    assert st.pending_strength, "el lunes rojo ya no aplaza; rehaz el test"
    rutina, aplazada = st.pending_strength

    # Sin pasar por los días intermedios: si se decidiera cada día, alguno
    # verde y libre la recuperaría antes de caducar, que es justo lo que este
    # test NO quiere.
    tarde = LUNES + timedelta(days=9)
    d = decide(cfg, tarde, sig(tarde), st)
    assert d.expired_deferral == (rutina, aplazada)

    txt = render_plain(d, cfg)
    assert "Sesión perdida" in txt
    assert rutina in txt, "hay que decir CUÁL se ha perdido"
    assert aplazada.isoformat() in txt, "y de qué día era"
    assert txt.count("Sesión perdida") == 1


def test_dentro_de_plazo_el_mensaje_no_da_por_perdida_la_sesion(cfg_sin_motivo):
    """Guarda del de arriba: un aviso que saliera siempre no informa de nada."""
    from datetime import timedelta

    from app.engine.decision import advance_state

    cfg = cfg_sin_motivo
    rojo = decide(cfg, LUNES, sig(LUNES, lower_discomfort=7), EngineState())
    st = advance_state(EngineState(), rojo, executed={})

    pronto = LUNES + timedelta(days=2)
    d = decide(cfg, pronto, sig(pronto), st)
    assert "Sesión perdida" not in render_plain(d, cfg)


def test_el_motivo_de_la_descarga_no_se_cuela_cuando_no_hay_descarga(cfg):
    """El motor apunta `descarga: <motivo>` TODOS los días, también el día que
    no toca descarga. Volcar los apuntes tal cual metía en el mensaje un "no
    toca esta semana" diario, que es justo el ruido que hace que no se lea la
    línea que sí importa."""
    d = decision(cfg)
    assert not d.deload.active
    assert any(n.startswith("descarga:") for n in d.notes), (
        "el motor ya no apunta la descarga; el test hay que rehacerlo"
    )
    assert "descarga:" not in render_plain(d, cfg)


def test_cuando_si_hay_descarga_el_motivo_va_pegado_al_aviso(cfg):
    """El origen del contador NO sale del YAML directamente: `decide` lo lee de
    `EngineState.program_start`, que el CLI rellena con `cfg.program_start`.
    Tocar `cfg.raw["program"]["start"]` aquí no habría activado nada."""
    from datetime import timedelta

    estado = EngineState(program_start=LUNES - timedelta(weeks=7))
    for semana in range(0, 12):
        dia = LUNES + timedelta(weeks=semana)
        d = decide(cfg, dia, sig(dia), estado)
        if d.deload.active:
            txt = render_plain(d, cfg)
            cabecera = next(ln for ln in txt.splitlines() if "Semana de descarga" in ln)
            assert d.deload.reason in cabecera, (
                f"el motivo no está en la cabecera: {cabecera!r}"
            )
            assert txt.count(d.deload.reason) == 1, "y no repetido más abajo"
            return
    pytest.fail("ninguna de las 12 semanas activó la descarga")


def test_la_descarga_llega_a_activarse_alguna_vez(cfg):
    """Guarda del test de arriba: si `decide` dejara de activar la descarga,
    aquel test fallaría por el `pytest.fail` y parecería un problema de
    formato. Este dice cuál es la avería de verdad."""
    from datetime import timedelta

    estado = EngineState(program_start=LUNES - timedelta(weeks=7))
    activas = [
        semana
        for semana in range(12)
        if decide(cfg, LUNES + timedelta(weeks=semana), sig(LUNES), estado).deload.active
    ]
    assert activas, "la descarga no se activa en 12 semanas"


def test_la_sesion_recuperada_no_se_dice_dos_veces(cfg):
    """La cabecera ya lo pone; el apunte del motor diría lo mismo."""
    from app.engine.decision import advance_state

    rojo = decide(cfg, LUNES, sig(LUNES, lower_discomfort=7), EngineState())
    st = advance_state(EngineState(), rojo, executed={})

    from datetime import timedelta

    for i in range(1, 8):
        dia = LUNES + timedelta(days=i)
        d = decide(cfg, dia, sig(dia), st)
        if d.session.deferred_from:
            txt = render_plain(d, cfg)
            assert txt.count("Recuperas la sesión") == 1
            assert "sesión recuperada del" not in txt
            return
    pytest.fail("ningún día de la semana recuperó la sesión aplazada")
