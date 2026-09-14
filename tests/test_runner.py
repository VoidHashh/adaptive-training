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
from app.models import Base, Decision, HevyWrite, Notification, WorkoutLog
from app.repository import load_state, save_decision, save_state
from app.runner import run_daily, run_reconcile
from tests.conftest import LUNES, dias, sig_completa

# Con la rotación ya no hay días sin fuerza: cualquier día, si vas, te toca la
# siguiente del ciclo. Lo que decide CUÁL es `EngineState.last_strength`, no el
# día de la semana, así que un test que necesite una rutina concreta pone el
# puntero en la anterior en vez de buscar el día del calendario que la traía.


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
    def __init__(self, *, revienta: bool = False, escribe: bool = True,
                 con_copia: bool = True):
        self.revienta = revienta
        self.escribe = escribe
        # Si hay copia del día con la que deshacer lo escrito esta mañana.
        # `False` es el caso feo: había que revertir y no se puede.
        self.con_copia = con_copia
        self.llamadas: list[tuple[str, dict]] = []
        self.reversiones: list[tuple[str, date]] = []
        # Lo que la app enseñaría ahora mismo. Sin esto los tests comprueban que
        # se llamó a la función correcta, que no es lo mismo que comprobar qué
        # queda en Hevy, y lo que queda en Hevy es lo único que importa aquí.
        self.contenido = "la rutina de la semana pasada"

    def write_routine(self, routine_id, payload, *, dry_run=False):
        self.llamadas.append((routine_id, payload))
        if self.revienta:
            raise RuntimeError("la API de Hevy ha devuelto 500")
        from app.integrations.hevy import WriteResult

        if self.escribe:
            self.contenido = (payload.get("routine") or {}).get("title") or "?"
        return WriteResult(
            written=self.escribe, routine_id=routine_id, reason="ok de mentira"
        )

    def revert_to_day_start(self, routine_id, dia):
        from app.integrations.hevy import HevyError, WriteResult

        if not self.con_copia:
            raise HevyError(
                f"no hay ninguna copia de la rutina {routine_id} tomada el "
                f"{dia:%Y-%m-%d}: no se puede deshacer lo escrito hoy"
            )
        self.reversiones.append((routine_id, dia))
        self.contenido = "la rutina de la semana pasada"
        return WriteResult(written=True, routine_id=routine_id,
                           reason="revertida al estado de 2026-09-07 08:59:00")


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


def test_sin_cliente_de_hevy_el_mensaje_no_describe_una_rutina_que_no_esta(db, cfg):
    """El caso simétrico al de arriba, que no se avisaba.

    Sin cliente el estado era "skipped", y el aviso de arriba del mensaje solo
    se pone cuando es "error". Resultado: el mensaje de las nueve describía con
    todo detalle -ejercicios, series, kilos- una sesión que en Hevy no estaba.
    Se abre la app, se ve la rutina de la semana pasada y se entrena esa,
    creyendo que es la de hoy porque el mensaje acaba de decirlo.

    `api._clientes` se traga la excepción del constructor en un WARNING, así
    que un HEVY_API_KEY mal escrito en el .env producía exactamente esto y
    ningún síntoma.
    """
    tg = TelegramFalso()
    res = corre(db, cfg, hevy=None, tg=tg)

    assert res.hevy_status == "error"
    assert tg.enviados, "sin Hevy el mensaje tiene que salir igual"
    assert "NO se ha escrito en Hevy" in tg.enviados[0]
    assert any("HEVY_API_KEY" in p for p in res.problemas), (
        "hay que decir por dónde empezar a mirar"
    )


def test_sin_cliente_de_hevy_el_intento_queda_registrado(db, cfg):
    """Que no haya cliente no exime de dejar la fila.

    En el histórico, un día sin fila de HevyWrite y un día que no tocaba Hevy
    son indistinguibles.
    """
    corre(db, cfg, hevy=None, tg=TelegramFalso())
    fila = db.scalars(select(HevyWrite)).first()
    assert fila is not None and fila.status == "error"


def test_sin_cliente_de_telegram_se_avisa_de_que_nadie_se_ha_enterado(db, cfg):
    """Un sistema que decide y no lo cuenta ha fallado, aunque no dé error."""
    res = corre(db, cfg, hevy=HevyFalso(), tg=None)
    assert res.telegram_status == "skipped"
    assert any("no se ha contado a nadie" in p for p in res.problemas)


# --- el modo de solo lectura no puede ser mudo -----------------------------
#
# El tercer caso de la misma familia, y el único que quedaba sin tapar. Los dos
# de arriba -Hevy revienta, no hay cliente- ya avisaban. Este no, porque
# `write_enabled: false` es deliberado y se clasificaba como salto legítimo.
#
# Lo deliberado es no escribir. Lo que no puede ser deliberado es mandar un
# mensaje que describe la rutina como si estuviera puesta. El usuario abre Hevy,
# ve la de la semana pasada y entrena esa.
#
# Estos dos tests usan el HevyClient DE VERDAD y no el doble, porque lo que se
# está fijando es la costura entre los dos ficheros: `write_routine` devuelve
# `written=False` con `error=None` -no es una avería- y el runner tiene que
# distinguir eso de un dry_run. Con `write_enabled` en false se devuelve antes de
# tocar la red, así que el fixture `sin_red` no estorba.


def cliente_real(tmp_path, *, write_enabled: bool):
    from app.integrations.hevy import HevyClient

    return HevyClient(
        api_key="no-se-usa",
        data_root=tmp_path,
        write_enabled=write_enabled,
    )


def test_el_modo_solo_lectura_no_puede_ser_mudo(db, cfg, tmp_path):
    tg = TelegramFalso()
    res = corre(db, cfg, hevy=cliente_real(tmp_path, write_enabled=False), tg=tg)

    assert res.hevy_status == "read_only", (
        f"con el interruptor cerrado y DRY_RUN apagado la rutina no está en "
        f"Hevy, y el estado tiene que decirlo: {res.hevy_status}"
    )
    assert tg.enviados
    assert "NO se ha escrito en Hevy" in tg.enviados[0]
    assert "write_enabled" in tg.enviados[0], (
        "el aviso tiene que nombrar el interruptor, que es lo que hay que tocar"
    )
    assert any("write_enabled" in p for p in res.problemas)


def test_el_ensayo_no_dispara_el_aviso(db, cfg, tmp_path):
    """El otro lado del par: en dry_run tampoco se escribe, y ahí está bien.

    Sin este test, "avisa siempre que no se escriba" pasaría igual, y el ensayo
    -que es el modo en el que se está probando el sistema ahora mismo- llenaría
    todos los mensajes de una alarma que no significa nada. Un aviso que sale
    siempre deja de leerse, y el día que salga de verdad tampoco se leerá.

    Aquí sí va el doble: el cliente de verdad, en dry_run con el interruptor
    abierto, se baja la rutina remota para poder hacer la copia antes de no
    enviar el PUT, y eso es red. El doble devuelve el mismo WriteResult que
    importa -`written=False`, `error=None`- que es justo el del caso de arriba:
    lo único que cambia entre los dos tests es el `dry_run`.
    """
    tg = TelegramFalso()
    res = corre(db, cfg, hevy=HevyFalso(escribe=False), tg=tg, dry_run=True)

    assert res.hevy_status == "dry_run"
    assert "NO se ha escrito en Hevy" not in tg.enviados[0]
    assert not any("Hevy" in p for p in res.problemas)


