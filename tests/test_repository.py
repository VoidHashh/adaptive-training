"""Que el motor no amanezca con la memoria en blanco.

Este sistema decide cada mañana apoyándose en lo que recuerda: cuántas sesiones
limpias lleva cada ejercicio, qué reglas especiales siguen vigentes, qué sesión
quedó aplazada, cuándo fue la última descarga. Todo eso vive en un
`EngineState` que existe en memoria mientras el proceso está vivo.

Un fallo aquí no se parece a un error: se parece a un sistema que funciona. La
decisión sale, el mensaje se envía, y lo único que pasa es que la racha de
sesiones limpias vuelve a cero cada noche y el hip thrust no sube nunca. O que
el peso muerto retirado catorce días reaparece al primer reinicio.

Por eso el test principal no es una lista de campos a mano -esa lista se queda
vieja el día que alguien añada uno-, sino un recorrido por
`dataclasses.fields(EngineState)`.
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.engine.decision import ActiveRule, EngineState, advance_state, decide
from app.models import Base, Decision as DecisionRow, ExerciseTarget, PendingStrength, RuleState
from app.repository import (
    CAMPOS_PERSISTIDOS,
    campos_sin_persistir,
    checkin_values,
    current_decision,
    get_checkin,
    load_state,
    read_pending,
    save_decision,
    save_state,
    sliders_del_config,
    state_as_dict,
    upsert_checkin,
)

from tests.conftest import LUNES, sig_completa


@pytest.fixture
def db():
    """Base de datos en memoria, nueva para cada test."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def estado_lleno():
    """Un estado con TODOS los campos puestos, ninguno en su valor por defecto.

    Que no haya defectos es el punto: un campo que se guardase mal pero cuyo
    valor coincidiese con el que se construye al cargar pasaría inadvertido.
    """
    return EngineState(
        clean_sessions={("dia_1", "hip_thrust_barra"): 2, ("dia_2", "remo_t_apoyado"): 0},
        compliance={("dia_1", "hip_thrust_barra"): True, ("dia_2", "remo_t_apoyado"): False},
        current_sets={
            ("dia_1", "hip_thrust_barra"): [
                {"type": "normal", "reps": 10, "weight_kg": 62.5},
                {"type": "normal", "reps": 10, "weight_kg": 62.5},
            ],
            ("dia_2", "remo_t_apoyado"): [{"type": "normal", "reps": 12, "weight_kg": 40.0}],
        },
        # Distintos entre sí y distintos de cero: un cero se confundiría con
        # "nunca guardado" y con el defecto de la columna.
        sessions_since_progress={
            ("dia_1", "hip_thrust_barra"): 3,
            ("dia_2", "remo_t_apoyado"): 7,
        },
        last_routine_light={"dia_1": "green", "dia_2": "amber"},
        active_rules=[
            ActiveRule(
                name="lumbar_retirada",
                action={"drop_exercises": ["peso_muerto_smith"]},
                active_from=LUNES,
                active_until=LUNES + timedelta(days=13),
                entity="peso_muerto_smith",
                reason="molestia lumbar 7/10",
                notify=True,
            )
        ],
        pending_strength=("dia_3", LUNES - timedelta(days=1)),
        program_start=LUNES - timedelta(weeks=4),
        last_deload_start=LUNES - timedelta(weeks=2),
    )


# ---------------------------------------------------------------------------
# La guarda: ningún campo del estado se queda sin guardar
# ---------------------------------------------------------------------------


def test_todos_los_campos_del_estado_estan_contemplados():
    """El test que evita el fallo que nadie ve.

    Si mañana `EngineState` gana un campo y `repository.py` no lo guarda, el
    sistema no dará ningún error: simplemente ese campo volverá a su valor por
    defecto cada arranque. Esto lo dice antes, y con el nombre del campo.
    """
    assert not campos_sin_persistir(), (
        f"campos de EngineState que nadie guarda: {sorted(campos_sin_persistir())}. "
        f"Añádelos a `repository.save_state`/`load_state` y a CAMPOS_PERSISTIDOS."
    )


