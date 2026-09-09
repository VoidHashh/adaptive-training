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


def test_un_ejercicio_sin_estrenar_no_arranca_penalizado(db):
    """`compliance` ausente vale True en el motor (`for_routine`). Guardar un
    False por defecto cerraría la progresión de un ejercicio nuevo sin que
    nadie hubiera fallado nada."""
    estado = EngineState(clean_sessions={("dia_1", "sentadilla"): 0})
    save_state(db, estado, day=LUNES)

    vuelto = load_state(db)
    assert ("dia_1", "sentadilla") not in vuelto.compliance
    comp, _ = vuelto.for_routine("dia_1", ["sentadilla"])
    assert comp["sentadilla"] is True


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


def test_sin_guardar_el_estado_no_sube_ninguna_carga(db, cfg):
    """El contraste que le da valor al test de arriba.

    Mismo bucle, tirando el estado cada día. Si aquí también subiera la carga,
    el test anterior no estaría midiendo la memoria sino otra cosa.
    """
    subidas = []
    for i in range(21):
        dia = LUNES + timedelta(days=i)
        d = decide(cfg, dia, sig_completa(dia), EngineState(program_start=cfg.program_start))
        subidas += d.progression.changes if d.progression else []

    assert subidas, "el escenario ya no progresa en absoluto; el test hay que rehacerlo"
    assert not [e for e in subidas if e.kind == "load"], (
        "sin memoria no debería poder subir carga: revisa qué mide el test anterior"
    )


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
