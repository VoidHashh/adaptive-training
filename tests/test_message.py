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
from app.engine.message import EMOJI, NOMBRE_LUZ, render_plain, render_telegram

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


def test_un_dia_de_descanso_no_lleva_foco(cfg):
    """Martes es descanso: sin `routine_key` no hay nada que buscar."""
    from datetime import timedelta

    from app.engine.decision import EngineState, decide

    martes = LUNES + timedelta(days=1)
    txt = render_plain(decide(cfg, martes, sig_completa(martes), EngineState()), cfg)
    assert "💪" not in txt
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