def test_la_lista_de_campos_no_inventa_ninguno():
    """Al revés que el anterior: que CAMPOS_PERSISTIDOS no nombre campos que ya
    no existen, porque entonces protegería a un fantasma."""
    reales = {f.name for f in dataclass_fields(EngineState)}
    assert set(CAMPOS_PERSISTIDOS) <= reales, (
        f"CAMPOS_PERSISTIDOS nombra campos inexistentes: "
        f"{sorted(set(CAMPOS_PERSISTIDOS) - reales)}"
    )


def test_el_estado_de_prueba_no_deja_ningun_campo_en_su_defecto(estado_lleno):
    """Guarda de `estado_lleno`: si un campo nuevo se quedase con su valor por
    defecto, la vuelta completa lo daría por bueno sin haberlo probado."""
    vacio = EngineState()
    iguales = [
        f.name
        for f in dataclass_fields(EngineState)
        if getattr(estado_lleno, f.name) == getattr(vacio, f.name)
    ]
    assert not iguales, (
        f"`estado_lleno` deja estos campos en su valor por defecto y por tanto "
        f"no los prueba: {iguales}"
    )


# ---------------------------------------------------------------------------
# La vuelta completa
# ---------------------------------------------------------------------------


def test_el_estado_sobrevive_a_la_ida_y_la_vuelta(db, estado_lleno):
    save_state(db, estado_lleno, day=LUNES)
    vuelto = load_state(db, program_start=estado_lleno.program_start)

    assert vuelto.clean_sessions == estado_lleno.clean_sessions
    assert vuelto.compliance == estado_lleno.compliance
    assert vuelto.current_sets == estado_lleno.current_sets, (
        "la carga vigente no ha sobrevivido: el ejercicio volvería al peso de "
        "partida de config.yaml"
    )
    assert vuelto.sessions_since_progress == estado_lleno.sessions_since_progress, (
        "el turno en la cola no ha sobrevivido: al reiniciar, todos los "
        "ejercicios volverían a empatar a cero y el cupo lo ganaría siempre el "
        "primero de la rutina"
    )
    assert vuelto.last_routine_light == estado_lleno.last_routine_light
    assert vuelto.pending_strength == estado_lleno.pending_strength
    assert vuelto.program_start == estado_lleno.program_start
    assert vuelto.last_deload_start == estado_lleno.last_deload_start


def test_la_regla_especial_vuelve_entera_y_no_solo_su_nombre(db, estado_lleno):
    """Una regla sin su `action` está activa y no hace nada.

    Es el peor resultado posible de los tres: el mensaje sigue diciendo "peso
    muerto retirado (hasta el 20/09)" y el peso muerto está en la sesión.
    """
    save_state(db, estado_lleno, day=LUNES)
    regla = load_state(db).active_rules[0]
    original = estado_lleno.active_rules[0]

    assert regla.name == original.name
    assert regla.action == original.action, "la acción es lo que la regla HACE"
    assert regla.active_from == original.active_from
    assert regla.active_until == original.active_until
    assert regla.entity == original.entity
    assert regla.reason == original.reason
    assert regla.notify == original.notify


def test_guardar_dos_veces_no_duplica_ni_revienta(db, estado_lleno):
    """El segundo día. Las tablas tienen clave única, así que un insert ciego
    fallaría aquí y no el primer día, que es cuando se probaría a mano."""
    save_state(db, estado_lleno, day=LUNES)
    save_state(db, estado_lleno, day=LUNES + timedelta(days=1))

    assert len(db.query(ExerciseTarget).all()) == 2
    assert len(db.query(RuleState).all()) == 1
    assert load_state(db).clean_sessions == estado_lleno.clean_sessions


def test_una_racha_que_cambia_se_actualiza_en_vez_de_anadir_otra_fila(db, estado_lleno):
    save_state(db, estado_lleno, day=LUNES)
    estado_lleno.clean_sessions[("dia_1", "hip_thrust_barra")] = 3
    save_state(db, estado_lleno, day=LUNES + timedelta(days=1))

    assert load_state(db).clean_sessions[("dia_1", "hip_thrust_barra")] == 3
    assert len(db.query(ExerciseTarget).all()) == 2


