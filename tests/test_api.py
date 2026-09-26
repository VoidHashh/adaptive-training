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
    # La caché de previsualizar vive en el módulo y dura diez minutos, así que
    # sin vaciarla sobrevive de un test al siguiente: el primero que
    # previsualice el LUNES deja ahí su lectura y el resto de la batería
    # decidiría con datos de otro test. Un diccionario nuevo por test, que
    # `monkeypatch` además deshace al terminar.
    monkeypatch.setattr("app.scheduler._previsualizaciones", {})

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
        "garmin_fetch", "decision_fallback", "recompute_early", "reconcile",
        "perception_notice", "watchdog",
        "backfill_wellness", "reconcile_arranque", "startup_audit",
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


def test_la_salud_dice_que_codigo_esta_corriendo(cliente, monkeypatch):
    """La otra mitad de `config_file`, que no la contestaba nadie.

    `config_file` dice si el YAML del disco es el que decide. Esto dice si el
    ARREGLO de ayer es el que decide, y esa pregunta se hace después de cada
    arreglo. No se podía contestar mirando el sistema: la etiqueta de la imagen
    lleva decenas de versiones en `0.1.0`, así que un contenedor de la semana
    pasada y uno reconstruido hace un minuto se ven idénticos desde fuera.
    """
    from app.settings import settings as s

    monkeypatch.setattr(s, "build_sha", "69de011")
    monkeypatch.setattr(s, "build_date", "2026-09-16T12:00:00+02:00")
    cuerpo = cliente.get("/api/health").json()

    assert cuerpo["build"]["sha"] == "69de011"
    assert cuerpo["build"]["date"] == "2026-09-16T12:00:00+02:00"
    assert cuerpo["build"]["unknown"] is False


def test_una_imagen_sin_marca_lo_dice_y_no_se_declara_enferma(cliente):
    """Dos exigencias que tiran en sentidos contrarios, y las dos importan.

    Sin marca hay que DECIRLO: callar dejaría creer que el `status: ok` de
    arriba cubre también «y es el código de ayer», que es justo lo que no
    cubre. Pero no puede contar como problema de salud: en local nunca hay
    marca, y un desarrollo que se autodiagnostica enfermo por no ser una imagen
    enseña a ignorar el diagnóstico, que es peor que no tenerlo.

    El cliente de pruebas no lleva marca, así que este es el caso por defecto y
    no hace falta montarlo.
    """
    cuerpo = cliente.get("/api/health").json()

    assert cuerpo["build"]["unknown"] is True
    assert cuerpo["build"]["sha"] is None
    assert cuerpo["build"]["note"]
    assert not any("construcción" in p for p in cuerpo["problemas"]), (
        f"no saber la versión no es una avería: {cuerpo['problemas']}"
    )


