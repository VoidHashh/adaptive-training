"""La API HTTP.

El camino que importa es uno solo: se rellena el formulario por la mañana, se
envía, y la decisión sale con el check-in ya dentro. Lo que se vigila aquí es
que ese camino no pueda contestar "hecho" cuando no lo está:

- un deslizador mal escrito no se puede tragar en silencio, porque el sistema
  decidiría sin ese dato diciendo que el check-in está completo;
- un check-in guardado cuya decisión falla no puede contestar un 200 mudo: el
  usuario cerraría el móvil convencido de que ya está.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import repository as repo
from app.api import app, get_config, _estado_del_reloj
from app.db import get_session
from app.models import Base, WorkoutLog
from app.settings import settings
from tests.conftest import LUNES, dias
from tests.dobles import doble_de
from app.config_loader import Config


@pytest.fixture
def db():
    """Base en memoria compartida entre hilos.

    `TestClient` atiende las peticiones en un hilo distinto al del test, así que
    hacen falta las dos cosas: `StaticPool` para que ambos vean la MISMA base en
    memoria -con el pool normal cada conexión sería una base vacía nueva- y
    `check_same_thread=False` para que SQLite no se niegue. Es la misma
    combinación que usa `app/db.py` en producción, donde APScheduler y FastAPI
    también trabajan en hilos distintos.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def cliente(db, cfg, monkeypatch):
    """La app real, con la base en memoria y sin red.

    Solo se sustituyen las dos fronteras: de dónde sale la sesión y de dónde
    salen los datos de Garmin. Todo lo demás -validación, motor, persistencia-
    es el código de producción.

    Se instancia SIN `with` a propósito: el `lifespan` llama a `init_db()`, que
    crea las tablas sobre el motor real de `app/db.py`. Un test no puede tocar la
    base del usuario.

    `_clientes` se sustituye por lo mismo que devolvería sin claves: ningún
    cliente y ningún motivo.
    No es cosmético. `POST /api/checkin` fabrica los clientes por dentro, y con
    el `.env` real del usuario -claves puestas, `DRY_RUN=false`- estos tests
    sobrescribieron su rutina de verdad en Hevy y le mandaron mensajes de verdad
    por Telegram. El cerrojo de red de `conftest` lo impide ya a lo bruto; esto
    hace además que los tests prueben lo que dicen probar en vez de estrellarse
    contra el cerrojo. Quien necesite clientes, se los inyecta.
    """
    def fetch(cfg_, day):
        return dias(day, 10, hrv=60.0, rhr=50.0, sleep_min=450, sleep_score=80), []

    monkeypatch.setattr("app.scheduler._fetch_garmin", fetch)
    monkeypatch.setattr("app.api._clientes", lambda cfg_: (None, None, {}))

    app.dependency_overrides[get_session] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# El cerrojo de red
# ---------------------------------------------------------------------------


def test_sin_el_doble_de_clientes_la_ruta_choca_contra_el_cerrojo(db, cfg, monkeypatch):
    """La prueba de que el cerrojo de `conftest` sirve para algo.

    Este test reproduce a propósito el fallo que ocurrió de verdad: `_clientes`
    SIN sustituir, es decir, fabricando los clientes de Hevy y Telegram con el
    `.env` real. Con las claves puestas y `DRY_RUN=false`, eso sobrescribió la
    rutina real del usuario y le mandó mensajes reales con fechas de mentira.

    Ahora esa ruta no llega a la red: choca contra el corte de sockets. Si algún
    día alguien quita el cerrojo, este test deja de fallar por dentro y se
    convierte en el aviso de que la batería puede volver a hablar con producción.

    Se comprueba en el error y no con un `raises`: `_decidir` se traga cualquier
    excepción y la convierte en `decided: False`, que es justo por dónde se
    escapó la primera vez.

    El `skip` de abajo no es un adorno. Sin claves en el `.env`, `_clientes`
    no devuelve ningún cliente, no se intenta ninguna conexión y el test pasaría sin
    haber comprobado nada -el mismo vacío que ya mordió una vez en este
    proyecto-. Si no hay clientes que construir, aquí no hay nada que demostrar.

    `dry_run` se fija a `False` a mano, y esa línea es media prueba. Este test
    dependía en silencio de que el `.env` real -que no está en git- lo tuviera
    en `false`. El día que se puso en `true` para la prueba local, la ruta dejó
    de intentar salir a la red, el cerrojo no llegó a saltar y el test se cayó
    señalando al cerrojo, que era lo único que no fallaba. Un test que
    comprueba una red de seguridad no puede depender de un fichero que decide
    si esa red hace falta.
    """
    from app.api import _clientes, settings as api_settings
    from tests.conftest import RedProhibidaEnTests

    hevy, tg, _ = _clientes(cfg)
    if hevy is None and tg is None:
        pytest.skip("sin claves en el .env no hay clientes reales que bloquear")

    monkeypatch.setattr(api_settings, "dry_run", False)

    def fetch(cfg_, day):
        return dias(day, 10, hrv=60.0, rhr=50.0, sleep_min=450, sleep_score=80), []

    monkeypatch.setattr("app.scheduler._fetch_garmin", fetch)
    app.dependency_overrides[get_session] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    try:
        c = TestClient(app)
        cuerpo = c.post(
            "/api/checkin", json={"day": str(LUNES), "fatigue": 3}
        ).json()
    finally:
        app.dependency_overrides.clear()

    huella = str(cuerpo.get("error", "")) + str(cuerpo.get("problems", ""))
    assert RedProhibidaEnTests.__name__ in huella or "conexión de red" in huella, (
        "la petición ha salido a la red de verdad: el cerrojo de sockets de "
        f"conftest no está haciendo su trabajo. Respuesta: {cuerpo}"
    )


# ---------------------------------------------------------------------------
# Por qué no hay cliente, y no solo que no lo hay
# ---------------------------------------------------------------------------
#
# `_clientes` dejaba la excepción del constructor en un `log.warning` y devolvía
# un None pelado. Que no hubiera cliente sí se avisaba -el runner marca la
# escritura como error y lo pone arriba del mensaje-, pero el aviso tenía que
# ADIVINAR la causa. Adivinar cuando se sabe manda a mirar donde no es, y a las
# nueve de la mañana la diferencia es arreglarlo desde el móvil o entrar por
# SSH a leer un log.


def test_clientes_devuelve_el_motivo_de_cada_fallo(cfg, monkeypatch):
    monkeypatch.setattr(
        "app.integrations.hevy.build_client",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("falta HEVY_API_KEY")),
    )
    monkeypatch.setattr(
        "app.integrations.telegram.build_client",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("falta TELEGRAM_CHAT_ID")),
    )
    from app.api import _clientes

    hevy, tg, motivos = _clientes(cfg)
    assert hevy is None and tg is None
    assert "HEVY_API_KEY" in motivos["hevy"]
    assert "TELEGRAM_CHAT_ID" in motivos["telegram"]


def test_un_cliente_que_se_construye_no_deja_motivo(cfg, monkeypatch):
    """La lista de motivos no puede ser una lista de clientes.

    Si un cliente sano dejara entrada, el mensaje avisaría de una avería que no
    existe y el aviso dejaría de significar nada.
    """
    monkeypatch.setattr("app.integrations.hevy.build_client", lambda *a, **k: object())
    monkeypatch.setattr(
        "app.integrations.telegram.build_client",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("falta TELEGRAM_CHAT_ID")),
    )
    from app.api import _clientes

    _hevy, _tg, motivos = _clientes(cfg)
    assert "hevy" not in motivos
    assert set(motivos) == {"telegram"}


# ---------------------------------------------------------------------------
# Salud
# ---------------------------------------------------------------------------


@pytest.fixture
def arrancada(cfg, monkeypatch):
    """La aplicación arrancada DE VERDAD, con su `lifespan`.

    El resto de tests usan `TestClient` sin `with` para no disparar el
    `lifespan`, porque llama a `init_db()` sobre el motor real y eso toca la base
    del usuario. Aquí hace falta lo contrario -es justo el arranque lo que se
    prueba-, así que se sustituyen las dos cosas que salen de la máquina:
    `init_db` y los clientes.
    """
    monkeypatch.setattr("app.api.init_db", lambda: None)
    monkeypatch.setattr("app.api._clientes", lambda cfg_: (None, None, {}))
    monkeypatch.setattr("app.api.get_config", lambda: cfg)
    app.dependency_overrides[get_config] = lambda: cfg
    try:
        yield
    finally:
        app.dependency_overrides.clear()


def test_al_arrancar_se_montan_los_trabajos(arrancada, monkeypatch):
    """El fallo que este test existe para impedir: `build_scheduler` estaba
    escrito, probado y no lo llamaba NADIE en producción. El contenedor arrancaba,
    servía la PWA, contestaba `status: ok` y no ejecutaba ni el refresco de
    Garmin, ni la decisión de las 09:00, ni la reconciliación. Desde fuera es
    idéntico a un día de descanso: no llega mensaje.

    `backfill_wellness` entra en la misma lista y por lo mismo: es el trabajo
    que tapa los días que el sistema se perdió, y dejarlo escrito sin montar
    sería otra vez un interruptor conectado a nada.

    Y `perception_notice` igual. Es el que evalúa la sesión de ayer y cuenta las
    disociaciones: sin montar, `session_performance` no se escribiría NUNCA, el
    contador de la vista 5 se quedaría a cero para siempre y la pantalla diría
    "todavía no se puede contar" mes tras mes sin que nada diera un error.

    `startup_audit` es el último en llegar y es el que vigila a los demás: mira
    qué trabajos debieron correr mientras el contenedor no estaba. Sin montar,
    el sistema vuelve a no tener forma de saber que se perdió una mañana, que es
    el silencio que todos los de esta lista comparten.
    """
    monkeypatch.setattr(settings, "scheduler_enabled", True)

    with TestClient(app) as c:
        sched = c.get("/api/health").json()["scheduler"]

    assert sched["running"] is True, "la aplicación arrancó sin planificador"
    assert set(sched["jobs"]) == {
        "garmin_fetch", "decision_fallback", "reconcile", "perception_notice",
        "backfill_wellness", "startup_audit",
    }
    assert all(sched["jobs"].values()), (
        "un trabajo sin próxima ejecución está montado pero no se va a ejecutar, "
        "que es el mismo silencio con otra forma"
    )


def test_sin_planificador_la_salud_no_dice_que_todo_va_bien(arrancada, monkeypatch):
    """Apagarlo es legítimo (tests, un segundo proceso solo-web), pero tiene que
    verse: si no, es una aplicación que no decide nada contestando 200."""
    monkeypatch.setattr(settings, "scheduler_enabled", False)

    with TestClient(app) as c:
        cuerpo = c.get("/api/health").json()

    assert cuerpo["scheduler"]["running"] is False
    assert cuerpo["scheduler"]["error"], "no se dice por qué no hay planificador"
    assert cuerpo["scheduler"]["jobs"] == {}


def test_si_el_planificador_revienta_la_pwa_sigue_en_pie_y_se_dice(
    arrancada, monkeypatch
):
    """Negarse a arrancar dejaría sin formulario, que es lo único que se puede
    hacer a mano cuando algo va mal. Pero tampoco puede fingir que arrancó."""
    monkeypatch.setattr(settings, "scheduler_enabled", True)

    def revienta(*a, **k):
        raise RuntimeError("la zona horaria del config no existe")

    monkeypatch.setattr("app.scheduler.build_scheduler", revienta)

    with TestClient(app) as c:
        cuerpo = c.get("/api/health").json()
        assert c.get("/").status_code == 200, "sin planificador la PWA sigue viva"

    assert cuerpo["scheduler"]["running"] is False
    assert "zona horaria" in cuerpo["scheduler"]["error"]


def test_al_parar_la_aplicacion_el_planificador_se_para(arrancada, monkeypatch):
    """Un planificador que sobrevive al proceso que lo montó sigue decidiendo con
    una configuración que ya nadie está mirando."""
    monkeypatch.setattr(settings, "scheduler_enabled", True)

    with TestClient(app):
        vivo = app.state.scheduler
        assert vivo.running

    assert not vivo.running


def test_el_log_level_del_env_se_aplica_de_verdad(arrancada, monkeypatch):
    """Otro interruptor que no estaba conectado a nada.

    `LOG_LEVEL` solo lo aplicaba `cli.py`. Bajo uvicorn -que es como corre esto
    en el Umbrel- nadie llamaba a `basicConfig`, el logger raíz se quedaba en
    WARNING y todos los `log.info` del sistema se tiraban. Poner `DEBUG` para ver
    por qué el motor decidió lo que decidió no producía ni una línea: parecía que
    el motor no tuviera nada que contar, y `docker compose logs` -lo que el
    README manda mirar cuando algo va mal- salía vacío por construcción.
    """
    import logging

    monkeypatch.setattr(settings, "scheduler_enabled", False)
    monkeypatch.setattr(settings, "log_level", "DEBUG")
    previo = logging.getLogger().level
    try:
        with TestClient(app):
            assert logging.getLogger().isEnabledFor(logging.INFO), (
                "el logger raíz sigue en WARNING con LOG_LEVEL=DEBUG: los logs "
                "de la aplicación no llegan a `docker compose logs`"
            )
            assert logging.getLogger("app.runner").isEnabledFor(logging.DEBUG)
    finally:
        logging.getLogger().setLevel(previo)


