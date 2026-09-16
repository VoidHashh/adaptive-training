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
from datetime import datetime, timedelta

import pytest

from app.engine.decision import EngineState, decide
from app.engine.message import (
    EMOJI,
    LIMIT,
    NOMBRE_LUZ,
    render_plain,
    render_telegram,
)

from tests.conftest import LUNES, sig, sig_completa
from tests.dobles import doble_de
from app.engine.bike_advisor import BikeRecommendation
from app.engine.decision import ActiveRule, DecisionAnulada
from app.engine.tendencia import Tendencia


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
# La cabecera: el semáforo no se degrada en silencio
# ---------------------------------------------------------------------------


def test_la_cabecera_lleva_el_semaforo_del_dia():
    """Las tres luces reales, con su emoji y su nombre."""
    for luz, emoji, nombre in (
        ("green", "🟢", "VERDE"),
        ("amber", "🟡", "ÁMBAR"),
        ("red", "🔴", "ROJO"),
    ):
        assert EMOJI[luz] == emoji
        assert NOMBRE_LUZ[luz] == nombre


def test_un_semaforo_desconocido_revienta_en_vez_de_salir_en_blanco(cfg):
    """Antes salía "⚪ ... — PURPLE" y el mensaje seguía adelante tan normal.

    El semáforo es la cabecera y el resumen de la decisión entera del día. Un
    valor que no está en la tabla solo puede venir de algo roto -un config.yaml
    con una luz nueva, una regla que devuelve otra cosa, una errata-, y en un
    sistema que decide solo eso tiene que parar, no pintarse de gris y pedir
    entrenar igual bajo una luz que no existe.
    """
    d = decision_completa(cfg)
    d.light = "purple"

    with pytest.raises(ValueError) as exc:
        render_plain(d, cfg)

    assert "purple" in str(exc.value)
    assert "semáforo desconocido" in str(exc.value)


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


def test_el_hiit_apagado_en_el_config_no_se_repite_cada_dia(cfg_copia):
    """Con `hiit.enabled: false` el motivo es el mismo hoy y dentro de seis
    meses. Una línea que sale todos los días no se lee: se aprende a saltarla,
    y con ella se saltan las que sí cambian.

    El apagado se monta aquí en vez de leerlo del `config.yaml` real. Cuando el
    HIIT se encendió de verdad -el 13 de septiembre de 2026- este test se cayó
    sin que la propiedad que vigila hubiera cambiado en nada: estaba atado al
    valor del flag y no a la regla.
    """
    cfg_copia.raw["hiit"]["enabled"] = False
    d = decision(cfg_copia)
    assert not any(n.startswith("sin HIIT") for n in d.session.notes)
    assert "sin HIIT" not in render_plain(d, cfg_copia)


def test_una_regla_que_quita_el_hiit_no_dice_el_motivo_al_reves(cfg_hiit, estado_hiit):
    """La rama `ok and permitido` metía en el `else` común el caso "tocaba
    HIIT pero algo lo ha quitado", y ahí `why` vale "semana 1, verde y dia_1
    lo admite": el motivo de que SÍ tocara, presentado como el de que no."""
    cfg_hiit.raw["actions"]["green"]["allow_hiit"] = False
    d = decision_completa(cfg_hiit, estado=estado_hiit)
    nota = next((n for n in d.session.notes if n.startswith("sin HIIT")), None)
    assert nota is not None
    assert "lo admite" not in nota, f"el motivo está del revés: {nota!r}"
    assert "regla especial" in nota


def test_el_hiit_se_nombra_por_su_titulo_y_no_por_su_clave(cfg_hiit, estado_hiit):
    """Salía `hiit_dia_1` en el mensaje de la mañana, que es el único sitio
    donde esto se lee, mientras el YAML tenía «Día 1 HIIT» a dos líneas del
    `hevy_routine_id`. Los ejercicios y la rutina de fuerza ya se traducían;
    el bloque HIIT era el único identificador crudo que quedaba en pantalla.
    """
    d = decision_completa(cfg_hiit, estado=estado_hiit)
    assert d.session.hiit_block == "hiit_dia_1", (
        f"el escenario ya no programa HIIT: {d.session.notes}"
    )
    txt = render_plain(d, cfg_hiit)
    assert "Día 1 HIIT" in txt
    assert "hiit_dia_1" not in txt


def test_un_bloque_de_hiit_sin_titulo_se_nombra_feo_pero_no_en_blanco(
    cfg_hiit, estado_hiit
):
    """Misma regla que los ejercicios y que las rutinas: una entrada a la que
    le falta el `title` se nombra con su clave, nunca con un hueco. La línea
    del HIIT sin nada detrás se leería como que hoy no hay HIIT, que es
    exactamente lo contrario de lo que pasa."""
    cfg_hiit.raw["routines"]["hiit_dia_1"].pop("title", None)
    d = decision_completa(cfg_hiit, estado=estado_hiit)
    assert d.session.hiit_block == "hiit_dia_1"
    assert "(entreno aparte):</b> hiit_dia_1" in render_telegram(d, cfg_hiit)


def test_un_bloque_de_hiit_que_no_existe_se_dice_en_vez_de_desaparecer(
    cfg_hiit, estado_hiit
):
    """El peor de los tres: todo decía que tocaba HIIT, el bloque no estaba en
    `routines`, y la sesión salía sin él sin una sola línea en ninguna parte.
    Una errata en `hiit.blocks` borraba el HIIT del programa en silencio."""
    cfg_hiit.raw["hiit"]["blocks"]["dia_1"] = "bloque_que_no_existe"
    d = decision_completa(cfg_hiit, estado=estado_hiit)
    assert d.session.hiit_block is None
    nota = next((n for n in d.session.notes if n.startswith("sin HIIT")), None)
    assert nota is not None, f"el bloque desapareció sin decir nada: {d.session.notes}"
    assert "bloque_que_no_existe" in nota
    assert "bloque_que_no_existe" in render_plain(d, cfg_hiit)


def test_un_dia_rojo_dice_que_la_rotacion_no_se_mueve(cfg):
    """Sin decirlo, un día rojo se lee como una sesión perdida.

    Antes la frase era "queda pendiente" y detrás había una tabla. Ahora no hay
    tabla y la frase tiene que decir la verdad nueva: que no hay nada que
    recuperar porque no se ha ido nada, y que el próximo día de gimnasio sigue
    tocando lo mismo.
    """
    d = decision(cfg, lower_discomfort=7)
    assert d.session.kind == "recovery"
    txt = render_plain(d, cfg)
    assert "la rotación no se mueve" in txt, txt
    assert d.rotation_routine in txt, "hay que decir CUÁL sigue tocando"


def test_los_dias_sin_fuerza_se_dicen_aunque_el_razonamiento_este_apagado(
    cfg_sin_motivo,
):
    """Cuánto hace que no entreno es un hecho, no la explicación de una decisión.

    Es la misma frontera que el bloque de "Decidido con datos incompletos":
    dentro de `include_reasoning` van los porqués, y quien lo apaga está
    diciendo "no me cuentes cómo lo has razonado", no "no me digas cuándo fue
    la última vez que levanté algo".

    Aquí estaba el aviso de "Sesión perdida" del aplazamiento caducado. Ya no
    se pierde ninguna sesión -la rotación espera-, así que lo que queda es el
    recuento, y el recuento no tiene umbral: sale siempre, diga 1 o diga 19. Un
    número que aparece el día 8 y no el día 7 no es un dato, es una opinión con
    un disfraz.
    """
    from datetime import timedelta

    from app.engine.decision import advance_state

    cfg = cfg_sin_motivo
    rojo = decide(cfg, LUNES, sig(LUNES, lower_discomfort=7), EngineState())
    st = advance_state(EngineState(), rojo, executed={})

    tarde = LUNES + timedelta(days=9)
    d = decide(cfg, tarde, sig(tarde), st)

    txt = render_plain(d, cfg)
    assert "sesión de fuerza" in txt
    assert "9 días" in txt or "todavía" in txt.lower()


def test_el_recuento_de_dias_sale_con_el_numero_y_la_fecha(cfg_sin_motivo):
    """Sin denominador y sin reproche: cuántos días y de qué día fue."""
    from datetime import timedelta

    cfg = cfg_sin_motivo
    hace_nueve = LUNES - timedelta(days=9)
    d = decide(cfg, LUNES, sig(LUNES), EngineState(last_strength=("dia_2", hace_nueve)))

    txt = render_plain(d, cfg)
    assert "9 días" in txt, txt
    assert hace_nueve.isoformat() in txt or hace_nueve.strftime("%d/%m") in txt, txt
    assert d.rotation_routine == "dia_3", "y sigue tocando la que tocaba"


def test_sin_ninguna_sesion_leida_el_mensaje_lo_dice_en_vez_de_poner_un_cero(
    cfg_sin_motivo,
):
    """El primer día. Cero días sin entrenar y "nunca he entrenado" son cosas
    distintas, y un 0 las confundiría."""
    cfg = cfg_sin_motivo
    d = decide(cfg, LUNES, sig(LUNES), EngineState())

    txt = render_plain(d, cfg)
    assert "0 días" not in txt
    assert "sesión de fuerza" in txt


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


def test_despues_de_un_rojo_el_mensaje_no_habla_de_recuperar_nada(cfg):
    """El día siguiente a un rojo es una mañana normal.

    Aquí estaba el test de "Recuperas la sesión": la cabecera lo decía una vez
    y el apunte del motor no tenía que repetirlo. Ya no hay nada que recuperar
    -la rotación no se movió, así que hoy toca lo mismo que ayer-, y el mensaje
    no puede hablar de recuperaciones ni de pendientes, porque las dos palabras
    dan a entender que hay una deuda apuntada en alguna parte.
    """
    from datetime import timedelta

    from app.engine.decision import advance_state

    rojo = decide(cfg, LUNES, sig(LUNES, lower_discomfort=7), EngineState())
    st = advance_state(EngineState(), rojo, executed={})

    martes = LUNES + timedelta(days=1)
    d = decide(cfg, martes, sig(martes), st)
    assert d.session.routine_key == "dia_1", "la rotación se movió con un día rojo"

    txt = render_plain(d, cfg)
    assert "Recuperas" not in txt
    assert "pendiente" not in txt
    assert "Si vas al gimnasio hoy" in txt


