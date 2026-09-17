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

from tests.conftest import LUNES, eligiendo, sig, sig_completa

JUEVES = LUNES + timedelta(days=3)


def _series(s) -> int:
    """Cuántas series lleva la sesión en total, calentamiento incluido.

    El ámbar recorta series, no ejercicios: le quita una efectiva a cada uno y
    deja la lista de ejercicios igual de larga. Medir el recorte contando
    ejercicios da el mismo número antes y después y no distingue una sesión
    recortada de una intacta.
    """
    return sum(len(e["sets"]) for e in s.exercises)


def _nombres(s) -> set[str]:
    """Los ejercicios de la sesión, por su nombre visible."""
    return {e["name"] for e in s.exercises}


def _tocados(s) -> set[str]:
    """Los ejercicios que el semáforo ha modificado, sacados de `changes`.

    `changes` es la lista de lo que se le ha hecho a la rutina base -«Jalón al
    pecho: 3->2 series efectivas»- y aquí solo interesa el nombre de delante.
    Sirve para comprobar QUÉ rutina se ha recortado y no solo cuánto: los
    nombres son de un día o del otro, y no hay forma de confundirlos.
    """
    return {c.split(":")[0] for c in s.changes}


# ---------------------------------------------------------------------------
# Semáforo -> sesión
# ---------------------------------------------------------------------------


def test_un_dia_sin_señales_malas_es_verde_y_entrena_entero(cfg):
    d = decide(cfg, LUNES, sig_completa(LUNES), EngineState())
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
        d = decide(cfg, dia, sig_completa(dia), st)
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
# El selector: lo que propone el ciclo y lo que se elige
# ---------------------------------------------------------------------------
#
# El caso que lo motivó: tocaba el Día 1, las piernas llegan cansadas y se
# prefiere el Día 2. Antes eso no se podía decir, y lo que quedaba escrito en
# Hevy esa mañana era el Día 1. Quien luego hacía el Día 2 lo hacía con los pesos
# que esa rutina tuviera guardados de la última vez, sin los ajustes del día.
#
# Elegir cambia HOY y no mañana. La rotación la sigue gobernando `workout_log`:
# desviarse no adelanta el ciclo, y el día que se salta vuelve a tocar al final
# de la vuelta sin que nadie lo anote en ninguna parte.


def test_elegir_otro_dia_del_ciclo_planifica_ese_dia(cfg):
    """Y lo planifica entero: la sesión, la progresión y lo que se escribirá."""
    d = decide(cfg, LUNES, eligiendo(sig(LUNES), "dia_2"), EngineState())

    assert d.rotation_routine == "dia_2"
    assert d.session.routine_key == "dia_2"
    assert d.progression is not None and d.progression.routine_key == "dia_2"

    # Y la propuesta original no se pierde por el camino. Sin ella, dentro de
    # tres meses no habría forma de saber que ese día hubo un desvío.
    assert d.propuesta == "dia_1"
    assert d.sesion_elegida == "dia_2"


def test_lo_elegido_se_lleva_los_ajustes_de_hoy_y_no_los_de_su_ultima_vez(cfg):
    """El motivo entero de que el selector escriba en Hevy.

    Un ámbar recorta la sesión del día. Si elegir el Día 2 dejase la rutina
    intacta -«total, la que el sistema había decidido era la otra»- se acabaría
    levantando en un día de molestias lo que estaba escrito para un día bueno.
    La elección no es un cambio de tema: es la misma mañana aplicada a otra
    rutina.
    """
    ambar = decide(
        cfg, LUNES, eligiendo(sig(LUNES, upper_discomfort=6), "dia_2"), EngineState()
    )
    assert ambar.light == "amber"
    assert ambar.session.routine_key == "dia_2"
    assert ambar.session.kind == "reduced", (
        "se ha planificado el Día 2 con la sesión de un día verde"
    )

    # El recorte se mide en series y no en ejercicios: el ámbar del Día 2 le
    # quita una serie efectiva a cada uno sin retirar ninguno. Contar
    # ejercicios da nueve en los dos casos y el test pasaría con la sesión sin
    # tocar.
    verde = decide(cfg, LUNES, eligiendo(sig_completa(LUNES), "dia_2"), EngineState())
    assert _series(ambar.session) < _series(verde.session)

    # Y lo recortado son los ejercicios del Día 2, uno por uno. Si el semáforo
    # se hubiera aplicado a la rutina propuesta y solo después se le hubiera
    # cambiado la etiqueta, aquí aparecerían los nombres del Día 1.
    assert _tocados(ambar.session) == _nombres(verde.session)