def test_un_log_level_mal_escrito_no_se_traga_en_silencio():
    """Caer a WARNING por un typo sería este mismo fallo con otra cara: el
    usuario pidió DEBUG, no lo vería, y culparía al motor."""
    from app.settings import Settings

    with pytest.raises(Exception) as exc:
        Settings(log_level="INFORMACION")
    assert "LOG_LEVEL" in str(exc.value)


# ---------------------------------------------------------------------------
# El aviso de que esto no tiene contraseña
# ---------------------------------------------------------------------------
# El montaje de pruebas en la LAN va sin autenticación a propósito: red de casa,
# un solo usuario. La decisión es del usuario y está bien; lo que no puede pasar
# es que VIAJE a otro despliegue sin que nadie se entere.
#
# Y no se puede comprobar desde dentro: estar detrás del `app_proxy` de Umbrel y
# estar publicado en crudo en el wifi se ven idénticos desde este proceso. Así
# que se declara con `AUTH_FRONT` y el arranque lo canta. Estos tests fijan las
# dos mitades que importan: que el silencio SOLO se compre declarando un proxy,
# y que el aviso diga qué queda abierto en vez de un "sin auth" que no asusta a
# nadie.


def _avisar(caplog, monkeypatch, valor):
    """Llama al aviso directamente, no a través del arranque completo.

    Por `basicConfig(..., force=True)`: el arranque reconfigura el logger raíz
    y `force=True` se lleva por delante TODOS los manejadores, incluido el que
    `caplog` acababa de poner. El aviso se emite -se ve en el stderr capturado-
    pero `caplog.records` sale vacío. Es una peculiaridad de cómo se prueba
    esto, no del aviso.

    Que el arranque llame a esto lo fija el test de más abajo, por separado.
    """
    import logging

    from app.api import _avisar_de_la_puerta

    monkeypatch.setattr(settings, "auth_front", valor)
    with caplog.at_level(logging.INFO, logger="app.api"):
        _avisar_de_la_puerta()
    return list(caplog.records)


def test_el_arranque_llama_al_aviso(arrancada, monkeypatch):
    """Lo de siempre: un aviso perfecto que nadie invoca no avisa de nada."""
    import app.api as api

    llamadas = []
    monkeypatch.setattr(settings, "scheduler_enabled", False)
    monkeypatch.setattr(api, "_avisar_de_la_puerta", lambda *a, **k: llamadas.append(1))
    with TestClient(app):
        pass
    assert llamadas, "el arranque no dice nada sobre quién protege la puerta"


@pytest.mark.parametrize("valor", ["", "ninguna"])
def test_sin_proxy_delante_el_arranque_avisa(caplog, monkeypatch, valor):
    avisos = [
        r for r in _avisar(caplog, monkeypatch, valor)
        if r.levelname == "WARNING" and "AUTENTICACIÓN" in r.getMessage()
    ]
    assert avisos, f"AUTH_FRONT={valor!r} tiene que avisar y no ha avisado"


def test_declarar_un_proxy_es_lo_unico_que_calla_el_aviso(caplog, monkeypatch):
    registros = _avisar(caplog, monkeypatch, "proxy")
    assert not [
        r for r in registros
        if r.levelname == "WARNING" and "AUTENTICACIÓN" in r.getMessage()
    ]
    assert [r for r in registros if "proxy con credenciales" in r.getMessage()], (
        "callar el aviso no puede ser callar del todo: que hay un proxy delante "
        "es justo el dato que hace falta el día que alguien lo quite"
    )


def test_el_aviso_dice_QUE_queda_abierto_no_solo_que_no_hay_contrasena(
    caplog, monkeypatch
):
    """«Sin autenticación» no mueve a nadie; «/api/export es tu histórico de
    sueño, HRV y lumbar, y POST /api/checkin dispara una decisión» sí.

    Y tiene que desmontar la coartada de `DRY_RUN`, que es la que uno se cuenta
    a sí mismo: hoy corta la escritura, pero toda esta fase existe para llegar a
    quitarlo.
    """
    texto = " ".join(
        r.getMessage() for r in _avisar(caplog, monkeypatch, "ninguna")
    )
    assert "/api/export" in texto
    assert "/api/checkin" in texto
    assert "DRY_RUN" in texto
    assert "AUTH_FRONT=proxy" in texto, "hay que decir cómo se arregla"


def test_el_aviso_distingue_lo_deliberado_de_lo_no_pensado(caplog, monkeypatch):
    """Las dos cosas son «sin contraseña», pero solo una es una decisión.

    Si el mensaje fuese el mismo, el de la LAN -que está bien- enseñaría a
    ignorar el de la máquina nueva, que es el que importa.
    """
    a_proposito = " ".join(
        r.getMessage() for r in _avisar(caplog, monkeypatch, "ninguna")
    )
    caplog.clear()
    sin_pensar = " ".join(
        r.getMessage() for r in _avisar(caplog, monkeypatch, "")
    )
    assert "a propósito" in a_proposito
    assert "a propósito" not in sin_pensar
    assert "nadie ha declarado" in sin_pensar


def test_la_salud_declara_lo_que_falta(cliente):
    """Un sistema arrancado a medias que contesta "ok" es peor que uno caído,
    porque nadie va a mirar.

    ESTE TEST AFIRMABA `status == "ok"` Y ESO NO PROBABA NADA. El campo estaba
    escrito a mano con esa constante, así que la única forma de que la
    aserción fallara era que alguien cambiara la constante. Un test que no
    puede fallar por la razón que dice vigilar es exactamente lo que su propio
    docstring denuncia: tranquiliza sin mirar.

    Lo que se comprueba ahora es que el veredicto y el detalle no puedan
    contradecirse, que es la avería de verdad: el 13 de septiembre esto
    devolvía `"ok"` a la vez que un `config_file` diciendo que el YAML del
    disco no se podía ni cargar.
    """
    r = cliente.get("/api/health")
    assert r.status_code == 200
    cuerpo = r.json()

    assert cuerpo["status"] in ("ok", "revisar")
    assert isinstance(cuerpo["problemas"], list)
    # La única relación que no puede romperse nunca, en los dos sentidos.
    assert (cuerpo["status"] == "ok") is (cuerpo["problemas"] == []), (
        "el veredicto y la lista de problemas se contradicen: uno de los dos "
        "está mintiendo y no se sabe cuál"
    )

    assert "secrets_missing" in cuerpo, (
        "sin esto, un despliegue sin claves parece sano hasta que no llega el "
        "mensaje de la mañana"
    )
    assert cuerpo["config_hash"]
    assert "dry_run" in cuerpo


def test_el_config_desincronizado_no_puede_salir_como_sano(cliente, monkeypatch):
    """La avería concreta que trajo todo esto.

    El contenedor del 13 de septiembre servía un `config.yaml` que ya no era el
    del disco, y encima su validador rechazaba el del disco de plano. Los dos
    hechos estaban en la respuesta, en `config_file`, y aun así arriba ponía
    `"ok"`. Quien mira la salud mira `status`; el bloque de abajo se lee el día
    que ya se sospecha algo, o sea demasiado tarde.
    """
    from app import api as mod

    monkeypatch.setattr(mod, "_estado_del_config", lambda cfg: {
        "path": "/app/config.yaml",
        "loaded_hash": "8a0e1e304324a6be",
        "file_hash": None,
        "in_sync": False,
        "error": "el config.yaml del disco no se puede cargar: sección desconocida",
    })
    cuerpo = cliente.get("/api/health").json()

    assert cuerpo["status"] == "revisar"
    assert any("config.yaml" in p for p in cuerpo["problemas"]), (
        f"el problema no se nombra arriba: {cuerpo['problemas']}"
    )


def test_las_claves_que_faltan_se_nombran_en_el_veredicto(cliente, monkeypatch):
    """Faltar una clave no se parece a una avería desde el móvil, y por eso.

    El check-in se envía, se guarda y contesta que todo bien. Lo que no pasa es
    que se escriba la rutina en Hevy ni que llegue el mensaje. Visto desde la
    pantalla, un día sin claves es idéntico a un día de descanso.
    """
    from app.settings import settings as s

    monkeypatch.setattr(type(s), "missing_secrets", lambda self: ["HEVY_API_KEY"])
    cuerpo = cliente.get("/api/health").json()

    assert cuerpo["status"] == "revisar"
    assert any("HEVY_API_KEY" in p for p in cuerpo["problemas"])


def test_un_planificador_vivo_y_sin_trabajos_tampoco_es_sano(cliente, monkeypatch):
    """Es el que mejor se disfraza: corriendo, sin error, y sin decidir nunca.

    `running: true` es la comprobación que se hace de memoria, y no basta. Un
    planificador arrancado con cero trabajos sirve la PWA, contesta 200 y deja
    pasar la mañana entera sin tocar nada.
    """
    from app import api as mod

    monkeypatch.setattr(mod, "_estado_planificador",
                        lambda req: {"running": True, "jobs": {}, "error": None})
    cuerpo = cliente.get("/api/health").json()

    assert cuerpo["status"] == "revisar"
    assert any("sin ningún trabajo" in p for p in cuerpo["problemas"])


def test_cuando_el_planificador_dice_por_que_el_veredicto_lo_repite(cliente, monkeypatch):
    """Que haya un problema no basta: hace falta que diga cuál.

    LO ENCONTRÓ UNA MUTACIÓN QUE SE ESCAPÓ. Quitando la rama que lee
    `scheduler.error` los tests seguían en verde, porque el recuento de
    problemas no cambiaba: el caso caía en la rama de al lado y salía «el
    planificador NO está corriendo». Cierto, pero inútil. `desactivado por
    SCHEDULER_ENABLED` se arregla tocando una variable de entorno y `no arrancó`
    se arregla mirando el log del arranque; son dos mañanas distintas buscando
    en dos sitios distintos, y la frase genérica no distingue cuál.

    O sea que contar problemas no es comprobar que sirvan. Aquí se comprueba que
    la razón que el servidor YA sabe llega hasta arriba sin perderse.
    """
    from app import api as mod

    monkeypatch.setattr(mod, "_estado_planificador", lambda req: {
        "running": False, "jobs": {},
        "error": "desactivado por SCHEDULER_ENABLED",
    })
    cuerpo = cliente.get("/api/health").json()

    assert cuerpo["status"] == "revisar"
    assert any("SCHEDULER_ENABLED" in p for p in cuerpo["problemas"]), (
        "el servidor sabe por qué no hay planificador y el veredicto se queda "
        f"en una frase genérica: {cuerpo['problemas']}"
    )


def test_lo_que_es_una_eleccion_y_no_una_averia_no_ensucia_el_veredicto(monkeypatch):
    """`dry_run` y `auth_front` no cuentan, y el criterio importa.

    Si el veredicto se pusiera en rojo por cosas que se eligen a propósito,
    estaría en rojo siempre, y un aviso que está siempre encendido deja de
    leerse justo antes del día en que hacía falta.
    """
    from app.api import _problemas_de_salud

    sano = {
        "secrets_missing": [],
        "dry_run": True,
        "auth_front": "sin_declarar",
        "writes": {"pending_write": None},
        "scheduler": {"running": True, "jobs": {"reconcile": "x"}, "error": None},
        "clock": {"matches": True, "error": None},
        "config_file": {"in_sync": True},
    }
    assert _problemas_de_salud(sano) == []


# ---------------------------------------------------------------------------
# El check-in
# ---------------------------------------------------------------------------


def test_un_dia_sin_checkin_lo_dice_y_manda_los_deslizadores(cliente):
    r = cliente.get(f"/api/checkin/today?day={LUNES}")
    cuerpo = r.json()
    assert cuerpo["submitted"] is False
    assert cuerpo["values"] == {}
    assert cuerpo["sliders"], "la PWA se dibuja con esto; vacío no hay formulario"


def test_el_checkin_se_guarda_y_se_puede_releer(cliente):
    cliente.post("/api/checkin", json={
        "day": str(LUNES), "fatigue": 4, "lower_discomfort": 2,
        "comments": "molestia leve",
    })

    cuerpo = cliente.get(f"/api/checkin/today?day={LUNES}").json()
    assert cuerpo["submitted"] is True
    assert cuerpo["values"]["fatigue"] == 4
    assert cuerpo["values"]["lower_discomfort"] == 2
    assert cuerpo["comments"] == "molestia leve"


def test_un_no_se_guarda_como_no_y_no_como_si_no_hubiera_contestado(cliente, db):
    """El `False` tiene que sobrevivir al viaje entero. Es el bug de una línea.

    `post_checkin` filtra los campos con `if v is not None`, y ahí está la trampa
    a un carácter de distancia: escrito `if v` -que es lo que uno escribe sin
    pensar- el `False` de «hoy no voy» se caería del diccionario y el día se
    guardaría como si no hubieras contestado esa pregunta. El mensaje volvería a
    prescribir la sesión, el histórico no tendría el no, y no habría ni un error
    en el log. El único síntoma sería que decir que no no sirve para nada.

    Por eso el test mira las dos: un `False` y un `True` en el mismo envío.
    """
    cliente.post(
        "/api/checkin",
        json={
            "day": str(LUNES),
            "fatigue": 4,
            "wants_to_train": True,
            "will_train": False,
        },
    )

    cuerpo = cliente.get(f"/api/checkin/today?day={LUNES}").json()
    assert cuerpo["values"]["wants_to_train"] is True
    assert cuerpo["values"]["will_train"] is False, (
        "el 'no' se ha perdido por el camino: se guarda como si no hubiera "
        "contestado"
    )
    assert repo.get_checkin(db, LUNES).will_train is False