def test_el_modo_solo_lectura_queda_registrado(db, cfg, tmp_path):
    """Y con su propio estado, no confundido con un salto.

    En el histórico, "hoy no tocaba Hevy" y "hoy tocaba pero el interruptor
    estaba cerrado" son dos cosas distintas: la primera es el calendario, la
    segunda es una rutina desactualizada durante los días que durase.
    """
    corre(db, cfg, hevy=cliente_real(tmp_path, write_enabled=False),
          tg=TelegramFalso())
    fila = db.scalars(select(HevyWrite)).first()
    assert fila is not None and fila.status == "read_only"
    assert fila.error is None, "no es una avería y no se inventa una"


# --- el aviso nombra su causa en vez de adivinarla -------------------------
#
# Que no haya cliente ya se avisa. Lo que se perdía era POR QUÉ: `api._clientes`
# dejaba la excepción del constructor en un `log.warning` y el aviso tenía que
# suponer la causa más probable. Un aviso que nombra su causa se arregla desde
# el móvil; uno que la adivina obliga a entrar por SSH a leer un log.


def test_el_motivo_real_de_no_haber_cliente_de_hevy_llega_al_mensaje(db, cfg):
    tg = TelegramFalso()
    res = corre(
        db, cfg, hevy=None, tg=tg,
        client_errors={"hevy": "la rutina 'empuje' no está en routine_ids"},
    )
    assert "routine_ids" in res.hevy_reason
    assert "routine_ids" in tg.enviados[0], (
        "el motivo tiene que verse donde se lee, no solo en el objeto"
    )


def test_sin_motivo_se_mantiene_la_sospecha_mas_probable(db, cfg):
    """Un `client_errors` que no llega no puede dejar el aviso mudo.

    Es el caso de cualquier llamada que no venga de `api._clientes`: sigue
    habiendo que decir por dónde empezar a mirar.
    """
    res = corre(db, cfg, hevy=None, tg=TelegramFalso())
    assert "HEVY_API_KEY" in res.hevy_reason


def test_el_motivo_real_de_no_haber_telegram_queda_en_los_problemas(db, cfg):
    res = corre(
        db, cfg, hevy=HevyFalso(), tg=None,
        client_errors={"telegram": "falta TELEGRAM_CHAT_ID"},
    )
    assert res.telegram_reason == "falta TELEGRAM_CHAT_ID"
    assert any("TELEGRAM_CHAT_ID" in p for p in res.problemas)


def test_el_motivo_de_un_cliente_no_se_le_atribuye_al_otro(db, cfg):
    """Dos fallos distintos con el mismo texto serían peor que ninguno."""
    res = corre(
        db, cfg, hevy=None, tg=None,
        client_errors={"hevy": "API key de Hevy inválida"},
    )
    assert "API key de Hevy" in res.hevy_reason
    assert "API key de Hevy" not in (res.telegram_reason or "")


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
# Lo que se leyó de Garmin, archivado
# ---------------------------------------------------------------------------
#
# Estas tablas existían vacías desde el primer día: `models.py` las declaraba y
# no las escribía nadie. No daba ningún error -el sistema decidía igual de bien-
# y por eso hacen falta estos tests: el fallo que impiden no se nota mirando el
# sistema funcionar, solo semanas después, cuando no hay contra qué analizar.


def test_la_mañana_archiva_lo_que_leyo_de_garmin(db, cfg):
    from app.models import DailyMetrics

    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())

    filas = db.scalars(select(DailyMetrics)).all()
    assert filas, (
        "el sistema ha decidido con las métricas de Garmin y no ha guardado "
        "ninguna: mañana no habrá contra qué correlacionar el check-in"
    )
    hoy = next((f for f in filas if f.date == LUNES), None)
    assert hoy is not None and hoy.hrv == 60.0 and hoy.rhr == 50.0


def test_las_salidas_se_archivan_con_su_clasificacion(db, cfg):
    """La etiqueta, no solo los números.

    `suave/media/intensa` depende de los umbrales del `config.yaml` del día en
    que se clasificó. Guardar solo la carga y reclasificar dentro de seis semanas
    con un YAML ya retocado daría otras etiquetas, y la pregunta "¿cuántos días
    de HRV cuesta una salida intensa?" se respondería sobre unas intensas que en
    su momento no lo fueron.
    """
    from app.models import Activity
    from tests.conftest import ride

    salida = ride(LUNES, load=180.0, zones=(600, 900, 1200, 600, 300), activity_id=77)
    run_daily(
        db, cfg, LUNES,
        metrics=metricas(), rides=[salida],
        hevy_client=HevyFalso(), telegram_client=TelegramFalso(),
    )

    fila = db.scalars(
        select(Activity).where(Activity.garmin_activity_id == 77)
    ).first()
    assert fila is not None, "la salida se ha clasificado, se ha usado y se ha tirado"
    assert fila.intensity_level, "sin etiqueta la fila no sirve para la vista 3"
    assert fila.classification_source, "y sin saber de dónde salió, tampoco"
    assert fila.date == LUNES
    assert fila.hr_zone_3_s == 1200.0


def test_archivar_dos_veces_el_mismo_dia_no_duplica(db, cfg):
    """La ventana se relee cada mañana: siete días archivados siete veces."""
    from app.models import Activity, DailyMetrics
    from tests.conftest import ride

    salida = ride(LUNES, load=180.0, zones=(600, 900, 1200, 600, 300), activity_id=77)
    for _ in range(3):
        run_daily(
            db, cfg, LUNES,
            metrics=metricas(), rides=[salida],
            hevy_client=HevyFalso(), telegram_client=TelegramFalso(),
        )

    assert len(db.scalars(select(DailyMetrics)).all()) == 10
    assert len(db.scalars(select(Activity)).all()) == 1


def test_un_hueco_de_hoy_no_borra_el_dato_de_ayer(db, cfg):
    """El fallo silencioso que esta función tiene prohibido cometer.

    Garmin falla a ratos. Si la relectura de mañana trae `hrv=None` para un día
    que ayer sí tenía dato, copiarlo encima borraría el dato bueno sin un solo
    error: quedaría una fila con un hueco, idéntica a la de un día en que el
    reloj se quedó en la mesilla.
    """
    from app.models import DailyMetrics

    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    assert db.scalars(
        select(DailyMetrics).where(DailyMetrics.date == LUNES)
    ).first().hrv == 60.0

    # Segunda pasada, esta vez Garmin no contesta lo del HRV.
    mudas = dias(LUNES, 10, hrv=None, rhr=51.0, sleep_min=450, sleep_score=80)
    run_daily(
        db, cfg, LUNES,
        metrics=mudas, rides=[],
        hevy_client=HevyFalso(), telegram_client=TelegramFalso(),
    )

    fila = db.scalars(
        select(DailyMetrics).where(DailyMetrics.date == LUNES)
    ).first()
    assert fila.hrv == 60.0, "un None de hoy ha borrado el dato bueno de ayer"
    assert fila.rhr == 51.0, "y el dato que SÍ venía tiene que actualizarse"


