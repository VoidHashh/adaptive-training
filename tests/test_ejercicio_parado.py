"""Un ejercicio que ha dejado de progresar tiene que decirlo el mensaje.

Este fichero existe por una función que nadie llamaba.
`ProgressionPlan.text_lines()` generaba desde el primer día las dos líneas que
avisan de un ejercicio parado -"techo alcanzado, toca cambiar el ejercicio" y
"Sin progresión por falta de carga registrada en Hevy: ..."- y
`render_telegram` no la invocaba en ningún sitio: pintaba `progression.changes`
y nada más. Las líneas se escribían, se probaban a mano y se tiraban.

Lo que costó, medido: en la simulación de doce semanas
`press_triceps_sentado` y `remo_t_apoyado` no subieron ni una sola vez, porque
esperaban una carga que nunca se apuntó en Hevy. Ochenta y cuatro mensajes de
buenos días y ninguno lo mencionó. El sistema lo sabía cada mañana y no lo
dijo, que es la forma más cara de saberlo: un ejercicio muerto que además es
invisible parece un ejercicio que va bien.

Y lo que lo tapaba: `scripts/smoke_progression.py` sí llamaba a `text_lines()`
e imprimía el resultado bajo el rótulo "Mensaje de Telegram completo". El único
sitio donde esas líneas se veían era un script que afirmaba estar enseñando lo
que se manda. Es el mismo patrón que la tabla de `docs/analisis.md` que
certificaba como resuelta una columna vacía: el documento que tenía que avisar
del hueco es el que jura que no lo hay.

Las normas que fija este fichero:

1. Lo que el plan da por parado sale en el mensaje de la mañana.
2. Sale AUNQUE se apague el razonamiento. Que un ejercicio lleve semanas sin
   moverse no es "por qué he decidido esto", es un hecho del programa.
3. `on_ceiling_notify: false` silencia el aviso de techo y NO borra
   `plan.ceilings`: "no me avises" no es "no lo apuntes".
4. Un techo y una carga sin registrar no dicen lo mismo, porque no se arreglan
   igual.
"""

from __future__ import annotations

import copy

import pytest

from app.engine.decision import EngineState, decide
from app.engine.message import render_plain

from tests.conftest import LUNES, sig_completa


# Los dos motivos de parada, uno de cada clase, en la rutina que toca el lunes.
#
#   - la plancha está en su techo de segundos: no hay más recorrido en el modo
#     `volume` y solo se destraba cambiando el ejercicio;
#   - el hip thrust tiene series registradas con 0 kg: el peso está en la barra
#     pero no en Hevy, y el motor no se inventa una carga de partida.
#
# Hip thrust y no un nombre cualquiera: es uno de los ejercicios reales del
# usuario que estaba en esta situación, y con una hernia L4-L5 es justo el que
# no interesa tener parado.
PARADOS = [
    {"key": "plancha", "name": "Plancha frontal", "progression_type": "volume",
     "max_seconds": 30, "sets": [{"duration_s": 30}, {"duration_s": 30}]},
    {"key": "hip", "name": "Hip thrust barra", "progression_type": "load",
     "sets": [{"reps": 10, "weight_kg": 0}, {"reps": 10, "weight_kg": 0}]},
]

# Uno que sí puede subir, para los tests que necesitan un día sin nada parado.
QUE_SUBE = [
    {"key": "prensa", "name": "Prensa", "progression_type": "load",
     "sets": [{"reps": 10, "weight_kg": 60}, {"reps": 10, "weight_kg": 60}]},
]


def preparar(cfg, ejercicios=PARADOS, *, notificar_techo=None, razonamiento=True):
    """El config real con la rutina del lunes sustituida por `ejercicios`."""
    c = copy.deepcopy(cfg)
    c.raw["routines"]["dia_1"]["exercises"] = copy.deepcopy(ejercicios)
    if notificar_techo is not None:
        c.raw["progression"]["modes"]["volume"]["on_ceiling_notify"] = notificar_techo
    if not razonamiento:
        tel = c.raw.setdefault("notifications", {}).setdefault("telegram", {})
        tel["include_reasoning"] = False
    return c