# ---------------------------------------------------------------------------
# «Hoy no voy a entrenar»: el mensaje deja de prescribir y pasa a informar
# ---------------------------------------------------------------------------
#
# La pregunta del check-in existe para los días que NO se entrena, que son los
# que hasta ahora no dejaban ningún rastro: sin fila en `checkins`, sin nada en
# las correlaciones, y con un mensaje a las siete de la mañana proponiendo ocho
# ejercicios a alguien que ya había decidido que no.
#
# Lo que este bloque fija es la frontera: se calla la PRESCRIPCIÓN -la sesión,
# las subidas, los ejercicios retirados- y se queda todo lo demás, porque nada
# de lo demás es una prescripción. Y se calla con `is False`, nunca con la
# falsedad a secas, porque el día sin contestar no es un día de "no voy".


def test_un_no_voy_no_propone_sesion_pero_dice_que_la_rutina_sigue_ahi(cfg):
    """Las dos frases que sustituyen al plan del día, y ninguna más.

    Un mensaje al que simplemente le falta el bloque de la sesión se lee como un
    mensaje roto. Las dos preguntas que quedan en el aire -¿pierdo el turno? ¿y
    si cambio de idea?- se contestan aquí, porque la respuesta a las dos es que
    no pasa nada y nadie se fía de lo que no se le cuenta.
    """
    d = decision_completa(cfg, will_train=False)
    txt = render_plain(d, cfg)

    assert "Hoy no entrenas" in txt, txt
    assert "la rotación no se mueve" in txt, txt
    assert "escrita en Hevy" in txt, "decir «no» por la mañana no cierra la puerta"

    # El TÍTULO, no la clave. "dia_1 sigue siendo la siguiente" es el sistema
    # hablando en su propio idioma, que es la queja de siempre.
    titulo = cfg.raw["routines"][d.rotation_routine]["title"]
    assert titulo in txt, f"hay que decir CUÁL sigue siendo la siguiente:\n{txt}"
    assert d.rotation_routine not in txt, "ha salido la clave cruda en vez del título"

    assert "Si vas al gimnasio hoy" not in txt, "sigue prescribiendo la sesión"
    for ex in d.session.exercises:
        assert ex["name"] not in txt, f"ha colado un ejercicio: {ex['name']}\n{txt}"


def test_un_no_voy_no_regana_ni_anima(cfg):
    """Has contestado una pregunta que hizo el sistema; discutir la respuesta enseña a no contestarla."""
    txt = render_plain(decision_completa(cfg, will_train=False), cfg).lower()
    for palabra in ("seguro", "ánimo", "anímate", "recuerda que", "deberías",
                    "¿de verdad", "esfuerzo", "excusa", "mañana sin falta"):
        assert palabra not in txt, f"el mensaje opina sobre la respuesta: {palabra!r}"


def test_sin_contestar_la_pregunta_el_mensaje_es_exactamente_el_de_siempre(cfg):
    """EL TEST QUE PROTEGE LOS 364 DÍAS QUE NO SE RELLENA EL FORMULARIO.

    `None` no es `False`. Escrito `if decision.va_a_entrenar:` en vez de
    `is not False`, el sistema dejaría de proponer sesión todos los días sin
    check-in -que son la mayoría- y lo haría sin romper nada: el mensaje seguiría
    llegando, con su semáforo, su tendencia y su bici, y sin el plan.

    Por eso se compara el texto ENTERO contra el de antes de que la pregunta
    existiera, y no solo se busca "Si vas al gimnasio". Un día sin contestar
    tiene que renderizar carácter por carácter como siempre.
    """
    antes = render_plain(decision_completa(cfg), cfg)
    despues = render_plain(decision_completa(cfg, will_train=None), cfg)
    assert despues == antes
    assert "Si vas al gimnasio hoy" in antes
    assert "Hoy no entrenas" not in antes


def test_un_si_voy_prescribe_igual_que_siempre(cfg):
    """La otra mitad: contestar que sí no cambia nada."""
    assert (
        render_plain(decision_completa(cfg, will_train=True), cfg)
        == render_plain(decision_completa(cfg), cfg)
    )


def test_que_no_te_apetezca_no_calla_la_sesion(cfg):
    """Las dos preguntas no son la misma pregunta.

    `wants_to_train` es la que se cruza con la otra para sacar la discordancia;
    no decide nada por su cuenta. Un día de «no me apetece pero voy» tiene que
    salir con su sesión entera, y este test es lo que impide que alguien
    conecte la pregunta equivocada al `if`.
    """
    d = decision_completa(cfg, wants_to_train=False, will_train=True)
    txt = render_plain(d, cfg)
    assert "Si vas al gimnasio hoy" in txt, txt
    assert "Hoy no entrenas" not in txt


def test_un_no_voy_conserva_todo_lo_que_no_es_una_prescripcion(cfg):
    """Estado, tendencia, bici, adopciones, intensas y recalibración se quedan.

    El día que no se entrena no es un día en blanco: la adopción de anoche sigue
    escrita en Hevy, el HRV sigue bajando desde hace seis días y la salida en
    bici sigue siendo hoy. Lo único que sobra es el plan.
    """
    from datetime import timedelta

    from app.engine.signals import IntensityCount
    from app.engine.tendencia import DecisionDia, evaluar_tendencia

    d = decide(cfg, LUNES, sig_completa(LUNES, will_train=False), EngineState())

    # `adopcion` y el montaje de la racha están definidos más abajo en este mismo
    # fichero, en sus bloques. Se reutilizan a propósito: una adopción escrita a
    # mano aquí se me quedó con las claves de otra versión y el test pasaba por
    # el motivo equivocado, enseñando "🛑 No adoptado · : se registraron  kg".
    d.load_adoptions = [adopcion(key="prensa_horizontal", after_kg=62.5)]
    d.signals.intense_count = IntensityCount(
        used=3, detail=[], desde=LUNES - timedelta(days=6), hasta=LUNES, unknown=0
    )
    # La racha se calcula con la capa de verdad, no se escribe a mano: dos copias
    # del criterio acaban divergiendo y este test dejaría de comprobar nada.
    historia = [
        DecisionDia(LUNES - timedelta(days=i), "green") for i in range(46, 5, -1)
    ] + [
        DecisionDia(LUNES - timedelta(days=i), "amber", "sueno_corto")
        for i in range(5, -1, -1)
    ]
    d.tendencia = evaluar_tendencia(cfg, LUNES, historia)
    assert d.tendencia.lineas(), "el montaje del test no produce tendencia"

    txt = render_plain(d, cfg)
    assert "Hoy no entrenas" in txt
    assert "62,5" in txt, f"la adopción de anoche ha desaparecido:\n{txt}"
    assert "6 días seguidos" in txt, f"la tendencia ha desaparecido:\n{txt}"
    assert "🔥" in txt, f"el recuento de intensas ha desaparecido:\n{txt}"
    assert "Bici" in txt or "🚴" in txt, f"la bici ha desaparecido:\n{txt}"
    assert "Última sesión de fuerza" in txt or "Todavía no hay" in txt


def test_un_rojo_con_no_voy_tampoco_prescribe_la_recuperacion(cfg):
    """El bloque de recuperación es una prescripción como cualquier otra.

    Es el caso que más fácil se cuela, porque la recuperación se anuncia en
    indicativo -no lleva el "si vas al gimnasio" delante- y se construye por una
    rama distinta del mensaje. Si el `if` estuviera dentro de la rama del `else`,
    este test sería el único que lo vería.
    """
    d = decide(cfg, LUNES, sig(LUNES, lower_discomfort=7, will_train=False), EngineState())
    assert d.session.kind == "recovery", "el semáforo lo decide el dolor, no la respuesta"

    txt = render_plain(d, cfg)
    assert "Hoy no entrenas" in txt
    assert "recuperación" not in txt, f"ha prescrito el bloque de recuperación:\n{txt}"
    for ex in d.session.exercises:
        assert ex["name"] not in txt, f"ha colado un ejercicio: {ex['name']}"


def test_la_pregunta_no_toca_el_semaforo_ni_la_sesion_que_se_escribe_en_hevy(cfg):
    """Se calla el mensaje, no se cambia la decisión.

    La rutina se sigue escribiendo en Hevy -por si cambias de idea a las siete
    de la tarde-, así que `decision.session` tiene que venir intacta: mismos
    ejercicios, mismo tipo y mismo color que el día idéntico en que se contestó
    que sí. Si algún día alguien "optimiza" esto vaciando la sesión en el motor,
    el mensaje seguiría igual de bonito y Hevy se quedaría vacío.
    """
    no = decision_completa(cfg, will_train=False)
    si = decision_completa(cfg, will_train=True)

    assert no.light == si.light
    assert no.session.kind == si.session.kind
    assert no.session.routine_key == si.session.routine_key
    assert [e["key"] for e in no.session.exercises] == [
        e["key"] for e in si.session.exercises
    ]
    assert no.rotation_routine == si.rotation_routine


def test_un_no_voy_no_saca_fuera_hoy_sin_haber_ensenado_la_sesion(cfg):
    """`s.dropped` lo llena el semáforo y le da igual lo que hayas contestado.

    Sin el `if`, el mensaje sacaría "➖ Fuera hoy: X, Y" sin haber enseñado antes
    ninguna sesión de la que sacarlos. Fuera ¿de qué?
    """
    # Un ámbar por cansancio: el config real recorta tres ejercicios ahí. Da
    # igual qué regla lo pinte -los recortes salen de `expendable_in_amber`, no
    # de la regla- pero tiene que ser una regla viva.
    d = decide(cfg, LUNES, sig_completa(LUNES, fatigue=7.0, will_train=False), EngineState())
    si = decide(cfg, LUNES, sig_completa(LUNES, fatigue=7.0), EngineState())
    assert si.session.dropped, "el montaje del test ya no recorta nada"
    assert d.session.dropped == si.session.dropped, (
        "la respuesta ha cambiado la sesión que se escribe en Hevy"
    )

    txt = render_plain(d, cfg)
    assert "Fuera hoy" not in txt, txt
    assert "Fuera hoy" in render_plain(si, cfg), "el bloque ya no sale ningún día"


def test_un_no_voy_no_anuncia_subidas_de_peso(cfg):
    """No se prescribe progresión el día que no se entrena.

    Hoy la lista ya viene vacía: `evaluate_gate` cierra la puerta antes que nada
    y `plan_progression` sale por `continue` sin tocar un ejercicio. Este test
    comprueba las dos mitades -la puerta cerrada con su motivo, y el bloque
    ausente del mensaje- para que el día que alguien mueva la comprobación de
    sitio, el fallo salga aquí y no en forma de "📈 Sube hoy" debajo de un
    "🌙 Hoy no entrenas".
    """
    d = decision_completa(cfg, will_train=False)
    assert d.progression is not None
    assert not d.progression.gate_open
    assert "no entrenas" in d.progression.gate_reason, d.progression.gate_reason
    assert d.progression.changes == []
    assert "Sube hoy" not in render_plain(d, cfg)


