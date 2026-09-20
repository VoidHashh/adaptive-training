"""Los trabajos automáticos y, sobre todo, cómo fallan.

Este módulo es el único del sistema que se ejecuta sin nadie delante. Eso hace
que lo que hay que probar no sean los caminos felices sino los silencios:

- que el fallback de las 09:00 no atropelle una decisión ya tomada con check-in
  y la deje registrada como tomada sin él;
- que un trabajo que revienta o que no llega a ejecutarse AVISE, porque no
  recibir mensaje se parece demasiado a un día de descanso;
- que los defectos de APScheduler que descartan trabajos callando estén
  cambiados, y sigan estándolo.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import repository as repo
from app.models import Base, Decision as DecisionRow, JobRun
from app.engine.rules import REGLA_SIN_DATOS
from app.scheduler import (
    MARGEN_S,
    _avisador,
    _hora,
    build_scheduler,
    job_decision,
    job_fetch_garmin,
    job_reconcile,
    ventana_de_reconciliacion,
)
from tests.conftest import LUNES, dias
from tests.test_runner import HevyFalso, TelegramFalso
from tests.dobles import doble_de, no_es_doble
from app.integrations.garmin import GarminClient
from app.integrations.hevy import HevyClient


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def en_memoria(db, monkeypatch):
    """Ata `session_scope` a la base en memoria del test.

    Los trabajos abren su propia sesión a propósito -se ejecutan en el hilo del
    scheduler, no en el de una petición- así que la única forma de probarlos es
    sustituir de dónde la sacan.
    """
    import app.scheduler as mod

    @contextmanager
    def scope():
        yield db

    monkeypatch.setattr(mod, "session_scope", scope)
    return db


def fetch_falso(cfg, day):
    return dias(day, 10, hrv=60.0, rhr=50.0, sleep_min=450, sleep_score=80), []


# ---------------------------------------------------------------------------
# El fallback de las 09:00
# ---------------------------------------------------------------------------


def test_con_checkin_el_fallback_no_actua(en_memoria, cfg):
    """El peligro no es decidir dos veces: es MENTIR sobre cómo se decidió.

    Si el usuario rellenó el formulario a las 07:40, la decisión buena ya está
    tomada. Volver a tomarla a las 09:00 la sustituiría por otra marcada como
    `fallback_0900`, y el registro diría que ese día se decidió sin check-in
    teniéndolo. Es un dato falso, no un trabajo repetido.
    """
    repo.upsert_checkin(
        en_memoria, LUNES, {"fatigue": 3, "lower_discomfort": 1}, config=cfg
    )
    en_memoria.commit()

    res = job_decision(cfg, day=LUNES, fetch=fetch_falso)

    assert res is None
    assert en_memoria.scalars(select(DecisionRow)).first() is None


def test_sin_checkin_el_fallback_decide_y_lo_declara(en_memoria, cfg):
    res = job_decision(
        cfg, day=LUNES, fetch=fetch_falso,
        hevy_client=HevyFalso(), telegram_client=TelegramFalso(),
    )

    assert res is not None
    fila = repo.current_decision(en_memoria, LUNES)
    assert fila is not None
    assert fila.source == "fallback_0900", (
        "una decisión tomada sin check-in tiene que quedar marcada como tal"
    )


def test_el_origen_no_esta_cableado(en_memoria, cfg):
    """La misma función sirve para el fallback y para lanzarla a mano."""
    job_decision(
        cfg, day=LUNES, fetch=fetch_falso, source="manual",
        solo_si_falta_checkin=False,
    )
    assert repo.current_decision(en_memoria, LUNES).source == "manual"


def test_sin_el_cerrojo_se_puede_forzar_la_decision(en_memoria, cfg):
    repo.upsert_checkin(en_memoria, LUNES, {"fatigue": 3}, config=cfg)
    en_memoria.commit()

    res = job_decision(
        cfg, day=LUNES, fetch=fetch_falso, solo_si_falta_checkin=False
    )
    assert res is not None


def test_el_fallback_avisa_por_telegram(en_memoria, cfg):
    """Sin check-in no hay formulario abierto: el mensaje es lo único que llega."""
    tg = TelegramFalso()
    job_decision(cfg, day=LUNES, fetch=fetch_falso, telegram_client=tg)
    assert tg.enviados, "el día que decide solo el fallback, nadie se entera"


# ---------------------------------------------------------------------------
# La recomputación de las 09:00
# ---------------------------------------------------------------------------
#
# El caso real, del 15 de septiembre de 2026: contenedor arriba a las 06:22,
# check-in a las 06:23, decisión a las 06:23:33. El reloj todavía no había
# subido la noche, Garmin contestó con `"hrv": {}` y el sueño a nulos, y el día
# se decidió con cinco reglas sin evaluar. A las 09:00 el dato ya estaba, pero
# el trabajo se suprimía por la única razón de que existía check-in.
#
# Lo que estos tests fijan es la distinción que faltaba: «ya tengo tu respuesta»
# no es «ya tengo todos los datos».


def sin_wellness(cfg, day):
    """Lo que contesta Garmin cuando el reloj no ha sincronizado la noche."""
    return dias(day, 10), []


def _manana_a_ciegas(db, cfg, day=LUNES):
    """Reproduce la mañana rota: check-in temprano y decisión sin datos del reloj.

    El orden importa y es el de verdad: primero se decide a ciegas -como hace la
    PWA al recibir el formulario- y DESPUÉS se deja el check-in en la base. Al
    revés, el cerrojo saltaría antes de escribir la decisión que estos tests
    necesitan tener delante.
    """
    job_decision(
        cfg, day=day, fetch=sin_wellness, source="checkin",
        solo_si_falta_checkin=False,
        hevy_client=HevyFalso(), telegram_client=TelegramFalso(),
    )
    repo.upsert_checkin(db, day, {"fatigue": 2, "lower_discomfort": 1}, config=cfg)
    db.commit()
    previa = repo.current_decision(db, day)
    assert previa is not None and previa.source == "checkin"
    return previa


def test_una_decision_completa_con_checkin_no_se_recomputa(en_memoria, cfg):
    """El cerrojo de siempre, ahora probado con una decisión DELANTE.

    `test_con_checkin_el_fallback_no_actua` pasaba sin que hubiera ninguna fila
    de decisión en la base, así que no distinguía «no actúo porque la decisión
    está completa» de «no actúo porque no hay nada que mirar». Este sí: hay
    check-in, hay decisión, y esa decisión evaluó todo lo que venía del reloj.
    """
    job_decision(
        cfg, day=LUNES, fetch=fetch_falso, source="checkin",
        solo_si_falta_checkin=False,
        hevy_client=HevyFalso(), telegram_client=TelegramFalso(),
    )
    repo.upsert_checkin(
        en_memoria, LUNES, {"fatigue": 3, "lower_discomfort": 1}, config=cfg
    )
    en_memoria.commit()

    pedido = []

    def fetch_espia(c, d):
        pedido.append(d)
        return fetch_falso(c, d)

    assert job_decision(cfg, day=LUNES, fetch=fetch_espia) is None
    assert not pedido, (
        "con la decisión ya completa no hay que volver a molestar a Garmin"
    )
    assert repo.current_decision(en_memoria, LUNES).source == "checkin"


def test_la_decision_ciega_se_recomputa_cuando_llega_el_wellness(en_memoria, cfg):
    """El arreglo, entero: se vuelve a decidir y la anterior deja de ser vigente."""
    previa = _manana_a_ciegas(en_memoria, cfg)
    saltadas = previa.skipped_rules_json or ""
    assert "hrv" in saltadas and "sleep_min" in saltadas, (
        "la premisa del test: la decisión de las 06:23 salió sin datos del reloj"
    )

    res = job_decision(
        cfg, day=LUNES, fetch=fetch_falso,
        hevy_client=HevyFalso(), telegram_client=TelegramFalso(),
    )

    assert res is not None, "con el dato ya disponible hay que volver a decidir"
    vigente = repo.current_decision(en_memoria, LUNES)
    assert vigente.source == "recompute", (
        "no es `fallback_0900`: ese día SÍ hubo check-in, y marcarlo así sería "
        "mentir sobre cómo se decidió"
    )
    assert vigente.id != previa.id
    en_memoria.refresh(previa)
    assert previa.is_current is False, (
        "la decisión ciega tiene que quedar registrada, pero no vigente"
    )


def test_si_el_wellness_sigue_sin_llegar_no_se_escribe_nada(en_memoria, cfg):
    """Una segunda decisión idéntica con otra fuente ensucia y no arregla nada.

    Este es el test que impide que el arreglo se pase de listo. Recomputar por
    el mero hecho de que faltara un dato -sin comprobar que el dato ha llegado-
    reescribiría la decisión todas las mañanas en las que el reloj no sincronice
    en toda la noche, cambiándole la fuente a `recompute` sin haber evaluado ni
    una regla más.
    """
    previa = _manana_a_ciegas(en_memoria, cfg)

    assert job_decision(cfg, day=LUNES, fetch=sin_wellness) is None
    en_memoria.refresh(previa)
    assert previa.is_current is True
    assert previa.source == "checkin"


def test_lo_que_falta_y_no_es_del_reloj_no_dispara_recomputacion(en_memoria, cfg):
    """Una regla saltada por una señal del formulario no la arregla Garmin.

    Y las derivadas tampoco cuentan: `hrv_baseline` falta porque no hay días
    suficientes de historia, no porque el reloj vaya tarde. Si contaran, cada
    mañana de una instalación recién estrenada sería una recomputación
    garantizada que nunca puede salir bien.
    """
    from app.scheduler import _medidas_que_faltaban

    def fila(missing):
        import json
        return SimpleNamespace(
            date=LUNES,
            skipped_rules_json=json.dumps([{"name": "r", "missing": missing}]),
        )

    assert _medidas_que_faltaban(fila(["fatigue", "training_desire"])) == set()
    assert _medidas_que_faltaban(fila(["hrv_baseline", "rhr_baseline"])) == set()
    assert _medidas_que_faltaban(fila(["rhr", "rhr_delta"])) == {"rhr"}
    assert _medidas_que_faltaban(None) == set()
    assert _medidas_que_faltaban(
        SimpleNamespace(date=LUNES, skipped_rules_json="{no es json")
    ) == set()


def test_la_recomputacion_cuenta_que_anula_a_la_de_antes(en_memoria, cfg):
    """El usuario tiene dos mensajes en el móvil: hay que decirle cuál manda."""
    previa = _manana_a_ciegas(en_memoria, cfg)
    tg = TelegramFalso()

    res = job_decision(
        cfg, day=LUNES, fetch=fetch_falso,
        hevy_client=HevyFalso(), telegram_client=tg,
    )

    a = res.decision.anulacion
    assert a is not None
    assert a.anterior == previa.light
    assert a.fuente_anterior == "checkin"
    # `hrv` y `sleep_min`, no `rhr`. Ver el test de abajo: al borrar las dos
    # reglas de pulso, `rhr` dejó de ser deducible de `skipped_rules_json`.
    assert "hrv" in a.medidas and "sleep_min" in a.medidas
    assert tg.enviados, "una recomputación que no se cuenta no sirve de nada"
    assert "♻️" in tg.enviados[-1], (
        "el mensaje de las 09:00 tiene que presentarse como lo que es"
    )


def test_el_historico_explica_el_cambio_sin_columna_nueva(en_memoria, cfg):
    """La anulación no se guarda en ninguna columna, y no hace falta.

    `save_decision` escribe campo a campo y no hay hueco para ella. La
    tentación es añadir uno; la razón para no hacerlo es que el histórico YA
    contesta la pregunta de dentro de tres meses -«¿por qué el martes el
    semáforo cambió a las nueve?»- con lo que guarda:

      · dos filas del mismo día, la ciega marcada `is_current=False` y con
        fuente `checkin`, la nueva vigente y con fuente `recompute`;
      · y la diferencia entre sus dos `skipped_rules_json`, que es exactamente
        la lista de lo que el reloj subió entre una hora y la otra.

    Este test fija esa reconstrucción. Si algún día deja de poder hacerse, la
    columna pasa de innecesaria a imprescindible y hay que enterarse aquí y no
    tres meses después, delante de una fila muda.

    LO QUE YA NO SE RECONSTRUYE, Y POR QUÉ
    --------------------------------------
    Aquí se pedían `hrv` y `rhr`. `rhr` se ha caído, y no por un fallo: esta
    lista se deduce de los `missing` de las reglas SALTADAS, así que solo puede
    hablar de medidas de las que dependa alguna regla. Al borrar
    `fc_reposo_elevada` y `fc_reposo_disparada` -sus lápidas están en
    `config.yaml`- el pulso de reposo dejó de tener ninguna, y con ello dejó de
    dejar rastro aquí.

    El dato NO se ha perdido: se sigue pidiendo, se sigue guardando en
    `daily_metrics` y se sigue pintando. Lo que se ha perdido es poder deducir
    A QUÉ HORA llegó mirando solo las decisiones. Es el precio de la lápida y se
    escribe aquí para que sea una decisión y no una sorpresa: el día que el
    pulso vuelva a tener regla, esta reconstrucción vuelve sola.
    """
    import json

    _manana_a_ciegas(en_memoria, cfg)
    job_decision(
        cfg, day=LUNES, fetch=fetch_falso,
        hevy_client=HevyFalso(), telegram_client=TelegramFalso(),
    )

    filas = en_memoria.scalars(
        select(DecisionRow).where(DecisionRow.date == LUNES).order_by(DecisionRow.id)
    ).all()
    assert [f.source for f in filas] == ["checkin", "recompute"]
    assert [f.is_current for f in filas] == [False, True]

    def faltaban(fila):
        return {
            s
            for r in json.loads(fila.skipped_rules_json or "[]")
            for s in r.get("missing") or []
        }

    llego = faltaban(filas[0]) - faltaban(filas[1])
    assert {"hrv", "sleep_min"} <= llego, (
        "de las dos filas tiene que poder deducirse qué subió el reloj a las "
        f"09:00, y de estas se deduce {sorted(llego)}"
    )


def test_el_ambar_por_precaucion_se_deshace_y_se_cuenta(en_memoria, cfg):
    """El ciclo entero del arreglo, de punta a punta y en el sentido nuevo.

    Hasta ahora la anulación solo se veía cuando el día EMPEORABA al llegar el
    dato: se había decidido verde a ciegas y la HRV lo bajaba a ámbar. Desde que
    un verde ciego sale ámbar por precaución, el caso normal es el contrario -el
    día MEJORA-, y ese es justo el que corre peligro de pasar callando: un
    segundo mensaje diciendo «verde» sin explicar nada se lee como si el de las
    06:23 nunca hubiera existido, o peor, como dos semáforos contradictorios del
    mismo día sin forma de saber cuál manda.

    Se comprueba la cadena completa porque cada eslabón se rompe por su cuenta:
    que la decisión ciega salga ámbar, que la recomputación siga disparándose
    -depende de `skipped_rules_json`, que la promoción NO debe contaminar-, que
    el color acabe en verde, y que el mensaje lo anuncie como anulación.
    """
    previa = _manana_a_ciegas(en_memoria, cfg)
    assert previa.light == "amber", (
        "la mañana a ciegas tiene que salir ámbar por precaución; si sale verde "
        "es que la promoción no está actuando y el resto del test no mide nada"
    )
    assert previa.trigger_rule == REGLA_SIN_DATOS

    tg = TelegramFalso()
    res = job_decision(
        cfg, day=LUNES, fetch=fetch_falso,
        hevy_client=HevyFalso(), telegram_client=tg,
    )

    assert res is not None
    assert res.decision.light == "green", (
        "con la noche ya subida y un check-in tranquilo, no queda nada que "
        "ponga el día en ámbar"
    )
    a = res.decision.anulacion
    assert a is not None and a.anterior == "amber"
    assert a.cambia_el_color("green")

    ultimo = tg.enviados[-1]
    assert "♻️" in ultimo and "<b>" in ultimo, (
        "cuando el color cambia, la anulación va en negrita y es la noticia del "
        "día: el mensaje anterior ya no vale"
    )
    assert "Ámbar por precaución" not in ultimo, (
        "el aviso es de la decisión de las 06:23, no de esta: repetirlo aquí "
        "diría que el verde también se ha decidido a ciegas"
    )


# ---------------------------------------------------------------------------
# La reconciliación
# ---------------------------------------------------------------------------


def test_reconciliar_sin_cliente_revienta_en_vez_de_callar(en_memoria, cfg):
    """Sin Hevy no se sabe qué se entrenó, y eso tiene que doler.

    Las rachas se quedan quietas -eso estaba bien y sigue igual: darlas por
    hechas subiría la carga sin haber entrenado-, pero antes se quedaban
    quietas en SILENCIO. Un WARNING a las 22:30, en un hilo de APScheduler y
    en un Umbrel al que nadie se conecta, no lo lee nadie nunca.

    Y lo que se pierde no es un log: sin reconciliación no se registra el
    cumplimiento, las rachas no avanzan y la progresión se para. La forma de
    enterarse era notar semanas después que no sube nada, sin ningún hilo del
    que tirar, porque el mensaje de las nueve seguía llegando como si tal cosa.

    Lanzando salta `_avisador` (EVENT_JOB_ERROR) y llega un Telegram esa misma
    noche.
    """
    with pytest.raises(RuntimeError) as exc:
        job_reconcile(cfg, day=LUNES, hevy_client=None)

    msg = str(exc.value)
    assert "progresión se para" in msg
    assert "HEVY_API_KEY" in msg, "el error tiene que decir por dónde empezar"


def _avisar_de(excepcion, monkeypatch, *respuestas):
    """Dispara `_avisador` con un cliente de Telegram DE VERDAD.

    Aquí había un doble con un `send()` de dos líneas que apuntaba el texto en
    una lista y devolvía `None`. Aceptaba cualquier cosa, así que el test no
    podía fallar por el motivo por el que el aviso fallaba de verdad: que
    Telegram no sabía leer el HTML y no mandaba nada. Ahora pasa por el
    `TelegramClient` real y por el doble de httpx, que reproduce ese 400.
    """
    import sys

    from apscheduler.events import EVENT_JOB_ERROR, JobExecutionEvent

    from app.integrations.telegram import TelegramClient
    from app.scheduler import _avisador
    from tests.conftest import FakeHTTP, FakeResponse

    doble = FakeHTTP(list(respuestas) or [FakeResponse(200, {"ok": True})])

    @no_es_doble("suplanta al MODULO httpx, no a una clase de app/")
    class ModuloFalso:
        @staticmethod
        def Client(*a, **k):  # noqa: N802 - imita la API de httpx
            return doble

    monkeypatch.setitem(sys.modules, "httpx", ModuloFalso)

    _avisador(TelegramClient(bot_token="123:abc", chat_id="42"))(
        JobExecutionEvent(
            EVENT_JOB_ERROR, "reconcile", None, None, exception=excepcion,
        )
    )
    return doble


def test_el_avisador_manda_telegram_cuando_la_reconciliacion_revienta(monkeypatch):
    """La cadena entera: excepción -> listener -> Telegram.

    Probar solo que lanza no sirve de nada si el aviso no llega: el motivo de
    lanzar es justamente que llegue.
    """
    doble = _avisar_de(RuntimeError("sin cliente de Hevy"), monkeypatch)

    assert len(doble.llamadas) == 1
    enviado = doble.llamadas[0]["json"]
    assert "reconcile" in enviado["text"]
    assert enviado["parse_mode"] == "HTML"


def test_el_aviso_sale_aunque_la_excepcion_lleve_angulos(monkeypatch):
    """EL FALLO. Un `<` en el texto de la excepción tumbaba el aviso entero.

    `str()` de las excepciones de Python lleva ángulos constantemente. Este
    `TypeError` es literal: es el que sale al comparar un `None` con un número,
    que es exactamente la forma en que revienta un trabajo al que le falta un
    dato. Sin escapar, Telegram contestaba `can't parse entities` y el aviso no
    salía. O sea que el único efecto hacia fuera sin freno -el que avisa de que
    algo ha reventado- se caía justo por reventar algo.

    FALSACIÓN: quitando `escapar_html` de `scheduler.py`, este test falla con el
    400 del doble. Comprobado.
    """
    exc = TypeError("'<' not supported between instances of 'NoneType' and 'int'")
    doble = _avisar_de(exc, monkeypatch)

    assert len(doble.llamadas) == 1, "un solo intento: no hizo falta el plan B"
    enviado = doble.llamadas[0]["json"]
    assert enviado["parse_mode"] == "HTML", "y salió con formato, no degradado"
    assert "&lt;" in enviado["text"], "el ángulo va escapado"
    assert "'<' not supported" not in enviado["text"]


def test_si_el_aviso_no_se_envia_queda_dicho_en_el_log(monkeypatch, caplog):
    """No se puede avisar de que el aviso falló mandando otro aviso.

    `send()` no lanza cuando la API rechaza: devuelve `SendResult(sent=False)`.
    El listener se lo tragaba. El log es el último sitio donde puede constar.
    """
    from tests.conftest import FakeResponse

    with caplog.at_level("ERROR"):
        _avisar_de(
            RuntimeError("boom"), monkeypatch,
            FakeResponse(400, text="Bad Request: chat not found"),
        )

    assert "NO se ha enviado" in caplog.text, caplog.text
    assert "chat not found" in caplog.text, "y con el motivo, no solo el hecho"


# ---------------------------------------------------------------------------
# La ventana de la reconciliación
#
# `workout_log` SOLO se escribe aquí. Así que la ventana de este trabajo no es
# un detalle de eficiencia: es cuánto pasado es capaz de recordar el sistema
# sobre lo que el usuario entrenó. Con los tres días fijos de antes, un apagón
# de cuatro noches no dejaba el historial incompleto, lo dejaba vacío en ese
# tramo, para siempre y sin ninguna marca de que faltaba algo.
# ---------------------------------------------------------------------------


def cerro_el(sesion, cuando: datetime) -> None:
    """Deja escrito que la reconciliación terminó bien en `cuando` (UTC naive).

    Es la marca que el escuchador del scheduler pone solo al TERMINAR un
    trabajo, o sea la única prueba positiva de que esa noche corrió.
    """
    sesion.add(JobRun(
        job_id="reconcile",
        first_seen_at=cuando - timedelta(days=30),
        last_finished_at=cuando,
    ))
    sesion.commit()


def test_al_dia_la_ventana_se_queda_en_el_suelo(en_memoria):
    """CONTRAGUARDA de todo este bloque: al día, la ventana NO se dispara.

    Si esto no estuviera, una ventana que devolviera siempre el tope pasaría
    todos los demás tests de aquí y nadie notaría que cada noche se repasan
    cuarenta y cinco días para nada.
    """
    cerro_el(en_memoria, datetime(2026, 9, 6, 20, 30))
    assert ventana_de_reconciliacion(LUNES, minimo=3, tope=45) == 3


def test_la_ventana_cubre_las_noches_que_no_se_cerraron(en_memoria):
    """EL FALLO. La marca de la última noche buena existía y no la leía nadie.

    `auditar_arranque` ya detectaba la cita perdida y ya avisaba por Telegram
    -«no se apuntó lo que entrenaste»-, y el trabajo siguiente volvía a mirar
    sus tres días fijos. El sistema sabía lo que le faltaba y no iba a por ello.
    """
    cerro_el(en_memoria, datetime(2026, 8, 30, 20, 30))
    assert ventana_de_reconciliacion(LUNES, minimo=3, tope=45) == 8


def test_la_ventana_no_pasa_del_tope(en_memoria):
    """El tope no es prudencia genérica: es el límite de `get_workouts`.

    Pagina de diez en diez y LANZA cuando sus páginas no cubren lo pedido,
    antes que devolver media lista haciéndola pasar por entera. Sin tope, «he
    tenido esto apagado cuatro meses» se convierte en un trabajo nocturno que
    revienta todas las noches sin recuperar nunca nada.
    """
    cerro_el(en_memoria, datetime(2025, 5, 1, 20, 30))
    assert ventana_de_reconciliacion(LUNES, minimo=3, tope=45) == 45


def test_sin_marca_de_cierre_se_mira_el_tope_entero(en_memoria):
    """Nunca se cerró una: no se sabe qué falta, y suponer que nada es el error.

    Esta es exactamente la situación que dejó `workout_log` con UNA fila -un
    historial entero anterior al sistema, y una ventana que nunca llegaba a
    él-. Mirar atrás del todo cuesta una vez; no mirar cuesta el historial.
    """
    assert en_memoria.get(JobRun, "reconcile") is None, "la tabla está vacía"
    assert ventana_de_reconciliacion(LUNES, minimo=3, tope=45) == 45


def test_reconciliar_mira_hacia_atras_y_no_solo_hoy(en_memoria, cfg):
    """Una sesión de las diez de la noche se sincroniza al día siguiente.

    Si la reconciliación mirara solo el día en curso, esa sesión llegaría tarde
    a la suya y la racha se rompería por un problema de reloj y no por una
    sesión mal hecha. Por eso `dias_atras` sigue siendo un SUELO y no desaparece
    cuando el sistema está al día.
    """
    cerro_el(en_memoria, datetime(2026, 9, 6, 20, 30))
    pedido = {}

    @doble_de(HevyClient)
    class Hevy:
        def get_workouts(self, since, *, max_pages=5):
            pedido["since"] = since
            return []

    salida = job_reconcile(cfg, day=LUNES, hevy_client=Hevy(), dias_atras=3)

    assert pedido["since"] == LUNES - timedelta(days=3)
    assert len(salida) == 4, "se repasan los 3 días de atrás Y el de hoy"
    assert [r.day for r in salida] == [
        LUNES - timedelta(days=i) for i in (3, 2, 1, 0)
    ]


def test_reconciliar_recupera_la_noche_que_el_pc_estuvo_apagado(
    en_memoria, cfg, caplog
):
    """EL CASO REAL. Este sistema corre en un PC que se apaga por la noche.

    La cita del 2026-09-16 a las 22:30 no llegó a existir, y la sesión de ese
    día seguía sin registrar al día siguiente. Se salvó de milagro: tres días
    daban justo para alcanzarla. Cuatro no habrían dado, y la sesión se habría
    perdido en silencio -sin fila, sin hueco visible, sin nada que mirar-.
    """
    cerro_el(en_memoria, datetime(2026, 8, 31, 20, 30))
    pedido = {}

    @doble_de(HevyClient)
    class Hevy:
        def get_workouts(self, since, *, max_pages=5):
            pedido["since"] = since
            return []

    with caplog.at_level("WARNING"):
        salida = job_reconcile(cfg, day=LUNES, hevy_client=Hevy(), dias_atras=3)

    assert pedido["since"] == LUNES - timedelta(days=7), "se pide desde el hueco"
    assert len(salida) == 8, "los 7 días sin cerrar Y el de hoy"
    assert [r.day for r in salida] == [
        LUNES - timedelta(days=i) for i in range(7, -1, -1)
    ]
    # Y que se sepa: una noche de recuperación no puede parecerse a una normal.
    assert "se miran 7 días y no 3" in caplog.text, caplog.text


def test_reconciliar_pide_los_entrenamientos_una_sola_vez(en_memoria, cfg):
    """Cuatro días no son cuatro peticiones: Hevy limita por IP.

    Y con la ventana variable esto importa más que antes, no menos: la noche
    de después de un apagón largo es justo cuando el bucle es más largo.
    """
    cerro_el(en_memoria, datetime(2026, 8, 31, 20, 30))
    llamadas = []

    @doble_de(HevyClient)
    class Hevy:
        def get_workouts(self, since, *, max_pages=5):
            llamadas.append(since)
            return []

    job_reconcile(cfg, day=LUNES, hevy_client=Hevy(), dias_atras=3)
    assert len(llamadas) == 1


# ---------------------------------------------------------------------------
# La caché de Garmin
# ---------------------------------------------------------------------------


def cache_sana(day, *, dias_cubiertos: int = 180):
    """Una caché larga y refrescada hoy: el caso normal a partir del día dos."""
    from app.engine.signals import Ride
    from app.integrations.activity_cache import CachedActivities

    primero = day - timedelta(days=dias_cubiertos - 1)
    return CachedActivities(
        rides=[Ride(date=primero, duration_s=3600), Ride(date=day, duration_s=3600)],
        file_mtime=datetime.combine(day, datetime.min.time()),
        total_activities=2,
        first_day=primero,
        last_day=day,
    )


def _espia_refresco(monkeypatch, cache=None):
    """Sustituye `refresh_cache` y, si se pide, también la caché que se lee.

    La caché se sustituye porque si no este test lee el
    `data/cache/activities.json` REAL del usuario, y entonces la ventana que
    sale depende de cuándo montó en bici por última vez.
    """
    visto: dict = {}

    def refresh(path, day, *, days, fetch):
        visto.update(path=path, day=day, days=days)
        return 7

    monkeypatch.setattr("app.integrations.activity_cache.refresh_cache", refresh)
    if cache is not None:
        monkeypatch.setattr(
            "app.integrations.activity_cache.load_cached_rides", lambda p: cache
        )
    return visto


def test_el_refresco_de_garmin_va_aparte_de_la_decision(monkeypatch, cfg):
    """Garmin limita por IP. Si leer y decidir fueran el mismo trabajo, un 429
    se llevaría por delante las dos cosas."""
    visto = _espia_refresco(monkeypatch, cache=cache_sana(LUNES))
    assert job_fetch_garmin(cfg, day=LUNES, fetch=None) == 7
    assert visto["day"] == LUNES
    assert visto["path"].name == "activities.json"


def test_el_refresco_diario_usa_la_ventana_corta_del_yaml(monkeypatch, cfg):
    """Aquí había un `RIDE_HISTORY_DAYS = 190` mientras `cycling.fetch`
    declaraba 10, y como `save_cache` fusiona y no poda nunca, esos 190 días
    se re-descargaban cada madrugada para añadir la salida de ayer. Con el
    cupo de Garmin, eso no es una ineficiencia: es el 429 que deja al trabajo
    de las 06:30 sin histórico."""
    visto = _espia_refresco(monkeypatch, cache=cache_sana(LUNES))
    job_fetch_garmin(cfg, day=LUNES, fetch=None)
    assert visto["days"] == cfg.raw["cycling"]["fetch"]["lookback_days"]


def test_sin_cache_el_refresco_se_trae_el_historico_entero(monkeypatch, cfg):
    """El primer arranque, y también el día que la caché se pierda: si no,
    los percentiles de carga se quedan sin base y nadie se entera."""
    from app.integrations.activity_cache import CachedActivities

    visto = _espia_refresco(monkeypatch, cache=CachedActivities(error="no existe"))
    job_fetch_garmin(cfg, day=LUNES, fetch=None)
    assert visto["days"] == cfg.raw["cycling"]["fetch"]["backfill_days"]


# ---------------------------------------------------------------------------
# El avisador
# ---------------------------------------------------------------------------
#
# Sin esto el modo de fallo del sistema es el silencio, y el silencio es
# indistinguible de un día de descanso.


def evento_error(exc=RuntimeError("Garmin no contesta")):
    return SimpleNamespace(job_id="decision_fallback", exception=exc)


def evento_perdido():
    return SimpleNamespace(
        job_id="decision_fallback",
        exception=None,
        scheduled_run_time=datetime(2026, 9, 7, 9, 0),
    )


def test_un_trabajo_que_revienta_se_cuenta():
    tg = TelegramFalso()
    _avisador(tg)(evento_error())

    assert len(tg.enviados) == 1
    texto = tg.enviados[0]
    assert "decision_fallback" in texto
    assert "Garmin no contesta" in texto, "el aviso sin la causa no sirve de nada"


def test_un_trabajo_que_no_llego_a_ejecutarse_tambien_se_cuenta():
    """El caso del Umbrel apagado. Sin aviso, un día sin decisión pasa por un
    día sin entrenamiento."""
    tg = TelegramFalso()
    _avisador(tg)(evento_perdido())

    assert len(tg.enviados) == 1
    assert "no llegó a ejecutarse" in tg.enviados[0]


def test_los_dos_avisos_se_distinguen():
    """Que reviente y que no se ejecute se arreglan de forma distinta."""
    tg = TelegramFalso()
    escuchar = _avisador(tg)
    escuchar(evento_error())
    escuchar(evento_perdido())
    assert tg.enviados[0] != tg.enviados[1]


def test_sin_telegram_el_avisador_no_tumba_el_scheduler():
    _avisador(None)(evento_error())  # no debe lanzar


def test_si_ni_el_aviso_se_puede_mandar_queda_escrito(caplog):
    """El último eslabón: si falla avisar del fallo, al menos que haya un log."""
    tg = TelegramFalso(revienta=True)
    with caplog.at_level("ERROR"):
        _avisador(tg)(evento_error())
    assert "no se pudo avisar" in caplog.text


# ---------------------------------------------------------------------------
# El montaje
# ---------------------------------------------------------------------------


def test_los_defectos_que_fallan_callando_estan_cambiados(cfg):
    """`misfire_grace_time` vale 1 segundo de fábrica.

    Con ese defecto, un contenedor que arranca a las 06:31 descarta el trabajo
    de las 06:30 sin ejecutarlo y sin avisar. En un Umbrel que se reinicia por
    una actualización eso es el caso normal, no el raro.
    """
    sched = build_scheduler(cfg, start=False)
    d = sched._job_defaults

    assert d["misfire_grace_time"] >= 3600, (
        "con menos margen que un reinicio, el trabajo del día se pierde callando"
    )
    assert d["misfire_grace_time"] == MARGEN_S
    assert d["coalesce"] is True, "tras una parada larga se decide UNA vez"
    assert d["max_instances"] == 1, "un único escritor sobre SQLite"


def test_estan_los_seis_trabajos_del_dia_y_los_dos_del_arranque(cfg):
    """Seis con hora y dos que se disparan al arrancar.

    Los dos del arranque hacen cosas distintas y por eso son dos: uno recupera
    de Garmin los días de bienestar que falten, y el otro -`startup_audit`-
    mira qué trabajos DEBIERON correr mientras el sistema no estaba. Ver
    `tests/test_arranque_perdido.py` para por qué el aviso de APScheduler no
    cubre ese caso.

    Dos de los seis no HACEN nada por su cuenta y por eso se confunden con un
    olvido si no se nombran: `watchdog` solo mira -ver `tests/test_vigilancia.py`-
    y `recompute_early` solo actúa sobre la decisión que se tomó ciega de la
    HRV y del sueño, que es el caso que se da cuando el formulario se rellena
    antes de que el reloj suba la noche. Ver `tests/test_datos_al_dia.py`.
    """
    sched = build_scheduler(cfg, start=False)
    assert {j.id for j in sched.get_jobs()} == {
        "garmin_fetch", "decision_fallback", "recompute_early", "reconcile",
        "perception_notice", "watchdog", "backfill_wellness", "startup_audit",
    }


def test_el_aviso_de_percepcion_va_despues_de_la_decision(cfg):
    """Es un mensaje sobre AYER, y tiene que llegar cuando el de hoy ya llegó.

    Si saliera antes del fallback de las nueve, el usuario recibiría primero un
    comentario sobre la sesión de ayer y después el plan de hoy, y los leería
    como una sola cosa: el contador se convertiría en el preámbulo de la
    decisión, que es justo lo que se pidió que no fuera.

    Y hay una razón de dato además de la de tono: la sesión de ayer necesita el
    check-in de ESTA mañana para tener su esfuerzo percibido. Avisar a las siete
    sería avisar antes de poder evaluar.
    """
    sched = build_scheduler(cfg, start=False)

    def minutos(job_id: str) -> int:
        t = sched.get_job(job_id).trigger
        campos = {f.name: str(f) for f in t.fields}
        return int(campos["hour"]) * 60 + int(campos["minute"])

    assert minutos("perception_notice") > minutos("decision_fallback"), (
        "el aviso de ayer tiene que llegar despues del plan de hoy"
    )
    # Y la vigilancia también, por otro motivo: mira en qué estado ha quedado
    # el sistema DESPUÉS de las escrituras del día. Antes de las nueve estaría
    # mirando las de ayer y diría que todo está bien el día que se rompa.
    assert minutos("watchdog") > minutos("decision_fallback"), (
        "la vigilancia tiene que mirar despues de que se haya escrito en Hevy"
    )


def test_la_recuperacion_no_tiene_hora_sino_retraso(cfg):
    """Los agujeros de `daily_metrics` no los abre una hora del día.

    Los abre que el proceso no estuviera corriendo. Un trabajo a las 06:40 no
    arregla nada si el PC estuvo apagado la semana entera, porque a las 06:40 de
    esos días tampoco había nadie. El único momento en que consta que el sistema
    está vivo es justo después de arrancar.

    El retraso es para no competir con el arranque: que el servidor acabe de
    levantarse y la PWA responda antes de ponerse a hablar con Garmin.
    """
    from zoneinfo import ZoneInfo

    from apscheduler.triggers.date import DateTrigger

    from app.scheduler import RETRASO_BACKFILL_S

    antes = datetime.now(ZoneInfo(cfg.timezone))
    sched = build_scheduler(cfg, start=False)
    trabajo = sched.get_job("backfill_wellness")

    assert isinstance(trabajo.trigger, DateTrigger), (
        "con un cron, un arranque a las 06:31 no repasaría nada hasta mañana"
    )
    espera = (trabajo.trigger.run_date - antes).total_seconds()
    assert RETRASO_BACKFILL_S - 5 <= espera <= RETRASO_BACKFILL_S + 5, espera
    assert 30 <= RETRASO_BACKFILL_S <= 300, (
        "ni a la vez que el arranque, ni tan tarde que un contenedor que se "
        "reinicia a menudo no llegue nunca a ejecutarlo"
    )


# ---------------------------------------------------------------------------
# La recuperación de los días perdidos
# ---------------------------------------------------------------------------


@doble_de(GarminClient)
class ClienteWellness:
    """Garmin de mentira para el repaso del arranque.

    `fetch_errors` ESTABA EN LA CLASE, Y EL ORIGINAL LO DA POR INSTANCIA
    -------------------------------------------------------------------
    Era `fetch_errors: list[str] = []` a nivel de clase, o sea UNA sola lista
    para todas las instancias de toda la batería. `GarminClient` lo declara con
    `field(default_factory=list)`, que da una por instancia. Así que el doble
    modelaba una cosa distinta de la que dice modelar, y además arrastraba los
    fallos de un test al siguiente: un test que pasa solo podía fallar en la
    batería entera, o al revés, sin que nada lo explicara.
    """

    def __init__(self, revienta_desde: int | None = None) -> None:
        self.revienta_desde = revienta_desde
        self.pedidos = []
        self.fetch_errors: list[str] = []

    def day_metrics(self, day):
        from app.engine.signals import DayMetrics
        from app.integrations.garmin import GarminRateLimited

        dia = day
        self.pedidos.append(dia)
        if self.revienta_desde is not None and len(self.pedidos) > self.revienta_desde:
            raise GarminRateLimited("429")
        return DayMetrics(
            date=dia, hrv=57.0, rhr=46.0, sleep_min=420, sleep_score=74,
            body_battery=70,
        )


def _wellness(cfg_copia, dias_atras: int):
    cfg_copia.raw.setdefault("wellness", {}).setdefault("backfill", {}).update(
        {"recovery_days": dias_atras, "pause_seconds": 0}
    )
    return cfg_copia


def test_la_recuperacion_no_se_conecta_si_no_falta_nada(en_memoria, cfg_copia):
    """Se mira la base ANTES de hacer login.

    En el caso normal -el PC encendido de ayer a hoy- no falta ningún día, y
    entonces esto no puede gastar ni una petición ni una sesión de Garmin.
    Conectarse primero y preguntar después convertiría cada despliegue en un
    login, que es de las cosas que Garmin cuenta para cortar por IP.

    Se prueba SIN inyectar cliente a propósito: si intentara construir uno,
    `build_client` saldría a la red y el cerrojo de `conftest` lo mataría. O
    sea que este test falla exactamente si el login deja de ser condicional.
    """
    from app.models import DailyMetrics
    from app.scheduler import job_backfill_wellness

    for i in range(2, 6):
        en_memoria.add(DailyMetrics(date=LUNES - timedelta(days=i), fetch_status="ok"))
    en_memoria.commit()

    assert job_backfill_wellness(_wellness(cfg_copia, 5), day=LUNES).pedidos == []


def test_la_recuperacion_apagada_no_pide_nada(en_memoria, cfg_copia):
    from app.scheduler import job_backfill_wellness

    assert job_backfill_wellness(_wellness(cfg_copia, 0), day=LUNES).pedidos == []


def test_la_recuperacion_escribe_los_dias_que_faltan_y_los_marca(
    en_memoria, cfg_copia
):
    """Ni hoy ni ayer: los datos de anoche llegan cuando el reloj sincroniza."""
    from app.models import DailyMetrics
    from app.scheduler import job_backfill_wellness

    cliente = ClienteWellness()
    res = job_backfill_wellness(_wellness(cfg_copia, 4), day=LUNES, cliente=cliente)

    esperados = [LUNES - timedelta(days=i) for i in (4, 3, 2)]
    assert res.escritos == esperados
    assert LUNES not in cliente.pedidos
    assert LUNES - timedelta(days=1) not in cliente.pedidos

    filas = en_memoria.scalars(select(DailyMetrics)).all()
    assert {f.date for f in filas} == set(esperados)
    assert all(f.recovered_at is not None for f in filas), (
        "se rellenaron a posteriori y tiene que constar, porque una fila "
        "recuperada puede tener huecos que la del día no habría tenido"
    )


def test_un_corte_por_limite_al_arrancar_revienta_para_que_se_sepa(
    en_memoria, cfg_copia
):
    """Un backfill a medias es justo el agujero que este trabajo existe para tapar.

    Y nadie lo va a repetir: la recuperación solo se dispara al arrancar, y un
    PC que se queda encendido no vuelve a arrancar en semanas. Lanzando, salta
    `_avisador` y llega un Telegram; devolviendo el resultado y ya está, el
    hueco se quedaría tapado a medias y en silencio, que es justo como se abrió.
    """
    from app.models import DailyMetrics
    from app.scheduler import job_backfill_wellness

    with pytest.raises(RuntimeError, match="límite"):
        job_backfill_wellness(
            _wellness(cfg_copia, 4), day=LUNES,
            cliente=ClienteWellness(revienta_desde=1),
        )

    # Lanzar no puede costar lo ya escrito: `rellenar` hace commit por día, así
    # que aquí no queda nada pendiente de una transacción que ya no se cierra.
    filas = en_memoria.scalars(select(DailyMetrics)).all()
    assert [f.date for f in filas] == [LUNES - timedelta(days=4)], (
        "el día que sí llegó tiene que seguir escrito"
    )


def test_las_horas_salen_del_config_y_no_del_codigo(cfg_copia):
    cfg_copia.raw["schedule"] = {
        "garmin_fetch_time": "05:15",
        "fallback_decision_time": "10:45",
        "evening_summary_time": "23:05",
    }
    sched = build_scheduler(cfg_copia, start=False)
    horas = {j.id: str(j.trigger) for j in sched.get_jobs()}

    assert "hour='5'" in horas["garmin_fetch"]
    assert "minute='15'" in horas["garmin_fetch"]
    assert "hour='10'" in horas["decision_fallback"]
    assert "minute='45'" in horas["decision_fallback"]
    assert "hour='23'" in horas["reconcile"]


def test_sin_horas_en_el_config_se_usan_las_de_siempre(cfg_copia):
    cfg_copia.raw.pop("schedule", None)
    assert _hora(cfg_copia, "fallback_decision_time", "09:00") == (9, 0)
    assert _hora(cfg_copia, "garmin_fetch_time", "06:30") == (6, 30)


def test_una_hora_vacia_en_el_config_cae_al_defecto(cfg_copia):
    cfg_copia.raw["schedule"] = {"fallback_decision_time": None}
    assert _hora(cfg_copia, "fallback_decision_time", "09:00") == (9, 0)


def test_el_scheduler_va_en_la_zona_horaria_del_config(cfg):
    """Decidir en UTC adelantaría la decisión dos horas en verano."""
    sched = build_scheduler(cfg, start=False)
    assert str(sched.timezone) == cfg.timezone


def test_el_avisador_queda_enganchado(cfg):
    """Un listener que no se registra deja el sistema exactamente igual de mudo
    que no tenerlo."""
    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED

    sched = build_scheduler(cfg, start=False)
    mascaras = [m for _, m in sched._listeners]
    assert any(m & EVENT_JOB_ERROR for m in mascaras)
    assert any(m & EVENT_JOB_MISSED for m in mascaras)


# ---------------------------------------------------------------------------
# El motivo de que no haya cliente llega hasta las 09:00
# ---------------------------------------------------------------------------
#
# `api._clientes` averigua POR QUÉ no se pudo construir cada cliente, pero eso
# solo sirve si el dato recorre entero el cable hasta `run_daily`. Y el tramo
# que importa es justo este: el trabajo de las 09:00 es el que corre solo, sin
# nadie delante que pueda mirar el log.
#
# Un cable cortado aquí no da error ni cambia ningún estado. Simplemente el
# aviso del mensaje vuelve a adivinar la causa, que es indistinguible de
# acertarla salvo el día que la causa es otra.


def test_el_trabajo_de_las_nueve_lleva_los_motivos_de_los_clientes(cfg):
    sched = build_scheduler(
        cfg, client_errors={"hevy": "falta HEVY_API_KEY"}, start=False
    )
    trabajo = sched.get_job("decision_fallback")
    assert trabajo.kwargs.get("client_errors") == {"hevy": "falta HEVY_API_KEY"}


def test_job_decision_le_pasa_los_motivos_a_run_daily(cfg, monkeypatch):
    """El último tramo: recibirlos y no reenviarlos es igual de mudo."""
    visto: dict = {}

    def falso_run_daily(*a, **kw):
        visto.update(kw)
        return None

    monkeypatch.setattr("app.scheduler.run_daily", falso_run_daily)
    monkeypatch.setattr(
        "app.scheduler._fetch_garmin", lambda cfg_, day: ([], [])
    )

    job_decision(
        cfg, day=LUNES, client_errors={"telegram": "falta TELEGRAM_CHAT_ID"},
        solo_si_falta_checkin=False,
    )
    assert visto.get("client_errors") == {"telegram": "falta TELEGRAM_CHAT_ID"}


# ---------------------------------------------------------------------------
# Cuántos días de wellness se le piden a Garmin
# ---------------------------------------------------------------------------


def test_la_ventana_de_wellness_sale_del_config_y_no_de_un_7_a_pelo(cfg_copia):
    """Estaba escrito `7` en `_fetch_garmin` y 7 en `baseline.window_days`.

    Que coincidieran era el estado inicial, no una relación. Subir
    `window_days` a 14 no movía la petición: la línea base de HRV se quedaba
    por debajo de `min_days_required` para siempre y el sistema informaba de
    que faltaban datos que estaban en Garmin sin pedir. Ese fallo no se
    diagnostica desde el síntoma, porque el síntoma acusa a Garmin.
    """
    from app.scheduler import dias_de_wellness

    cfg_copia.raw["baseline"]["window_days"] = 14
    assert dias_de_wellness(cfg_copia) == 15


def test_se_pide_un_dia_mas_que_la_ventana(cfg):
    """`_baseline_for` cuenta desde `day - 1`, así que con `window_days` justos
    entraban `window_days - 1` días y la media se calculaba sobre uno menos de
    los que dice el config."""
    from app.scheduler import dias_de_wellness

    assert dias_de_wellness(cfg) == int(cfg.raw["baseline"]["window_days"]) + 1


def test_la_decision_lee_la_cache_antes_de_decidir_cuanto_pedir(monkeypatch, cfg):
    """El orden importa. `_fetch_garmin` cargaba la caché DESPUÉS de llamar a
    Garmin, así que la petición no podía tenerla en cuenta y siempre pedía el
    histórico entero. Ahora la caché decide la ventana: con una sana basta la
    corta, porque el resto ya está en disco y se fusiona después."""
    import app.scheduler as mod

    visto = {}

    @doble_de(GarminClient)
    class ClienteFalso:
        def connect(self):
            pass

        def window(self, day, days=7, ride_days=None):
            visto.update(days=days, ride_days=ride_days)
            return [], []

    monkeypatch.setattr(
        "app.integrations.activity_cache.load_cached_rides",
        lambda p: cache_sana(LUNES),
    )
    monkeypatch.setattr(
        "app.integrations.garmin.build_client", lambda s, c=None: ClienteFalso()
    )
    mod._fetch_garmin(cfg, LUNES)
    assert visto["ride_days"] == cfg.raw["cycling"]["fetch"]["lookback_days"]


def test_sin_cache_la_decision_se_trae_el_historico_entero(monkeypatch, cfg):
    """Aquí la petición es la ÚNICA fuente: si se queda corta, los percentiles
    de carga no tienen base y las reglas que los usan no se evalúan."""
    import app.scheduler as mod
    from app.integrations.activity_cache import CachedActivities

    visto = {}

    @doble_de(GarminClient)
    class ClienteFalso:
        def connect(self):
            pass

        def window(self, day, days=7, ride_days=None):
            visto.update(ride_days=ride_days)
            return [], []

    monkeypatch.setattr(
        "app.integrations.activity_cache.load_cached_rides",
        lambda p: CachedActivities(error="no existe"),
    )
    monkeypatch.setattr(
        "app.integrations.garmin.build_client", lambda s, c=None: ClienteFalso()
    )
    mod._fetch_garmin(cfg, LUNES)
    assert visto["ride_days"] == cfg.raw["cycling"]["fetch"]["backfill_days"]


# ---------------------------------------------------------------------------
# La caché de previsualizar: para qué es, y sobre todo para qué NO
# ---------------------------------------------------------------------------
#
# Previsualizar es mirar qué decidiría el sistema con lo que hay escrito en el
# formulario, sin escribir en Hevy, sin Telegram y sin guardar decisión. Eso
# invita a hacerlo varias veces seguidas -es justo lo que se pide: cambiar una
# respuesta y volver a mirar-, y cada pasada era un `client.connect()` contra
# Garmin. Garmin limita por IP y ya ha limitado a este usuario una vez.
#
# La caché es, por tanto, para previsualizar Y PARA NADA MÁS. Los tests de aquí
# abajo pinchan las dos mitades de esa frase, y la segunda es la que importa.


@pytest.fixture
def sin_previsualizaciones(monkeypatch):
    """Cada test arranca con la caché vacía y no se la deja al siguiente.

    Se sustituye el diccionario entero en vez de vaciarlo porque `monkeypatch`
    lo devuelve solo al terminar. Una caché con estado que sobreviviera entre
    tests haría que el orden de ejecución decidiera el resultado, que es el
    fallo más caro de diagnosticar que existe.
    """
    import app.scheduler as mod

    monkeypatch.setattr(mod, "_previsualizaciones", {})
    return mod


def _garmin_contado(monkeypatch, mod):
    """Sustituye la lectura real y devuelve la lista de días que se pidieron."""
    llamadas: list = []

    def falso(cfg_, day):
        llamadas.append(day)
        return [f"wellness de {day}"], [f"salidas de {day}"]

    monkeypatch.setattr(mod, "_fetch_garmin", falso)
    return llamadas


def test_previsualizar_dos_veces_el_mismo_dia_es_un_solo_login(
    monkeypatch, cfg, sin_previsualizaciones
):
    """Cinco previsualizaciones eran cinco logins, y de ahí salió el 429.

    Calibrar es previsualizar, cambiar una respuesta y volver a previsualizar.
    Lo que cambia entre una pasada y la siguiente es el formulario; el wellness
    del día es el mismo -el reloj ya sincronizó o todavía no-, así que la
    segunda lectura no aporta nada y sí gasta cupo.
    """
    mod = sin_previsualizaciones
    llamadas = _garmin_contado(monkeypatch, mod)

    primera = mod.garmin_para_previsualizar(cfg, LUNES, ahora=100.0)
    segunda = mod.garmin_para_previsualizar(cfg, LUNES, ahora=120.0)

    assert llamadas == [LUNES]
    assert primera == segunda


def test_pasado_el_plazo_la_previsualizacion_vuelve_a_preguntar(
    monkeypatch, cfg, sin_previsualizaciones
):
    """El plazo no es una optimización más: es el compromiso entero.

    Garmin corrige hacia atrás -el sueño de esta noche cambia cuando el reloj
    termina de sincronizar-, así que una lectura guardada envejece de verdad.
    El plazo dura lo que dura una tanda de calibración; pasada esa, se vuelve a
    mirar aunque sea el mismo día.
    """
    mod = sin_previsualizaciones
    llamadas = _garmin_contado(monkeypatch, mod)

    mod.garmin_para_previsualizar(cfg, LUNES, ahora=100.0)
    mod.garmin_para_previsualizar(
        cfg, LUNES, ahora=100.0 + mod.TTL_PREVISUALIZAR_S + 1
    )

    assert llamadas == [LUNES, LUNES]


def test_la_cache_de_previsualizar_no_sirve_un_dia_por_otro(
    monkeypatch, cfg, sin_previsualizaciones
):
    """Servir el wellness de ayer como el de hoy es el mismo fallo que el
    service worker tiene prohibido: una mentira que no se distingue de la
    verdad, porque la pantalla sale igual de completa."""
    mod = sin_previsualizaciones
    llamadas = _garmin_contado(monkeypatch, mod)
    martes = LUNES + timedelta(days=1)

    lunes = mod.garmin_para_previsualizar(cfg, LUNES, ahora=100.0)
    otro = mod.garmin_para_previsualizar(cfg, martes, ahora=101.0)

    assert llamadas == [LUNES, martes]
    assert lunes != otro
    assert otro[0] == [f"wellness de {martes}"]


def test_la_decision_de_verdad_no_se_sirve_de_la_cache_de_previsualizar(
    monkeypatch, cfg, sin_previsualizaciones
):
    """LA GUARDA QUE IMPORTA, Y LA QUE ALGUIEN VA A QUERER QUITAR.

    La caché vive por ENCIMA de `_fetch_garmin`, no dentro. Bajarla un nivel
    parece la simplificación evidente -una sola caché para todos- y rompe dos
    cosas a la vez, ninguna de las cuales se ve desde la pantalla:

    - Previsualizar a las 06:50, antes de que el reloj sincronice, y enviar a
      las 06:55, después, decidiría de verdad con la lectura de las 06:50. El
      usuario habría visto un semáforo, aceptado, y el sistema habría escrito
      la rutina con un sueño que ya no era el de hoy.
    - Los trabajos de las 07:00, 09:00 y 09:40 existen precisamente para
      recoger lo que Garmin corrige hacia atrás. Servirles una copia haría que
      recalcular no recalculara nada, y el síntoma sería «sale lo mismo»,
      que se parece demasiado a «no había nada que corregir».

    Por eso se cuentan los `connect()` de verdad y no las llamadas a
    `_fetch_garmin`: lo que no debe repetirse es el login, y lo que no debe
    ahorrarse es el de la decisión.
    """
    mod = sin_previsualizaciones
    conexiones = []

    @doble_de(GarminClient)
    class ClienteFalso:
        def connect(self):
            conexiones.append(1)

        def window(self, day, days=7, ride_days=None):
            return [], []

    monkeypatch.setattr(
        "app.integrations.activity_cache.load_cached_rides",
        lambda p: cache_sana(LUNES),
    )
    monkeypatch.setattr(
        "app.integrations.garmin.build_client", lambda s, c=None: ClienteFalso()
    )

    mod.garmin_para_previsualizar(cfg, LUNES, ahora=100.0)
    assert len(conexiones) == 1

    # La decisión de verdad, con la caché de previsualizar recién calentada.
    mod._fetch_garmin(cfg, LUNES)
    assert len(conexiones) == 2


def test_un_garmin_que_revienta_no_se_queda_guardado(
    monkeypatch, cfg, sin_previsualizaciones
):
    """Guardar el fallo daría diez minutos de pantalla rota sin poder hacer
    nada. Y guardarlo como lectura vacía, que es la versión que sale sola si
    uno no piensa en ello, sería peor: la previsualización decidiría sin datos
    y lo enseñaría con la misma cara que una decisión con datos."""
    mod = sin_previsualizaciones
    llamadas: list = []

    def revienta(cfg_, day):
        llamadas.append(day)
        raise RuntimeError("garmin dice que no")

    monkeypatch.setattr(mod, "_fetch_garmin", revienta)

    for momento in (100.0, 101.0):
        with pytest.raises(RuntimeError):
            mod.garmin_para_previsualizar(cfg, LUNES, ahora=momento)

    assert llamadas == [LUNES, LUNES]
    assert mod._previsualizaciones == {}


def test_la_cache_de_previsualizar_no_crece_sin_freno(
    monkeypatch, cfg, sin_previsualizaciones
):
    """Este proceso vive meses seguidos dentro del Umbrel sin reiniciarse.

    Una entrada por día que no se borra nunca retiene el histórico de salidas
    entero por cada día que el usuario previsualizó alguna vez, y ninguna de
    esas entradas puede volver a servir a nadie: pasado el plazo están muertas
    por definición. Se barren al entrar, que es el único momento en el que hay
    alguien mirando.
    """
    mod = sin_previsualizaciones
    _garmin_contado(monkeypatch, mod)

    for i in range(30):
        mod.garmin_para_previsualizar(
            cfg, LUNES + timedelta(days=i),
            ahora=100.0 + i * (mod.TTL_PREVISUALIZAR_S + 1),
        )

    assert len(mod._previsualizaciones) == 1