def test_no_contestar_una_pregunta_no_es_contestar_que_no(cliente, db):
    """El tercer estado, comprobado en la frontera donde se puede perder.

    Un día sin check-in y un día en el que dijiste que no ibas a entrenar son
    cosas opuestas, y para una columna booleana con defecto `False` serían la
    misma. La columna es nulable justo para que no lo sean, y esta es la prueba
    de que el `None` llega hasta abajo en vez de convertirse en un no.
    """
    cliente.post("/api/checkin", json={"day": str(LUNES), "fatigue": 4})

    cuerpo = cliente.get(f"/api/checkin/today?day={LUNES}").json()
    assert "will_train" not in cuerpo["values"], (
        "una pregunta sin contestar no puede aparecer con valor: para el motor "
        "sería una respuesta"
    )
    assert repo.get_checkin(db, LUNES).will_train is None


def test_las_preguntas_viajan_a_la_pwa_como_lista_propia(cliente, cfg):
    """La PWA no las lleva escritas a mano, igual que con los deslizadores.

    Y llegan en su propio array, no mezcladas con `sliders`. Si vinieran en la
    misma lista, el código que pinta barras de 1 a 10 tendría que mirar un campo
    de tipo antes de cada una, y el día que alguien añada un consumidor nuevo y
    se olvide de mirarlo, «¿Vas a entrenar hoy?» aparecerá como un deslizador.
    """
    cuerpo = cliente.get(f"/api/checkin/today?day={LUNES}").json()

    claves = [p["key"] for p in cuerpo["preguntas"]]
    assert claves == cfg.pregunta_keys()
    assert all(p.get("label") for p in cuerpo["preguntas"]), "sin etiqueta no se pintan"
    assert not {s["key"] for s in cuerpo["sliders"]} & set(claves)


# ---------------------------------------------------------------------------
# El selector de sesión, tal y como sale hacia la pantalla
# ---------------------------------------------------------------------------
#
# LO QUE SE PROTEGE AQUÍ ES QUE LA PANTALLA NO SE INVENTE NADA Y NO DECIDA NADA.
#
# «No se invente nada»: ni una opción escrita a mano en el JavaScript. Las cinco
# salen del `config.yaml` -tres del ciclo, dos de `sin_fuerza`- por la misma
# función que valida lo que se guarda, así que la pantalla no puede ofrecer algo
# que el guardado vaya a rechazar.
#
# «No decida nada»: la propuesta viaja en su propio campo y NUNCA dentro de
# `values`. `values` es lo que contestó el usuario. Si la propuesta entrara ahí,
# el formulario abriría con el selector ya contestado por el sistema, y a partir
# de esa mañana el histórico no podría distinguir «elegí el Día 2» de «no miré el
# selector». Es el mismo aplastamiento de tres estados en dos que las preguntas
# de Sí/No evitan con dos botones, cometido en el único sitio donde después no
# hay forma de deshacerlo.


def _hizo(db, claves, *, desde=LUNES):
    """Historial de fuerza, de más reciente a más antiguo, uno cada dos días."""
    for i, k in enumerate(claves):
        db.add(WorkoutLog(
            date=desde - timedelta(days=2 * i),
            routine_key=k,
            hevy_workout_id=f"w{i}-{k}",
        ))
    db.flush()


def test_las_opciones_del_selector_son_las_del_config_y_no_las_del_javascript(
    cliente, cfg
):
    """Las cinco salen del YAML, con el título con el que se leen en el móvil.

    Escritas en el JavaScript, añadir un `dia_4` al ciclo dejaría la pantalla
    ofreciendo tres opciones mientras el sistema rota entre cuatro, y el cuarto
    día no se podría declarar nunca. Es el mismo fallo que ya se pagó una vez con
    el calendario fijo, que estaba impecable y sencillamente no nombraba `dia_3`.
    """
    sel = cliente.get(f"/api/checkin/today?day={LUNES}").json()["selector"]

    assert [o["key"] for o in sel["opciones"]] == cfg.opciones_selector()
    # Con el título de leer, no con la clave. `dia_2` en la pantalla del móvil es
    # el identificador crudo asomando por donde no debe.
    por_clave = {o["key"]: o for o in sel["opciones"]}
    assert por_clave["dia_2"]["label"] == "Día 2"
    assert por_clave["bici"]["label"] == "Bici"
    # Y quién es fuerza y quién no, porque de eso depende lo que la pantalla diga
    # debajo: elegir «bici» no prescribe sesión.
    assert por_clave["dia_2"]["es_fuerza"] is True
    assert por_clave["bici"]["es_fuerza"] is False


def test_la_propuesta_es_la_que_el_motor_va_a_planificar(cliente, db, cfg):
    """La misma rutina que saldría escrita en Hevy si no se toca el selector.

    Este es EL test del bloque. La pantalla calcula la propuesta por su cuenta
    -`siguiente_en_rotacion` sobre lo último ejecutado- y `decide` la calcula por
    la suya. Son dos caminos, y el día que uno se desvíe el formulario dirá que
    hoy toca el Día 3 y en Hevy aparecerá el Día 1, sin que nada falle.

    Así que no se comprueba contra una constante escrita aquí: se comprueba
    contra lo que el motor planifica de verdad cuando se le envía el check-in.
    """
    _hizo(db, ["dia_2", "dia_1"])

    sel = cliente.get(f"/api/checkin/today?day={LUNES}").json()["selector"]
    assert sel["propuesta"] == "dia_3", "después del Día 2 toca el Día 3"

    cliente.post("/api/checkin", json={"day": str(LUNES), "fatigue": 4})
    fila = repo.current_decision(db, LUNES)
    planificada = json.loads(fila.planned_session_json)["routine"]
    assert planificada == sel["propuesta"], (
        "la rutina que la pantalla anuncia como propuesta no es la que el motor "
        "ha planificado: dos caminos distintos para el mismo número"
    )


def test_la_propuesta_no_se_cuela_entre_lo_contestado(cliente, db):
    """Viaja aparte, y `values` sigue vacío mientras nadie toque nada.

    Si la propuesta entrara en `values`, `recuperar()` la pintaría como respuesta
    al abrir el formulario y el selector saldría contestado sin que nadie lo
    hubiera tocado. El día siguiente, el aviso de haber entrenado otra cosa diría
    «declaraste Día 3» sobre una mañana en la que nadie declaró nada.
    """
    _hizo(db, ["dia_2"])

    cuerpo = cliente.get(f"/api/checkin/today?day={LUNES}").json()

    assert cuerpo["selector"]["propuesta"] == "dia_3"
    assert cuerpo["values"] == {}, (
        f"la propuesta se ha colado entre lo contestado: {cuerpo['values']}"
    )
    assert "chosen_session" not in cuerpo["values"]


def test_el_dia_que_lleva_mas_de_una_vuelta_parado_sale_marcado(cliente, db):
    """La marca del selector y la línea del mensaje cuentan lo mismo.

    Y tiene que ser el mismo cálculo, no dos parecidos: el mensaje de la mañana
    dice «Día 1: han pasado 6 sesiones» y el selector marca esa misma opción. Si
    cada uno contara por su cuenta, un día dirían cosas distintas sobre la misma
    rutina y no habría forma de saber cuál miente.
    """
    # dia_1 al fondo, dia_2 y dia_3 recientes: el Día 1 lleva una vuelta y una
    # sesión más sin hacerse, que es el umbral entero de `rotacion.pendientes`.
    _hizo(db, ["dia_3", "dia_2", "dia_3", "dia_2", "dia_1"])

    sel = cliente.get(f"/api/checkin/today?day={LUNES}").json()["selector"]
    por_clave = {o["key"]: o for o in sel["opciones"]}

    assert por_clave["dia_1"]["pendiente"] == 4
    assert por_clave["dia_1"]["ultima_vez"] == str(LUNES - timedelta(days=8))
    # Y las demás no. Marcarlas todas sería no marcar ninguna.
    assert por_clave["dia_2"]["pendiente"] is None
    assert por_clave["dia_3"]["pendiente"] is None
    assert por_clave["bici"]["pendiente"] is None


def test_una_rotacion_normal_no_marca_ninguna_opcion(cliente, db):
    """El control, y sin él lo de arriba no demuestra nada.

    Una marca que saliera siempre no distinguiría el caso que quiere señalar:
    sería decoración fija al lado de las cinco opciones.
    """
    _hizo(db, ["dia_3", "dia_2", "dia_1"])

    sel = cliente.get(f"/api/checkin/today?day={LUNES}").json()["selector"]
    assert all(o["pendiente"] is None for o in sel["opciones"]), (
        f"algo sale marcado en una rotación limpia: {sel['opciones']}"
    )


def test_la_marca_caduca_pero_la_opcion_sigue_estando(cliente, db):
    """Al caducar deja de marcarse, y nunca deja de poder elegirse.

    Las dos mitades importan. La primera porque una marca que sale todas las
    mañanas durante meses deja de informar y empieza a sonar a reproche por pura
    insistencia; es la misma caducidad que aplica el mensaje del día.

    La segunda es la que de verdad no se puede romper: lo que caduca es DECIRLO,
    nunca lo que se puede elegir. Un selector que escondiera el Día 1 por llevar
    mucho parado haría imposible volver a hacerlo, que es exactamente lo
    contrario de lo que la marca persigue.
    """
    # Siete sesiones sin el Día 1: por encima de `len(orden) + CADUCA_TRAS`.
    _hizo(db, ["dia_3", "dia_2"] * 4 + ["dia_1"])

    sel = cliente.get(f"/api/checkin/today?day={LUNES}").json()["selector"]
    por_clave = {o["key"]: o for o in sel["opciones"]}

    assert por_clave["dia_1"]["pendiente"] is None, "la marca tenía que haber caducado"
    assert "dia_1" in por_clave, "la opción no se puede esconder nunca"


def test_el_selector_llega_con_su_enunciado_y_su_nota(cliente, cfg):
    """El texto sale del config, como el de las preguntas y el de los comentarios.

    La nota no es decorativa: es la que evita el malentendido de leer el selector
    como «apúntame el entreno». Escrita en el JavaScript, cambiarla en el YAML no
    cambiaría nada de lo que se lee en el móvil.
    """
    sel = cliente.get(f"/api/checkin/today?day={LUNES}").json()["selector"]

    assert sel["key"] == cfg.raw["checkin_selector"]["key"]
    assert sel["label"] == cfg.raw["checkin_selector"]["label"]
    assert sel["nota"] and "lo que registres en Hevy" in sel["nota"]


def test_lo_elegido_se_guarda_y_vuelve_a_la_pantalla(cliente, db):
    """El viaje entero de una cadena, que es el tipo nuevo de este formulario."""
    r = cliente.post(
        "/api/checkin", json={"day": str(LUNES), "fatigue": 4, "chosen_session": "dia_2"}
    )
    assert r.status_code == 200, r.text

    cuerpo = cliente.get(f"/api/checkin/today?day={LUNES}").json()
    assert cuerpo["values"]["chosen_session"] == "dia_2"
    assert repo.get_checkin(db, LUNES).chosen_session == "dia_2"


def test_una_sesion_que_no_esta_en_el_selector_se_rechaza(cliente):
    """Un `dia_4` en un ciclo de tres no se guarda callando.

    Y es el rechazo que más falta hace de los tres del formulario, porque es el
    único invisible: un deslizador fuera de rango sigue siendo un número que se
    ve raro, pero una elección inventada se guarda, no coincide con ninguna
    rutina, y el sistema se comporta igual que si no hubieras contestado.
    """
    r = cliente.post(
        "/api/checkin", json={"day": str(LUNES), "chosen_session": "dia_4"}
    )
    assert r.status_code == 400, f"se ha colado con un {r.status_code}"
    assert "dia_4" in r.json()["detail"]


def test_enviar_el_checkin_decide_el_dia_en_ese_momento(cliente, db):
    """Las dos cosas van juntas a propósito.

    Si se guardara aquí y se decidiera en otro sitio, el usuario enviaría el
    formulario y no pasaría nada visible hasta una hora después.
    """
    r = cliente.post("/api/checkin", json={"day": str(LUNES), "fatigue": 3})
    cuerpo = r.json()

    assert cuerpo["checkin_saved"] is True
    assert cuerpo["decided"] is True
    assert cuerpo["light"] in {"green", "amber", "red"}
    assert cuerpo["session"]
    assert repo.current_decision(db, LUNES).source == "checkin"