# ---------------------------------------------------------------------------
# `routines.*.focus`: la única frase que dice de qué va el día
# ---------------------------------------------------------------------------
#
# Llevaba desde el principio en el YAML -"Tren inferior + core", "Cadena
# posterior + espalda", "Caderas + hombro + brazo + core"- sin que lo leyera
# nadie. El mensaje enumeraba ocho ejercicios sin encabezarlos.


def test_el_foco_del_dia_llega_al_encabezado(cfg):
    txt = render_plain(decision_completa(cfg), cfg)
    foco = cfg.raw["routines"]["dia_1"]["focus"]
    assert foco in txt, f"el foco del día no aparece:\n{txt}"


def test_el_foco_va_pegado_al_titulo_y_no_en_una_linea_suelta(cfg):
    """El mensaje ya tiene bloques de sobra: esto es un subtítulo."""
    txt = render_plain(decision_completa(cfg), cfg)
    foco = cfg.raw["routines"]["dia_1"]["focus"]
    cabecera = next(l for l in txt.splitlines() if l.startswith("💪"))
    assert cabecera.endswith(f"— {foco}")
    assert "sesión completa" in cabecera, "el tipo de sesión no se pierde"


def test_el_foco_sale_del_yaml_y_no_de_una_tabla_a_mano(cfg):
    c = copy.deepcopy(cfg)
    c.raw["routines"]["dia_1"]["focus"] = "Tirón horizontal y nada más"
    assert "Tirón horizontal y nada más" in render_plain(decision_completa(c), c)


def test_una_rutina_sin_foco_no_revienta_el_mensaje(cfg):
    """El config lo exige donde se lee, pero el renderizador no puede caerse:
    llega aquí con el `raw` en la mano desde sitios que no validan."""
    c = copy.deepcopy(cfg)
    del c.raw["routines"]["dia_1"]["focus"]
    cabecera = next(
        l for l in render_plain(decision_completa(c), c).splitlines()
        if l.startswith("💪")
    )
    assert cabecera.endswith("(sesión completa)")


def test_el_foco_sobrevive_a_include_reasoning_false(cfg_sin_motivo):
    """No es razonamiento: es de qué va la sesión."""
    txt = render_plain(decision_completa(cfg_sin_motivo), cfg_sin_motivo)
    assert cfg_sin_motivo.raw["routines"]["dia_1"]["focus"] in txt


def test_un_dia_de_recuperacion_no_lleva_foco(cfg):
    """El único día cuyo `routine_key` no apunta a `routines`.

    Era "un día de descanso", pero ya no hay días de descanso decididos por el
    sistema. Queda el rojo: su `routine_key` apunta a `recovery_blocks`, la
    búsqueda del foco no encuentra nada y el encabezado se queda sin subtítulo.
    Si algún día el foco se buscara con un `or` de relleno, saldría el de otra
    rutina y el mensaje diría "Tren inferior + core" encima de un bloque de
    banda elástica.
    """
    d = decision(cfg, lower_discomfort=7)
    assert d.session.kind == "recovery"
    txt = render_plain(d, cfg)
    for r in cfg.raw["routines"].values():
        assert r.get("focus", "\0") not in txt


# ---------------------------------------------------------------------------
# Las adopciones de carga: por qué el peso de hoy no es el que se anunció ayer
# ---------------------------------------------------------------------------
#
# Este bloque es la mitad visible del punto 13. El motor puede ajustar la carga
# a lo que de verdad se levantó, pero si no lo cuenta, el usuario ve un número
# distinto del que el mensaje de ayer prometía y no tiene forma de saber si eso
# es el sistema funcionando o el sistema roto. Y las RECHAZADAS importan
# igual: un tope que actúa en silencio deja un ejercicio quieto sin motivo
# visible.


def adopcion(**kw) -> dict:
    base = {
        "routine": "dia_1",
        "key": "prensa_horizontal",
        "direction": "up",
        "prescribed_kg": 60.0,
        "executed_kg": 65.0,
        "before_kg": 60.0,
        "after_kg": 65.0,
        "applied": True,
        "reason": "se levantó eso de verdad",
    }
    base.update(kw)
    return base


def con_adopciones(cfg, *adopciones):
    d = decision_completa(cfg)
    d.load_adoptions = list(adopciones)
    return render_plain(d, cfg)


def test_una_adopcion_aplicada_se_cuenta_con_los_dos_pesos(cfg):
    txt = con_adopciones(cfg, adopcion())
    assert "Ajustado a lo que levantaste" in txt
    linea = next(l for l in txt.splitlines() if "Prensa horizontal" in l and "→" in l)
    assert "60" in linea and "65" in linea
    assert "se levantó eso de verdad" in linea, "el motivo es la mitad del aviso"


def test_la_flecha_distingue_subir_de_bajar(cfg):
    """Bajar la carga es la noticia importante del mensaje: tiene que verse de
    un vistazo y no confundirse con una subida."""
    assert "↑" in con_adopciones(cfg, adopcion(direction="up"))
    txt = con_adopciones(
        cfg, adopcion(direction="down", before_kg=60.0, after_kg=50.0, executed_kg=50.0)
    )
    assert "↓" in txt
    assert "50" in txt


def test_una_adopcion_rechazada_sale_en_su_propio_bloque(cfg):
    """"He movido esto" y "he visto un número raro y NO lo he tocado" son dos
    cosas distintas de leer, y mezclarlas haría que la segunda se perdiera."""
    txt = con_adopciones(
        cfg,
        adopcion(applied=False, after_kg=None, executed_kg=600.0,
                 reason="salto de 540 kg: pasa del máximo"),
    )
    assert "No adoptado" in txt
    assert "600" in txt
    assert "sigue en 60" in txt
    assert "pasa del máximo" in txt
    assert "Ajustado a lo que levantaste" not in txt, (
        "una rechazada no puede aparecer bajo el título de las aplicadas"
    )


def test_aplicadas_y_rechazadas_conviven_separadas(cfg):
    txt = con_adopciones(
        cfg,
        adopcion(),
        adopcion(key="extension_cuadriceps", applied=False, after_kg=None,
                 executed_kg=600.0, reason="pasa del máximo"),
    )
    assert txt.index("Ajustado a lo que levantaste") < txt.index("No adoptado")
    assert "Prensa horizontal" in txt
    assert "Extensión de cuádriceps" in txt


def test_el_nombre_sale_del_config_de_hoy_y_no_de_la_fila_guardada(cfg):
    """La fila guarda la clave, no el nombre, a propósito: un nombre copiado en
    la base de datos es una copia del YAML que envejece sola y acaba
    contradiciendo al resto del mensaje."""
    c = copy.deepcopy(cfg)
    c.raw["routines"]["dia_1"]["exercises"][0]["name"] = "Prensa nueva"
    d = decision_completa(c)
    d.load_adoptions = [adopcion()]
    assert "Prensa nueva" in render_plain(d, c)


def test_un_ejercicio_que_ya_no_existe_sale_por_su_clave(cfg):
    """Feo pero cierto. Callar la adopción porque el ejercicio se quitó de la
    rutina dejaría el cambio de carga sin explicar, que es lo único que este
    bloque existe para impedir."""
    txt = con_adopciones(cfg, adopcion(key="ejercicio_borrado"))
    assert "ejercicio_borrado" in txt


def test_sin_adopciones_no_aparece_el_bloque(cfg):
    txt = render_plain(decision_completa(cfg), cfg)
    assert "Ajustado a lo que levantaste" not in txt
    assert "No adoptado" not in txt
    assert "Sin nada que leer" not in txt


def test_un_ejercicio_sin_peso_apuntado_se_cuenta_aparte(cfg):
    """El suitcase carry: en la sesión, sin una sola serie efectiva apuntada.

    Antes esto no salía en ninguna parte. Un ejercicio que cae aquí sesión tras
    sesión tiene la carga congelada para siempre y lo único que se ve es un
    número que no cambia, que es justo lo que no se ve.
    """
    txt = con_adopciones(
        cfg,
        adopcion(
            applied=False, after_kg=None, executed_kg=None,
            reason="está en el entrenamiento pero sin una sola serie efectiva apuntada",
        ),
    )
    assert "Sin nada que leer" in txt
    assert "sin una sola serie efectiva apuntada" in txt
    assert "sigue en 60" in txt
    assert "No adoptado" not in txt, (
        "sin kilos que enseñar, la línea de las rechazadas diría 'se registraron "
        "— kg' y se leería como un fallo de formato"
    )


def test_lo_que_no_se_pudo_leer_no_se_mezcla_con_lo_que_se_rechazo(cfg):
    """Son dos avisos distintos: "he visto un número raro y no lo he tocado" y
    "no he visto ningún número"."""
    txt = con_adopciones(
        cfg,
        adopcion(applied=False, after_kg=None, executed_kg=600.0,
                 reason="pasa del máximo"),
        adopcion(key="extension_cuadriceps", applied=False, after_kg=None,
                 executed_kg=None, reason="no aparece en el entrenamiento"),
    )
    assert txt.index("No adoptado") < txt.index("Sin nada que leer")
    assert "600" in txt
    assert "no aparece en el entrenamiento" in txt


def test_una_adopcion_sin_motivo_lo_dice_en_vez_de_dejar_el_guion_colgando(cfg):
    """"62,5 kg —" y nada detrás parece un error de formato, no un dato ausente.

    El motivo es la mitad del aviso: sin él la línea dice que la carga se movió
    sola y no dice por qué, que es exactamente lo que `load_adoptions` existe
    para impedir. Que falte puede pasar; que no se note, no.
    """
    linea = next(
        l
        for l in con_adopciones(cfg, adopcion(reason=None)).splitlines()
        if "Prensa horizontal" in l and "→" in l
    )

    assert "sin motivo registrado" in linea
    assert not linea.rstrip().endswith("—"), "el guion no puede quedarse colgando"


def test_una_adopcion_rechazada_sin_motivo_tambien_lo_dice(cfg):
    """El mismo hueco en el otro bloque, donde además duele más: una rechazada
    ES su motivo. "He visto un número raro y no lo he tocado" sin decir cuál es
    un aviso que no se puede accionar."""
    txt = con_adopciones(
        cfg, adopcion(applied=False, after_kg=None, executed_kg=600.0, reason="")
    )

    assert "sin motivo registrado" in txt


