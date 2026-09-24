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


@no_es_doble(
    "no es doble de la fila `Decision` de `app/models.py`: son los cuatro "
    "campos que `job_decision` le lee para construir el `DecisionAnulada`. "
    "Nació de un fallo del arnés: con un `object()` pelado el trabajo "
    "reventaba con un AttributeError, y el `except Exception` de `_job` se lo "
    "tragaba, así que el test decía «no ha decidido» cuando lo que pasaba era "
    "que se había roto"
)
class DecisionPrevia:
    light = "amber"
    computed_at = "2026-09-20T06:25:00"
    source = "checkin"


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


# ---------------------------------------------------------------------------
# El recálculo temprano
# ---------------------------------------------------------------------------
#
# LA MEDIDA QUE LO JUSTIFICA, de los cinco primeros check-ins reales y con las
# horas ya pasadas a hora local (la base sella en UTC y España va +2; leerlas
# sin convertir hace pensar que el formulario se rellena a las 04:23):
#
#   15-09  06:23  faltaron hrv, hrv_ratio, rhr, rhr_delta, sleep_min
#   16-09  07:04  faltaron hrv, hrv_ratio, rhr, rhr_delta, sleep_min
#   18-09  06:25  faltaron hrv, hrv_ratio, sleep_min
#   19-09  07:13  nada
#   20-09  08:44  nada
#
# El reloj sube la noche entre las 07:04 y las 07:13. El formulario se rellena
# hacia las 07:00, o sea justo antes. Tres de cinco decisiones se tomaron
# ciegas, el mensaje lo dijo -«a esta hora el reloj todavía no había subido la
# noche... si el dato llega luego, el día se recalcula y te aviso»- y quien
# cumplía esa promesa era el fallback de las 09:00. Tarde: el entreno empieza
# sobre las 08:00, así que a las nueve la sesión ya está hecha con la rutina
# que escribió la decisión ciega.


def _job(cfg, monkeypatch, *, checkin, faltaron, llego=None, **kw):
    """Corre `job_decision` con el día ya sembrado y apunta si decidió.

    `llego` es lo que el reloj ha subido YA, y por defecto es todo lo que
    faltaba. No es un detalle del arnés: el trabajo no rehace la decisión por
    el hecho de que faltara algo, sino cuando ese algo ha llegado. Se descubrió
    escribiendo estos tests con las métricas vacías -el trabajo no decidía y
    tenía razón-.
    """
    import app.scheduler as mod

    decidido: list[str] = []
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        @contextmanager
        def scope():
            yield s

        monkeypatch.setattr(mod, "session_scope", scope)
        monkeypatch.setattr(mod.repo, "get_checkin", lambda *a, **k: object() if checkin else None)
        monkeypatch.setattr(mod.repo, "current_decision", lambda *a, **k: DecisionPrevia())
        monkeypatch.setattr(mod, "_medidas_que_faltaban", lambda fila: set(faltaron))
        monkeypatch.setattr(mod, "poner_al_dia_lo_entrenado", lambda *a, **k: [])
        monkeypatch.setattr(
            mod, "_fetch_garmin",
            lambda c, d: decidido.append("garmin") or ([], []),
        )
        monkeypatch.setattr(
            mod, "_lo_que_llego",
            lambda m, d, faltan: set(faltaron) if llego is None else set(llego),
        )
        monkeypatch.setattr(mod, "run_daily", lambda *a, **k: decidido.append("decidio"))
        mod.job_decision(cfg, day=HOY, **kw)
    return decidido


def test_el_recalculo_temprano_rehace_la_decision_ciega(cfg, monkeypatch):
    """El caso que existe: hay check-in y se decidió sin la noche."""
    hecho = _job(cfg, monkeypatch, checkin=True, faltaron={"hrv", "sleep_min"},
                 solo_recomputar=True)
    assert "decidio" in hecho, (
        "la decisión ciega no se rehace: el mensaje promete un recálculo que "
        "entonces no llega nunca a tiempo"
    )


def test_el_recalculo_temprano_no_toca_un_dia_que_se_decidio_con_todo(cfg, monkeypatch):
    """Si no faltó nada, rehacerlo escribiría en Hevy y mandaría un Telegram
    idénticos, y el segundo mensaje del día enseña a no leer el primero."""
    assert _job(cfg, monkeypatch, checkin=True, faltaron=set(),
                solo_recomputar=True) == []