def test_un_deslizador_que_no_existe_es_un_400(cliente, db):
    """El fallo silencioso más caro de este endpoint.

    Un `fatiga` por `fatigue` enviado desde la PWA no se guardaría en ninguna
    parte, y el sistema decidiría sin ese dato contestando que el check-in está
    completo. El freno lumbar se quedaría sin `lower_discomfort` y nadie vería
    un error: solo una decisión tomada a ciegas con cara de normal.
    """
    r = cliente.post("/api/checkin", json={"day": str(LUNES), "fatiga": 7})

    assert r.status_code in (400, 422), (
        f"se ha tragado un deslizador inexistente y ha contestado "
        f"{r.status_code}: {r.text}"
    )
    assert repo.get_checkin(db, LUNES) is None or not repo.checkin_values(
        repo.get_checkin(db, LUNES)
    ), "no puede quedar guardado un check-in vacío como si fuera bueno"


def test_un_campo_que_el_config_no_declara_es_un_400(cliente, cfg_copia, db):
    """El segundo cerrojo, el que protege de una asimetría futura.

    `extra="forbid"` para en la puerta lo que el modelo no conoce. Pero un campo
    que el modelo SÍ tiene y que el `config.yaml` ha dejado de declarar pasa esa
    puerta y llega a `upsert_checkin`, que es quien conoce la lista de verdad.
    Ahí tiene que ser un 400 y no un campo que se guarda en ninguna parte.

    Se llega quitando un deslizador del YAML, que es justo el cambio que lo
    provocaría en producción.
    """
    cfg_copia.raw["checkin_sliders"] = [
        s for s in cfg_copia.raw["checkin_sliders"] if s["key"] != "fatigue"
    ]
    app.dependency_overrides[get_config] = lambda: cfg_copia

    r = cliente.post("/api/checkin", json={"day": str(LUNES), "fatigue": 3})

    assert r.status_code == 400, (
        f"un campo que el config ya no declara se ha colado con un "
        f"{r.status_code}: se guardaría en ninguna parte"
    )
    assert "fatigue" in r.json()["detail"], "hay que decir CUÁL es el campo malo"


def test_los_deslizadores_del_config_y_del_modelo_coinciden(cfg):
    """La simétrica del test anterior, y se rompe igual de callando.

    El `config.yaml` es quien dibuja el formulario y quien decide qué claves
    acepta `upsert_checkin`; `CheckinIn` es quien deja pasar la petición. Si se
    añade un deslizador al YAML y no aquí, la PWA lo pinta, el usuario lo mueve,
    y `extra="forbid"` rechaza el envío ENTERO con un 422: se pierde también el
    resto del check-in. Al revés -aquí y no en el YAML- el campo pasa la
    validación y muere en `upsert_checkin` con un 400.

    Las dos listas tienen que ser la misma, y este test es lo único que lo
    sostiene el día que se toque una de ellas.

    Se compara contra la UNIÓN de las TRES secciones del check-in. Aquí, y solo
    aquí, deslizadores, preguntas de Sí/No y selector son lo mismo: campos que la
    PWA manda y que el modelo tiene que dejar pasar. Es exactamente la misma
    unión que admite `repo.upsert_checkin`, y no por casualidad: los dos
    contestan la misma pregunta -qué puede llegar del formulario- desde los dos
    extremos del cable.

    La frontera entre los tres -quién puede mover el semáforo, quién llega a
    `signals.values`- vive en `config_loader`, y meterla también en este test
    haría que la asimetría que sí importa se colara por el hueco.
    """
    from app.api import CheckinIn

    del_config = {s["key"] for s in cfg.raw.get("checkin_sliders", [])}
    del_config |= {p["key"] for p in cfg.raw.get("checkin_preguntas", [])}
    # El selector llega por el mismo cable aunque no sea ni un deslizador ni una
    # pregunta. Sin él aquí, quitarlo de `CheckinIn` daría verde: `extra="forbid"`
    # rechazaría el envío entero con un 422 el día que se tocara el selector, y
    # se perdería también el resto del check-in.
    del_config |= {(cfg.raw.get("checkin_selector") or {})["key"]}
    del_modelo = set(CheckinIn.model_fields) - {"comments", "day"}

    assert del_modelo == del_config, (
        f"solo en el modelo: {sorted(del_modelo - del_config)}; "
        f"solo en el config: {sorted(del_config - del_modelo)}"
    )


def test_un_valor_fuera_de_rango_se_rechaza(cliente):
    """0-10. Un 70 por un 7 mal tecleado no puede entrar al motor."""
    r = cliente.post("/api/checkin", json={"day": str(LUNES), "fatigue": 70})
    assert r.status_code == 422


def test_se_puede_rehacer_el_checkin_sin_mentirle_a_la_fecha(cliente, db):
    ayer = LUNES - timedelta(days=1)
    cliente.post("/api/checkin", json={"day": str(ayer), "fatigue": 2})
    assert repo.checkin_values(repo.get_checkin(db, ayer))["fatigue"] == 2

    r = cliente.post("/api/checkin", json={"day": str(ayer), "fatigue": 9})
    assert repo.checkin_values(repo.get_checkin(db, ayer))["fatigue"] == 9
    assert r.json()["decided"] is True, (
        "rehacer el check-in guarda el valor nuevo pero no vuelve a decidir: "
        "el usuario corrige el formulario y sigue con el plan de la respuesta vieja"
    )


def test_reenviar_el_checkin_vuelve_a_decidir_y_no_duplica(cliente, db):
    """Las dos mitades importan, y la primera es la que se rompía.

    Comprobar solo que no hay decisiones duplicadas deja pasar el peor fallo
    posible: que el segundo envío reviente entero y haga `rollback`. Como la
    decisión del primer envío sí quedó, el recuento seguiría dando 1 y el test
    pasaría tan feliz mientras el usuario recibe un mensaje de Telegram con un
    plan que no está guardado en ninguna parte.

    Así fue exactamente como el `UNIQUE` de `notifications` sobrevivió a la
    primera versión de este test: `_decidir` se traga la excepción y la convierte
    en un `decided: False` que nadie estaba mirando.
    """
    from sqlalchemy import select

    from app.models import Decision as DecisionRow, Notification

    for i in range(3):
        r = cliente.post("/api/checkin", json={"day": str(LUNES), "fatigue": 3})
        assert r.json()["decided"] is True, (
            f"el envío nº {i + 1} del día no ha llegado a decidir: "
            f"{r.json().get('error')}"
        )

    vigentes = db.scalars(
        select(DecisionRow).where(
            DecisionRow.date == LUNES, DecisionRow.is_current.is_(True)
        )
    ).all()
    assert len(vigentes) == 1, "tres envíos han dejado tres decisiones vigentes"

    # Y el registro de avisos se parece a la realidad: tres intentos, tres filas.
    avisos = db.scalars(
        select(Notification).where(Notification.date == LUNES)
    ).all()
    assert len(avisos) == 3


# ---------------------------------------------------------------------------
# Cuando la decisión falla
# ---------------------------------------------------------------------------


def test_si_garmin_falla_el_checkin_se_guarda_y_se_dice_que_no_se_decidio(
    cliente, db, monkeypatch
):
    """Contestar 200 y callarse sería lo peor posible.

    El usuario cerraría el móvil convencido de que ya está, y no habría ni
    mensaje ni rutina ni forma de enterarse hasta la noche.
    """
    def revienta(cfg_, day):
        raise RuntimeError("Garmin ha devuelto 429")

    monkeypatch.setattr("app.scheduler._fetch_garmin", revienta)

    r = cliente.post("/api/checkin", json={"day": str(LUNES), "fatigue": 3})
    cuerpo = r.json()

    assert cuerpo["checkin_saved"] is True
    assert cuerpo["decided"] is False, (
        "sin este campo la PWA no tiene cómo distinguir el fallo del éxito"
    )
    assert "429" in cuerpo["error"], "el aviso sin la causa no sirve para arreglarlo"
    assert "guardado" in cuerpo["error"], (
        "hay que decir explícitamente qué SÍ se guardó, o el usuario reenviará"
    )
    # Y el check-in tiene que estar de verdad, no solo dicho.
    assert repo.checkin_values(repo.get_checkin(db, LUNES))["fatigue"] == 3


def test_si_la_decision_revienta_tambien_se_dice(cliente, monkeypatch):
    def revienta(*a, **kw):
        raise RuntimeError("el motor ha petado")

    monkeypatch.setattr("app.runner.run_daily", revienta)

    cuerpo = cliente.post(
        "/api/checkin", json={"day": str(LUNES), "fatigue": 3}
    ).json()
    assert cuerpo["checkin_saved"] is True
    assert cuerpo["decided"] is False
    assert "el motor ha petado" in cuerpo["error"]


def test_decided_esta_siempre_pase_lo_que_pase(cliente, monkeypatch):
    """El contrato que sostiene a la PWA: `decided` no puede faltar nunca."""
    cuerpo = cliente.post(
        "/api/checkin", json={"day": str(LUNES), "fatigue": 3}
    ).json()
    assert "decided" in cuerpo

    def revienta(cfg_, day):
        raise RuntimeError("nada va")

    monkeypatch.setattr("app.scheduler._fetch_garmin", revienta)
    otro = cliente.post(
        "/api/checkin", json={"day": str(LUNES), "fatigue": 3}
    ).json()
    assert "decided" in otro


def test_los_problemas_viajan_al_cliente_aunque_la_decision_salga(cliente):
    """Que Telegram no enviara es algo que el usuario tiene que saber AHORA,
    no cuando eche en falta el mensaje."""
    cuerpo = cliente.post(
        "/api/checkin", json={"day": str(LUNES), "fatigue": 3}
    ).json()
    assert "problems" in cuerpo
    assert "hevy" in cuerpo and "telegram" in cuerpo


# ---------------------------------------------------------------------------
# Consultas
# ---------------------------------------------------------------------------


def test_sin_decision_del_dia_se_contesta_404_y_no_una_vacia(cliente):
    """Una decisión vacía con un 200 se dibujaría como una sesión sin ejercicios."""
    r = cliente.get(f"/api/decision?day={LUNES}")
    assert r.status_code == 404


def test_la_decision_guardada_se_lee_entera(cliente):
    cliente.post("/api/checkin", json={"day": str(LUNES), "fatigue": 3})
    cuerpo = cliente.get(f"/api/decision?day={LUNES}").json()

    assert cuerpo["day"] == str(LUNES)
    assert cuerpo["light"]
    assert cuerpo["source"] == "checkin"
    assert cuerpo["config_hash"], "sin el hash no se puede explicar una decisión vieja"
    assert cuerpo["session"]["exercises"]
    assert isinstance(cuerpo["progressed"], list)


def test_el_estado_del_motor_es_legible(cliente):
    cuerpo = cliente.get("/api/state").json()
    assert "clean_sessions" in cuerpo
    assert "active_rules" in cuerpo


def test_reconciliar_sin_cliente_de_hevy_es_un_503_y_no_un_exito(cliente):
    """Sin Hevy no se puede saber qué se entrenó. Contestar 200 diría que las
    rachas están al día cuando no se ha mirado nada."""
    r = cliente.post("/api/reconcile")
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# Exportación
# ---------------------------------------------------------------------------
#
# Existe porque los datos son del usuario y tiene que poder sacarlos sin
# depender de esta aplicación ni de que siga existiendo.


def test_el_csv_sale_con_cabecera_y_una_fila_por_dia(cliente):
    cliente.post("/api/checkin", json={"day": str(LUNES), "fatigue": 3})
    cliente.post("/api/checkin", json={
        "day": str(LUNES + timedelta(days=1)), "fatigue": 4
    })

    r = cliente.get(f"/api/export?desde={LUNES}&hasta={LUNES + timedelta(days=1)}")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert ".csv" in r.headers["content-disposition"]

    filas = list(csv.reader(io.StringIO(r.text), delimiter=";"))
    assert filas[0][0] == "fecha"
    assert len(filas) == 3, f"esperaba cabecera + 2 días, hay {len(filas)}"
    assert filas[1][0] == str(LUNES)


def test_el_csv_dice_si_se_entreno_o_no(cliente, db, cfg):
    from app.models import WorkoutLog

    cliente.post("/api/checkin", json={"day": str(LUNES), "fatigue": 3})
    db.add(WorkoutLog(
        date=LUNES, routine_key="dia_1", hevy_workout_id="w1",
        all_sets_at_target=True,
    ))
    db.commit()

    r = cliente.get(f"/api/export?desde={LUNES}&hasta={LUNES}")
    filas = list(csv.reader(io.StringIO(r.text), delimiter=";"))
    cabecera, fila = filas[0], filas[1]
    assert fila[cabecera.index("entrenado")] == "si"


def test_el_csv_de_un_rango_vacio_es_solo_la_cabecera(cliente):
    r = cliente.get(f"/api/export?desde={LUNES}&hasta={LUNES}")
    filas = [f for f in csv.reader(io.StringIO(r.text), delimiter=";") if f]
    assert len(filas) == 1


# ---------------------------------------------------------------------------
# La PWA
# ---------------------------------------------------------------------------
#
# `StaticFiles` montado en "/" se traga todo lo que no haya casado antes. Es un
# fallo que no se ve mirando el código -el montaje está a 400 líneas de las
# rutas- y que en producción se manifiesta como la aplicación entera
# contestando HTML a `/api/...`. Estos tests son el único sitio donde eso salta.