def decidir(c, dia=LUNES):
    """Un lunes verde con la puerta abierta y todo el cumplimiento en regla.

    Hace falta que la puerta esté abierta: un ejercicio bloqueado por el
    semáforo no está parado, está esperando, y eso ya se cuenta en otro sitio.
    """
    claves = [e["key"] for e in c.raw["routines"]["dia_1"]["exercises"]]
    st = EngineState(
        compliance={("dia_1", k): True for k in claves},
        clean_sessions={("dia_1", k): 5 for k in claves},
    )
    return decide(c, dia, sig_completa(dia), st)


def mensaje(c, dia=LUNES):
    return render_plain(decidir(c, dia), c)


# ---------------------------------------------------------------------------
# 1. Lo que está parado llega al mensaje de la mañana
# ---------------------------------------------------------------------------


def test_el_techo_llega_al_mensaje(cfg):
    """El caso que motivó el fichero: el plan lo sabía y Telegram no lo decía."""
    c = preparar(cfg)
    plan = decidir(c).progression
    assert plan.ceilings == ["Plancha frontal"], "el plan tiene que detectarlo"
    assert "techo alcanzado" in mensaje(c), "y el mensaje tiene que contarlo"


def test_el_mensaje_nombra_al_ejercicio_del_techo(cfg):
    """"Un ejercicio ha llegado a su techo" no sirve de nada en una rutina de
    ocho. Hay que poder abrir Hevy y saber cuál cambiar."""
    assert "Plancha frontal: techo alcanzado" in mensaje(preparar(cfg))


def test_la_carga_sin_registrar_llega_al_mensaje_con_el_nombre(cfg):
    c = preparar(cfg)
    plan = decidir(c).progression
    assert plan.missing_data == ["Hip thrust barra"]
    txt = mensaje(c)
    assert "falta de carga registrada en Hevy" in txt
    assert "Hip thrust barra" in txt


def test_el_aviso_dice_como_se_arregla(cfg):
    """Un aviso que no dice qué hacer con él es ruido. Este manda a Hevy."""
    assert "Hevy" in mensaje(preparar(cfg))


def test_un_techo_y_una_carga_sin_registrar_no_dicen_lo_mismo(cfg):
    """La acción es distinta: uno se resuelve cambiando el ejercicio y el otro
    apuntando el peso. Decirle a alguien que cambie un ejercicio que solo
    necesita que anote la carga es mandarle a tirar un ejercicio que va bien."""
    lineas = decidir(preparar(cfg)).progression.stopped_lines()
    del_techo = [l for l in lineas if "Plancha" in l]
    de_la_carga = [l for l in lineas if "Hip thrust" in l]
    assert len(del_techo) == 1 and len(de_la_carga) == 1
    assert "toca cambiar el ejercicio" in del_techo[0]
    assert "toca cambiar el ejercicio" not in de_la_carga[0]


# ---------------------------------------------------------------------------
# 2. Apagar el razonamiento no lo esconde
# ---------------------------------------------------------------------------


def test_el_bloque_sobrevive_a_include_reasoning_false(cfg):
    """Mismo criterio que "decidido con datos incompletos" y que la sesión
    perdida: `include_reasoning: false` significa "no me cuentes por qué", no
    "ocúltame que llevo tres semanas sin subir el hip thrust"."""
    txt = mensaje(preparar(cfg, razonamiento=False))
    assert "Por qué" not in txt, "el razonamiento sí tiene que estar apagado"
    assert "techo alcanzado" in txt
    assert "falta de carga registrada en Hevy" in txt


# ---------------------------------------------------------------------------
# 3. `on_ceiling_notify`, la opción que gobierna solo el aviso
# ---------------------------------------------------------------------------