def test_elegir_lo_que_ya_tocaba_es_el_dia_de_siempre(cfg):
    """El caso normal, que es el 90% de las mañanas: el selector viene
    preseleccionado con la propuesta y no se toca.

    Se comparan las decisiones enteras y no la rutina, porque lo que hay que
    descartar es que el camino nuevo haga algo de más en cualquier otro sitio.
    """
    callado = decide(cfg, LUNES, sig(LUNES), EngineState()).to_dict()
    elegido = decide(
        cfg, LUNES, eligiendo(sig(LUNES), "dia_1"), EngineState()
    ).to_dict()

    # Lo declarado sí cambia, claro: en uno se contestó y en el otro no.
    assert elegido.pop("sesion_elegida") == "dia_1"
    assert callado.pop("sesion_elegida") is None
    # Y el `inputs` lleva dentro el snapshot de las señales, que difieren por lo
    # mismo. Todo lo demás tiene que ser idéntico.
    elegido.pop("inputs"), callado.pop("inputs")

    assert elegido == callado


@pytest.mark.parametrize(
    "eleccion, trozo",
    [("bici", "bici"), ("otro", "otra cosa")],
)
def test_bici_y_otro_no_prescriben_fuerza_pero_dejan_la_rutina_puesta(
    cfg, eleccion, trozo
):
    """Las dos mitades del mismo día, y la segunda es la que se olvida.

    No prescribir es lo que se pidió: si hoy sales en bici, el mensaje no te
    anuncia subidas de peso. Escribir la rutina igual también, y por el mismo
    argumento que el «hoy no voy a entrenar»: la respuesta de las siete de la
    mañana es una intención, y si a las siete de la tarde se cambia de idea, lo
    que tiene que haber en Hevy es la sesión de HOY y no la de hace dos semanas.
    """
    d = decide(cfg, LUNES, eligiendo(sig_completa(LUNES), eleccion), EngineState())

    assert d.progression is not None
    assert not d.progression.gate_open
    assert trozo in d.progression.gate_reason
    assert not d.progression.changes, "se ha anunciado una subida en un día sin fuerza"

    # Y la rutina del ciclo sigue planificada entera, con sus ejercicios y sus
    # series: es lo que `runner._escribir_hevy` va a subir a la aplicación.
    assert d.rotation_routine == "dia_1"
    assert d.session.routine_key == "dia_1"
    assert d.session.exercises, "no hay nada que escribir en Hevy"
    assert d.session.kind == "full"


def test_el_motivo_de_la_puerta_no_regana(cfg):
    """Mismo listón que el «hoy no voy»: informa de lo que pasa y de cuándo se
    retoma, sin calificar la decisión ni insinuar que se pierde algo."""
    for eleccion in ("bici", "otro"):
        motivo = decide(
            cfg, LUNES, eligiendo(sig(LUNES), eleccion), EngineState()
        ).progression.gate_reason
        assert "próxima vez" in motivo
        for reproche in ("deberías", "perdido", "pierdes", "incumpl", "fallo", "solo"):
            assert reproche not in motivo.lower(), f"«{motivo}» suena a reproche"