def test_las_adopciones_sobreviven_a_include_reasoning_false(cfg_sin_motivo):
    """No es razonamiento: es "el peso de hoy no es el que te dije ayer".

    Apagar el porqué de la decisión no puede ocultar que la carga se movió sola.
    """
    txt = con_adopciones(cfg_sin_motivo, adopcion())
    assert "Ajustado a lo que levantaste" in txt


def test_las_adopciones_van_antes_de_lo_que_sube_hoy(cfg):
    """Orden de lectura: primero "esto ya no es lo que creías", después "y
    encima hoy sube".

    Ocurrió en ese orden -la adopción se decide al reconciliar por la noche y
    fija el punto de partida desde el que la mañana progresa-, así que leerlo al
    revés haría que un "62,5→65" pareciera contradecir un objetivo que dos
    líneas más arriba era 60.
    """
    c = copy.deepcopy(cfg)
    c.raw["routines"]["dia_1"]["exercises"] = [
        {
            "key": "prensa_horizontal",
            "name": "Prensa horizontal",
            "progression_type": "load",
            "sets": [{"reps": 10, "weight_kg": 60}, {"reps": 10, "weight_kg": 60}],
        }
    ]
    st = EngineState(
        compliance={("dia_1", "prensa_horizontal"): True},
        clean_sessions={("dia_1", "prensa_horizontal"): 5},
    )
    d = decide(c, LUNES, sig_completa(LUNES), st)
    d.load_adoptions = [adopcion()]
    txt = render_plain(d, c)

    assert "Sube hoy" in txt, "el montaje tenía que producir una subida de verdad"
    assert txt.index("Ajustado a lo que levantaste") < txt.index("Sube hoy")


# ---------------------------------------------------------------------------
# El bloque de tendencia
# ---------------------------------------------------------------------------
#
# Mismo argumento que las degradaciones, por el otro extremo. Las notas dicen
# "hoy he decidido con menos datos de los que debería"; la tendencia dice "y
# además llevas seis días así, cosa que no ha entrado en la decisión porque
# ninguna regla mira tan atrás". Las dos son información que el razonamiento no
# contiene, y por eso las dos sobreviven a `include_reasoning: false`.


def con_tendencia(cfg, dias_seguidos: int = 6, **kw):
    """Un mensaje con una racha real detrás, calculada por la capa de verdad."""
    from datetime import timedelta

    from app.engine.tendencia import DecisionDia, evaluar_tendencia

    dec = decision_completa(cfg)
    historia = [
        DecisionDia(LUNES - timedelta(days=i), "green")
        for i in range(dias_seguidos + 40, dias_seguidos - 1, -1)
    ] + [
        DecisionDia(LUNES - timedelta(days=i), "amber", "sueno_corto")
        for i in range(dias_seguidos - 1, -1, -1)
    ]
    dec.tendencia = evaluar_tendencia(cfg, LUNES, historia, **kw)
    return render_telegram(dec, cfg), dec.tendencia


def test_la_tendencia_sale_en_el_mensaje(cfg):
    txt, t = con_tendencia(cfg)
    assert "Tendencia" in txt
    assert "6 días seguidos sin un verde" in txt


def test_la_tendencia_sobrevive_a_include_reasoning_false(cfg_sin_motivo):
    """No es razonamiento: es lo que el razonamiento NO podía ver.

    Ninguna regla del semáforo mira más de tres días atrás. "Llevas seis días
    sin un verde" no explica la decisión de hoy porque no entró en ella: es lo
    único que lo dice. Ocultarlo al apagar el porqué sería ocultar justo la
    parte que no se puede deducir de ninguna otra línea del mensaje.
    """
    txt, _ = con_tendencia(cfg_sin_motivo)
    assert "6 días seguidos sin un verde" in txt


def test_el_prefijo_no_se_repite_en_cada_linea(cfg):
    """La cabecera del bloque ya lo dice; repetirlo ocho palabras más abajo no."""
    txt, _ = con_tendencia(cfg)
    assert "• Tendencia:" not in txt


def test_sin_tendencia_no_hay_bloque(cfg):
    """Un mensaje anterior a esta capa, o un `--dry-run` que no la calcula."""
    d = decision_completa(cfg)
    assert "📉" not in render_telegram(d, cfg)


def test_la_capa_apagada_no_deja_bloque_vacio(cfg_copia):
    """`enabled: false` se calla del todo: ni cabecera huérfana."""
    cfg_copia.raw["trend"]["enabled"] = False
    txt, t = con_tendencia(cfg_copia)
    assert t.activa is False
    assert "📉" not in txt


def test_el_sin_muestra_tambien_se_imprime(cfg):
    """Que falte muestra no puede leerse como que no pasa nada.

    Es la misma norma que las degradaciones: el mensaje del día que se sabe todo
    y el del día que no se sabe casi nada no pueden ser idénticos.
    """
    txt, _ = con_tendencia(cfg, dias_seguidos=2)
    assert "sin muestra" in txt


def test_va_despues_de_la_bici_y_antes_de_las_degradaciones(cfg):
    """Orden de lectura: qué hago hoy, hacia dónde voy, con qué fiabilidad.

    La tendencia cierra el plan y abre las advertencias. Si se colara entre las
    notas de datos incompletos, "llevas seis días sin un verde" se leería como
    una degradación más y es lo contrario: es un dato que sí se tiene.
    """
    from datetime import timedelta

    from app.engine.tendencia import DecisionDia, evaluar_tendencia

    d = decision(cfg, notas=DEGRADACIONES)
    historia = [
        DecisionDia(LUNES - timedelta(days=i), "green") for i in range(46, 5, -1)
    ] + [
        DecisionDia(LUNES - timedelta(days=i), "amber", "sueno_corto")
        for i in range(5, -1, -1)
    ]
    d.tendencia = evaluar_tendencia(cfg, LUNES, historia)
    txt = render_telegram(d, cfg)

    assert txt.index("Tendencia") < txt.index(DEGRADACIONES[0])


# ---------------------------------------------------------------------------
# El recuento rodante: informa todos los días y no regaña ninguno
# ---------------------------------------------------------------------------
#
# Antes el número solo se escribía cuando servía para recortar la salida del
# sábado, así que de lunes a viernes el sistema lo sabía y no lo decía. Un dato
# que solo aparece cuando además te frena no es información: es la
# justificación del frenazo. Ahora que no frena nada, tiene que estar todos los
# días o no está.
#
# Y ya no dice «esta semana» sino «en los últimos 7 días». El cambio de palabra
# es el cambio de cuenta: la semana natural se vaciaba cada lunes de madrugada,
# y en 8 de los 26 lunes del histórico el mensaje decía «ninguna sesión intensa
# esta semana todavía» con una o dos intensas en los siete días anteriores -en
# tres de esos lunes, con el sábado Y el domingo intensos doce horas antes-.


def _con_conteo(cfg, used: int, unknown: int = 0):
    from app.engine.signals import IntensityCount

    d = decision(cfg)
    d.signals.intense_count = IntensityCount(
        used=used,
        detail=[],
        desde=LUNES - timedelta(days=6),
        hasta=LUNES,
        unknown=unknown,
    )
    return d


def test_el_recuento_sale_un_lunes_que_no_es_dia_de_bici(cfg):
    """LUNES: no hay recomendación de bici, y el número tiene que salir igual."""
    txt = render_telegram(_con_conteo(cfg, used=3), cfg)
    assert "3 sesiones intensas en los últimos 7 días" in txt


def test_el_recuento_sale_tambien_cuando_es_cero(cfg):
    """Cero no es un hueco: es el dato de que no ha habido nada fuerte en 7 días.

    Si el bloque se saltara con `if conteo.used:`, el lunes por la mañana -que
    es cuando más sentido tiene leerlo- no habría línea, y el mensaje del día
    que no has hecho nada sería idéntico al del día que el recuento se rompió.
    """
    txt = render_telegram(_con_conteo(cfg, used=0), cfg)
    assert "inguna sesión intensa en los últimos 7 días" in txt


def test_sin_recuento_no_hay_linea_ni_hueco(cfg):
    """El None legítimo: una ruta que no construye el recuento no pinta nada."""
    d = decision(cfg)
    assert d.signals.intense_count is None
    assert "🔥" not in render_telegram(d, cfg)


def test_el_recuento_del_mensaje_no_lleva_denominador(cfg):
    """Un "de 4" convierte el dato en un marcador, y un marcador prescribe.

    Da igual que el código no recorte: quien lee "3 de 4" en el móvil un martes
    entiende que le queda una, y eso es exactamente lo que se ha quitado.
    """
    txt = render_telegram(_con_conteo(cfg, used=3), cfg)
    linea = next(l for l in txt.splitlines() if "sesiones intensas" in l)
    assert "/" not in linea and " de " not in linea, linea


def test_el_recuento_alto_tampoco_regaña(cfg):
    """Nueve sesiones en una semana es la semana de viaje, no una infracción."""
    txt = render_telegram(_con_conteo(cfg, used=9), cfg).lower()
    linea = next(l for l in txt.splitlines() if "sesiones intensas" in l)
    for palabra in ("demasiad", "exceso", "excedid", "cuidado", "agotado", "límite"):
        assert palabra not in linea, f"tono de reproche: '{palabra}' en {linea!r}"


def _con_bici(day, **valores):
    """Señales con histórico de salidas intensas suficiente para que la bici hable.

    Desde que se quitó el calendario, el punto de partida de la bici sale de los
    huecos entre salidas intensas propias, así que unas señales sin histórico ya
    no producen recomendación.

    EL HISTORIAL SE IMPORTA DE `test_bike_advisor`, NO SE COPIA
    ------------------------------------------------------------
    Aquí había una copia a mano de los huecos y del número de días, con un
    comentario que decía «los mismos que en `test_bike_advisor`». Dejaron de
    serlo en cuanto los percentiles del YAML pasaron de p25/p75 a p40/p60: allí
    se recalcularon las fronteras y aquí se quedó un 7 que ya no caía en la
    banda de 'intensa'. Los tests de este fichero no comprueban las bandas
    -comprueban que lo que decide la bici llega al móvil- así que habrían
    seguido en verde describiendo un escenario que no era el que decían.

    Importar el fixture de verdad cuesta una dependencia entre módulos de test y
    ahorra que este bloque vuelva a caducar en silencio. Es el mismo trato que
    `falsear_bici.py` hace con `_baseline_gaps`: llamar a lo real en vez de
    reimplementarlo al lado.
    """
    from app.engine.signals import ClassifiedRide, Ride

    from tests.test_bike_advisor import DIAS_INTENSA, _fechas_intensas

    s = sig_completa(day, **valores)
    s.rides = [
        ClassifiedRide(
            ride=Ride(date=d, duration_s=7200),
            level="intensa",
            source="test",
            load=100.0,
            load_estimated=False,
        )
        for d in _fechas_intensas(day, DIAS_INTENSA)
    ]
    return s