def test_la_raiz_sirve_el_armazon_de_la_pwa(cliente):
    r = cliente.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "deslizadores" in r.text, (
        "el formulario se pinta dentro de este hueco; sin él no hay check-in"
    )


def test_montar_la_pwa_no_se_come_la_api(cliente):
    """El montaje en "/" va el último a propósito. Si alguien lo sube de sitio,
    esto es lo que se entera."""
    for ruta in ("/api/health", f"/api/checkin/today?day={LUNES}"):
        r = cliente.get(ruta)
        assert r.status_code == 200, f"{ruta} devolvió {r.status_code}"
        assert "application/json" in r.headers["content-type"], (
            f"{ruta} ya no contesta JSON: la PWA se ha tragado la API"
        )


def test_una_ruta_de_api_que_no_existe_es_un_404_y_no_el_index(cliente):
    """`html=True` sirve `index.html` cuando no encuentra el fichero. Para una
    ruta de `/api/` eso convierte un 404 honesto en un 200 con HTML dentro, que
    el cliente parsea como JSON y revienta lejos de aquí."""
    r = cliente.get("/api/no-existe-esto")
    assert r.status_code == 404
    assert "<html" not in r.text.lower()


def test_el_service_worker_se_sirve_desde_la_raiz(cliente):
    """El ámbito de un service worker es la carpeta desde la que se sirve. Desde
    `/static/sw.js` no cubriría `/`, la PWA no se instalaría y no habría ningún
    error: simplemente no aparecería el botón. Un fallo mudo de manual."""
    r = cliente.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert "no-cache" in r.headers.get("cache-control", ""), (
        "un service worker cacheado se queda clavado en la versión vieja y ya no "
        "hay forma de actualizar la aplicación desde el móvil"
    )


def test_el_service_worker_no_cachea_nada_de_la_api(cliente):
    """Una respuesta cacheada de `/api/checkin/today` abre el formulario diciendo
    "ya está hecho" un día que no lo está, y el sistema decide sin check-in."""
    codigo = cliente.get("/sw.js").text
    assert '"/api/"' in codigo or "'/api/'" in codigo
    assert "/api/" not in codigo.split("ARMAZON")[1].split("]")[0], (
        "hay una ruta de la API en la lista de cosas que se precachean"
    )


def test_el_checkin_manda_tambien_la_etiqueta_del_comentario(cliente):
    """Igual que los deslizadores: escrita a mano en la PWA, cambiarla en el
    `config.yaml` no cambiaría nada y nadie sabría por qué."""
    cuerpo = cliente.get(f"/api/checkin/today?day={LUNES}").json()
    assert cuerpo["comment_label"]


# ---------------------------------------------------------------------------
# El estado del sistema llega al móvil
# ---------------------------------------------------------------------------
#
# `/api/health` decía desde el principio qué secretos faltan y si el
# planificador está vivo, y su único lector era el healthcheck de Docker, que
# solo comprueba que el 200 llegue. O sea: la instalación a medio configurar
# -formulario que se envía y se guarda, Hevy que no se reescribe, Telegram que
# no llega- no se veía desde el único sitio donde se mira esto, que es el móvil.
#
# Estos tests sujetan los dos extremos del cable. Que la API siga diciéndolo, y
# que la PWA siga leyéndolo con el mismo nombre.


CLAVES_QUE_LEE_LA_PWA = ("secrets_missing", "dry_run", "scheduler", "clock")
CLAVES_DEL_PLANIFICADOR = ("running", "jobs", "error")
CLAVES_DEL_RELOJ = ("timezone", "offset", "matches")


def _codigo_pwa(cliente) -> str:
    """El JavaScript de la PWA SIN comentarios.

    Se quitan a propósito. Un test que busca `secrets_missing` en el fichero
    entero se conforma con encontrarlo en un comentario que lo menciona, y un
    comentario no lee nada: la comprobación pasaría con el código ya desconectado.
    Es la misma distinción que en el `config.yaml` -una clave es un interruptor,
    un comentario es documentación- aplicada al sitio donde se comprueba.
    """
    codigo = cliente.get("/app.js").text
    codigo = re.sub(r"/\*.*?\*/", "", codigo, flags=re.DOTALL)   # /* ... */
    codigo = re.sub(r"(?m)^\s*//.*$", "", codigo)                # // línea entera
    return codigo


def test_health_sigue_diciendo_lo_que_la_pwa_va_a_pintar(cliente):
    cuerpo = cliente.get("/api/health").json()
    for clave in CLAVES_QUE_LEE_LA_PWA:
        assert clave in cuerpo, f"/api/health ya no devuelve '{clave}'"
    for clave in CLAVES_DEL_PLANIFICADOR:
        assert clave in cuerpo["scheduler"], f"scheduler ya no lleva '{clave}'"
    for clave in CLAVES_DEL_RELOJ:
        assert clave in cuerpo["clock"], f"clock ya no lleva '{clave}'"


def test_la_pwa_pregunta_por_la_salud_del_sistema(cliente):
    """Sin esta llamada todo lo demás de esta sección es decorado."""
    assert "/api/health" in _codigo_pwa(cliente), (
        "la PWA ha dejado de preguntar por /api/health: una instalación sin "
        "claves o sin planificador vuelve a ser invisible desde el móvil"
    )


@pytest.mark.parametrize(
    "clave", CLAVES_QUE_LEE_LA_PWA + CLAVES_DEL_PLANIFICADOR + CLAVES_DEL_RELOJ
)
def test_la_pwa_lee_cada_clave_con_el_nombre_que_la_api_le_da(cliente, clave):
    """El fallo que esto persigue no da error en ninguna parte.

    Renombrar `secrets_missing` en `app/api.py` deja el `s.secrets_missing` de
    `app.js` valiendo `undefined`, `[].length` no salta, y el aviso deja de
    pintarse. La API contesta 200, la PWA carga, y lo único que cambia es que
    nadie vuelve a enterarse de que faltan las claves.
    """
    assert clave in _codigo_pwa(cliente), (
        f"la PWA ya no lee '{clave}'. Si se ha renombrado en la API, hay que "
        f"renombrarlo también en `static/app.js`: aquí un nombre que no "
        f"coincide no es un error, es un aviso que deja de aparecer"
    )


def test_el_modo_en_seco_tambien_se_avisa(cliente):
    """En seco NO llega mensaje de Telegram, y eso es correcto.

    Es el único de los tres avisos que no denuncia una avería. Está por lo
    contrario: sin él, el silencio deliberado de `DRY_RUN=true` se lee como una
    avería y se acaba tocando lo que no está roto.
    """
    codigo = _codigo_pwa(cliente)
    assert "dry_run" in codigo
    # Dentro de la rama de `dry_run`, no en cualquier parte del fichero: lo que
    # importa es que el aviso lo dispare ESA condición y no otra.
    rama = codigo.split("if (s.dry_run)", 1)
    assert len(rama) == 2, "ya no hay una rama que dependa de dry_run"
    assert "seco" in rama[1][:600].lower(), (
        "la rama de dry_run ya no dice que el silencio es deliberado: sin eso, "
        "un día sin mensaje de Telegram se lee como una avería"
    )


@doble_de(Config)
class _CfgConZona:
    """Un `config` de mentira del que solo se mira la zona horaria."""

    def __init__(self, timezone: str) -> None:
        self.timezone = timezone


def _zona_con_otro_desplazamiento() -> str:
    """Una zona que HOY no coincide con el reloj de esta máquina.

    Se busca en vez de escribirla a mano porque el desplazamiento local depende
    de dónde corran los tests y de si es verano. Una constante convertiría este
    test en un test que pasa o falla según el mes.
    """
    from datetime import datetime, timezone as tz

    ahora = datetime.now(tz.utc)
    local = ahora.astimezone().utcoffset()
    for nombre in ("Pacific/Kiritimati", "Pacific/Midway", "Asia/Tokyo", "UTC"):
        if ahora.astimezone(ZoneInfo(nombre)).utcoffset() != local:
            return nombre
    raise AssertionError("no se encontró ninguna zona distinta de la local")


def test_el_aviso_del_reloj_puede_fallar_de_verdad():
    """El test central de esta sección, y el motivo de que exista.

    La comprobación que había antes -mirar que los trabajos del planificador
    salgan en `+02:00` y no en `+00:00`- NO PUEDE FALLAR NUNCA: los disparadores
    se construyen con `ZoneInfo(cfg.timezone)`, así que dan `+02:00` aunque el
    contenedor esté en UTC. Comprobado levantando la imagen sin `TZ`.

    Una comprobación que no puede fallar es peor que ninguna, porque ocupa su
    sitio y tranquiliza. Así que lo que se sujeta aquí no es que el aviso exista,
    es que sepa decir que NO.
    """
    otra = _zona_con_otro_desplazamiento()
    estado = _estado_del_reloj(_CfgConZona(otra))
    assert estado["matches"] is False, (
        f"el reloj local y {otra} tienen desplazamientos distintos y el aviso "
        f"dice que coinciden: vuelve a ser una comprobación que no puede fallar"
    )
    assert estado["offset"] is not None


def test_el_aviso_del_reloj_no_grita_cuando_todo_esta_bien():
    """La otra mitad: un aviso que salta siempre se aprende a ignorar."""
    from datetime import datetime, timezone as tz

    ahora = datetime.now(tz.utc)
    local = ahora.astimezone().utcoffset()
    # Se compara contra una zona con el MISMO desplazamiento que la local, no
    # contra el nombre de la local: son el mismo día a la misma hora, que es lo
    # único que decide bajo qué fecha se guarda un check-in.
    misma = next(
        (
            n
            for n in ("Europe/Madrid", "Europe/Paris", "UTC", "Asia/Tokyo",
                      "America/New_York", "Pacific/Kiritimati")
            if ahora.astimezone(ZoneInfo(n)).utcoffset() == local
        ),
        None,
    )
    assert misma is not None, "ninguna zona candidata coincide con el reloj local"
    estado = _estado_del_reloj(_CfgConZona(misma))
    assert estado["matches"] is True, (
        f"{misma} tiene el mismo desplazamiento que el reloj local y aun así "
        f"salta el aviso: un aviso falso enseña a no leer los avisos"
    )
    assert estado["error"] is None


def test_una_zona_mal_escrita_en_el_config_se_dice_y_no_revienta_el_health():
    """`/api/health` es lo que se mira cuando algo va mal; no puede caerse.

    Una zona inventada en el `config.yaml` haría saltar a `ZoneInfo`. Si eso
    subiera, el health devolvería 500 justo el día que hace falta leerlo, y el
    healthcheck de Docker reiniciaría el contenedor en bucle sin decir por qué.
    """
    estado = _estado_del_reloj(_CfgConZona("Europa/Madrid_mal_escrito"))
    assert estado["matches"] is False
    assert estado["error"] and "config.yaml" in estado["error"]


def test_un_health_que_falla_no_se_lleva_por_delante_el_formulario(cliente):
    """El check-in es lo importante; el panel es un extra.

    Van en dos llamadas separadas y sin `await` entre ellas justo por esto: el
    día que `/api/health` se caiga o tarde, el formulario tiene que salir igual.
    """
    codigo = _codigo_pwa(cliente)
    # La llamada suelta, a principio de línea, y NO la definición: buscar
    # "comprobarSalud()" a secas también encuentra `function comprobarSalud()`,
    # así que borrar la llamada y dejar la función habría pasado el test.
    lineas = [ln.strip() for ln in codigo.splitlines()]
    assert "comprobarSalud();" in lineas, (
        "nadie llama a comprobarSalud(): la función existe y no la ejecuta "
        "nadie, que es exactamente el fallo que esta sección venía a cerrar"
    )
    assert "await comprobarSalud();" not in lineas, (
        "encadenar el panel al arranque hace que un /api/health lento retrase "
        "el formulario, que es lo único que hay que rellenar por la mañana"
    )


# ---------------------------------------------------------------------------
# Métricas y análisis
#
# Lo que se vigila aquí no son las cuentas -están probadas en
# `test_analysis_vistas` con datos de resultado conocido- sino el contrato de la
# frontera: que lo que sale por HTTP sea JSON, que traiga siempre `n` y la
# ventana, y que la PWA no tenga que calcular nada para pintarlo.
# ---------------------------------------------------------------------------


def _sembrar_metricas(db, dias_n=40):
    """Cansancio y HRV moviéndose al revés, más una salida de bici con su RPE."""
    from app.models import Activity, Checkin, DailyMetrics

    hoy = date.today()
    for i in range(dias_n):
        d = hoy - timedelta(days=dias_n - i)
        db.add(Checkin(date=d, fatigue=1 + (i % 5), yesterday_rpe=1 + (i % 5)))
        db.add(DailyMetrics(date=d, fetch_status="ok", hrv=70.0 - (i % 5) * 6))
        db.add(
            Activity(
                garmin_activity_id=5000 + i,
                date=d,
                is_cycling=True,
                training_load=50.0 + (i % 5) * 30,
            )
        )
    db.commit()


