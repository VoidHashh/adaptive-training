"""El recordatorio de que toca volver a mirar los umbrales.

Este fichero vigila una pieza rara: la única del sistema que no decide nada. No
cambia el semáforo, no toca la carga y no entra en ninguna regla. Lo único que
hace es hablar, y por eso la forma de que falle es distinta a la del resto del
motor: no revienta, se calla.

Callarse tiene aquí tres caminos, y hay un bloque de tests para cada uno:

1. **Que la cuenta no llegue nunca.** Contar mal, contar filas en vez de días,
   o reiniciarse por el motivo equivocado. De esto último va el test sobre el
   `config_hash`, que es la versión que NO se implementó y que sigue siendo la
   tentadora: se reiniciaba sola al recalibrar, y también al apagar Telegram.

2. **Que la clave que lo dispara desaparezca del YAML.** Un recordatorio que se
   puede borrar por descuido y que, borrado, no se distingue de uno que aún no
   ha llegado el momento de dar. Por eso el cargador se niega a arrancar.

3. **Que el aviso llegue pero no diga nada accionable.** Nombra dos rutas del
   `config.yaml` -las de los umbrales calibrados sobre muestras cortas- y una
   tercera para callarlo. Las tres se comprueban CONTRA EL config.yaml REAL: un
   renombrado dejaría el mensaje señalando a un sitio que no existe, y eso no lo
   nota nadie porque el texto sigue leyéndose igual de bien.
"""

from __future__ import annotations

import copy
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config_loader import _validate
from app.engine.decision import EngineState, decide
from app.engine.message import render_telegram
from app.engine.recalibracion import Recalibracion, evaluar_recalibracion
from app.models import Base
from app.repository import dias_con_decision, save_decision
from app.runner import run_daily
from tests.conftest import LUNES, dias, sig_completa


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def errores(data) -> str:
    return " | ".join(_validate(data))


def cfg_con(cfg, **program):
    """El config real con la sección `program` retocada."""
    c = copy.deepcopy(cfg)
    c.raw["program"].update(program)
    return c


# ---------------------------------------------------------------------------
# 1. La cuenta
# ---------------------------------------------------------------------------


def test_no_avisa_hasta_que_la_muestra_esta_completa(cfg):
    """El día de antes no dice nada; el día que toca, habla."""
    cada = cfg.recalibrar_cada_dias
    assert evaluar_recalibracion(cfg, cada - 1).lineas() == []
    assert evaluar_recalibracion(cfg, cada).lineas() != []
    # Y sigue hablando después: no es un aviso de un solo día, que sería el que
    # se pierde justo el día que Telegram falla.
    assert evaluar_recalibracion(cfg, cada + 40).lineas() != []


def test_la_cuenta_atras_no_se_enseña_mientras_no_toca(cfg):
    """`restantes` existe para el JSON, no para el mensaje.

    Una línea que sale todas las mañanas del año deja de leerse mucho antes de
    tener algo que decir, así que el mensaje se calla hasta que toca aunque el
    objeto sepa perfectamente cuántos días faltan.
    """
    r = evaluar_recalibracion(cfg, cfg.recalibrar_cada_dias - 9)
    assert r.restantes == 9
    assert not r.toca
    assert r.lineas() == []


def test_cuando_toca_no_quedan_dias_negativos(cfg):
    """`restantes` no baja de cero: '-14 días' no significa nada para nadie."""
    assert evaluar_recalibracion(cfg, cfg.recalibrar_cada_dias + 14).restantes == 0


def test_una_cuenta_negativa_es_error_duro(cfg):
    """Quien cuente mal se entera.

    Un número negativo que pasara callando dejaría el aviso apagado para
    siempre, y apagado no se distingue de 'todavía no toca'.
    """
    with pytest.raises(ValueError, match="negativos"):
        evaluar_recalibracion(cfg, -1)