# ---------------------------------------------------------------------------
# Reglas caducadas
# ---------------------------------------------------------------------------


def test_una_regla_que_ya_no_esta_en_el_estado_desaparece_de_la_tabla(db, estado_lleno):
    """`advance_state` devuelve las que SIGUEN vigentes. Si al guardar solo se
    añadiera, la caducada seguiría en la tabla y volvería a cargarse cada
    mañana: retirada a perpetuidad y sin motivo a la vista."""
    save_state(db, estado_lleno, day=LUNES)
    estado_lleno.active_rules = []
    save_state(db, estado_lleno, day=LUNES + timedelta(days=20))

    assert db.query(RuleState).all() == []
    assert load_state(db).active_rules == []


# ---------------------------------------------------------------------------
# La sesión aplazada
# ---------------------------------------------------------------------------


def test_la_sesion_aplazada_se_recuerda(db, estado_lleno):
    save_state(db, estado_lleno, day=LUNES)
    assert read_pending(db) == ("dia_3", LUNES - timedelta(days=1))


def test_al_recuperarla_se_marca_pero_no_se_borra(db, estado_lleno):
    """El histórico de "esta sesión roja se recuperó tres días después" es
    justo lo que se querrá mirar dentro de un mes."""
    save_state(db, estado_lleno, day=LUNES)
    estado_lleno.pending_strength = None
    save_state(db, estado_lleno, day=LUNES + timedelta(days=2))

    assert read_pending(db) is None
    filas = db.query(PendingStrength).all()
    assert len(filas) == 1, "la fila se marca, no se borra"
    assert filas[0].status == "recovered"


def test_no_se_acumulan_dos_aplazadas_de_la_misma_sesion(db, estado_lleno):
    save_state(db, estado_lleno, day=LUNES)
    save_state(db, estado_lleno, day=LUNES + timedelta(days=1))
    assert len(db.query(PendingStrength).all()) == 1


# ---------------------------------------------------------------------------
# Base de datos vacía: el primer día
# ---------------------------------------------------------------------------


def test_una_base_vacia_da_un_estado_limpio_y_no_un_error(db):
    """El primer arranque. Nada que recuperar no es un fallo."""
    estado = load_state(db, program_start=LUNES)
    assert estado.clean_sessions == {}
    assert estado.active_rules == []
    assert estado.pending_strength is None
    assert estado.last_deload_start is None
    assert estado.program_start == LUNES, "esto sí viene, pero del config"


def test_un_ejercicio_sin_estrenar_no_se_guarda_ni_como_si_ni_como_no(db):
    """La ausencia se conserva como ausencia hasta arriba.

    Guardar un False de relleno acusaría de un incumplimiento inventado;
    guardar un True abriría la progresión sobre una sesión que no ha existido.
    Lo correcto es que no haya fila y que el motor reciba `None`.
    """
    estado = EngineState(clean_sessions={("dia_1", "sentadilla"): 0})
    save_state(db, estado, day=LUNES)

    vuelto = load_state(db)
    assert ("dia_1", "sentadilla") not in vuelto.compliance
    comp, _ = vuelto.for_routine("dia_1", ["sentadilla"])
    assert comp["sentadilla"] is None, (
        "un True aquí es el fallo silencioso: convierte 'no tengo registro' "
        "en 'la última sesión fue perfecta' y abre la puerta de la carga"
    )


# ---------------------------------------------------------------------------
# `program_start` no se guarda a propósito
# ---------------------------------------------------------------------------


def test_el_inicio_del_programa_manda_el_config_y_no_la_base_de_datos(db, estado_lleno):
    """Si se guardara, el día que el usuario corrija la fecha en el YAML la
    base seguiría con la vieja y no habría forma de saber cuál manda."""
    save_state(db, estado_lleno, day=LUNES)
    otra = date(2025, 1, 6)
    assert load_state(db, program_start=otra).program_start == otra


