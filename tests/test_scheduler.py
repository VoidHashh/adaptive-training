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


def test_reconciliar_sin_cliente_no_revienta_pero_no_inventa(en_memoria, cfg, caplog):
    """Sin Hevy no se sabe qué se entrenó. Las rachas se quedan quietas.

    Lo que NO puede pasar es que se den por hechas: detrás de la racha va la
    subida de carga.
    """
    with caplog.at_level("WARNING"):
        assert job_reconcile(cfg, day=LUNES, hevy_client=None) == []
    assert "sin cliente de Hevy" in caplog.text


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


def test_el_refresco_de_garmin_va_aparte_de_la_decision(tmp_path, monkeypatch, cfg):
    """Garmin limita por IP. Si leer y decidir fueran el mismo trabajo, un 429
    se llevaría por delante las dos cosas."""
    import app.scheduler as mod

    visto = {}

    def refresh(path, day, *, days, fetch):
        visto.update(path=path, day=day, days=days)
        return 7

    monkeypatch.setattr(
        "app.integrations.activity_cache.refresh_cache", refresh
    )
    assert job_fetch_garmin(cfg, day=LUNES, fetch=None) == 7
    assert visto["day"] == LUNES
    assert visto["days"] == mod.RIDE_HISTORY_DAYS
    assert visto["path"].name == "activities.json"


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


def test_estan_los_tres_trabajos_del_dia(cfg):
    sched = build_scheduler(cfg, start=False)
    assert {j.id for j in sched.get_jobs()} == {
        "garmin_fetch", "decision_fallback", "reconcile"
    }


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
