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
    comments: str | None = None
    # Permite rehacer el check-in de ayer sin mentirle a la fecha.
    day: date | None = None


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health(request: Request, cfg=Depends(get_config)) -> dict[str, Any]:
    """Sirve para el healthcheck de Docker y para ver qué falta.

    Devuelve `secrets_missing` en vez de esconderlo: un sistema arrancado a
    medias que contesta "ok" es peor que uno caído, porque nadie va a mirar.
    """
    return {
        "status": "ok",
        "config_hash": cfg.hash,
        "timezone": cfg.timezone,
        "secrets_missing": settings.missing_secrets(),
        "dry_run": settings.dry_run,
        # Los dos interruptores de escritura, al lado del `dry_run`. Estaban
        # repartidos entre el `.env` y el YAML, y para saber si el sistema iba a
        # tocar algo hacia fuera había que abrir dos ficheros y acordarse de que
        # el que manda es el que se cargó al arrancar, no el que está en disco.
        "writes": _estado_escrituras(cfg),
        # Sin esto, una aplicación sin planificador es indistinguible de una
        # sana: sirve la PWA, contesta 200, y no decide nunca. Se dice cuántos
        # trabajos hay y cuándo toca cada uno, porque "arrancado" tampoco basta:
        # un planificador vivo con cero trabajos falla exactamente igual.
        "scheduler": _estado_planificador(request),
        "clock": _estado_del_reloj(cfg),
        "config_file": _estado_del_config(cfg),
    }


