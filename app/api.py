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
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
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


def get_config():
    """El `config.yaml`, cargado una vez por proceso."""
    if not hasattr(get_config, "_cache"):
        get_config._cache = load_config(settings.config_path)
    return get_config._cache


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    faltan = settings.missing_secrets()
    if faltan:
        # Se arranca igual -hay que poder abrir el formulario para ver qué
        # falta- pero no se hace como si nada.
        log.warning("faltan secretos en el .env: %s", ", ".join(faltan))
    yield


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
def health(cfg=Depends(get_config)) -> dict[str, Any]:
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
        hevy, tg = _clientes(cfg)
        res = run_daily(
            s, cfg, day, metrics=metrics, rides=rides,
            hevy_client=hevy, telegram_client=tg,
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


def _clientes(cfg) -> tuple[Any, Any]:
    from app.integrations.hevy import build_client as hevy_client
    from app.integrations.telegram import build_client as tg_client

    hevy = tg = None
    try:
        hevy = hevy_client(settings, cfg)
    except Exception as exc:  # noqa: BLE001
        log.warning("sin cliente de Hevy: %s", exc)
    try:
        tg = tg_client(settings, cfg)
    except Exception as exc:  # noqa: BLE001
        log.warning("sin cliente de Telegram: %s", exc)
    return hevy, tg


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
    hevy, _ = _clientes(cfg)
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
