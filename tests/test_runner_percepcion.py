"""El aviso de la mañana siguiente, y sobre todo cuándo NO se marca como dado.

Este trabajo tiene una particularidad que lo separa de todo lo demás del
sistema: su equivocación grave es silenciosa Y es irreversible. Si marca una
disociación como contada sin haberla contado, esa mañana no vuelve a salir
nunca -no hay reintento, porque `reported_at` ya está puesto- y nadie se entera,
porque un aviso que no llega es indistinguible de un día en que no hubo nada que
avisar.

Así que casi todo lo que se prueba aquí es la misma frase mirada desde ángulos
distintos: **se marca DESPUÉS de que el envío haya salido bien, y solo
entonces**. Falla el envío, no se marca. No hay cliente, no se marca. Es un
ensayo, no se marca. Y en los tres casos la fila sigue pendiente para mañana.

Lo otro que se vigila es que esto no se mezcle con la decisión del día. Es un
mensaje sobre AYER; pegado al plan de hoy se convertiría en un argumento a favor
o en contra de entrenar, que es exactamente lo que se pidió que no fuera.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import Base, Notification, SessionPerformance
from app.runner import run_aviso_percepcion
from tests.test_analysis_rendimiento import HOY, contraria, disociada, juicio
from tests.test_runner import TelegramFalso


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def pendientes(db) -> list[SessionPerformance]:
    return list(
        db.scalars(
            select(SessionPerformance).where(SessionPerformance.reported_at.is_(None))
        )
    )


def avisos(db) -> list[Notification]:
    return list(
        db.scalars(select(Notification).where(Notification.kind == "perception"))
    )


# ---------------------------------------------------------------------------
# El orden que no es negociable
# ---------------------------------------------------------------------------


def test_se_manda_y_solo_entonces_se_marca(db, cfg):
    """El camino normal, para tener contra qué comparar los tres que siguen."""
    disociada(db, HOY - timedelta(days=1), clave="a")

    tg = TelegramFalso()
    res = run_aviso_percepcion(db, cfg, HOY, telegram_client=tg)

    assert res.pendientes == 1
    assert res.marcadas == 1
    assert res.status == "sent"
    assert len(tg.enviados) == 1
    assert pendientes(db) == []


def test_si_el_envio_revienta_la_fila_sigue_pendiente(db, cfg):
    """Marcar y luego enviar borraría el aviso sin haberlo dado.

    Es el orden al revés, y es irreversible: `reported_at` puesto significa "ya
    se conto", así que mañana `pendientes_de_avisar` no la devolvería y ese día
    no saldría nunca más. Un fallo de red se habría comido el dato en silencio.
    """
    disociada(db, HOY - timedelta(days=1), clave="a")

    tg = TelegramFalso(revienta=True)
    res = run_aviso_percepcion(db, cfg, HOY, telegram_client=tg)

    assert res.status == "error"
    assert res.marcadas == 0
    assert len(pendientes(db)) == 1, "un fallo de red no puede consumir el aviso"
    assert res.problemas, "un envío fallido tiene que salir en los problemas"


def test_sin_cliente_de_telegram_no_se_marca_pero_queda_registrado(db, cfg):
    """No hay a quién avisar, así que el aviso NO está dado.

    Y se escribe la fila de `notifications` igual. Sin ella, un día en el que la
    disociación existió y no se contó a nadie sería indistinguible en el
    histórico de un día tranquilo, que es la clase de hueco que hace imposible
    entender después por qué el contador no cuadra.
    """
    disociada(db, HOY - timedelta(days=1), clave="a")

    res = run_aviso_percepcion(db, cfg, HOY, telegram_client=None)

    assert res.status == "skipped"
    assert res.marcadas == 0
    assert len(pendientes(db)) == 1

    fila = avisos(db)
    assert len(fila) == 1
    assert fila[0].status == "skipped"
    assert fila[0].error, "una fila sin motivo no explica nada"
    assert res.problemas


def test_un_ensayo_no_se_come_el_aviso_de_verdad(db, cfg):
    """`DRY_RUN=true` no puede consumir nada.

    El doble de Telegram de los tests contesta `sent=True` aunque se le pida un
    ensayo -el cliente de verdad contesta `sent=False`-, así que este test
    comprueba EXACTAMENTE la salvaguarda que hay en `run_aviso_percepcion` y no
    la buena educación del cliente. Si alguien quita el `and not dry_run`
    fiándose de lo que devuelve el cliente, esto se cae.
    """
    disociada(db, HOY - timedelta(days=1), clave="a")

    tg = TelegramFalso()
    res = run_aviso_percepcion(db, cfg, HOY, telegram_client=tg, dry_run=True)

    assert res.marcadas == 0
    assert len(pendientes(db)) == 1, "un ensayo ha consumido el aviso de verdad"


def test_lo_contado_no_se_repite_al_dia_siguiente(db, cfg):
    """Sin `reported_at`, el mismo día saldría cada mañana hasta salir de la
    ventana. Un aviso que se repite se aprende a ignorar en tres días."""
    disociada(db, HOY - timedelta(days=1), clave="a")

    tg = TelegramFalso()
    run_aviso_percepcion(db, cfg, HOY, telegram_client=tg)
    res = run_aviso_percepcion(db, cfg, HOY + timedelta(days=1), telegram_client=tg)

    assert res.pendientes == 0
    assert res.motivo == "no hay ninguna disociación sin contar"
    assert len(tg.enviados) == 1, "la misma disociación se ha contado dos veces"


# ---------------------------------------------------------------------------
# Qué se cuenta y qué no
# ---------------------------------------------------------------------------


def test_la_direccion_contraria_se_guarda_pero_no_se_manda(db, cfg):
    """Se cuenta en la pantalla y no se manda al móvil.

    El mensaje existe para una mañana concreta -la de levantarse pensando que no
    se puede entrenar-. Avisar también de los días en que la mañana prometió de
    más lo convertiría en un comentario diario sobre el estado de ánimo, que no
    es lo que se pidió ni lo que hace falta.
    """
    contraria(db, HOY - timedelta(days=1), clave="a")

    tg = TelegramFalso()
    res = run_aviso_percepcion(db, cfg, HOY, telegram_client=tg)

    assert res.pendientes == 0
    assert tg.enviados == []


def test_una_sesion_alineada_no_genera_aviso(db, cfg):
    """Lo normal es que no pase nada, y entonces no se manda nada."""
    juicio(db, HOY - timedelta(days=1), clave="a")

    tg = TelegramFalso()
    res = run_aviso_percepcion(db, cfg, HOY, telegram_client=tg)

    assert res.pendientes == 0
    assert tg.enviados == []
    assert avisos(db) == [], "sin nada que contar tampoco hay notificación que guardar"


def test_el_mensaje_cita_el_acumulado_y_no_la_ventana(db, cfg):
    """Las cifras del móvil y las de la pantalla tienen que ser las mismas.

    El contador de la vista va sobre una ventana y puede BAJAR con el tiempo -una
    disociación de hace siete meses sale de la ventana- aunque ninguna fila se
    reescriba. El del mensaje va sobre todo el histórico y no baja. Si el aviso
    citara el de la ventana, dos capturas del mismo suceso con seis meses de
    diferencia darían números distintos y el contador dejaría de servir.

    Aquí hay ocho sesiones repartidas por dos años; una ventana de 180 días solo
    vería las recientes.
    """
    from app.analysis.rendimiento import contador_historico

    for i in range(3):
        disociada(db, HOY - timedelta(days=400 + i * 10), clave=f"vieja{i}")
    for i in range(4):
        juicio(db, HOY - timedelta(days=420 + i * 10), clave=f"alineada{i}")
    disociada(db, HOY - timedelta(days=1), clave="nueva")

    tg = TelegramFalso()
    run_aviso_percepcion(db, cfg, HOY, telegram_client=tg)

    acumulado = contador_historico(db, hasta=HOY)
    assert acumulado["veces"] == 4
    assert acumulado["de"] == 8
    assert "4 de 8" in tg.enviados[0], tg.enviados[0]


def test_varias_pendientes_salen_en_un_solo_mensaje(db, cfg):
    """Dos avisos sueltos el mismo minuto son dos notificaciones seguidas.

    Se juntan en un envío porque llegan a la vez y se leen a la vez; y porque
    marcar una sí y otra no según cuál de los dos POST fallara dejaría la mitad
    del suceso contada.
    """
    disociada(db, HOY - timedelta(days=1), clave="a")
    disociada(db, HOY - timedelta(days=2), clave="b")

    tg = TelegramFalso()
    res = run_aviso_percepcion(db, cfg, HOY, telegram_client=tg)

    assert res.pendientes == 2
    assert res.marcadas == 2
    assert len(tg.enviados) == 1
    assert pendientes(db) == []


# ---------------------------------------------------------------------------
# Que no se mezcle con la decisión del día
# ---------------------------------------------------------------------------


def test_el_aviso_es_su_propia_notificacion_y_no_la_de_la_decision(db, cfg):
    """Va en un envío propio, con su `kind`.

    Si se colgara del mensaje de la decisión pasarían dos cosas malas: el
    contador se leería como parte del plan de hoy -un argumento a favor o en
    contra de entrenar, que es justo lo que no puede ser- y, como la decisión se
    manda por dos caminos distintos (el check-in de la PWA y el fallback de las
    nueve), el aviso se duplicaría o se perdería según a qué hora se rellenara el
    formulario.
    """
    disociada(db, HOY - timedelta(days=1), clave="a")

    tg = TelegramFalso()
    run_aviso_percepcion(db, cfg, HOY, telegram_client=tg)

    fila = avisos(db)
    assert len(fila) == 1
    assert fila[0].kind == "perception", "no puede ir como 'decision'"
    assert fila[0].date == HOY
    assert fila[0].body == tg.enviados[0]


def test_el_mensaje_no_propone_nada_sobre_hoy(db, cfg):
    """Esta vista es un espejo, no un consejo.

    La sesión de hoy la decide el motor con sus reglas. Un aviso que además
    dijera qué hacer estaría opinando sobre la decisión desde fuera del motor, y
    convertiría el contador en un argumento.
    """
    disociada(db, HOY - timedelta(days=1), clave="a")

    tg = TelegramFalso()
    run_aviso_percepcion(db, cfg, HOY, telegram_client=tg)

    texto = tg.enviados[0].lower()
    for palabra in ("hoy", "deberías", "deberias", "descansa", "entrena", "ánimo"):
        assert palabra not in texto, f"el aviso opina sobre hoy: {palabra!r}"


# ---------------------------------------------------------------------------
# Evaluar y avisar van juntos, pero no dependen el uno del otro
# ---------------------------------------------------------------------------


def test_se_evalua_aunque_no_haya_a_quien_avisar(db, cfg):
    """La tabla es el histórico que sostiene la vista entera.

    Dejar de escribirla porque hoy no hay Telegram configurado ataría el registro
    a que funcione el mensajero: se perderían para siempre las sesiones de los
    días sin bot, y el denominador del contador dejaría de ser el total de
    sesiones sin que nada lo dijera.
    """
    from app.analysis.rendimiento import evaluar_pendientes

    llamadas: list[date] = []
    real = evaluar_pendientes

    import app.analysis.rendimiento as mod

    def espia(session, cfg_, *, hasta, **kw):
        llamadas.append(hasta)
        return real(session, cfg_, hasta=hasta, **kw)

    mod.evaluar_pendientes = espia
    try:
        run_aviso_percepcion(db, cfg, HOY, telegram_client=None)
    finally:
        mod.evaluar_pendientes = real

    assert llamadas == [HOY], "sin cliente de Telegram no se ha evaluado nada"
