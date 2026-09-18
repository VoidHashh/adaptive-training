"""La API HTTP: el check-in de la PWA y las consultas de estado.

El camino que importa es uno solo: se rellena el formulario por la mañana, se
envía, y la decisión se toma EN ESE MOMENTO con el check-in ya dentro. El
trabajo de las 09:00 solo existe por si ese envío no llega.

QUÉ SE DEVUELVE CUANDO ALGO VA MAL
----------------------------------
Un check-in que se guarda pero cuya decisión falla NO puede contestar 200 y
callarse: el usuario cerraría el móvil convencido de que ya está. Se contesta
con el detalle del fallo y se dice explícitamente qué se guardó y qué no.

Un deslizador con un nombre que no existe se RECHAZA, no se ignora. Un `fatiga`
por `fatigue` enviado desde la PWA no se guardaría en ninguna parte y el sistema
decidiría sin ese dato diciendo que el check-in está completo. Hay dos rechazos
distintos porque son dos errores distintos:

- un campo que el modelo no conoce es un 422 de pydantic (`extra="forbid"`);
- un campo que el modelo conoce pero que el `config.yaml` no declara como
  deslizador es un 400 desde `upsert_checkin`.
"""

from __future__ import annotations

import csv
import io
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import repository as repo
from app.config_loader import load_config
from app.db import get_session, init_db
from app.engine.rotacion import pendientes as rutinas_pendientes
from app.engine.session_builder import orden_de_rotacion, siguiente_en_rotacion
from app.models import Decision as DecisionRow
from app.models import WorkoutLog
from app.settings import settings

log = logging.getLogger(__name__)


def _configurar_logs() -> None:
    """Aplica `LOG_LEVEL`. Sin esto la variable no hacía nada dentro de Docker.

    `logging.basicConfig` solo lo llamaba la CLI, así que bajo `uvicorn` -que es
    como corre esto en el Umbrel- el logger raíz se quedaba en su WARNING por
    defecto y todos los `log.info` del sistema se tiraban. Efectos:

    - `LOG_LEVEL=DEBUG` en el `.env`, documentado como la forma de ver por qué el
      motor decidió lo que decidió, no producía ni una línea. Parecía que el
      motor no tuviera nada que contar.
    - `docker compose logs`, que es lo que el README manda mirar cuando algo va
      mal, salía vacío por construcción salvo avisos y excepciones.

    Se toca el logger RAÍZ y no el de `app`: los mensajes interesantes salen de
    `app.runner`, `app.engine.*`, `app.integrations.*` y `apscheduler`, y
    configurar uno por uno garantiza olvidarse de alguno el día que se añada un
    módulo. `force=True` porque uvicorn ya ha instalado sus manejadores para
    entonces y sin él `basicConfig` no haría nada, que sería este mismo fallo
    otra vez.
    """
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )


def _avisar_de_la_puerta(log_: logging.Logger | None = None) -> None:
    """Deja dicho en el log qué protege el acceso, o que no lo protege nada.

    Va por LOG y NO por Telegram a propósito. Telegram es el canal de la
    decisión de la mañana: lo que llega ahí se lee mientras uno se ata las
    zapatillas, y un aviso de despliegue metido en medio o se ignora o
    convierte el mensaje útil en ruido. Esto es un hecho del arranque, y el
    sitio de los hechos del arranque es `docker compose logs`.

    Y va en el arranque y no en una comprobación periódica porque el momento en
    que importa es exactamente ese: cuando alguien levanta esto en una máquina
    nueva. El resto del tiempo la información no ha cambiado.
    """
    log_ = log_ or log
    if settings.auth_front == "proxy":
        log_.info(
            "acceso: hay un proxy con credenciales delante (AUTH_FRONT=proxy); "
            "la aplicación no comprueba nada por su cuenta"
        )
        return

    if settings.auth_front == "ninguna":
        cabeza = (
            "SIRVIENDO SIN AUTENTICACIÓN, y está declarado así a propósito "
            "(AUTH_FRONT=ninguna)."
        )
    else:
        cabeza = (
            "SIRVIENDO SIN AUTENTICACIÓN conocida: nadie ha declarado qué hay "
            "delante (AUTH_FRONT sin poner)."
        )

    # El detalle importa tanto como el titular: "sin autenticación" suena a
    # molestia menor hasta que uno recuerda QUÉ queda abierto.
    log_.warning(
        "%s Cualquiera que alcance este puerto puede leer /api/export -el "
        "histórico entero: sueño, HRV, RPE y el registro de la lumbar- y "
        "llamar a POST /api/checkin, que no solo lee: crea un check-in y "
        "dispara una decisión. DRY_RUN no cubre esto y además está pensado "
        "para quitarse. Si hay un proxy con login delante, ponlo por escrito "
        "con AUTH_FRONT=proxy y este aviso desaparece.",
        cabeza,
    )