def _estado_escrituras(cfg) -> dict[str, Any]:
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
    """
    from app.integrations.hevy import read_pending

    raw = cfg.raw if hasattr(cfg, "raw") else (cfg or {})
    hevy_cfg = ((raw.get("integrations") or {}).get("hevy") or {})
    tg_cfg = ((raw.get("integrations") or {}).get("telegram") or {})

    try:
        pendiente = read_pending(_raiz_de_datos())
    except Exception as e:  # noqa: BLE001 - el healthcheck no puede caerse
        # Que no se pueda leer la marca es en sí mismo un dato: significa que no
        # se sabe si hay una escritura a medias. Se dice, en vez de contestar
        # que no hay ninguna, que es lo que haría un `except` silencioso.
        return {
            "hevy_write_enabled": bool(hevy_cfg.get("write_enabled")),
            "telegram_send_enabled": bool(tg_cfg.get("send_enabled")),
            "pending_write": None,
            "pending_error": f"no se ha podido leer la marca de escritura: {e}",
        }

    return {
        "hevy_write_enabled": bool(hevy_cfg.get("write_enabled")),
        "telegram_send_enabled": bool(tg_cfg.get("send_enabled")),
        # `None` cuando no hay nada a medias. Cuando lo hay, sale el contenido
        # entero de la marca: rutina, fecha y hora del intento.
        "pending_write": pendiente,
    }


def _raiz_de_datos() -> Path:
    """La carpeta de datos, deducida de `database_url`."""
    url = str(settings.database_url)
    return Path(url.split("///")[-1]).parent if "///" in url else Path("data")


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
        salida["error"] = (
            "el config.yaml del disco NO es el que está cargado en memoria. "
            "Los cambios no se aplican hasta recrear el contenedor: "
            "docker compose up -d --force-recreate app"
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


def _estado_planificador(request: Request) -> dict[str, Any]:
    sched = getattr(request.app.state, "scheduler", None)
    error = getattr(request.app.state, "scheduler_error", None)
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
        "comment_label": (cfg.raw.get("checkin_comment") or {}).get(
            "label", "Comentarios"
        ),
    }


@app.post("/api/checkin")
def post_checkin(
    body: CheckinIn,
    s: Session = Depends(get_session),
    cfg=Depends(get_config),
) -> dict[str, Any]:
    """Guarda el check-in y decide el día con él dentro.

    Las dos cosas van juntas a propósito: es el camino normal del sistema. Si se
    guardara aquí y se decidiera en otro sitio, el usuario enviaría el
    formulario y no pasaría nada visible hasta una hora después.
    """
    day = body.day or date.today()
    valores = {
        k: v
        for k, v in body.model_dump(exclude={"comments", "day"}).items()
        if v is not None
    }

    try:
        repo.upsert_checkin(s, day, valores, config=cfg, comments=body.comments)
    except ValueError as exc:
        # Clave desconocida: 400 y no un 200 que se traga el campo.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    resultado = _decidir(s, cfg, day, source="checkin")
    return {
        "day": day.isoformat(),
        "checkin_saved": True,
        **resultado,
    }


def _decidir(s: Session, cfg, day: date, *, source: str) -> dict[str, Any]:
    """Decide el día, y si no puede lo dice sin fingir que sí."""
    from app.runner import run_daily
    from app.scheduler import _fetch_garmin

    try:
        metrics, rides = _fetch_garmin(cfg, day)
    except Exception as exc:  # noqa: BLE001
        log.exception("no se pudo leer Garmin al decidir")
        return {
            "decided": False,
            "error": f"el check-in SÍ se ha guardado, pero no se pudo leer "
                     f"Garmin y por tanto no se ha decidido: {exc}",
        }

    try:
        hevy, tg, motivos = _clientes(cfg)
        res = run_daily(
            s, cfg, day, metrics=metrics, rides=rides,
            hevy_client=hevy, telegram_client=tg, client_errors=motivos,
            dry_run=settings.dry_run, source=source,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("fallo decidiendo")
        return {
            "decided": False,
            "error": f"el check-in SÍ se ha guardado, pero la decisión ha "
                     f"fallado: {exc}",
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
    estado = repo.load_state(s, program_start=cfg.program_start)
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


@app.get("/api/metrics/concordancia")
def metrics_concordancia(
    dias: int = Query(180, ge=7, le=730),
    metodo: str = Query("spearman"),
    s: Session = Depends(get_session),
    cfg=Depends(get_config),
) -> dict[str, Any]:
    """Vista 1: lo que nota frente a lo que mide el reloj, el mismo día."""
    from app.analysis.concordancia import vista_concordancia
    from app.analysis.series import comprobar_sliders

    comprobar_sliders(cfg)
    return vista_concordancia(s, dias=dias, metodo=_metodo(metodo))


@app.get("/api/metrics/desfase")
def metrics_desfase(
    dias: int = Query(180, ge=7, le=730),
    metodo: str = Query("spearman"),
    s: Session = Depends(get_session),
    cfg=Depends(get_config),
) -> dict[str, Any]:
    """Vista 2: la misma pregunta corriendo la ventana de -3 a +3 días."""
    from app.analysis.concordancia import vista_desfase
    from app.analysis.series import comprobar_sliders

    comprobar_sliders(cfg)
    return vista_desfase(s, dias=dias, metodo=_metodo(metodo))


@app.get("/api/metrics/impacto")
def metrics_impacto(
    dias: int = Query(180, ge=7, le=730),
    metodo: str = Query("spearman"),
    s: Session = Depends(get_session),
    cfg=Depends(get_config),
) -> dict[str, Any]:
    """Vista 3: qué le hace al cuerpo cada cosa, uno, dos y tres días después."""
    from app.analysis.impacto import vista_impacto
    from app.analysis.series import comprobar_sliders

    comprobar_sliders(cfg)
    return vista_impacto(s, dias=dias, metodo=_metodo(metodo))


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
    from app.analysis.impacto import ranking_ejercicios
    from app.analysis.series import comprobar_sliders

    comprobar_sliders(cfg)
    try:
        return ranking_ejercicios(
            s, cfg, respuesta=respuesta, dias=dias, metodo=_metodo(metodo)
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
    from app.analysis.series import comprobar_sliders

    comprobar_sliders(cfg)
    return vista_auditoria(s, cfg, dias=dias)


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

    `comprobar_sliders` sí se llama, aunque esta vista no lea los deslizadores
    de la base: los lee de `series.DEFINICIONES` para saber el sentido de cada
    uno, y un deslizador que exista en el formulario y no en esa tabla se caería
    del índice de percepción sin dar un solo error. Es la defensa de siempre
    contra el interruptor conectado a nada.
    """
    from app.analysis.rendimiento import vista_percepcion
    from app.analysis.series import comprobar_sliders

    comprobar_sliders(cfg)
    return vista_percepcion(s, dias=dias)


@app.post("/api/reconcile")
def post_reconcile(
    day: date | None = None,
    dias: int = Query(3, ge=0, le=30),
    s: Session = Depends(get_session),
    cfg=Depends(get_config),
) -> dict[str, Any]:
    """Relanza la reconciliación a mano. Es idempotente, así que no hace daño."""
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

    app.mount("/", StaticFiles(directory=_ESTATICOS, html=True), name="pwa")
else:  # pragma: no cover
    log.warning("no hay carpeta `static/`: la PWA no se sirve")