def test_elegir_no_adelanta_la_rotacion_ni_aunque_sea_otro_dia(cfg):
    """Lo de hoy es de hoy. Mañana sigue mandando lo que se ejecutó.

    Es la trampa que este diseño evita: si declarar el Día 2 moviera el puntero,
    tres mañanas seguidas tocando el selector y sin pisar el gimnasio darían una
    vuelta entera al ciclo sin haber levantado nada.
    """
    st = EngineState()
    d = decide(cfg, LUNES, eligiendo(sig(LUNES), "dia_2"), st)
    despues = advance_state(st, d, executed=None)

    assert despues.last_strength is None
    martes = LUNES + timedelta(days=1)
    assert decide(cfg, martes, sig(martes), despues).propuesta == "dia_1", (
        "declarar el Día 2 ha adelantado el ciclo sin que se entrenara"
    )


def test_hacer_el_dia_elegido_si_mueve_el_puntero_a_ese(cfg):
    """Y entonces el Día 1 se queda para el final de la vuelta.

    Que es lo acordado: no se reordena nada ni se recupera nada. El ciclo da la
    vuelta y el día saltado vuelve a tocar cuando le toca.
    """
    st = EngineState()
    d = decide(cfg, LUNES, eligiendo(sig(LUNES), "dia_2"), st)
    despues = advance_state(
        st, d, executed={e["key"]: True for e in d.session.exercises}
    )
    assert despues.last_strength == ("dia_2", LUNES)

    martes = LUNES + timedelta(days=1)
    assert decide(cfg, martes, sig(martes), despues).rotation_routine == "dia_3"


def test_un_dia_de_bici_no_toca_el_estado_mas_que_un_dia_callado(cfg):
    """La misma exigencia que se le puso al «hoy no voy», y por el mismo motivo:
    si declarar la bici costara algo -perder el turno, romper una racha, sumar a
    un contador- el selector se dejaría sin tocar y el sistema volvería a no
    saber qué se hizo los días que no hubo fuerza."""
    st = EngineState(last_strength=("dia_1", LUNES))
    martes = LUNES + timedelta(days=1)

    callado = advance_state(st, decide(cfg, martes, sig(martes), st), executed=None)
    en_bici = advance_state(
        st, decide(cfg, martes, eligiendo(sig(martes), "bici"), st), executed=None
    )

    assert en_bici.__dict__ == callado.__dict__


def test_una_eleccion_de_una_rutina_que_no_existe_se_ignora_sin_romper_el_dia(cfg):
    """`upsert_checkin` no deja guardar esto, pero el motor no se apoya en eso.

    El archivo viene de antes de que el selector existiera, el ciclo del config
    se puede editar, y un `dia_4` guardado ayer con un ciclo de cuatro se lee hoy
    con uno de tres. Que el día siga decidiéndose con la propuesta es lo que hace
    que ninguno de esos tres casos deje una mañana sin rutina.
    """
    d = decide(cfg, LUNES, eligiendo(sig(LUNES), "dia_9"), EngineState())
    assert d.rotation_routine == "dia_1"
    assert d.session.exercises
    # Cae del lado de «hoy no hay fuerza que prescribir», que es lo único que se
    # puede afirmar de una elección que no nombra ninguna rutina del ciclo.
    assert not d.progression.gate_open
    assert "dia_9" in d.progression.gate_reason


def test_lo_propuesto_y_lo_elegido_quedan_los_dos_en_la_decision_guardada(cfg):
    """Es lo que permitirá contestar «¿me salto el Día 1 a menudo?».

    De `workout_log` sale lo que se hizo. Lo que se iba a hacer no sale de
    ninguna parte si no se escribe aquí, y una pregunta sobre uno mismo que
    depende de un dato que nadie guardó no se puede contestar más tarde: hay que
    empezar a guardarlo antes de necesitarlo.
    """
    d = decide(cfg, LUNES, eligiendo(sig(LUNES), "dia_2"), EngineState())
    guardada = json.loads(json.dumps(d.to_dict(), default=str))

    assert guardada["propuesta"] == "dia_1"
    assert guardada["sesion_elegida"] == "dia_2"
    assert guardada["rotation_routine"] == "dia_2"


def test_no_contestar_el_selector_deja_los_tres_campos_coherentes(cfg):
    """El histórico entero anterior a hoy, y la mayoría de los días de mañana."""
    d = decide(cfg, LUNES, sig(LUNES), EngineState())
    assert d.sesion_elegida is None
    assert d.propuesta == d.rotation_routine == "dia_1"
    assert d.progression.gate_reason != ""


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