def test_si_archivar_falla_la_mañana_termina_y_se_dice(db, cfg, monkeypatch):
    """Perder un día de histórico es malo; quedarse sin plan por eso, peor.

    Pero tampoco puede pasar callando: el aviso viaja en `problemas`, que es lo
    que el usuario acaba viendo, no un log que nadie abre.
    """
    def revienta(*a, **k):
        raise RuntimeError("la tabla no existe")

    monkeypatch.setattr("app.repository.upsert_daily_metrics", revienta)

    tg = TelegramFalso()
    res = corre(db, cfg, hevy=HevyFalso(), tg=tg)

    assert res.telegram_status == "sent", "la mañana se ha caído por no poder archivar"
    assert any("archivar" in p for p in res.problemas), (
        f"ha fallado el archivo y no se dice: {res.problemas}"
    )


# ---------------------------------------------------------------------------
# La noche
# ---------------------------------------------------------------------------


def _entrenamiento_completo(
    plan: dict, wid: str = "w1", day: date = LUNES, factor_peso: float = 1.0
) -> dict:
    """Un entrenamiento que cumple el plan entero, construido DESDE el plan.

    `weight_kg` viaja, y no es un detalle de fidelidad: el cumplimiento compara
    el peso, así que un entrenamiento de mentira sin pesos no es "el plan hecho
    entero", es el plan hecho a cero kilos. Cuando esto no lo copiaba, seis
    semanas de sesiones perfectas no subían ni un kilo y el test lo cantaba.

    `factor_peso` sirve para el caso contrario: 0.8 son las mismas reps con menos
    peso, que es exactamente la sesión que antes se colaba como limpia.
    """
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
                        "weight_kg": (
                            None
                            if s.get("weight_kg") is None
                            else round(float(s["weight_kg"]) * factor_peso, 2)
                        ),
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
    """Sin plan no hay contra qué comparar, y no se inventa uno.

    Pero tampoco se tira el entrenamiento. Esto antes salía por una puerta
    temprana que devolvía sin escribir una fila, así que entrenar un día del que
    el sistema no tenía decisión guardada equivalía a no haber entrenado: ni en
    las métricas, ni en el volumen, ni en el presupuesto de intensas. Queda
    registrado y marcado como fuera del plan, con el motivo escrito.
    """
    w = _entrenamiento_completo({"exercises": []}, wid="sin_plan")
    res = run_reconcile(db, cfg, LUNES, workouts=[w])

    assert not res.avanzado, "sin plan no hay nada que progresar"
    assert [s["hevy_workout_id"] for s in res.sueltos] == ["sin_plan"]

    fila = db.scalars(select(WorkoutLog)).one()
    assert fila.unplanned is True
    assert fila.all_sets_at_target is None, "no había plan contra el que juzgarlo"
    assert "no había decisión guardada" in fila.motivo_suelto


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
# Nada de lo que se hace en Hevy se pierde
# ---------------------------------------------------------------------------
#
# El principio es del usuario y es literal: "si registro un entreno, el sistema
# tiene que verlo, aunque no progrese cargas". Lo que se vigila aquí es que
# REGISTRAR y RECONCILIAR sean dos cosas distintas. Estaban pegadas: si no había
# nada contra lo que comparar -sin decisión guardada, día sin fuerza, rutina
# desconocida- la función salía por una puerta temprana sin escribir una sola
# fila, y el entrenamiento desaparecía de las métricas, del volumen y del
# presupuesto de sesiones intensas.


@pytest.fixture
def cfg_lunes(cfg_copia):
    """El config real con el programa empezando el lunes de los tests.

    `hiit_applies` cuenta semanas desde `program.start`; con la fecha real del
    YAML -el 2026-09-14- el lunes de los tests cae antes del arranque y el
    bloque no entra nunca. Esto no enciende el HIIT: ya está encendido.
    """
    cfg_copia.raw["program"]["start"] = LUNES
    return cfg_copia


def _rid(cfg, rkey: str) -> str:
    return cfg.raw["routines"][rkey]["hevy_routine_id"]


def _parte(plan: dict, claves: set[str], *, dentro: bool, wid: str, rid: str) -> dict:
    """Media sesión: solo los ejercicios de `claves`, o solo los demás."""
    w = _entrenamiento_completo(
        {"exercises": [e for e in plan["exercises"] if (e["key"] in claves) is dentro]},
        wid=wid,
    )
    w["routine_id"] = rid
    return w


def test_el_hiit_previsto_para_hoy_no_sale_como_fuera_del_plan(db, cfg_lunes):
    """El motor añade el HIIT a la rutina de fuerza, pero en Hevy los bloques
    existen sueltos y se ejecutan como un entrenamiento propio: así están los del
    8 y el 9 de septiembre en la cuenta. Sin reconocerlos, hacer exactamente lo
    que el plan pedía saldría cada noche como "visto fuera del plan", que es la
    clase de aviso que enseña a no leer los avisos.
    """
    corre(db, cfg_lunes, hevy=HevyFalso(), tg=TelegramFalso())
    plan = _plan_guardado(db)
    assert plan["hiit_block"] == "hiit_dia_1", (
        f"el escenario ya no lleva HIIT; el test hay que rehacerlo: {plan.get('hiit_block')}"
    )

    bloque = {e["key"] for e in cfg_lunes.raw["routines"]["hiit_dia_1"]["exercises"]}
    fuerza = _parte(plan, bloque, dentro=False, wid="fuerza", rid=_rid(cfg_lunes, "dia_1"))
    hiit = _parte(plan, bloque, dentro=True, wid="hiit", rid=_rid(cfg_lunes, "hiit_dia_1"))

    res = run_reconcile(db, cfg_lunes, LUNES, workouts=[fuerza, hiit])

    assert res.sueltos == [], f"lo que el plan pedía sale como fuera del plan: {res.sueltos}"
    filas = {f.hevy_workout_id: f for f in db.scalars(select(WorkoutLog)).all()}
    assert set(filas) == {"fuerza", "hiit"}
    assert filas["hiit"].routine_key == "hiit_dia_1"
    assert filas["hiit"].unplanned is False
    assert filas["hiit"].all_sets_at_target is None, (
        "el veredicto del día es el de la fuerza; el HIIT no se juzga y no lo hereda"
    )
    assert filas["fuerza"].all_sets_at_target is True


def test_un_hiit_que_el_plan_no_pedia_queda_visible_con_su_motivo(db, cfg):
    """"Si un día hago algo que el sistema no esperaba, quiero saberlo, no que
    desaparezca". Y el motivo va escrito en la fila porque lo lee el mensaje de
    la mañana: avisar de que pasó algo sin decir el qué obliga a abrir la base
    de datos, y a las nueve desde el móvil eso es no avisar.
    """
    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    assert _plan_guardado(db).get("hiit_block") is None

    w = _entrenamiento_completo({"exercises": []}, wid="hiit_suelto")
    w["routine_id"] = _rid(cfg, "hiit_dia_1")

    res = run_reconcile(db, cfg, LUNES, workouts=[w])

    assert [s["routine"] for s in res.sueltos] == ["hiit_dia_1"]
    fila = db.scalars(select(WorkoutLog)).one()
    assert fila.unplanned is True
    assert "HIIT por libre" in fila.motivo_suelto, fila.motivo_suelto