# ---------------------------------------------------------------------------
# Representación legible
# ---------------------------------------------------------------------------


def test_el_estado_legible_no_pierde_las_claves_compuestas(estado_lleno):
    d = state_as_dict(estado_lleno)
    assert d["clean_sessions"]["dia_1/hip_thrust_barra"] == 2
    assert d["pending_strength"]["routine"] == "dia_3"
    assert d["last_deload_start"] == estado_lleno.last_deload_start.isoformat()


# ---------------------------------------------------------------------------
# El bucle de verdad, pasando por disco cada día
# ---------------------------------------------------------------------------


def _simula(db, cfg, dias: int, arranque=LUNES):
    """Corre `dias` días haciendo `load_state` y `save_state` en CADA uno.

    Releer el estado del disco todos los días es el punto: imita el proceso que
    se reinicia, que en un Umbrel pasa cada vez que se actualiza el contenedor.
    Una vuelta completa que funciona en memoria puede seguir perdiéndolo todo
    aquí si el bucle se olvida de guardar.
    """
    decisiones = []
    for i in range(dias):
        dia = arranque + timedelta(days=i)
        estado = load_state(db, program_start=cfg.program_start)
        d = decide(cfg, dia, sig_completa(dia), estado)
        # Sesión ejecutada y limpia: es lo que alimenta la racha.
        ejecutado = {ex["key"]: True for ex in d.session.exercises if ex.get("key")}
        save_state(db, advance_state(estado, d, executed=ejecutado), day=dia)
        decisiones.append(d)
    return decisiones


def test_la_racha_se_acumula_entre_reinicios_y_el_programa_progresa(db, cfg):
    """La prueba de que esto sirve para algo.

    Si el estado no sobreviviera al disco, la racha volvería a cero cada día,
    nunca llegaría a `clean_sessions_required` y NADA subiría jamás. El sistema
    seguiría mandando su mensaje cada mañana, con los mismos pesos para
    siempre, sin un solo error en el log.
    """
    decisiones = _simula(db, cfg, dias=21)

    # Se mira la subida de CARGA, no cualquier cambio. El volumen sube también
    # sin memoria -no necesita racha-, así que un `assert subidas` a secas
    # pasaría con la base de datos desconectada y no probaría nada. La carga es
    # lo único que exige `clean_sessions_required` sesiones limpias seguidas, y
    # por tanto lo único que demuestra que el estado sobrevive al disco.
    cargas = [
        e
        for d in decisiones
        for e in (d.progression.changes if d.progression else [])
        if e.kind == "load"
    ]
    assert cargas, "en tres semanas no ha subido ninguna carga: el estado no se recuerda"

    rachas = load_state(db).clean_sessions
    assert any(v > 0 for v in rachas.values()), f"todas las rachas a cero: {rachas}"


def test_sin_guardar_el_estado_no_se_mueve_absolutamente_nada(db, cfg):
    """El contraste que le da valor al test de arriba.

    Mismo bucle, tirando el estado cada día.

    Este test comprobaba antes algo más flojo: que sin memoria no subiera la
    CARGA, dando por bueno que el volumen sí subiera. Y subía: 12 anuncios de
    subida en tres semanas, repitiendo los mismos -`gemelo_sentado` 12→13 el
    día 7 y otra vez el día 14-, que es la firma exacta de la amnesia. Anunciar
    para siempre, avanzar nunca.

    El motivo era `for_routine`, que convertía "no tengo ni un registro de este
    ejercicio" en "la última sesión fue perfecta". La puerta general se abría
    con eso, y la puerta gobierna también el volumen. Un contenedor que
    perdiera la base de datos habría seguido mandando su mensaje cada mañana,
    con subidas inventadas, sin un solo error en el log.

    Ahora la ausencia de registro cierra la puerta: sin memoria no se mueve
    nada.
    """
    decisiones = []
    for i in range(21):
        dia = LUNES + timedelta(days=i)
        decisiones.append(
            decide(cfg, dia, sig_completa(dia), EngineState(program_start=cfg.program_start))
        )

    subidas = [
        e for d in decisiones for e in (d.progression.changes if d.progression else [])
    ]
    assert not subidas, f"sin memoria no debería moverse nada, y se movió: {subidas}"

    # Y que no se mueva por el motivo correcto, no porque el escenario se haya
    # quedado por el camino sin una sola sesión de fuerza. Sin esta parte el
    # test de arriba pasaría aunque `decide` devolviera siempre None.
    con_rutina = [d.progression for d in decisiones if d.progression]
    assert con_rutina, "el escenario ya no tiene ni una sesión de fuerza; rehazlo"
    assert not any(p.gate_open for p in con_rutina)
    motivos = {p.gate_reason for p in con_rutina}
    assert any("no hay registro" in m for m in motivos), motivos