# ---------------------------------------------------------------------------
# La descarga que solo descargaba la mitad
# ---------------------------------------------------------------------------
#
# La fuerza bajaba al 60% de carga y al 70% de series, y el bloque HIIT entraba
# ENTERO: quince series de 30 s más el wall ball a peso completo, la sesión más
# dura de la semana, justo en la semana que existe para descargar. Una descarga
# a la que se le deja fuera la parte más glucolítica no es una descarga: es una
# semana normal con menos peso en las barras.
#
# Y la batería pasaba entera, en las dos direcciones: ningún test miraba el
# bloque HIIT en semana de descarga ni para comprobar que se recortaba ni para
# comprobar que no. Por eso estos tests están aquí y no en el módulo del
# constructor: lo que hay que vigilar es el DÍA -la fuerza y el HIIT juntos-,
# que es donde la asimetría existe y donde se puede volver a abrir.
#
# El calendario manda sobre el color y no al revés. La descarga es de
# calendario: un verde en semana de descarga es un día perfectamente normal, y
# es exactamente el día en que esto pasaba, porque el bloque solo entra en
# verde. El ámbar y las reglas especiales sí van atadas al color y por eso no
# llegan nunca al bloque.


def _dia_de_descarga(cfg, estado_en_descarga):
    """El lunes de descarga con el día completo y verde, que es cuando pasa.

    `sig` a secas es ámbar -deja trece reglas sin evaluar- y en ámbar el bloque
    HIIT no entra: el escenario en el que esto se rompía no se puede montar sin
    `sig_completa`.
    """
    d = decide(cfg, LUNES, sig_completa(LUNES), estado_en_descarga)
    assert d.deload.active and d.light == "green", (
        f"el escenario ya no es 'verde en semana de descarga' "
        f"({d.light}, descarga={d.deload.active}); el test hay que rehacerlo"
    )
    assert d.session.hiit is not None, (
        f"el escenario ya no lleva bloque HIIT: {d.session.notes}"
    )
    return d


def _efectivas(sesion, clave: str) -> list[dict]:
    ex = next(e for e in sesion.exercises if e["key"] == clave)
    return [s for s in ex["sets"] if s.get("type") != "warmup"]


def test_en_semana_de_descarga_el_hiit_tambien_se_recorta(cfg, estado_en_descarga):
    """Lo de arriba, medido: series, segundos y kilos del bloque.

    Los números no se copian del config a mano. Se leen de `progression.deload`
    y de `special_rules`, se aplican a lo que el YAML prescribe y se comparan
    con lo que sale, porque un test que repita el 0,7 escrito a mano deja de
    vigilar el día que alguien cambie el factor: seguiría verde midiendo contra
    su propia copia.
    """
    dl = cfg.raw["progression"]["deload"]
    factor_carga = next(
        r for r in cfg.raw["special_rules"] if r["name"] == "semana_de_descarga"
    )["action"]["load_factor"]

    d = _dia_de_descarga(cfg, estado_en_descarga)
    hiit = d.session.hiit
    prescrito = {
        e["key"]: e["sets"] for e in cfg.raw["routines"][hiit.routine_key]["exercises"]
    }

    for ex in hiit.exercises:
        base = prescrito[ex["key"]]
        salieron = _efectivas(hiit, ex["key"])

        assert len(salieron) == max(2, int(len(base) * dl["sets_factor"])), (
            f"{ex['key']}: {len(base)} series prescritas y {len(salieron)} "
            f"en la sesión; con sets_factor={dl['sets_factor']} no cuadra"
        )
        for s, b in zip(salieron, base):
            if "duration_s" in b:
                assert s["duration_s"] == max(5, int(b["duration_s"] * dl["seconds_factor"]))
            if "reps" in b:
                assert s["reps"] == max(1, int(b["reps"] * dl["reps_factor"]))
            if b.get("weight_kg"):
                assert s["weight_kg"] == pytest.approx(b["weight_kg"] * factor_carga)