def test_un_entrenamiento_de_un_dia_que_no_planificaba_fuerza_se_registra_igual(db, cfg):
    """Antes esto devolvía sin escribir nada y el entrenamiento no había
    existido: ni volumen, ni series, ni presupuesto de intensas.

    El escenario ha cambiado de forma con la rotación. Antes era un domingo:
    el calendario no ponía fuerza ese día y punto. Ya no hay días sin fuerza
    -si voy, me toca la siguiente del ciclo, sea domingo o jueves-, así que el
    único plan que no es fuerza es el bloque de recuperación de un día rojo. Y
    ese caso es más interesante que el domingo, porque es el que pasa de
    verdad: el sistema dice "hoy toca cuidarse" y yo voy al gimnasio igual.
    Que quede registrado no es opcional; el registro no opina.
    """
    from app.repository import upsert_checkin

    upsert_checkin(
        db, LUNES, dict(CHECKIN_TRANQUILO, lower_discomfort=8), config=cfg
    )
    corre(db, cfg, day=LUNES, hevy=HevyFalso(), tg=TelegramFalso())
    plan = _plan_guardado(db, LUNES)
    assert plan.get("kind") == "recovery", (
        f"el escenario necesita un día rojo y salió {plan.get('kind')}"
    )

    w = _entrenamiento_completo({"exercises": []}, wid="rojo", day=LUNES)
    res = run_reconcile(db, cfg, LUNES, workouts=[w])

    assert not res.avanzado
    assert res.workouts_nuevos == 1
    fila = db.scalars(select(WorkoutLog)).one()
    assert fila.unplanned is True
    assert fila.date == LUNES


def test_lo_registrado_lleva_duracion_series_y_volumen(db, cfg):
    """Las tres columnas llevaban desde el principio declaradas y nadie las
    llenaba. `docs/analisis.md` daba por hecho que la vista de volumen salía de
    aquí, y salía de NULL."""
    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    plan = _plan_guardado(db)
    w = _entrenamiento_completo(plan)
    w["start_time"] = f"{LUNES.isoformat()}T18:00:00Z"
    w["end_time"] = f"{LUNES.isoformat()}T19:00:00Z"

    run_reconcile(db, cfg, LUNES, workouts=[w])

    fila = db.scalars(select(WorkoutLog)).one()
    assert fila.total_sets and fila.total_sets > 0
    assert fila.total_volume_kg and fila.total_volume_kg > 0
    assert fila.raw_json, "sin el crudo no se puede arreglar nada hacia atrás"


def test_el_hiit_del_plan_no_puede_cerrar_la_puerta_de_la_fuerza(db, cfg_lunes):
    """LA comprobación que justifica encender el HIIT, y la única cuyo fallo se
    paga en una espalda con hernia.

    El bloque HIIT se AÑADE a los ejercicios de la rutina, así que `executed`
    pasa a llevar claves de HIIT y el veredicto del día se vuelve False en cuanto
    falte una de ellas. Si esas claves contaran para la progresión de fuerza,
    saltarse el HIIT congelaría la carga de la sesión de fuerza para siempre, o
    -peor, si el signo estuviera al revés- haría subir con una sesión a medias.
    Aquí se hace la fuerza ENTERA y nada del HIIT, y la racha de fuerza tiene que
    avanzar exactamente igual.
    """
    corre(db, cfg_lunes, hevy=HevyFalso(), tg=TelegramFalso())
    plan = _plan_guardado(db)
    assert plan["hiit_block"] == "hiit_dia_1", "el escenario ya no lleva HIIT"

    bloque = {e["key"] for e in cfg_lunes.raw["routines"]["hiit_dia_1"]["exercises"]}
    solo_fuerza = _parte(
        plan, bloque, dentro=False, wid="fuerza", rid=_rid(cfg_lunes, "dia_1")
    )

    res = run_reconcile(db, cfg_lunes, LUNES, workouts=[solo_fuerza])
    assert res.avanzado

    estado = load_state(db, program_start=cfg_lunes.program_start)
    base = [e["key"] for e in cfg_lunes.raw["routines"]["dia_1"]["exercises"]]
    assert all(estado.clean_sessions.get(("dia_1", k), 0) == 1 for k in base), (
        "saltarse el HIIT ha roto la racha de la fuerza: "
        f"{[(k, estado.clean_sessions.get(('dia_1', k), 0)) for k in base]}"
    )


def test_un_hiit_registrado_el_martes_se_cuenta_el_sabado(db, cfg):
    """El agujero que el usuario diagnosticó: "si el HIIT no suma, ese
    presupuesto va corto y me está dejando margen que no tengo".

    ERA UN PARÁMETRO MUERTO. `build_signals` acepta `sessions=` desde el primer
    día y nadie se lo pasaba nunca -ni `run_daily` ni `cli.py`-; el único sitio
    del proyecto donde se construía un `StrengthSession` era `test_signals.py`.
    Así que `counts_as_intense.hiit_executed: true` llevaba toda la vida puesto
    y sin efecto, y el recuento semanal de intensas solo contaba salidas de bici.

    Cuando esto era un presupuesto, un numerador incompleto era peor que no
    tener límite: parecía que alguien lo estaba vigilando. Ahora que solo cuenta,
    el precio es otro y sigue importando: el número que el usuario lee cada
    mañana tiene que ser el número de verdad, o no sirve para nada. Un dato que
    no decide nada es justamente el que nadie va a ir a verificar.

    El test va por `run_daily` a propósito. El fallo no estaba en el cálculo
    -`intensity_count` siempre supo contar HIIT- sino en el cable, y un test que
    llamara a `intensity_count` directamente habría pasado desde el principio
    sin enterarse de nada.
    """
    martes = LUNES + timedelta(days=1)
    db.add(WorkoutLog(hevy_workout_id="h", date=martes, routine_key="hiit_dia_1"))
    db.flush()

    sabado = LUNES + timedelta(days=5)
    res = corre(db, cfg, day=sabado, hevy=HevyFalso(), tg=TelegramFalso())

    conteo = res.decision.signals.intense_count
    assert conteo is not None
    assert conteo.used == 1, f"el HIIT del martes no se ha contado: {conteo.detail}"
    assert any("HIIT" in d for d in conteo.detail), conteo.detail


def test_la_fuerza_registrada_no_cuenta_como_sesion_intensa(db, cfg):
    """`counts_as_intense.strength_session` está en false y tiene que seguir
    mandando ahora que las sesiones sí llegan. Contar la fuerza como intensa
    haría que el número subiera cada semana solo por entrenar el programa, y un
    recuento que sube siempre igual deja de informar de nada."""
    martes = LUNES + timedelta(days=1)
    db.add(WorkoutLog(hevy_workout_id="f", date=martes, routine_key="dia_1"))
    db.flush()

    res = corre(db, cfg, day=LUNES + timedelta(days=5), hevy=HevyFalso(), tg=TelegramFalso())
    conteo = res.decision.signals.intense_count
    assert conteo.used == 0, conteo.detail


# ---------------------------------------------------------------------------
# La carga ejecutada, de punta a punta
# ---------------------------------------------------------------------------
#
# Aquí no se prueba la lógica de adopción -eso es `test_adoption.py`- sino el
# CABLEADO: que el peso leído de Hevy llega al motor, que el motor mueve el
# objetivo, que el objetivo sobrevive a la base de datos y que el mensaje de la
# mañana siguiente lo cuenta. Cualquiera de los cuatro tramos puede estar
# desconectado sin que nada dé error, y ese es justo el fallo de esta casa.


