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
from app.models import Base, Decision as DecisionRow
from app.scheduler import (
    MARGEN_S,
    _avisador,
    _hora,
    build_scheduler,
    job_decision,
    job_fetch_garmin,
    job_reconcile,
)
from tests.conftest import LUNES, dias
from tests.test_runner import HevyFalso, TelegramFalso


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


def test_el_avisador_manda_telegram_cuando_la_reconciliacion_revienta():
    """La cadena entera: excepción -> listener -> Telegram.

    Probar solo que lanza no sirve de nada si el aviso no llega: el motivo de
    lanzar es justamente que llegue.
    """
    from apscheduler.events import EVENT_JOB_ERROR, JobExecutionEvent

    from app.scheduler import _avisador

    enviados = []

    class Tg:
        def send(self, texto, **kw):
            enviados.append(texto)

    _avisador(Tg())(
        JobExecutionEvent(
            EVENT_JOB_ERROR, "reconcile", None, None,
            exception=RuntimeError("sin cliente de Hevy"),
        )
    )
    assert len(enviados) == 1
    assert "reconcile" in enviados[0]


def test_reconciliar_mira_hacia_atras_y_no_solo_hoy(en_memoria, cfg):
    """Una sesión de las diez de la noche se sincroniza al día siguiente.

    Si la reconciliación mirara solo el día en curso, esa sesión llegaría tarde
    a la suya y la racha se rompería por un problema de reloj y no por una
    sesión mal hecha.
    """
    pedido = {}

    class Hevy:
        def get_workouts(self, *, since):
            pedido["since"] = since
            return []

    salida = job_reconcile(cfg, day=LUNES, hevy_client=Hevy(), dias_atras=3)

    assert pedido["since"] == LUNES - timedelta(days=3)
    assert len(salida) == 4, "se repasan los 3 días de atrás Y el de hoy"
    assert [r.day for r in salida] == [
        LUNES - timedelta(days=i) for i in (3, 2, 1, 0)
    ]


def test_reconciliar_pide_los_entrenamientos_una_sola_vez(en_memoria, cfg):
    """Cuatro días no son cuatro peticiones: Hevy limita por IP."""
    llamadas = []

    class Hevy:
        def get_workouts(self, *, since):
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


def test_estan_los_cuatro_trabajos_del_dia_y_el_del_arranque(cfg):
    sched = build_scheduler(cfg, start=False)
    assert {j.id for j in sched.get_jobs()} == {
        "garmin_fetch", "decision_fallback", "reconcile", "perception_notice",
        "backfill_wellness",
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


class ClienteWellness:
    """Garmin de mentira para el repaso del arranque."""

    fetch_errors: list[str] = []

    def __init__(self, revienta_desde: int | None = None) -> None:
        self.revienta_desde = revienta_desde
        self.pedidos = []

    def day_metrics(self, dia):
        from app.engine.signals import DayMetrics
        from app.integrations.garmin import GarminRateLimited

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

    class ClienteFalso:
        def connect(self):
            pass

        def window(self, day, days, *, ride_days):
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

    class ClienteFalso:
        def connect(self):
            pass

        def window(self, day, days, *, ride_days):
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