def test_la_descarga_recorta_el_hiit_con_el_mismo_criterio_que_la_fuerza(
    cfg, estado_en_descarga
):
    """"Si la fuerza baja y el HIIT no, la descarga no es una descarga."

    El test anterior mide el bloque contra el config; este mide el bloque contra
    LA FUERZA DEL MISMO DÍA, que es la comparación que define la avería. Los dos
    hacen falta: con solo el primero, cambiar el criterio de la fuerza y
    olvidarse del HIIT volvería a abrir la asimetría sin que nada la viera.

    Se compara la proporción y no los valores: son ejercicios distintos, con
    duraciones y pesos distintos. Lo que tiene que coincidir es cuánto se
    recorta, no cuánto queda.
    """
    d = _dia_de_descarga(cfg, estado_en_descarga)
    routines = cfg.raw["routines"]

    def proporcion_de_series(sesion) -> float:
        prescrito = {
            e["key"]: e["sets"] for e in routines[sesion.routine_key]["exercises"]
        }
        base = sum(
            len([s for s in v if s.get("type") != "warmup"]) for v in prescrito.values()
        )
        return sum(len(_efectivas(sesion, e["key"])) for e in sesion.exercises) / base

    fuerza = proporcion_de_series(d.session)
    hiit = proporcion_de_series(d.session.hiit)
    assert hiit < 1.0, "el bloque HIIT ha entrado entero en semana de descarga"
    assert hiit == pytest.approx(fuerza, abs=0.12), (
        f"la fuerza se queda en el {fuerza:.0%} de sus series y el HIIT en el "
        f"{hiit:.0%}. El mismo día no puede descargarse a dos ritmos"
    )


def test_el_recorte_del_hiit_queda_escrito_y_no_solo_hecho(cfg, estado_en_descarga):
    """La sesión de fuerza anota su recorte y la del HIIT no anotaba nada.

    Sin la línea, la única forma de saber que el bloque se ha recortado es
    contar las series en la app y acordarse de cuántas había. "Déjalo escrito"
    es esto: el porqué viaja con el plan del día, no en la cabeza.
    """
    d = _dia_de_descarga(cfg, estado_en_descarga)
    linea = next(
        (c for c in d.session.hiit.changes if "descarga" in c.lower()), None
    )
    assert linea is not None, (
        f"el bloque se recorta sin decirlo: {d.session.hiit.changes}"
    )
    assert "HIIT" in linea, linea
    assert any("descarga" in c.lower() for c in d.session.changes), (
        "y la fuerza tiene que seguir anotando el suyo"
    )


def test_el_recorte_de_la_descarga_no_se_adopta_como_carga_vigente(
    cfg, estado_en_descarga
):
    """El recorte es de un día; el objetivo vigente es lo prescrito.

    `target_sets` se fija DESPUÉS de la progresión y ANTES del recorte. Al revés
    la plancha bajaría de 30 s a 24 s, esos 24 s se guardarían como carga
    vigente y la semana siguiente partiría de ahí: la descarga se habría
    convertido en un retroceso permanente, y encima progresando desde el número
    bajo parecería que sube. Es el mismo error que ya estaba resuelto en la
    fuerza -pasos 2b y 3 de `build_session`- y que el bloque HIIT no heredaba.

    Se comprueba en la base, no en el objeto: lo que sobrevive al día es
    `EngineState.current_sets`, y es lo que leerá `con_carga_vigente` mañana.
    """
    d = _dia_de_descarga(cfg, estado_en_descarga)
    hiit = d.session.hiit
    prescrito = {
        e["key"]: e["sets"] for e in cfg.raw["routines"][hiit.routine_key]["exercises"]
    }

    recortada = _efectivas(hiit, "plancha_frontal")
    assert recortada[0]["duration_s"] < prescrito["plancha_frontal"][0]["duration_s"], (
        "sin recorte este test no prueba nada"
    )

    st = advance_state(estado_en_descarga, d, executed=None)
    for clave, base in prescrito.items():
        guardado = st.current_sets[(hiit.routine_key, clave)]
        assert guardado == [s for s in base if s.get("type") != "warmup"], (
            f"{clave}: la descarga se ha quedado como objetivo vigente. "
            f"Prescrito {base}, guardado {guardado}"
        )