def test_concordancia_contesta_con_todo_lo_que_hace_falta_para_pintar(cliente, db):
    """Un JSON del que la PWA saca el gráfico y las frases sin hacer una cuenta."""
    _sembrar_metricas(db)
    r = cliente.get("/api/metrics/concordancia?dias=90")
    assert r.status_code == 200
    d = r.json()

    assert d["vista"] == "concordancia"
    assert d["metodo"] == "spearman"
    assert d["ventana"]["dias"] == 90
    assert d["cobertura"]["garmin"] is not None

    par = next(p for p in d["pares"] if p["x"] == "fatigue" and p["y"] == "hrv")
    assert par["r"] == -1.0
    assert par["n"] == 40
    assert par["lectura"] and "coincide" in par["lectura"]
    # Y las etiquetas, que si no la PWA tendría que llevar su propia tabla de
    # nombres y se desincronizaría con el `config.yaml` a la primera.
    assert par["etiqueta_x"] == "Cansancio general"
    assert par["etiqueta_y"] == "Variabilidad (HRV)"

    serie_hrv = next(s for s in d["series"] if s["clave"] == "hrv")
    assert serie_hrv["puntos"][0]["valor"] is not None
    assert serie_hrv["puntos"][0]["escala"] is not None


def test_desfase_contesta_la_rejilla_entera(cliente, db):
    """La rejilla entera, y "entera" se cuenta, no se escribe.

    Estaba clavado en 35 -siete deslizadores por cinco métricas- y el número se
    quedó viejo el día que entraron las dos preguntas de Sí/No y la
    discordancia. Un test que se pone rojo porque han aparecido filas nuevas
    avisa de lo que no hay que hacer -quitarlas- en vez de comprobar lo que se
    quería comprobar, que es que no FALTA ninguna.
    """
    from app.analysis import series as S

    _sembrar_metricas(db)
    d = cliente.get("/api/metrics/desfase?dias=90").json()

    assert d["vista"] == "desfase"
    assert d["rango_desfase"] == [-3, 3]
    assert len(d["rejilla"]) == (len(S.SLIDERS) + len(S.PREGUNTAS)) * len(S.GARMIN)
    # Y las tres nuevas están de verdad, no solo cuadra la multiplicación.
    ejes_x = {c["x"] for c in d["rejilla"]}
    assert {"wants_to_train", "will_train", "discordancia"} <= ejes_x
    casilla = next(c for c in d["rejilla"] if c["x"] == "fatigue" and c["y"] == "hrv")
    assert casilla["mejor_desfase"] == 0
    assert sorted(int(k) for k in casilla["por_desfase"]) == [-3, -2, -1, 0, 1, 2, 3]


def _sembrar_entrenos(db, dias_n=40):
    """Un Día 2 cada cuatro, y la lumbar subiendo justo al día siguiente.

    Sin ruido a propósito: si el montaje pierde o desplaza un día, con una
    relación perfecta se ve, y con datos realistas se confundiría con el ruido.
    El `0EB695C9` es un `template_id` de verdad del `config.yaml`, que es lo que
    permite comprobar que el endpoint le pasa la configuración al ranking.
    """
    from app.models import Checkin, WorkoutLog

    hoy = date.today()
    for i in range(dias_n):
        d = hoy - timedelta(days=dias_n - i)
        db.add(Checkin(date=d, lower_discomfort=5 if i % 4 == 1 else 2))
        ejercicios = [{"exercise_template_id": "comun", "title": "Plancha"}]
        if i % 4 == 0:
            ejercicios.append(
                {"exercise_template_id": "0EB695C9", "title": "Leg Press (Machine)"}
            )
        db.add(
            WorkoutLog(
                hevy_workout_id=f"e{i}",
                date=d,
                routine_key="dia_2" if i % 4 == 0 else "dia_1",
                raw_json=json.dumps({"exercises": ejercicios}),
            )
        )
    db.commit()


def test_impacto_contesta_la_rejilla_con_la_advertencia_dentro(cliente, db):
    """Vista 3: la resaca de cada cosa a +1, +2 y +3, y el aviso de confusión.

    La advertencia va en la respuesta y no en un comentario del código porque
    quien lee un ranking de ejercicios tiene que leer, en la misma pantalla, que
    los ejercicios no se hacen sueltos.
    """
    _sembrar_entrenos(db)
    r = cliente.get("/api/metrics/impacto?dias=90")
    assert r.status_code == 200
    d = r.json()

    assert d["vista"] == "impacto"
    assert d["retardos"] == [1, 2, 3]
    assert d["ventana"]["dias"] == 90
    assert "no puede separar" in d["advertencia"].lower()

    fila = next(
        f
        for f in d["rejilla"]
        if f["exposicion"]["clave"] == "rutina_dia_2"
        and f["respuesta"]["clave"] == "lower_discomfort"
    )
    uno = next(c for c in fila["por_dia"] if c["dias_despues"] == 1)
    assert uno["media_expuesto"] == 5.0
    assert uno["media_no_expuesto"] == 2.0
    assert uno["r"] == 1.0
    assert "p_corregida" in uno
    assert "sube" in fila["lectura"] and "(peor)" in fila["lectura"]


def test_el_ranking_de_ejercicios_llega_con_los_nombres_del_yaml(cliente, db):
    """El endpoint le pasa el `config.yaml`, así que el nombre es el suyo.

    "Leg Press (Machine)" es como lo llama Hevy; "Prensa horizontal" es como lo
    llama él. Un ranking con los nombres de Hevy le obliga a traducir cada fila
    mentalmente para saber de qué le están hablando.
    """
    _sembrar_entrenos(db)
    r = cliente.get("/api/metrics/ranking-ejercicios?dias=90")
    assert r.status_code == 200
    d = r.json()

    assert d["vista"] == "ranking_ejercicios"
    assert d["respuesta"]["clave"] == "lower_discomfort"
    assert d["respuesta"]["etiqueta"] == "Molestias lumbares"
    assert d["ordenado_por"] == "correlación a +1 día"

    primero = d["ranking"][0]
    assert primero["clave"] == "0EB695C9"
    assert primero["etiqueta"] == "Prensa horizontal"
    assert next(c for c in primero["por_dia"] if c["dias_despues"] == 1)["r"] == 1.0

    # Y el que se hace todos los días sigue en la lista, sin r y con su porqué.
    comun = next(f for f in d["ranking"] if f["clave"] == "comun")
    casilla = next(c for c in comun["por_dia"] if c["dias_despues"] == 1)
    assert casilla["r"] is None
    assert casilla["na"]


def _sembrar_decisiones(db, dias_n=30):
    """Un mes de semáforo con las tres situaciones que separan los contadores.

    Los tres nombres son reglas de verdad del `config.yaml`, que es lo que
    permite comprobar que el endpoint le pasa la configuración a la auditoría en
    vez de reconstruir el catálogo a partir del histórico -que sería circular: a
    una regla que nunca disparó no se la encontraría por ningún lado-.

      - `lumbar_alto` (roja) dispara seis veces y manda las seis: cuando salta,
        no hay nada por encima;
      - `lumbar_medio` (ámbar) dispara quince veces y manda doce, porque las
        otras tres coincide con el rojo y pierde;
      - `cansancio_alto` (ámbar) dispara esas mismas quince veces y no manda
        NINGUNA, porque `lumbar_medio` va antes en el YAML y gana el desempate.

    Esa última es la que justifica que haya dos contadores: con uno solo
    parecería una de las reglas que más gobierna el semáforo cuando no le ha
    cambiado el color ni un día.

    Un día se deja sin decisión a propósito -el penúltimo- para que el hueco
    llegue hasta la respuesta del endpoint y no solo hasta la función.
    """
    from app.models import Decision

    hoy = date.today()
    for i in range(dias_n):
        if i == dias_n - 2:
            continue  # el hueco
        rojo = i % 5 == 0
        disparadas = ["lumbar_medio", "cansancio_alto"] if i % 2 else []
        if rojo:
            disparadas.append("lumbar_alto")
        db.add(
            Decision(
                date=hoy - timedelta(days=dias_n - i),
                light="red" if rojo else ("amber" if disparadas else "green"),
                trigger_rule=(
                    "lumbar_alto" if rojo else ("lumbar_medio" if disparadas else None)
                ),
                fired_rules_json=json.dumps(disparadas),
                skipped_rules_json=json.dumps([]),
                inputs_snapshot_json=json.dumps(
                    {"values": {"fatigue": 3 + (i % 5), "lower_discomfort": 7 if rojo else 2}}
                ),
                config_hash="h1",
                source="checkin",
                is_current=True,
            )
        )
    db.commit()


def test_auditoria_cuenta_los_disparos_y_separa_al_que_manda(cliente, db):
    """Vista 4: disparar y decidir son dos contadores, y por eso van separados.

    `cansancio_alto` dispara quince veces y no manda ninguna. Si el panel
    enseñara un solo número, esa regla parecería la que gobierna el semáforo
    cuando en realidad no ha cambiado ni un día de color.
    """
    _sembrar_decisiones(db)
    r = cliente.get("/api/metrics/auditoria?dias=90")
    assert r.status_code == 200
    d = r.json()

    assert d["vista"] == "auditoria"
    assert d["ventana"]["dias"] == 90

    reglas = {f["nombre"]: f for f in d["reglas"]}

    # Dispara mucho y no manda nunca: el caso que obliga a los dos contadores.
    assert reglas["cansancio_alto"]["veces_disparada"] == 15
    assert reglas["cansancio_alto"]["veces_determinante"] == 0

    # Manda siempre que dispara: no hay nada por encima del rojo.
    assert reglas["lumbar_alto"]["veces_disparada"] == 6
    assert reglas["lumbar_alto"]["veces_determinante"] == 6

    # Y el caso intermedio, que es el que hace que los dos números no sean
    # redundantes ni iguales: dispara quince veces y manda doce.
    assert reglas["lumbar_medio"]["veces_disparada"] == 15
    assert reglas["lumbar_medio"]["veces_determinante"] == 12

    # Con histórico de verdad, las que no se cumplieron ni una vez ya SÍ se
    # pueden señalar: se evaluaron y no saltaron.
    assert d["nunca_dispararon"]
    assert all(
        f["estado"] in ("nunca_disparo", "nunca_evaluada") for f in d["nunca_dispararon"]
    )
    assert all(f["lectura"] for f in d["nunca_dispararon"])


def test_el_dia_sin_decision_no_se_cuela_como_verde_por_el_endpoint(cliente, db):
    """El fallo que convertiría un mes con el PC apagado en un mes estupendo.

    El hueco tiene que llegar hasta la respuesta con su motivo escrito y quedarse
    FUERA del denominador del reparto de luces.
    """
    _sembrar_decisiones(db)
    d = cliente.get("/api/metrics/auditoria?dias=90").json()

    g = d["distribucion"]["global"]
    assert g["sin_decision"] >= 1
    assert g["n"] == g["green"] + g["amber"] + g["red"]
    assert sum(g["porcentaje"].values()) == pytest.approx(100.0)

    huecos = [x for x in d["dias"] if x["luz"] is None]
    assert huecos, "el día sin decisión tiene que salir, no desaparecer"
    assert all(x["na"] for x in huecos)


def test_la_auditoria_no_acepta_metodo_porque_no_correlaciona_nada(cliente, db):
    """Una opción muerta es una opción muerta aunque venga de la coherencia.

    Las otras cuatro rutas llevan `metodo` porque calculan correlaciones. Esta
    cuenta disparos. Darle el parámetro para que las cinco firmas se parecieran
    dejaría un mando en el panel conectado a nada, que es exactamente el tipo de
    cosa que este proyecto persigue.
    """
    _sembrar_decisiones(db)
    d = cliente.get("/api/metrics/auditoria?dias=90&metodo=kendall")
    # FastAPI ignora lo que no declara: sale 200 y en la respuesta no hay rastro
    # de método por ninguna parte.
    assert d.status_code == 200
    assert "metodo" not in json.dumps(d.json())


def _sembrar_juicios(db):
    """Un histórico de percepción con las tres situaciones que separan el contador.

    Cuatro sesiones que se pudieron juzgar -dos disociadas en la dirección que
    se mira, una en la contraria y una alineada- y dos que no: una sin check-in
    esa mañana y otra con demasiado poco histórico detrás. El contador tiene que
    decir 2 de 4 y además decir que hay 6 sesiones registradas, porque "dos de
    cuatro" y "dos de seis" son dos afirmaciones distintas y solo una es la que
    se quiso hacer.
    """
    from app.analysis.rendimiento import (
        ALINEADO,
        BASE_MINIMA,
        FUERZA,
        PERCEPCION_MEJOR,
        PERCEPCION_PEOR,
        SIN_DATO,
    )
    from app.models import SessionPerformance

    hoy = date.today()

    def fila(i, **kw):
        kw.setdefault("perception_index", 50.0)
        kw.setdefault("performance_index", 50.0)
        kw.setdefault("n_sessions_base", 30)
        db.add(
            SessionPerformance(
                date=hoy - timedelta(days=i),
                kind=FUERZA,
                source_key=f"hevy:W{i}",
                routine_key="dia1",
                **kw,
            )
        )

    fila(
        1,
        perception_index=10.0,
        perception_pct=8.0,
        performance_index=71.0,
        performance_pct=68.0,
        gap_pct=60.0,
        direction=PERCEPCION_PEOR,
        dissociation=True,
        comp_compliance=100.0,
        components_json=json.dumps(
            {
                "rendimiento": {
                    "componentes": {
                        "cumplimiento": {
                            "valor": 100.0,
                            "logradas": 12,
                            "prescritas": 12,
                        }
                    }
                }
            }
        ),
    )
    fila(
        3,
        perception_index=12.0,
        perception_pct=10.0,
        performance_index=70.0,
        performance_pct=66.0,
        gap_pct=56.0,
        direction=PERCEPCION_PEOR,
        dissociation=True,
        comp_compliance=90.0,
    )
    fila(
        5,
        perception_index=80.0,
        perception_pct=90.0,
        performance_index=20.0,
        performance_pct=15.0,
        gap_pct=-75.0,
        direction=PERCEPCION_MEJOR,
        dissociation=True,
    )
    fila(7, perception_pct=50.0, performance_pct=50.0, gap_pct=0.0, direction=ALINEADO)
    fila(
        9,
        perception_index=None,
        perception_pct=None,
        performance_pct=50.0,
        gap_pct=None,
        direction=SIN_DATO,
        na_reason="solo 0 de los 6 deslizadores están contestados",
    )
    fila(
        11,
        perception_pct=20.0,
        performance_pct=80.0,
        gap_pct=60.0,
        direction=PERCEPCION_PEOR,
        n_sessions_base=BASE_MINIMA - 1,
    )
    db.commit()