def test_las_notas_de_la_bici_llegan_al_mensaje(cfg):
    """Las notas existen PARA esto, y nada más lo comprobaba.

    Lo encontró la falsación: borrar el bucle que las pinta -`for nota in []`-
    dejaba los 59 tests de este fichero en verde. Los tests del sábado
    comprueban que la recomendación LLEVA las notas, y el test del veneno
    comprueba que si se pintan van escapadas; entre los dos quedaba el hueco
    exacto de que no se pintaran. Y es el hueco que importa: una nota que se
    calcula bien y no sale del móvil es un dato que no existe.
    """
    from datetime import timedelta

    sabado = LUNES + timedelta(days=5)
    s = _con_bici(sabado, yesterday_ride_level="intensa")
    d = decide(cfg, sabado, s, EngineState())

    assert d.bike is not None and d.bike.applies
    txt = render_telegram(d, cfg)
    for nota in d.bike.texto_notas():
        assert nota in txt, f"la nota «{nota}» se calcula y no sale del móvil"
    assert d.bike.texto_notas(), "sin notas este test no prueba nada"


def test_de_donde_sale_el_nivel_llega_al_movil(cfg):
    """Calcular la explicación y no enseñarla es igual que no calcularla.

    Mismo hueco que el de las notas, y por eso está pegado a él: hay tests que
    comprueban que `baseline_en_claro` se calcula bien y tests que comprueban
    que el mensaje escapa lo que pinta, y entre los dos cabe entero el caso de
    que no se pinte. Con una diferencia que lo hace peor que en las notas: esta
    frase es la ÚNICA forma que tiene el usuario de saber de dónde sale el
    nivel, porque el punto de partida ya no es un número del YAML que se pueda
    ir a mirar.

    Si esto deja de salir, el mensaje vuelve a afirmar «intensa» a secas, que es
    la definición de aparentar más certeza de la que se tiene.
    """
    from datetime import timedelta

    sabado = LUNES + timedelta(days=5)
    s = _con_bici(sabado, yesterday_ride_level="intensa")
    d = decide(cfg, sabado, s, EngineState())

    assert d.bike is not None and d.bike.applies
    assert d.bike.baseline_en_claro, "sin explicacion este test no prueba nada"
    txt = render_telegram(d, cfg)
    assert d.bike.baseline_en_claro in txt, (
        f"«{d.bike.baseline_en_claro}» se calcula y no sale del movil: el nivel "
        f"vuelve a caer del cielo"
    )


def test_las_notas_van_debajo_del_nivel_y_no_pegadas_a_el(cfg):
    """La distinción entre recortar y contar tiene que verse en la pantalla.

    Si la nota compartiera línea con "Bici: intensa", se leería como el motivo
    del nivel. No lo es: ninguna nota ha entrado en la decisión. Van en líneas
    propias y con viñeta, que es lo que separa un dato de una justificación.

    Ahora hay TRES cosas debajo del emoji y cada una dice algo distinto, así que
    la tipografía tiene que distinguirlas o el mensaje vuelve a ser un montón:

      🚴 Si sales hoy: ...        <- lo que se recomienda
         <i>llevas 9 días...</i>  <- POR QUÉ (sí entró en la decisión)
         · ayer hiciste una...    <- un hecho (NO entró en la decisión)
    """
    from datetime import timedelta

    sabado = LUNES + timedelta(days=5)
    s = _con_bici(sabado, yesterday_ride_level="intensa")
    txt = render_telegram(decide(cfg, sabado, s, EngineState()), cfg)

    lineas = txt.splitlines()
    i = next(n for n, l in enumerate(lineas) if "🚴" in l)
    assert "hiciste una salida intensa" not in lineas[i], "la nota se ha pegado al nivel"

    # La nota ya no es la línea de justo debajo: en medio va la procedencia del
    # punto de partida. Se busca por la viñeta y no por posición a propósito,
    # porque lo que este test defiende NO es el orden sino la distinción
    # tipográfica. Atarlo a `i + 1` lo rompería cada vez que se añada una línea
    # que no le incumbe, y un test que se rompe por motivos ajenos acaba
    # relajado a mano hasta que deja de comprobar lo suyo.
    bloque = lineas[i + 1 : i + 5]
    con_vineta = [l for l in bloque if l.lstrip().startswith("·")]
    assert con_vineta, f"la nota no sale con viñeta debajo del nivel: {bloque}"
    assert any("hiciste una salida intensa" in l for l in con_vineta)

    # Y la procedencia, que sí explica el nivel, va en cursiva y SIN viñeta.
    claro = [l for l in bloque if "<i>" in l]
    assert claro, f"el punto de partida no se explica debajo del nivel: {bloque}"
    assert not claro[0].lstrip().startswith("·"), (
        "la procedencia del nivel ha cogido la vineta de las notas: en la pantalla "
        "pasaria a leerse como un hecho que no entro en la decision, y es justo al "
        "reves"
    )


def test_el_dia_sin_base_de_bici_llega_al_movil(cfg):
    """El día que la bici no tiene punto de partida, eso TIENE que leerse.

    Lo encontró `scripts/mutar_bici.py`: cambiar en `message.py` la pregunta
    `decision.bike.se_muestra` por la vieja `decision.bike.applies` dejaba toda
    la batería en verde. Y esa mutación no es teórica, es volver a la línea que
    había antes. Su efecto: el día sin histórico suficiente el bloque de bici
    desaparece del mensaje sin hueco y sin error, y el usuario no distingue
    «hoy no toca bici» de «hoy el sistema no sabe».

    Los tests que había comprobaban la RECOMENDACIÓN -que `applies` es False,
    que `skip_reason` está puesto, que `text()` devuelve la frase-. Ninguno
    comprobaba que la frase saliera del móvil, que es para lo único que existe.
    """
    d = decide(cfg, LUNES, sig_completa(LUNES), EngineState())

    assert d.bike is not None
    assert not d.bike.applies, "sin salidas en las señales no puede haber base"
    assert d.bike.se_muestra, "y aun así el bloque ocupa sitio, con su excusa"

    txt = render_telegram(d, cfg)
    assert "🚴" in txt, "el bloque de bici ha desaparecido del mensaje"
    assert "no hay punto de partida" in txt
    assert d.bike.skip_reason and d.bike.skip_reason in txt, (
        "el motivo se calcula y no sale del móvil"
    )


def test_el_recuento_no_se_repite(cfg):
    """Vive en UN solo sitio, y este test es lo que impide que vuelva a dos.

    Estuvo repartido entre las notas de la bici -fines de semana- y una línea
    suelta -el resto-, con un `if` eligiendo cuál de los dos. Eso aguantó hasta
    que la bici empezó a hablar todos los días: entonces la línea suelta dejó de
    salir nunca y el recuento pasó a depender de que `intense_count` ya
    estuviera puesto cuando se construyó la recomendación. Si no lo estaba,
    desaparecía del mensaje sin error y sin hueco.
    """
    from app.engine.signals import IntensityCount

    sabado = LUNES + timedelta(days=5)
    s = _con_bici(sabado)
    s.intense_count = IntensityCount(
        used=5, detail=[], desde=sabado - timedelta(days=6), hasta=sabado
    )
    txt = render_telegram(decide(cfg, sabado, s, EngineState()), cfg)
    assert txt.count("sesiones intensas en los últimos 7 días") == 1


def test_el_recuento_sale_aunque_se_calcule_despues_de_decidir(cfg):
    """El acoplamiento exacto que tenía esto, escrito como test.

    `decide()` construye la recomendación de bici; si el recuento se pintara
    desde las notas de esa recomendación, rellenar `intense_count` después -que
    es lo que hacen varias rutas- lo borraría del mensaje entero. El número se
    lee en el momento de escribir, y por eso sale igual.
    """
    from app.engine.signals import IntensityCount

    sabado = LUNES + timedelta(days=5)
    d = decide(cfg, sabado, _con_bici(sabado), EngineState())
    assert d.bike.applies, "sin recomendación de bici el test no prueba nada"
    d.signals.intense_count = IntensityCount(
        used=4, detail=[], desde=sabado - timedelta(days=6), hasta=sabado
    )
    assert "4 sesiones intensas en los últimos 7 días" in render_telegram(d, cfg)


def test_las_salidas_sin_clasificar_salen_en_la_misma_linea(cfg):
    """`used` es un MÍNIMO, y el mensaje tiene que decir que lo es.

    Callarlo sería enseñar un número redondo que puede estar corto, y un número
    corto en el sitio donde el usuario se hace la idea de cómo va su semana vale
    menos que no poner nada.
    """
    txt = render_telegram(_con_conteo(cfg, used=2, unknown=1), cfg)
    assert "sin clasificar" in txt


# ---------------------------------------------------------------------------
# El HTML que sale de aquí tiene que poder parsearlo Telegram
# ---------------------------------------------------------------------------
#
# `render_telegram` escribe `<b>`, `<i>` y `<code>` y los manda con
# `parse_mode=HTML`. Telegram valida ese HTML DE VERDAD, y lo valida antes
# incluso de mirar si el chat existe: si no lo sabe leer contesta
# `400 can't parse entities` y no manda nada.
#
# El riesgo no es teórico y no es de este fichero: el mismo fallo estaba puesto
# en `scheduler.py`, que metía `str(excepción)` dentro de `<code>` sin escapar.
# Aquí está latente porque hoy el `config.yaml` no tiene ni un `<`, `>` ni `&`.
# Estos tests son para que siga siendo latente el día que los tenga, y para que
# una interpolación nueva sin escapar se caiga aquí y no un lunes a las 06:30.
#
# `_telegram_rechazaria` no es una aproximación: reproduce, medidas contra la
# API real con un `chat_id` inválido, las respuestas exactas de Telegram.