def test_fuera_de_la_semana_de_descarga_el_bloque_hiit_entra_entero(cfg):
    """La contraguarda: el recorte tiene que ser de la descarga y de nada más.

    Sin este test, un `deload_active` que se quedara pegado a `True` -o un
    recorte aplicado siempre- pasaría por bueno: los tests de arriba solo miran
    la semana en la que SÍ toca, y verían exactamente lo mismo.
    """
    normal = EngineState(program_start=LUNES - timedelta(weeks=1))
    d = decide(cfg, LUNES, sig_completa(LUNES), normal)
    assert not d.deload.active and d.light == "green"
    assert d.session.hiit is not None, f"el escenario no lleva HIIT: {d.session.notes}"

    prescrito = {
        e["key"]: e["sets"]
        for e in cfg.raw["routines"][d.session.hiit.routine_key]["exercises"]
    }
    for ex in d.session.hiit.exercises:
        assert ex["sets"] == prescrito[ex["key"]], (
            f"{ex['key']} sale recortado en una semana que no es de descarga"
        )
    assert not any("descarga" in c.lower() for c in d.session.hiit.changes)


# ---------------------------------------------------------------------------
# El aplazamiento de la descarga, que ni aplazaba ni se acordaba
#
# Devolvía `shifted=True` y no lo apuntaba en ningún sitio, así que el efecto
# duraba un día: el martes la misma semana seguía siendo múltiplo de
# `every_n_weeks` y la descarga entraba igual. Y si esa semana se acababa sin
# concederla, la siguiente ocasión era SIETE semanas después.
# ---------------------------------------------------------------------------


def _aplaza(cfg, estado, dia):
    """Un lunes de descarga que arranca en rojo, y el estado que deja."""
    d = decide(cfg, dia, sig(dia, lower_discomfort=7), estado)
    assert d.light == "red" and not d.deload.active and d.deload.shifted
    return advance_state(estado, d, executed=None)


def test_la_semana_aplazada_no_se_cuela_el_martes(cfg, estado_en_descarga):
    """El aplazamiento duraba veinticuatro horas y el mensaje decía una semana.

    El lunes se retrasaba; el martes el calendario no había cambiado -la semana
    seguía siendo la séptima- y la descarga entraba con `start` el lunes. El
    martes ni siquiera hacía falta que fuese verde: la comprobación del rojo
    solo miraba los lunes.
    """
    st = _aplaza(cfg, estado_en_descarga, LUNES)
    assert st.deload_aplazada_desde == LUNES

    martes = LUNES + timedelta(days=1)
    d = decide(cfg, martes, sig(martes), st)
    assert not d.deload.active, "la descarga aplazada se ha colado un día después"
    assert d.deload.shifted


def test_la_descarga_aplazada_entra_a_la_semana_siguiente(cfg, estado_en_descarga):
    """El retraso que anuncia el mensaje: una semana, de verdad."""
    st = _aplaza(cfg, estado_en_descarga, LUNES)

    siguiente = LUNES + timedelta(weeks=1)
    d = decide(cfg, siguiente, sig(siguiente), st)
    assert d.deload.active
    assert d.deload.start == siguiente
    assert not d.deload.shifted


def test_una_descarga_aplazada_no_se_pierde_siete_semanas(cfg, estado_en_descarga):
    """Lo que pasaba si la semana aplazada se acababa sin conceder la descarga.

    La siguiente ocasión era el siguiente múltiplo de `every_n_weeks`, siete
    semanas más tarde. Aplazar no retrasaba la descarga: la borraba.
    """
    st = _aplaza(cfg, estado_en_descarga, LUNES)

    cuando = [
        LUNES + timedelta(days=i)
        for i in range(1, 15)
        if decide(cfg, LUNES + timedelta(days=i), sig(LUNES + timedelta(days=i)), st)
        .deload.active
    ]
    assert cuando, "la descarga aplazada no vuelve: se ha perdido el turno entero"
    assert cuando[0] == LUNES + timedelta(weeks=1)


