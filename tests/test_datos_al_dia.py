"""Decidir con lo entrenado al día, y no con lo que hubiera en la base.

EL FALLO QUE LO TRAE
--------------------
El 20 de septiembre de 2026, un domingo, el sistema propuso Día 3. El Día 3 se
había hecho el viernes 18. No se equivocaba: la rotación se lee de lo
EJECUTADO, el entreno del viernes estaba en Hevy y no en la base, y el último
que el sistema conocía era el Día 2 del miércoles. Con su información, acertaba.

`reconcile` -el trabajo que lee de Hevy lo entrenado- corre a las 22:30 y
llevaba desde el 15 sin ejecutarse una sola vez, porque a esa hora el ordenador
estaba apagado. El sistema lo detectó y trató de avisar; el Telegram falló por
un error de DNS diez segundos después de arrancar el contenedor, y la marca de
«ya revisado» avanzó igual, así que el aviso se perdió para siempre.

LA ASIMETRÍA QUE NADIE HABÍA MIRADO
-----------------------------------
Los dos caminos que deciden -`job_decision` a las 09:00 y `_decidir` cuando se
envía el check-in- llamaban a `_fetch_garmin` como primerísima cosa. O sea que
Garmin SIEMPRE se lee fresco en el momento de decidir. Lo entrenado no tenía
ese equivalente: se leía una vez al día, de noche, y si esa vez no ocurría la
decisión de la mañana trabajaba con lo que hubiera.

Esa diferencia no respondía a ninguna razón. Era el orden en que se escribieron
las piezas.

LO QUE SE COMPRUEBA AQUÍ
------------------------
Que los dos caminos releen Hevy antes de decidir, que un fallo de Hevy no deja
la mañana sin decisión, y que releer de más no rompe nada.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base
from app.scheduler import poner_al_dia_lo_entrenado
from tests.dobles import no_es_doble

HOY = date(2026, 9, 20)


@no_es_doble(
    "no representa nada de `app/`: cuenta llamadas a `job_reconcile`, que es "
    "una función y no una clase"
)
class Contador:
    def __init__(self, *, revienta: bool = False):
        self.revienta = revienta
        self.llamadas: list[dict] = []

    def __call__(self, cfg, *, day=None, hevy_client=None, **kw):
        self.llamadas.append({"day": day, "hevy_client": hevy_client, **kw})
        if self.revienta:
            raise RuntimeError("Hevy contesta 503")
        return ["lo que devuelva reconcile"]


@no_es_doble("un cliente de Hevy visto solo como 'algo que no es None'")
class HevyCualquiera:
    pass


# ---------------------------------------------------------------------------
# El ayudante, por su cuenta
# ---------------------------------------------------------------------------


def test_releer_lo_entrenado_llama_a_la_reconciliacion(cfg, monkeypatch):
    """Sin condición previa: `job_reconcile` ya decide su propia ventana.

    Preguntar aquí «¿hace falta?» sería un segundo juez sobre la misma
    pregunta que `ventana_de_reconciliacion` contesta por dentro mirando
    `job_runs`, y dos jueces del mismo dato acaban discrepando.
    """
    import app.scheduler as mod

    espia = Contador()
    monkeypatch.setattr(mod, "job_reconcile", espia)
    cli = HevyCualquiera()

    fuera = poner_al_dia_lo_entrenado(cfg, hevy_client=cli, day=HOY)

    assert len(espia.llamadas) == 1
    assert espia.llamadas[0]["day"] == HOY
    assert espia.llamadas[0]["hevy_client"] is cli
    assert fuera == ["lo que devuelva reconcile"]


def test_si_hevy_revienta_la_manana_se_decide_igual(cfg, monkeypatch, caplog):
    """LA PARTE QUE IMPORTA. Esto se cuela en el camino de la decisión.

    Que Hevy esté caído puede dejar la rotación desfasada un día. No puede
    dejar la mañana sin decidir: eso convierte una molestia en una avería, y
    encima en la única función que el sistema tiene que cumplir sí o sí.
    """
    import logging

    import app.scheduler as mod

    monkeypatch.setattr(mod, "job_reconcile", Contador(revienta=True))

    with caplog.at_level(logging.ERROR):
        fuera = poner_al_dia_lo_entrenado(cfg, hevy_client=HevyCualquiera(), day=HOY)

    assert fuera == [], "un fallo no puede pasar por una reconciliación vacía"
    assert any("desfasado" in r.message for r in caplog.records), (
        "el fallo se ha tragado sin dejar constancia: el día que la rotación "
        "vaya atrasada no habrá dónde mirar por qué"
    )


def test_sin_cliente_de_hevy_no_se_inventa_una_lectura(cfg, monkeypatch):
    """No es una avería de este camino, y por eso no revienta aquí.

    `job_reconcile` ya lanza por su cuenta cuando le falta el cliente, y por
    ahí sale el Telegram. Duplicar el grito desde aquí mandaría dos avisos del
    mismo problema.
    """
    import app.scheduler as mod

    espia = Contador()
    monkeypatch.setattr(mod, "job_reconcile", espia)

    assert poner_al_dia_lo_entrenado(cfg, hevy_client=None, day=HOY) == []
    assert not espia.llamadas, "se ha llamado a reconcile sin cliente"


# ---------------------------------------------------------------------------
# Los dos caminos que deciden
# ---------------------------------------------------------------------------


def test_el_trabajo_de_las_nueve_relee_antes_de_decidir(cfg, monkeypatch):
    """El camino del fallback."""
    import app.scheduler as mod

    llamadas: list[str] = []
    monkeypatch.setattr(
        mod, "poner_al_dia_lo_entrenado",
        lambda *a, **k: llamadas.append("entrenado") or [],
    )
    monkeypatch.setattr(
        mod, "_fetch_garmin", lambda cfg, day: llamadas.append("garmin") or ([], []),
    )

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        @contextmanager
        def scope():
            yield s

        monkeypatch.setattr(mod, "session_scope", scope)
        monkeypatch.setattr(mod, "run_daily", lambda *a, **k: None)
        try:
            mod.job_decision(cfg, day=HOY, hevy_client=HevyCualquiera())
        except Exception:
            # Lo que pase después de las dos lecturas no es asunto de este test:
            # `run_daily` está anulado y el resto del trabajo no tiene con qué
            # seguir. Lo que se comprueba es el ORDEN de lo que va antes.
            pass

    assert "entrenado" in llamadas, (
        "el trabajo de las 09:00 decide sin releer lo entrenado: puede proponer "
        "una rutina que ya se hizo"
    )
    assert llamadas.index("entrenado") < llamadas.index("garmin"), (
        "lo entrenado se relee DESPUÉS de Garmin. Va antes porque es lo que "
        f"decide QUÉ rutina toca: {llamadas}"
    )


def test_el_checkin_del_movil_relee_antes_de_decidir(cfg, monkeypatch):
    """El camino por el que se coló el fallo del 20-09-2026.

    El fallback de las 09:00 ni siquiera llegó a actuar ese día: a las 06:44 ya
    había check-in enviado desde el móvil, y fue esa decisión la que propuso un
    Día 3 hecho dos días antes. Cubrir solo el trabajo de las nueve habría
    dejado el fallo exactamente donde estaba.
    """
    import app.api as api

    llamadas: list[str] = []
    monkeypatch.setattr(
        api, "_clientes", lambda cfg: (HevyCualquiera(), None, {}),
    )

    import app.scheduler as mod
    monkeypatch.setattr(
        mod, "poner_al_dia_lo_entrenado",
        lambda *a, **k: llamadas.append("entrenado") or [],
    )
    monkeypatch.setattr(
        mod, "_fetch_garmin",
        lambda cfg, day: llamadas.append("garmin") or ([], []),
    )

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        api._decidir(s, cfg, HOY, source="checkin")

    assert "entrenado" in llamadas, (
        "el check-in del móvil decide sin releer lo entrenado. Es el camino "
        "exacto del fallo del 20-09-2026"
    )
    assert llamadas.index("entrenado") < llamadas.index("garmin"), (
        f"lo entrenado tiene que releerse antes que Garmin: {llamadas}"
    )