# ---------------------------------------------------------------------------
# Check-in
# ---------------------------------------------------------------------------


def test_el_checkin_se_guarda_y_se_lee(db, cfg):
    upsert_checkin(db, LUNES, {"fatigue": 4, "lower_discomfort": 2}, config=cfg)
    assert checkin_values(get_checkin(db, LUNES)) == {
        "fatigue": 4,
        "lower_discomfort": 2,
    }


def test_reenviar_el_formulario_corrige_en_vez_de_duplicar(db, cfg):
    """Uno se equivoca de deslizador y lo vuelve a mandar. Vale el último."""
    upsert_checkin(db, LUNES, {"fatigue": 9}, config=cfg)
    upsert_checkin(db, LUNES, {"fatigue": 3}, config=cfg)
    assert checkin_values(get_checkin(db, LUNES))["fatigue"] == 3


def test_un_deslizador_sin_contestar_no_existe_en_vez_de_valer_cero(db, cfg):
    """Para las reglas no es lo mismo "molestia 0" que "no lo he contestado":
    lo primero es un dato bueno, lo segundo hace que la regla no se evalúe."""
    upsert_checkin(db, LUNES, {"fatigue": 4}, config=cfg)
    valores = checkin_values(get_checkin(db, LUNES))
    assert "lower_discomfort" not in valores
    assert valores["fatigue"] == 4


def test_un_cero_de_verdad_si_se_guarda(db, cfg):
    """La otra cara: un 0 contestado es un dato y tiene que llegar."""
    upsert_checkin(db, LUNES, {"lower_discomfort": 0}, config=cfg)
    assert checkin_values(get_checkin(db, LUNES))["lower_discomfort"] == 0


def test_un_deslizador_mal_escrito_es_un_error_y_no_un_campo_ignorado(db, cfg):
    """`fatiga` por `fatigue` desde la PWA se guardaría en ninguna parte y el
    sistema decidiría sin ese dato creyendo el check-in completo."""
    with pytest.raises(ValueError, match="fatiga"):
        upsert_checkin(db, LUNES, {"fatiga": 4}, config=cfg)


def test_una_columna_real_que_no_es_deslizador_tambien_se_rechaza(db, cfg):
    """`comments` es columna, pero no un deslizador: va por su parámetro."""
    with pytest.raises(ValueError, match="checkin_sliders"):
        upsert_checkin(db, LUNES, {"comments": "hola"}, config=cfg)


def test_los_deslizadores_validos_salen_del_yaml(cfg):
    """Los siete del config, incluido el `yesterday_rpe` que se añadió después
    del diseño inicial. Una lista a mano se habría quedado en seis."""
    claves = sliders_del_config(cfg)
    assert "yesterday_rpe" in claves
    assert len(claves) == 7


def test_los_comentarios_se_guardan_aparte(db, cfg):
    upsert_checkin(db, LUNES, {"fatigue": 4}, config=cfg, comments="lumbar rara")
    fila = get_checkin(db, LUNES)
    assert fila.comments == "lumbar rara"
    assert "comments" not in checkin_values(fila), "no es una señal para las reglas"