def test_el_dockerfile_escribe_la_marca_que_los_ajustes_leen(cliente):
    """Los dos extremos del cable, que viven en ficheros distintos y en
    lenguajes distintos: el `ARG`/`ENV` del `Dockerfile` y el campo de
    `Settings`. Si los nombres dejan de coincidir no falla nada, no avisa
    nadie, y `/api/health` dice para siempre «no se sabe qué código es este»
    con toda la cadena montada.
    """
    from pathlib import Path

    from app.settings import Settings

    texto = Path("Dockerfile").read_text(encoding="utf-8")
    for campo in ("build_sha", "build_date"):
        assert campo in Settings.model_fields
        var = campo.upper()
        assert f"ARG {var}=" in texto, f"el Dockerfile ya no declara ARG {var}"
        assert f"{var}=${var}" in texto, f"el Dockerfile no pasa {var} al ENV"


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

    monkeypatch.setattr(
        mod, "_estado_planificador",
        lambda sched, error=None: {"running": True, "jobs": {}, "error": None},
    )
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

    # Dos argumentos: `_estado_planificador` recibe el planificador y su error,
    # no la petición. Cambió al sacar `estado_de_salud` del endpoint para que el
    # trabajo de vigilancia de las 09:45 pudiera mirar lo mismo que la pantalla,
    # y ese trabajo corre en el hilo del planificador, donde no hay ninguna
    # petición de la que sacarlo.
    monkeypatch.setattr(mod, "_estado_planificador", lambda sched, error=None: {
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
    # Las tres aserciones de abajo aprueban a la vez con cero preguntas: la
    # primera porque `cfg.pregunta_keys()` lee `checkin_preguntas` con un
    # `.get(..., [])` y sería `[] == []`; la segunda porque `all([])` es cierto;
    # la tercera porque la intersección con el vacío es vacía. O sea que el día
    # que esa sección se renombre en el `config.yaml`, el formulario sale sin
    # preguntas y este test lo aplaude. Así que primero: que haya.
    assert claves, (
        "el formulario no trae ni una pregunta. Las tres comprobaciones de "
        "abajo aprueban igual, ninguna habría mirado nada."
    )
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


def test_una_rotacion_normal_no_marca_ninguna_opcion(cliente, db, cfg):
    """El control, y sin él lo de arriba no demuestra nada.

    Una marca que saliera siempre no distinguiría el caso que quiere señalar:
    sería decoración fija al lado de las cinco opciones.

    Y éste es el único del bloque que puede aprobar en vacío. Sus hermanos leen
    `por_clave["dia_1"]` y reventarían con un `KeyError` el día que el selector
    devolviera cero opciones; éste afirma con un `all(...)`, y `all([])` es
    cierto. O sea que un selector roto del todo -que es el fallo más gordo que
    puede tener esta pantalla- dejaría el control en verde diciendo que no hay
    nada mal marcado, porque no habría nada que mirar. Por eso se comprueba
    primero que las opciones están, y contra el config, que es de donde salen.
    """
    _hizo(db, ["dia_3", "dia_2", "dia_1"])

    sel = cliente.get(f"/api/checkin/today?day={LUNES}").json()["selector"]
    # Contra el config NO SIRVE: las dos listas salen de la misma
    # `cfg.opciones_selector()`, así que un config sin opciones las deja
    # iguales -`[] == []`- y la comparación aprueba a la vez que el `all`. Lo
    # que hay que exigir son los tres días que este test acaba de sembrar, que
    # es lo único que no puede desaparecer sin que la afirmación cambie de
    # significado.
    claves = [o["key"] for o in sel["opciones"]]
    assert claves == cfg.opciones_selector(), (
        f"el selector no ofrece las opciones del config: {sel['opciones']}"
    )
    assert {"dia_1", "dia_2", "dia_3"} <= set(claves), (
        f"el selector solo ofrece {claves}: los tres días sembrados no están, "
        "y el `all(...)` de abajo aprueba sin mirar una sola marca"
    )
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
# La previsualización
# ---------------------------------------------------------------------------
#
# LO QUE SE PROTEGE AQUÍ ES QUE MIRAR NO SEA HACER.
#
# `POST /api/preview` contesta a "¿qué decidirías con esto?" y no decide nada:
# ni guarda el check-in, ni deja decisión vigente, ni escribe en Hevy, ni manda
# un Telegram. Todo lo de esta sección son formas distintas de comprobar esa
# misma frase, porque es la única que sostiene el uso que tiene: probar
# respuestas hasta entender dónde están los umbrales.
#
# El fallo que estos tests existen para impedir no daría ningún error. Una
# previsualización que escribiera el check-in se vería igual en pantalla, y lo
# que rompería está tres meses más allá: `checkins` es de donde salen los
# percentiles de los umbrales adaptativos, así que una mañana probando cuatro
# fatigas distintas metería cuatro lecturas inventadas en la ventana y el
# sistema se calibraría contra respuestas que nadie dio nunca.


def _filas(db, modelo, day=None):
    from sqlalchemy import select as _select

    q = _select(modelo)
    if day is not None:
        q = q.where(modelo.date == day)
    return list(db.scalars(q).all())


def test_previsualizar_no_guarda_ni_el_checkin_ni_la_decision(cliente, db):
    """La frase entera, comprobada por ausencia en las dos tablas que importan."""
    from app.models import Checkin as CheckinRow, Decision as DecisionRow
    from app.models import Preview as PreviewRow

    r = cliente.post("/api/preview", json={"day": str(LUNES), "fatigue": 3})
    assert r.status_code == 200, r.text

    assert _filas(db, CheckinRow, LUNES) == [], (
        "previsualizar ha guardado el check-in: esas respuestas tentativas "
        "entran en la ventana de los umbrales adaptativos"
    )
    assert _filas(db, DecisionRow, LUNES) == [], (
        "previsualizar ha dejado una decisión: el día queda decidido sin que "
        "nadie lo haya enviado"
    )
    # Y lo que sí se guarda, se guarda: la previsualización misma, que es el
    # dato de calibración.
    assert len(_filas(db, PreviewRow, LUNES)) == 1


def test_previsualizar_no_deja_una_fila_nueva_en_ninguna_otra_tabla(cliente, db):
    """El invariante entero, y no solo en las dos tablas que uno se acuerda de mirar.

    `pensar_el_dia` hoy solo lee, pero eso es una propiedad que se pierde sin
    querer: basta con que alguien meta ahí dentro un `save_*`, o con que una
    función que ya se llama desde ahí empiece a sembrar una fila por su cuenta.
    El test de arriba mira `checkins` y `decisions` porque son las que rompen la
    calibración; este cuenta TODAS, que es lo que convierte «mirar no es hacer»
    en algo comprobable en vez de una intención.
    """
    from sqlalchemy import func as _func, select as _select

    from app.models import Base

    def censo():
        return {
            t.name: db.scalar(_select(_func.count()).select_from(t))
            for t in Base.metadata.sorted_tables
        }

    antes = censo()
    r = cliente.post("/api/preview", json={"day": str(LUNES), "fatigue": 3})
    assert r.status_code == 200, r.text
    despues = censo()

    crecieron = {t for t, n in despues.items() if n != antes[t]}
    assert crecieron == {"previews"}, (
        f"previsualizar ha escrito donde no debía: {sorted(crecieron - {'previews'})}"
    )


def test_la_previsualizacion_dice_con_todas_las_letras_lo_que_no_ha_hecho(cliente):
    """Y también lo que SÍ ha hecho, que es lo que la hace creíble.

    La petición del usuario era "que no me quede duda de si ya está hecho o
    no". Un `escrito: false` a secas cumpliría la letra y sería mentira: la
    fila de `previews` se escribe. Así que se contesta desglosado -qué no se ha
    tocado, y qué sí- porque un sistema que miente en lo pequeño para sonar
    tranquilizador es exactamente el que no se puede usar para calibrar.
    """
    c = cliente.post("/api/preview", json={"day": str(LUNES), "fatigue": 3}).json()

    assert c["ejecutado"] is False
    assert c["checkin_guardado"] is False
    assert c["decision_guardada"] is False
    assert c["hevy"] == "sin tocar"
    assert c["telegram"] == "sin tocar"
    # Lo único que sí se ha escrito, dicho por su nombre y con su identificador.
    assert c["previsualizacion_guardada"] is True
    assert isinstance(c["preview_id"], int)


def test_la_previsualizacion_no_se_puede_confundir_con_un_envio(cliente):
    """El fallo de pantalla que esto cierra, y que no daría ningún error.

    `static/app.js::pintarResultado` elige qué tarjeta pinta mirando `decided`.
    Si la respuesta de aquí llevara esa clave -o `checkin_saved`- una
    previsualización caída por error en esa función anunciaría «Guardado, pero
    sin decidir»: dos afirmaciones falsas seguidas, en la pantalla que existe
    justamente para que no quede duda de si ya está hecho.

    Con nombres distintos, no encaja. Y este test es lo que impide que alguien
    los unifique más adelante buscando coherencia.
    """
    from app.api import CLAVES_DEL_ENVIO

    for ruta, cuerpo, codigo in (
        ("/api/preview", {"day": str(LUNES), "fatigue": 3}, 200),
        # Las dos salidas de error tienen que cumplirlo igual: son las que se
        # pintan con más prisa y menos mirando.
        (
            "/api/preview",
            {"day": str(LUNES), "lower_discomfort": 7, "requested_session": "full"},
            409,
        ),
    ):
        r = cliente.post(ruta, json=cuerpo)
        assert r.status_code == codigo, r.text
        c = r.json()
        c = c.get("detail", c)
        assert CLAVES_DEL_ENVIO.isdisjoint(c), (
            f"la respuesta {codigo} lleva claves del envío: "
            f"{sorted(CLAVES_DEL_ENVIO & set(c))}"
        )


def test_previsualizar_ni_siquiera_fabrica_los_clientes_de_fuera(
    cliente, monkeypatch
):
    """No basta con no llamarlos: no se construyen.

    `_clientes` lee el `.env` y monta el cliente de Hevy y el de Telegram. Si la
    ruta lo llamara, un fallo posterior de cualquier clase dejaría abierta la
    puerta por la que esta batería ya se escapó una vez a producción -ver el
    test del cerrojo de red, arriba-. Se sustituye por algo que revienta: la
    única forma de que este test pase es que nadie lo llame.
    """
    def explota(cfg_):
        raise AssertionError("previsualizar ha fabricado los clientes de fuera")

    monkeypatch.setattr("app.api._clientes", explota)

    r = cliente.post("/api/preview", json={"day": str(LUNES), "fatigue": 3})
    assert r.status_code == 200, r.text


def test_lo_que_solo_existe_al_previsualizar_no_entra_en_las_senales(cliente, db):
    """El campo de opinión que viaja de incógnito como si fuera del cuerpo.

    `disagreed` y compañía llegan por el mismo cuerpo JSON que la fatiga y la
    lumbar. Si se colaran en `respuestas`, irían a `signals.values`, que es el
    espacio de nombres donde se evalúan las reglas del semáforo: un booleano de
    opinión con voto en el color del día, y encima uno que quedaría escrito en
    el histórico como si fuera una señal más.
    """
    from app.api import CAMPOS_QUE_NO_SON_RESPUESTAS
    from app.models import Preview as PreviewRow

    cliente.post("/api/preview", json={
        "day": str(LUNES),
        "fatigue": 3,
        "disagreed": True,
        "disagreement_reason": "me encuentro mejor de lo que dice",
    })

    fila = _filas(db, PreviewRow, LUNES)[0]
    respuestas = json.loads(fila.answers_json)
    decision = json.loads(fila.decision_json)

    for campo in CAMPOS_QUE_NO_SON_RESPUESTAS:
        assert campo not in respuestas, (
            f"{campo!r} se ha guardado como si fuera una respuesta del "
            f"formulario"
        )
        assert campo not in decision["inputs"]["values"], (
            f"{campo!r} ha llegado al espacio de nombres de las reglas"
        )

    # Y el desacuerdo sí queda, en su sitio y no en el de las señales.
    assert fila.disagreed is True
    assert fila.disagreement_reason == "me encuentro mejor de lo que dice"


def test_la_segunda_previsualizacion_del_dia_se_marca_como_revision(cliente, db):
    """«No quiero que la segunda tape a la primera».

    La diferencia entre las dos ES el dato: qué respuesta se cambió y cuánto
    movió eso la decisión. Por eso se numeran en vez de sobrescribirse, y por
    eso la pantalla tiene que poder decir que esto ya es una revisión.
    """
    from app.models import Preview as PreviewRow

    primera = cliente.post(
        "/api/preview", json={"day": str(LUNES), "fatigue": 3}
    ).json()
    segunda = cliente.post(
        "/api/preview", json={"day": str(LUNES), "fatigue": 8}
    ).json()

    assert (primera["seq"], primera["revision"]) == (1, False)
    assert (segunda["seq"], segunda["revision"]) == (2, True)
    assert primera["preview_id"] != segunda["preview_id"]

    filas = _filas(db, PreviewRow, LUNES)
    assert len(filas) == 2, "la segunda previsualización ha tapado a la primera"
    assert [json.loads(f.answers_json)["fatigue"] for f in filas] == [3, 8]


def test_subir_en_rojo_sin_confirmar_devuelve_la_pregunta_y_no_la_sesion(
    cliente, db
):
    """El día peligroso, con una hernia L4-L5 de por medio.

    No se impide: se pregunta. Y mientras no se conteste no hay sesión que
    enseñar, así que la respuesta es un 409 con las dos sesiones puestas para
    que la pantalla pueda redactar la pregunta sin volver a calcular nada.

    NO se guarda fila. No es un olvido: sin decisión no hay nada que guardar
    -`ConfirmacionNecesaria` salta dentro del motor, antes de que exista- y el
    intento sin confirmar no dice nada que no diga la fila confirmada que viene
    dos segundos después.
    """
    from app.models import Preview as PreviewRow

    r = cliente.post("/api/preview", json={
        "day": str(LUNES),
        "lower_discomfort": 7,
        "requested_session": "full",
    })

    assert r.status_code == 409, r.text
    detalle = r.json()["detail"]
    assert detalle["confirmacion_necesaria"] is True
    assert detalle["propuesta"] == "recovery"
    assert detalle["pedida"] == "full"
    # Aunque no haya sesión, el contrato de "no se ha tocado nada" se mantiene.
    assert detalle["ejecutado"] is False

    assert _filas(db, PreviewRow, LUNES) == []


def test_confirmando_la_subida_en_rojo_queda_marcada_como_forzada(cliente, db):
    """Y ahora sí sale la sesión pedida, con su marca puesta en los datos.

    La marca no es para impedirlo después: es para poder mirar cuántas veces se
    subió el día que el semáforo decía que no, que es la tercera de las medidas
    que esto existe para dar.
    """
    from app.models import Preview as PreviewRow

    c = cliente.post("/api/preview", json={
        "day": str(LUNES),
        "lower_discomfort": 7,
        "requested_session": "full",
        "confirm_upgrade": True,
        "override_reason": "es solo agujetas",
    }).json()

    assert c["light"] == "red"
    assert c["decision"]["session"]["kind"] == "full"

    fila = _filas(db, PreviewRow, LUNES)[0]
    assert fila.override_session_type == "full"
    assert fila.forced_on_red is True
    assert fila.light == "red"


def test_previsualizar_y_luego_enviar_enlaza_las_dos(cliente, db):
    """La pregunta de "¿se llegó a hacer?", contestada sin un booleano aparte.

    Es la segunda medida del usuario: quién tenía razón, medido contra cómo
    fue la sesión. Sin el enlace, una previsualización con anulación y una
    ejecutada de verdad se cuentan igual, y el número de "anulaciones que
    acabaron ocurriendo" sale de la nada.
    """
    from app import repository as repo_
    from app.models import Preview as PreviewRow

    prev = cliente.post(
        "/api/preview", json={"day": str(LUNES), "fatigue": 3}
    ).json()
    enviado = cliente.post(
        "/api/checkin", json={"day": str(LUNES), "fatigue": 3}
    ).json()
    assert enviado["decided"] is True, enviado.get("error")

    decision = repo_.current_decision(db, LUNES)
    fila = db.get(PreviewRow, prev["preview_id"])
    assert fila.decision_id == decision.id


def test_una_previsualizacion_que_no_se_envia_se_queda_sin_decision(cliente, db):
    """La otra mitad del enlace, que es la que le da significado.

    Si se enlazara siempre -o si el enlace se rellenara con la decisión de
    cualquier día- la medida diría que todo se ejecuta y no mediría nada. Un
    `None` aquí es una previsualización que se miró y no se mandó, y esas son
    justamente las que cuentan cuando el usuario cambia de idea al verlo.
    """
    from app.models import Preview as PreviewRow

    cliente.post("/api/preview", json={"day": str(LUNES), "fatigue": 3})

    fila = _filas(db, PreviewRow, LUNES)[0]
    assert fila.decision_id is None


def test_el_envio_tambien_acepta_la_anulacion_y_la_deja_escrita(cliente, db):
    """«Que se envíe lo que yo decida, registrado como anulación».

    Sin esto, la previsualización sería un callejón sin salida: el usuario ve
    que quiere otra cosa, y para conseguirla tiene que saltarse el sistema. Un
    desacuerdo que obliga a saltarse el sistema es un desacuerdo que no se
    puede medir.
    """
    from app import repository as repo_

    enviado = cliente.post("/api/checkin", json={
        "day": str(LUNES),
        "fatigue": 3,
        "requested_session": "reduced",
        "override_reason": "vengo justo de tiempo",
    }).json()
    assert enviado["decided"] is True, enviado.get("error")
    assert enviado["kind"] == "reduced"

    # Se lee con el mismo accesor que usa producción, para que el test recorra
    # el camino de verdad y no una copia suya que puede quedarse atrás.
    fila = repo_.current_decision(db, LUNES)
    anulacion = repo_.planned_session(fila)["anulacion"]
    assert anulacion["propuesta"] == "full"
    assert anulacion["pedida"] == "reduced"
    assert anulacion["motivo"] == "vengo justo de tiempo"
    # Bajar de intensidad no se marca como forzado ni aunque el día fuera rojo:
    # lo que se vigila es subir.
    assert anulacion["forzada_en_rojo"] is False


def test_el_envio_tampoco_sube_en_rojo_sin_que_se_lo_confirmen(cliente, db):
    """La misma guarda en la puerta por la que se escribe de verdad.

    Tenerla solo en la previsualización sería peor que no tenerla: daría la
    impresión de que el sistema pregunta, mientras el camino que sí escribe en
    Hevy la esquiva entera.
    """
    from app.models import Decision as DecisionRow

    r = cliente.post("/api/checkin", json={
        "day": str(LUNES),
        "lower_discomfort": 7,
        "requested_session": "full",
    })

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["pedida"] == "full"
    # Y el check-in NO se ha guardado a medias: o entra todo o no entra nada.
    assert _filas(db, DecisionRow, LUNES) == []


# ---------------------------------------------------------------------------
# «No estoy de acuerdo», declarado sobre una previsualización que ya se ha visto
# ---------------------------------------------------------------------------
#
# `PreviewIn` ya acepta `disagreed`, y aun así hace falta esta ruta: cuando se
# manda el POST que crea la previsualización todavía no se ha visto nada, y de lo
# que no se ha visto no se discrepa. Son dos PUERTAS al mismo hecho -la columna
# es la misma- y lo que cambia es CUÁNDO se sabe.
#
# Lo que no se hace, y es lo que más importa de este bloque: no se inserta una
# fila nueva. Volver a previsualizar relee Garmin y vuelve a correr el motor, así
# que la fila nueva puede traer OTRA decisión; el juicio quedaría pegado a una
# tarjeta que nadie vio, `seq` subiría y `revision` se encendería anunciando un
# cambio de respuestas que no hubo. Nada de eso da error, y las tres medidas que
# esta tabla existe para dar saldrían torcidas a la vez.


def _una_previsualizacion(cliente) -> dict:
    r = cliente.post("/api/preview", json={"day": str(LUNES), "fatigue": 3})
    assert r.status_code == 200, r.text
    return r.json()


def test_el_desacuerdo_se_apunta_en_la_fila_que_se_estaba_mirando(cliente, db):
    """Anota la fila existente. Ni una fila más, ni un `seq` movido."""
    from app.models import Preview as PreviewRow

    prev = _una_previsualizacion(cliente)

    r = cliente.post(
        f"/api/preview/{prev['preview_id']}/desacuerdo",
        json={"disagreed": True, "reason": "el lumbar está bien hoy"},
    )
    assert r.status_code == 200, r.text

    filas = _filas(db, PreviewRow, LUNES)
    assert len(filas) == 1, (
        "declarar el desacuerdo ha insertado una fila nueva: el juicio queda "
        "pegado a una previsualización que nadie ha visto"
    )
    assert filas[0].id == prev["preview_id"]
    assert filas[0].seq == 1
    assert filas[0].disagreed is True
    assert filas[0].disagreement_reason == "el lumbar está bien hoy"


def test_lo_que_contesta_es_lo_que_ha_quedado_guardado(cliente):
    """Relee la fila, no repite el cuerpo.

    La pantalla pinta la confirmación con esto, así que un eco del cuerpo
    enseñaría como apuntado un motivo que en la base de datos no está. Aquí se ve
    con el motivo en blanco, que es el caso donde las dos cosas se separan.
    """
    prev = _una_previsualizacion(cliente)

    d = cliente.post(
        f"/api/preview/{prev['preview_id']}/desacuerdo",
        json={"disagreed": True, "reason": "   \n  "},
    ).json()

    assert d["disagreement_reason"] is None, (
        "un motivo de solo espacios se ha guardado como si fuera un motivo"
    )
    assert d["disagreed"] is True
    assert d["preview_id"] == prev["preview_id"]
    assert d["seq"] == 1
    assert d["day"] == str(LUNES)


def test_declarar_el_desacuerdo_no_ejecuta_nada(cliente, db):
    """El mismo desglose que las otras tres salidas, y por el mismo motivo.

    La pantalla pinta esta cabecera con lo que informe la respuesta, hecho por
    hecho. Una respuesta que no informara dejaría la tarjeta diciendo «no se
    sabe» justo después de tocar un botón, que es cuando la pregunta «¿ha pasado
    algo?» más se hace.
    """
    from app.models import Checkin as CheckinRow
    from app.models import Decision as DecisionRow

    prev = _una_previsualizacion(cliente)

    d = cliente.post(
        f"/api/preview/{prev['preview_id']}/desacuerdo",
        json={"disagreed": True},
    ).json()

    assert d["ejecutado"] is False
    assert d["checkin_guardado"] is False
    assert d["decision_guardada"] is False
    assert d["hevy"] == "sin tocar"
    assert d["telegram"] == "sin tocar"
    # Y que sea verdad, no solo que lo diga.
    assert _filas(db, CheckinRow, LUNES) == []
    assert _filas(db, DecisionRow, LUNES) == []


def test_un_cuerpo_sin_decir_si_se_discrepa_no_pasa(cliente):
    """`disagreed` NO tiene defecto, y ésa es toda la decisión de `DesacuerdoIn`.

    Con `disagreed: bool = True`, un cuerpo vacío -una petición a medias, un
    reintento raro del navegador- apuntaría un desacuerdo que nadie declaró. Eso
    no rompe nada hoy y estropea justo el número que esta tabla existe para dar:
    cuántas veces se discrepó.
    """
    prev = _una_previsualizacion(cliente)
    ruta = f"/api/preview/{prev['preview_id']}/desacuerdo"

    assert cliente.post(ruta, json={}).status_code == 422
    assert cliente.post(ruta, json={"reason": "sin decir que no"}).status_code == 422
    # Y nada que no sea del modelo: un `reasson` mal escrito tiene que cantar,
    # no guardarse un desacuerdo mudo.
    assert cliente.post(
        ruta, json={"disagreed": True, "reasson": "ups"}
    ).status_code == 422


def test_se_puede_retirar_el_desacuerdo(cliente, db):
    """`disagreed: false` vuelve a dejarlo sin marcar, motivo incluido.

    Hace falta porque el botón se puede pulsar sin querer, y un desacuerdo
    declarado por error que no se pueda quitar es ruido permanente en la única
    medida que no se puede reconstruir después.
    """
    from app.models import Preview as PreviewRow

    prev = _una_previsualizacion(cliente)
    ruta = f"/api/preview/{prev['preview_id']}/desacuerdo"

    cliente.post(ruta, json={"disagreed": True, "reason": "me he colado"})
    d = cliente.post(ruta, json={"disagreed": False}).json()

    assert d["disagreed"] is False
    assert d["disagreement_reason"] is None, (
        "se ha quitado el desacuerdo y el motivo se ha quedado colgando"
    )
    fila = db.get(PreviewRow, prev["preview_id"])
    db.refresh(fila)
    assert fila.disagreed is False
    assert fila.disagreement_reason is None


def test_discrepar_de_una_fila_que_no_existe_da_404_y_lo_dice(cliente):
    """Y con el desglose puesto, que es lo que la pantalla del error va a pintar.

    Sin él, el 404 saldría en la tarjeta con los cinco hechos en «no se sabe»
    detrás de un botón que no toca nada.
    """
    r = cliente.post("/api/preview/9999/desacuerdo", json={"disagreed": True})

    assert r.status_code == 404
    detalle = r.json()["detail"]
    assert "9999" in detalle["error"]
    assert detalle["ejecutado"] is False
    assert detalle["checkin_guardado"] is False
    assert detalle["hevy"] == "sin tocar"


def test_el_desacuerdo_no_toca_las_respuestas_de_la_previsualizacion(cliente, db):
    """«Sin tocar respuestas», literal del encargo.

    Si discrepar cambiara alguna, la fila dejaría de servir para la medida que
    importa: en qué umbral se concentra el desacuerdo.

    Se recorren TODAS las columnas y se exceptúan las dos del juicio, en vez de
    nombrar las que no deben cambiar. Es la diferencia entre un test que mira lo
    que se le dijo que mirara y uno que se entera de una columna nueva: esta
    tabla va a crecer -las medidas de calibración todavía no están escritas- y
    una lista a mano se quedaría corta sin que nada lo dijera.
    """
    from sqlalchemy import inspect as sa_inspect

    from app.models import Preview as PreviewRow

    JUICIO = {"disagreed", "disagreement_reason"}

    prev = _una_previsualizacion(cliente)
    fila = db.get(PreviewRow, prev["preview_id"])
    columnas = [c.key for c in sa_inspect(PreviewRow).mapper.column_attrs]
    assert JUICIO < set(columnas), "las columnas del juicio han cambiado de nombre"
    antes = {c: getattr(fila, c) for c in columnas if c not in JUICIO}

    cliente.post(
        f"/api/preview/{prev['preview_id']}/desacuerdo",
        json={"disagreed": True, "reason": "hoy me encuentro bien"},
    )

    db.refresh(fila)
    despues = {c: getattr(fila, c) for c in columnas if c not in JUICIO}
    assert despues == antes, (
        f"declarar el desacuerdo ha tocado "
        f"{ {c for c in antes if antes[c] != despues[c]} }"
    )


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


def test_si_revienta_DESPUES_de_mandar_el_telegram_se_dice_que_se_mando(
    cliente, monkeypatch
):
    """La pantalla del fallo afirmaba dos cosas que no podía saber.

    El 18 de septiembre de 2026 la decisión reventó al apuntar el aviso en
    `notifications` -el `UNIQUE` que seguía vivo en la base desplegada-, o sea
    DESPUÉS de escribir en Hevy y DESPUÉS de que Telegram entregara el mensaje.
    La PWA pintó "no se ha tocado la rutina de Hevy ni se ha enviado ningún
    mensaje" mientras el móvil tenía el mensaje delante.

    Esa frase estaba escrita a mano en `app.js` y no miraba nada: era verdad si
    el fallo ocurría pronto y mentira si ocurría tarde, y el caso tardío es
    justo el que deja efectos fuera de la base de datos, que son los únicos que
    el `rollback` no deshace. Decirle a alguien que no se mandó un mensaje que
    sí se mandó es peor que no decirle nada: le hace rehacer el check-in.

    Así que el servidor cuenta hasta dónde llegó, y el que no llegó a empezar lo
    dice también.
    """
    from app import runner

    real = runner.run_daily

    def revienta_al_final(*a, **kw):
        res = real(*a, **kw)
        # El estado que tenía `res` cuando saltó el `UNIQUE`: Hevy escrito y
        # Telegram entregado.
        assert res.telegram_status in {"sent", "dry_run", "skipped"}
        raise runner.DecisionInterrumpida(RuntimeError("UNIQUE constraint"), res)

    monkeypatch.setattr("app.runner.run_daily", revienta_al_final)

    cuerpo = cliente.post(
        "/api/checkin", json={"day": str(LUNES), "fatigue": 3}
    ).json()

    assert cuerpo["decided"] is False
    assert "UNIQUE" in cuerpo["error"]
    # Lo que la pantalla necesita para no inventarse nada.
    assert cuerpo["hevy"] is not None, (
        "sin el estado de Hevy la pantalla vuelve a tener que adivinar, que es "
        "exactamente de donde venía la frase falsa"
    )
    assert cuerpo["telegram"] is not None


def test_si_revienta_ANTES_de_empezar_se_dice_que_no_se_toco_nada(
    cliente, monkeypatch
):
    """La otra mitad, que es la que hace útil a la primera.

    Un servidor que contestara "no se sabe" siempre sería igual de inútil que
    uno que miente: el día que de verdad no se ha tocado nada hay que poder
    decirlo, porque es cuando reenviar el check-in es la respuesta correcta.
    """
    def revienta(cfg_, day):
        raise RuntimeError("Garmin ha devuelto 429")

    monkeypatch.setattr("app.scheduler._fetch_garmin", revienta)

    cuerpo = cliente.post(
        "/api/checkin", json={"day": str(LUNES), "fatigue": 3}
    ).json()

    assert cuerpo["decided"] is False
    assert cuerpo["hevy"] == "skipped", (
        "fallar leyendo Garmin es fallar antes de tocar nada, y eso no es lo "
        "mismo que no saberlo"
    )
    assert cuerpo["telegram"] == "skipped"

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


def test_umbral_contesta_con_todo_lo_que_hace_falta_para_pintar(cliente, db):
    """La vista 6 entera por la puerta de la API, con la PWA sin hacer cuentas.

    Lo importante de este test no es que salga 200: es que salgan TODAS las
    piezas que `pintarUmbral` va a buscar. Si el endpoint se dejara una, la
    pantalla no daría error -el navegador pinta `undefined` tan tranquilo- y el
    fallo llegaría al móvil disfrazado de hueco.
    """
    _sembrar_metricas(db)
    r = cliente.get("/api/metrics/umbral?dias=90")
    assert r.status_code == 200
    d = r.json()

    assert d["vista"] == "umbral"
    assert d["metodo"] == "spearman"
    assert d["ventana"]["dias"] == 90

    e = d["encabezado"]
    assert "a partir de cuánta bici" in e["pregunta"].lower()
    assert e["estado"] in {"con_datos", "flojo", "sin_datos"}
    assert "salidas medidas" in e["resumen"]
    # La moneda del encabezado es la salida, no el día: con 40 salidas en una
    # ventana de 90 días, `de` NO puede ser 90.
    #
    # Y son 39 de 40, no 40 de 40: la salida de ayer todavía no tiene la HRV de
    # la mañana siguiente, porque esa mañana es hoy. Se cae del numerador y
    # sigue contando en el denominador, que es exactamente lo que tiene que
    # pasar: es un dato que va a llegar, no un dato que falte.
    assert e["n"] == 39
    assert e["de"] == 40
    assert d["salidas"]["fuera"]["sin_hrv_despues"] == 1

    # Las dos preguntas, cada una con su parte del payload.
    assert set(d["umbral"]) >= {"tramos", "frontera", "retardo", "na"}
    assert {c["titulo"] for c in d["recuperacion"]["curvas"]}
    assert len(d["recuperacion"]["curvas"]) == 2
    assert d["recuperacion"]["aislamiento"] == 2
    assert set(d["grafica"]) >= {"puntos", "salidas", "rango", "corte", "altura_corte"}

    # Y el recuento de descartes, que es lo que hace creíble a todo lo demás.
    assert d["salidas"]["medidas"] == 39
    assert set(d["salidas"]["fuera"]) == {
        "sin_carga",
        "sin_hrv_antes",
        "sin_hrv_despues",
    }
    assert set(d["salidas"]["sin_cobertura"]) == {"antes", "despues"}


def test_con_bici_todos_los_dias_el_umbral_dice_por_que_no_hay_curva(cliente, db):
    """Ninguna salida está aislada, y eso se escribe en vez de salir un cero.

    `_sembrar_metricas` mete una salida CADA día. Con `DIAS_AISLAMIENTO` a dos,
    ni una sola salida tiene los dos días limpios que la curva necesita, así que
    la respuesta correcta no es una curva plana en cero -que se leería como "la
    bici no te hace nada"- sino el motivo escrito.
    """
    _sembrar_metricas(db)
    d = cliente.get("/api/metrics/umbral?dias=90").json()

    assert d["salidas"]["aisladas"] == 0
    # El motivo escrito solo existe si la curva sigue viniendo. Una respuesta
    # sin curvas deja este bucle sin dar una vuelta y el test aprueba diciendo
    # que el motivo está escrito en ninguna parte, que es precisamente la
    # pantalla en blanco que este test existe para impedir.
    curvas = d["recuperacion"]["curvas"]
    assert curvas, "la respuesta no trae curvas: el bucle de abajo no comprueba nada"
    for c in curvas:
        assert c["n_salidas"] == 0
        assert c["na"], c["titulo"]
        assert "aislada" in c["na"]
        # Ni un cero de relleno en ninguna barra.
        assert all(b["media"] is None for b in c["por_dia"])
        # `na` y `aviso` son excluyentes: con un motivo escrito no hay además un
        # aviso de muestra corta sobre una curva que no existe.
        assert c["aviso"] is None


def test_el_umbral_acepta_pearson_y_se_nota(cliente, db):
    _sembrar_metricas(db)
    d = cliente.get("/api/metrics/umbral?dias=90&metodo=pearson").json()
    assert d["metodo"] == "pearson"
    fr = d["umbral"]["frontera"]
    if fr["continua"] is not None:
        assert fr["continua"]["metodo"] == "pearson"


def _rutas_de_metricas(*, con_parametro: str | None = None) -> list[str]:
    """Las rutas de métricas SACADAS de la app, no escritas a mano aquí.

    Una lista a mano de rutas es un agujero con forma de test verde: se añade una
    vista, se olvida meterla en la lista, y los tres tests de abajo siguen
    pasando sin haberla mirado nunca. Ya pasó con los dos contadores de
    `test_pwa.py`, y `/api/metrics/umbral` habría entrado por la misma puerta.

    Con `con_parametro` se filtra por lo que la firma DECLARA, que es justo la
    distinción que ya estaba escrita en prosa: las vistas que correlacionan
    llevan `metodo` y las que cuentan disparos no. Derivarla de la firma en vez
    de repetirla quiere decir que quitarle el `metodo` a una vista mueve el test
    solo, en lugar de dejarlo comprobando un mando que ya no existe.
    """
    rutas = []
    for r in app.routes:
        camino = getattr(r, "path", "")
        if not camino.startswith("/api/metrics/") or "GET" not in getattr(
            r, "methods", set()
        ):
            continue
        if con_parametro is not None and con_parametro not in {
            p.name for p in getattr(r, "dependant", None).query_params
        }:
            continue
        rutas.append(camino)
    assert rutas, "no se ha encontrado ninguna ruta de métricas en la app"
    return sorted(rutas)


def test_las_rutas_de_metricas_derivadas_son_las_que_hay(cliente):
    """El derivador tiene que fallar si se rompe, no devolver una lista vacía.

    Sin esto, un cambio en FastAPI que dejara `dependant` en otro sitio haría que
    los filtros de abajo no encontraran nada y los bucles pasaran de largo sin
    comprobar ni una ruta. Un test que no mira nada pasa siempre.
    """
    todas = _rutas_de_metricas()
    assert len(todas) >= 7
    assert "/api/metrics/umbral" in todas

    # Las que correlacionan llevan `metodo`; `auditoria` y `percepcion` no, y eso
    # está razonado en sus propios tests. Aquí solo se comprueba que el filtro
    # separa de verdad en dos grupos y ninguno se queda vacío.
    con = _rutas_de_metricas(con_parametro="metodo")
    assert "/api/metrics/auditoria" not in con
    assert "/api/metrics/percepcion" not in con
    assert "/api/metrics/umbral" in con
    assert 0 < len(con) < len(todas)


def test_sin_datos_las_metricas_contestan_200_con_los_motivos(cliente):
    """Una sección de métricas vacía NO es un error: es el primer día.

    Contestar 404 o 500 con la base recién creada dejaría la pantalla en blanco
    justo cuando lo útil es ver qué falta y cuánto. Sale un 200 con las siete
    parejas y su motivo, que es lo que se pidió: nada oculto, nada aplazado.
    """
    from app.analysis.encabezados import POR_VISTA

    for ruta in _rutas_de_metricas():
        r = cliente.get(ruta)
        assert r.status_code == 200, ruta
        # Y con la base vacía, que es cuando más falta hace, cada respuesta dice
        # qué vista es. Dos de ellas llegaron a contestar 200 sin decirlo.
        assert r.json().get("vista"), ruta
        # El encabezado lo llevan todas menos la portada, que no necesita uno
        # porque ES un encabezado entera. La excepción se saca del registro y no
        # se escribe aquí: si algún día la portada pasara a llevarlo, este test
        # empieza a exigírselo solo.
        if ruta.rsplit("/", 1)[-1] in POR_VISTA:
            assert r.json().get("encabezado"), ruta
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
    for ruta in _rutas_de_metricas(con_parametro="metodo"):
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
    for ruta in _rutas_de_metricas():
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


# ---------------------------------------------------------------------------
# El formulario de despues de entrenar
# ---------------------------------------------------------------------------


def _mete_entreno(db, dia, *, ejercicios):
    """Un entreno de Hevy ya guardado, para no depender de la red."""
    import json as _json

    from app.models import WorkoutLog

    db.add(WorkoutLog(
        hevy_workout_id="w-test",
        date=dia,
        routine_key="dia_1",
        title="Día 1",
        raw_json=_json.dumps({"id": "w-test", "exercises": list(ejercicios)}),
    ))
    db.commit()


def test_la_pantalla_de_despues_trae_el_vocabulario_y_no_lo_lleva_escrito(cliente):
    """La PWA no puede llevar ni una opcion escrita a mano.

    Misma regla que los deslizadores del check-in: si las opciones vivieran en
    el JavaScript, anadir una en Python la dejaria fuera del formulario y el
    usuario no podria contestarla sin que nada fallara.
    """
    from app.engine.feedback import FALTA, HECHO, RESPUESTAS_FALTA, RESPUESTAS_HECHO

    r = cliente.get("/api/sesion/hoy", params={"day": LUNES.isoformat()})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["respuestas"][HECHO] == RESPUESTAS_HECHO
    assert d["respuestas"][FALTA] == RESPUESTAS_FALTA


def test_sin_entreno_lo_dice_en_vez_de_devolver_una_lista_vacia(cliente):
    r = cliente.get("/api/sesion/hoy", params={"day": LUNES.isoformat()})
    assert r.json()["hay_sesion"] is False


def test_se_guarda_y_se_puede_rectificar(cliente):
    dia = LUNES.isoformat()
    primero = cliente.post("/api/sesion/feedback", json={
        "day": dia, "rpe": 7, "lower_discomfort_after": 4,
        "ejercicios": [{"key": "a", "estado": "hecho", "respuesta": None}],
    })
    assert primero.status_code == 200, primero.text

    segundo = cliente.post("/api/sesion/feedback", json={
        "day": dia, "rpe": 9, "lower_discomfort_after": 6,
        "nota": "la espalda tras el peso muerto",
        "ejercicios": [
            {"key": "a", "estado": "hecho", "respuesta": "molestia_lumbar"},
        ],
    })
    assert segundo.status_code == 200, segundo.text

    d = cliente.get("/api/sesion/hoy", params={"day": dia}).json()
    assert d["guardado"]["enviado"] is True
    assert d["guardado"]["rpe"] == 9
    assert d["guardado"]["nota"] == "la espalda tras el peso muerto"


def test_una_respuesta_inventada_se_rechaza_con_422(cliente):
    """Y no un 500: lo que ha llegado mal es la peticion, no el servidor."""
    r = cliente.post("/api/sesion/feedback", json={
        "day": LUNES.isoformat(),
        "ejercicios": [{"key": "a", "estado": "hecho", "respuesta": "me_dio_pereza"}],
    })
    assert r.status_code == 422
    assert "me_dio_pereza" in r.text


def test_un_campo_con_errata_se_rechaza_en_vez_de_perderse(cliente):
    """`extra=forbid`: `rpé` en vez de `rpe` se guardaria a None en silencio."""
    r = cliente.post("/api/sesion/feedback", json={
        "day": LUNES.isoformat(), "esfuerzo": 7,
    })
    assert r.status_code == 422


def test_la_respuesta_guardada_sobrevive_pero_el_estado_se_recalcula(cliente, db):
    """Si entre dos envios se apunta en Hevy lo que faltaba, cambia el estado.

    Lo que cuesta escribir es la RESPUESTA, asi que esa se conserva. El
    `estado` sale del cruce recien hecho: seguir ofreciendo el desplegable de
    «¿por que no esta?» sobre un ejercicio que ya esta apuntado seria enseñar
    una pregunta que ya no tiene sentido.
    """
    dia = LUNES.isoformat()
    cliente.post("/api/sesion/feedback", json={
        "day": dia,
        "ejercicios": [
            {"key": "x", "name": "Equis", "estado": "falta",
             "respuesta": "molestia_lumbar"},
        ],
    })
    d = cliente.get("/api/sesion/hoy", params={"day": dia}).json()
    # Sin plan ni entreno el ejercicio ya no sale en el cruce, y eso tambien es
    # correcto: el formulario describe la sesion que hay, no la que hubo.
    assert d["hay_sesion"] is False
    assert d["guardado"]["enviado"] is True


def test_ninguna_ruta_de_api_se_declara_despues_del_montaje_de_estaticos():
    """`StaticFiles` en "/" se traga todo lo que no haya casado ANTES que el.

    El fichero lo avisa donde se monta, y aun asi el 25/09/2026 los dos
    endpoints del formulario de despues nacieron pegados al final del modulo y
    contestaron 404 desde el primer momento. El sintoma es traicionero: no hay
    error de importacion, no hay ruta duplicada, la funcion existe y el
    decorador se ejecuta. Solo que nunca casa.

    Un aviso en un comentario no basta para algo que se rompe pegando codigo al
    final de un fichero, que es lo mas natural del mundo. Esto si.
    """
    from starlette.routing import Mount

    from app.api import app

    raiz = next(
        (i for i, r in enumerate(app.routes)
         if isinstance(r, Mount) and r.path in ("", "/")),
        None,
    )
    if raiz is None:  # sin `static/` delante no hay montaje y no hay nada que vigilar
        pytest.skip("no hay montaje en la raíz en este entorno")

    tarde = [
        r.path for r in app.routes[raiz + 1:]
        if getattr(r, "path", "").startswith("/api")
    ]
    assert not tarde, (
        f"estas rutas se declaran DESPUÉS del montaje en «/» y por eso "
        f"contestan 404: {tarde}. Muévelas por encima de la sección de "
        f"estáticos de `app/api.py`"
    )


def test_las_cuatro_preguntas_generales_se_guardan_y_vuelven(cliente):
    """Las cinco escalas y las tres elecciones, ida y vuelta.

    Una por una, porque el fallo tipico aqui es que un campo nuevo se declare
    en el modelo y no se copie en el `guardar_feedback`: se aceptaria el envio,
    devolveria 200 y el dato se perderia en silencio.
    """
    dia = LUNES.isoformat()
    r = cliente.post("/api/sesion/feedback", json={
        "day": dia,
        "rpe": 7,
        "lower_discomfort_after": 6,
        "training_desire_after": 4,
        "satisfaccion": 8,
        "cantidad": "corta",
        "tecnica": "se_iba",
        "mas_costoso": "peso_muerto",
        "ejercicios": [
            {"key": "peso_muerto", "name": "Peso muerto", "estado": "hecho",
             "respuesta": None},
        ],
    })
    assert r.status_code == 200, r.text

    g = cliente.get("/api/sesion/hoy", params={"day": dia}).json()["guardado"]
    assert g["rpe"] == 7
    assert g["lower_discomfort_after"] == 6
    assert g["training_desire_after"] == 4
    assert g["satisfaccion"] == 8
    assert g["cantidad"] == "corta"
    assert g["tecnica"] == "se_iba"
    assert g["mas_costoso"] == "peso_muerto"


def test_el_que_mas_costo_tiene_que_estar_en_la_sesion(cliente):
    """422 y no un guardado silencioso de una clave que no existe."""
    r = cliente.post("/api/sesion/feedback", json={
        "day": LUNES.isoformat(),
        "mas_costoso": "dominadas",
        "ejercicios": [{"key": "peso_muerto", "estado": "hecho"}],
    })
    assert r.status_code == 422
    assert "dominadas" in r.text


def test_una_eleccion_inventada_se_rechaza(cliente):
    r = cliente.post("/api/sesion/feedback", json={
        "day": LUNES.isoformat(), "cantidad": "regular",
    })
    assert r.status_code == 422


def test_los_vocabularios_de_sesion_tambien_viajan_al_formulario(cliente):
    """Las elecciones viajan CON su enunciado, y los desplegables con sus textos:
    la pantalla no lleva ni una pregunta escrita."""
    from app.engine.feedback import CANTIDAD, ELECCIONES, PREGUNTA_MAS_COSTOSO, TECNICA

    d = cliente.get("/api/sesion/hoy", params={"day": LUNES.isoformat()}).json()
    por_clave = {e["key"]: e for e in d["elecciones"]}
    assert por_clave["cantidad"]["opciones"] == CANTIDAD
    assert por_clave["tecnica"]["opciones"] == TECNICA
    assert [e["enunciado"] for e in d["elecciones"]] == [e["enunciado"] for e in ELECCIONES]
    assert d["textos"]["mas_costoso"] == PREGUNTA_MAS_COSTOSO


# ---------------------------------------------------------------------------
# El recálculo al abrir (25/09/2026)
# ---------------------------------------------------------------------------


def test_recalcular_llama_a_la_misma_puerta_que_el_scheduler(cliente, monkeypatch):
    """No es una copia de `job_decision`: es `recalcular_si_hace_falta`, que
    pasa por él con su cerrojo. Y con el día de HOY, que es el único que tiene
    sentido rehacer al abrir la app; lo que conteste se devuelve tal cual."""
    llamadas = []

    def espia(cfg, **kw):
        llamadas.append(kw)
        return {"estado": "sigue_sin_dato", "tono": "ojo", "aviso": "x"}

    monkeypatch.setattr("app.scheduler.recalcular_si_hace_falta", espia)
    r = cliente.post("/api/decision/recalcular")

    assert r.status_code == 200
    assert r.json() == {"estado": "sigue_sin_dato", "tono": "ojo", "aviso": "x"}
    (kw,) = llamadas
    assert kw["day"] == date.today()


def test_recalcular_no_se_dispara_con_un_get(cliente):
    """Cuando actúa escribe en Hevy y manda un Telegram: no va detrás de un
    verbo que cualquier precargador de enlaces puede disparar solo."""
    assert cliente.get("/api/decision/recalcular").status_code in (404, 405)


def test_el_checkin_del_movil_espera_al_mismo_cerrojo_que_el_scheduler(monkeypatch):
    """Tres caminos deciden el día y solo dos compartían cerrojo (25/09/2026).

    Un check-in enviado justo a la hora del reintento podía decidir a la vez que
    él: dos decisiones, dos escrituras en Hevy y dos Telegram. Se sujeta el
    cerrojo desde fuera y se comprueba que el envío espera en vez de entrar.
    """
    import threading

    import app.api as api
    import app.scheduler as sched

    entro = threading.Event()
    monkeypatch.setattr(api, "_decidir_sin_cerrojo", lambda *a, **k: entro.set() or {})

    with sched._DECIDIENDO:
        hilo = threading.Thread(
            target=lambda: api._decidir(None, None, date.today(), source="checkin"),
            daemon=True,
        )
        hilo.start()
        assert not entro.wait(0.5), "el check-in ha decidido con el cerrojo cogido"
    assert entro.wait(10), "al soltar el cerrojo el check-in no ha decidido"
    hilo.join(10)


# ---------------------------------------------------------------------------
# «Hoy» abre con la decisión: `decision_de_hoy` (25/09/2026)
# ---------------------------------------------------------------------------


def test_sin_decision_el_dia_no_trae_decision(cliente):
    assert cliente.get(f"/api/checkin/today?day={LUNES}").json()["decision_de_hoy"] is None


def test_con_el_dia_decidido_viaja_la_decision_con_la_forma_de_la_tarjeta(cliente):
    """La misma forma que la respuesta del envío: la pantalla la pinta con la
    misma función, y dos formas para lo mismo acabarían pintándose distinto."""
    enviado = cliente.post("/api/checkin", json={"day": str(LUNES), "fatigue": 4}).json()
    d = cliente.get(f"/api/checkin/today?day={LUNES}").json()["decision_de_hoy"]

    assert d["decided"] is True
    assert d["light"] == enviado["light"]
    assert d["session"] == enviado["session"]
    assert d["kind"] == enviado["kind"]
    assert d["cuando"].startswith("Decidido a las ") and "con tu check-in" in d["cuando"]


def test_hevy_y_telegram_salen_de_lo_guardado_y_si_no_hay_fila_no_se_sabe(cliente, db):
    """Sin cliente de Hevy en el test no hay escritura: `None`, que la pantalla
    dice «no se sabe», y no un «skipped» inventado. Con fila, su estado tal cual."""
    from app.models import HevyWrite, Notification

    cliente.post("/api/checkin", json={"day": str(LUNES), "fatigue": 4})
    d = cliente.get(f"/api/checkin/today?day={LUNES}").json()["decision_de_hoy"]
    plan = repo.planned_session(repo.current_decision(db, LUNES))
    escrita = (
        db.query(HevyWrite).filter_by(date=LUNES, routine_key=plan["routine"])
        .order_by(HevyWrite.id.desc()).first()
    )
    avisado = (
        db.query(Notification).filter_by(date=LUNES, kind="decision")
        .order_by(Notification.id.desc()).first()
    )
    assert d["hevy"] == (escrita.status if escrita else None)
    assert d["telegram"] == (avisado.status if avisado else None)

    db.add(HevyWrite(date=LUNES, routine_key=plan["routine"], status="ok"))
    db.add(Notification(date=LUNES, kind="decision", channel="telegram", status="sent"))
    db.commit()
    d = cliente.get(f"/api/checkin/today?day={LUNES}").json()["decision_de_hoy"]
    assert (d["hevy"], d["telegram"]) == ("ok", "sent")


def test_al_recalcular_se_devuelve_la_decision_nueva_para_repintar(cliente, monkeypatch):
    """La tarjeta que la pantalla tenía delante deja de ser la vigente: sin la
    decisión nueva en la respuesta, seguiría enseñando el ámbar de antes."""
    monkeypatch.setattr(
        "app.scheduler.recalcular_si_hace_falta",
        lambda cfg, **kw: {"estado": "recalculado", "tono": "bien", "aviso": "x"},
    )
    monkeypatch.setattr("app.api._decision_de_hoy", lambda s, cfg, day: {"light": "green", "dia": str(day)})

    r = cliente.post("/api/decision/recalcular").json()
    assert r["decision_de_hoy"] == {"light": "green", "dia": str(date.today())}


def test_si_no_se_recalcula_no_se_manda_decision(cliente, monkeypatch):
    monkeypatch.setattr(
        "app.scheduler.recalcular_si_hace_falta",
        lambda cfg, **kw: {"estado": "nada_que_recalcular", "aviso": None},
    )
    assert "decision_de_hoy" not in cliente.post("/api/decision/recalcular").json()


def test_un_dia_de_bici_dice_que_hevy_no_se_ha_tocado_y_no_que_no_se_sabe(cliente, db):
    """Un día de bici guarda `routine: None`, y buscar su escritura por rutina no
    encontraba nada: la tarjeta decía «Hevy: no se sabe» el día en que el
    servidor sí lo sabía (26/09/2026). Sin fila es que no había nada que
    deshacer -`skipped`-, y con la reversión de la rutina de esta mañana, que
    va con la clave de ESA rutina, es la reversión."""
    from app.models import HevyWrite

    enviado = cliente.post(
        "/api/checkin", json={"day": str(LUNES), "chosen_session": "bici"}
    ).json()
    assert enviado["kind"] == "bici", "el montaje pide un día de bici"

    d = cliente.get(f"/api/checkin/today?day={LUNES}").json()["decision_de_hoy"]
    assert (d["session"], d["kind"]) == ("Bici", "bici")
    assert d["hevy"] == "skipped" == enviado["hevy"]

    db.add(HevyWrite(date=LUNES, routine_key="dia_1", status="ok"))
    db.add(HevyWrite(date=LUNES, routine_key="dia_1", status="reverted"))
    db.commit()
    d = cliente.get(f"/api/checkin/today?day={LUNES}").json()["decision_de_hoy"]
    assert d["hevy"] == "reverted"


def test_en_un_dia_de_bici_la_fila_de_un_bloque_hiit_no_cuenta(cliente, db, monkeypatch):
    """La fila del bloque HIIT la deshace otro camino y no dice cómo quedó la
    rutina de fuerza. Con los bloques apagados en el config real no hay claves
    que apartar, así que se encienden aquí: si no, el filtro no se vería."""
    from app.models import HevyWrite

    monkeypatch.setattr("app.integrations.hevy.claves_hiit", lambda config=None: {"hiit_dia_1"})
    cliente.post("/api/checkin", json={"day": str(LUNES), "chosen_session": "bici"})
    db.add(HevyWrite(date=LUNES, routine_key="dia_1", status="reverted"))
    db.add(HevyWrite(date=LUNES, routine_key="hiit_dia_1", status="ok"))
    db.commit()
    d = cliente.get(f"/api/checkin/today?day={LUNES}").json()["decision_de_hoy"]
    assert d["hevy"] == "reverted"