def _primer_ejercicio_con_peso(plan: dict) -> dict:
    for ex in plan.get("exercises") or []:
        if any((s.get("weight_kg") or 0) > 0 for s in ex.get("sets") or []):
            return ex
    raise AssertionError("el plan del lunes no tiene ningún ejercicio con peso")


def test_las_mismas_reps_con_menos_peso_no_avanzan_la_racha(db, cfg):
    """LA regresión del punto 13, de punta a punta.

    Mientras el peso quedó fuera del cumplimiento, una sesión al 80% de la carga
    contaba como limpia y pagaba la subida siguiente: el plan se iba subiendo
    mientras la realidad bajaba, sin un solo error y en una espalda con hernia.
    """
    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    plan = _plan_guardado(db)
    flojo = _entrenamiento_completo(plan, factor_peso=0.8)

    run_reconcile(db, cfg, LUNES, workouts=[flojo])

    estado = load_state(db, program_start=cfg.program_start)
    key = _primer_ejercicio_con_peso(plan)["key"]
    assert estado.clean_sessions.get(("dia_1", key), 0) == 0, (
        "una sesión al 80% de la carga ha contado como limpia"
    )


def test_el_peso_ejecutado_llega_al_resultado_de_la_reconciliacion(db, cfg):
    """`cli.py` los enseña: una adopción que solo se ve en el mensaje de mañana
    no se puede comprobar hoy, que es cuando se está ensayando a mano."""
    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    plan = _plan_guardado(db)
    ex = _primer_ejercicio_con_peso(plan)
    esperado = max(float(s.get("weight_kg") or 0) for s in ex["sets"])

    res = run_reconcile(db, cfg, LUNES, workouts=[_entrenamiento_completo(plan)])
    assert res.pesos.get(ex["key"]) == esperado


def test_subir_el_peso_a_mano_en_hevy_mueve_el_objetivo_guardado(db, cfg):
    """El caso del usuario: la máquina no tiene ese disco, o 60 salió fácil.

    Sin esto, al día siguiente el motor volvería a planificar desde SU número y
    anunciaría "60→62,5" a alguien que ya está en 65.
    """
    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    plan = _plan_guardado(db)
    ex = _primer_ejercicio_con_peso(plan)
    antes = max(float(s.get("weight_kg") or 0) for s in ex["sets"])

    w = _entrenamiento_completo(plan)
    # Solo ese ejercicio, y con +2,5 kg en todas sus series: dentro del tope.
    for e in w["exercises"]:
        if e["exercise_template_id"] == ex.get("template_id"):
            for s in e["sets"]:
                if s.get("weight_kg"):
                    s["weight_kg"] = round(s["weight_kg"] + 2.5, 2)

    res = run_reconcile(db, cfg, LUNES, workouts=[w])

    assert any(a["applied"] and a["key"] == ex["key"] for a in res.adopciones), (
        f"no se ha adoptado nada para {ex['key']}: {res.adopciones}"
    )
    estado = load_state(db, program_start=cfg.program_start)
    despues = max(
        float(s.get("weight_kg") or 0) for s in estado.current_sets[("dia_1", ex["key"])]
    )
    assert despues == antes + 2.5, "el objetivo guardado no se ha movido"


def test_la_adopcion_de_anoche_se_cuenta_en_el_mensaje_de_la_manana(db, cfg):
    """El último tramo del cable. Sin él, el usuario ve un peso distinto del que
    el mensaje de ayer prometía y no puede saber si es el sistema funcionando o
    el sistema roto."""
    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    plan = _plan_guardado(db)
    ex = _primer_ejercicio_con_peso(plan)

    w = _entrenamiento_completo(plan)
    for e in w["exercises"]:
        if e["exercise_template_id"] == ex.get("template_id"):
            for s in e["sets"]:
                if s.get("weight_kg"):
                    s["weight_kg"] = round(s["weight_kg"] + 2.5, 2)
    run_reconcile(db, cfg, LUNES, workouts=[w])

    tg = TelegramFalso()
    corre(db, cfg, day=LUNES + timedelta(days=1), hevy=HevyFalso(), tg=tg)

    assert tg.enviados, "no se ha mandado mensaje"
    texto = tg.enviados[-1]
    assert "Ajustado a lo que levantaste" in texto, texto
    assert ex["name"] in texto


def test_si_telegram_falla_la_adopcion_se_cuenta_al_dia_siguiente(db, cfg):
    """Por esto leer las adopciones y sellarlas son dos pasos.

    `_mandar_telegram` se traga los fallos de envío a propósito, para que un
    Telegram caído no tumbe la mañana entera. La transacción se confirma igual.
    Si el sellado no mirase el resultado del envío, la adopción quedaría dada por
    explicada por un mensaje que nunca llegó al móvil, y el usuario se
    encontraría el peso cambiado sin un solo aviso, para siempre.
    """
    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    plan = _plan_guardado(db)
    ex = _primer_ejercicio_con_peso(plan)

    w = _entrenamiento_completo(plan)
    for e in w["exercises"]:
        if e["exercise_template_id"] == ex.get("template_id"):
            for s in e["sets"]:
                if s.get("weight_kg"):
                    s["weight_kg"] = round(s["weight_kg"] + 2.5, 2)
    run_reconcile(db, cfg, LUNES, workouts=[w])

    # Martes: el mensaje se compone, lleva la adopción... y el envío revienta.
    roto = TelegramFalso(revienta=True)
    r1 = corre(db, cfg, day=LUNES + timedelta(days=1), hevy=HevyFalso(), tg=roto)
    assert r1.telegram_status not in {"sent", "dry_run"}, r1.telegram_status

    # Miércoles: sigue sin explicarse, así que vuelve a salir.
    tg = TelegramFalso()
    corre(db, cfg, day=LUNES + timedelta(days=2), hevy=HevyFalso(), tg=tg)
    assert "Ajustado a lo que levantaste" in tg.enviados[-1], (
        "la adopción se selló con un mensaje que nunca llegó: el cambio de carga "
        "se queda sin explicar para siempre"
    )


def test_una_adopcion_contada_no_se_repite_al_dia_siguiente(db, cfg):
    """Repetir "ajustado a 65 kg" tres mañanas seguidas es la forma de que se
    deje de leer el bloque el día que diga algo nuevo."""
    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    plan = _plan_guardado(db)
    ex = _primer_ejercicio_con_peso(plan)

    w = _entrenamiento_completo(plan)
    for e in w["exercises"]:
        if e["exercise_template_id"] == ex.get("template_id"):
            for s in e["sets"]:
                if s.get("weight_kg"):
                    s["weight_kg"] = round(s["weight_kg"] + 2.5, 2)
    run_reconcile(db, cfg, LUNES, workouts=[w])

    tg1 = TelegramFalso()
    corre(db, cfg, day=LUNES + timedelta(days=1), hevy=HevyFalso(), tg=tg1)
    assert "Ajustado a lo que levantaste" in tg1.enviados[-1]

    tg2 = TelegramFalso()
    corre(db, cfg, day=LUNES + timedelta(days=2), hevy=HevyFalso(), tg=tg2)
    assert "Ajustado a lo que levantaste" not in tg2.enviados[-1]