# ---------------------------------------------------------------------------
# Decisiones
# ---------------------------------------------------------------------------


def test_la_decision_se_guarda_con_lo_que_hace_falta_para_reproducirla(db, cfg):
    d = decide(cfg, LUNES, sig_completa(LUNES), EngineState())
    fila = save_decision(db, d)

    assert fila.light == d.light
    assert fila.is_current is True
    assert fila.config_hash == d.config_hash, "sin esto no se puede reproducir"
    assert fila.inputs_snapshot_json, "ni sin la foto de las señales"


def test_decidir_dos_veces_el_mismo_dia_deja_rastro_de_la_primera(db, cfg):
    """A las 07:00 sin check-in y a las 09:40 con él. La primera no se pisa:
    se marca. Es lo que hace depurable un semáforo que cambió a media mañana."""
    primera = decide(cfg, LUNES, sig_completa(LUNES), EngineState())
    save_decision(db, primera)

    segunda = decide(cfg, LUNES, sig_completa(LUNES, lower_discomfort=7), EngineState())
    save_decision(db, segunda)

    todas = db.query(DecisionRow).filter(DecisionRow.date == LUNES).all()
    assert len(todas) == 2, "append-only: la primera no se borra"
    assert sum(1 for f in todas if f.is_current) == 1
    assert current_decision(db, LUNES).light == segunda.light
    assert segunda.light == "red", "el escenario ya no cambia el semáforo; rehacer"


# ---------------------------------------------------------------------------
# La carga progresada se acumula
# ---------------------------------------------------------------------------


def _tope_vigente(session, rutina: str, ejercicio: str) -> float | None:
    """El peso de la serie efectiva más pesada, leído de la base de datos."""
    estado = load_state(session)
    series = estado.current_sets.get((rutina, ejercicio))
    if not series:
        return None
    pesos = [s.get("weight_kg") or 0 for s in series]
    return max(pesos) if any(pesos) else None


def test_la_carga_progresada_se_acumula_en_vez_de_reiniciarse(db, cfg):
    """Tres progresiones seguidas del mismo ejercicio tienen que SUMAR.

    Este es el test del fallo más caro que ha tenido el proyecto, y no daba
    ningún error. La sesión se construía siempre desde `config.yaml` y el
    incremento se sumaba encima, así que la carga oscilaba entre el peso de
    partida y ese peso más un escalón, para siempre. Telegram anunciaba
    "100→105 kg" cada pocas semanas, Hevy mostraba 105 ese día, y la siguiente
    vez que tocaba esa rutina volvía a 100. Visto desde fuera era un sistema
    que progresa; medido, era uno que no se mueve.

    Se simulan varios meses entrenando todo lo que se manda, y se mira la carga
    VIGENTE que queda guardada, no la que se escribe hoy: una semana de descarga
    escribe menos a propósito y eso no es un retroceso.
    """
    raw = cfg.raw
    rutina = "dia_1"
    ejercicio = next(
        e for e in raw["routines"][rutina]["exercises"]
        if any((s.get("weight_kg") or 0) > 0 for s in e.get("sets") or [])
    )
    clave = ejercicio["key"]
    partida = max((s.get("weight_kg") or 0) for s in ejercicio["sets"])
    incremento = float(
        ejercicio.get("increment_kg",
                      (raw.get("progression") or {}).get("default_increment_kg", 2.5))
    )

    vistos: list[float] = [partida]
    for n in range(140):
        dia = LUNES + timedelta(days=n)
        estado = load_state(db, program_start=raw.get("program", {}).get("start_date"))
        decision = decide(cfg, dia, sig_completa(dia), estado)
        sesion = decision.session
        if sesion.routine_key == rutina and sesion.kind in {"full", "reduced"}:
            # Se entrena todo lo mandado: es la única forma de acumular racha.
            ejecutado = {e["key"]: True for e in sesion.exercises if e.get("key")}
        else:
            ejecutado = None
        nuevo = advance_state(estado, decision, executed=ejecutado)
        save_state(db, nuevo, day=dia)

        tope = _tope_vigente(db, rutina, clave)
        if tope is not None and tope != vistos[-1]:
            vistos.append(tope)
        if len(vistos) >= 4:
            break

    esperado = [partida, partida + incremento, partida + 2 * incremento]
    assert vistos[:3] == esperado, (
        f"'{clave}' no acumula carga: se ha visto {vistos[:3]} y tenía que ser "
        f"{esperado}. Si se repite el mismo peso, la progresión se está "
        f"calculando otra vez sobre el punto de partida de config.yaml."
    )
    assert vistos == sorted(vistos), f"la carga ha retrocedido: {vistos}"


