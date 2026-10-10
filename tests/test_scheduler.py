"""Los trabajos automáticos y, sobre todo, cómo fallan.

Este módulo es el único del sistema que se ejecuta sin nadie delante. Eso hace
que lo que hay que probar no sean los caminos felices sino los silencios:

- que ninguno decida el día, escriba en Hevy o mande el plan sin el formulario
  (10/10/2026: «solo quiero que actúe y envíe el mensaje de Telegram si yo
  relleno el formulario»);
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
# Nada decide el día sin el formulario (10/10/2026)
# ---------------------------------------------------------------------------
#
# Hasta ese día, un trabajo de las 09:00 decidía sin check-in, escribía en Hevy
# y mandaba el Telegram; otro de las 07:30 rehacía la decisión cuando el reloj
# subía la noche tarde; y abrir la PWA disparaba ese mismo recálculo. Ahora el
# plan sale solo del envío del formulario, y abrir la app solo avisa.


def sin_wellness(cfg, day):
    """Lo que contesta Garmin cuando el reloj no ha sincronizado la noche."""
    return dias(day, 10), []


def _manana_a_ciegas(db, cfg, day=LUNES):
    """Un envío del formulario decidido antes de que el reloj subiera la noche."""
    from app.runner import run_daily

    metrics, rides = sin_wellness(cfg, day)
    repo.upsert_checkin(db, day, {"fatigue": 2, "lower_discomfort": 1}, config=cfg)
    run_daily(db, cfg, day, metrics=metrics, rides=rides, source="checkin",
              hevy_client=HevyFalso(), telegram_client=TelegramFalso())
    db.commit()
    previa = repo.current_decision(db, day)
    assert previa is not None and previa.source == "checkin"
    return previa


def _entrenado(db, day=LUNES):
    from app.models import WorkoutLog

    db.add(WorkoutLog(hevy_workout_id="hecho", date=day, routine_key="dia_1", title="x"))
    db.commit()


def _decisiones(db, day=LUNES):
    return db.scalars(select(DecisionRow).where(DecisionRow.date == day)).all()


def _recalcular(cfg):
    from app.scheduler import recalcular_si_hace_falta

    return recalcular_si_hace_falta(cfg, day=LUNES)


def test_ningun_trabajo_decide_el_dia(cfg):
    """El 30/09 y los cinco días siguientes el de las 09:00 decidió y reescribió
    Hevy sin que nadie hubiera contestado nada. Que no vuelvan, ni con otro
    nombre: ningún trabajo con hora puede llamar al motor."""
    from app.runner import run_daily

    sched = build_scheduler(cfg, start=False)
    ids = {j.id for j in sched.get_jobs()}
    assert not ids & {"decision_fallback", "recompute_early"}
    assert all(j.func is not run_daily for j in sched.get_jobs())


def test_al_abrir_sin_checkin_no_hay_nada_que_decir(en_memoria, cfg):
    assert _recalcular(cfg) == {"estado": "sin_checkin", "aviso": None}


def test_al_abrir_con_la_decision_completa_no_hay_aviso(en_memoria, cfg):
    from app.runner import run_daily

    repo.upsert_checkin(en_memoria, LUNES, {"fatigue": 3, "lower_discomfort": 1}, config=cfg)
    metrics, rides = fetch_falso(cfg, LUNES)
    run_daily(en_memoria, cfg, LUNES, metrics=metrics, rides=rides, source="checkin",
              hevy_client=HevyFalso(), telegram_client=TelegramFalso())
    en_memoria.commit()

    assert _recalcular(cfg) == {"estado": "nada_que_recalcular", "aviso": None}


def test_al_abrir_un_dia_decidido_a_ciegas_se_avisa_y_no_se_rehace(en_memoria, cfg):
    """Hasta el 10/10/2026 esto escribía en Hevy y mandaba otro Telegram. Ahora
    lo dice, y volver a enviar es cosa del usuario."""
    previa = _manana_a_ciegas(en_memoria, cfg)
    antes = len(_decisiones(en_memoria))

    salida = _recalcular(cfg)

    assert salida["estado"] == "decidido_sin_dato"
    assert salida["tono"] == "ojo"
    assert "vuelve a enviar el formulario" in salida["aviso"]
    assert "ya no rehace el día" in salida["aviso"]
    assert len(_decisiones(en_memoria)) == antes, "ha decidido otra vez sin el formulario"
    en_memoria.refresh(previa)
    assert previa.is_current is True


def test_al_abrir_despues_de_entrenar_se_dice_sin_mas(en_memoria, cfg):
    _manana_a_ciegas(en_memoria, cfg)
    _entrenado(en_memoria)

    salida = _recalcular(cfg)

    assert salida["estado"] == "ya_entrenado"
    assert salida["tono"] == "tenue"


def test_lo_que_falta_y_no_es_del_reloj_no_da_aviso(en_memoria, cfg):
    """Un deslizador sin contestar no lo arregla esperar al reloj: no se avisa."""
    from app.runner import run_daily

    repo.upsert_checkin(en_memoria, LUNES, {"fatigue": 3}, config=cfg)
    metrics, rides = fetch_falso(cfg, LUNES)
    run_daily(en_memoria, cfg, LUNES, metrics=metrics, rides=rides, source="checkin",
              hevy_client=HevyFalso(), telegram_client=TelegramFalso())
    en_memoria.commit()

    assert _recalcular(cfg)["aviso"] is None


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
    return SimpleNamespace(job_id="garmin_fetch", exception=exc)


def evento_perdido():
    return SimpleNamespace(
        job_id="garmin_fetch",
        exception=None,
        scheduled_run_time=datetime(2026, 9, 7, 9, 0),
    )


def test_un_trabajo_que_revienta_se_cuenta():
    tg = TelegramFalso()
    _avisador(tg)(evento_error())

    assert len(tg.enviados) == 1
    texto = tg.enviados[0]
    assert "garmin_fetch" in texto
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


def test_estan_los_cuatro_trabajos_del_dia_y_los_tres_del_arranque(cfg):
    """Cuatro con hora y TRES que se disparan al arrancar.

    Eran seis con hora hasta el 10/10/2026: se fueron `decision_fallback` y
    `recompute_early`, los dos que decidían el día sin el formulario.

    Los tres del arranque hacen cosas distintas y por eso son tres: uno
    recupera de Garmin los días de bienestar que falten, otro lee de Hevy lo
    entrenado que quedó sin apuntar, y `startup_audit` mira qué trabajos
    DEBIERON correr mientras el sistema no estaba. Ver
    `tests/test_arranque_perdido.py` para por qué el aviso de APScheduler no
    cubre ese caso.

    `reconcile_arranque` es el más nuevo y el que más falta hacía: la
    reconciliación de las 22:30 no se ejecuta porque el equipo está apagado, y
    la que se cuela antes de decidir solo ocurre si se envía el check-in. Sin
    ésta, dos mañanas sin enviar dejan Hevy sin leer.

    `watchdog` no HACE nada por su cuenta y por eso se confunde con un olvido
    si no se nombra: solo mira -ver `tests/test_vigilancia.py`-.
    """
    sched = build_scheduler(cfg, start=False)
    assert {j.id for j in sched.get_jobs()} == {
        "garmin_fetch", "reconcile", "perception_notice", "watchdog",
        "backfill_wellness", "reconcile_arranque", "startup_audit",
    }




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
        "watchdog_time": "10:45",
        "evening_summary_time": "23:05",
    }
    sched = build_scheduler(cfg_copia, start=False)
    horas = {j.id: str(j.trigger) for j in sched.get_jobs()}

    assert "hour='5'" in horas["garmin_fetch"]
    assert "minute='15'" in horas["garmin_fetch"]
    assert "hour='10'" in horas["watchdog"]
    assert "minute='45'" in horas["watchdog"]
    assert "hour='23'" in horas["reconcile"]


def test_sin_horas_en_el_config_se_usan_las_de_siempre(cfg_copia):
    cfg_copia.raw.pop("schedule", None)
    assert _hora(cfg_copia, "watchdog_time", "09:45") == (9, 45)
    assert _hora(cfg_copia, "garmin_fetch_time", "06:30") == (6, 30)


def test_una_hora_vacia_en_el_config_cae_al_defecto(cfg_copia):
    cfg_copia.raw["schedule"] = {"watchdog_time": None}
    assert _hora(cfg_copia, "watchdog_time", "09:45") == (9, 45)


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
#
# `api._clientes` averigua POR QUÉ no se pudo construir cada cliente, pero eso
# solo sirve si el dato recorre entero el cable hasta `run_daily`. Y el tramo
# que importa es justo este: el trabajo de las 09:00 es el que corre solo, sin
# nadie delante que pueda mirar el log.
#
# Un cable cortado aquí no da error ni cambia ningún estado. Simplemente el
# aviso del mensaje vuelve a adivinar la causa, que es indistinguible de
# acertarla salvo el día que la causa es otra.






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
    """`baseline_for` cuenta desde `day - 1`, así que con `window_days` justos
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