def test_el_entreno_fuera_del_plan_se_cuenta_en_el_mensaje_de_la_manana(db, cfg):
    """"Si un día hago algo que el sistema no esperaba, quiero saberlo."

    Registrarlo en la base de datos no es enterarse: enterarse es que lo diga el
    mensaje. Y con el motivo, porque un aviso que obliga a abrir la base de datos
    para entenderlo, a las nueve de la mañana y desde el móvil, es no avisar.
    """
    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    w = _entrenamiento_completo({"exercises": []}, wid="hiit_suelto")
    w["routine_id"] = _rid(cfg, "hiit_dia_1")
    w["title"] = "HIIT Día 1"
    run_reconcile(db, cfg, LUNES, workouts=[w])

    tg = TelegramFalso()
    corre(db, cfg, day=LUNES + timedelta(days=1), hevy=HevyFalso(), tg=tg)

    texto = tg.enviados[-1]
    assert "Visto en Hevy, fuera del plan" in texto, texto
    assert "HIIT Día 1" in texto
    assert "HIIT por libre" in texto, "se avisa de que pasó algo sin decir el qué"


def test_si_telegram_falla_el_entreno_suelto_se_cuenta_al_dia_siguiente(db, cfg):
    """Mismo motivo que con las adopciones, y por eso leer y sellar van
    separados: `_mandar_telegram` se traga los fallos de envío para que un
    Telegram caído no tumbe la mañana, así que sellar al leer daría por contado
    un entrenamiento que nadie llegó a ver nunca."""
    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    w = _entrenamiento_completo({"exercises": []}, wid="suelto")
    w["routine_id"] = _rid(cfg, "hiit_dia_1")
    run_reconcile(db, cfg, LUNES, workouts=[w])

    roto = TelegramFalso(revienta=True)
    r1 = corre(db, cfg, day=LUNES + timedelta(days=1), hevy=HevyFalso(), tg=roto)
    assert r1.telegram_status not in {"sent", "dry_run"}, r1.telegram_status

    tg = TelegramFalso()
    corre(db, cfg, day=LUNES + timedelta(days=2), hevy=HevyFalso(), tg=tg)
    assert "Visto en Hevy, fuera del plan" in tg.enviados[-1], (
        "se selló con un mensaje que nunca llegó: el entrenamiento se queda sin contar"
    )


def test_un_entreno_suelto_ya_contado_no_se_repite_cada_manana(db, cfg):
    """Una línea que sale todos los días se aprende a saltar, y con ella se
    saltan las que sí cambian."""
    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    w = _entrenamiento_completo({"exercises": []}, wid="suelto")
    w["routine_id"] = _rid(cfg, "hiit_dia_1")
    run_reconcile(db, cfg, LUNES, workouts=[w])

    tg1 = TelegramFalso()
    corre(db, cfg, day=LUNES + timedelta(days=1), hevy=HevyFalso(), tg=tg1)
    assert "Visto en Hevy, fuera del plan" in tg1.enviados[-1]

    tg2 = TelegramFalso()
    corre(db, cfg, day=LUNES + timedelta(days=2), hevy=HevyFalso(), tg=tg2)
    assert "Visto en Hevy, fuera del plan" not in tg2.enviados[-1]


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
        progressed=(),
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
        st, routine_key="dia_1", exercises=EJS, executed={"hip_thrust": True},
        progressed=(),
    )
    assert st.clean_sessions[("dia_1", "hip_thrust")] == 1
    assert st.clean_sessions[("dia_3", "hip_thrust")] == 4


# ---------------------------------------------------------------------------
# La capa de tendencia
# ---------------------------------------------------------------------------


def test_la_mañana_calcula_la_tendencia(db, cfg):
    res = corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    assert res.decision.tendencia is not None
    assert res.decision.tendencia.day == LUNES


def test_la_tendencia_incluye_la_decision_de_hoy(db, cfg):
    """Hoy todavía no está escrita en `decisions` cuando se calcula.

    A las 06:30 la fila del día no existe aún, y en el recálculo de las 09:40 la
    que existe es la anterior. Si la capa leyera solo de la base, la racha
    siempre iría un día por detrás y el día que la racha llega a cinco el
    mensaje diría cuatro.
    """
    res = corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    assert res.decision.tendencia.n == 1


def test_la_tendencia_lee_el_historico_guardado(db, cfg):
    """Diez días previos en `decisions` tienen que llegar a la capa."""
    for i in range(10, 0, -1):
        db.add(Decision(date=LUNES - timedelta(days=i), light="amber",
                        trigger_rule="sueno_corto"))
    db.commit()
    res = corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    assert res.decision.tendencia.n == 11


def test_una_racha_larga_llega_al_mensaje(db, cfg):
    """La prueba de punta a punta: base de datos → capa → Telegram.

    Hoy tiene que salir no verde también: la racha se cuenta hacia atrás desde
    hoy, así que un verde hoy la corta por definición. Se fuerza con 5 h 40 de
    sueño, que es lo que dispara `sueno_corto`.
    """
    for i in range(45, 0, -1):
        luz = "amber" if i <= 8 else "green"
        db.add(Decision(date=LUNES - timedelta(days=i), light=luz,
                        trigger_rule="sueno_corto" if luz == "amber" else None))
    db.commit()
    tg = TelegramFalso()
    res = run_daily(
        db, cfg, LUNES,
        metrics=dias(LUNES, 10, hrv=60.0, rhr=50.0, sleep_min=340, sleep_score=80),
        rides=[], hevy_client=HevyFalso(), telegram_client=tg,
    )
    assert res.decision.light == "amber", "el montaje tenía que dar un día no verde"
    assert "9 días seguidos sin un verde" in tg.enviados[0]


# ---------------------------------------------------------------------------
# Que la mañana LEA el histórico de check-ins, no solo que pueda
# ---------------------------------------------------------------------------
#
# Quitarle el defecto a `checkin_history` obliga a pasarlo, pero no obliga a
# pasarlo BIEN: `run_daily` podía cumplir el tipo con un `[]` fijo y el
# parámetro seguiría muerto con una firma más estricta. Es literalmente lo que
# pasó con `sessions` durante toda la vida del sistema. Esto lo comprueba desde
# fuera, contra la base de datos.


def test_la_mañana_lee_el_historico_de_checkins_de_la_base(db, cfg):
    from app.repository import upsert_checkin

    for i in range(1, 11):
        upsert_checkin(db, LUNES - timedelta(days=i), {"fatigue": 3}, config=cfg)
    db.flush()

    res = corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    serie = res.decision.signals.history["fatigue"]
    assert len(serie) == 10, (
        f"la serie llega con {len(serie)} punto(s): el histórico no se lee. "
        "Un percentil sobre esto sería un percentil de sí mismo."
    )


def test_la_mañana_no_mete_el_checkin_de_hoy_dos_veces(db, cfg):
    """Hoy lo añade `build_signals` por su cuenta, desde `checkin`.

    Si `run_daily` pidiera el histórico hasta HOY inclusive, el valor del día
    entraría por los dos caminos. Da la misma clave de diccionario, así que no
    se duplicaría el punto -pero dejaría el día de hoy dentro de la ventana que
    `resolve_adaptive_threshold` cierra AYER a propósito, que es el error que
    esa precaución existe para evitar.
    """
    from app.repository import upsert_checkin

    upsert_checkin(db, LUNES, {"fatigue": 9}, config=cfg)
    upsert_checkin(db, LUNES - timedelta(days=1), {"fatigue": 2}, config=cfg)
    db.flush()

    res = corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    serie = res.decision.signals.history["fatigue"]
    assert serie[LUNES] == 9
    assert serie[LUNES - timedelta(days=1)] == 2
    assert len(serie) == 2