def get_config():
    """El `config.yaml`, cargado una vez por proceso."""
    if not hasattr(get_config, "_cache"):
        get_config._cache = load_config(settings.config_path)
    return get_config._cache


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Arranca la base, y con ella los tres trabajos del día.

    El planificador vive AQUÍ y no en un proceso aparte a propósito. Es un
    sistema que decide solo: sin los trabajos montados sirve la PWA, contesta
    `status: ok` y no pasa nada nunca -ni el refresco de Garmin de las 06:30, ni
    la decisión de las 09:00 cuando no hay check-in, ni la reconciliación de las
    22:30-. Eso no se ve desde fuera, y el modo de fallo es idéntico a un día de
    descanso: no llega mensaje.

    Si el planificador no llega a arrancar NO se tumba la aplicación: se anota
    en `app.state` y `/api/health` lo dice. Negarse a arrancar dejaría sin
    formulario, que es lo único que se puede hacer a mano; contestar "ok" sin
    trabajos sería mentir.
    """
    from app.scheduler import build_scheduler

    _configurar_logs()

    init_db()
    # Lo primero del log, antes que nada: cuando algo va mal, la primera
    # pregunta es siempre «¿y esto qué versión es?», y la respuesta tiene que
    # estar arriba del todo y no haber que buscarla.
    marca = _estado_del_build()
    if marca["unknown"]:
        log.info("código: sin marca de construcción (%s)", marca["note"])
    else:
        log.info("código: %s, construido %s", marca["sha"], marca["date"] or "sin fecha")
    _avisar_de_la_puerta()
    faltan = settings.missing_secrets()
    if faltan:
        # Se arranca igual -hay que poder abrir el formulario para ver qué
        # falta- pero no se hace como si nada.
        log.warning("faltan secretos en el .env: %s", ", ".join(faltan))

    app.state.scheduler = None
    app.state.scheduler_error = None
    if settings.scheduler_enabled:
        try:
            cfg = get_config()
            hevy, tg, motivos = _clientes(cfg)
            app.state.scheduler = build_scheduler(
                cfg,
                hevy_client=hevy,
                telegram_client=tg,
                client_errors=motivos,
                dry_run=settings.dry_run,
            )
            log.info(
                "planificador arrancado con %d trabajo(s)",
                len(app.state.scheduler.get_jobs()),
            )
        except Exception as exc:  # noqa: BLE001
            app.state.scheduler_error = str(exc)
            log.exception("el planificador NO ha arrancado")
    else:
        log.warning(
            "planificador desactivado (SCHEDULER_ENABLED=false): no habrá "
            "decisión automática ni reconciliación"
        )

    yield

    if app.state.scheduler is not None:
        # `wait=False`: al parar el contenedor no se espera a que termine un
        # trabajo largo, pero sí se le dice a APScheduler que no lance más.
        app.state.scheduler.shutdown(wait=False)


app = FastAPI(title="Entrenamiento adaptativo", version="0.1.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Modelos de entrada
# ---------------------------------------------------------------------------


class CheckinIn(BaseModel):
    """El formulario de la mañana.

    `extra="forbid"` no es rigor de manual: es el único freno a un fallo que no
    da error. Pydantic ignora por defecto los campos que no conoce, así que un
    `fatiga` por `fatigue` mal escrito en la PWA no llegaría a `valores`, no se
    guardaría en ninguna parte, y el sistema decidiría sin ese dato contestando
    `checkin_saved: true`. El freno lumbar se quedaría sin `lower_discomfort` y
    la única señal sería una decisión con cara de normal.

    `tests/test_api.py::test_los_deslizadores_del_config_y_del_modelo_coinciden`
    comprueba lo simétrico: que un deslizador añadido al `config.yaml` esté
    también aquí. Sin eso la PWA lo dibujaría, el usuario lo movería, y el envío
    entero se rechazaría con un 422.
    """

    model_config = ConfigDict(extra="forbid")

    fatigue: int | None = Field(None, ge=0, le=10)
    mood: int | None = Field(None, ge=0, le=10)
    upper_discomfort: int | None = Field(None, ge=0, le=10)
    lower_discomfort: int | None = Field(None, ge=0, le=10)
    sleep_quality: int | None = Field(None, ge=0, le=10)
    training_desire: int | None = Field(None, ge=0, le=10)
    yesterday_rpe: int | None = Field(None, ge=0, le=10)

    # Las dos preguntas de Sí/No. `bool | None`, y el `None` es un estado de
    # verdad: «no me lo han dicho». Son los días en que no se contesta el
    # formulario, y esos días el sistema tiene que seguir prescribiendo como
    # siempre. Un `bool = False` por defecto los convertiría a todos en días en
    # los que dijiste que no ibas a entrenar.
    #
    # Sin `ge/le` porque no hay rango que validar: Pydantic ya rechaza un 7 aquí.
    # Y por eso el test de simetría de más abajo compara contra la UNIÓN de las
    # dos listas del config, no solo contra `checkin_sliders`.
    wants_to_train: bool | None = None
    will_train: bool | None = None

    # El selector de sesión. Una CADENA, que es lo que lo hace distinto de todo
    # lo de arriba, y por eso no está en `checkin_sliders` ni en
    # `checkin_preguntas`: esas dos listas son las que acaban en `signals.values`,
    # donde todo tiene que ser número o booleano.
    #
    # Sin `Literal[...]` con las cinco opciones a mano, y no por pereza. Las
    # opciones salen de `rotation.order`, que está en el `config.yaml`: fijarlas
    # aquí las pondría en un segundo sitio capaz de decir algo distinto, y el día
    # que el ciclo creciera a cuatro días la pantalla ofrecería el `dia_4` y esto
    # lo rechazaría con un 422 que nadie sabría leer. Quien valida el valor es
    # `repo.upsert_checkin` contra `config_loader.opciones_selector`, que es la
    # misma función que dibuja las opciones. Aquí solo se comprueba que es texto.
    #
    # `None` es el tercer estado otra vez: «no he tocado el selector». No
    # significa «no voy a entrenar» ni «haré lo propuesto»; significa que nadie
    # ha dicho nada, y ese día la rotación propone como siempre.
    chosen_session: str | None = None

    comments: str | None = None
    # Permite rehacer el check-in de ayer sin mentirle a la fecha.
    day: date | None = None


# Los campos que viajan en el mismo JSON que las respuestas y NO son respuestas.
#
# Escrito a mano y aparte por un motivo muy concreto: las respuestas se le pasan
# a `pensar_el_dia` como `respuestas`, y de ahí van a `Checkin.values` y a
# `signals.values`, que es el espacio de nombres de las reglas. Un `disagreed`
# colado ahí dentro sería un booleano de opinión con voto en el color del día, y
# encima quedaría escrito en el histórico con la misma cara que la lumbar.
#
# La exclusión va por lista explícita, y no por un prefijo o un `startswith`,
# porque un campo nuevo que se olvidara de la convención no avisaría de nada:
# entraría en las señales calladamente, que es justo el modo de fallo que esto
# existe para cerrar. `tests/test_api.py` recorre esta lista y comprueba, campo
# a campo, que ninguno llega ni a `answers_json` ni a `inputs.values`.
CAMPOS_QUE_NO_SON_RESPUESTAS = frozenset({
    "disagreed",
    "disagreement_reason",
    "requested_session",
    "confirm_upgrade",
    "override_reason",
})


class EnvioIn(CheckinIn):
    """El check-in más la anulación: lo que acepta `POST /api/checkin`.

    La anulación está en el ENVÍO y no solo en la previsualización a propósito.
    Sin esto, previsualizar sería un callejón sin salida: el usuario ve que el
    sistema propone una cosa, sabe que quiere otra, y para conseguirla tiene que
    saltarse el sistema entero -entrar en Hevy a mano-. Un desacuerdo que obliga
    a saltarse el sistema es un desacuerdo que no queda registrado en ninguna
    parte, y por tanto uno que no se puede medir.

    Hereda de `CheckinIn` en vez de repetir los campos: son literalmente las
    mismas respuestas, y duplicarlas dejaría dos listas capaces de separarse -un
    deslizador nuevo añadido arriba y olvidado aquí daría un 422 en una ruta y
    un 200 en la otra-.
    """

    # Qué sesión se pide en vez de la propuesta: `full`, `reduced` o `recovery`.
    # Sin `Literal[...]`, por lo mismo que `chosen_session`: quien valida el
    # valor es el motor (`session_builder.DUREZA`), que es quien lo usa.
    requested_session: str | None = None
    override_reason: str | None = None
    # Subir de dureza con el semáforo en ROJO. Por defecto `False`, y ese
    # defecto es la guarda entera: sin marcarlo, el motor lanza
    # `ConfirmacionNecesaria` y las dos rutas devuelven la pregunta en vez de la
    # sesión. No es «he leído el aviso» en general; es la respuesta a una
    # pregunta concreta que solo se hace ese día.
    confirm_upgrade: bool = False


class PreviewIn(EnvioIn):
    """Lo mismo que un envío, más el desacuerdo, que solo existe al mirar.

    `disagreed` no está en `EnvioIn` porque no se discrepa de lo que uno acaba
    de mandar: se discrepa de lo que el sistema acaba de ENSEÑAR. Es la
    diferencia entre las dos rutas, y meterlo arriba lo dejaría disponible en un
    sitio donde no significa nada.
    """

    # `None` es «no he dicho nada», que es lo que va a ser casi siempre. Tres
    # estados y no dos: ver `models.Preview.disagreed`.
    disagreed: bool | None = None
    disagreement_reason: str | None = None


class DesacuerdoIn(BaseModel):
    """El juicio sobre una previsualización que ya está en pantalla.

    Modelo propio y no `PreviewIn` recortado: aquí no hay ni una sola respuesta
    del formulario, y heredar de `CheckinIn` dejaría esta ruta aceptando una
    fatiga que no iría a ninguna parte. Un campo aceptado que nadie lee es la
    forma más barata de perder un dato sin enterarse.

    `disagreed` NO tiene defecto a propósito, y es la única guarda de este
    modelo. Con `= True` puesto, un cuerpo vacío por un error de la pantalla
    apuntaría un desacuerdo que nadie declaró, y esa fila contaría en la medida
    de «cuántas veces discrepo» exactamente igual que las de verdad. Sin
    defecto, ese cuerpo se va en un 422 que se ve.

    Los tres estados del modelo -`True`, `False`, `NULL`- siguen vivos en la
    COLUMNA: el `NULL` es no haber pasado por aquí, y por eso no se puede pedir
    desde aquí. «No dije nada» no es algo que se diga.
    """

    model_config = ConfigDict(extra="forbid")

    disagreed: bool
    # `reason` y no `disagreement_reason`: dentro de un cuerpo que solo habla de
    # esto, el prefijo no distingue de nada. En `PreviewIn` sí hace falta,
    # porque allí convive con siete deslizadores y dos preguntas.
    reason: str | None = None


def _respuestas(body: CheckinIn) -> dict[str, Any]:
    """Solo las respuestas del formulario, listas para el motor.

    Los `None` se caen porque en `signals.values` un `None` no es un valor: es
    «no contestado», y el sitio donde eso se representa es la AUSENCIA de la
    clave. Meterlo haría que una regla con `requires: [fatigue]` lo diera por
    presente y comparase contra nada.
    """
    return {
        k: v
        for k, v in body.model_dump(
            exclude=CAMPOS_QUE_NO_SON_RESPUESTAS | {"comments", "day"}
        ).items()
        if v is not None
    }


def _sesion_pedida(body: EnvioIn) -> Any:
    """La anulación del usuario como la entiende el motor, o `None` si no hay.

    `None` y no un `SesionPedida` vacío: el motor distingue «no se pidió nada»
    -el día normal- de «se pidió justo lo que ya proponía», y solo el segundo
    deja rastro de anulación.
    """
    from app.engine.session_builder import SesionPedida

    if not body.requested_session:
        return None
    return SesionPedida(
        tipo=body.requested_session,
        confirmada=bool(body.confirm_upgrade),
        motivo=body.override_reason,
    )


# Lo que contesta cualquier respuesta que NO ha llegado a ejecutar nada.
#
# Va en una constante y no escrito a mano en cada sitio porque son tres salidas
# distintas -la previsualización buena, el 409 de la confirmación y el 502 de
# Garmin- y las tres tienen que decir lo mismo. La petición del usuario era «que
# no me quede duda de si ya está hecho o no», y una de las tres callándose sería
# exactamente esa duda.
#
# Se desglosa en cuatro hechos en vez de un `escrito: false` que sonaría mejor y
# sería mentira: la fila de `previews` SÍ se escribe. Lo que no se ha tocado son
# las respuestas guardadas, la decisión del día, Hevy y Telegram, que son las
# cuatro cosas por las que uno se pregunta si ya está hecho.
#
# `hevy` y `telegram` dicen "sin tocar" y no `null` a posta: `null` es lo que
# `_decidir` usa para «no sé hasta dónde se llegó», y aquí sí se sabe.
#
# Y NINGUNA CLAVE SE LLAMA COMO LAS DEL ENVÍO. No es descuido ni falta de
# criterio: `static/app.js::pintarResultado` decide qué tarjeta pinta mirando
# `decided`, y un `decision_guardada` que se llamara `decided` haría que una
# previsualización cayera en la rama del envío fallido y anunciara "Guardado,
# pero sin decidir" -dos mentiras seguidas- sin que nada fallara. Con nombres
# distintos, una previsualización que llegue por error a esa función no encaja,
# y `tests/test_api.py` lo comprueba clave a clave.
NADA_EJECUTADO: dict[str, Any] = {
    "ejecutado": False,
    "checkin_guardado": False,
    "decision_guardada": False,
    "hevy": "sin tocar",
    "telegram": "sin tocar",
}

# Las claves con las que `pintarResultado` distingue un envío que salió de uno
# que falló. Ninguna respuesta de previsualización puede llevarlas.
CLAVES_DEL_ENVIO = frozenset({"decided", "checkin_saved"})


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health(
    request: Request,
    s: Session = Depends(get_session),
    cfg=Depends(get_config),
) -> dict[str, Any]:
    """Sirve para el healthcheck de Docker y para ver qué falta.

    Devuelve `secrets_missing` en vez de esconderlo: un sistema arrancado a
    medias que contesta "ok" es peor que uno caído, porque nadie va a mirar.

    Y `status` SE CALCULA DE LOS BLOQUES, NO SE ESCRIBE. Estaba puesto a mano a
    `"ok"` y no lo movía nada: el 13 de septiembre este endpoint devolvía a la
    vez `"status": "ok"` y un `config_file` diciendo que el `config.yaml` del
    disco ni siquiera se podía cargar con el código que estaba corriendo. Las
    dos cosas eran ciertas y la primera tapaba a la segunda, porque `status` es
    lo que se mira primero y a veces lo único que se mira.

    Es el mismo fallo que este propio bloque vino a denunciar -«el valor que se
    lee no es el valor que se usa»- cometido por el que avisa. Un resumen que no
    puede salir mal no resume: tranquiliza.

    NO ROMPE EL HEALTHCHECK DE DOCKER, y conviene saber por qué antes de tocar
    esto. Los dos compose comprueban `urlopen(...).status == 200`, o sea el
    código HTTP, no este campo. Así que un `status: "revisar"` se ve desde el
    móvil sin marcar el contenedor como enfermo ni disparar reinicios. Es
    deliberado: un contenedor con el config desincronizado sigue decidiendo bien
    con el que tiene cargado, y reiniciarlo por eso cambiaría una degradación
    avisada por una avería total -precisamente porque el código viejo rechaza el
    YAML nuevo, así que no volvería a levantarse-.
    """
    return estado_de_salud(
        cfg, s,
        sched=getattr(request.app.state, "scheduler", None),
        sched_error=getattr(request.app.state, "scheduler_error", None),
    )


def estado_de_salud(cfg, s: Session, *, sched, sched_error=None) -> dict[str, Any]:
    """El mismo diccionario que sirve `/api/health`, sin depender de HTTP.

    Se saca del endpoint para que el trabajo de vigilancia de las 09:45 mire
    EXACTAMENTE lo que mira la pantalla. La alternativa era que el trabajo se
    hiciera sus propias comprobaciones, y entonces habría dos opiniones sobre la
    salud del sistema que podrían discrepar: el Telegram diciendo que todo va
    bien y la pantalla diciendo que no, sin forma de saber cuál miente.

    Es el mismo argumento que `_problemas_de_salud` ya hacía un nivel más
    abajo -«no vuelve a preguntar nada, lee lo ya calculado»- aplicado un
    escalón más arriba.
    """
    salida = {
        "config_hash": cfg.hash,
        "timezone": cfg.timezone,
        "secrets_missing": settings.missing_secrets(),
        "dry_run": settings.dry_run,
        # Los dos interruptores de escritura, al lado del `dry_run`. Estaban
        # repartidos entre el `.env` y el YAML, y para saber si el sistema iba a
        # tocar algo hacia fuera había que abrir dos ficheros y acordarse de que
        # el que manda es el que se cargó al arrancar, no el que está en disco.
        "writes": _estado_escrituras(cfg, s),
        # Sin esto, una aplicación sin planificador es indistinguible de una
        # sana: sirve la PWA, contesta 200, y no decide nunca. Se dice cuántos
        # trabajos hay y cuándo toca cada uno, porque "arrancado" tampoco basta:
        # un planificador vivo con cero trabajos falla exactamente igual.
        "scheduler": _estado_planificador(sched, sched_error),
        "clock": _estado_del_reloj(cfg),
        "config_file": _estado_del_config(cfg),
        # Y qué código. `config_file` dice si el YAML del disco es el que
        # decide; esto dice si el ARREGLO de ayer es el que decide, que es la
        # otra mitad de la misma pregunta y no la sabía contestar nadie.
        "build": _estado_del_build(),
        # Qué hay delante de la puerta, según quien arrancó esto. Hasta ahora
        # sólo se decía en un WARNING del arranque, y un WARNING del arranque
        # se lee una vez y nunca más.
        #
        # Lo que lo trajo aquí: el 12 de septiembre el contenedor llevaba el día
        # entero recreado SIN el fichero de superposición de la LAN. Desde fuera
        # se veía idéntico -200, `status: ok`, planificador con sus trabajos- y
        # por dentro era otra cosa: montaba el bind mount de Windows en vez del
        # volumen nombrado, así que las copias previas a cada escritura en Hevy
        # estaban en un sitio distinto del que la guía dice, y `AUTH_FRONT` iba
        # sin declarar. Recrearlo con el fichero bueno habría cambiado el
        # directorio de datos debajo de los pies sin avisar.
        #
        # De las dos diferencias, ésta es la única que la aplicación puede ver:
        # el punto de montaje es `/app/data` en los dos casos. Sirve de testigo
        # barato -si aquí pone `sin_declarar` en este PC, el contenedor se
        # levantó con el compose equivocado- y no cuesta nada mirarlo.
        "auth_front": settings.auth_front or "sin_declarar",
    }
    problemas = _problemas_de_salud(salida)
    # El orden importa: `status` primero, y los problemas justo detrás, para que
    # quien abra esto en el móvil lea el veredicto y la razón sin desplazarse.
    return {"status": "revisar" if problemas else "ok",
            "problemas": problemas, **salida}


def _problemas_de_salud(s: dict[str, Any]) -> list[str]:
    """Una frase por cosa que hay que mirar, leyendo los bloques ya calculados.

    NO VUELVE A PREGUNTAR NADA. Lee el diccionario que se acaba de construir, y
    eso es a propósito: si esto hiciera sus propias comprobaciones podrían decir
    una cosa distinta de la que devuelve el endpoint, y entonces habría dos
    verdades sobre el mismo sistema en la misma respuesta.

    LO QUE CUENTA COMO PROBLEMA es lo que hace que el sistema decida mal o no
    decida, no lo que resulta incómodo. Un `dry_run` activo no entra: es un modo
    que se elige. Un `auth_front` sin declarar tampoco: dice cómo se levantó
    esto, no si funciona.
    """
    p: list[str] = []

    faltan = s.get("secrets_missing") or []
    if faltan:
        # Lo más traicionero de la lista, porque el check-in se envía y se
        # guarda igual: sin la clave de Hevy no se escribe la rutina y sin la de
        # Telegram no llega el mensaje, y desde el móvil eso es idéntico a un
        # día de descanso.
        p.append("faltan credenciales: " + ", ".join(faltan))

    plan = s.get("scheduler") or {}
    if plan.get("error"):
        p.append(f"el planificador da error: {plan['error']}")
    elif not plan.get("running"):
        p.append("el planificador NO está corriendo: nadie va a decidir por la mañana")
    elif not plan.get("jobs"):
        # Un planificador vivo con cero trabajos falla exactamente igual que uno
        # parado, y desde fuera se ve más sano.
        p.append("el planificador está corriendo pero sin ningún trabajo puesto")

    reloj = s.get("clock") or {}
    if reloj.get("error"):
        p.append(f"el reloj da error: {reloj['error']}")
    elif reloj.get("matches") is False:
        p.append("el reloj del proceso y el `timezone` del config no deciden "
                 "el mismo día")

    conf = s.get("config_file") or {}
    if conf.get("in_sync") is False:
        p.append(conf.get("error") or "el config.yaml del disco no es el cargado")

    esc = s.get("writes") or {}
    if esc.get("pending_write"):
        # Una escritura marcada y sin cerrar significa que se empezó a tocar
        # Hevy y no consta que terminara. Hay una copia previa esperando.
        #
        # Se sacan los dos campos que hacen falta para ir a mirarlo en vez de
        # volcar el diccionario entero. En Python el volcado se lee -mal, pero se
        # lee-; el mismo valor viajaba por JSON hasta la PWA, que lo metía en una
        # plantilla de cadena y escribía «[object Object]». Que las dos pantallas
        # digan lo mismo empieza por que el dato salga ya redactado de aquí.
        marca = esc["pending_write"]
        if isinstance(marca, dict):
            detalle = ", ".join(
                trozo
                for trozo in (
                    f"rutina {marca['routine_id']}" if marca.get("routine_id") else "",
                    f"empezada a las {marca['started_at']}"
                    if marca.get("started_at")
                    else "",
                    str(marca.get("note") or ""),
                )
                if trozo
            ) or str(marca)
        else:
            detalle = str(marca)
        p.append(f"hay una escritura a medias sin cerrar: {detalle}")
    if esc.get("pending_error"):
        # Estaba calculándose y no lo leía nadie. `_estado_escrituras` se toma
        # la molestia de distinguir «no hay marca» de «no se ha podido mirar si
        # la hay», y luego esa distinción se perdía aquí: las dos salían como
        # un healthcheck en verde. No saber es un problema, no una ausencia.
        p.append(esc["pending_error"])
    if esc.get("stale_write"):
        # El check-in tardío que no se pudo deshacer. En Hevy hay AHORA MISMO
        # una sesión que el sistema ya ha decidido que hoy no toca, y el único
        # que puede arreglarlo es quien abra la app. Sale también por Telegram,
        # pero un mensaje se lee una vez y a las nueve de la mañana; la pantalla
        # se mira justo antes de entrenar, que es cuando importa.
        #
        # Se apaga solo al día siguiente, porque se pregunta por HOY. Un aviso
        # que no se pueda cerrar nunca acaba siendo un aviso que no se lee.
        p.append(esc["stale_write"])
    if esc.get("stale_error"):
        # Mismo motivo que `pending_error`: no haber podido mirar no es haber
        # mirado y no haber encontrado nada.
        p.append(esc["stale_error"])

    return p


def _estado_escrituras(cfg, s: Session) -> dict[str, Any]:
    """Qué puede tocar el sistema hacia fuera, y si hay algo a medias.

    LA MARCA PENDIENTE. `hevy.write_routine` escribe un fichero justo antes del
    PUT y lo borra justo después. Existe para el caso en que el proceso muera
    entre las dos cosas: entonces la marca sobrevive y dice que hay una rutina en
    estado desconocido -puede ser la vieja, la nueva, o media escritura-. Estaba
    escribiéndose desde el principio y no la leía **nadie**: ni un endpoint, ni
    el mensaje de la mañana, ni un aviso. Una señal que se emite y nadie escucha
    es igual de útil que no emitirla, y peor, porque parece que sí.

    Aquí es donde tenía que estar: el healthcheck es lo único que se mira sin
    que haya pasado algo. Si aparece `pending`, el sistema NO está sano aunque
    conteste 200, y por eso el `status` de arriba se queda en "ok" pero este
    bloque lo dice con todas las letras.

    LA RUTINA HUÉRFANA (`stale_write`). El otro estado que hay que ver desde el
    móvil sin haber pasado nada. Cuando el check-in tardío anula lo que el
    respaldo de las 09:00 escribió y la reversión NO se puede hacer, en Hevy se
    queda AHORA MISMO una sesión que el sistema ya ha decidido que hoy no toca.
    Sale por Telegram, sí, pero un mensaje se lee una vez y a las nueve de la
    mañana; la pantalla se mira justo antes de entrenar, que es cuando importa.
    """
    from app.integrations.hevy import read_pending

    raw = cfg.raw if hasattr(cfg, "raw") else (cfg or {})
    hevy_cfg = ((raw.get("integrations") or {}).get("hevy") or {})
    tg_cfg = ((raw.get("integrations") or {}).get("telegram") or {})

    salida: dict[str, Any] = {
        "hevy_write_enabled": bool(hevy_cfg.get("write_enabled")),
        "telegram_send_enabled": bool(tg_cfg.get("send_enabled")),
        "pending_write": None,
        "pending_error": None,
        "stale_write": None,
        "stale_error": None,
    }

    try:
        # `None` cuando no hay nada a medias. Cuando lo hay, sale el contenido
        # entero de la marca: rutina, fecha y hora del intento.
        salida["pending_write"] = read_pending(_raiz_de_datos())
    except Exception as e:  # noqa: BLE001 - el healthcheck no puede caerse
        # Que no se pueda leer la marca es en sí mismo un dato: significa que no
        # se sabe si hay una escritura a medias. Se dice, en vez de contestar
        # que no hay ninguna, que es lo que haría un `except` silencioso.
        salida["pending_error"] = f"no se ha podido leer la marca de escritura: {e}"

    salida["stale_write"], salida["stale_error"] = _rutina_huerfana(cfg, s)
    return salida


def _rutina_huerfana(cfg, s: Session) -> tuple[str | None, str | None]:
    """La frase que hay que leer si hoy quedó en Hevy algo que no toca.

    SE PREGUNTA POR HOY, Y SE APAGA SOLO MAÑANA. La fila `stale` de ayer ya no
    describe el estado de la app: mañana el trabajo de las 09:00 vuelve a
    escribir y lo que hubiera se pisa. Un aviso que no se pueda cerrar nunca
    acaba siendo un aviso que no se lee, así que éste se cierra solo.

    MANDA LA ÚLTIMA FILA DEL DÍA, NO LA PRIMERA QUE SEA `stale`. Un día puede
    tener varias escrituras y el orden es el dato: si a las 10:30 la reversión
    falló (`stale`) y a las 13:00 un segundo check-in salió verde y reescribió
    (`ok`), en Hevy hay lo correcto y avisar sería mentir. Por eso se mira la
    última y se compara su estado, en vez de buscar un `stale` cualquiera.

    EL FALLO NO SE TRAGA, SE CUENTA. Un `except` que devolviera `(None, None)`
    diría «no hay ninguna rutina huérfana» cuando lo que ha pasado es que no se
    ha podido mirar, y las dos cosas se verían igual desde el móvil: un
    healthcheck en verde. Por eso hay `stale_error`, y por eso sale en
    `problemas` como cualquier otro.

    Y SE DESHACE LA TRANSACCIÓN antes de volver. La sesión viene de
    `session_scope`, que hace `commit()` al salir; si aquí se deja marcada por
    un error y se sigue como si nada, el `commit()` de la salida revienta
    DESPUÉS de haber construido la respuesta, fuera de este `try` y donde ya no
    hay quien lo cuente.
    """
    from app.models import HevyWrite

    try:
        hoy = datetime.now(ZoneInfo(cfg.timezone)).date()
        fila = s.scalars(
            select(HevyWrite)
            .where(HevyWrite.date == hoy)
            .order_by(HevyWrite.id.desc())
            .limit(1)
        ).first()
    except Exception as e:  # noqa: BLE001 - el healthcheck no puede caerse
        try:
            s.rollback()
        except Exception:  # noqa: BLE001 - si ni eso, tampoco hay nada que hacer
            pass
        return None, f"no se ha podido mirar si hoy quedó una rutina huérfana: {e}"

    if fila is None or fila.status != "stale":
        return None, None
    # El motivo se guardó ya redactado para que sirva para actuar -«Abre Hevy y
    # NO hagas X: hoy toca Y»- y lleva los títulos reales de las dos rutinas. No
    # se reescribe aquí: si esta pantalla dijera una cosa y Telegram otra sobre
    # el mismo día, habría dos verdades sobre el mismo hecho y ninguna de fiar.
    return (fila.reason or
            "en Hevy quedó una rutina de una decisión anulada y no se ha "
            "podido deshacer"), None


def _raiz_de_datos() -> Path:
    """La carpeta de datos, deducida de `database_url`."""
    url = str(settings.database_url)
    return Path(url.split("///")[-1]).parent if "///" in url else Path("data")


def _estado_del_build() -> dict[str, Any]:
    """Qué CÓDIGO está corriendo, no qué config.

    `_estado_del_config` contesta la mitad de la pregunta y la contesta bien: si
    lo que hay escrito en el disco es lo que está decidiendo. La otra mitad no
    la contestaba nadie, y es la que hay que hacer después de arreglar algo:
    ¿está corriendo el arreglo? La etiqueta de la imagen lleva cuarenta commits
    parada en `0.1.0`, así que un contenedor de la semana pasada y uno
    reconstruido hace un minuto se ven idénticos desde fuera.

    `unknown: true` cuando el build no dejó marca. NO es un problema de salud y
    no tiñe el `status`: en local nunca hay marca, y un desarrollo que se
    autodiagnostica enfermo por no ser una imagen enseña a ignorar el
    diagnóstico. Lo que hace es decir que no se sabe, que es distinto de
    insinuar que sí.
    """
    sha = (settings.build_sha or "").strip()
    fecha = (settings.build_date or "").strip()
    return {
        "sha": sha or None,
        "date": fecha or None,
        "unknown": not sha,
        # El tag de la imagen no sirve para esto y decirlo aquí ahorra el viaje
        # de ir a mirarlo: es fijo, no se mueve entre versiones.
        "note": (
            "sin marca de construcción: o esto no es una imagen, o se construyó "
            "sin pasarle GIT_SHA. La etiqueta de la imagen no lo dice: es fija."
            if not sha else None
        ),
    }


def _estado_del_config(cfg) -> dict[str, Any]:
    """El hash del config en memoria contra el del fichero en disco AHORA.

    POR QUÉ. «El valor que se lee no es el valor que se usa» es el fallo que más
    veces ha aparecido en este proyecto: imagen vieja, esquema viejo, Caddyfile
    viejo. Los tres tenían la misma forma -editar el fichero y dar por hecho que
    el proceso lo había visto- y los tres se descubrieron tarde.

    El `config.yaml` se carga UNA vez por proceso y se cachea. Editarlo no
    cambia nada hasta recrear el contenedor. Hasta ahora `/api/health` devolvía
    el hash de memoria, que es el que manda, pero sin nada con que compararlo:
    coincidía consigo mismo siempre, o sea que tranquilizaba sin mirar.

    Esto lee el fichero en cada petición -no se cachea, y ese es el punto- y
    compara. `in_sync: false` significa exactamente una cosa: lo que hay escrito
    en el disco no es lo que está decidiendo, y hace falta recrear el
    contenedor. Es la comprobación de después de tocar el config, sin tener que
    acordarse de hacerla a mano.
    """
    ruta = Path(settings.config_path)
    try:
        en_disco = load_config(ruta)
    except Exception as e:  # noqa: BLE001 - un YAML roto no tumba el health
        # Esto es un aviso serio y no un error de lectura cualquiera: el fichero
        # del disco no se puede cargar, así que la próxima vez que se recree el
        # contenedor la aplicación NO va a arrancar. Mejor enterarse ahora.
        return {
            "path": str(ruta),
            "loaded_hash": cfg.hash,
            "file_hash": None,
            "in_sync": False,
            "error": f"el config.yaml del disco no se puede cargar: {e}",
        }

    igual = en_disco.hash == cfg.hash
    salida = {
        "path": str(ruta),
        "loaded_hash": cfg.hash,
        "file_hash": en_disco.hash,
        "in_sync": igual,
    }
    if not igual:
        # El comando exacto NO se escribe aquí, y no por pereza. Este código
        # corre en dos sitios con compose distintos -el PC de pruebas necesita
        # `-f docker-compose.yml -f docker-compose.pruebas-lan.yml`, el Umbrel
        # no-, y un aviso que dicta el comando de otro despliegue es peor que
        # uno que no dicta ninguno: el 12 de septiembre el comando corto recreó
        # el contenedor apuntando a un directorio de datos distinto, con otra
        # base y otras copias de Hevy, y contestando `ok` todo el rato.
        #
        # Lo que sí se dice es la regla, que es la misma en los dos: el mismo
        # compose con el que se levantó, y verificar después.
        salida["error"] = (
            "el config.yaml del disco NO es el que está cargado en memoria. "
            "Los cambios no se aplican hasta recrear el contenedor, con el "
            "MISMO compose con el que está levantado (el comando exacto de "
            "este despliegue, en docs/primer-dia.md)."
        )
    return salida


def _estado_del_reloj(cfg) -> dict[str, Any]:
    """¿Deciden el mismo día el reloj del proceso y el `config.yaml`?

    Hace falta preguntarlo aparte, y la razón es que la respuesta obvia NO
    sirve. Mirar la hora de los trabajos del planificador parece la comprobación
    natural -"que ponga +02:00 y no +00:00"- pero no comprueba nada: los
    disparadores se construyen con `ZoneInfo(cfg.timezone)`, así que salen en
    `+02:00` aunque el contenedor esté en UTC y aunque `TZ` no exista. Es una
    comprobación que no puede fallar, y una comprobación que no puede fallar
    tranquiliza sin mirar.

    Lo que sí depende del reloj del proceso son los nueve `date.today()` y
    `datetime.now()` repartidos por `api.py`, `scheduler.py`, `cli.py` y
    `hevy.py`: ahí es donde se decide bajo qué día se guarda un check-in. Con el
    contenedor en UTC y las reglas en Europe/Madrid, un check-in enviado entre
    las 00:00 y las 02:00 se archiva con la fecha de AYER, y a la mañana
    siguiente el trabajo de las 09:00 decide como si no lo hubiera habido.

    Se comparan DESPLAZAMIENTOS, no nombres de zona. El nombre puede diferir sin
    consecuencias -Europe/Madrid y Europe/Paris son el mismo día a la misma
    hora-, y lo único que elige la fecha es el desplazamiento. Comparar nombres
    daría avisos falsos; comparar desplazamientos avisa cuando, y solo cuando,
    las dos fuentes pueden ya discrepar en qué día es hoy.
    """
    ahora = datetime.now(timezone.utc)
    del_reloj = ahora.astimezone().utcoffset()
    try:
        de_las_reglas = ahora.astimezone(ZoneInfo(cfg.timezone)).utcoffset()
    except Exception as e:  # zona escrita mal en el `config.yaml`
        return {
            "timezone": cfg.timezone,
            "offset": None,
            "matches": False,
            "error": f"zona horaria desconocida en config.yaml: {e}",
        }

    return {
        "timezone": cfg.timezone,
        # El del PROCESO, que es el que manda en `date.today()`.
        "offset": _iso_offset(del_reloj),
        "matches": del_reloj == de_las_reglas,
        "error": None,
    }


def _iso_offset(delta: timedelta | None) -> str | None:
    if delta is None:
        return None
    total = int(delta.total_seconds())
    signo = "-" if total < 0 else "+"
    horas, resto = divmod(abs(total), 3600)
    return f"{signo}{horas:02d}:{resto // 60:02d}"


def _estado_planificador(sched: Any, error: str | None = None) -> dict[str, Any]:
    """Recibe el planificador, no la petición.

    Lo pedía entero -`request`- para sacarle dos atributos, y eso lo ataba a
    haber una petición HTTP delante. El trabajo de vigilancia corre DENTRO del
    planificador y no tiene ninguna: con la firma vieja habría tenido que
    fabricar una petición falsa o saltarse este bloque, y saltárselo es peor de
    lo que parece -`_problemas_de_salud` lee un `scheduler` vacío como «no está
    corriendo» y habría avisado de una avería inventada cada día-.
    """
    if sched is None:
        return {
            "running": False,
            "jobs": {},
            "error": error or (
                "desactivado por SCHEDULER_ENABLED"
                if not settings.scheduler_enabled
                else "no arrancó"
            ),
        }
    return {
        "running": sched.running,
        "jobs": {
            j.id: (j.next_run_time.isoformat() if j.next_run_time else None)
            for j in sched.get_jobs()
        },
        "error": None,
    }


@app.get("/api/checkin/today")
def checkin_today(
    day: date | None = None,
    s: Session = Depends(get_session),
    cfg=Depends(get_config),
) -> dict[str, Any]:
    """Lo que ya se contestó hoy, para que la PWA no lo pida dos veces."""
    day = day or date.today()
    fila = repo.get_checkin(s, day)
    return {
        "day": day.isoformat(),
        "submitted": fila is not None,
        "values": repo.checkin_values(fila),
        "comments": getattr(fila, "comments", None),
        # La PWA no lleva ninguna lista de deslizadores escrita a mano: los pinta
        # a partir de esto. Si los tuviera escritos, añadir uno al `config.yaml`
        # lo dejaría fuera del formulario y el sistema decidiría sin ese dato sin
        # que nadie lo notara.
        "sliders": cfg.raw.get("checkin_sliders", []),
        # Y lo mismo con las dos preguntas de Sí/No, por el mismo motivo y en una
        # lista aparte. Aparte y no mezcladas con una bandera de tipo: la PWA las
        # pinta distinto -dos botones en vez de una barra- y separarlas aquí es lo
        # que permite que `pintarSliders` siga sin saber que existen los
        # booleanos. Un solo array con `tipo: "bool"` obligaría a cada consumidor
        # de esta respuesta a mirar el tipo antes de nada, y el día que alguien se
        # olvide pintaría una barra de 1 a 10 para «¿Vas a entrenar hoy?».
        "preguntas": cfg.raw.get("checkin_preguntas", []),
        "selector": _selector_de_hoy(s, cfg, day),
        "comment_label": (cfg.raw.get("checkin_comment") or {}).get(
            "label", "Comentarios"
        ),
    }


def _selector_de_hoy(s: Session, cfg: Any, day: date) -> dict[str, Any]:
    """Las opciones del selector, con su título, la propuesta y lo que lleva parado.

    TODO ESTO SE RESUELVE AQUÍ Y NO EN LA PANTALLA, y es la misma regla que ya
    gobierna los deslizadores: la PWA no lleva escrita ni una opción. Si las
    llevara, añadir un `dia_4` al ciclo dejaría la pantalla ofreciendo tres
    mientras el sistema rota entre cuatro, y nadie se enteraría hasta mirar por
    qué el cuarto día no sale nunca.

    Las opciones salen de `cfg.opciones_selector()`, que es LA MISMA función que
    usa `repo.upsert_checkin` para validar lo que llega. Que dibuje y valide la
    misma lista es lo que impide la peor discordancia posible aquí: una pantalla
    que ofrece algo que el guardado rechaza.

    LOS TÍTULOS. Las rutinas del ciclo se nombran como en `config.raw["routines"]`
    -«Día 1», no `dia_1`-; las que no son fuerza traen el suyo escrito en
    `checkin_selector.sin_fuerza`. Una clave sin título se nombra con la clave:
    feo pero cierto, como en `message._titulo_rutina`, y nunca un hueco.

    LA PROPUESTA VIAJA APARTE DE LO CONTESTADO. Va en `propuesta` y no en
    `values`, porque `values` es lo que dijo el usuario y la propuesta no la ha
    dicho nadie. Mezclarlas haría que la pantalla abriera con el selector
    contestado sin que nadie lo hubiera tocado, y a partir de ahí «declaraste
    Día 2» se escribiría en el historial de todas las mañanas en que el
    formulario se envió sin mirar el selector. Es exactamente el aplastamiento de
    los tres estados que el resto de este formulario se construyó para evitar,
    hecho en el único sitio donde después no hay forma de deshacerlo.

    `pendiente` marca las rutinas que llevan más de una vuelta sin hacerse, y se
    filtran las caducadas por el mismo motivo por el que el mensaje de la mañana
    también las calla: una marca que sale todos los días durante meses deja de
    informar. Ver `CADUCA_TRAS` en `app/engine/rotacion.py`.
    """
    orden = orden_de_rotacion(cfg)
    sesiones = repo.sesiones_del_ciclo(s, orden, hasta=day)

    # LA PROPUESTA SE CALCULA COMO LA CALCULA `decide`, con la misma función pura
    # y sobre el mismo dato: la última sesión del ciclo EJECUTADA. No se lee de la
    # decisión guardada de hoy, y eso es deliberado: a las 07:00 puede no haber
    # ninguna, y entonces la pantalla se quedaría sin decir qué toca justo el día
    # en que se abre antes de que el sistema haya decidido nada.
    ultima = sesiones[0][0] if sesiones else None
    propuesta = siguiente_en_rotacion(cfg, ultima) if orden else None

    paradas = {
        p.clave: p
        for p in rutinas_pendientes(orden, sesiones)
        if not p.caducada
    }

    rutinas = cfg.raw.get("routines") or {}
    etiquetas = {
        str(o["key"]): str(o.get("label") or o["key"])
        for o in ((cfg.raw.get("checkin_selector") or {}).get("sin_fuerza") or [])
        if o.get("key")
    }

    opciones = []
    for clave in cfg.opciones_selector():
        parada = paradas.get(clave)
        opciones.append({
            "key": clave,
            "label": etiquetas.get(
                clave, str((rutinas.get(clave) or {}).get("title") or clave)
            ),
            "es_fuerza": clave in orden,
            # Cuántas sesiones de fuerza van desde la última vez, o `None` si no
            # lleva parada. `None` y no 0: un cero diría «hecha hoy mismo».
            "pendiente": parada.sesiones_desde if parada else None,
            "ultima_vez": parada.ultima_vez.isoformat() if parada else None,
        })

    sel = cfg.selector()
    return {
        "key": str(sel.get("key") or "chosen_session"),
        "label": str(sel.get("label") or "¿Qué vas a hacer hoy?"),
        "nota": sel.get("nota") or None,
        "propuesta": propuesta,
        "opciones": opciones,
    }


@app.post("/api/preview")
def post_preview(
    body: PreviewIn,
    s: Session = Depends(get_session),
    cfg=Depends(get_config),
) -> dict[str, Any]:
    """Enseña qué decidiría el sistema con estas respuestas. No decide nada.

    MIRAR NO ES HACER, Y ESE ES EL PUNTO ENTERO
    -------------------------------------------
    No guarda el check-in, no deja decisión vigente, no escribe en Hevy y no
    manda ningún Telegram. Sirve para CALIBRAR: probar respuestas hasta entender
    dónde están los umbrales, y que con el tiempo lo que dice el sistema y lo
    que el usuario sabe de sí mismo converjan.

    Por eso las respuestas van por parámetro a `pensar_el_dia` en vez de
    guardarse primero. Si se guardaran, una mañana probando cuatro fatigas
    distintas metería cuatro lecturas inventadas en `checkins`, que es de donde
    salen los percentiles de los umbrales adaptativos, y el sistema acabaría
    calibrándose contra respuestas que nadie dio nunca. El fallo no daría ningún
    error y se vería tres meses después, en un umbral movido sin motivo.

    LO QUE SÍ ESCRIBE, Y POR QUÉ NO CONTRADICE LO ANTERIOR
    -----------------------------------------------------
    Escribe una fila en `previews`, que es una tabla que no alimenta ninguna
    regla. Es el dato de calibración: qué se preguntó, qué contestó el sistema,
    y si el usuario estuvo de acuerdo. Varias filas por día a propósito -la
    segunda no tapa a la primera- porque la DIFERENCIA entre dos
    previsualizaciones del mismo día es justamente lo que se quiere medir.

    Y por eso la respuesta desglosa qué no se ha tocado en vez de decir un
    `escrito: false` que sonaría más tranquilizador y sería mentira.
    """
    from app.engine.session_builder import ConfirmacionNecesaria
    from app.runner import pensar_el_dia
    from app.scheduler import garmin_para_previsualizar

    day = body.day or date.today()
    respuestas = _respuestas(body)

    try:
        # La versión con caché, y no `_fetch_garmin`. Una tanda de calibración
        # son cinco o seis previsualizaciones en diez minutos, y cada una con su
        # login es la forma de que Garmin corte el acceso justo el día que se
        # está usando esto.
        metrics, rides = garmin_para_previsualizar(cfg, day)
    except Exception as exc:  # noqa: BLE001
        log.exception("no se pudo leer Garmin al previsualizar")
        # 502 y no un 200 con los datos a medias. Sin wellness, todas las
        # señales salen degradadas y el semáforo contesta ámbar por falta de
        # datos: una previsualización con esa cara diría que el día es ámbar
        # cuando lo que pasa es que no se ha podido mirar.
        raise HTTPException(status_code=502, detail={
            "error": f"no se ha podido leer Garmin, así que no hay nada que "
                     f"previsualizar: {exc}",
            **NADA_EJECUTADO,
        }) from exc

    try:
        pensado = pensar_el_dia(
            s, cfg, day,
            metrics=metrics, rides=rides,
            # Queda dentro del JSON guardado, y hace la fila autoexplicativa:
            # un `source: checkin` dentro de `previews` sería justo la clase de
            # etiqueta que dentro de tres meses hace dudar de la tabla entera.
            source="preview",
            respuestas=respuestas,
            sesion_pedida=_sesion_pedida(body),
        )
    except ConfirmacionNecesaria as exc:
        raise HTTPException(status_code=409, detail={
            "confirmacion_necesaria": True,
            "propuesta": exc.propuesta,
            "pedida": exc.pedida,
            "motivo": str(exc),
            **NADA_EJECUTADO,
        }) from exc

    fila = repo.save_preview(
        s, pensado.decision,
        answers=respuestas,
        disagreed=body.disagreed,
        disagreement_reason=body.disagreement_reason,
    )

    return {
        "day": day.isoformat(),
        **NADA_EJECUTADO,
        # Lo único que sí se ha escrito, con su nombre y su identificador. El
        # `preview_id` no es decorativo: es lo que permite enlazar esta
        # previsualización con la decisión, si acaba enviándose.
        "previsualizacion_guardada": True,
        "preview_id": fila.id,
        "seq": fila.seq,
        # Redundante con `seq > 1`, y a posta: quien pinta la tarjeta no tiene
        # por qué saber que la numeración empieza en 1, y esa clase de detalle
        # aritmético en el navegador es donde acaban apareciendo los off-by-one.
        "revision": fila.seq > 1,
        "light": pensado.decision.light,
        "trigger_rule": pensado.decision.trigger_rule,
        "decision": pensado.decision.to_dict(),
    }


@app.post("/api/preview/{preview_id}/desacuerdo")
def post_desacuerdo(
    preview_id: int,
    body: DesacuerdoIn,
    s: Session = Depends(get_session),
) -> dict[str, Any]:
    """Anota que lo que enseñó esta previsualización no se comparte, y por qué.

    ES UNA RUTA APARTE PORQUE EL DESACUERDO LLEGA DESPUÉS
    ----------------------------------------------------
    `PreviewIn` ya acepta `disagreed`, y aun así la pantalla no puede usar esa
    puerta: cuando se manda el POST que crea la previsualización todavía no se
    ha visto nada, y de lo que no se ha visto no se discrepa. El desacuerdo se
    declara mirando la tarjeta, o sea sobre una fila que ya existe, y por eso se
    identifica con el `preview_id` que la respuesta de arriba devuelve.

    Las dos puertas no son dos fuentes del mismo hecho: la de `PreviewIn` sirve
    para reproducir de una sola vez una previsualización con su juicio -el CLI,
    los tests, cualquier cliente que ya sepa las dos cosas-, y ésta es la del
    usuario delante del móvil. Escriben la misma columna porque es el mismo
    hecho; lo que cambia es en qué momento se sabe.

    NO EJECUTA NADA, y lo dice con las mismas palabras que la previsualización.
    Importa que lo diga aquí también: es un botón que se pulsa justo encima del
    de enviar, y «he apuntado que no estoy de acuerdo» podría leerse como que el
    sistema ha hecho algo al respecto. No lo ha hecho. Lo que decide sigue
    decidiéndose en el formulario.
    """
    try:
        fila = repo.marcar_desacuerdo(
            s, preview_id, disagreed=body.disagreed, reason=body.reason
        )
    except ValueError as exc:
        # 404 y no 400: lo que no existe es el recurso de la URL. Un 400 haría
        # pensar que el cuerpo está mal y mandaría a mirar el sitio equivocado.
        raise HTTPException(status_code=404, detail={
            "error": str(exc),
            **NADA_EJECUTADO,
        }) from exc

    return {
        "day": fila.date.isoformat(),
        "preview_id": fila.id,
        "seq": fila.seq,
        # Se devuelve lo que ha QUEDADO ESCRITO, releído de la fila, y no lo que
        # venía en el cuerpo. Es la diferencia entre confirmar que se ha
        # guardado y repetir lo que me acaban de decir: el motivo se normaliza
        # al guardarlo -unos espacios en blanco quedan en `null`- y una pantalla
        # que pintara el eco del cuerpo enseñaría un motivo apuntado que en la
        # base no está.
        "disagreed": fila.disagreed,
        "disagreement_reason": fila.disagreement_reason,
        **NADA_EJECUTADO,
    }


@app.post("/api/checkin")
def post_checkin(
    body: EnvioIn,
    s: Session = Depends(get_session),
    cfg=Depends(get_config),
) -> dict[str, Any]:
    """Guarda el check-in y decide el día con él dentro.

    Las dos cosas van juntas a propósito: es el camino normal del sistema. Si se
    guardara aquí y se decidiera en otro sitio, el usuario enviaría el
    formulario y no pasaría nada visible hasta una hora después.
    """
    from app.engine.session_builder import ConfirmacionNecesaria

    day = body.day or date.today()
    valores = _respuestas(body)

    try:
        repo.upsert_checkin(s, day, valores, config=cfg, comments=body.comments)
    except ValueError as exc:
        # Clave desconocida: 400 y no un 200 que se traga el campo.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        resultado = _decidir(
            s, cfg, day, source="checkin", sesion_pedida=_sesion_pedida(body)
        )
    except ConfirmacionNecesaria as exc:
        # El `rollback` es la mitad importante. Sin él, el check-in se quedaría
        # guardado mientras la respuesta dice que hace falta confirmar: el
        # usuario contestaría que no, y el día se quedaría con unas respuestas
        # escritas que nunca llegaron a decidir nada.
        s.rollback()
        raise HTTPException(status_code=409, detail={
            "confirmacion_necesaria": True,
            "propuesta": exc.propuesta,
            "pedida": exc.pedida,
            "motivo": str(exc),
            **NADA_EJECUTADO,
        }) from exc

    if resultado.get("decided"):
        # Las previsualizaciones de hoy acaban de convertirse en algo que SÍ
        # ocurrió, y esa es la respuesta a "¿la anulación se llegó a ejecutar?".
        # Se enlazan aquí, dentro de la misma transacción que la decisión: un
        # enlace que se guardara aparte podría afirmar que se ejecutó mientras
        # `decisions` no tiene la fila.
        decision = repo.current_decision(s, day)
        if decision is not None:
            repo.enlazar_previews_pendientes(s, day, decision.id)

    return {
        "day": day.isoformat(),
        "checkin_saved": True,
        **resultado,
    }


def _decidir(
    s: Session, cfg, day: date, *, source: str, sesion_pedida: Any = None
) -> dict[str, Any]:
    """Decide el día, y si no puede lo dice sin fingir que sí.

    Y CUANDO NO PUEDE, DICE ADEMÁS HASTA DÓNDE LLEGÓ
    ------------------------------------------------
    Un fallo decidiendo no es un suceso, son dos muy distintos, y hasta el 18 de
    septiembre de 2026 los dos salían por el mismo sitio y con la misma cara.

    Uno es caerse ANTES: Garmin no contesta, no hay métricas, no hay decisión y
    no se ha tocado nada de fuera. El otro es caerse DESPUÉS: la rutina ya está
    escrita en Hevy y el mensaje ya está en el móvil, y lo que revienta es
    apuntarlo. Hevy y Telegram están fuera de la transacción, así que el
    `rollback` deshace la fila y no deshace ni la rutina ni el mensaje.

    Los dos devolvían un diccionario sin `hevy` ni `telegram`, y la pantalla, que
    no tenía de dónde sacarlos, se los inventaba: decía que no se había enviado
    nada. Ese día había un Telegram entregado en el móvil del usuario mientras la
    pantalla decía lo contrario, que es la clase de mentira que hace que alguien
    reenvíe a mano algo que ya estaba hecho.

    Así que ahora las dos ramas contestan lo que saben: la temprana pone
    `skipped` porque de verdad no se tocó nada, y la tardía saca el estado real
    del `DailyResult` que `DecisionInterrumpida` trae consigo.

    Y HAY UNA EXCEPCIÓN QUE NO ES UN FALLO
    --------------------------------------
    `ConfirmacionNecesaria` no dice "no he podido": dice "necesito que me
    contestes a una pregunta". El `except Exception` de abajo la convertiría en
    un `decided: false` con un texto de error, y la pantalla la enseñaría como
    una avería en vez de como el diálogo que es -«el sistema propone
    recuperación, has pedido completa»-. Sale hacia arriba intacta para que
    quien llama pueda redactar la pregunta.
    """
    from app.engine.session_builder import ConfirmacionNecesaria
    from app.runner import DecisionInterrumpida, run_daily
    from app.scheduler import _fetch_garmin

    try:
        metrics, rides = _fetch_garmin(cfg, day)
    except Exception as exc:  # noqa: BLE001
        log.exception("no se pudo leer Garmin al decidir")
        return {
            "decided": False,
            "error": f"el check-in SÍ se ha guardado, pero no se pudo leer "
                     f"Garmin y por tanto no se ha decidido: {exc}",
            # Aquí sí se puede afirmar: leer Garmin es lo primero que se hace y
            # pasa antes de tocar nada de fuera.
            "hevy": "skipped",
            "telegram": "skipped",
        }

    try:
        hevy, tg, motivos = _clientes(cfg)
        res = run_daily(
            s, cfg, day, metrics=metrics, rides=rides,
            hevy_client=hevy, telegram_client=tg, client_errors=motivos,
            dry_run=settings.dry_run, source=source,
            sesion_pedida=sesion_pedida,
        )
    except ConfirmacionNecesaria:
        # No es un fallo; ver el docstring. Salta DENTRO de `pensar_el_dia`, o
        # sea antes de que se toque nada de fuera, así que no hay nada a medias
        # que contar.
        raise
    except DecisionInterrumpida as exc:
        log.exception("fallo decidiendo a medias")
        return {
            "decided": False,
            "error": f"el check-in SÍ se ha guardado, pero la decisión ha "
                     f"fallado: {exc}",
            "hevy": exc.res.hevy_status,
            "telegram": exc.res.telegram_status,
            "problems": exc.res.problemas,
        }
    except Exception as exc:  # noqa: BLE001
        log.exception("fallo decidiendo")
        return {
            "decided": False,
            "error": f"el check-in SÍ se ha guardado, pero la decisión ha "
                     f"fallado: {exc}",
            # Sin `DecisionInterrumpida` no hay forma de saber hasta dónde se
            # llegó, y `None` es precisamente eso: no lo sé. La pantalla tiene
            # que distinguir «no se tocó» de «no lo sé», porque no son lo mismo.
            "hevy": None,
            "telegram": None,
        }

    return {
        "decided": True,
        "light": res.decision.light,
        "session": res.decision.session.title,
        "kind": res.decision.session.kind,
        "hevy": res.hevy_status,
        "telegram": res.telegram_status,
        # Los problemas viajan al cliente aunque la decisión haya salido: que
        # Telegram no enviara es algo que el usuario tiene que saber ahora, no
        # cuando eche en falta el mensaje.
        "problems": res.problemas,
    }


def _clientes(cfg) -> tuple[Any, Any, dict[str, str]]:
    """Construye los dos clientes. El tercer elemento es POR QUÉ falló cada uno.

    Antes se devolvía solo `(hevy, tg)` y el motivo se quedaba en un
    `log.warning` que en Umbrel no lee nadie a las nueve de la mañana. El
    resultado no era invisible -el runner ya marca la escritura como error y lo
    pone arriba del mensaje-, pero sí ANÓNIMO: el aviso tenía que adivinar la
    causa ("revisa HEVY_API_KEY en el .env"), que es la más probable y no la
    única. Si lo que falla es otra cosa, la adivinanza manda a mirar donde no
    es y el error real solo existe en un fichero de log.

    Un aviso que nombra su causa se arregla desde el móvil; uno que la adivina
    obliga a entrar por SSH.
    """
    from app.integrations.hevy import build_client as hevy_client
    from app.integrations.telegram import build_client as tg_client

    hevy = tg = None
    motivos: dict[str, str] = {}
    try:
        hevy = hevy_client(settings, cfg)
    except Exception as exc:  # noqa: BLE001
        motivos["hevy"] = str(exc)
        log.warning("sin cliente de Hevy: %s", exc)
    try:
        tg = tg_client(settings, cfg)
    except Exception as exc:  # noqa: BLE001
        motivos["telegram"] = str(exc)
        log.warning("sin cliente de Telegram: %s", exc)
    return hevy, tg, motivos


@app.get("/api/decision")
def get_decision(
    day: date | None = None,
    s: Session = Depends(get_session),
) -> dict[str, Any]:
    day = day or date.today()
    fila = repo.current_decision(s, day)
    if fila is None:
        raise HTTPException(status_code=404, detail=f"no hay decisión del {day}")
    return {
        "day": fila.date.isoformat(),
        "light": fila.light,
        "trigger_rule": fila.trigger_rule,
        "source": fila.source,
        "config_hash": fila.config_hash,
        "session": repo.planned_session(fila),
        "progressed": repo.progressed_keys(fila),
    }


@app.get("/api/state")
def get_state(
    s: Session = Depends(get_session), cfg=Depends(get_config)
) -> dict[str, Any]:
    """El estado del motor, legible. Es la ventana a por qué hace lo que hace."""
    estado = repo.load_state(s, program_start=cfg.program_start, rotation_order=cfg.rotation_order())
    return repo.state_as_dict(estado)


# ---------------------------------------------------------------------------
# Métricas y análisis
#
# Todo lo de aquí se calcula EN EL SERVIDOR y viaja ya masticado. La PWA no hace
# ni una media. No es manía de arquitectura: es que el día que un número salga
# raro tiene que poder mirarse con un test, y un cálculo que vive en el navegador
# no se puede probar ni auditar desde el móvil, que es desde donde se va a mirar.
#
# Los parámetros son los mismos en todos: `dias` de ventana y `metodo`. Spearman
# por defecto porque seis de las siete series de percepción son un 1-5 ordinal,
# donde la distancia entre un 2 y un 3 no tiene por qué ser la misma que entre un
# 4 y un 5. Pearson se puede pedir, y compararlos dice bastante.
# ---------------------------------------------------------------------------

METODOS = ("spearman", "pearson")


def _metodo(metodo: str) -> str:
    if metodo not in METODOS:
        raise HTTPException(
            status_code=400,
            detail=f"método desconocido: {metodo!r}. Los que hay: {list(METODOS)}",
        )
    return metodo


@app.get("/api/metrics/portada")
def metrics_portada(
    dias: int = Query(180, ge=7, le=730),
    metodo: str = Query("spearman"),
    s: Session = Depends(get_session),
    cfg=Depends(get_config),
) -> dict[str, Any]:
    """La portada: lo que ya se sabe, antes de pedirle a nadie que elija nada.

    Va primera porque es lo primero que se lee, y existe porque las otras cinco
    vistas empezaban por el APARATO DE MEDIR -un desplegable de variables- en
    vez de por la medida. El 2026-09-13 había 27 relaciones fiables calculadas y
    enviadas al navegador, y la pantalla de entrada no enseñaba ninguna.

    Se monta entera aquí y no pegando seis respuestas en el móvil. Ensamblarla
    en el navegador sería lento y frágil, pero sobre todo volvería a meter en el
    JavaScript las reglas de qué se cuenta y cómo, que es de donde este rediseño
    las está sacando.
    """
    from app.analysis.portada import vista_portada
    from app.analysis.series import comprobar_series

    comprobar_series(cfg)
    return vista_portada(s, cfg, dias=dias, metodo=_metodo(metodo))


@app.get("/api/metrics/concordancia")
def metrics_concordancia(
    dias: int = Query(180, ge=7, le=730),
    metodo: str = Query("spearman"),
    s: Session = Depends(get_session),
    cfg=Depends(get_config),
) -> dict[str, Any]:
    """Vista 1: lo que nota frente a lo que mide el reloj, el mismo día."""
    from app.analysis.concordancia import vista_concordancia
    from app.analysis.encabezados import poner
    from app.analysis.series import comprobar_series

    comprobar_series(cfg)
    return poner(
        "concordancia", vista_concordancia(s, dias=dias, metodo=_metodo(metodo))
    )


@app.get("/api/metrics/desfase")
def metrics_desfase(
    dias: int = Query(180, ge=7, le=730),
    metodo: str = Query("spearman"),
    s: Session = Depends(get_session),
    cfg=Depends(get_config),
) -> dict[str, Any]:
    """Vista 2: la misma pregunta corriendo la ventana de -3 a +3 días."""
    from app.analysis.concordancia import vista_desfase
    from app.analysis.encabezados import poner
    from app.analysis.series import comprobar_series

    comprobar_series(cfg)
    return poner("desfase", vista_desfase(s, dias=dias, metodo=_metodo(metodo)))


@app.get("/api/metrics/impacto")
def metrics_impacto(
    dias: int = Query(180, ge=7, le=730),
    metodo: str = Query("spearman"),
    s: Session = Depends(get_session),
    cfg=Depends(get_config),
) -> dict[str, Any]:
    """Vista 3: qué le hace al cuerpo cada cosa, uno, dos y tres días después."""
    from app.analysis.encabezados import poner
    from app.analysis.impacto import vista_impacto
    from app.analysis.series import comprobar_series

    comprobar_series(cfg)
    return poner("impacto", vista_impacto(s, dias=dias, metodo=_metodo(metodo)))


@app.get("/api/metrics/ranking-ejercicios")
def metrics_ranking_ejercicios(
    respuesta: str = Query("lower_discomfort"),
    dias: int = Query(180, ge=7, le=730),
    metodo: str = Query("spearman"),
    s: Session = Depends(get_session),
    cfg=Depends(get_config),
) -> dict[str, Any]:
    """Vista 3, el ranking: los ejercicios ordenados por la molestia del día siguiente.

    `respuesta` se puede cambiar -la misma lista sirve para el cansancio o para la
    HRV-, pero por defecto es la lumbar, que es lo que se pidió y lo que más pesa
    con una hernia L4-L5 de por medio.

    El `ValueError` de una respuesta desconocida se traduce a un 400 con la lista
    entera dentro. Un 500 diría que el servidor está roto, y lo que pasa es que se
    ha pedido algo que no existe; y devolver un ranking vacío sería peor que las
    dos cosas, porque parecería la respuesta.
    """
    from app.analysis.encabezados import poner
    from app.analysis.impacto import ranking_ejercicios
    from app.analysis.series import comprobar_series

    comprobar_series(cfg)
    try:
        return poner(
            "ranking-ejercicios",
            ranking_ejercicios(
                s, cfg, respuesta=respuesta, dias=dias, metodo=_metodo(metodo)
            ),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/api/metrics/auditoria")
def metrics_auditoria(
    dias: int = Query(180, ge=7, le=730),
    s: Session = Depends(get_session),
    cfg=Depends(get_config),
) -> dict[str, Any]:
    """Vista 4: el motor auditándose a sí mismo.

    No lleva `metodo` porque aquí no se correlaciona nada: se cuentan disparos,
    se reparten luces y se sigue la carga de cada ficha. Añadirle el parámetro
    para que las cuatro rutas de métricas tuvieran la misma firma sería añadir
    una opción muerta, que es justo lo que no se hace en esta casa.

    Se pasa `cfg` porque la mitad de esta vista es la comparación entre lo que el
    YAML declara hoy y lo que el histórico guardó: una regla que disparó veinte
    veces y ya no está escrita sale como retirada, y eso solo se puede saber
    teniendo las dos cosas delante a la vez.
    """
    from app.analysis.auditoria import vista_auditoria
    from app.analysis.encabezados import poner
    from app.analysis.series import comprobar_series

    comprobar_series(cfg)
    return poner("auditoria", vista_auditoria(s, cfg, dias=dias))


@app.get("/api/metrics/percepcion")
def metrics_percepcion(
    dias: int = Query(180, ge=7, le=730),
    s: Session = Depends(get_session),
    cfg=Depends(get_config),
) -> dict[str, Any]:
    """Vista 5: la percepción de la mañana frente a lo que de verdad salió.

    SOLO LEE. No evalúa sesiones, no escribe filas y no marca nada como
    reportado. Eso lo hace el barrido de la mañana (`evaluar_pendientes`), y
    tiene que ser así: abrir la pantalla no puede crear el juicio de una sesión
    a la que todavía le falta el esfuerzo percibido, porque la fila no se
    reescribe después y ese hueco se quedaría para siempre. Una vista que además
    escribe convierte "mirar el contador" en "mover el contador".

    Tampoco lleva `metodo`, por lo mismo que la auditoría: aquí no se
    correlaciona nada, se restan dos percentiles ya guardados.

    `comprobar_series` sí se llama, aunque esta vista no lea los deslizadores
    de la base: los lee de `series.DEFINICIONES` para saber el sentido de cada
    uno, y un deslizador que exista en el formulario y no en esa tabla se caería
    del índice de percepción sin dar un solo error. Es la defensa de siempre
    contra el interruptor conectado a nada.
    """
    from app.analysis.encabezados import poner
    from app.analysis.rendimiento import vista_percepcion
    from app.analysis.series import comprobar_series

    comprobar_series(cfg)
    return poner("percepcion", vista_percepcion(s, dias=dias))


@app.get("/api/metrics/umbral")
def metrics_umbral(
    dias: int = Query(180, ge=7, le=730),
    metodo: str = Query("spearman"),
    s: Session = Depends(get_session),
    cfg=Depends(get_config),
) -> dict[str, Any]:
    """Vista 6: a partir de cuánta bici se nota, y cuántos días dura.

    Es la única vista que contesta con un NÚMERO DE CARGA en vez de con una `r`,
    y ésa es toda su razón de existir. Impacto ya dice que la carga y la HRV van
    de la mano; lo que no dice -y no puede decir, porque una correlación no
    tiene esa forma- es a partir de qué cifra empieza a notarse. Sin ese número
    la relación es cierta y no sirve para decidir nada el domingo por la mañana.

    Lleva `metodo` porque los cortes sí se contrastan: cada candidato compara
    dos grupos y eso sale de `correlacion`, igual que en las otras vistas.

    `cfg` se pasa a la vista aunque hoy no lo mire. Es a propósito y está
    explicado en `vista_umbral`: el día que las bandas salgan del YAML, esta
    función no se toca.
    """
    from app.analysis.encabezados import poner
    from app.analysis.series import comprobar_series
    from app.analysis.umbral import vista_umbral

    comprobar_series(cfg)
    return poner("umbral", vista_umbral(s, cfg, dias=dias, metodo=_metodo(metodo)))


@app.get("/api/metrics/calibracion")
def metrics_calibracion(
    dias: int = Query(180, ge=7, le=730),
    s: Session = Depends(get_session),
) -> dict[str, Any]:
    """Vista 7: dónde y hacia dónde no compartes lo que decide el motor.

    SOLO LEE, y aquí eso es más fuerte que en las demás. Esta vista se calcula
    sobre `previews`, que es el registro de lo que se pensó cada mañana: una
    pantalla que al abrirse escribiera ahí estaría cambiando el dato por mirarlo.
    Abrir las métricas no mueve ninguna de las tres medidas.

    No lleva `metodo` -no se correlaciona nada, se cuenta y se comparan dos
    medianas- y es la ÚNICA de las siete que no lleva `cfg`. Las demás llaman a
    `comprobar_series(cfg)` para que un deslizador del `config.yaml` que no esté
    en `series.DEFINICIONES` no se caiga de las métricas en silencio; aquí no hay
    ni un deslizador que se mire. Lo que se agrupa son reglas y colores, que
    salen de la decisión guardada, no del catálogo de señales. Pedir `cfg` para
    no usarlo sería la clave decorativa de siempre: un parámetro que parece que
    valida algo y no valida nada.
    """
    from app.analysis.calibracion import vista_calibracion
    from app.analysis.encabezados import poner

    return poner("calibracion", vista_calibracion(s, dias=dias))


@app.post("/api/probar/telegram")
def probar_telegram_endpoint(
    texto: str | None = None,
    cfg=Depends(get_config),
) -> dict[str, Any]:
    """Manda un mensaje de prueba de verdad al canal de siempre.

    Es POST y no GET a propósito, aunque "probar" suene a consulta: esto tiene
    un efecto en el mundo -llega un mensaje al móvil- y las cosas con efecto no
    se ponen detrás de un verbo que cualquier precargador de enlaces puede
    disparar solo.
    """
    from app.diagnostico import probar_telegram

    return probar_telegram(settings, cfg, texto=texto)


@app.post("/api/probar/hevy")
def probar_hevy_endpoint(cfg=Depends(get_config)) -> dict[str, Any]:
    """Comprueba la conexión con Hevy SIN escribir nada.

    Lee los entrenamientos del último mes y pide una por una las rutinas que el
    config dice que va a reescribir. Un identificador que apunta a una rutina
    borrada desde el móvil no daría la cara hasta la mañana en que toca
    escribirla.
    """
    from app.diagnostico import probar_hevy

    return probar_hevy(settings, cfg)


@app.post("/api/probar/garmin")
def probar_garmin_endpoint(cfg=Depends(get_config)) -> dict[str, Any]:
    """Entra en Garmin y lee un día reciente.

    El resultado dice si la sesión se reanudó de los tokens o si hubo login
    nuevo, porque de eso depende que se pueda repetir la prueba sin provocar un
    429.
    """
    from app.diagnostico import probar_garmin

    return probar_garmin(settings, cfg)


@app.post("/api/reconcile")
def post_reconcile(
    day: date | None = None,
    dias: int = Query(3, ge=0, le=365),
    s: Session = Depends(get_session),
    cfg=Depends(get_config),
) -> dict[str, Any]:
    """Relanza la reconciliación a mano. Es idempotente, así que no hace daño.

    EL TOPE ERA 30 Y ESE ERA EL PROBLEMA, NO LA PROTECCIÓN. Esta es la vía para
    rellenar un histórico que nadie apuntó -el trabajo nocturno solo alcanza su
    ventana-, y el histórico de esta cuenta empezaba 34 días atrás: el único uso
    serio del endpoint caía justo fuera de su propio límite.

    Un tope de un año no deja esto sin guarda, solo mueve la guarda al sitio
    donde el límite es de verdad: `get_workouts` pagina de diez en diez y LANZA
    si sus páginas no cubren la ventana pedida, diciendo que suba `max_pages`.
    Eso es un error en voz alta con instrucciones, que es exactamente lo que se
    quiere de un relleno lanzado a mano y mirando.
    """
    from app.runner import run_reconcile

    day = day or date.today()
    hevy, _, _ = _clientes(cfg)
    if hevy is None:
        raise HTTPException(status_code=503, detail="sin cliente de Hevy")

    desde = day - timedelta(days=dias)
    workouts = hevy.get_workouts(since=desde)
    out = []
    for i in range(dias + 1):
        d = desde + timedelta(days=i)
        r = run_reconcile(s, cfg, d, workouts=workouts)
        out.append({
            "day": d.isoformat(),
            "advanced": r.avanzado,
            "new": r.workouts_nuevos,
            "already_counted": r.workouts_ya_contados,
            "reason": r.motivo,
        })
    return {"results": out}


@app.get("/api/export")
def export_csv(
    desde: date | None = None,
    hasta: date | None = None,
    s: Session = Depends(get_session),
) -> StreamingResponse:
    """Histórico en CSV: decisiones, semáforo, check-in y qué se entrenó.

    Existe porque los datos son del usuario y tiene que poder sacarlos sin
    depender de esta aplicación ni de que siga existiendo.
    """
    hasta = hasta or date.today()
    desde = desde or (hasta - timedelta(days=365))

    filas = s.scalars(
        select(DecisionRow)
        .where(
            DecisionRow.date >= desde,
            DecisionRow.date <= hasta,
            DecisionRow.is_current.is_(True),
        )
        .order_by(DecisionRow.date)
    ).all()

    entrenados = {
        w.date for w in s.scalars(
            select(WorkoutLog).where(WorkoutLog.date >= desde, WorkoutLog.date <= hasta)
        ).all()
    }

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([
        "fecha", "semaforo", "regla_disparo", "origen", "sesion", "tipo",
        "entrenado", "progresiones", "config_hash",
    ])
    for f in filas:
        plan = repo.planned_session(f)
        w.writerow([
            f.date.isoformat(), f.light, f.trigger_rule or "", f.source,
            plan.get("title", ""), plan.get("kind", ""),
            "si" if f.date in entrenados else "no",
            "|".join(repo.progressed_keys(f)),
            f.config_hash or "",
        ])

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="entrenamiento_{desde}_{hasta}.csv"'
            )
        },
    )


# ---------------------------------------------------------------------------
# La PWA
#
# Se monta LA ÚLTIMA y en la raíz. `StaticFiles` en "/" se traga todo lo que no
# haya casado antes, así que si esto subiera de sitio se comería `/api/...` y la
# aplicación entera contestaría 404 en HTML. Va aquí abajo a propósito.
# ---------------------------------------------------------------------------

_ESTATICOS = Path(__file__).resolve().parent.parent / "static"

if _ESTATICOS.is_dir():
    @app.get("/sw.js", include_in_schema=False)
    def service_worker() -> FileResponse:
        """El service worker se sirve aparte por el ámbito.

        Un service worker solo controla las rutas por debajo de donde se sirve.
        Servido desde `/static/sw.js` gobernaría `/static/...` y nada más, o sea
        que la aplicación no se podría instalar y no habría ningún error: solo
        no saldría el botón de instalar. `Service-Worker-Allowed` no hace falta
        si el fichero está en la raíz, que es lo que se hace aquí.
        """
        return FileResponse(
            _ESTATICOS / "sw.js",
            media_type="application/javascript",
            # Sin esto, el navegador puede quedarse con un worker viejo y la
            # actualización del contenedor no llegaría nunca al móvil.
            headers={"Cache-Control": "no-cache"},
        )

    class _EstaticosQueSeRevalidan(StaticFiles):
        """`StaticFiles` diciendo cuánto se puede fiar el navegador. Que no es solo.

        EL FALLO QUE ARREGLA
        --------------------
        `StaticFiles` manda `ETag` y `Last-Modified` y NINGÚN `Cache-Control`. Sin
        esa cabecera el navegador no se queda sin caché: se queda sin instrucción,
        y entonces aplica «caducidad heurística» -reutiliza la respuesta sin
        preguntar durante una fracción del tiempo que lleva sin cambiar el
        fichero-. O sea que el navegador se inventa un plazo y no hay 304, no hay
        petición y no hay forma de enterarse.

        Encima de eso va el service worker, y ahí es donde duele. Su `fetch` está
        escrito «primero la red» -y su comentario lo promete: que una versión
        nueva del contenedor se note al primer arranque con cobertura-, pero
        `fetch()` pasa por el caché HTTP del navegador como cualquier otra
        petición. Con la caducidad heurística encima, «primero la red» se
        convierte en «primero lo viejo», y el service worker ADEMÁS guarda esa
        copia vieja en su propio caché. La promesa del comentario era falsa y no
        había forma de verlo: la pantalla sale entera, bien pintada y anterior.

        Se encontró desplegando el panel nuevo para mirarlo desde el móvil: el
        contenedor servía el `metricas.js` de ahora, el navegador ejecutaba el de
        antes y no había un solo error en ninguna consola.

        POR QUÉ `no-cache` Y NO `no-store`
        ----------------------------------
        `no-cache` no quiere decir «no lo guardes»: quiere decir «guárdalo, pero
        pregunta antes de usarlo». Como `StaticFiles` ya manda `ETag`, esa
        pregunta se contesta casi siempre con un 304 sin cuerpo. Sale gratis y
        sale siempre fresco. `no-store` obligaría a bajar la PWA entera en cada
        arranque para no ganar nada.

        Y NO ROMPE EL MODO SIN CONEXIÓN, que es la duda razonable: lo que sostiene
        ese modo es el `CacheStorage` del service worker, que es otro almacén y no
        mira esta cabecera. Sin red, el `fetch` de `sw.js` falla y se tira de ahí
        igual que antes.

        POR QUÉ AQUÍ Y NO EN UNA RUTA MÁS
        ---------------------------------
        Porque `/sw.js` ya tenía su `Cache-Control` puesto a mano, con el motivo
        escrito al lado, y eso NO bastó: el razonamiento valía para los otros doce
        ficheros del armazón y se quedó en el único donde alguien lo pensó. Poner
        la cabecera en el sitio por el que salen todos es lo que impide que el
        siguiente fichero nazca otra vez sin ella.
        """

        async def get_response(self, path: str, scope):  # type: ignore[no-untyped-def]
            respuesta = await super().get_response(path, scope)
            respuesta.headers["Cache-Control"] = "no-cache"
            return respuesta

    app.mount("/", _EstaticosQueSeRevalidan(directory=_ESTATICOS, html=True), name="pwa")
else:  # pragma: no cover
    log.warning("no hay carpeta `static/`: la PWA no se sirve")
