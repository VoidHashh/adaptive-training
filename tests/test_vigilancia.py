"""El trabajo que avisa de lo que ESTÁ mal, no de lo que acaba de pasar.

LA DIFERENCIA QUE LO MOTIVA
---------------------------
Hasta el 18 de septiembre de 2026, Telegram avisaba de cosas que OCURREN: la
decisión del día, un trabajo que revienta, un trabajo que no llegó a
ejecutarse. Todas tienen un instante en el que suceden, y el planificador las
ve suceder.

Había una segunda familia que no avisaba nadie: las cosas que ESTÁN. Una
escritura de Hevy que se quedó a medias, una credencial que falta, el
`config.yaml` del disco distinto del que está decidiendo, el reloj del proceso
en otra zona que las reglas. Esas cuatro no ocurren en ningún momento
concreto -o mejor dicho: ocurrieron una vez y se quedaron-, así que ningún
avisador de sucesos las coge. Salían en `/api/health` y en la banda roja de la
pantalla de check-in, o sea que solo se veían MIRANDO.

La que lo volvió urgente es la escritura a medias. Con `dry_run` puesto no
podía pasar. Desde que el sistema escribe en Hevy de verdad, sí: el proceso
puede morir entre el fichero de marca y el PUT, y entonces hay una rutina en
estado desconocido y el sistema sigue contestando 200 y mandando su mensaje
diario tan contento.

LO QUE SE COMPRUEBA AQUÍ
------------------------
Que avise cuando hay algo, que se calle cuando no, que lo que diga sea
exactamente lo que dice la pantalla, y que no se lleve por delante el
planificador cuando el propio aviso falla.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.integrations.telegram import TelegramClient
from app.models import Base
from app.scheduler import job_vigilancia, mensaje_de_vigilancia
from tests.dobles import doble_de, no_es_doble


@pytest.fixture
def en_memoria(monkeypatch):
    """Ata `session_scope` a una base en memoria, como en `test_scheduler.py`.

    El trabajo abre su propia sesión porque corre en el hilo del planificador y
    no en el de una petición, así que la única forma de probarlo es sustituir
    de dónde la saca.
    """
    import app.scheduler as mod

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as sesion:
        @contextmanager
        def scope():
            yield sesion

        monkeypatch.setattr(mod, "session_scope", scope)
        yield sesion


@doble_de(TelegramClient)
class TelegramFalso:
    """Mismo `send` que el de verdad, sin la llamada HTTP.

    Se intentó declararlo con `salvo=("send",)` y la guarda lo rechazó: la
    firma coincide, así que la exención no excusaba nada y se habría quedado
    tapando el día que sí divergieran. Lo que importa conservar es el
    CONTRATO -devolver un `SendResult` en vez de lanzar cuando Telegram
    rechaza el envío-, que es del que depende la mitad de este fichero.
    """

    def __init__(self, *, revienta: bool = False, rechaza: bool = False):
        self.revienta = revienta
        self.rechaza = rechaza
        self.enviados: list[str] = []

    def send(self, text, *, dry_run=False):
        from app.integrations.telegram import SendResult

        if self.revienta:
            raise RuntimeError("la red se ha caído")
        self.enviados.append(text)
        if self.rechaza:
            return SendResult(sent=False, parts=0, reason="chat_id inválido")
        return SendResult(sent=True, parts=1, reason="")


@no_es_doble(
    "no representa nada de `app/`: hace de `BackgroundScheduler` de APScheduler, "
    "que es una dependencia de fuera. Solo se le piden los dos atributos que "
    "`_estado_planificador` mira"
)
class PlanificadorFalso:
    """Lo mínimo que `_estado_planificador` le pregunta al planificador."""

    running = True

    @no_es_doble("un trabajo de APScheduler visto por los dos campos que se leen")
    class _Trabajo:
        id = "watchdog"
        next_run_time = None

    def get_jobs(self):
        return [self._Trabajo()]


@pytest.fixture
def sano(monkeypatch):
    """Dicta el estado de salud que va a ver el trabajo. Empieza sin nada malo.

    Devuelve un diccionario que cada test retoca -`sano["secretos"] = [...]`- en
    vez de dejar que cada uno reasigne atributos del módulo por su cuenta. La
    primera versión de este fichero hacía lo segundo y estaba mal de una forma
    que no se ve: `api._estado_escrituras = ...` a pelo NO lo deshace nadie al
    acabar el test, así que el siguiente que tocara la salud heredaba el doble.
    Con `monkeypatch` se revierte solo.

    `missing_secrets` se parchea en la CLASE y no en la instancia porque
    `Settings` es un modelo de pydantic y rechaza que le cuelguen un atributo
    que no está declarado como campo.
    """
    import app.api as api

    estado = {
        "escrituras": {
            "hevy_write_enabled": True, "telegram_send_enabled": True,
            "pending_write": None, "pending_error": None,
            "stale_write": None, "stale_error": None,
        },
        "secretos": [],
        "reloj": {
            "timezone": "Europe/Madrid", "offset": "+02:00",
            "matches": True, "error": None,
        },
        "config": {"path": "/app/config.yaml", "in_sync": True, "error": None},
    }

    monkeypatch.setattr(api, "_estado_escrituras", lambda cfg, s: estado["escrituras"])
    monkeypatch.setattr(
        type(api.settings), "missing_secrets", lambda self: estado["secretos"]
    )
    monkeypatch.setattr(api, "_estado_del_reloj", lambda cfg: estado["reloj"])
    monkeypatch.setattr(api, "_estado_del_config", lambda cfg: estado["config"])
    return estado


def _correr(cfg, tg=None, **kwargs):
    return job_vigilancia(
        cfg, sched=PlanificadorFalso(), telegram_client=tg, **kwargs
    )


# ---------------------------------------------------------------------------
# Que se calle cuando no hay nada
# ---------------------------------------------------------------------------


def test_un_sistema_sano_no_manda_nada(en_memoria, cfg, sano):
    """La mitad del valor de este trabajo es el silencio.

    Un vigilante que manda un mensaje todos los días diciendo «todo bien» deja
    de leerse en una semana, y entonces el día que diga otra cosa tampoco se
    lee. El aviso solo vale si su llegada ya es la noticia.
    """
    tg = TelegramFalso()
    problemas = _correr(cfg, tg)

    assert problemas == []
    assert tg.enviados == [], (
        f"el sistema está sano y aun así ha mandado {tg.enviados!r}"
    )


# ---------------------------------------------------------------------------
# Que avise cuando lo hay
# ---------------------------------------------------------------------------


def test_una_escritura_de_hevy_a_medias_llega_por_telegram(en_memoria, cfg, sano):
    """El caso que trajo este trabajo al mundo.

    `hevy.write_routine` deja un fichero de marca antes del PUT y lo borra
    después. Si el proceso muere en medio, la marca sobrevive y quiere decir
    que hay una rutina en estado desconocido -la vieja, la nueva, o media-.
    Antes eso solo se veía abriendo la aplicación.
    """
    sano["escrituras"]["pending_write"] = {
        "routine_id": "abc123", "started_at": "2026-09-18T06:31:02",
    }

    tg = TelegramFalso()
    problemas = _correr(cfg, tg)

    assert len(tg.enviados) == 1, "una escritura a medias tiene que avisar"
    cuerpo = tg.enviados[0]
    assert "abc123" in cuerpo, (
        f"el aviso no dice QUÉ rutina hay que ir a mirar:\n{cuerpo}"
    )
    assert any("abc123" in p for p in problemas)


def test_faltar_una_credencial_tambien_avisa(en_memoria, cfg, sano):
    """Es lo más traicionero, porque el check-in se envía y se guarda igual.

    Sin la clave de Hevy no se escribe la rutina y sin la de Telegram no llega
    el mensaje; desde el móvil las dos cosas son idénticas a un día de
    descanso. (Si la que falta es la de Telegram, este aviso tampoco sale: por
    eso el trabajo deja además una línea de error en el log.)
    """
    sano["secretos"] = ["HEVY_API_KEY"]

    tg = TelegramFalso()
    problemas = _correr(cfg, tg)

    assert len(tg.enviados) == 1
    assert "HEVY_API_KEY" in tg.enviados[0]
    assert any("HEVY_API_KEY" in p for p in problemas)


def test_el_config_del_disco_desincronizado_avisa(en_memoria, cfg, sano):
    """El sistema decide bien y con las reglas de antes, que es lo peor.

    No falla nada: contesta 200, manda su mensaje y aplica el `config.yaml` que
    cargó al arrancar. Lo que ha cambiado es el del disco, o sea la única
    versión que alguien ha leído.
    """
    sano["config"] = {
        "path": "/app/config.yaml", "in_sync": False,
        "error": "el config.yaml del disco no es el cargado",
    }

    tg = TelegramFalso()
    _correr(cfg, tg)

    assert len(tg.enviados) == 1
    assert "config" in tg.enviados[0].lower()


# ---------------------------------------------------------------------------
# Que diga lo mismo que la pantalla
# ---------------------------------------------------------------------------


def test_el_mensaje_no_reescribe_las_frases_de_la_pantalla(en_memoria, cfg, sano):
    """Dos textos para el mismo fallo son dos verdades que pueden discrepar.

    Las frases salen de `_problemas_de_salud`, que es la misma función que
    llena el bloque `problemas` de `/api/health` y la banda roja de la pantalla
    de check-in. Si este trabajo las redactara por su cuenta, el día que
    alguien mejorase una de las dos redacciones habría que averiguar cuál de
    las dos pantallas mira bien.
    """
    sano["secretos"] = ["TELEGRAM_BOT_TOKEN"]

    tg = TelegramFalso()
    problemas = _correr(cfg, tg)

    for frase in problemas:
        assert frase in tg.enviados[0], (
            f"la pantalla dice {frase!r} y el mensaje no lo lleva:\n{tg.enviados[0]}"
        )


def test_el_mensaje_escapa_lo_que_mete_dentro():
    """Un `<` sin escapar deja el aviso de avería mudo, que es el peor momento.

    Telegram contesta 400 «can't parse entities» y el mensaje NO sale. Es
    exactamente el razonamiento que ya está escrito en `_avisador`, y aquí
    vuelve a hacer falta porque estas frases llevan dentro rutas, valores de
    config y trozos de error que vienen de fuera.
    """
    texto = mensaje_de_vigilancia(["la rutina <b>rota</b> & lo que sea"])
    assert "<b>rota</b>" not in texto, "la frase entra sin escapar en el HTML"
    assert "&lt;b&gt;rota&lt;/b&gt;" in texto
    assert "&amp;" in texto


def test_el_mensaje_dice_que_va_a_repetirse():
    """Porque va a repetirse, y sin decirlo se lee como un fallo nuevo cada día.

    Son estados que hay que arreglar a mano. Mandarlos una vez y callar sería
    un aviso que se lee el día que uno está ocupado y no vuelve nunca; sin
    avisar de la repetición, el tercer mensaje idéntico parece que el fallo ha
    pasado tres veces.
    """
    texto = mensaje_de_vigilancia(["algo"])
    assert "cada mañana" in texto and "arregle" in texto


# ---------------------------------------------------------------------------
# Que no se lleve por delante nada cuando el propio aviso falla
# ---------------------------------------------------------------------------


def test_sin_cliente_de_telegram_no_revienta_pero_deja_rastro(
    en_memoria, cfg, sano, caplog
):
    """Reventar aquí mandaría un Telegram de error por el canal que no está.

    Es el razonamiento de `job_aviso_percepcion`, y aquí aprieta más: la
    ausencia de cliente de Telegram es ELLA MISMA una de las cosas de las que
    este trabajo avisa.
    """
    import logging

    sano["secretos"] = ["TELEGRAM_BOT_TOKEN"]

    with caplog.at_level(logging.ERROR):
        problemas = _correr(cfg, None)

    assert problemas, "el problema se devuelve aunque no haya a quién contárselo"
    assert any("vigilancia" in r.message.lower() for r in caplog.records), (
        "sin cliente no se manda nada y tampoco queda escrito: el aviso "
        "desaparece entero y en silencio"
    )


def test_si_telegram_revienta_el_trabajo_no_tumba_el_planificador(
    en_memoria, cfg, sano
):
    """Una excepción aquí la recogería `_avisador`... y volvería a Telegram.

    Se atrapa y se registra, como en la auditoría de arranque. Lo que no se
    hace es tragársela: el trabajo devuelve los problemas igual, así que quien
    lo llame desde un test o desde la consola los ve.
    """
    sano["secretos"] = ["HEVY_API_KEY"]

    problemas = _correr(cfg, TelegramFalso(revienta=True))
    assert problemas, "los problemas se devuelven aunque el envío falle"


def test_un_rechazo_de_telegram_queda_escrito(en_memoria, cfg, sano, caplog):
    """`send` no lanza cuando Telegram dice que no: devuelve un resultado.

    Tirar ese valor a la basura dejaba el aviso de avería sin rastro en ninguna
    parte -ni mensaje, ni línea de log-, que es el modo de fallo exacto que
    este trabajo existe para impedir. Ya pasó dos veces en este fichero:
    `_avisador` y la auditoría de arranque.
    """
    import logging

    sano["secretos"] = ["HEVY_API_KEY"]

    with caplog.at_level(logging.ERROR):
        _correr(cfg, TelegramFalso(rechaza=True))

    assert any("NO se ha enviado" in r.message for r in caplog.records), (
        "Telegram ha rechazado el aviso y no ha quedado constancia"
    )


# ---------------------------------------------------------------------------
# El hueco que este trabajo NO puede tapar
# ---------------------------------------------------------------------------


def test_el_planificador_de_verdad_es_el_que_se_mira(en_memoria, cfg, sano):
    """Saltarse el bloque del planificador avisaría de una avería inventada.

    `_problemas_de_salud` lee un `scheduler` vacío como «no está corriendo». Si
    este trabajo no le pasara el planificador de verdad -y no puede sacarlo de
    una petición HTTP, porque no hay ninguna-, mandaría todos los días un
    Telegram diciendo que nadie va a decidir por la mañana, justo mientras
    decide por la mañana.

    Dicho al revés: este trabajo NO puede avisar de su propio planificador
    muerto, porque entonces él tampoco corre. Ese hueco lo tapan
    `auditar_arranque` y el healthcheck de Docker, no esto.
    """
    tg = TelegramFalso()
    assert _correr(cfg, tg) == []

    # Y sin planificador, lo contrario: se entera.
    problemas = job_vigilancia(cfg, sched=None, telegram_client=tg)
    assert any("planificador" in p for p in problemas)