def test_el_recalculo_temprano_no_hace_de_fallback(cfg, monkeypatch):
    """LO QUE HACE QUE PUEDA IR TAN TEMPRANO.

    Un día sin check-in a las 07:30 no es un día sin check-in: es un día en que
    todavía no se ha rellenado. Decidir ahí le quita al usuario la mañana
    entera para contestar, que es justo para lo que el fallback espera hasta
    las nueve. Sin esta rama, poner el trabajo temprano adelantaría el fallback
    hora y media sin que nadie lo hubiera pedido.
    """
    assert _job(cfg, monkeypatch, checkin=False, faltaron=set(),
                solo_recomputar=True) == [], (
        "sin check-in y a las 07:30 ha decidido igual: eso es adelantar el "
        "fallback, no recalcular"
    )


def test_el_fallback_de_las_nueve_si_decide_sin_checkin(cfg, monkeypatch):
    """El contrapeso. Sin esto, la rama de arriba podría estar apagando el
    fallback entero y los tres tests anteriores seguirían en verde."""
    assert "decidio" in _job(cfg, monkeypatch, checkin=False, faltaron=set()), (
        "el fallback de las 09:00 ha dejado de decidir los días sin check-in"
    )


def test_el_recalculo_va_antes_que_el_fallback(cfg):
    """Su razón de ser es llegar a tiempo. A la hora del fallback, o después,
    no aporta nada que el fallback no haga ya."""
    from app.scheduler import build_scheduler

    sched = build_scheduler(cfg, start=False)

    def minutos(job_id: str) -> int:
        campos = {f.name: str(f) for f in sched.get_job(job_id).trigger.fields}
        return int(campos["hour"]) * 60 + int(campos["minute"])

    assert minutos("recompute_early") < minutos("decision_fallback")


def test_si_el_reloj_sigue_sin_subir_la_noche_no_se_rehace_nada(cfg, monkeypatch):
    """Rehacer sin el dato sería escribir la misma decisión ciega otra vez.

    Con otra hora, otra fila en el histórico y un segundo Telegram idéntico. El
    trabajo no mira si FALTÓ algo, mira si ese algo ya está; lo descubrí al
    revés, escribiendo el test de arriba con las métricas vacías y viendo que
    no decidía. Tenía razón el código.
    """
    assert _job(cfg, monkeypatch, checkin=True, faltaron={"hrv", "sleep_min"},
                llego=set(), solo_recomputar=True) == ["garmin"], (
        "ha rehecho la decisión sin que el dato que faltaba haya llegado"
    )


# ---------------------------------------------------------------------------
# Una sola conexión escribiendo
# ---------------------------------------------------------------------------
#
# LA REGRESIÓN DEL 22-09-2026, que es la del 20 repetida y con otra causa. El
# sistema volvió a proponer una rutina hecha el día antes, esta vez el Día 1.
# El arreglo del 21 estaba desplegado y CORRIÓ -el log lo enseña ampliando la
# ventana a siete días a las 06:55:18- y treinta segundos después:
#
#   06:55:49 ERROR no se ha podido releer lo entrenado antes de decidir; se
#            decide con lo que hay en la base, que puede estar desfasado
#   sqlite3.OperationalError: database is locked
#
# El `try/except` hizo su trabajo: la mañana se decidió igual y quedó escrito
# que lo entrenado podía ir desfasado. Pero lo desfasado era justo lo que el
# arreglo venía a poner al día, así que el síntoma volvió entero.
#
# LA CAUSA ES DE PLOMERÍA Y ERA MÍA. `post_checkin` guarda el check-in en la
# sesión de la petición y llama a `_decidir` con ella todavía abierta;
# `poner_al_dia_lo_entrenado` abría una SEGUNDA conexión con `session_scope()`.
# SQLite admite un escritor: la segunda pide el lock, no lo consigue y revienta.
#
# Estos tests usan SQLite DE FICHERO y dos sesiones de verdad. Con
# `:memory:` no valdría: cada conexión tendría su propia base y el bloqueo -que
# es lo único que hay que reproducir- no se daría nunca.


def _engine_de_fichero(tmp_path, monkeypatch=None):
    """Una base en FICHERO, y `session_scope` apuntando a ella.

    Las dos mitades hacen falta y la segunda se me olvidó la primera vez: sin
    atar `session_scope` al mismo fichero, la conexión de respaldo se va a la
    base configurada de la aplicación, escribe allí tan tranquila y el bloqueo
    -que es TODO lo que estos tests reproducen- no llega a darse. Los tests
    salían verdes con el código roto de ayer.
    """
    from contextlib import contextmanager as ctx
    from sqlalchemy import create_engine as crear

    e = crear(f"sqlite:///{tmp_path / 'app.db'}", future=True)
    Base.metadata.create_all(e)

    if monkeypatch is not None:
        import app.scheduler as mod

        @ctx
        def otra_conexion():
            with Session(e) as s:
                yield s

        monkeypatch.setattr(mod, "session_scope", otra_conexion)
    return e