def test_la_percepcion_cuenta_una_direccion_y_registra_la_otra(cliente, db):
    """Vista 5: el número que se mira la mañana de levantarse pensando que no.

    Y la dirección contraria, que no se destaca pero se cuenta: un marcador que
    apunta los aciertos y no los fallos no es un marcador, es un cartel.
    """
    _sembrar_juicios(db)
    r = cliente.get("/api/metrics/percepcion?dias=30")
    assert r.status_code == 200
    d = r.json()

    c = d["contador"]
    assert (c["veces"], c["de"], c["pct"]) == (2, 4, 50.0)
    assert c["total_sesiones"] == 6
    assert c["sin_juicio"] == 2
    assert c["na"] is None

    assert d["contraria"]["veces"] == 1
    assert d["alineadas"] == 1

    # El listado que se pidió, del más reciente al más viejo y con los números.
    assert [x["fecha"] for x in d["disociaciones"]] == [
        (date.today() - timedelta(days=1)).isoformat(),
        (date.today() - timedelta(days=3)).isoformat(),
    ]
    assert d["disociaciones"][0]["percepcion"] == 10.0
    assert d["disociaciones"][0]["rendimiento"] == 71.0

    # Y la frase, con sus cifras y sin una sola palabra de ánimo.
    assert "percentil 8" in d["mensaje"]
    assert "12 de 12 series" in d["mensaje"]
    assert "Van 2 de 4" in d["mensaje"]


def test_la_sesion_sin_juicio_sale_con_su_motivo_y_fuera_del_denominador(cliente, db):
    """Meterla en el denominador premiaría el olvido del formulario.

    Cada vez que se dejara una mañana sin rellenar, el contador bajaría solo.
    Dejarla fuera y no decirlo sería peor, así que salen las dos cifras y sale
    el reparto de motivos.
    """
    _sembrar_juicios(db)
    c = cliente.get("/api/metrics/percepcion?dias=30").json()["contador"]

    assert c["de"] < c["total_sesiones"]
    assert "solo 0 de los 6 deslizadores están contestados" in c["motivos"]
    assert sum(c["motivos"].values()) == c["sin_juicio"]
    assert "no sobre todas las registradas" in c["nota"]


def test_la_percepcion_no_acepta_metodo_porque_resta_dos_percentiles(cliente, db):
    """Aquí no se correlaciona nada: se restan dos números ya guardados."""
    _sembrar_juicios(db)
    r = cliente.get("/api/metrics/percepcion?dias=30&metodo=kendall")
    assert r.status_code == 200
    assert "metodo" not in json.dumps(r.json())


def test_mirar_el_contador_no_puede_moverlo(cliente, db):
    """La vista SOLO lee. Ni evalúa sesiones ni marca nada como reportado.

    Si abrir la pantalla evaluara, se escribiría el juicio de una sesión a la
    que todavía le falta el esfuerzo percibido de la mañana siguiente; y como la
    fila no se reescribe, ese hueco se quedaría para siempre. Abrir la pantalla
    dos veces tampoco puede cambiar una coma.
    """
    from app.models import SessionPerformance

    _sembrar_juicios(db)
    antes = cliente.get("/api/metrics/percepcion?dias=30").json()
    reportadas = db.query(SessionPerformance).filter(
        SessionPerformance.reported_at.is_not(None)
    ).count()

    despues = cliente.get("/api/metrics/percepcion?dias=30").json()

    assert antes == despues
    assert db.query(SessionPerformance).count() == 6
    assert reportadas == 0, "mirar la pantalla no da por avisado ningún día"


def test_una_respuesta_desconocida_en_el_ranking_es_un_400_con_la_lista(cliente):
    """Un 500 diría que el servidor está roto; un ranking vacío sería peor.

    Vacío se leería como "ningún ejercicio se relaciona con nada", que es una
    conclusión, y sería mentira.
    """
    r = cliente.get("/api/metrics/ranking-ejercicios?respuesta=lumbago")
    assert r.status_code == 400
    assert "lumbago" in r.json()["detail"]
    assert "lower_discomfort" in r.json()["detail"]


def test_sin_datos_las_metricas_contestan_200_con_los_motivos(cliente):
    """Una sección de métricas vacía NO es un error: es el primer día.

    Contestar 404 o 500 con la base recién creada dejaría la pantalla en blanco
    justo cuando lo útil es ver qué falta y cuánto. Sale un 200 con las siete
    parejas y su motivo, que es lo que se pidió: nada oculto, nada aplazado.
    """
    for ruta in (
        "/api/metrics/concordancia",
        "/api/metrics/desfase",
        "/api/metrics/impacto",
        "/api/metrics/ranking-ejercicios",
        "/api/metrics/auditoria",
        "/api/metrics/percepcion",
    ):
        r = cliente.get(ruta)
        assert r.status_code == 200, ruta
    d = cliente.get("/api/metrics/concordancia").json()
    assert len(d["pares"]) == 7
    assert all(p["na"] for p in d["pares"])
    assert all(p["r"] is None for p in d["pares"])

    # La vista 3 tampoco se esconde: las exposiciones que no dependen de lo que
    # se haya entrenado siguen ahí, con el motivo escrito en cada casilla.
    v3 = cliente.get("/api/metrics/impacto").json()
    # Nueve exposiciones contra TODAS las respuestas. El número de respuestas se
    # cuenta de `series.py` -no se escribe- porque el que estaba escrito, doce,
    # se quedó viejo en cuanto entraron las dos preguntas de Sí/No y la
    # discordancia. Lo que hay que comprobar aquí es que ninguna falta.
    from app.analysis import series as S

    n_respuestas = len(S.SLIDERS) + len(S.PREGUNTAS) + len(S.GARMIN)
    assert len(v3["rejilla"]) == 9 * n_respuestas
    assert all(c["na"] for f in v3["rejilla"] for c in f["por_dia"])
    # Y el ranking sin entrenos es una lista vacía CON su advertencia y su
    # cobertura, no un 404 que dejaría la pantalla en blanco el primer día.
    r3 = cliente.get("/api/metrics/ranking-ejercicios").json()
    assert r3["n_ejercicios"] == 0
    assert r3["ranking"] == []
    assert r3["advertencia"]
    assert r3["cobertura"]["fuerza"] is None

    # Y la vista 4 con la base recién estrenada NO acusa a ninguna regla. Las
    # trece siguen listadas con su estado, pero `nunca_dispararon` -que se lee
    # bajo "o están mal calibradas o sobran"- sale vacía, porque el motor no ha
    # llegado a evaluarlas ni una vez y eso no es un defecto de la regla.
    v4 = cliente.get("/api/metrics/auditoria").json()
    assert v4["reglas"], "las reglas del YAML tienen que salir aunque no haya histórico"
    assert all(f["estado"] == "sin_historico" for f in v4["reglas"])
    assert v4["nunca_dispararon"] == []
    assert v4["distribucion"]["global"]["n"] == 0
    assert v4["distribucion"]["global"]["porcentaje"] is None

    # Y la vista 5 el primer día NO dice "nunca te ha pasado". Dice que todavía
    # no se puede contar, que es una frase distinta: un 0% sobre 0 sesiones se
    # lee como un veredicto sobre la percepción cuando lo único que hay es una
    # base vacía. Es exactamente el mismo fallo que el día verde inventado de la
    # vista 4, en la vista donde más caro saldría.
    v5 = cliente.get("/api/metrics/percepcion").json()
    assert v5["contador"]["veces"] == 0
    assert v5["contador"]["de"] == 0
    assert v5["contador"]["pct"] is None
    assert "no es un cero" in v5["contador"]["na"]
    assert v5["disociaciones"] == []
    assert v5["mensaje"] is None
    # Los componentes salen todos, con su n a cero y su motivo. Ninguno se
    # esconde a la espera de tener datos.
    assert len(v5["componentes"]) == 6
    assert all(c["n"] == 0 and c["na"] for c in v5["componentes"].values())


def test_un_metodo_inventado_se_rechaza_en_vez_de_caer_en_uno_por_defecto(cliente):
    """Un `metodo=kendall` que silenciosamente diera Spearman sería mentir.

    El método viaja en la respuesta y se pinta en pantalla. Aceptar cualquier
    cosa y calcular otra distinta pondría "kendall" encima de un número que no
    lo es.
    """
    for ruta in (
        "/api/metrics/concordancia",
        "/api/metrics/desfase",
        "/api/metrics/impacto",
        "/api/metrics/ranking-ejercicios",
    ):
        r = cliente.get(f"{ruta}?metodo=kendall")
        assert r.status_code == 400, ruta
        assert "kendall" in r.json()["detail"]


def test_pearson_se_puede_pedir_y_se_nota(cliente, db):
    _sembrar_metricas(db)
    d = cliente.get("/api/metrics/concordancia?dias=90&metodo=pearson").json()
    assert d["metodo"] == "pearson"
    assert all(p["metodo"] == "pearson" for p in d["pares"])


def test_una_ventana_absurda_se_rechaza(cliente):
    """Cuatro días no dan para nada y cinco años no existen."""
    for ruta in (
        "/api/metrics/concordancia",
        "/api/metrics/desfase",
        "/api/metrics/impacto",
        "/api/metrics/ranking-ejercicios",
        "/api/metrics/auditoria",
        "/api/metrics/percepcion",
    ):
        assert cliente.get(f"{ruta}?dias=4").status_code == 422, ruta
        assert cliente.get(f"{ruta}?dias=5000").status_code == 422, ruta


def test_la_pwa_no_calcula_nada_de_estadistica(cliente):
    """El requisito escrito, convertido en test.

    No se puede comprobar "no hay estadística" en general, pero sí se puede
    cerrar la puerta a que vuelva a entrar por donde entraría: alguien que
    quisiera pintar una correlación y no encontrara el endpoint la escribiría a
    mano en el cliente. Si algún día aparece aquí una raíz cuadrada o un
    sumatorio de productos, este test lo para.
    """
    codigo = _codigo_pwa(cliente)
    for sospecha in ("Math.sqrt", "pearson", "spearman", "percentil("):
        assert sospecha not in codigo, (
            f"la PWA contiene {sospecha!r}: la estadística se calcula en el "
            f"servidor, que es el único sitio donde se puede probar"
        )


# ---------------------------------------------------------------------------
# El hash del config: el de memoria contra el del disco
# ---------------------------------------------------------------------------


def test_health_dice_si_el_config_del_disco_es_el_que_esta_cargado(cliente, tmp_path, monkeypatch):
    """El fallo que más veces ha aparecido en este proyecto, convertido en aviso.

    «El valor que se lee no es el valor que se usa»: imagen vieja, esquema
    viejo, Caddyfile viejo. Los tres tenían la misma forma -editar el fichero y
    dar por hecho que el proceso lo había visto-. El `config.yaml` se carga UNA
    vez por proceso, así que editarlo no cambia nada hasta recrear el
    contenedor, y hasta ahora no había forma de notarlo desde fuera.

    Se comprueban los dos lados, porque solo el primero no prueba nada: un
    `in_sync: true` que no puede volverse `false` tranquiliza sin mirar.
    """
    fichero = tmp_path / "config.yaml"
    original = Path("config.yaml").read_text(encoding="utf-8")
    fichero.write_text(original, encoding="utf-8")
    monkeypatch.setattr(settings, "config_path", fichero)

    # El cliente sirve el `cfg` de la fixture; para que la comparación tenga
    # sentido, el fichero de disco arranca siendo ese mismo config.
    cargado = cliente.get("/api/health").json()["config_file"]
    assert cargado["file_hash"] is not None

    # Y ahora el cambio de verdad: mover el interruptor de escritura en el
    # disco sin reiniciar. Es exactamente lo que pasó al abrir los frenos.
    #
    # Se mueve en la dirección que toque en vez de escribir el valor a pelo.
    # La primera versión ponía `false -> true` fijo, y dejó de comprobar nada
    # el día que el interruptor se abrió: la sustitución no encontraba el texto,
    # el fichero salía idéntico y el test pasaba sin haber cambiado el hash. Un
    # test que depende del valor actual del config no prueba, acompaña.
    if "write_enabled: true" in original:
        tocado = original.replace("write_enabled: true", "write_enabled: false")
    else:
        tocado = original.replace("write_enabled: false", "write_enabled: true")
    assert tocado != original, "no se ha tocado el config: el test no probaría nada"
    fichero.write_text(tocado, encoding="utf-8")
    tocado = cliente.get("/api/health").json()["config_file"]
    assert tocado["file_hash"] != cargado["file_hash"], (
        "cambiar write_enabled en el disco no ha movido el hash del fichero: "
        "entonces la comparación no puede avisar de nada"
    )
    assert tocado["in_sync"] is False
    assert "recrear" in tocado["error"]