# Los cinco caracteres que importan, en el mínimo de sitio posible: el `<` que
# abre una etiqueta inventada, el `>`, el `&`, la comilla suelta que es lo que
# traen los `str()` de las excepciones, y un `</b>` huérfano. Corto a propósito:
# con todos los bloques del mensaje encendidos a la vez, un veneno largo empuja
# el texto por encima de 4096 y el recorte se lleva por delante justo las
# interpolaciones que hay que cubrir.
VENENO = " <x & 'y' </b>"


def _envenenar_config(cfg):
    """El mismo config con los caracteres peligrosos metidos en cada texto."""
    c = copy.deepcopy(cfg)
    for r in (c.raw.get("routines") or {}).values():
        if not isinstance(r, dict):
            continue
        if r.get("focus"):
            r["focus"] = f"{r['focus']}{VENENO}"
        for ex in r.get("exercises") or []:
            if isinstance(ex, dict) and ex.get("name"):
                ex["name"] = f"{ex['name']}{VENENO}"
    return c


@doble_de(BikeRecommendation)
class _Texto:
    """Un objeto con `.text()` y `.texto_notas()`, que es lo que `message.py` pide.

    Las notas nacieron aquí mismo: este doble no las tenía y el mensaje reventó
    con un AttributeError en cuanto se añadió la línea que las pinta. Eso está
    bien y es lo que tenía que pasar -un doble que se queda corto respecto a la
    interfaz real tiene que romperse, no fingir-, pero además abre un hueco de
    verdad: las notas son texto que acaba en un mensaje HTML, así que también
    hay que envenenarlas para comprobar que pasan por `escapar_html`.

    Y volvió a pasar con `baseline_en_claro`, la frase que explica de dónde sale
    el nivel. Mismo desenlace y misma lección: el doble reventó, se añadió el
    campo, y de paso se envenenó, porque también es texto libre que se interpola
    en HTML. Cada vez que este doble se rompe hay un sitio nuevo que escapar.
    """

    def __init__(
        self, t: str, notas: list[str] | None = None, en_claro: str | None = None
    ):
        self._t = t
        self._notas = list(notas or [])
        self.applies = True
        self.skip_visible = False
        self.baseline_en_claro = en_claro

    @property
    def se_muestra(self) -> bool:
        return self.applies or self.skip_visible

    def text(self) -> str:
        return self._t

    def texto_notas(self) -> list[str]:
        return list(self._notas)


@doble_de(Tendencia)
class _Lineas:
    def __init__(self, *l: str):
        self._l = list(l)

    def lineas(self) -> list[str]:
        return self._l


def _envenenar_decision(d, cfg=None):
    """Y lo mismo con todo lo que pone el motor, que no sale del YAML.

    Enciende TODOS los bloques del mensaje a propósito. Un día real no los tiene
    todos a la vez, y esa es justamente la trampa: si el veneno solo llega a los
    cuatro bloques del día normal, el test da verde y deja sin cubrir los otros
    dieciséis sitios donde se interpola. Se comprobó quitando los `escapar_html`
    de uno en uno: con la versión corta de esta función, dieciséis de veintitrés
    sobrevivían sin que fallara nada.
    """
    from datetime import timedelta

    d.session.title = f"Día 1{VENENO}"
    d.session.hiit_block = f"8x30s{VENENO}"
    d.session.dropped = [f"peso muerto{VENENO}", f"remo{VENENO}"]
    d.session.notes = list(d.session.notes) + [f"nota de sesión{VENENO}"]
    d.notes = list(d.notes) + [f"apunte del motor{VENENO}"]
    d.signals.notes = list(d.signals.notes) + [f"degradación{VENENO}"]
    # Dos ejercicios y no los nueve del día: con todos los bloques encendidos a
    # la vez el mensaje se pasa de 4096 y el recorte se come la mitad de las
    # interpolaciones que este test existe para cubrir.
    d.session.exercises = d.session.exercises[:2]
    for ex in d.session.exercises:
        ex["name"] = f"{ex.get('name') or ex.get('key')}{VENENO}"

    # Descarga, con motivo.
    d.deload.active = True
    d.deload.reason = f"semana 8{VENENO}"

    # Adopciones: aplicada y rechazada, con motivo y con nombre del YAML.
    clave = (d.session.exercises[0].get("key") if d.session.exercises else "x")
    d.load_adoptions = [
        {"routine": d.session.routine_key, "key": clave, "direction": "up",
         "before_kg": 60.0, "after_kg": 65.0, "applied": True,
         "reason": f"se levantó eso{VENENO}"},
        {"routine": d.session.routine_key, "key": clave, "applied": False,
         "before_kg": 60.0, "executed_kg": 600.0,
         "reason": f"salto raro{VENENO}"},
    ]

    # Progresión: cambios, parados y la puerta cerrada con motivo. Se envenenan
    # los objetos DE VERDAD -`changes` es una propiedad derivada de `exercises`
    # y `stopped_lines()` se construye sola- en vez de poner un doble encima:
    # así el texto que llega al mensaje pasa por el mismo `text()` que en
    # producción.
    if d.progression is not None:
        for e in d.progression.exercises:
            e.name = f"{e.name}{VENENO}"
            e.changed = True
            e.what = f"{e.what}{VENENO}"
            e.why = f"{e.why}{VENENO}"
        d.progression.notify_ceiling = True
        d.progression.ceilings = [f"press militar{VENENO}"]
        d.progression.missing_data = [f"remo en T{VENENO}"]
        d.progression.gate_open = False
        d.progression.gate_reason = f"el semáforo está en ámbar{VENENO}"
        # La rama con rótulo, que es la que interpola el motivo DETRÁS de
        # "Progresión cerrada:". La otra -el estreno, sin rótulo- tiene su
        # propio test, porque `decision(c)` sale de un estado vacío y saldría
        # siempre por ahí, dejando esta sin envenenar.
        d.progression.estreno = False

    # Reglas activas, con y sin fecha de caducidad.
    @doble_de(ActiveRule)
    class _Regla:
        def __init__(self, name, hasta=None, detail=None):
            self.name, self.active_until = name, hasta
            self.detail = detail or []

    d.active_rules = [
        _Regla(f"lumbar_alto{VENENO}", LUNES + timedelta(days=4)),
        _Regla(f"otra_regla{VENENO}"),
    ]

    # Bici, tendencia, recalibración, días sin fuerza y el bloque "Por qué".
    d.bike = _Texto(
        f"Si sales hoy: intensa{VENENO}",
        notas=[
            f"ayer hiciste una salida intensa{VENENO}",
            f"llevas 5 sesiones intensas esta semana{VENENO}",
        ],
        en_claro=f"llevas 9 días sin una salida intensa{VENENO}",
    )
    d.bike.applies = True
    d.tendencia = _Lineas(f"Tendencia: seis días sin verde{VENENO}")
    d.recalibracion = _Lineas(f"revisa caida_min_min{VENENO}")
    # La rama larga del recuento de días sin fuerza. Sin esto se pinta la otra
    # -"todavía no hay ninguna sesión leída"-, que no interpola nada, y el
    # bloque se quedaría fuera de este test sin que se notara.
    d.last_strength = (f"dia_2{VENENO}", LUNES - timedelta(days=9))
    d.trigger_rule = f"lumbar_alto{VENENO}"
    d.light_decision.fired = [
        _Regla(d.trigger_rule, detail=[f"lower_discomfort 6 > 5{VENENO}"])
    ]
    return d


def _dia_de_recuperacion(cfg):
    """La otra cabecera: el día rojo no lleva el "Si vas al gimnasio hoy".

    Era `_dia_de_descanso`, con `kind="rest"`. Ese `kind` ya no existe: no hay
    días que el sistema declare de descanso. La segunda rama de la cabecera es
    ahora la del rojo, y sigue habiendo dos sitios donde se interpola el título
    de la sesión, que es lo que este helper existe para cubrir.
    """
    d = decision(cfg)
    d.session.kind = "recovery"
    d.session.title = f"Recuperación{VENENO}"
    return d


def test_el_mensaje_de_un_dia_normal_lo_acepta_telegram(cfg):
    from tests.conftest import _telegram_rechazaria

    txt = render_telegram(decision(cfg, notas=DEGRADACIONES), cfg)
    r = _telegram_rechazaria({"text": txt, "parse_mode": "HTML"})
    assert r is None, r.text if r else ""


def test_el_mensaje_lo_acepta_telegram_aunque_los_textos_lleven_angulos(cfg):
    """El caso que hoy no puede pasar y mañana sí: un nombre con un `<`.

    FALSACIÓN: quitando cualquiera de los `escapar_html` de `message.py`, este
    test falla con el 400 de entidades. Comprobado quitándolos de uno en uno.
    """
    from tests.conftest import _telegram_rechazaria

    c = _envenenar_config(cfg)
    d = _envenenar_decision(decision(c), c)
    txt = render_telegram(d, c)

    assert "&lt;" in txt, "el veneno tiene que haber llegado al mensaje"
    # Todos los bloques encendidos: si alguno deja de pintarse, este test deja
    # de cubrir su interpolación y hay que enterarse aquí.
    for cabecera in ("Ajustado a lo que levantaste", "No adoptado", "Sube hoy",
                     "Sin progresar", "Fuera hoy", "Reglas activas",
                     "Tendencia", "Decidido con datos incompletos",
                     "Última sesión de fuerza", "Toca recalibrar",
                     "Semana de descarga", "HIIT", "Progresión cerrada",
                     "Si vas al gimnasio hoy"):
        assert cabecera in txt, f"el bloque «{cabecera}» no se ha pintado"

    r = _telegram_rechazaria({"text": txt, "parse_mode": "HTML"})
    assert r is None, r.text if r else ""


def test_la_cabecera_de_un_dia_rojo_tambien_va_escapada(cfg):
    """El día rojo tiene su propia rama de cabecera y también interpola."""
    from tests.conftest import _telegram_rechazaria

    txt = render_telegram(_dia_de_recuperacion(cfg), cfg)
    assert "&lt;" in txt
    r = _telegram_rechazaria({"text": txt, "parse_mode": "HTML"})
    assert r is None, r.text if r else ""


def test_el_mensaje_recortado_tampoco_deja_una_etiqueta_a_medias(cfg):
    """El recorte a 4096 corta por líneas, y cada línea cierra lo que abre.

    Si cortara a media etiqueta, Telegram devolvería `Can't find end tag` y se
    perdería el mensaje entero justo el día que hay más que contar.
    """
    from tests.conftest import _telegram_rechazaria

    c = _envenenar_config(cfg)
    d = _envenenar_decision(decision(c, notas=DEGRADACIONES * 40), c)
    txt = render_telegram(d, c)

    assert len(txt) <= LIMIT
    assert "[mensaje recortado]" in txt, "el texto del test ya no se recorta"
    r = _telegram_rechazaria({"text": txt, "parse_mode": "HTML"})
    assert r is None, r.text if r else ""