def _reconcile_que_escribe(monkeypatch):
    """Sustituye `run_reconcile` por una escritura mínima.

    Lo que se prueba aquí es de qué CONEXIÓN sale el INSERT, no qué reconcilia.
    Con la reconciliación de verdad el test necesitaría Hevy y entrenos
    sembrados, y el bloqueo -que es lo único que importa- quedaría escondido
    detrás de todo eso.
    """
    import app.scheduler as mod
    from app.models import JobRun

    def falso(s, cfg, d, **kw):
        s.add(JobRun(job_id=f"prueba-{d}"))
        s.flush()
        return f"reconciliado {d}"

    monkeypatch.setattr(mod, "run_reconcile", falso)


@no_es_doble("un cliente de Hevy del que solo se llama `get_workouts`")
class HevySinEntrenos:
    def get_workouts(self, since=None, **kw):
        return []


def test_reconciliar_con_la_sesion_que_se_le_pasa_no_bloquea_la_base(tmp_path, cfg, monkeypatch):
    """EL TEST. Con la sesión de la petición, la escritura sale por su conexión.

    Es el camino del check-in: el check-in ya está dentro de esa sesión y sin
    confirmar, así que la base tiene el lock de escritura cogido. Reconciliar
    por ahí mismo no lo pide otra vez.
    """
    import app.scheduler as mod

    _reconcile_que_escribe(monkeypatch)
    engine = _engine_de_fichero(tmp_path, monkeypatch)

    with Session(engine) as peticion:
        # El check-in recién guardado y sin confirmar: esto es lo que coge el
        # lock de escritura en SQLite y lo que hacía fallar a la segunda conexión.
        from app.models import JobRun

        peticion.add(JobRun(job_id="el-checkin-de-esta-manana"))
        peticion.flush()

        fuera = mod.job_reconcile(
            cfg, day=HOY, hevy_client=HevySinEntrenos(), session=peticion
        )

    assert fuera, "no ha reconciliado ningún día"


def test_sin_sesion_y_con_otra_escribiendo_es_cuando_se_bloquea(tmp_path, cfg, monkeypatch):
    """EL CONTRAPESO, y es el que demuestra que el test de arriba prueba algo.

    Sin él, `session=` podría no estar usándose para nada -abriendo igual su
    propia conexión- y el test anterior saldría verde de todas formas, porque
    con la base libre una segunda conexión escribe sin problema. Aquí se deja
    el lock cogido y se llama SIN sesión: tiene que reventar, y con el mismo
    error que salió el 22 de septiembre.
    """
    import app.scheduler as mod

    import pytest as pt
    from sqlalchemy.exc import OperationalError

    _reconcile_que_escribe(monkeypatch)
    engine = _engine_de_fichero(tmp_path, monkeypatch)

    with Session(engine) as peticion:
        from app.models import JobRun

        peticion.add(JobRun(job_id="el-checkin-de-esta-manana"))
        peticion.flush()

        with pt.raises(OperationalError, match="database is locked"):
            mod.job_reconcile(cfg, day=HOY, hevy_client=HevySinEntrenos())


def test_el_checkin_le_pasa_su_propia_sesion(tmp_path, cfg, monkeypatch):
    """Y que el camino del check-in la pase de verdad, que es lo que faltaba.

    Los dos tests de arriba prueban que `job_reconcile` sabe recibir una
    sesión. Éste prueba que `_decidir` se la da: sin esta línea, saber
    recibirla no sirve de nada y el 22 de septiembre se repite igual.
    """
    import app.api as api
    import app.scheduler as mod

    recibido: dict = {}
    monkeypatch.setattr(api, "_clientes", lambda cfg: (HevyCualquiera(), None, {}))
    monkeypatch.setattr(
        mod, "poner_al_dia_lo_entrenado",
        lambda c, **kw: recibido.update(kw) or [],
    )
    monkeypatch.setattr(mod, "_fetch_garmin", lambda c, d: ([], []))

    engine = _engine_de_fichero(tmp_path)
    with Session(engine) as s:
        api._decidir(s, cfg, HOY, source="checkin")

    assert recibido.get("session") is s, (
        "el check-in no le pasa su sesión: se abrirá una segunda conexión y "
        "SQLite contestará `database is locked` con el check-in sin confirmar"
    )