def test_por_defecto_se_avisa(cfg):
    """Sin la clave en el YAML el aviso se manda. El silencio se pide a mano:
    un aviso que hay que activar es un aviso que nadie tiene activado."""
    c = preparar(cfg)
    del c.raw["progression"]["modes"]["volume"]["on_ceiling_notify"]
    plan = decidir(c).progression
    assert plan.notify_ceiling is True
    assert "techo alcanzado" in mensaje(c)


def test_on_ceiling_notify_false_silencia_el_techo(cfg):
    c = preparar(cfg, notificar_techo=False)
    assert decidir(c).progression.notify_ceiling is False
    assert "techo alcanzado" not in mensaje(c)


def test_on_ceiling_notify_false_no_silencia_la_carga_sin_registrar(cfg):
    """La opción habla de techos. Un ejercicio sin carga apuntada no está en su
    techo: está esperando un dato que solo puede dar el usuario, y callarlo lo
    deja parado para siempre sin que nada lo diga."""
    assert "falta de carga registrada en Hevy" in mensaje(
        preparar(cfg, notificar_techo=False)
    )


def test_on_ceiling_notify_false_no_borra_el_registro(cfg):
    """"No me avises" no es "no lo apuntes".

    `plan.ceilings` se rellena igual y viaja a `decisions.progression_json`. Si
    el interruptor de notificación borrase también el registro, dentro de un mes
    no habría forma de contestar desde qué día está parada la plancha.
    """
    plan = decidir(preparar(cfg, notificar_techo=False)).progression
    assert plan.ceilings == ["Plancha frontal"]
    d = plan.to_dict()
    assert d["ceilings"] == ["Plancha frontal"]
    # Y el registro dice por qué no se avisó, que si no el hueco entre lo
    # apuntado y lo mandado parece un fallo.
    assert d["notify_ceiling"] is False


# ---------------------------------------------------------------------------
# 4. Sin nada parado, ningún bloque
# ---------------------------------------------------------------------------


def test_sin_ejercicios_parados_no_hay_bloque(cfg):
    """Una cabecera vacía todas las mañanas se deja de leer, y el día que lleve
    algo debajo tampoco se leerá."""
    txt = mensaje(preparar(cfg, QUE_SUBE))
    assert "Sin progresar" not in txt
    assert "Sube hoy" in txt, "el día de control tiene que subir algo de verdad"


def test_un_dia_sin_fuerza_no_revienta(cfg):
    """Martes: descanso, y por tanto `decision.progression is None`."""
    from datetime import timedelta

    c = preparar(cfg)
    dec = decidir(c, LUNES + timedelta(days=1))
    assert dec.progression is None
    assert "Sin progresar" not in render_plain(dec, c)


# ---------------------------------------------------------------------------
# 5. El script de diagnóstico y el mensaje salen de la misma función
# ---------------------------------------------------------------------------


def test_text_lines_incluye_lo_que_se_manda(cfg):
    """`scripts/smoke_progression.py` usa `text_lines()` y lo enseña como el
    mensaje de Telegram. Mientras las dos mitades salgan de `stopped_lines()`,
    el script no puede volver a enseñar líneas que no se mandan."""
    plan = decidir(preparar(cfg)).progression
    completo = plan.text_lines()
    for linea in plan.stopped_lines():
        assert linea in completo
    assert completo[: len(plan.changes)] == [e.text() for e in plan.changes]


@pytest.mark.parametrize("notificar", [True, False])
def test_text_lines_respeta_el_interruptor(cfg, notificar):
    """Si el script enseñara el techo con el aviso apagado volvería a enseñar
    algo que no se manda, que es exactamente el fallo que cerró este fichero."""
    plan = decidir(preparar(cfg, notificar_techo=notificar)).progression
    hay_techo = any("techo alcanzado" in l for l in plan.text_lines())
    assert hay_techo is notificar