def test_se_cuentan_dias_y_no_filas(db, cfg):
    """Dos decisiones del mismo día son un día de muestra, no dos.

    Pasa todas las mañanas en las que llega el check-in tarde: una decisión a
    las 07:00 sin él y otra a las 09:40 con él. Contando filas, la muestra
    acumulada crecería al doble de velocidad que los datos.
    """
    dia = date(2026, 10, 1)
    d1 = decide(cfg, dia, sig_completa(dia), EngineState(), source="fallback_0900")
    save_decision(db, d1)
    d2 = decide(cfg, dia, sig_completa(dia), EngineState(), source="checkin")
    save_decision(db, d2)

    assert dias_con_decision(db, desde=dia, hasta=dia) == 1


def test_solo_se_cuentan_los_dias_desde_la_ultima_revision(db, cfg):
    """Mover `recalibrado_el` hacia delante es lo que pone la cuenta a cero."""
    primero = date(2026, 10, 1)
    for i in range(10):
        dia = primero + timedelta(days=i)
        save_decision(db, decide(cfg, dia, sig_completa(dia), EngineState()))

    ultimo = primero + timedelta(days=9)
    assert dias_con_decision(db, desde=primero, hasta=ultimo) == 10
    # La revisión de ayer deja la cuenta en los días que van desde ayer.
    assert dias_con_decision(db, desde=ultimo - timedelta(days=1), hasta=ultimo) == 2


def test_los_dias_sin_decision_no_se_inventan(db, cfg):
    """Ocho días de calendario con tres decisiones son tres días de muestra.

    Es la diferencia entera entre este contador y uno de calendario, y es lo que
    impide que cuatro semanas con el sistema apagado la mitad del tiempo se
    presenten como cuatro semanas de datos.
    """
    primero = date(2026, 10, 1)
    for i in (0, 3, 7):
        dia = primero + timedelta(days=i)
        save_decision(db, decide(cfg, dia, sig_completa(dia), EngineState()))

    assert dias_con_decision(db, desde=primero, hasta=primero + timedelta(days=7)) == 3


def test_cambiar_otra_cosa_del_config_no_reinicia_la_cuenta(db, cfg):
    """La razón por la que NO se cuenta por `config_hash`.

    Contar decisiones tomadas bajo el hash de hoy tenía la gracia de reiniciarse
    solo al recalibrar. Pero el hash cambia con cualquier edición -apagar
    Telegram, tocar un texto de una rutina-, así que el contador se habría
    puesto a cero sin que nadie hubiera revisado un umbral, y el aviso habría
    desaparecido sin haberse atendido.

    Aquí se guardan cinco días con un hash y cinco con otro, y tienen que contar
    los diez.
    """
    primero = date(2026, 10, 1)
    for i in range(10):
        dia = primero + timedelta(days=i)
        d = decide(cfg, dia, sig_completa(dia), EngineState())
        d.config_hash = "antes" if i < 5 else "despues"
        save_decision(db, d)

    assert dias_con_decision(db, desde=primero, hasta=primero + timedelta(days=9)) == 10


def test_un_rango_invertido_es_error_duro(db):
    """Un cero tranquilo aquí dejaría el aviso contando desde cero para siempre."""
    with pytest.raises(ValueError, match="rango invertido"):
        dias_con_decision(db, desde=date(2026, 10, 2), hasta=date(2026, 10, 1))


# ---------------------------------------------------------------------------
# 2. El YAML: las dos claves no se pueden perder
# ---------------------------------------------------------------------------


def test_el_config_real_trae_las_dos_claves(cfg):
    assert cfg.recalibrar_cada_dias >= 1
    assert isinstance(cfg.recalibrado_el, date)
    # La primera cuenta arranca en el origen del programa: el día cero no se
    # había revisado nada todavía.
    assert cfg.recalibrado_el >= cfg.program_start