def test_la_consola_ve_el_texto_original_y_no_el_escapado(cfg):
    """`render_plain` deshace el escapado: un `--dry-run` con `&lt;` mentiría."""
    c = _envenenar_config(cfg)
    d = _envenenar_decision(decision(c), c)
    plano = render_plain(d, c)

    assert "&lt;" not in plano and "&amp;" not in plano
    assert "<b>" not in plano and "<code>" not in plano
    assert "<x & 'y' </b>" in plano, "el original se lee tal cual"


# ---------------------------------------------------------------------------
# La puerta cerrada por estreno no se pinta como una avería
# ---------------------------------------------------------------------------


def test_el_estreno_de_una_rutina_no_lleva_el_rotulo_de_progresion_cerrada(cfg):
    """«Progresión cerrada: primera vez que el sistema ve el Día 1» son dos
    malas noticias donde no hay ninguna.

    La primera semana de un ciclo, ESTE es el mensaje que llega casi todos los
    días: cada rutina se estrena una vez y hasta la cuarta sesión no hay una
    sola puerta que pueda abrirse. Que se lea como un fallo del sistema durante
    dos semanas seguidas es la forma más rápida de que deje de leerse.
    """
    d = decision_completa(cfg)
    assert d.progression is not None and d.progression.estreno, (
        "la premisa: con el estado vacío, hoy la rutina se estrena"
    )

    txt = render_telegram(d, cfg)

    assert "Primera vez que el sistema ve el Día 1" in txt
    assert "Progresión cerrada" not in txt


def test_la_puerta_cerrada_por_cualquier_otra_cosa_sí_lleva_el_rótulo(cfg):
    """El contraste. Sin él, el cambio de arriba podría haberse llevado por
    delante el rótulo entero y este fichero no se enteraría."""
    d = decision_completa(cfg)
    d.progression.estreno = False
    d.progression.gate_reason = "el semáforo está en ámbar, no en verde"

    txt = render_telegram(d, cfg)

    assert "Progresión cerrada: el semáforo está en ámbar, no en verde" in txt


def test_el_motivo_del_estreno_tambien_va_escapado(cfg):
    """Rama nueva, interpolación nueva: el `<` de un título de rutina."""
    from tests.conftest import _telegram_rechazaria

    d = decision_completa(cfg)
    d.progression.estreno = True
    d.progression.gate_reason = "primera vez que el sistema ve el <Día 1>"

    txt = render_telegram(d, cfg)

    assert "&lt;Día 1&gt;" in txt
    assert _telegram_rechazaria({"text": txt, "parse_mode": "HTML"}) is None


# ---------------------------------------------------------------------------
# La anulación: el segundo mensaje del día se presenta
# ---------------------------------------------------------------------------
#
# Cuando el reloj sube la noche DESPUÉS del check-in, el fallback de las 09:00
# recomputa y el móvil recibe un segundo semáforo del mismo día. Sin esta línea
# serían dos mensajes idénticos en el tono y contradictorios en el color, sin
# nada que dijera cuál manda. Lo que se prueba aquí es que el segundo se
# presenta: qué anula, a qué hora se decidió lo anulado y con qué dato nuevo.


# Las 04:23:18 UTC son las 06:23 en Madrid en septiembre. El check-in real que
# destapó todo esto se mandó a las 06:23:18 y se guardó con la hora de UTC: si
# el mensaje dijera "anula la de las 04:23", el usuario no reconocería su
# propio check-in.
DECIDIDA_A = datetime(LUNES.year, LUNES.month, LUNES.day, 4, 23, 18)


def _anulada(cfg, *, ahora="amber", antes="green", medidas=("hrv",),
             sin_llegar=(), cuando=DECIDIDA_A):
    """Una decisión recomputada a las 09:00 sobre otra decidida a ciegas."""
    d = decision_completa(cfg)
    d.light = ahora
    d.anulacion = DecisionAnulada(
        anterior=antes,
        decidida_a=cuando,
        fuente_anterior="checkin",
        medidas=list(medidas),
        sin_llegar=list(sin_llegar),
    )
    return d


def test_los_nombres_de_las_medidas_no_se_separan_de_los_de_la_ficha():
    """`EN_FRASE` es una copia deliberada, y una copia se despega sola.

    `message.py` no puede importar `app/analysis/series.py` -arrastra
    `app.models` y `app.repository`, y el motor no abre la base de datos-, así
    que los nombres en frase de las medidas del reloj están escritos dos veces.
    El comentario que hay sobre `EN_FRASE` promete que este test existe y se
    pone rojo si difieren. Aquí está.

    De paso ata la tercera copia: el `frozenset` del scheduler que decide QUÉ
    medidas ausentes justifican recomputar. Si alguien añade una señal del reloj
    en un sitio y no en los otros dos, el mensaje hablaría de un dato que el
    fallback no vigila, o al revés.
    """
    from app.analysis.series import GARMIN
    from app.engine.message import EN_FRASE
    from app.scheduler import MEDIDAS_DE_GARMIN

    assert set(EN_FRASE) == set(GARMIN) == set(MEDIDAS_DE_GARMIN)
    for clave, texto in EN_FRASE.items():
        assert texto == GARMIN[clave].en_frase, (
            f"«{clave}» se llama «{texto}» en el mensaje y "
            f"«{GARMIN[clave].en_frase}» en la ficha"
        )


def test_si_el_semaforo_cambia_la_anulacion_es_la_noticia_del_dia(cfg):
    """En negrita y pegada a la cabecera, antes que el plan que condiciona.

    Si esta línea cayera entre los apuntes del final, el usuario leería la
    sesión entera creyendo que es la primera del día.
    """
    txt = render_telegram(_anulada(cfg), cfg)
    lineas = txt.split("\n")

    assert lineas[1].startswith("♻️"), (
        "la anulación tiene que ir justo debajo de la cabecera, y va en "
        f"la línea {[i for i, l in enumerate(lineas) if l.startswith('♻️')]}"
    )
    assert "<b>Esto anula la decisión de las 06:23, que salió verde.</b>" in txt
    assert "la variabilidad no se pudo evaluar" in txt
    assert "el semáforo cambia" in txt


def test_si_el_semaforo_no_cambia_la_anulacion_es_un_acuse_en_cursiva(cfg):
    """El segundo mensaje se sigue presentando aunque el color sea el mismo.

    Dos mensajes iguales sin explicación son peores que uno explicado: el
    usuario no tiene forma de saber si el sistema se ha repetido o ha decidido
    dos veces.
    """
    txt = render_telegram(_anulada(cfg, ahora="green", antes="green"), cfg)

    assert "♻️ <i>Recalculado con la variabilidad" in txt
    assert "El semáforo no cambia." in txt
    assert "Esto anula" not in txt, "sin cambio de color no hay titular"


def test_la_hora_que_se_lee_es_la_del_reloj_de_pared_y_no_la_de_utc(cfg):
    """`computed_at` se guarda en UTC; el usuario recuerda las 06:23.

    Este es el fallo de las dos horas que ya se coló una vez leyendo el log de
    las 04:30 como un evento distinto del de las 06:30. Aquí no sería un error
    de lectura mío: sería el mensaje diciéndole al usuario que anula un
    check-in que él mandó dos horas después.
    """
    txt = render_telegram(_anulada(cfg), cfg)
    assert "de las 06:23" in txt
    assert "04:23" not in txt


def test_sin_zona_horaria_la_frase_se_queda_sin_hora_en_vez_de_mentir(cfg):
    """Peor mensaje, pero no un mensaje FALSO."""
    c = copy.deepcopy(cfg)
    c.raw.pop("timezone", None)

    txt = render_telegram(_anulada(c), c)

    assert "Esto anula la decisión, que salió verde" in txt
    assert "de las" not in txt


def test_la_hora_local_no_inventa_nada_si_la_zona_no_existe():
    """Una zona inventada en el `config.yaml` no puede tumbar el mensaje."""
    from app.engine.message import _hora_local

    assert _hora_local(DECIDIDA_A, "Europe/Madrid") == "06:23"
    assert _hora_local(DECIDIDA_A, "Marte/Olympus") == ""
    assert _hora_local(DECIDIDA_A, None) == ""
    assert _hora_local(None, "Europe/Madrid") == ""


def test_la_frase_concuerda_con_cuantas_medidas_llegaron(cfg):
    """"la variabilidad y la nota de sueño no se pudo evaluar" no se manda.

    Es una falta pequeña en la única línea del mensaje que avisa de que el
    semáforo de hace dos horas ya no vale.
    """
    una = render_telegram(_anulada(cfg, medidas=("hrv",)), cfg)
    assert "la variabilidad no se pudo evaluar" in una
    assert "Con el dato ya puesto, el semáforo cambia" in una

    varias = render_telegram(
        _anulada(cfg, medidas=("hrv", "sleep_score", "body_battery")), cfg
    )
    assert (
        "la variabilidad, la nota de sueño y el Body Battery no se pudieron "
        "evaluar" in varias
    )
    assert "Con los datos ya puestos, el semáforo cambia" in varias

    acuse = render_telegram(
        _anulada(cfg, ahora="green", antes="green", medidas=("hrv", "rhr")), cfg
    )
    assert "la variabilidad y el pulso en reposo, que a la hora del check-in " \
           "todavía no estaban" in acuse


def test_lo_que_sigue_sin_llegar_se_dice_en_la_misma_linea(cfg):
    """Recomputar no es lo mismo que tener el día completo.

    Si a las 09:00 llega la HRV pero el Body Battery sigue sin subir, la
    decisión nueva también está coja, y callarlo la haría parecer definitiva.
    """
    una = render_telegram(_anulada(cfg, sin_llegar=("body_battery",)), cfg)
    assert "Sigue sin llegar el Body Battery, así que esta decisión tampoco " \
           "está completa." in una

    varias = render_telegram(
        _anulada(cfg, sin_llegar=("body_battery", "sleep_min")), cfg
    )
    assert "Siguen sin llegar el Body Battery y lo que duermes" in varias


def test_un_dia_normal_no_lleva_ninguna_linea_de_anulacion(cfg):
    """La inmensa mayoría de las mañanas no hay nada que anular."""
    txt = render_telegram(decision_completa(cfg), cfg)
    assert "♻️" not in txt
    assert "anula" not in txt