def _simular(db, cfg, dias: int, rutina: str) -> dict[str, list[list[dict]]]:
    """Entrena todo lo que se manda y devuelve, por ejercicio, la traza de
    series vigentes DISTINTAS que han ido quedando guardadas en la base."""
    raw = cfg.raw
    traza: dict[str, list[list[dict]]] = {}
    for n in range(dias):
        dia = LUNES + timedelta(days=n)
        estado = load_state(db, program_start=raw.get("program", {}).get("start_date"))
        decision = decide(cfg, dia, sig_completa(dia), estado)
        sesion = decision.session
        if sesion.routine_key == rutina and sesion.kind in {"full", "reduced"}:
            ejecutado = {e["key"]: True for e in sesion.exercises if e.get("key")}
        else:
            ejecutado = None
        save_state(db, advance_state(estado, decision, executed=ejecutado), day=dia)

        for (rk, ek), series in load_state(db).current_sets.items():
            if rk != rutina:
                continue
            t = traza.setdefault(ek, [])
            if not t or t[-1] != series:
                t.append(series)
    return traza


def _claves_por_modo(cfg, rutina: str) -> dict[str, list[str]]:
    modos: dict[str, list[str]] = {}
    for e in cfg.raw["routines"][rutina]["exercises"]:
        modos.setdefault(str(e.get("progression_type")), []).append(e["key"])
    return modos


def test_las_reps_del_modo_volume_se_acumulan(db, cfg):
    """El volumen tiene el mismo fallo que la carga si no se persiste.

    En `volume` no hay peso que mirar: lo que sube son reps o segundos, y viven
    dentro de las mismas series. Si el estado no vuelve, el plan de mañana se
    calcula otra vez sobre las reps de `config.yaml` y el ejercicio se queda
    clavado en 12→14 para siempre, que es justo lo que no se ve en Telegram
    porque el mensaje sí anuncia la subida cada vez.
    """
    modos = _claves_por_modo(cfg, "dia_1")
    traza = _simular(db, cfg, 140, "dia_1")

    con_reps = [
        k for k in modos.get("volume", [])
        if traza.get(k) and any(s.get("reps") for s in traza[k][0])
    ]
    assert con_reps, "config.yaml ya no tiene ningún ejercicio volume por reps"

    for clave in con_reps:
        serie_reps = [min(int(s["reps"]) for s in v if s.get("reps"))
                      for v in traza[clave]]
        assert len(serie_reps) >= 3, (
            f"'{clave}' solo ha cambiado {len(serie_reps)} vez/veces en 140 días: "
            f"{serie_reps}. El volumen no está acumulando."
        )
        assert serie_reps == sorted(serie_reps), (
            f"'{clave}' ha RETROCEDIDO en reps: {serie_reps}"
        )
        assert serie_reps[2] > serie_reps[0], (
            f"'{clave}' no suma: {serie_reps[:3]}. Si las reps oscilan entre dos "
            f"valores, se están recalculando sobre el punto de partida del YAML."
        )


