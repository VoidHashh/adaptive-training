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
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import repository as repo
from app.api import app, get_config
from app.db import get_session
from app.models import Base
from app.settings import settings
from tests.conftest import LUNES, dias


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

    `_clientes` se sustituye por lo mismo que devolvería sin claves: (None, None).
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
    monkeypatch.setattr("app.api._clientes", lambda cfg_: (None, None))

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

    El `skip` de arriba no es un adorno. Sin claves en el `.env`, `_clientes`
    devuelve (None, None), no se intenta ninguna conexión y el test pasaría sin
    haber comprobado nada -el mismo vacío que ya mordió una vez en este
    proyecto-. Si no hay clientes que construir, aquí no hay nada que demostrar.
    """
    from app.api import _clientes
    from tests.conftest import RedProhibidaEnTests

    if all(c is None for c in _clientes(cfg)):
        pytest.skip("sin claves en el .env no hay clientes reales que bloquear")

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
    monkeypatch.setattr("app.api._clientes", lambda cfg_: (None, None))
    monkeypatch.setattr("app.api.get_config", lambda: cfg)
    app.dependency_overrides[get_config] = lambda: cfg
    try:
        yield
    finally:
        app.dependency_overrides.clear()


def test_al_arrancar_se_montan_los_tres_trabajos_del_dia(arrancada, monkeypatch):
    """El fallo que este test existe para impedir: `build_scheduler` estaba
    escrito, probado y no lo llamaba NADIE en producción. El contenedor arrancaba,
    servía la PWA, contestaba `status: ok` y no ejecutaba ni el refresco de
    Garmin, ni la decisión de las 09:00, ni la reconciliación. Desde fuera es
    idéntico a un día de descanso: no llega mensaje."""
    monkeypatch.setattr(settings, "scheduler_enabled", True)

    with TestClient(app) as c:
        sched = c.get("/api/health").json()["scheduler"]

    assert sched["running"] is True, "la aplicación arrancó sin planificador"
    assert set(sched["jobs"]) == {"garmin_fetch", "decision_fallback", "reconcile"}
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


def test_la_salud_declara_lo_que_falta(cliente):
    """Un sistema arrancado a medias que contesta "ok" es peor que uno caído,
    porque nadie va a mirar."""
    r = cliente.get("/api/health")
    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["status"] == "ok"
    assert "secrets_missing" in cuerpo, (
        "sin esto, un despliegue sin claves parece sano hasta que no llega el "
        "mensaje de la mañana"
    )
    assert cuerpo["config_hash"]
    assert "dry_run" in cuerpo


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
    """
    from app.api import CheckinIn

    del_config = {s["key"] for s in cfg.raw.get("checkin_sliders", [])}
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