def test_health_avisa_si_el_config_del_disco_esta_roto(cliente, tmp_path, monkeypatch):
    """Un YAML que no carga es un arranque futuro fallido, no un detalle.

    La aplicación en marcha sobrevive -tiene el suyo en memoria- y justo por eso
    nadie se enteraría hasta el siguiente reinicio, que es cuando ya no arranca.
    """
    fichero = tmp_path / "config.yaml"
    fichero.write_text("integrations: [esto no\n  es: yaml", encoding="utf-8")
    monkeypatch.setattr(settings, "config_path", fichero)

    bloque = cliente.get("/api/health").json()["config_file"]
    assert bloque["in_sync"] is False
    assert bloque["file_hash"] is None
    assert "no se puede cargar" in bloque["error"]
    # Y el health sigue contestando 200: la app en marcha no está rota.
    assert cliente.get("/api/health").status_code == 200


# ---------------------------------------------------------------------------
# La marca de escritura en curso
# ---------------------------------------------------------------------------


def test_health_saca_la_escritura_a_medias(cliente, tmp_path, monkeypatch):
    """La señal que se emitía y no escuchaba nadie.

    `write_routine` escribe esta marca justo antes del PUT y la borra justo
    después, para que sobreviva a que el proceso muera entre las dos cosas.
    Estaba escribiéndose desde el principio y no la leía nadie: ni endpoint, ni
    mensaje, ni aviso. Si aparece, hay una rutina en Hevy cuyo estado no se
    conoce, y eso tiene que verse sin ir a buscarlo.
    """
    from app.integrations.hevy import pending_marker

    monkeypatch.setattr("app.api._raiz_de_datos", lambda: tmp_path)
    assert cliente.get("/api/health").json()["writes"]["pending_write"] is None

    marca = pending_marker(tmp_path)
    marca.parent.mkdir(parents=True, exist_ok=True)
    marca.write_text(
        json.dumps({"routine_id": "abc", "backup": "x.json", "started_at": "2026-09-14T07:05:00"}),
        encoding="utf-8",
    )
    pendiente = cliente.get("/api/health").json()["writes"]["pending_write"]
    assert pendiente is not None, "la marca existe y el health dice que no hay ninguna"
    assert pendiente["routine_id"] == "abc"

    # Una marca corrupta NO es lo mismo que ninguna marca: sigue significando
    # que hubo un PUT sin confirmar, solo que sin saber de qué rutina.
    marca.write_text("{no es json", encoding="utf-8")
    rota = cliente.get("/api/health").json()["writes"]["pending_write"]
    assert rota is not None and rota["routine_id"] == "?"

    marca.unlink()
    assert cliente.get("/api/health").json()["writes"]["pending_write"] is None


def test_health_publica_los_dos_interruptores_de_escritura(cliente):
    """Los dos frenos, en el mismo sitio que el `dry_run`.

    Estaban repartidos entre el `.env` y el YAML. Para saber si el sistema iba a
    tocar algo hacia fuera había que abrir dos ficheros, y acordarse de que el
    que manda no es el del disco sino el que se cargó al arrancar.
    """
    bloque = cliente.get("/api/health").json()["writes"]
    assert set(bloque) >= {"hevy_write_enabled", "telegram_send_enabled", "pending_write"}
    assert isinstance(bloque["hevy_write_enabled"], bool)
    assert isinstance(bloque["telegram_send_enabled"], bool)


def test_health_dice_quien_hay_delante_de_la_puerta(cliente, monkeypatch):
    """El testigo de «me han recreado con el compose equivocado».

    Pasó el 12 de septiembre: el contenedor llevaba el día entero levantado sin
    el fichero de superposición de la LAN. Desde fuera era indistinguible de uno
    sano -200, `status: ok`, planificador con sus cinco trabajos- y por dentro
    montaba el bind mount de Windows en lugar del volumen nombrado, así que las
    copias previas a cada escritura en Hevy vivían en otro sitio del que la guía
    dice. El punto de montaje es `/app/data` en los dos casos, o sea que la
    aplicación no puede ver esa diferencia. Sí puede ver ésta.

    Se comprueban los tres valores y no sólo el de hoy: uno que no puede cambiar
    tranquiliza sin mirar, que es peor que no mirar.
    """
    for puesto, esperado in (("ninguna", "ninguna"), ("proxy", "proxy"), ("", "sin_declarar")):
        monkeypatch.setattr(settings, "auth_front", puesto)
        assert cliente.get("/api/health").json()["auth_front"] == esperado


# ---------------------------------------------------------------------------
# La rutina huerfana en el healthcheck
# ---------------------------------------------------------------------------


def _fila_hevy(day, status, reason, **extra):
    """Una fila de `hevy_writes` con lo mínimo para que el healthcheck la lea."""
    from app.models import HevyWrite

    campos = {
        "date": day,
        "routine_key": "dia_1",
        "hevy_routine_id": "rid-1",
        "status": status,
        "reason": reason,
    }
    campos.update(extra)
    return HevyWrite(**campos)


def test_una_rutina_huerfana_de_hoy_sale_en_el_veredicto(cliente, db, cfg):
    """La frase que hay que poder leer justo antes de entrenar.

    Ya sale por Telegram, y no basta: un mensaje se lee una vez y a las nueve de
    la mañana. Lo que hay en Hevy AHORA MISMO se mira en el móvil al llegar al
    gimnasio, y es ahí donde tiene que estar el aviso.

    Se comprueba que el texto llega ENTERO, no que haya "un problema". El motivo
    se redactó para que sirva para actuar -«Abre Hevy y NO hagas X: hoy toca Y»-
    y lleva los títulos reales de las dos rutinas; un veredicto que dijera
    «revisar Hevy» y nada más no sirve para decidir qué hacer al llegar.
    """
    hoy = datetime.now(ZoneInfo(cfg.timezone)).date()
    db.add(_fila_hevy(
        hoy, "stale",
        "esta mañana se escribió «Día 1» en Hevy con una decisión que ya no "
        "vale, y hoy toca «Recuperación». No se ha podido deshacer. Abre Hevy "
        "y NO hagas «Día 1»: hoy toca «Recuperación»",
    ))
    db.commit()

    cuerpo = cliente.get("/api/health").json()

    assert cuerpo["status"] == "revisar"
    assert cuerpo["writes"]["stale_write"], "el bloque no trae la frase"
    assert any("NO hagas «Día 1»" in p for p in cuerpo["problemas"]), (
        f"el aviso no llega con lo que hay que hacer: {cuerpo['problemas']}"
    )


def test_la_huerfana_de_ayer_ya_no_avisa(cliente, db, cfg):
    """Se pregunta por HOY, y por eso se apaga sola.

    Mañana el trabajo de las 09:00 vuelve a escribir y lo que hubiera se pisa,
    así que la fila `stale` de ayer ya no describe lo que hay en la app. Un
    aviso que no se pueda cerrar nunca acaba encendido siempre, y un aviso
    encendido siempre deja de leerse justo antes del día en que hacía falta.
    """
    hoy = datetime.now(ZoneInfo(cfg.timezone)).date()
    db.add(_fila_hevy(hoy - timedelta(days=1), "stale", "lo de ayer"))
    db.commit()

    cuerpo = cliente.get("/api/health").json()

    assert cuerpo["writes"]["stale_write"] is None
    assert not any("lo de ayer" in p for p in cuerpo["problemas"])


def test_si_despues_de_la_huerfana_se_reescribio_bien_no_se_avisa(cliente, db, cfg):
    """Manda la ÚLTIMA fila del día, no la primera que sea `stale`.

    La secuencia existe: a las 10:30 el check-in tardío sale rojo, la reversión
    falla y queda `stale`; a las 13:00 el usuario rehace el check-in, sale verde
    y se reescribe la rutina buena. En Hevy hay lo correcto, y avisar de que hay
    una rutina huérfana sería mentir sobre el estado de otra aplicación.

    Buscar «algún `stale` de hoy» daría el resultado contrario y seguiría
    pareciendo razonable, que es lo que lo hace peligroso.
    """
    hoy = datetime.now(ZoneInfo(cfg.timezone)).date()
    db.add(_fila_hevy(hoy, "stale", "quedó el Día 1 y no se pudo deshacer"))
    db.commit()
    db.add(_fila_hevy(hoy, "ok", "reescrita tras el segundo check-in"))
    db.commit()

    cuerpo = cliente.get("/api/health").json()

    assert cuerpo["writes"]["stale_write"] is None, (
        "se avisa de una rutina huérfana que ya se corrigió: el aviso no mira "
        "la última escritura del día, sino cualquiera"
    )


def test_una_huerfana_sin_motivo_guardado_avisa_igual(cliente, db, cfg):
    """El aviso no puede depender de que el motivo se escribiera.

    `reason` es una columna añadida a posteriori, así que una fila `stale`
    anterior a ella la tiene a NULL. Callarse en ese caso sería perder el aviso
    entero por no tener el texto bonito, cuando el hecho -en Hevy hay algo que
    hoy no toca- se sabe igual.
    """
    hoy = datetime.now(ZoneInfo(cfg.timezone)).date()
    db.add(_fila_hevy(hoy, "stale", None))
    db.commit()

    cuerpo = cliente.get("/api/health").json()

    assert cuerpo["status"] == "revisar"
    assert cuerpo["writes"]["stale_write"], (
        "una fila `stale` sin motivo no avisa de nada: el aviso cuelga del "
        "texto en vez de colgar del estado"
    )


def test_un_dia_normal_no_inventa_huerfanas(cliente, db, cfg):
    """El control. Sin esto, los tres de arriba pasarían con `stale_write` fijo."""
    hoy = datetime.now(ZoneInfo(cfg.timezone)).date()
    db.add(_fila_hevy(hoy, "ok", "rutina del día escrita"))
    db.commit()

    cuerpo = cliente.get("/api/health").json()
    assert cuerpo["writes"]["stale_write"] is None
    assert cuerpo["writes"]["stale_error"] is None


def test_no_poder_mirar_la_huerfana_no_es_no_tenerla(cliente, db, monkeypatch):
    """El fallo se cuenta, no se traga.

    Un `except` que devolviera «no hay huérfana» haría que una base ilegible y
    un día limpio se vieran EXACTAMENTE igual desde el móvil: healthcheck en
    verde. Es el fallo silencioso de manual, y encima en el sitio cuyo trabajo
    es no tenerlos.

    Y se comprueba que `/api/health` sigue contestando 200: los dos compose
    miran el código HTTP para decidir si el contenedor está enfermo, así que un
    500 aquí cambiaría una degradación avisada por un bucle de reinicios -con
    la base rota, reiniciar no arregla nada-.
    """
    from app import api as mod

    def revienta(*_a, **_k):
        raise RuntimeError("database disk image is malformed")

    monkeypatch.setattr(db, "scalars", revienta)

    r = cliente.get("/api/health")
    assert r.status_code == 200, "un healthcheck que revienta dispara reinicios"
    cuerpo = r.json()

    assert cuerpo["writes"]["stale_write"] is None
    assert cuerpo["writes"]["stale_error"], "no se dice que no se ha podido mirar"
    assert cuerpo["status"] == "revisar"
    assert any("malformed" in p for p in cuerpo["problemas"]), (
        f"el motivo real no llega al veredicto: {cuerpo['problemas']}"
    )


def test_no_poder_leer_la_marca_de_escritura_tampoco_es_no_tenerla(cliente, monkeypatch):
    """`pending_error` llevaba calculándose desde el principio y no lo leía nadie.

    `_estado_escrituras` se toma la molestia de distinguir «no hay marca» de «no
    se ha podido mirar si la hay», y esa distinción se perdía en
    `_problemas_de_salud`, que solo miraba `pending_write`. Las dos salían como
    un healthcheck en verde, o sea que el trabajo de distinguirlas no servía
    para nada.
    """
    def revienta(_raiz):
        raise OSError("permission denied")

    monkeypatch.setattr("app.integrations.hevy.read_pending", revienta)

    cuerpo = cliente.get("/api/health").json()

    assert cuerpo["writes"]["pending_error"]
    assert cuerpo["status"] == "revisar"
    assert any("permission denied" in p for p in cuerpo["problemas"]), (
        f"el motivo real no llega al veredicto: {cuerpo['problemas']}"
    )
