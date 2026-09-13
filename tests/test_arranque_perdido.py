"""El hueco que deja un contenedor apagado, y por qué nadie lo veía.

EL FALLO QUE ESTE MÓDULO VIGILA
-------------------------------
El sistema ya avisaba de un trabajo perdido: `_avisador` escucha
`EVENT_JOB_MISSED` y manda un Telegram. El domingo 13 se perdieron cuatro
trabajos y salieron sus cuatro mensajes, comprobado en el log del contenedor.

Pero ese aviso necesita un PROCESO VIVO al que se le pase la hora. Ese día la
máquina virtual estuvo suspendida: el proceso siguió existiendo, congelado, y al
despertar APScheduler miró el reloj, vio las horas pasadas y protestó.

Cuando el contenedor se para de verdad -`docker stop`, un reinicio del
anfitrión, una actualización, un cuelgue- el planificador muere. Al volver se
construye otro con el almacén de trabajos EN MEMORIA, que nace sin pasado: para
un `CronTrigger` recién creado, las 09:00 de esta mañana no son una cita
perdida, son que la próxima cita es mañana. No salta ningún evento y no se manda
ningún mensaje.

O sea: el aviso cubría el caso en que la máquina se DUERME, y no cubría el caso
en que se APAGA, que es el más probable de los dos. Y el modo de fallo era el
peor de todos, el que este proyecto persigue: una mañana sin decisión es, desde
el sofá, idéntica a una mañana de descanso.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, JobRun
from app.scheduler import (
    TOPE_PERDIDOS,
    _apuntador,
    auditar_arranque,
    disparos_perdidos,
    mensaje_de_arranque,
    suelo_de_vigilancia,
)
from tests.test_runner import TelegramFalso

TZ = ZoneInfo("Europe/Madrid")


def local(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=TZ)


def utc_naive(momento):
    return momento.astimezone(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def en_memoria(db, monkeypatch):
    import app.scheduler as mod

    @contextmanager
    def scope():
        yield db

    monkeypatch.setattr(mod, "session_scope", scope)
    return db


def a_las_nueve():
    return CronTrigger(hour=9, minute=0, timezone=TZ)


# ---------------------------------------------------------------------------
# Contar los disparos de un intervalo ya pasado
# ---------------------------------------------------------------------------


def test_una_noche_entera_parado_son_los_disparos_de_esos_dias():
    """Tres días apagado, un trabajo diario: tres disparos perdidos."""
    perdidos, hay_mas = disparos_perdidos(
        a_las_nueve(), local(2026, 9, 10, 12, 0), local(2026, 9, 13, 12, 0)
    )
    assert [p.astimezone(TZ).date().day for p in perdidos] == [11, 12, 13]
    assert hay_mas is False


def test_un_reinicio_rapido_no_acusa_de_nada():
    """Parar y arrancar a la misma hora no pierde ningún disparo.

    Es el caso NORMAL -un `docker compose up -d --build`- y tiene que salir
    limpio. Un aviso que salta en cada despliegue es un aviso que se ignora.
    """
    perdidos, hay_mas = disparos_perdidos(
        a_las_nueve(), local(2026, 9, 13, 15, 0), local(2026, 9, 13, 15, 2)
    )
    assert perdidos == []
    assert hay_mas is False


def test_el_disparo_que_cae_justo_en_el_suelo_no_se_vuelve_a_contar():
    """El extremo de abajo es ABIERTO, y esto no es quisquillosidad.

    `desde` es la última vez que el trabajo TERMINÓ BIEN. Si el intervalo lo
    incluyera, la auditoría del siguiente arranque acusaría de haber perdido
    justo la ejecución que sí se hizo, y lo haría cada vez.
    """
    perdidos, _ = disparos_perdidos(
        a_las_nueve(), local(2026, 9, 13, 9, 0), local(2026, 9, 13, 20, 0)
    )
    assert perdidos == []


def test_el_disparo_que_cae_justo_en_el_techo_si_cuenta():
    """El extremo de arriba es CERRADO: `hasta` es ahora, y las 09:00 de hoy
    ya pasaron si son las 09:00 clavadas."""
    perdidos, _ = disparos_perdidos(
        a_las_nueve(), local(2026, 9, 12, 20, 0), local(2026, 9, 13, 9, 0)
    )
    assert len(perdidos) == 1
    assert perdidos[0].astimezone(TZ) == local(2026, 9, 13, 9, 0)


def test_tres_meses_parado_corta_por_el_tope_y_lo_dice():
    """Con el sistema parado una temporada, la lista se corta.

    Lo que NO puede pasar es que se corte callando: un `20` a secas donde había
    noventa es un número inventado, y este proyecto ya ha pagado ese error otras
    veces. Por eso `disparos_perdidos` devuelve el booleano.
    """
    perdidos, hay_mas = disparos_perdidos(
        a_las_nueve(), local(2026, 6, 1), local(2026, 9, 13)
    )
    assert len(perdidos) == TOPE_PERDIDOS
    assert hay_mas is True


# ---------------------------------------------------------------------------
# El suelo desde el que se cuenta
# ---------------------------------------------------------------------------


def test_sin_fila_el_suelo_es_el_arranque_y_no_acusa_a_nadie():
    """El primer arranque de la vida del sistema no puede culpar a nadie.

    No se inventa un pasado limpio: es que de verdad no hay pasado que juzgar.
    Y decir lo contrario -listar como perdidos los disparos de antes de que la
    tabla existiera- sería el mismo fallo, con el signo cambiado.
    """
    arranque = datetime(2026, 9, 13, 15, 0)
    assert suelo_de_vigilancia(None, arranque) == arranque


def test_el_suelo_es_la_marca_MAS_RECIENTE_de_las_tres():
    """Cada marca contesta una pregunta distinta; vale la mayor.

    Si valiera `last_finished_at` a secas, un trabajo que corrió el lunes y del
    que la auditoría del martes ya avisó volvería a salir el miércoles: el
    `checked_through` del martes es lo único que sabe que ese hueco ya se contó.
    """
    fila = JobRun(
        job_id="x",
        first_seen_at=datetime(2026, 9, 1),
        last_finished_at=datetime(2026, 9, 10),
        checked_through=datetime(2026, 9, 12),
    )
    assert suelo_de_vigilancia(fila, datetime(2026, 9, 13)) == datetime(2026, 9, 12)

    fila.checked_through = None
    assert suelo_de_vigilancia(fila, datetime(2026, 9, 13)) == datetime(2026, 9, 10)


# ---------------------------------------------------------------------------
# La auditoría entera, con base de datos
# ---------------------------------------------------------------------------


def test_el_primer_arranque_no_avisa_pero_deja_el_suelo_puesto(en_memoria):
    """Arranque uno: nada que reprochar, pero la vigilancia queda armada."""
    tg = TelegramFalso()
    ahora = local(2026, 9, 13, 15, 0)

    hallado = auditar_arranque(
        {"decision_fallback": a_las_nueve()}, TZ, telegram_client=tg, ahora=ahora
    )

    assert hallado == []
    assert tg.enviados == []
    fila = en_memoria.get(JobRun, "decision_fallback")
    assert fila is not None
    assert fila.first_seen_at == utc_naive(ahora)
    assert fila.checked_through == utc_naive(ahora)


def test_apagado_tres_dias_avisa_por_telegram(en_memoria):
    """EL CASO QUE MOTIVA TODO ESTO.

    El trabajo terminó bien el día 10. El contenedor se paró. Al volver el 13,
    APScheduler no sabe nada de los tres disparos que se perdieron, porque su
    almacén de trabajos vive en memoria y nació hace un segundo. La auditoría
    tiene que encontrarlos y tiene que DECIRLO.
    """
    en_memoria.add(JobRun(
        job_id="decision_fallback",
        first_seen_at=utc_naive(local(2026, 9, 1)),
        last_finished_at=utc_naive(local(2026, 9, 10, 9, 0)),
    ))
    en_memoria.commit()

    tg = TelegramFalso()
    hallado = auditar_arranque(
        {"decision_fallback": a_las_nueve()}, TZ,
        telegram_client=tg, ahora=local(2026, 9, 13, 15, 0),
    )

    assert len(hallado) == 1
    job_id, perdidos, hay_mas = hallado[0]
    assert job_id == "decision_fallback"
    assert len(perdidos) == 3
    assert hay_mas is False

    assert len(tg.enviados) == 1
    texto = tg.enviados[0]
    # En el idioma del usuario, no en el de APScheduler.
    assert "decidir el semáforo del día" in texto
    assert "decision_fallback" not in texto
    assert "11/09 a las 09:00" in texto


def test_el_segundo_arranque_no_repite_el_mismo_aviso(en_memoria):
    """Un aviso que se repite enseña a no leer los avisos.

    Reiniciar dos veces seguidas después de una parada larga tiene que avisar
    UNA vez. Es lo que hace `checked_through`, y sin él el usuario recibiría el
    mismo mensaje en cada `docker compose up`.
    """
    en_memoria.add(JobRun(
        job_id="decision_fallback",
        first_seen_at=utc_naive(local(2026, 9, 1)),
        last_finished_at=utc_naive(local(2026, 9, 10, 9, 0)),
    ))
    en_memoria.commit()

    tg = TelegramFalso()
    vigilados = {"decision_fallback": a_las_nueve()}

    primero = auditar_arranque(
        vigilados, TZ, telegram_client=tg, ahora=local(2026, 9, 13, 15, 0)
    )
    segundo = auditar_arranque(
        vigilados, TZ, telegram_client=tg, ahora=local(2026, 9, 13, 15, 5)
    )

    assert len(primero) == 1
    assert segundo == []
    assert len(tg.enviados) == 1


def test_avisa_de_cada_trabajo_por_separado(en_memoria):
    """Cuatro trabajos parados son cuatro líneas, no un «algo falló»."""
    for job_id in ("garmin_fetch", "decision_fallback", "reconcile"):
        en_memoria.add(JobRun(
            job_id=job_id,
            first_seen_at=utc_naive(local(2026, 9, 1)),
            last_finished_at=utc_naive(local(2026, 9, 11, 23, 0)),
        ))
    en_memoria.commit()

    tg = TelegramFalso()
    hallado = auditar_arranque(
        {
            "garmin_fetch": CronTrigger(hour=6, minute=30, timezone=TZ),
            "decision_fallback": a_las_nueve(),
            "reconcile": CronTrigger(hour=22, minute=30, timezone=TZ),
        },
        TZ, telegram_client=tg, ahora=local(2026, 9, 13, 15, 0),
    )

    assert {h[0] for h in hallado} == {
        "garmin_fetch", "decision_fallback", "reconcile"
    }
    texto = tg.enviados[0]
    for legible in (
        "traer los datos de Garmin",
        "decidir el semáforo del día",
        "apuntar lo que entrenaste",
    ):
        assert legible in texto


def test_un_telegram_que_revienta_no_tumba_la_auditoria(en_memoria):
    """El aviso de avería no puede ser la avería.

    Y la marca tiene que quedar puesta igual: si un fallo de Telegram dejara
    `checked_through` sin mover, el siguiente arranque volvería a intentarlo,
    que es lo correcto, pero si además dejara la transacción a medias se
    perderían las marcas de los demás trabajos.
    """
    en_memoria.add(JobRun(
        job_id="decision_fallback",
        first_seen_at=utc_naive(local(2026, 9, 1)),
        last_finished_at=utc_naive(local(2026, 9, 10, 9, 0)),
    ))
    en_memoria.commit()

    hallado = auditar_arranque(
        {"decision_fallback": a_las_nueve()}, TZ,
        telegram_client=TelegramFalso(revienta=True),
        ahora=local(2026, 9, 13, 15, 0),
    )

    assert len(hallado) == 1
    assert en_memoria.get(JobRun, "decision_fallback").checked_through is not None


def test_sin_telegram_la_auditoria_sigue_devolviendo_lo_encontrado(en_memoria):
    """Sin cliente configurado no hay mensaje, pero sí hay respuesta y log."""
    en_memoria.add(JobRun(
        job_id="reconcile",
        first_seen_at=utc_naive(local(2026, 9, 1)),
        last_finished_at=utc_naive(local(2026, 9, 10)),
    ))
    en_memoria.commit()

    hallado = auditar_arranque(
        {"reconcile": CronTrigger(hour=22, minute=30, timezone=TZ)},
        TZ, telegram_client=None, ahora=local(2026, 9, 13, 15, 0),
    )
    assert len(hallado) == 1


# ---------------------------------------------------------------------------
# El apuntador: de dónde sale el suelo
# ---------------------------------------------------------------------------


def test_el_apuntador_marca_el_trabajo_que_termina_bien(en_memoria):
    """Sin esto la auditoría no tendría suelo y acusaría en cada arranque."""
    escuchar = _apuntador()
    escuchar(type("E", (), {"job_id": "reconcile"})())

    fila = en_memoria.get(JobRun, "reconcile")
    assert fila is not None
    assert fila.last_finished_at is not None


def test_un_trabajo_que_revienta_NO_se_apunta_como_ejecutado(cfg):
    """El apuntador va colgado de EVENT_JOB_EXECUTED Y DE NADA MÁS.

    Marcar como ejecutado un trabajo que lanzó taparía el hueco que la auditoría
    del próximo arranque tendría que encontrar, y lo taparía con una afirmación
    falsa -«corrió»- en vez de con un silencio. Un trabajo que revienta no ha
    hecho su trabajo; de ese ya avisa `_avisador` por su lado.

    Este test mira la MÁSCARA REGISTRADA, no el código fuente. La primera
    versión buscaba el texto `"_apuntador(), EVENT_JOB_EXECUTED"` en el fuente
    de `build_scheduler`, y sobrevivía a la mutación que lo cambiaba por
    `EVENT_JOB_EXECUTED | EVENT_JOB_ERROR`: el texto buscado seguía estando
    dentro del texto mutado. Un test que comprueba una subcadena da por buena
    cualquier ampliación de lo que comprueba.
    """
    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
    from app.scheduler import build_scheduler

    sched = build_scheduler(cfg, start=False)
    mascaras = [
        mask for cb, mask in sched._listeners
        if getattr(cb, "__qualname__", "").startswith("_apuntador")
    ]
    assert len(mascaras) == 1, "el apuntador no está enganchado exactamente una vez"
    assert mascaras[0] == EVENT_JOB_EXECUTED
    assert not mascaras[0] & EVENT_JOB_ERROR
    assert not mascaras[0] & EVENT_JOB_MISSED


def test_los_cuatro_trabajos_con_hora_quedan_vigilados(cfg):
    """La lista de vigilados sale de donde se crean los trabajos, no de una
    segunda lista escrita a mano que se quedaría vieja.

    Si alguien añade un quinto trabajo con hora y no se vigila, su hueco sería
    invisible otra vez. Este test se rompe el día que eso pase.
    """
    from app.scheduler import build_scheduler

    sched = build_scheduler(cfg, start=False)
    con_hora = {
        j.id for j in sched.get_jobs() if isinstance(j.trigger, CronTrigger)
    }
    auditoria = [j for j in sched.get_jobs() if j.id == "startup_audit"]
    assert auditoria, "no se ha programado la auditoría de arranque"

    vigilados = auditoria[0].args[0]
    assert set(vigilados) == con_hora, (
        f"trabajos con hora sin vigilar: {con_hora - set(vigilados)}"
    )


def test_el_mensaje_dice_que_hay_mas_cuando_se_corta():
    """El `+` del «20+» no es decorativo: sin él el mensaje mentiría."""
    texto = mensaje_de_arranque(
        [("reconcile", [local(2026, 6, 1, 22, 30)] * TOPE_PERDIDOS, True)], TZ
    )
    assert f"{TOPE_PERDIDOS}+" in texto

    texto = mensaje_de_arranque([("reconcile", [local(2026, 9, 12, 22, 30)], False)], TZ)
    # Un solo disparo se dice en singular y sin "la primera": no hay primera de
    # una sola. "1 vez/veces" delata la plantilla, y este mensaje llega el día
    # en que hay que entender algo deprisa.
    assert "1 vez," in texto
    assert "veces" not in texto
    assert "la primera" not in texto
    assert "12/09 a las 22:30" in texto
