"""El día completo: decidir, escribir, avisar y luego enterarse de qué se hizo.

Lo que se vigila aquí no son los caminos felices sino las costuras, que es donde
este sistema puede hacer daño sin dar un error:

- que la reconciliación no cuente dos veces el mismo entrenamiento, porque
  contarlo dos veces sube la carga antes de tiempo;
- que un fallo de Hevy no se convierta en un mensaje que describe una rutina
  inexistente;
- que reconciliar no borre las reglas activas de rebote;
- que un ejercicio que sube por la mañana no conserve la racha por la noche.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.engine.decision import ActiveRule, EngineState, apply_execution
from app.models import Base, HevyWrite, Notification, WorkoutLog
from app.repository import load_state, save_decision, save_state
from app.runner import run_daily, run_reconcile
from tests.conftest import LUNES, dias, sig_completa

# `with_pool` no programa `dia_3`; los tests que necesitan fuerza usan cfg_summer
# o un lunes, que sí entrena.


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


# ---------------------------------------------------------------------------
# Dobles de los clientes
# ---------------------------------------------------------------------------


class HevyFalso:
    def __init__(self, *, revienta: bool = False, escribe: bool = True):
        self.revienta = revienta
        self.escribe = escribe
        self.llamadas: list[tuple[str, dict]] = []

    def write_routine(self, routine_id, payload, *, dry_run=False):
        self.llamadas.append((routine_id, payload))
        if self.revienta:
            raise RuntimeError("la API de Hevy ha devuelto 500")
        from app.integrations.hevy import WriteResult

        return WriteResult(
            written=self.escribe, routine_id=routine_id, reason="ok de mentira"
        )


class TelegramFalso:
    def __init__(self, *, revienta: bool = False):
        self.revienta = revienta
        self.enviados: list[str] = []

    def send(self, texto, *, dry_run=False):
        self.enviados.append(texto)
        if self.revienta:
            raise RuntimeError("Telegram no contesta")
        from app.integrations.telegram import SendResult

        return SendResult(sent=True, parts=1, reason="")


def metricas():
    return dias(LUNES, 10, hrv=60.0, rhr=50.0, sleep_min=450, sleep_score=80)


def corre(db, cfg, *, hevy=None, tg=None, day=LUNES, **kw):
    return run_daily(
        db, cfg, day,
        metrics=metricas(), rides=[],
        hevy_client=hevy, telegram_client=tg, **kw,
    )


# ---------------------------------------------------------------------------
# La mañana
# ---------------------------------------------------------------------------


def test_la_mañana_decide_guarda_y_avisa(db, cfg):
    tg = TelegramFalso()
    res = corre(db, cfg, hevy=HevyFalso(), tg=tg)

    assert res.decision is not None
    assert res.telegram_status == "sent"
    assert tg.enviados, "no se ha mandado ningún mensaje"
    assert db.scalars(select(Notification)).first() is not None


def test_un_dia_que_no_se_conto_a_nadie_queda_registrado(db, cfg):
    """Sin cliente de Telegram la decisión se toma igual, pero hay que apuntarlo.

    Antes se salía de `_mandar_telegram` antes de escribir la fila, así que esos
    días no dejaban rastro en `notifications`. En el histórico, un día en el que
    nadie se enteró y un día avisado correctamente se veían igual: los dos sin
    nada raro. Ahora queda la fila con `status="skipped"` y el motivo escrito.
    """
    res = corre(db, cfg, hevy=HevyFalso(), tg=None)

    fila = db.scalars(select(Notification)).first()
    assert fila is not None, "el día que nadie se entera no deja rastro"
    assert fila.status == "skipped"
    assert "Telegram" in (fila.error or "")
    assert res.problemas, "y además tiene que viajar al cliente en el momento"


def test_la_mañana_no_avanza_las_rachas(db, cfg):
    """A las siete la sesión no se ha hecho todavía.

    Si la racha avanzara aquí, avanzaría por el simple hecho de haber decidido:
    el peso subiría por días transcurridos y no por sesiones completadas.

    La racha se siembra ANTES a propósito. La primera versión de este test
    afirmaba `all(v == 0 ...)` sobre el estado recién creado, que está vacío: la
    comprobación era cierta por no tener nada que comprobar y habría pasado
    igual con el motor haciendo cualquier cosa. Sembrando un valor distinto de
    cero la afirmación pasa a ser "lo dejó como estaba", que es lo que se quiere
    decir, y además distingue avanzar de resetear -las dos cosas están mal aquí
    y un `== 0` solo detecta una-.
    """
    previo = EngineState(
        clean_sessions={("dia_1", "hip_thrust_barra"): 2},
        program_start=cfg.program_start,
    )
    save_state(db, previo, day=LUNES - timedelta(days=2))

    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())

    estado = load_state(db, program_start=cfg.program_start)
    assert estado.clean_sessions.get(("dia_1", "hip_thrust_barra")) == 2, (
        f"la mañana ha tocado la racha sin que nadie haya entrenado: "
        f"{estado.clean_sessions}"
    )


def test_si_hevy_falla_el_mensaje_sale_igual_y_lo_dice(db, cfg):
    """Las dos alternativas son peores que un aviso feo.

    Callarse deja al usuario sin plan y sin saber por qué; mandar el mensaje de
    siempre le describe una rutina que en la aplicación no está.
    """
    tg = TelegramFalso()
    res = corre(db, cfg, hevy=HevyFalso(revienta=True), tg=tg)

    assert res.hevy_status == "error"
    assert tg.enviados, "Hevy falló y además nadie se enteró"
    assert "NO se ha escrito en Hevy" in tg.enviados[0]
    assert res.problemas


def test_un_fallo_de_hevy_queda_registrado(db, cfg):
    corre(db, cfg, hevy=HevyFalso(revienta=True), tg=TelegramFalso())
    fila = db.scalars(select(HevyWrite)).first()
    assert fila is not None and fila.status == "error"
    assert fila.error


def test_sin_cliente_de_telegram_se_avisa_de_que_nadie_se_ha_enterado(db, cfg):
    """Un sistema que decide y no lo cuenta ha fallado, aunque no dé error."""
    res = corre(db, cfg, hevy=HevyFalso(), tg=None)
    assert res.telegram_status == "skipped"
    assert any("no se ha contado a nadie" in p for p in res.problemas)


def test_la_decision_se_guarda_con_su_progresion(db, cfg):
    """Sin esto la noche no puede saber qué subió, y la racha sobreviviría."""
    from app.models import Decision as DecisionRow

    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    fila = db.scalars(select(DecisionRow)).first()
    assert fila.planned_session_json
    # Puede no haber progresión un día concreto, pero la columna tiene que
    # existir y poder llenarse.
    assert hasattr(fila, "progression_json")


# ---------------------------------------------------------------------------
# La noche
# ---------------------------------------------------------------------------


def _entrenamiento_completo(plan: dict, wid: str = "w1", day: date = LUNES) -> dict:
    """Un entrenamiento que cumple el plan entero, construido DESDE el plan."""
    ejercicios = []
    for ex in plan.get("exercises") or []:
        ejercicios.append(
            {
                "exercise_template_id": ex.get("template_id"),
                "sets": [
                    {
                        "type": s.get("type", "normal"),
                        "reps": s.get("reps"),
                        "duration_seconds": s.get("duration_s"),
                    }
                    for s in ex.get("sets") or []
                ],
            }
        )
    return {
        "id": wid,
        "start_time": f"{day.isoformat()}T18:00:00Z",
        "title": "Sesión",
        "exercises": ejercicios,
    }


def _plan_guardado(db, day: date = LUNES):
    from app.repository import current_decision, planned_session

    return planned_session(current_decision(db, day))


def test_reconciliar_avanza_la_racha(db, cfg):
    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    plan = _plan_guardado(db)
    w = _entrenamiento_completo(plan)

    res = run_reconcile(db, cfg, LUNES, workouts=[w])
    assert res.avanzado, res.motivo

    estado = load_state(db, program_start=cfg.program_start)
    assert any(v > 0 for v in estado.clean_sessions.values()), (
        "se completó la sesión entera y no ha avanzado ninguna racha"
    )


def test_reconciliar_dos_veces_no_cuenta_dos_veces(db, cfg):
    """El seguro que impide que la carga suba antes de tiempo.

    Este job se reintenta si falla y se puede lanzar a mano. Contar dos veces el
    mismo entrenamiento subiría el peso con la mitad de las sesiones limpias que
    lo justifican, sin un solo error por ninguna parte.
    """
    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    w = _entrenamiento_completo(_plan_guardado(db))

    run_reconcile(db, cfg, LUNES, workouts=[w])
    primera = dict(load_state(db, program_start=cfg.program_start).clean_sessions)

    segunda_res = run_reconcile(db, cfg, LUNES, workouts=[w])
    segunda = dict(load_state(db, program_start=cfg.program_start).clean_sessions)

    assert not segunda_res.avanzado
    assert segunda_res.workouts_ya_contados == 1
    assert segunda == primera, "la racha ha avanzado dos veces con un solo entrenamiento"


def test_reconciliar_no_borra_las_reglas_activas(db, cfg):
    """La trampa de llamar a `advance_state` con una decisión rehidratada.

    `advance_state` reconstruye `active_rules` desde la decisión que recibe. Al
    reconciliar de noche esa decisión vendría de la base de datos y llegaría
    incompleta, así que las reglas se vaciarían en silencio: el peso muerto
    retirado catorce días reaparecería esa misma noche.
    """
    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())

    estado = load_state(db, program_start=cfg.program_start)
    estado.active_rules = [
        ActiveRule(
            name="lumbar_retirada",
            action={"drop_exercises": ["peso_muerto_smith"]},
            active_from=LUNES,
            active_until=LUNES + timedelta(days=14),
            reason="molestia lumbar",
        )
    ]
    save_state(db, estado, day=LUNES)

    run_reconcile(db, cfg, LUNES, workouts=[_entrenamiento_completo(_plan_guardado(db))])

    despues = load_state(db, program_start=cfg.program_start)
    assert [r.name for r in despues.active_rules] == ["lumbar_retirada"]


def test_un_dia_sin_decision_guardada_no_reconcilia_nada(db, cfg):
    """Sin plan no hay contra qué comparar, y no se inventa uno."""
    res = run_reconcile(db, cfg, LUNES, workouts=[])
    assert not res.avanzado
    assert "no hay decisión guardada" in res.motivo


def test_un_entrenamiento_de_otro_dia_no_cuenta(db, cfg):
    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    plan = _plan_guardado(db)
    ayer = _entrenamiento_completo(plan, wid="viejo", day=LUNES - timedelta(days=1))

    res = run_reconcile(db, cfg, LUNES, workouts=[ayer])
    assert not res.avanzado
    assert res.workouts_nuevos == 0


def test_los_entrenamientos_contados_quedan_registrados(db, cfg):
    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    run_reconcile(db, cfg, LUNES, workouts=[_entrenamiento_completo(_plan_guardado(db))])
    filas = db.scalars(select(WorkoutLog)).all()
    assert [f.hevy_workout_id for f in filas] == ["w1"]


# ---------------------------------------------------------------------------
# El bucle entero
# ---------------------------------------------------------------------------


CHECKIN_TRANQUILO = {
    "fatigue": 3, "mood": 7, "upper_discomfort": 1, "lower_discomfort": 1,
    "sleep_quality": 7, "training_desire": 8, "yesterday_rpe": 6,
}


def _seis_semanas(db, cfg, *, reconciliar: bool) -> Counter:
    """Seis semanas de días verdes. Devuelve qué tipos de progresión hubo."""
    from app.repository import upsert_checkin

    tipos: Counter = Counter()
    for i in range(42):
        d = LUNES + timedelta(days=i)
        # El check-in es imprescindible y no un adorno: sin `lower_discomfort`
        # el freno lumbar no se puede evaluar y la carga no sube, que es el
        # comportamiento correcto y no el que se quiere medir aquí.
        upsert_checkin(db, d, dict(CHECKIN_TRANQUILO), config=cfg)
        met = dias(d, 10, hrv=60.0, rhr=50.0, sleep_min=450, sleep_score=80)
        res = run_daily(
            db, cfg, d, metrics=met, rides=[],
            hevy_client=HevyFalso(), telegram_client=TelegramFalso(),
        )
        if res.decision.progression:
            for ch in res.decision.progression.changes:
                tipos[ch.kind] += 1
        if reconciliar:
            plan = _plan_guardado(db, d)
            if plan.get("exercises"):
                run_reconcile(
                    db, cfg, d,
                    workouts=[_entrenamiento_completo(plan, wid=f"w{i}", day=d)],
                )
    return tipos


def test_sin_reconciliar_la_carga_no_sube_nunca(db, cfg):
    """El fallo que motivó todo este módulo, convertido en test.

    Es el más peligroso del sistema porque no se parece a un fallo: la decisión
    sale, el mensaje se manda, todo está verde. Simplemente el peso es el mismo
    en la semana seis que en la uno, para siempre, porque nadie le cuenta nunca
    al motor que la sesión se completó.

    El volumen SÍ sube sin reconciliar -por eso este test mira `load` y no "que
    haya alguna progresión": la primera versión de la comprobación equivalente
    en `test_repository.py` pasaba por culpa de eso, midiendo volumen y creyendo
    que medía memoria-.
    """
    tipos = _seis_semanas(db, cfg, reconciliar=False)
    assert tipos.get("load", 0) == 0, (
        f"ha subido carga sin que nadie confirmara una sola sesión: {dict(tipos)}"
    )


def test_reconciliando_el_programa_progresa(db, cfg):
    """La contraparte: con el bucle cerrado, seis semanas limpias suben peso."""
    tipos = _seis_semanas(db, cfg, reconciliar=True)
    assert tipos.get("load", 0) > 0, (
        f"seis semanas completando todo y la carga no ha subido nunca: {dict(tipos)}"
    )
    assert tipos.get("volume", 0) > 0


# ---------------------------------------------------------------------------
# apply_execution: la mitad que mueve las rachas
# ---------------------------------------------------------------------------


EJS = [{"key": "hip_thrust"}, {"key": "remo"}]


def test_la_racha_se_rompe_entera_no_se_decrementa():
    """"Sesiones limpias CONSECUTIVAS": decrementar sería una media."""
    st = EngineState(clean_sessions={("dia_1", "hip_thrust"): 5})
    apply_execution(
        st, routine_key="dia_1", exercises=EJS,
        executed={"hip_thrust": False, "remo": True},
    )
    assert st.clean_sessions[("dia_1", "hip_thrust")] == 0
    assert st.clean_sessions[("dia_1", "remo")] == 1


def test_un_ejercicio_que_ha_subido_hoy_empieza_racha_de_cero():
    """Las sesiones limpias que pagaron la subida ya se han gastado en ella.

    Sin esto un ejercicio que sube por la mañana y se completa por la noche
    conservaría la racha entera y podría volver a subir al día siguiente: dos
    subidas seguidas sin las sesiones limpias que las justifican.
    """
    st = EngineState(clean_sessions={("dia_1", "hip_thrust"): 2})
    apply_execution(
        st, routine_key="dia_1", exercises=EJS,
        executed={"hip_thrust": True, "remo": True},
        progressed=["hip_thrust"],
    )
    assert st.clean_sessions[("dia_1", "hip_thrust")] == 0
    assert st.clean_sessions[("dia_1", "remo")] == 1


def test_la_racha_es_por_rutina_y_ejercicio():
    """La plancha del Día 1 y la del Día 3 no comparten mérito."""
    st = EngineState(clean_sessions={("dia_3", "hip_thrust"): 4})
    apply_execution(
        st, routine_key="dia_1", exercises=EJS, executed={"hip_thrust": True}
    )
    assert st.clean_sessions[("dia_1", "hip_thrust")] == 1
    assert st.clean_sessions[("dia_3", "hip_thrust")] == 4