@pytest.mark.parametrize(
    "clave, fragmento",
    [
        ("recalibrar_cada_dias", "program.recalibrar_cada_dias está vacío"),
        ("recalibrado_el", "program.recalibrado_el está vacío"),
    ],
)
def test_borrar_una_de_las_dos_claves_impide_arrancar(cfg, clave, fragmento):
    """Sin ellas el aviso no se da NUNCA, y no darse no se ve."""
    data = copy.deepcopy(cfg.raw)
    del data["program"][clave]
    assert fragmento in errores(data)


def test_una_errata_en_el_nombre_de_la_clave_impide_arrancar(cfg):
    """`recalibrar_cada` en vez de `recalibrar_cada_dias`.

    Sin lista blanca esto daba un arranque limpio, la clave buena ausente y el
    recordatorio desactivado para siempre. `program` era la última sección de
    primer nivel que no tenía lista blanca.
    """
    data = copy.deepcopy(cfg.raw)
    data["program"]["recalibrar_cada"] = data["program"].pop("recalibrar_cada_dias")
    problemas = errores(data)
    assert "clave desconocida 'recalibrar_cada'" in problemas
    assert "program.recalibrar_cada_dias está vacío" in problemas


@pytest.mark.parametrize(
    "valor, fragmento",
    [
        pytest.param(0, "mayor que cero", id="cero"),
        pytest.param(-7, "mayor que cero", id="negativo"),
        pytest.param(28.5, "entero", id="con_decimales"),
        pytest.param("28", "entero", id="texto"),
        pytest.param(True, "entero", id="booleano"),
    ],
)
def test_un_cada_dias_que_no_es_un_entero_de_dias_impide_arrancar(cfg, valor, fragmento):
    data = copy.deepcopy(cfg.raw)
    data["program"]["recalibrar_cada_dias"] = valor
    assert fragmento in errores(data)


@pytest.mark.parametrize(
    "valor, fragmento",
    [
        pytest.param("2026-09-08", "entre comillas", id="entrecomillada"),
        pytest.param("el martes", "no es una fecha", id="texto_libre"),
        pytest.param(
            __import__("datetime").datetime(2026, 9, 8, 7, 30),
            "lleva hora",
            id="con_hora",
        ),
    ],
)
def test_un_recalibrado_el_mal_escrito_impide_arrancar(cfg, valor, fragmento):
    data = copy.deepcopy(cfg.raw)
    data["program"]["recalibrado_el"] = valor
    assert fragmento in errores(data)


def test_recalibrado_el_antes_del_arranque_impide_arrancar(cfg):
    """La cuenta arrancaría en días que el programa no había vivido."""
    data = copy.deepcopy(cfg.raw)
    data["program"]["recalibrado_el"] = data["program"]["start"] - timedelta(days=1)
    assert "es anterior a program.start" in errores(data)


def test_recalibrado_el_en_el_futuro_impide_arrancar(cfg):
    """Casi siempre es un año mal escrito, y silencia el aviso durante meses."""
    data = copy.deepcopy(cfg.raw)
    data["program"]["recalibrado_el"] = date.today() + timedelta(days=1)
    assert "está en el futuro" in errores(data)


def test_el_accesor_no_se_inventa_un_valor_por_defecto(cfg):
    """Ni `.get(..., 28)` ni nada parecido.

    Un defecto escondido en el accesor haría que borrar la clave dejara el aviso
    funcionando con un número que no está escrito en ninguna parte, y el
    validador de arriba dejaría de ser la última palabra.
    """
    c = copy.deepcopy(cfg)
    del c.raw["program"]["recalibrar_cada_dias"]
    with pytest.raises(KeyError):
        c.recalibrar_cada_dias

    c2 = copy.deepcopy(cfg)
    del c2.raw["program"]["recalibrado_el"]
    with pytest.raises(KeyError):
        c2.recalibrado_el


# ---------------------------------------------------------------------------
# 3. Lo que dice, y que lo que dice exista
# ---------------------------------------------------------------------------