def test_los_segundos_del_modo_volume_se_acumulan(db, cfg):
    """Lo mismo que las reps, pero para los isométricos (plancha y compañía),
    que progresan en `duration_s` y no tienen ni peso ni repeticiones."""
    modos = _claves_por_modo(cfg, "dia_1")
    traza = _simular(db, cfg, 140, "dia_1")

    con_segundos = [
        k for k in modos.get("volume", [])
        if traza.get(k) and any(s.get("duration_s") for s in traza[k][0])
    ]
    assert con_segundos, "config.yaml ya no tiene ningún ejercicio volume por tiempo"

    for clave in con_segundos:
        segs = [min(int(s["duration_s"]) for s in v if s.get("duration_s"))
                for v in traza[clave]]
        assert len(segs) >= 3, f"'{clave}' apenas se mueve en 140 días: {segs}"
        assert segs == sorted(segs), f"'{clave}' ha RETROCEDIDO en segundos: {segs}"
        assert segs[2] > segs[0], (
            f"'{clave}' no suma segundos: {segs[:3]}. Se están recalculando sobre "
            f"el punto de partida del YAML."
        )


def test_las_series_anadidas_en_modo_sets_se_acumulan(db, cfg):
    """El modo `sets` es el que más importa con una hernia L4-L5.

    Es la vía por la que el sistema añade volumen ANTES que carga -una serie más
    de hip thrust antes que 5 kg más-, así que si la cuenta de series no vuelve
    de la base, el ejercicio se queda en las series del YAML y el sistema pasa a
    subir peso mucho antes de lo que debería. El fallo silencioso aquí no es
    "progresa menos": es "progresa por donde no toca".
    """
    modos = _claves_por_modo(cfg, "dia_1")
    traza = _simular(db, cfg, 140, "dia_1")
    claves = [k for k in modos.get("sets", []) if traza.get(k)]
    assert claves, "config.yaml ya no tiene ningún ejercicio en modo sets"

    for clave in claves:
        cuentas = [len(v) for v in traza[clave]]
        assert cuentas == sorted(cuentas), (
            f"'{clave}' ha PERDIDO series por el camino: {cuentas}"
        )
        assert max(cuentas) > cuentas[0], (
            f"'{clave}' nunca añade una serie: {cuentas}. Con el estado sin "
            f"persistir, cada día vuelve a las series de config.yaml y la serie "
            f"añadida ayer desaparece."
        )


def test_ningun_ejercicio_se_queda_sin_progresar_por_perder_siempre_el_cupo(db, cfg):
    """La cola de los cupos tiene que rotar. Nadie pasa hambre.

    Hay como mucho `max_volume_increases_per_session` subidas de volumen por
    sesión, y en Día 1 hay más candidatos que cupo casi todas las semanas. El
    desempate es `queue_policy: waiting_longest`, pero `waiting` lo alimentaba
    `sessions_since_progress`, que en producción no lo rellenaba NADIE: solo lo
    pasaban los scripts de simulación. Con el contador siempre a cero el único
    criterio que quedaba era el orden de la rutina, y los ejercicios del final
    perdían el cupo todas las veces.

    El daño no era "progresa más despacio". En 140 días simulados el perro de
    caza no subía ni una sola repetición y la plancha lateral subía una vez,
    mientras la prensa sumaba carga: los dos que se quedaban parados son los de
    estabilidad lumbar, que con una hernia L4-L5 son justo los que deben ganar
    volumen antes de que nada gane peso. Y no había forma de notarlo, porque el
    mensaje diario era correcto cada mañana: solo decía lo que subía hoy, nunca
    lo que llevaba medio año sin subir.
    """
    modos = _claves_por_modo(cfg, "dia_1")
    traza = _simular(db, cfg, 140, "dia_1")

    candidatos = [k for k in modos.get("volume", []) if traza.get(k)]
    assert candidatos, "config.yaml ya no tiene ejercicios en modo volume en dia_1"

    parados = [k for k in candidatos if len(traza[k]) < 2]
    assert not parados, (
        f"en 140 días estos ejercicios no progresaron NUNCA: {parados}. "
        f"Pierden el cupo de volumen en todas las sesiones, así que la cola no "
        f"está rotando: comprueba que `sessions_since_progress` llega de verdad "
        f"a `plan_progression` y que avanza en `apply_execution`."
    )