def test_sin_checkins_anteriores_la_mañana_sigue_funcionando(db, cfg):
    """El arranque del sistema: la serie vacía es un estado válido, no un fallo."""
    res = corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    assert res.decision is not None
    assert res.decision.signals.history["fatigue"] == {}


# ---------------------------------------------------------------------------
# El check-in tardío y lo que queda escrito en Hevy
# ---------------------------------------------------------------------------
#
# La secuencia que abre este agujero es la normal, no una rara:
#
#     09:00  no ha llegado el check-in. El trabajo de respaldo decide con lo que
#            hay -solo Garmin-, sale verde y ESCRIBE `Día 1` en Hevy.
#     10:30  llega el check-in. Sale rojo. La sesión de hoy es recuperación, que
#            no toca Hevy.
#
# La decisión se rehacía bien -dos filas en `decisions`, la de las 09:00 marcada
# `is_current=False`- y aun así en Hevy se quedaba el `Día 1` entero, porque
# `_escribir_hevy` veía que hoy no hay nada que escribir y se iba. Mirado solo,
# ese salto es verdad. Mirado en secuencia, es falso: hoy SÍ se escribió algo, y
# la decisión que lo escribió ya no vale.
#
# El resultado era un Telegram diciendo «Recuperación» y una app enseñando la
# sesión fuerte, sin un solo aviso. Con una hernia L4-L5 el error va en la única
# dirección que no se puede permitir.

CHECKIN_ROJO = {"fatigue": 9, "mood": 2, "sleep_quality": 2, "training_desire": 1,
                "yesterday_rpe": 10, "lower_discomfort": 8, "upper_discomfort": 7}
CHECKIN_VERDE = {"fatigue": 2, "mood": 8, "sleep_quality": 8, "training_desire": 9,
                 "yesterday_rpe": 3, "lower_discomfort": 0, "upper_discomfort": 0}


def manana_sin_checkin(db, cfg, hevy, tg):
    """Las 09:00: el trabajo de respaldo decide sin formulario y escribe."""
    res = corre(db, cfg, hevy=hevy, tg=tg, source="fallback_0900")
    db.flush()
    assert res.hevy_status == "ok", "el montaje exige que a las 09:00 se escriba"
    return res


def checkin_tardio(db, cfg, hevy, tg, valores):
    """Las 10:30: llega el formulario y la decisión se rehace."""
    from app.repository import upsert_checkin

    upsert_checkin(db, LUNES, dict(valores), config=cfg)
    db.flush()
    res = corre(db, cfg, hevy=hevy, tg=tg, source="checkin")
    db.flush()
    return res


def test_un_checkin_rojo_tardio_deshace_lo_que_escribio_el_respaldo(db, cfg):
    """Lo que queda en Hevy tiene que ser de la ÚLTIMA decisión, no de la primera.

    Como la última no escribe nada, la única forma de cumplirlo es devolver la
    rutina a como estaba antes de la primera escritura del día. El día queda
    entonces idéntico a como habría quedado si el respaldo no hubiera corrido,
    que es la propiedad que de verdad se persigue: el respaldo no puede empeorar
    un día por haber actuado.
    """
    hevy, tg = HevyFalso(), TelegramFalso()
    manana_sin_checkin(db, cfg, hevy, tg)
    assert hevy.contenido == "Día 1"

    res = checkin_tardio(db, cfg, hevy, tg, CHECKIN_ROJO)

    assert res.decision.light == "red"
    assert res.decision.session.write_to_hevy is False
    assert res.hevy_status == "reverted"
    assert [d for _, d in hevy.reversiones] == [LUNES]
    assert hevy.contenido == "la rutina de la semana pasada", (
        "en Hevy ha quedado la sesión de una decisión anulada: es exactamente el "
        "fallo que este arreglo existe para cerrar"
    )


def test_la_reversion_queda_registrada_como_escritura_con_su_motivo(db, cfg):
    """Una reversión es un toque a Hevy, y el histórico tiene que poder leerlo.

    Sin fila, el registro del día diría que se puso `Día 1` y ahí se acabó la
    historia. Y la fila tiene que hablar de la rutina que se REVIRTIÓ -`dia_1`-,
    no de la sesión de hoy: sacar el nombre de `decision.session` dejaría escrito
    que se revirtió «Recuperación», que no se tocó nunca.
    """
    hevy, tg = HevyFalso(), TelegramFalso()
    manana_sin_checkin(db, cfg, hevy, tg)
    checkin_tardio(db, cfg, hevy, tg, CHECKIN_ROJO)

    filas = db.scalars(select(HevyWrite).order_by(HevyWrite.id)).all()
    assert [f.status for f in filas] == ["ok", "reverted"]

    vuelta = filas[-1]
    assert vuelta.date == LUNES
    assert vuelta.routine_key == "dia_1", (
        f"la fila de la reversión dice que se revirtió {vuelta.routine_key!r}, "
        f"que no es la rutina que se escribió esta mañana"
    )
    assert vuelta.hevy_routine_id == filas[0].hevy_routine_id
    assert vuelta.reason, "una reversión sin motivo es media auditoría"
    assert "Día 1" in vuelta.reason and "Recuperación" in vuelta.reason
    # Las dos filas apuntan a decisiones DISTINTAS. Es lo que permite reconstruir
    # el orden: quién escribió y quién deshizo.
    assert filas[0].decision_id != vuelta.decision_id


def test_el_motivo_va_en_todas_las_filas_no_solo_en_las_que_fallan(db, cfg):
    """`error` solo se rellena cuando algo se rompe, y con eso una fila normal
    quedaba con el qué y sin el por qué."""
    hevy, tg = HevyFalso(), TelegramFalso()
    manana_sin_checkin(db, cfg, hevy, tg)

    fila = db.scalars(select(HevyWrite)).first()
    assert fila.status == "ok"
    assert fila.error is None, "no ha fallado nada, así que `error` va vacío"
    assert fila.reason, "y aun así el motivo se guarda"


def test_un_checkin_verde_tardio_reescribe_en_vez_de_deshacer(db, cfg):
    """El control. Si la decisión nueva SÍ toca Hevy, se escribe encima y punto.

    Hace falta como test propio porque el arreglo se metió en la rama del salto:
    si por descuido alcanzara a este camino, el día verde acabaría con la rutina
    de la semana pasada puesta. El mismo fallo, en la otra dirección.
    """
    hevy, tg = HevyFalso(), TelegramFalso()
    manana_sin_checkin(db, cfg, hevy, tg)

    res = checkin_tardio(db, cfg, hevy, tg, CHECKIN_VERDE)

    assert res.hevy_status == "ok"
    assert hevy.reversiones == []
    assert hevy.contenido == "Día 1"
    assert len(hevy.llamadas) == 2, "dos escrituras el mismo día, y las dos cuentan"
    assert [f.status for f in db.scalars(select(HevyWrite)).all()] == ["ok", "ok"]