# Las rutas que el aviso manda mirar. Si alguna se renombra en el `config.yaml`,
# el mensaje seguiría leyéndose perfectamente y mandaría a un sitio que ya no
# está: es la clase de fallo que solo se descubre intentando seguir la
# instrucción, seis semanas después y con prisa.
RUTAS_CITADAS = [
    ("cycling", "classification"),
    ("cycling", "weekend", "total_hours_threshold"),
    ("program", "recalibrado_el"),
]


@pytest.mark.parametrize("ruta", RUTAS_CITADAS, ids=lambda r: ".".join(r))
def test_las_rutas_que_nombra_el_aviso_existen_en_el_config_real(cfg, ruta):
    texto = "\n".join(evaluar_recalibracion(cfg, cfg.recalibrar_cada_dias).lineas())
    assert ".".join(ruta) in texto, "el aviso ya no nombra esta ruta"

    nodo = cfg.raw
    for tramo in ruta:
        assert tramo in nodo, f"{'.'.join(ruta)} no existe en el config.yaml"
        nodo = nodo[tramo]


def test_el_aviso_dice_los_dos_numeros_de_la_cuenta(cfg):
    """Sin ellos es un '¿ya toca?' sin forma de comprobarlo."""
    r = Recalibracion(desde=date(2026, 9, 8), dias=31, cada=28)
    texto = " ".join(r.lineas())
    assert "31" in texto
    assert "28" in texto
    assert "2026-09-08" in texto


def test_el_aviso_explica_que_no_cambiar_nada_tambien_cuenta(cfg):
    """Si no, el único modo de callarlo sería tocar un umbral sin querer tocarlo.

    Y ese es el peor final posible de un recordatorio: que para librarse de él
    haya que cambiar un número que se había decidido dejar como estaba.
    """
    texto = " ".join(evaluar_recalibracion(cfg, cfg.recalibrar_cada_dias).lineas())
    assert "no cambias nada" in texto


# ---------------------------------------------------------------------------
# 4. Que llegue al único sitio donde se lee
# ---------------------------------------------------------------------------


def _decision_con(cfg, dias: int):
    d = decide(cfg, LUNES, sig_completa(LUNES), EngineState())
    d.recalibracion = evaluar_recalibracion(cfg, dias)
    return d


def test_cuando_toca_el_mensaje_de_la_mañana_lo_dice(cfg):
    texto = render_telegram(_decision_con(cfg, cfg.recalibrar_cada_dias), cfg)
    assert "Toca recalibrar" in texto
    assert "program.recalibrado_el" in texto


def test_mientras_no_toca_el_mensaje_no_lo_menciona(cfg):
    texto = render_telegram(_decision_con(cfg, cfg.recalibrar_cada_dias - 1), cfg)
    assert "Toca recalibrar" not in texto
    assert "recalibrar" not in texto.lower()


def test_el_aviso_sale_aunque_el_razonamiento_este_apagado(cfg):
    """`include_reasoning: false` es 'no me cuentes por qué has decidido esto'.

    No es 'ocúltame que llevo un mes decidiendo con unos umbrales que prometí
    revisar'. El aviso vive justo al lado del bloque del razonamiento, que es
    donde habría sido fácil meterlo dentro por descuido.
    """
    c = copy.deepcopy(cfg)
    c.raw.setdefault("notifications", {}).setdefault("telegram", {})
    c.raw["notifications"]["telegram"]["include_reasoning"] = False
    texto = render_telegram(_decision_con(c, c.recalibrar_cada_dias), c)
    assert "Por qué" not in texto
    assert "Toca recalibrar" in texto


def test_una_decision_sin_recalibracion_colgada_no_revienta_el_mensaje(cfg):
    """El replay y los tests construyen decisiones sin pasar por `run_daily`."""
    d = decide(cfg, LUNES, sig_completa(LUNES), EngineState())
    assert d.recalibracion is None
    assert "Toca recalibrar" not in render_telegram(d, cfg)


