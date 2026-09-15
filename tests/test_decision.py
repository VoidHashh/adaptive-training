"""El orquestador: `decide` y `advance_state` contra el config real.

Aquí se prueba lo que solo se puede romper en la juntura entre módulos: el
orden de las operaciones, la persistencia de las reglas especiales entre días,
el ámbito rutina+ejercicio del estado, y el avance del estado tras ejecutar la
sesión.

Es la traducción a pytest de `scripts/smoke_decision.py`, que se mantiene como
herramienta de diagnóstico manual (imprime la traza entera); lo que se ejecuta
en automático es esto.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from app.engine.decision import EngineState, advance_state, decide
from app.engine.signals import Signals

from tests.conftest import LUNES, sig

JUEVES = LUNES + timedelta(days=3)


# ---------------------------------------------------------------------------
# Semáforo -> sesión
# ---------------------------------------------------------------------------


def test_un_dia_sin_señales_malas_es_verde_y_entrena_entero(cfg):
    d = decide(cfg, LUNES, sig(LUNES), EngineState())
    assert d.light == "green"
    assert d.session.kind == "full"
    assert d.session.routine_key == "dia_1"
    assert d.rotation_routine == "dia_1"
    assert d.progression is not None
    assert d.bike is not None
    assert not d.deload.active


def test_una_molestia_cervical_de_6_baja_a_ambar_y_reduce(cfg):
    d = decide(cfg, LUNES, sig(LUNES, upper_discomfort=6), EngineState())
    assert d.light == "amber"
    assert d.session.kind == "reduced"
    assert d.progression is not None and not d.progression.gate_open


def test_una_molestia_lumbar_de_7_pone_el_dia_en_rojo(cfg):
    d = decide(cfg, LUNES, sig(LUNES, lower_discomfort=7), EngineState())
    assert d.light == "red"
    assert d.session.kind == "recovery"


# ---------------------------------------------------------------------------
# La rotación se aplaza sola
# ---------------------------------------------------------------------------
#
# Aquí estaban los cinco tests del aplazamiento: la sesión de un día rojo
# quedaba pendiente en una tabla, se recuperaba en el siguiente día libre y
# verde, y caducaba a los siete días. Toda esa maquinaria existía para
# compensar un calendario fijo: si el lunes tocaba `dia_1` y el lunes salía
# rojo, había que anotar en algún sitio que `dia_1` no se había hecho, porque
# el martes el calendario ya estaba diciendo otra cosa.
#
# Con la rotación no hay nada que anotar. El puntero es "la última sesión de
# fuerza que HICE", así que un día que no se entrena no lo mueve, y la sesión
# que no se hizo sigue siendo la siguiente mañana, y la siguiente, hasta que se
# haga. El aplazamiento no se ha quitado: se ha vuelto la conducta por defecto.


def test_un_dia_rojo_no_mueve_el_puntero(cfg):
    """Lo que antes hacían la tabla `pending_strength` y sus tres estados.

    El bloque de recuperación de un día rojo no es un escalón del ciclo: se
    hace en casa, con banda elástica, y no es el Día 1. Si lo contara, la
    próxima vez que pisara el gimnasio me tocaría el Día 2 sin haber hecho
    nunca el Día 1.
    """
    d = decide(cfg, LUNES, sig(LUNES, lower_discomfort=7), EngineState())
    assert d.session.kind == "recovery"

    st = advance_state(EngineState(), d, executed={})
    assert st.last_strength is None, (
        "el bloque de recuperación ha movido la rotación: la sesión que el rojo "
        "protegía se habría perdido igual que con el calendario fijo"
    )

    # Y al día siguiente vuelve a tocar lo mismo, sin que nadie lo haya anotado.
    martes = LUNES + timedelta(days=1)
    d2 = decide(cfg, martes, sig(martes), st)
    assert d2.rotation_routine == "dia_1"
    assert d2.session.routine_key == "dia_1"


def test_planificar_no_es_entrenar(cfg):
    """El puntero sale de lo EJECUTADO, nunca de lo planificado.

    Es la diferencia entera entre la rotación y el calendario que sustituye. Un
    puntero que avanzara al planificar correría solo: tres días sin pisar el
    gimnasio y me habría "hecho" el ciclo completo sin levantar nada.
    """
    d = decide(cfg, LUNES, sig(LUNES), EngineState())
    assert d.session.routine_key == "dia_1"

    st = advance_state(EngineState(), d, executed=None)
    assert st.last_strength is None

    d2 = decide(cfg, LUNES + timedelta(days=1), sig(LUNES + timedelta(days=1)), st)
    assert d2.rotation_routine == "dia_1", "la rotación ha avanzado sin entrenar"


def test_haber_entrenado_de_verdad_si_mueve_el_puntero(cfg):
    d = decide(cfg, LUNES, sig(LUNES), EngineState())
    st = advance_state(
        EngineState(), d, executed={k["key"]: True for k in d.session.exercises}
    )
    assert st.last_strength == ("dia_1", LUNES)

    martes = LUNES + timedelta(days=1)
    assert decide(cfg, martes, sig(martes), st).rotation_routine == "dia_2"


def test_decir_que_no_vas_no_mueve_la_rotacion_ni_cuenta_como_saltada(cfg):
    """Decir «hoy no voy» tiene que costar exactamente lo mismo que no decir nada.

    Es la mitad silenciosa de la pregunta del check-in, y la que decide si el
    usuario la contesta más de una semana. Si contestar que no tuviera cualquier
    coste -perder el turno de la rutina, sumar a un contador de saltadas,
    reiniciar una racha- la pregunta se quedaría sin contestar y el sistema
    volvería a no saber nada de los días que no se entrena, que es justo lo que
    la pregunta existe para arreglar.

    No hace falta que `advance_state` sepa nada de `will_train`, y a propósito:
    el puntero ya se mueve solo con `executed is not None`, o sea al reconciliar
    contra Hevy por la noche. Lo que este test fija es que eso SIGA siendo así, y
    que a nadie se le ocurra "ayudar" restando algo por la mañana. Se compara
    campo a campo contra el mismo día sin contestar en vez de mirar solo la
    rotación: un contador nuevo que se tocara aquí pasaría desapercibido.
    """
    st = EngineState(last_strength=("dia_1", LUNES))
    martes = LUNES + timedelta(days=1)

    callado = advance_state(st, decide(cfg, martes, sig(martes), st), executed=None)
    dije_que_no = advance_state(
        st, decide(cfg, martes, sig(martes, will_train=False), st), executed=None
    )

    assert dije_que_no.last_strength == st.last_strength == ("dia_1", LUNES)
    assert dije_que_no.__dict__ == callado.__dict__, (
        "contestar «no voy» ha cambiado el estado respecto a no contestar"
    )

    miercoles = LUNES + timedelta(days=2)
    assert decide(cfg, miercoles, sig(miercoles), dije_que_no).rotation_routine == "dia_2"


def test_y_si_dice_que_no_y_entrena_igual_cuenta_como_una_sesion_normal(cfg):
    """Manda Hevy, no el formulario.

    La respuesta de la mañana es una intención, no un hecho. El hecho es la
    sesión leída por la noche, y cuando existe vale por encima de lo que se
    contestó: mueve el puntero, cuenta la sesión limpia y sigue la racha igual
    que cualquier otro día. Un sistema que descartara la sesión por contradecir
    el formulario estaría castigando al usuario por cambiar de idea.
    """
    st = EngineState(last_strength=("dia_1", LUNES))
    martes = LUNES + timedelta(days=1)

    d = decide(cfg, martes, sig(martes, will_train=False), st)
    ejecutado = {e["key"]: True for e in d.session.exercises}
    assert ejecutado, "la sesión se escribe en Hevy igual, con sus ejercicios"

    despues = advance_state(st, d, executed=ejecutado)
    assert despues.last_strength == ("dia_2", martes)

    miercoles = LUNES + timedelta(days=2)
    assert decide(cfg, miercoles, sig(miercoles), despues).rotation_routine == "dia_3"


def test_da_igual_el_dia_de_la_semana_que_sea(cfg):
    """Ni "los lunes toca Día 1" ni "los domingos se descansa".

    Se entrena cuando se va al gimnasio. El mismo estado tiene que dar la misma
    rutina los siete días, porque lo que manda es por dónde va el ciclo.
    """
    st = EngineState(last_strength=("dia_1", LUNES))
    for salto in range(7):
        dia = LUNES + timedelta(days=1 + salto)
        d = decide(cfg, dia, sig(dia), st)
        assert d.rotation_routine == "dia_2", f"{dia} ({dia.strftime('%A')}) se ha salido"
        assert d.session.kind == "full"


def test_el_ciclo_da_la_vuelta_pasando_por_el_dia_3(cfg):
    """Las tres, en orden, y el Día 3 dentro.

    Con el calendario `with_pool` que había activo, `dia_3` no aparecía en
    ningún día de la semana: todas las sesiones de Día 3 se registraban como
    entrenos sueltos, sin racha, sin adopción de carga y sin progresión.
    """
    st = EngineState()
    hechas = []
    for i in range(4):
        dia = LUNES + timedelta(days=i * 2)
        d = decide(cfg, dia, sig(dia), st)
        hechas.append(d.session.routine_key)
        st = advance_state(st, d, executed={k["key"]: True for k in d.session.exercises})

    assert hechas == ["dia_1", "dia_2", "dia_3", "dia_1"]


def test_una_semana_entera_sin_pisar_el_gimnasio_deja_el_puntero_quieto(cfg):
    """Lo que antes caducaba a los siete días. Ahora no caduca: espera."""
    st = EngineState(last_strength=("dia_2", LUNES))
    for i in range(1, 15):
        dia = LUNES + timedelta(days=i)
        d = decide(cfg, dia, sig(dia), st)
        st = advance_state(st, d, executed=None)

    assert st.last_strength == ("dia_2", LUNES)
    ultimo = decide(cfg, LUNES + timedelta(days=15), sig(LUNES + timedelta(days=15)), st)
    assert ultimo.rotation_routine == "dia_3"


# ---------------------------------------------------------------------------
# Reglas especiales: vigencia por calendario, no por síntoma de hoy
# ---------------------------------------------------------------------------


def hist_lumbar(dia: date, valor: int, dias: int = 2) -> dict:
    return {"lower_discomfort": {dia - timedelta(days=i): valor for i in range(dias)}}


def test_dos_dias_de_lumbar_a_5_retiran_el_peso_muerto(cfg):
    señales = Signals(
        day=JUEVES,
        values={"lower_discomfort": 5},
        history=hist_lumbar(JUEVES, 5),
    )
    d = decide(cfg, JUEVES, señales, EngineState())
    nombres = [r.name for r in d.active_rules]
    assert "retirada_peso_muerto" in nombres
    assert "peso_muerto_smith" not in [e.get("key") for e in d.session.exercises]


def test_la_retirada_dura_catorce_dias(cfg):
    señales = Signals(
        day=JUEVES, values={"lower_discomfort": 5}, history=hist_lumbar(JUEVES, 5)
    )
    d = decide(cfg, JUEVES, señales, EngineState())
    regla = next(r for r in d.active_rules if r.name == "retirada_peso_muerto")
    assert (regla.active_until - regla.active_from).days == 13  # 14 días inclusive


def test_la_regla_sobrevive_a_que_hoy_no_duela_nada(cfg):
    """Si caducara con el síntoma, la protección desaparecería el primer día
    bueno, que es justo cuando uno se anima a cargar."""
    señales = Signals(
        day=JUEVES, values={"lower_discomfort": 5}, history=hist_lumbar(JUEVES, 5)
    )
    st = advance_state(EngineState(), decide(cfg, JUEVES, señales, EngineState()), executed=None)

    despues = JUEVES + timedelta(days=7)
    d = decide(cfg, despues, sig(despues, lower_discomfort=0), st)
    assert "retirada_peso_muerto" in [r.name for r in d.active_rules]


def test_y_caduca_al_dia_quince(cfg):
    señales = Signals(
        day=JUEVES, values={"lower_discomfort": 5}, history=hist_lumbar(JUEVES, 5)
    )
    st = advance_state(EngineState(), decide(cfg, JUEVES, señales, EngineState()), executed=None)

    caducada = JUEVES + timedelta(days=14)
    d = decide(cfg, caducada, sig(caducada, lower_discomfort=0), st)
    assert "retirada_peso_muerto" not in [r.name for r in d.active_rules]


def test_el_recorte_de_carga_de_una_regla_aplica_su_factor_y_cae_en_disco(cfg):
    """Y se ACUMULA con el recorte de series del ámbar, no lo sustituye.

    Antes esto comparaba contra `antes[0] * 0.70` pelado, y pasaba de milagro:
    el config tenía 10 kg en esa serie y 10 × 0,70 = 7,0 cae justo en la
    rejilla de medio kilo. Al releer las rutinas de Hevy el 11-09-2026 la serie
    pasó a 12,5 kg; 12,5 × 0,70 = 8,75 y el motor devolvió 9,0 — que es lo
    CORRECTO, porque `cut_load` redondea a 0,5 kg a propósito: no hay discos de
    8,75, y un número que no se puede cargar no es un objetivo, es un adorno.

    O sea que el contrato nunca fue "el factor exacto", era "el factor y
    después la rejilla". Se comprueban las dos cosas por separado en vez de
    clavar un literal: los pesos del config cambian cada vez que se releen las
    rutinas, y un test que se rompe por eso no está midiendo el motor.
    """
    viernes = date(2026, 9, 11)
    antes = [
        s.get("weight_kg")
        for ex in cfg.raw["routines"]["dia_3"]["exercises"]
        if ex["key"] == "press_hombro_maquina"
        for s in ex["sets"]
    ]

    # El `dia_3` se pide por donde va el ciclo, no por el día de la semana: se
    # viene de haber hecho el `dia_2`. Antes esto necesitaba una variante de
    # calendario entera -`summer`- solo porque la activa no lo programaba nunca.
    despues_del_dia_2 = EngineState(last_strength=("dia_2", viernes - timedelta(days=2)))
    d = decide(cfg, viernes, sig(viernes, upper_discomfort=6), despues_del_dia_2)
    assert d.session.routine_key == "dia_3"
    despues = [
        s.get("weight_kg")
        for ex in d.session.exercises
        if ex.get("key") == "press_hombro_maquina"
        for s in ex["sets"]
    ]

    assert "descarga_press_hombro" in [r.name for r in d.active_rules]
    esperado = antes[0] * 0.70
    assert abs(despues[0] - esperado) <= 0.25, (
        f"el recorte debería quedar a menos de medio disco de {esperado} kg, "
        f"y salió {despues[0]}"
    )
    assert despues[0] * 2 == int(despues[0] * 2), (
        f"{despues[0]} kg no se puede cargar: el recorte tiene que caer en la "
        "rejilla de 0,5 kg"
    )
    assert d.light == "amber"
    assert len(despues) < len(antes), "el ámbar recorta series ADEMÁS de la carga"


# ---------------------------------------------------------------------------
# Semana de descarga
# ---------------------------------------------------------------------------
# Es la parte del sistema con consecuencias a más largo plazo: 44 años, déficit
# calórico y hernia L4-L5. Que no se active nunca es un fallo silencioso, así
# que su calendario se prueba entero.


@pytest.fixture
def estado_en_descarga():
    return EngineState(program_start=LUNES - timedelta(weeks=7))


def test_a_las_siete_semanas_toca_descarga(cfg, estado_en_descarga):
    d = decide(cfg, LUNES, sig(LUNES), estado_en_descarga)
    assert d.deload.active
    assert d.deload.reason


def test_la_descarga_congela_la_progresion(cfg, estado_en_descarga):
    d = decide(cfg, LUNES, sig(LUNES), estado_en_descarga)
    assert d.progression is not None and not d.progression.gate_open
    assert not d.progression.changes


def test_la_semana_anterior_no_es_de_descarga(cfg, estado_en_descarga):
    antes = LUNES - timedelta(weeks=1)
    assert not decide(cfg, antes, sig(antes), estado_en_descarga).deload.active


def test_la_descarga_cubre_los_siete_dias_no_solo_el_que_dispara(cfg, estado_en_descarga):
    d = decide(cfg, LUNES, sig(LUNES), estado_en_descarga)
    st = advance_state(estado_en_descarga, d, executed=None)
    assert st.last_deload_start == LUNES

    for i in range(1, 7):
        dia = LUNES + timedelta(days=i)
        assert decide(cfg, dia, sig(dia), st).deload.active, f"el día +{i} se cayó"


def test_no_se_encadenan_dos_descargas_seguidas(cfg, estado_en_descarga):
    d = decide(cfg, LUNES, sig(LUNES), estado_en_descarga)
    st = advance_state(estado_en_descarga, d, executed=None)
    siguiente = LUNES + timedelta(weeks=1)
    assert not decide(cfg, siguiente, sig(siguiente), st).deload.active


def test_una_descarga_que_arranca_en_rojo_se_retrasa(cfg, estado_en_descarga):
    """Descargar sobre una semana ya frenada no descarga: no hay de qué."""
    d = decide(cfg, LUNES, sig(LUNES, lower_discomfort=7), estado_en_descarga)
    assert d.light == "red"
    assert not d.deload.active
    assert d.deload.shifted


def test_la_descarga_se_programa_cada_siete_semanas_y_siempre_en_lunes(cfg):
    """Un año simulado desde el `program_start` real del YAML.

    `_deload` mide entre INICIOS DE SEMANA, no desde la fecha cruda. Cuando el
    origen era un martes eso hacía que la primera descarga cayera a 6,9 semanas
    de la fecha escrita -correcto, pero raro de leer-. Desde que el validador
    exige que `program.start` sea lunes, el redondeo no mueve nada y la cuenta
    sale EXACTA. Por eso aquí no hay tolerancia: si vuelve a aparecer un 6,9 es
    que alguien ha puesto un origen a media semana y el validador lo dejó pasar.
    """
    inicio = cfg.program_start
    st = EngineState(program_start=inicio)
    arranques: list[date] = []
    dias_en_descarga = 0

    dia = inicio
    for _ in range(370):
        d = decide(cfg, dia, sig(dia), st)
        if d.deload.active:
            dias_en_descarga += 1
            if not arranques or (dia - arranques[-1]).days > 7:
                arranques.append(dia)
        st = advance_state(st, d, executed=None)
        dia += timedelta(days=1)

    assert arranques, "en un año entero no se programó ni una descarga"
    assert all(a.weekday() == 0 for a in arranques), "las descargas empiezan en lunes"

    assert inicio.weekday() == 0, f"program.start {inicio} no es lunes"
    assert (arranques[0] - inicio).days / 7 == 7.0

    separaciones = [
        (b - a).days / 7 for a, b in zip(arranques, arranques[1:])
    ]
    assert all(6 <= s <= 8 for s in separaciones), separaciones
    assert dias_en_descarga == len(arranques) * 7


# ---------------------------------------------------------------------------
# Avance del estado
# ---------------------------------------------------------------------------


def test_las_rachas_son_por_rutina_y_ejercicio(cfg):
    """La plancha lateral aparece en varias rutinas y cada una lleva su racha.

    Con ámbito solo-ejercicio, hacerla bien en `dia_1` haría progresar la de
    `dia_2` sin haberla ejecutado.
    """
    st = EngineState()
    for dia in (date(2026, 9, 7), date(2026, 9, 9)):
        d = decide(cfg, dia, sig(dia), st)
        st = advance_state(st, d, executed={e["key"]: True for e in d.session.exercises})

    planchas = {k: v for k, v in st.clean_sessions.items() if "plancha_lateral" in k[1]}
    assert len(planchas) >= 2
    assert all(isinstance(k, tuple) and len(k) == 2 for k in st.clean_sessions)
    assert st.last_routine_light.get("dia_1") == "green"


def test_una_serie_sin_completar_resetea_la_racha_entera(cfg):
    st = EngineState()
    lunes = date(2026, 9, 7)
    d = decide(cfg, lunes, sig(lunes), st)
    st = advance_state(st, d, executed={e["key"]: True for e in d.session.exercises})

    siguiente = lunes + timedelta(days=7)
    d2 = decide(cfg, siguiente, sig(siguiente), st)
    falla = next(e["key"] for e in d2.session.exercises)
    ejecutado = {e["key"]: True for e in d2.session.exercises}
    ejecutado[falla] = False

    st2 = advance_state(st, d2, executed=ejecutado)
    # La rutina se lee de la decisión y no se escribe a mano: con la rotación,
    # la segunda sesión ya no es el mismo `dia_1` que la primera, y un literal
    # aquí probaría una clave que no existe -y `.get` devolvería None, que no
    # es 0, así que al menos este fallaría en vez de callarse-.
    assert st2.clean_sessions.get((d2.session.routine_key, falla)) == 0


def test_el_estado_no_avanza_si_la_sesion_aun_no_se_ha_ejecutado(cfg):
    """A las siete de la mañana la decisión existe, pero la racha no."""
    st = EngineState()
    d = decide(cfg, date(2026, 9, 7), sig(date(2026, 9, 7)), st)
    despues = advance_state(st, d, executed=None)
    assert despues.clean_sessions == {}
    assert despues.last_routine_light == {}


# ---------------------------------------------------------------------------
# "No lo sé" no es "sí": el cumplimiento de la sesión anterior
# ---------------------------------------------------------------------------
#
# `for_routine` proyectaba la ausencia de registro a `True`, es decir: "no
# tengo ni un apunte de este ejercicio" se convertía en "la última sesión se
# completó entera". La puerta general pide justo ese dato para subir carga, y
# `evaluate_gate` ya sabía tratar el `None` -lo cerraba nombrando el motivo-,
# pero nunca llegaba a verlo. Con una hernia L4-L5 la dirección del fallo
# importa: la puerta se abría, no se cerraba.


def test_un_ejercicio_sin_registro_llega_al_motor_como_no_lo_se(cfg):
    st = EngineState()
    comp, clean = st.for_routine("dia_1", ["prensa_horizontal"])
    assert comp["prensa_horizontal"] is None
    assert clean["prensa_horizontal"] == 0, (
        "la racha sí conserva el cero, y no es incoherente: cero sesiones "
        "limpias es un valor honesto que CIERRA la puerta; un True inventado "
        "la abre"
    )


def test_un_registro_de_verdad_sigue_pasando_tal_cual(cfg):
    st = EngineState(compliance={("dia_1", "prensa_horizontal"): False,
                                 ("dia_1", "gemelo_sentado"): True})
    comp, _ = st.for_routine("dia_1", ["prensa_horizontal", "gemelo_sentado"])
    assert comp["prensa_horizontal"] is False
    assert comp["gemelo_sentado"] is True


def test_el_registro_de_otra_rutina_no_vale_por_esta(cfg):
    """La clave es (rutina, ejercicio). Si no lo fuera, haber cumplido en
    `dia_3` abriría la puerta del `dia_1` sin haberlo entrenado."""
    st = EngineState(compliance={("dia_3", "prensa_horizontal"): True})
    comp, _ = st.for_routine("dia_1", ["prensa_horizontal"])
    assert comp["prensa_horizontal"] is None


def test_en_frio_la_puerta_se_cierra_en_vez_de_abrirse(cfg):
    """Instalación recién estrenada: no hay ni una sesión reconciliada.

    La puerta cerrada es lo importante y no ha cambiado. Lo que cambia es CÓMO
    se cuenta: "no hay registro de la última sesión con el que comparar" es
    verdad y suena a avería -¿se ha perdido algo?, ¿ha fallado la
    reconciliación?- justo el día que estrenas el ciclo, que es cuando más
    veces se va a leer.
    """
    d = decide(cfg, LUNES, sig(LUNES, lower_discomfort=1), EngineState())
    assert d.progression is not None
    assert not d.progression.gate_open
    assert d.progression.estreno is True
    assert "primera vez que el sistema ve el Día 1" in d.progression.gate_reason
    assert "arranca la próxima vez que toque" in d.progression.gate_reason, (
        "hay que decir CUÁNDO empieza a subir, que es la pregunta que deja"
    )
    assert not d.progression.changes, (
        "y no se mueve nada: la puerta gobierna también el volumen"
    )


def test_una_rutina_con_historia_no_se_presenta_como_estrenada(cfg):
    """Con las claves renombradas sigue sin haber registro, pero no es estreno.

    El caso: una rutina con meses de historia a la que se le cambian las claves
    de los ejercicios. La proyección de `for_routine` sale entera a `None` -las
    claves nuevas no tienen registro-, exactamente igual que en una instalación
    recién puesta. La diferencia solo se ve mirando `compliance` ENTERO, que es
    lo que hace `estrenada`.

    Si esto se leyera como estreno, el mensaje prometería que "la progresión
    arranca la próxima vez que toque", la próxima vez no arrancaría -porque el
    registro que falta seguirá faltando- y no habría nada que lo explicara.
    """
    st = EngineState(compliance={("dia_1", "un_ejercicio_que_ya_no_existe"): True})
    d = decide(cfg, LUNES, sig(LUNES, lower_discomfort=1), st)

    assert not d.progression.gate_open
    assert d.progression.estreno is False
    assert "no hay registro" in d.progression.gate_reason
    assert "primera vez" not in d.progression.gate_reason


@pytest.mark.parametrize(
    "molestia, color",
    [
        ({"upper_discomfort": 6}, "amber"),
        ({"lower_discomfort": 7}, "red"),
    ],
)
def test_en_un_dia_sin_verde_la_rutina_virgen_no_se_presenta_como_estreno(
    cfg, molestia, color
):
    """Sin estrenar Y en ámbar: lo que cierra la puerta hoy es el color.

    Las dos condiciones se dan a la vez y solo una es la que manda. Decir aquí
    "primera vez que el sistema ve el Día 1: la progresión arranca la próxima
    vez que toque" sería prometer algo que el color no permite prometer: la
    próxima vez que toque el Día 1 puede volver a salir ámbar, y entonces
    tampoco arrancaría.

    Por eso `estreno` no es `rutina_estrenada is False` a secas sino esa
    condición Y que el motivo que ha cerrado la puerta sea EL del estreno. Este
    test es el que hace falsable esa segunda mitad: si se cae, la puerta sigue
    cerrada igual -no cambia ninguna decisión- y solo cambia el tono del
    mensaje, que es exactamente el tipo de rotura que pasa desapercibida.
    """
    d = decide(cfg, LUNES, sig(LUNES, **molestia), EngineState())

    assert d.light == color
    assert d.progression is not None and not d.progression.gate_open
    assert d.progression.estreno is False, (
        "un día sin verde en una rutina nueva se cierra por el semáforo, y el "
        "mensaje estaría prometiendo un arranque que el color no garantiza"
    )
    assert "el semáforo está en" in d.progression.gate_reason
    assert "primera vez" not in d.progression.gate_reason


def test_un_ejercicio_nuevo_en_una_rutina_en_marcha_frena_a_toda_la_rutina(cfg):
    """El caso de en medio, que es el que estaba peor.

    Ocho ejercicios con registro y uno estrenado hoy. `all(known)` devolvía
    True: la puerta se abría con la evidencia que había e ignoraba la que
    faltaba. Progresar con la evidencia elegida es progresar a ciegas.
    """
    keys = [e["key"] for e in cfg.raw["routines"]["dia_1"]["exercises"]]
    nuevo = keys[-1]
    st = EngineState(
        compliance={("dia_1", k): True for k in keys if k != nuevo},
        clean_sessions={("dia_1", k): 5 for k in keys},
    )
    d = decide(cfg, LUNES, sig(LUNES, lower_discomfort=1), st)

    assert not d.progression.gate_open
    assert nuevo in d.progression.gate_reason, "hay que decir CUÁL falta"
    assert not d.progression.changes


def test_con_todos_registrados_y_cumplidos_la_puerta_se_abre(cfg):
    """El contraste. Si esto no pasara, el arreglo habría congelado el motor
    entero en vez de tapar un agujero."""
    keys = [e["key"] for e in cfg.raw["routines"]["dia_1"]["exercises"]]
    st = EngineState(
        compliance={("dia_1", k): True for k in keys},
        clean_sessions={("dia_1", k): 5 for k in keys},
    )
    d = decide(cfg, LUNES, sig(LUNES, lower_discomfort=1), st)

    assert d.progression.gate_open, d.progression.gate_reason
    assert d.progression.changes, "con todo en regla algo tiene que subir"


def test_una_sola_sesion_reconciliada_desbloquea_la_rutina_entera(cfg):
    """La otra mitad del arreglo, y la que impide que sea un cierre permanente.

    Cerrar la puerta ante la falta de registro solo es aceptable si el registro
    se consigue. `advance_state` recorre TODOS los ejercicios de la sesión y a
    los que no aparecen en `executed` les pone False, no los deja en None: tras
    una sesión reconciliada no queda ni un `None` en la rutina.

    Si algún día un ejercicio del config dejara de llegar a la sesión, esa
    clave se quedaría en None para siempre y la rutina no volvería a progresar.
    Este test es la alarma de eso.
    """
    st = EngineState()
    d = decide(cfg, LUNES, sig(LUNES, lower_discomfort=1), st)
    st = advance_state(st, d, executed={e["key"]: True for e in d.session.exercises})

    keys_cfg = [e["key"] for e in cfg.raw["routines"][d.session.routine_key]["exercises"]]
    comp, _ = st.for_routine(d.session.routine_key, keys_cfg)
    sin_registro = [k for k, v in comp.items() if v is None]
    assert not sin_registro, (
        f"estos ejercicios están en config.yaml pero no llegan a la sesión, "
        f"así que no se reconcilian nunca y congelan la rutina: {sin_registro}"
    )


def test_un_incumplimiento_confirmado_manda_sobre_los_que_faltan(cfg):
    """Si lo confirmado ya decide, lo que falta da igual.

    Es el criterio de `weekend_summary`. Un False confirmado cierra la puerta
    por incumplimiento, no por falta de registro: el motivo tiene que decir la
    verdad, porque es lo que se lee en el móvil.
    """
    keys = [e["key"] for e in cfg.raw["routines"]["dia_1"]["exercises"]]
    st = EngineState(
        compliance={("dia_1", keys[0]): False, ("dia_1", keys[1]): None},
        clean_sessions={("dia_1", k): 5 for k in keys},
    )
    d = decide(cfg, LUNES, sig(LUNES, lower_discomfort=1), st)

    assert not d.progression.gate_open
    assert "no se completaron" in d.progression.gate_reason
    assert "no hay registro" not in d.progression.gate_reason


# ---------------------------------------------------------------------------
# Serialización
# ---------------------------------------------------------------------------


def test_la_decision_se_puede_guardar_como_json(cfg):
    d = decide(cfg, LUNES, sig(LUNES), EngineState())
    recargada = json.loads(json.dumps(d.to_dict(), ensure_ascii=False))
    assert recargada["light"] == "green"
    assert "inputs" in recargada, "sin la fotografía de señales no se puede auditar"


def test_el_estreno_viaja_en_el_json_y_no_solo_en_la_prosa(cfg):
    """El motivo del estreno se guarda DOS veces, y es a propósito.

    En `gate_reason` va la frase, que es lo que se lee. En `estreno` va el
    booleano, que es lo que se consulta. Dentro de tres meses, "¿cuántas de las
    puertas cerradas de septiembre fueron estrenos y cuántas averías?" se
    contesta con un filtro sobre el JSON; con solo la prosa habría que buscar
    subcadenas contra un texto que para entonces puede estar reescrito, que es
    exactamente la fragilidad que el booleano existe para evitar.
    """
    d = decide(cfg, LUNES, sig(LUNES, lower_discomfort=1), EngineState())
    recargada = json.loads(json.dumps(d.to_dict(), ensure_ascii=False))
    assert recargada["progression"]["estreno"] is True

    con_historia = EngineState(compliance={("dia_1", "lo_que_sea"): True})
    d2 = decide(cfg, LUNES, sig(LUNES, lower_discomfort=1), con_historia)
    assert json.loads(json.dumps(d2.to_dict()))["progression"]["estreno"] is False


def test_la_respuesta_del_dia_viaja_en_el_json_con_sus_tres_estados(cfg):
    """El volcado tiene que distinguir «no voy» de «no has contestado».

    No hay columna `decisions.va_a_entrenar` a propósito -la respuesta ya vive en
    `checkins.will_train`-, así que este JSON es lo que lee la ficha y lo que lee
    la simulación. Con el campo perdido en `to_dict`, el mensaje del día seguiría
    saliendo perfecto y el día siguiente nadie podría contestar por qué el 17 de
    septiembre no se prescribió nada: los dos extremos bien y el cable suelto en
    medio, que es como se pierden las cosas sin que nada reviente.

    Los tres estados se comprueban con `is`, no con `==`: `False == 0` y
    `None == False` es falso pero `not None` es cierto, y este campo existe
    justamente para que esa diferencia sobreviva hasta el archivo.
    """
    for respuesta in (True, False, None):
        d = decide(cfg, LUNES, sig(LUNES, will_train=respuesta), EngineState())
        recargada = json.loads(json.dumps(d.to_dict(), ensure_ascii=False))
        assert "va_a_entrenar" in recargada, "el volcado no cuenta lo que se contestó"
        assert recargada["va_a_entrenar"] is respuesta

        # Y la fotografía de señales la lleva también, que es de donde salen las
        # correlaciones y la tabla 2x2 de la ficha. Las dos copias no sobran: el
        # campo de arriba dice qué hizo el motor con la respuesta, y el de aquí
        # dentro dice qué se leyó, aunque algún día el motor deje de mirarla.
        assert recargada["inputs"]["values"]["will_train"] is respuesta