def test_la_deuda_sobrevive_a_que_el_calendario_deje_de_nombrarla(cfg, estado_en_descarga):
    """La semana siguiente ya NO es múltiplo de siete, y da igual.

    Es la mitad que faltaba: sin deuda, la rama que mira el calendario contesta
    «no toca esta semana» y no hay nada que la contradiga.
    """
    st = _aplaza(cfg, estado_en_descarga, LUNES)
    siguiente = LUNES + timedelta(weeks=1)

    sin_deuda = EngineState(program_start=estado_en_descarga.program_start)
    assert not decide(cfg, siguiente, sig(siguiente), sin_deuda).deload.active
    assert decide(cfg, siguiente, sig(siguiente), st).deload.active


def test_agotado_el_margen_la_descarga_entra_aunque_siga_en_rojo(cfg, estado_en_descarga):
    """`jitter_weeks` es un presupuesto, no una excusa indefinida.

    Con una hernia L4-L5 la descarga es lo último que puede saltarse, y una
    racha de semanas rojas es justo cuando más falta hace. Un margen infinito
    convertiría «se retrasa» en «no se hace nunca» para quien peor está.
    """
    st = _aplaza(cfg, estado_en_descarga, LUNES)

    siguiente = LUNES + timedelta(weeks=1)
    d = decide(cfg, siguiente, sig(siguiente, lower_discomfort=7), st)
    assert d.light == "red"
    assert d.deload.active, "la descarga se ha quedado sin hacer por seguir en rojo"


def test_conceder_la_descarga_cancela_la_deuda(cfg, estado_en_descarga):
    """Si no, una descarga ya hecha seguiría constando debida para siempre y
    se repetiría en cuanto pasara la semana."""
    st = _aplaza(cfg, estado_en_descarga, LUNES)

    siguiente = LUNES + timedelta(weeks=1)
    d = decide(cfg, siguiente, sig(siguiente), st)
    st = advance_state(st, d, executed=None)
    assert st.deload_aplazada_desde is None
    assert st.last_deload_start == siguiente


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

    # `assert arranques` no basta para lo de abajo: con UNA sola descarga en el
    # año, `zip(arranques, arranques[1:])` sale vacío, `all([])` es cierto y la
    # cadencia -que es lo que este test existe para medir- no se comprueba. Y
    # una sola descarga al año es exactamente la forma que tendría la avería.
    assert len(arranques) >= 2, (
        f"solo {len(arranques)} descarga(s) en un año: la cadencia de abajo no "
        "tiene dos arranques que comparar y aprueba sin medir nada"
    )
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
        d = decide(cfg, dia, sig_completa(dia), st)
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
    d = decide(cfg, LUNES, sig_completa(LUNES), EngineState())
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
    d = decide(cfg, LUNES, sig_completa(LUNES), st)

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
    d = decide(cfg, LUNES, sig_completa(LUNES), st)

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
    d = decide(cfg, LUNES, sig_completa(LUNES), st)

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
    d = decide(cfg, LUNES, sig_completa(LUNES), st)
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
    d = decide(cfg, LUNES, sig_completa(LUNES), st)

    assert not d.progression.gate_open
    assert "no se completaron" in d.progression.gate_reason
    assert "no hay registro" not in d.progression.gate_reason


# ---------------------------------------------------------------------------
# Serialización
# ---------------------------------------------------------------------------


def test_la_decision_se_puede_guardar_como_json(cfg):
    d = decide(cfg, LUNES, sig_completa(LUNES), EngineState())
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
    d = decide(cfg, LUNES, sig_completa(LUNES), EngineState())
    recargada = json.loads(json.dumps(d.to_dict(), ensure_ascii=False))
    assert recargada["progression"]["estreno"] is True

    con_historia = EngineState(compliance={("dia_1", "lo_que_sea"): True})
    d2 = decide(cfg, LUNES, sig_completa(LUNES), con_historia)
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