def test_sin_escritura_previa_el_salto_sigue_siendo_un_salto(db, cfg):
    """Un día rojo normal, con el check-in a su hora, no toca Hevy ni para revertir.

    Es la mitad que no se puede perder al arreglar la otra: revertir aquí
    cambiaría la rutina sin motivo, un día en que nadie había escrito nada.
    """
    hevy, tg = HevyFalso(), TelegramFalso()
    res = checkin_tardio(db, cfg, hevy, tg, CHECKIN_ROJO)

    assert res.decision.session.write_to_hevy is False
    assert res.hevy_status == "skipped"
    assert res.hevy_reason == "hoy la sesión no toca Hevy"
    assert hevy.reversiones == [] and hevy.llamadas == []
    assert hevy.contenido == "la rutina de la semana pasada"


@pytest.mark.parametrize("dia_de_la_fila, espera", [
    (LUNES, "reverted"),
    (LUNES - timedelta(days=1), "skipped"),
])
def test_lo_escrito_ayer_no_se_deshace_hoy(db, cfg, dia_de_la_fila, espera):
    """La ventana es el DÍA, y las dos mitades van juntas a propósito.

    Sin el filtro por fecha, cualquier día de recuperación borraría la rutina
    del día anterior por haberla encontrado en la tabla: una escritura de ayer
    es el estado NORMAL de Hevy, no un resto que limpiar.

    La primera versión de este test corría un `run_daily` de verdad el domingo y
    pasaba SIN PROBAR NADA: el domingo no hay sesión de fuerza, así que nunca
    llegaba a existir la escritura `ok` que el filtro tenía que descartar.
    Quitar el filtro de fecha no lo rompía. Lo cazó la batería de mutaciones, y
    es el mismo error de siempre -un test que pasa por el motivo equivocado-.

    Ahora la fila se pone a mano y se prueban los dos días con el MISMO montaje,
    así que lo único que puede explicar la diferencia de resultado es la fecha.
    """
    db.add(HevyWrite(date=dia_de_la_fila, routine_key="dia_1",
                     hevy_routine_id="r1", status="ok", reason="de mentira"))
    db.flush()

    hevy, tg = HevyFalso(), TelegramFalso()
    res = checkin_tardio(db, cfg, hevy, tg, CHECKIN_ROJO)

    assert res.decision.session.write_to_hevy is False, "el montaje: hoy no escribe"
    assert res.hevy_status == espera
    assert bool(hevy.reversiones) is (espera == "reverted")


@pytest.mark.parametrize("estado", ["dry_run", "read_only", "skipped"])
def test_solo_se_deshace_lo_que_de_verdad_llego_a_hevy(db, cfg, estado):
    """`dry_run` no tocó nada. `read_only` se paró antes del PUT. `skipped` ni lo
    intentó. Deshacer cualquiera de ellos cambiaría la rutina por una TERCERA
    cosa: ni la de hoy ni la de antes de hoy.

    Se monta la fila a mano porque lo que se prueba es el criterio de lectura, no
    cómo se llega a cada estado.
    """
    db.add(HevyWrite(date=LUNES, routine_key="dia_1", hevy_routine_id="r1",
                     status=estado, reason="de mentira"))
    db.flush()

    hevy, tg = HevyFalso(), TelegramFalso()
    res = checkin_tardio(db, cfg, hevy, tg, CHECKIN_ROJO)

    assert res.hevy_status == "skipped"
    assert hevy.reversiones == []


def test_una_escritura_a_medias_no_se_deshace_a_ciegas(db, cfg):
    """El caso incómodo: `error` puede haber llegado a medias, o no haber llegado.

    Revertir sin saberlo es adivinar, y adivinar mal deja la rutina en un tercer
    estado. Ese caso ya tiene su propio aviso -la marca de escritura pendiente
    que `/api/health` publica como `pending_write`- y ese es mejor sitio para él.
    """
    db.add(HevyWrite(date=LUNES, routine_key="dia_1", hevy_routine_id="r1",
                     status="error", error="500 de Hevy"))
    db.flush()

    hevy, tg = HevyFalso(), TelegramFalso()
    res = checkin_tardio(db, cfg, hevy, tg, CHECKIN_ROJO)

    assert res.hevy_status == "skipped"
    assert hevy.reversiones == []


def test_si_no_se_puede_deshacer_el_mensaje_dice_que_hacer(db, cfg):
    """Enterarse no basta: la frase tiene que servir para actuar.

    Aquí en Hevy NO falta nada, sobra. Hay puesta una rutina que el sistema ya ha
    decidido que hoy no toca, así que el aviso de siempre -«tendrás que montarlo
    a mano»- diría justo lo contrario de lo que hay que hacer. Tiene que nombrar
    las dos: la que ha quedado y la que toca.
    """
    hevy, tg = HevyFalso(con_copia=False), TelegramFalso()
    manana_sin_checkin(db, cfg, hevy, tg)

    res = checkin_tardio(db, cfg, hevy, tg, CHECKIN_ROJO)

    assert res.hevy_status == "stale"
    assert hevy.contenido == "Día 1", "el montaje: la reversión no ha podido ser"
    assert "Día 1" in res.hevy_reason and "Recuperación" in res.hevy_reason
    assert "NO hagas" in res.hevy_reason
    assert any("Hevy" in p for p in res.problemas), (
        "un estado que el usuario tiene que resolver a mano no puede quedarse "
        "fuera de `problemas`"
    )

    texto = tg.enviados[-1]
    assert texto.startswith("⚠️ <b>En Hevy ha quedado una rutina que hoy NO toca</b>")
    assert "montarlo a mano" not in texto.split("\n\n")[0], (
        "el aviso de «no se ha escrito» manda a hacer lo contrario de lo que "
        "hay que hacer cuando lo que pasa es que sobra una rutina"
    )
    fila = db.scalars(select(HevyWrite).order_by(HevyWrite.id.desc())).first()
    assert fila.status == "stale" and fila.error, "esto sí es una avería"


def test_la_reversion_tambien_se_cuenta_aunque_salga_bien(db, cfg):
    """Que la rutina de Hevy cambie sola entre las nueve y las once es de las
    cosas de las que hay que enterarse, no descubrirlas abriendo la app."""
    hevy, tg = HevyFalso(), TelegramFalso()
    manana_sin_checkin(db, cfg, hevy, tg)
    checkin_tardio(db, cfg, hevy, tg, CHECKIN_ROJO)

    texto = tg.enviados[-1]
    assert texto.startswith("↩️ <b>Hevy se ha devuelto a como estaba</b>")
    assert "Día 1" in texto


def test_en_ensayo_no_se_deshace_nada_pero_se_dice(db, cfg):
    """`--dry-run` no puede tocar Hevy ni para arreglarlo. Y tiene que contar
    qué habría hecho, que es para lo que sirve un ensayo."""
    from app.repository import upsert_checkin

    hevy, tg = HevyFalso(), TelegramFalso()
    corre(db, cfg, hevy=hevy, tg=tg, source="fallback_0900")
    db.flush()

    upsert_checkin(db, LUNES, dict(CHECKIN_ROJO), config=cfg)
    db.flush()
    res = corre(db, cfg, hevy=hevy, tg=tg, source="checkin", dry_run=True)
    db.flush()

    assert res.hevy_status == "dry_run"
    assert hevy.reversiones == []
    assert "Se habría deshecho" in res.hevy_reason
    assert "Día 1" in res.hevy_reason