def test_la_consola_tambien_cuenta_la_anulacion(cfg):
    """`render_plain` delega en `render_telegram`: hereda la línea sin etiquetas."""
    plano = render_plain(_anulada(cfg), cfg)

    assert "Esto anula la decisión de las 06:23, que salió verde." in plano
    assert "<b>" not in plano and "<i>" not in plano


def test_la_anulacion_tambien_va_escapada(cfg):
    """`anterior` sale de la base, y de la base puede salir cualquier cosa.

    `_NOMBRE_LUZ.get(luz, luz)` devuelve la clave tal cual si no la conoce -a
    propósito, para que se vea QUÉ color llegó-, así que un valor raro guardado
    en `decisions.light` acaba interpolado en el mensaje.
    """
    from tests.conftest import _telegram_rechazaria

    d = _anulada(cfg, antes="<x & 'y'>")
    txt = render_telegram(d, cfg)

    assert "&lt;x &amp; 'y'&gt;" in txt
    assert _telegram_rechazaria({"text": txt, "parse_mode": "HTML"}) is None


# ---------------------------------------------------------------------------
# Lo que lleva más de una vuelta sin hacerse
# ---------------------------------------------------------------------------
#
# LA LÍNEA NO ES UNA COLA DE TAREAS, Y ESO ES LO QUE SE PROTEGE AQUÍ.
#
# El encargo fue explícito: el ciclo no se reordena, nadie adelanta nada y el
# tono no regaña. Lo único que se pidió es SABER que el Día 1 lleva sin hacerse
# más de una vuelta. Eso convierte a este bloque en un caso raro dentro del
# mensaje: casi todas las demás líneas se comprueban mirando si el dato sale;
# ésta hay que comprobarla también por lo que NO hace.
#
# Las tres cosas que puede romper alguien de buena fe, y que por tanto tienen
# test: enumerar las dos paradas en vez de la que más lleva -y convertir el
# apunte en una lista de deberes-, seguir nombrando la caducada para siempre -y
# convertirlo en decorado-, y dejar de guardarla al caducar -y perder el dato
# justo cuando pasa a ser interesante-.


def _con_pendientes(cfg, historial):
    """Una mañana normal a la que se le cuelga la rotación de `historial`.

    `historial` es lo que hay en `workout_log`, MÁS RECIENTE PRIMERO, igual que
    lo que devuelve `repository.sesiones_del_ciclo`. Se pasa por la función de
    verdad -`rotacion.pendientes`- en vez de fabricar objetos `Pendiente` a
    mano: escritos a mano, el umbral y el orden serían los que yo creo que son
    y no los que el módulo aplica, y este fichero dejaría de notar que cambien.
    """
    from app.engine.rotacion import pendientes
    from app.engine.session_builder import orden_de_rotacion

    d = decision_completa(cfg)
    d.pendientes = pendientes(orden_de_rotacion(cfg), historial)
    return d


def _historial(claves, *, desde=None):
    """De más reciente a más antigua, una sesión cada dos días."""
    dia = desde or LUNES
    return [(k, dia - timedelta(days=2 * i)) for i, k in enumerate(claves)]


def test_una_rutina_parada_mas_de_una_vuelta_se_nombra_con_su_titulo(cfg):
    """«Día 1», las sesiones que han pasado y desde cuándo.

    Las tres cosas en la misma línea porque las tres hacen falta para saber si
    importa: el nombre dice cuál, la cuenta dice cuánto -en sesiones, que es la
    unidad del ciclo- y la fecha ancla eso a un día real. Con la cuenta sola,
    «cinco sesiones» puede ser hace una semana o hace dos meses.
    """
    d = _con_pendientes(
        cfg, _historial(["dia_2", "dia_3", "dia_2", "dia_3", "dia_2", "dia_1"])
    )
    assert [p.clave for p in d.pendientes] == ["dia_1"], d.pendientes

    txt = render_telegram(d, cfg)
    assert "Día 1: han pasado 5 sesiones de fuerza desde la última vez" in txt, txt
    # La fecha es la de la última vez que se hizo, no la de hoy.
    ultima = LUNES - timedelta(days=10)
    assert f"({ultima.strftime('%d/%m')})" in txt, txt
    assert "dia_1" not in txt, f"ha salido el identificador crudo:\n{txt}"


def test_solo_se_nombra_la_que_mas_lleva_parada(cfg):
    """Con dos paradas, una línea. Enumerarlas es hacer una lista de deberes.

    Y no es una preferencia de estilo: con un ciclo de tres, que haya DOS
    paradas ya lo está diciendo la línea de arriba -«última sesión de fuerza:
    hace tantos días»-, así que la segunda línea no añade un hecho, añade
    insistencia.
    """
    d = _con_pendientes(cfg, _historial(["dia_3"] * 5 + ["dia_2", "dia_1"]))
    assert [p.clave for p in d.pendientes] == ["dia_1", "dia_2"], d.pendientes
    assert not any(p.caducada for p in d.pendientes), "el montaje no vale: ver abajo"

    txt = render_telegram(d, cfg)
    assert txt.count("🗓") == 1, f"se han enumerado varias:\n{txt}"
    # La más parada es el Día 1: está una sesión más atrás que el Día 2.
    assert "Día 1: han pasado 6" in txt, txt
    assert "Día 2: han pasado" not in txt, txt


def test_si_la_mas_parada_ya_caduco_se_nombra_la_siguiente(cfg):
    """«La primera» y «la primera de las vivas» no son la misma línea.

    Con el Día 3 repetido siete veces, el Día 1 lleva siete sesiones parado -ya
    caducado, ya no se nombra- y el Día 2 lleva seis, que todavía está dentro de
    la ventana. Escrito `p = decision.pendientes[0]` y filtrando después, el
    mensaje se quedaría mudo: cogería la caducada, vería que lo está y no diría
    nada, callando una rutina que sí toca nombrar.

    Es el fallo que sale solo cuando dos rutinas se paran a la vez, o sea el día
    que el aviso más falta hace, y no se ve en ninguno de los otros tests
    porque todos tienen una pendiente sola.
    """
    d = _con_pendientes(cfg, _historial(["dia_3"] * 6 + ["dia_2", "dia_1"]))
    assert [(p.clave, p.caducada) for p in d.pendientes] == [
        ("dia_1", True),
        ("dia_2", False),
    ], d.pendientes

    txt = render_telegram(d, cfg)
    assert "Día 2: han pasado 6" in txt, txt
    assert "Día 1: han pasado" not in txt, txt


def test_la_caducada_deja_de_nombrarse_pero_no_deja_de_guardarse(cfg):
    """Las dos mitades de `CADUCA_TRAS`, que son fáciles de confundir en una.

    Lo que caduca es decirlo en voz alta. Una línea idéntica cada mañana durante
    meses se deja de leer a la tercera, y a partir de ahí no informa: insiste.
    Pero el dato al caducar es MÁS interesante que antes, no menos -una rutina
    que lleva nueve sesiones parada es una rutina abandonada, y eso es justo lo
    que se querrá ver en el histórico-, así que sigue en la decisión guardada.

    Filtrar la lista al colgarla en la decisión habría sido lo natural de
    escribir y habría cumplido la primera mitad rompiendo la segunda sin que se
    notara: el mensaje se vería bien y el JSON tendría un hueco que nadie mira
    hasta dentro de tres meses.
    """
    d = _con_pendientes(cfg, _historial(["dia_2"] * 9 + ["dia_1"]))
    assert [p.clave for p in d.pendientes] == ["dia_1"]
    assert d.pendientes[0].caducada is True, d.pendientes

    txt = render_telegram(d, cfg)
    assert "🗓" not in txt, f"la caducada sigue saliendo:\n{txt}"
    assert "han pasado" not in txt, txt

    guardado = d.to_dict()["pendientes"]
    assert guardado == [
        {
            "clave": "dia_1",
            "sesiones_desde": 9,
            "ultima_vez": (LUNES - timedelta(days=18)).isoformat(),
            "caducada": True,
        }
    ], guardado


def test_una_rotacion_normal_no_saca_ninguna_linea(cfg):
    """El caso de todas las mañanas: 1, 2, 3, 1, 2, 3 y nada que decir.

    Es el test que impide que esto se convierta en una cabecera fija. El umbral
    vive en `rotacion.py` y allí tiene el suyo; éste comprueba lo otro, que es
    que el mensaje respeta la lista vacía en vez de pintar el bloque igual.
    """
    d = _con_pendientes(cfg, _historial(["dia_3", "dia_2", "dia_1"] * 3))
    assert d.pendientes == []

    assert "🗓" not in render_telegram(d, cfg)
    # Y un día sin el campo siquiera -toda decisión anterior a esto- tampoco.
    assert "🗓" not in render_telegram(decision_completa(cfg), cfg)


def test_el_desvio_puntual_de_ayer_no_sale_en_el_mensaje_de_hoy(cfg):
    """El caso que motivó el encargo, visto desde el único sitio que se lee.

    Tocaba el Día 1, llegué con las piernas cansadas, hice el Día 2. A la mañana
    siguiente el Día 1 está a una vuelta EXACTA, y decirle «llevas sin hacer el
    Día 1» a alguien que se lo saltó ayer a propósito es contarle lo que acaba
    de decidir.

    El umbral que sostiene esto es el `>` de `rotacion.pendientes`, y allí está
    su test. Éste existe porque ese `>` no protege nada por sí solo: protege
    esta frase, y quien lo cambie a `>=` verá un test rojo que habla de reglas y
    otro que habla de la mañana siguiente a un desvío.
    """
    d = _con_pendientes(cfg, _historial(["dia_2", "dia_3", "dia_2", "dia_1"]))
    assert d.pendientes == [], d.pendientes
    assert "🗓" not in render_telegram(d, cfg)


def test_la_linea_de_pendiente_tambien_va_escapada(cfg):
    """El título sale de `config.yaml`, y de ahí puede salir cualquier cosa."""
    from tests.conftest import _telegram_rechazaria

    c = copy.deepcopy(cfg)
    c.raw["routines"]["dia_1"]["title"] = "Día <1> & 'pierna'"
    d = _con_pendientes(
        c, _historial(["dia_2", "dia_3", "dia_2", "dia_3", "dia_2", "dia_1"])
    )
    txt = render_telegram(d, c)

    assert "Día &lt;1&gt; &amp; 'pierna'" in txt, txt
    assert _telegram_rechazaria({"text": txt, "parse_mode": "HTML"}) is None