def test_el_ayudante_reenvia_la_sesion_que_recibe(cfg, monkeypatch):
    """El eslabón del medio, que se me quedó sin cubrir.

    La cadena tiene tres: `_decidir` le da su sesión al ayudante, el ayudante
    se la pasa a `job_reconcile`, y `job_reconcile` la usa en vez de abrir
    otra. Los otros dos tests de este bloque llaman a `job_reconcile` DIRECTO,
    así que se saltan éste: quitar el reenvío no ponía rojo a nadie y la
    cadena volvía a romperse por el mismo sitio, con el mismo
    `database is locked`.
    """
    import app.scheduler as mod

    espia = Contador()
    monkeypatch.setattr(mod, "job_reconcile", espia)
    marca = object()

    poner_al_dia_lo_entrenado(
        cfg, hevy_client=HevyCualquiera(), day=HOY, session=marca
    )

    assert espia.llamadas[0].get("session") is marca, (
        f"el ayudante no reenvía la sesión: {espia.llamadas[0]}. Sin ella "
        f"`job_reconcile` abre una segunda conexión y SQLite la bloquea"
    )


# ---------------------------------------------------------------------------
# La reconciliación del arranque
# ---------------------------------------------------------------------------
#
# LA TERCERA VEZ EN CINCO DÍAS que el sistema propone una rutina ya hecha, y la
# tercera causa distinta. Las dos primeras estaban arregladas y desplegadas; el
# 24-09-2026 volvió a pasar igual, y al mirar la secuencia se ve que ninguno de
# los dos arreglos podía haberlo evitado:
#
#   martes 22, 06:55  check-in enviado
#   martes 22, 07:22  se ENTRENA el Día 2, media hora DESPUÉS del check-in
#   martes 22, 22:30  la reconciliación nocturna no corre: el equipo está apagado
#   miércoles 23      se previsualiza pero NO se envía -> no hay decisión que
#                     arrastre la reconciliación previa
#   jueves 24         tampoco se ha enviado, y el sistema propone el Día 2
#
# El entreno del martes existía en Hevy desde las 07:22 y nadie lo leyó en dos
# días. Ni siquiera una reconciliación perfecta esa mañana lo habría visto: aún
# no había ocurrido.
#
# O SEA QUE LA CADENA COLGABA DE QUE SE ENVIARA EL FORMULARIO. Era la única
# pieza que de verdad se ejecutaba, y basta con no contestar dos mañanas para
# que el sistema decida con lo que sabía del lunes.
#
# El arranque es lo único que sí ocurre todos los días mientras el equipo se
# encienda, y no depende de que nadie conteste nada.


def test_al_arrancar_se_lee_lo_entrenado(cfg):
    """El trabajo existe y se dispara al arrancar, no a una hora."""
    from app.scheduler import build_scheduler

    sched = build_scheduler(cfg, start=False)
    j = sched.get_job("reconcile_arranque")

    assert j is not None, (
        "no hay reconciliación de arranque: la única que se ejecuta de verdad "
        "vuelve a ser la que depende de que se envíe el check-in"
    )
    assert not getattr(j.trigger, "fields", None), (
        "la reconciliación de arranque tiene hora fija. Entonces es otra cita "
        "más que perder con el equipo apagado, que es el problema que viene a "
        "resolver"
    )


def test_la_del_arranque_y_la_de_las_2230_son_el_mismo_trabajo(cfg):
    """Y no una variante, que es donde empezarían a divergir.

    `job_reconcile` calcula su propia ventana mirando `job_runs`, así que al
    arrancar recupera exactamente lo que quedó sin apuntar. Una función aparte
    «para el arranque» tendría que decidir otra vez cuánto mirar hacia atrás, y
    dos respuestas a esa pregunta acaban siendo dos ventanas distintas.
    """
    from app.scheduler import build_scheduler, job_reconcile

    sched = build_scheduler(cfg, start=False)
    assert sched.get_job("reconcile_arranque").func is job_reconcile
    assert sched.get_job("reconcile").func is job_reconcile


def test_la_del_arranque_lleva_su_cliente_de_hevy(cfg):
    """Sin cliente no lee nada, y `job_reconcile` lanzaría.

    Es el mismo olvido que haría inútil todo lo demás: un trabajo registrado,
    corriendo cada arranque y reventando siempre. Saltaría por Telegram -para
    eso está `_avisador`- pero el dato seguiría sin leerse.
    """
    from app.scheduler import build_scheduler

    sched = build_scheduler(cfg, start=False)
    assert "hevy_client" in (sched.get_job("reconcile_arranque").kwargs or {})