def test_la_cuenta_viaja_en_el_json_de_la_decision(cfg):
    """Para poder mirarla sin esperar a que el aviso salte."""
    d = _decision_con(cfg, 3)
    bloque = d.to_dict()["recalibracion"]
    assert bloque["dias"] == 3
    assert bloque["cada"] == cfg.recalibrar_cada_dias
    assert bloque["toca"] is False
    assert bloque["lineas"] == []


# ---------------------------------------------------------------------------
# 5. La mañana entera
# ---------------------------------------------------------------------------


class TelegramFalso:
    def __init__(self):
        self.enviados: list[str] = []

    def send(self, texto, *, dry_run=False):
        from app.integrations.telegram import SendResult

        self.enviados.append(texto)
        return SendResult(sent=True, parts=1, reason="")


def _manana(db, cfg, dia, tg=None):
    return run_daily(
        db,
        cfg,
        dia,
        metrics=metricas_de(dia),
        rides=[],
        telegram_client=tg,
    )


def metricas_de(dia):
    return dias(dia, 10, hrv=60.0, rhr=50.0, sleep_min=450, sleep_score=80)


def test_la_mañana_cuenta_el_dia_de_hoy(db, cfg):
    """Hoy ya es muestra: la decisión está guardada antes de redactar el mensaje.

    Sin esto el aviso llegaría siempre un día tarde, que no rompe nada y por eso
    mismo nadie lo notaría nunca.
    """
    dia = LUNES + timedelta(days=7)
    res = _manana(db, cfg_con(cfg, recalibrado_el=dia), dia)
    assert res.decision.recalibracion.dias == 1


def test_decidir_un_dia_anterior_a_la_ultima_revision_no_revienta(db, cfg):
    """Pasa en cada replay y al recalcular una mañana vieja.

    El rango saldría invertido, y `dias_con_decision` lo rechaza a gritos. Que
    ese caso sea legítimo se sabe en `run_daily` y en ningún otro sitio.
    """
    dia = LUNES + timedelta(days=7)
    c = cfg_con(cfg, recalibrado_el=dia + timedelta(days=30))
    res = _manana(db, c, dia)
    assert res.decision.recalibracion.dias == 0
    assert res.decision.recalibracion.lineas() == []


def test_de_la_base_de_datos_al_telegram(db, cfg):
    """El camino completo: días guardados -> cuenta -> aviso en el mensaje.

    Se guardan los días justos para que el de hoy sea el que completa la
    muestra. Un día menos y el mensaje se calla.
    """
    cada = cfg.recalibrar_cada_dias
    primero = LUNES + timedelta(days=7)
    hoy = primero + timedelta(days=cada - 1)
    c = cfg_con(cfg, recalibrado_el=primero)

    for i in range(cada - 1):
        dia = primero + timedelta(days=i)
        save_decision(db, decide(c, dia, sig_completa(dia), EngineState()))

    tg = TelegramFalso()
    res = _manana(db, c, hoy, tg=tg)

    assert res.decision.recalibracion.dias == cada
    assert "Toca recalibrar" in tg.enviados[0]


def test_un_dia_antes_el_telegram_se_calla(db, cfg):
    """El control negativo del test de arriba, con un día menos de muestra."""
    cada = cfg.recalibrar_cada_dias
    primero = LUNES + timedelta(days=7)
    hoy = primero + timedelta(days=cada - 2)
    c = cfg_con(cfg, recalibrado_el=primero)

    for i in range(cada - 2):
        dia = primero + timedelta(days=i)
        save_decision(db, decide(c, dia, sig_completa(dia), EngineState()))

    tg = TelegramFalso()
    res = _manana(db, c, hoy, tg=tg)

    assert res.decision.recalibracion.dias == cada - 1
    assert "Toca recalibrar" not in tg.enviados[0]
