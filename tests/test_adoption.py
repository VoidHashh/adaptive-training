"""Adoptar la carga ejecutada: la asimetría, el ritmo y el tope.

Lo que se prueba aquí no es "que el número cambie". Es que cambie por el motivo
correcto y, sobre todo, que NO cambie por los motivos equivocados: una semana de
descarga bien hecha no puede bajar el objetivo, una serie cortada a 70 kg no
puede subirlo, y una errata de tecleo en Hevy no puede acabar siendo la rutina
de mañana.

El sesgo de toda la batería es hacia el lado caro. Que una subida legítima se
pierda cuesta una semana de progreso; que una bajada silenciosa se cuele cuesta
una espalda con hernia L4-L5 cargando por encima de lo que se está levantando.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.engine.adoption import (
    ABAJO,
    ARRIBA,
    Adopcion,
    _desplazar,
    _margen,
    adoptar_cargas,
    tope_efectivo,
)
from tests.dobles import doble_de
from app.engine.decision import EngineState

RUTINA = "dia_1"
CLAVE = (RUTINA, "hip_thrust")

# El de `config.yaml`, copiado a mano a propósito: si allí cambian los números,
# los tests de aquí siguen probando la regla que describen y no se adaptan solos
# a un valor nuevo sin que nadie lo lea.
PROG = {
    "adopt_executed_load": {
        "enabled": True,
        "down_after_sessions": 3,
        "max_jump_kg": 5,
        "max_jump_pct": 0.20,
    }
}
SETS_CFG = {"source": "api"}


# ---------------------------------------------------------------------------
# Dobles mínimos
# ---------------------------------------------------------------------------


@doble_de(EngineState)
@dataclass
class EstadoFalso:
    """Solo los tres diccionarios que `adoptar_cargas` toca.

    Un `EngineState` de verdad arrastraría reglas activas, aplazadas y
    calendario, y un test que falla por cualquiera de esos no está probando la
    adopción.
    """

    current_sets: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    below_plan_streak: dict[tuple[str, str], int] = field(default_factory=dict)
    below_plan_best_kg: dict[tuple[str, str], float] = field(default_factory=dict)


def series(*pesos: float, reps: int = 10) -> list[dict[str, Any]]:
    return [{"type": "normal", "reps": reps, "weight_kg": p} for p in pesos]


def ejercicio(*pesos: float, key: str = "hip_thrust", reps: int = 10) -> dict[str, Any]:
    return {"key": key, "name": "Hip thrust", "sets": series(*pesos, reps=reps)}


def adoptar(
    estado: EstadoFalso,
    plan: list[dict[str, Any]],
    pesos: dict[str, float | None],
    limpio: dict[str, bool] | None = None,
    prog: dict[str, Any] | None = None,
    motivos: dict[str, str] | None = None,
    limpio_arriba: dict[str, bool] | None = None,
    motivos_arriba: dict[str, str] | None = None,
):
    """Los dos veredictos, y por defecto `arriba` COPIA al estricto.

    Copiarlo no es pereza: mantiene el significado de los tests escritos antes
    del 25/09/2026, que decían «esta sesión no fue limpia» sin distinguir por
    qué. Si el defecto fuera «siempre limpio para subir», esos tests pasarían a
    probar otra cosa en silencio, que es la forma de que una batería entera
    quede certificando lo que ya no hace.

    Los tests que SÍ distinguen -una serie más ligera frente a una serie corta
    de reps- pasan los dos a mano.
    """
    estricto = limpio if limpio is not None else {k: True for k in pesos}
    return adoptar_cargas(
        estado,
        routine_key=RUTINA,
        exercises=plan,
        pesos_hechos=pesos,
        limpio=estricto,
        motivos=motivos or {},
        limpio_arriba=limpio_arriba if limpio_arriba is not None else estricto,
        motivos_arriba=motivos_arriba if motivos_arriba is not None else (motivos or {}),
        set_cfg=SETS_CFG,
        prog_cfg=prog or PROG,
    )


def estado_en(*pesos: float) -> EstadoFalso:
    return EstadoFalso(current_sets={CLAVE: series(*pesos)})


# ---------------------------------------------------------------------------
# Piezas sueltas
# ---------------------------------------------------------------------------


def test_el_tope_efectivo_es_el_maximo_y_no_el_ultimo():
    assert tope_efectivo(series(50, 60, 65)) == 65.0
    assert tope_efectivo(series(65, 60, 50)) == 65.0


def test_sin_series_el_tope_es_cero_y_no_revienta():
    """0 kg es "sin carga registrada", un estado que el resto del motor ya sabe
    leer (`needs_data`). Una excepción aquí tumbaría la reconciliación entera."""
    assert tope_efectivo([]) == 0.0
    assert tope_efectivo(None) == 0.0


def test_se_desplaza_la_rampa_entera_y_no_se_aplana():
    """Delta y no peso absoluto.

    A peso absoluto una rampa 50/60/65 saldría 65/65/65: el esquema se destruiría
    de un golpe y las tres series pasarían a ir a tope. Con delta el esquema
    sigue siendo el que era.
    """
    assert [s["weight_kg"] for s in _desplazar(series(50, 60, 65), 5)] == [55, 65, 70]


def test_el_desplazamiento_no_deja_pesos_negativos():
    """Un -10 kg en la rutina de mañana sería un dato imposible viajando a Hevy."""
    assert [s["weight_kg"] for s in _desplazar(series(5, 20), -10)] == [0, 10]


def test_el_margen_es_el_menor_de_los_dos():
    """Los dos topes son Y. «Nunca más de 5 kg de golpe» tiene que significar eso.

    En `max` el tope se aflojaba justo donde la carga absoluta es mayor: 30 kg de
    margen sobre una prensa de 150, que es el sentido contrario del que tiene que
    tener un tope con una L4-L5.
    """
    cfg = PROG["adopt_executed_load"]
    assert _margen(12, cfg) == pytest.approx(2.4)  # 20% de 12 → aprieta el %
    assert _margen(150, cfg) == 5.0  # 20% de 150 = 30 → manda el mínimo en kg


def test_el_margen_nunca_baja_del_incremento_del_ejercicio():
    """Un tope que prohíbe el paso que el programa acaba de pedir no protege de
    nada: 20% de 10 kg son 2, y el ejercicio sube de 2,5 en 2,5."""
    cfg = PROG["adopt_executed_load"]
    assert _margen(10, cfg) == 2.0
    assert _margen(10, cfg, 2.5) == 2.5
    # El suelo solo levanta: no puede aflojar el tope de los pesados.
    assert _margen(150, cfg, 2.5) == 5.0


def test_bajar_conserva_el_tope_ancho():
    """El tope aprieta en la dirección insegura, que es una sola.

    Apretando también al bajar, un desfase de 10 kg sobre 60 no se cerraría
    nunca: la adopción lo rechazaría cada sesión y el objetivo se quedaría para
    siempre por encima de lo que se levanta. El tope puesto para proteger la
    espalda acabaría obligando a intentar un peso que no sale.
    """
    cfg = PROG["adopt_executed_load"]
    assert _margen(60, cfg, 5, ABAJO) == 12.0
    assert _margen(60, cfg, 5, ARRIBA) == 5.0


# ---------------------------------------------------------------------------
# Hacia arriba
# ---------------------------------------------------------------------------


def test_se_adopta_hacia_arriba_a_la_primera():
    """La prueba de que se puede con 70 es que se hicieron las diez a 70.

    No hace falta esperar a repetirlo: esperar significaría anunciar "60→62,5"
    a alguien que ya está en 70, y un mensaje que no describe la realidad se
    deja de leer.
    """
    e = estado_en(60)
    (a,) = adoptar(e, [ejercicio(60)], {"hip_thrust": 63})

    assert a.aplicada is True
    assert a.direccion == ARRIBA
    assert a.objetivo_antes_kg == 60
    assert a.objetivo_despues_kg == 63
    assert tope_efectivo(e.current_sets[CLAVE]) == 63


def test_no_se_adopta_hacia_arriba_una_sesion_que_no_se_completo():
    """70 kg a cuatro reps cuando se pedían diez no es un objetivo nuevo.

    Es una serie que se cortó. Adoptarlo subiría el objetivo apoyándose
    justamente en el fallo.
    """
    e = estado_en(60)
    (a,) = adoptar(
        e, [ejercicio(60)], {"hip_thrust": 70}, {"hip_thrust": False},
        motivos={"hip_thrust": "la serie 2 se quedó en 4 de las 10 reps"},
    )

    assert a.aplicada is False
    assert a.direccion == ARRIBA
    assert "no quedó completo" in a.motivo
    assert tope_efectivo(e.current_sets[CLAVE]) == 60, "el objetivo no debía moverse"


def test_el_motivo_de_no_adoptar_dice_lo_que_falló_y_no_una_frase_de_plantilla():
    """Lo corto fue el PESO, y eso es lo que tiene que leerse.

    Antes la frase decía «no se completó a las reps objetivo» pasara lo que
    pasara, así que en este caso mandaba a revisar unas reps que estaban
    clavadas y callaba el kilaje, que era lo único que había fallado.
    """
    e = estado_en(60)
    (a,) = adoptar(
        e, [ejercicio(60)], {"hip_thrust": 70}, {"hip_thrust": False},
        motivos={"hip_thrust": "la serie 3 se hizo a 50 kg y pedía 60"},
    )

    assert "se hizo a 50 kg y pedía 60" in a.motivo
    assert "reps" not in a.motivo


def test_sin_motivo_no_se_inventa_uno():
    """Si no consta la causa, se dice que no consta.

    Nombrar una magnitud al azar en el mismo aviso que explica por qué NO ha
    subido la carga es la forma más cara de equivocarse: manda a corregir algo
    que a lo mejor estaba bien.
    """
    e = estado_en(60)
    (a,) = adoptar(e, [ejercicio(60)], {"hip_thrust": 70}, {"hip_thrust": False})

    assert "no consta" in a.motivo
    assert "reps" not in a.motivo


def test_la_subida_se_mide_contra_el_objetivo_y_no_contra_el_plan_del_dia():
    """En descarga el plan del día pide el 60%. Hacer ese 60% no supera nada.

    Si la subida se midiera contra el plan del día, cumplir una semana de
    descarga subiría la carga real, que es exactamente lo contrario de lo que
    una descarga es.
    """
    e = estado_en(60)
    # El plan de hoy pide 36 (60%) y se hacen 40: por encima de lo pedido, muy
    # por debajo del objetivo vigente.
    assert adoptar(e, [ejercicio(36)], {"hip_thrust": 40}) == []
    assert tope_efectivo(e.current_sets[CLAVE]) == 60


def test_la_subida_conserva_el_esquema_de_la_rampa():
    e = EstadoFalso(current_sets={CLAVE: series(50, 60, 65)})
    adoptar(e, [ejercicio(50, 60, 65)], {"hip_thrust": 70})
    assert [s["weight_kg"] for s in e.current_sets[CLAVE]] == [55, 65, 70]


# ---------------------------------------------------------------------------
# El tope de salto
# ---------------------------------------------------------------------------


def test_un_salto_desmedido_no_se_adopta_pero_se_cuenta():
    """Un 600 en vez de un 60 al teclear en Hevy.

    Sin el tope eso sería la rutina de mañana. Y el tope avisa: un tope que
    actúa sin decirlo es un tope que nadie puede corregir.
    """
    e = estado_en(60)
    (a,) = adoptar(e, [ejercicio(60)], {"hip_thrust": 600})

    assert a.aplicada is False
    assert "máximo" in a.motivo
    assert tope_efectivo(e.current_sets[CLAVE]) == 60


def test_el_tope_deja_pasar_lo_que_esta_justo_dentro():
    """60 + 5 = 65: un disco de cada lado entra, el siguiente ya no.

    Antes el límite estaba en 72 porque mandaba el 20%. Justo al revés de lo que
    tiene que hacer un tope: cuanto más pesaba el ejercicio, más suelto.
    """
    e = estado_en(60)
    (a,) = adoptar(e, [ejercicio(60)], {"hip_thrust": 65})
    assert a.aplicada is True

    e2 = estado_en(60)
    (b,) = adoptar(e2, [ejercicio(60)], {"hip_thrust": 65.5})
    assert b.aplicada is False


def test_la_primera_carga_registrada_no_pasa_por_el_tope():
    """Un objetivo a 0 no es un salto desde 0.

    Es el ejercicio que todavía no tiene carga apuntada, el caso que hoy deja la
    progresión parada avisando "apunta el peso real en Hevy". Aplicarle el tope
    lo dejaría parado para siempre protegiendo un número que no existe.
    """
    e = EstadoFalso(current_sets={CLAVE: series(0, 0)})
    (a,) = adoptar(e, [{"key": "hip_thrust", "name": "Hip thrust", "sets": series(0, 0)}],
                   {"hip_thrust": 80})

    assert a.aplicada is True
    assert a.motivo == "primera carga registrada en Hevy"
    assert tope_efectivo(e.current_sets[CLAVE]) == 80


def test_el_tope_tambien_frena_hacia_abajo():
    """Un 6 en vez de un 60 al teclear tampoco puede hundir el objetivo.

    La asimetría es de RITMO -a la primera arriba, a las tres abajo-, no de
    protección: el tope vale para los dos sentidos.
    """
    e = estado_en(60)
    for _ in range(3):
        adopciones = adoptar(e, [ejercicio(60)], {"hip_thrust": 6})
    (a,) = adopciones
    assert a.aplicada is False
    assert a.direccion == ABAJO
    assert tope_efectivo(e.current_sets[CLAVE]) == 60


# ---------------------------------------------------------------------------
# Hacia abajo
# ---------------------------------------------------------------------------


def test_una_sola_sesion_floja_no_baja_nada_y_no_dice_nada():
    """Casi siempre es el gimnasio lleno o la máquina ocupada.

    Bajar el objetivo por una sesión es la forma silenciosa de que un programa se
    desinfle. Y avisar de cada una entrenaría a saltarse el bloque justo antes
    del aviso que sí importa.
    """
    e = estado_en(60)
    assert adoptar(e, [ejercicio(60)], {"hip_thrust": 50}) == []
    assert tope_efectivo(e.current_sets[CLAVE]) == 60
    assert e.below_plan_streak[CLAVE] == 1


def test_a_las_tres_seguidas_si_baja():
    e = estado_en(60)
    assert adoptar(e, [ejercicio(60)], {"hip_thrust": 50}) == []
    assert adoptar(e, [ejercicio(60)], {"hip_thrust": 50}) == []
    (a,) = adoptar(e, [ejercicio(60)], {"hip_thrust": 50})

    assert a.aplicada is True
    assert a.direccion == ABAJO
    assert a.objetivo_despues_kg == 50
    assert tope_efectivo(e.current_sets[CLAVE]) == 50


def test_al_bajar_se_adopta_la_mejor_de_la_racha_y_no_la_ultima():
    """Un día malo no fija el suelo.

    Si se adoptara la última, la sesión del día que uno llega reventado del
    trabajo se convertiría en el objetivo permanente.
    """
    e = estado_en(60)
    adoptar(e, [ejercicio(60)], {"hip_thrust": 50})
    adoptar(e, [ejercicio(60)], {"hip_thrust": 57.5})
    (a,) = adoptar(e, [ejercicio(60)], {"hip_thrust": 45})

    assert a.objetivo_despues_kg == 57.5
    assert tope_efectivo(e.current_sets[CLAVE]) == 57.5


def test_una_sesion_buena_rompe_la_racha_por_debajo():
    """Dos flojas, una buena, una floja: no son tres seguidas y no baja nada."""
    e = estado_en(60)
    adoptar(e, [ejercicio(60)], {"hip_thrust": 50})
    adoptar(e, [ejercicio(60)], {"hip_thrust": 50})
    adoptar(e, [ejercicio(60)], {"hip_thrust": 60})

    assert CLAVE not in e.below_plan_streak
    assert CLAVE not in e.below_plan_best_kg

    assert adoptar(e, [ejercicio(60)], {"hip_thrust": 50}) == []
    assert tope_efectivo(e.current_sets[CLAVE]) == 60


def test_la_bajada_se_mide_contra_lo_que_se_pidio_hoy_y_no_contra_el_objetivo():
    """LA prueba de la asimetría, y la que evita que una descarga hunda el plan.

    Tres sesiones de una semana de descarga están las tres por debajo del
    objetivo vigente. Si la bajada se midiera contra el objetivo, cumplir la
    descarga al pie de la letra bajaría el objetivo real a las tres sesiones:
    la misma deriva que `BuiltSession.target_sets` evita capturando el objetivo
    ANTES de los recortes del día.
    """
    e = estado_en(60)
    for _ in range(3):
        # El plan del día pide 36 (descarga al 60%) y se hacen 36 clavados.
        assert adoptar(e, [ejercicio(36)], {"hip_thrust": 36}) == []

    assert tope_efectivo(e.current_sets[CLAVE]) == 60
    assert CLAVE not in e.below_plan_streak


def test_quedarse_corto_del_plan_pero_no_del_objetivo_no_baja_nada():
    """El plan del día pedía 70 -una semana de sobrecarga- y se hicieron 60
    clavados, que es justo el objetivo vigente. Tres veces.

    Se ha quedado corto de lo que se le pidió, sí, pero ni una sola vez por
    debajo de la carga que el motor considera suya. Sin esta salida, la tercera
    sesión llamaría a `_aplicar` con un delta de cero: una "bajada" de 60 a 60
    contada en el mensaje de la mañana, que es ruido puro.
    """
    e = estado_en(60)
    for _ in range(3):
        assert adoptar(e, [ejercicio(70)], {"hip_thrust": 60}) == []

    assert tope_efectivo(e.current_sets[CLAVE]) == 60
    assert CLAVE not in e.below_plan_streak, "la racha tenía que quedar limpia"


def test_pasarse_del_objetivo_sin_llegar_al_plan_del_dia_si_sube():
    """El caso contrario, y no es contradictorio: el plan pedía 70, se hicieron
    65 y el objetivo era 60. Del día se quedó corto, pero del OBJETIVO se pasó,
    y la subida se mide contra el objetivo. Se levantaron 65: eso es real."""
    e = estado_en(60)
    (a,) = adoptar(e, [ejercicio(70)], {"hip_thrust": 65})

    assert a.direccion == ARRIBA
    assert a.aplicada is True
    assert tope_efectivo(e.current_sets[CLAVE]) == 65


def test_al_bajar_se_conserva_el_esquema_de_la_rampa():
    e = EstadoFalso(current_sets={CLAVE: series(50, 60, 65)})
    for _ in range(3):
        adoptar(e, [ejercicio(50, 60, 65)], {"hip_thrust": 60})
    assert [s["weight_kg"] for s in e.current_sets[CLAVE]] == [45, 55, 60]


def test_despues_de_bajar_la_racha_queda_a_cero():
    """Si no se limpiara, la siguiente sesión por debajo bajaría otra vez de
    inmediato: el objetivo caería en escalera sin darle una sola oportunidad a
    la carga nueva."""
    e = estado_en(60)
    for _ in range(3):
        adoptar(e, [ejercicio(60)], {"hip_thrust": 50})

    assert CLAVE not in e.below_plan_streak
    assert CLAVE not in e.below_plan_best_kg


# ---------------------------------------------------------------------------
# Lo que no se toca
# ---------------------------------------------------------------------------


def test_sin_peso_registrado_no_se_toca_nada_ni_se_rompe_la_racha():
    """Ausencia de prueba no es prueba de haber levantado menos.

    Y tampoco dice que el problema se haya resuelto, así que la racha ni avanza
    ni se borra: un día suelto sin apuntar no puede ni bajar la carga ni salvarla.
    """
    e = estado_en(60)
    adoptar(e, [ejercicio(60)], {"hip_thrust": 50})
    assert e.below_plan_streak[CLAVE] == 1

    assert adoptar(e, [ejercicio(60)], {"hip_thrust": None}) == []
    assert e.below_plan_streak[CLAVE] == 1, "un día sin dato no cuenta ni a favor ni en contra"
    assert tope_efectivo(e.current_sets[CLAVE]) == 60


def test_un_ejercicio_sin_peso_y_sin_completar_se_cuenta_aunque_no_se_adopte():
    """El suitcase carry. No hay nada que adoptar, pero callarlo era el otro
    extremo: la carga se queda congelada para siempre y lo único visible es un
    número que no cambia, que es justo lo que no se ve."""
    e = estado_en(20)
    (a,) = adoptar(
        e, [ejercicio(20)], {"hip_thrust": None}, {"hip_thrust": False},
        motivos={"hip_thrust": "está en el entrenamiento pero sin series"},
    )

    assert a.aplicada is False
    assert a.hecho_kg is None, "no hay peso que enseñar, y un 0 se leería como 'sin carga'"
    assert a.objetivo_antes_kg == 20
    assert "sin series" in a.motivo
    assert tope_efectivo(e.current_sets[CLAVE]) == 20, "no se mueve nada"
    assert CLAVE not in e.below_plan_streak, "sin prueba no hay sesión por debajo"


def test_un_ejercicio_sin_peso_pero_completado_no_dice_nada():
    """Una plancha a los segundos pedidos no lleva kilos y no es una incidencia."""
    e = estado_en(20)
    assert adoptar(e, [ejercicio(20)], {"hip_thrust": None}, {"hip_thrust": True}) == []


def test_un_ejercicio_sin_objetivo_guardado_se_ignora():
    """No debería pasar -la mañana guarda el objetivo de toda la rutina- pero
    adoptar sobre una lista que no existe crearía una carga vigente a partir de
    un ejercicio que nunca se planificó."""
    e = EstadoFalso()
    assert adoptar(e, [ejercicio(60)], {"hip_thrust": 70}) == []
    assert e.current_sets == {}


def test_apagar_la_opcion_la_apaga_de_verdad():
    """Una opción que no se conecta a nada es el error recurrente de esta casa."""
    e = estado_en(60)
    prog = {"adopt_executed_load": {**PROG["adopt_executed_load"], "enabled": False}}

    assert adoptar(e, [ejercicio(60)], {"hip_thrust": 70}, prog=prog) == []
    assert tope_efectivo(e.current_sets[CLAVE]) == 60
    assert e.below_plan_streak == {}


def test_el_calentamiento_del_plan_no_cuenta_como_lo_pedido():
    """Si contara, el "pedido" de un ejercicio con aproximación ligera sería el
    peso del calentamiento y cualquier sesión quedaría por encima: la bajada no
    saltaría nunca."""
    e = estado_en(60)
    ex = {
        "key": "hip_thrust",
        "name": "Hip thrust",
        "sets": [
            {"type": "warmup", "reps": 5, "weight_kg": 20},
            {"type": "normal", "reps": 10, "weight_kg": 60},
        ],
    }
    for _ in range(3):
        adopciones = adoptar(e, [ex], {"hip_thrust": 50})
    (a,) = adopciones
    assert a.prescrito_kg == 60, "lo pedido eran 60, no los 20 del calentamiento"
    assert a.aplicada is True


def test_dos_ejercicios_no_se_pisan_la_racha():
    """Cada ejercicio lleva su propia cuenta. Si compartieran contador, una
    sentadilla floja bajaría el remo."""
    e = EstadoFalso(
        current_sets={
            (RUTINA, "hip_thrust"): series(60),
            (RUTINA, "remo"): series(40),
        }
    )
    plan = [ejercicio(60), ejercicio(40, key="remo")]
    for _ in range(3):
        adopciones = adoptar(e, plan, {"hip_thrust": 50, "remo": 40})

    assert [a.exercise_key for a in adopciones] == ["hip_thrust"]
    assert tope_efectivo(e.current_sets[(RUTINA, "remo")]) == 40


# ---------------------------------------------------------------------------
# Lo que sale al mensaje y a la base de datos
# ---------------------------------------------------------------------------


def test_la_adopcion_lleva_los_cuatro_numeros():
    """La pregunta de dentro de tres meses -"¿por qué el hip thrust está en 62,5
    y no en 70?"- no se puede contestar con menos."""
    e = estado_en(60)
    (a,) = adoptar(e, [ejercicio(55)], {"hip_thrust": 63})
    d = a.to_dict()

    assert d["prescribed_kg"] == 55
    assert d["executed_kg"] == 63
    assert d["before_kg"] == 60
    assert d["after_kg"] == 63
    assert d["direction"] == ARRIBA
    assert d["applied"] is True
    assert d["reason"]


def test_una_adopcion_rechazada_tambien_se_cuenta():
    e = estado_en(60)
    (a,) = adoptar(e, [ejercicio(60)], {"hip_thrust": 600})
    d = a.to_dict()

    assert d["applied"] is False
    assert d["after_kg"] is None, "no hay 'después' si no se aplicó"
    assert d["executed_kg"] == 600


def test_el_texto_dice_de_donde_a_donde():
    """Y lo dice CON PALABRAS, no con una flecha.

    El 25/09/2026 el usuario leyó «12,5→15 kg — ... y el plan pedía 12,5» y
    entendió que el sistema le había bajado a 12,5 cuando le había subido a 15:
    la flecha es fina y el motivo termina en el número viejo, así que el viejo
    era a la vez el primero y el último de la frase. «de 12,5 a 15 kg» no se
    puede leer al revés.
    """
    e = estado_en(60)
    (a,) = adoptar(e, [ejercicio(60)], {"hip_thrust": 65})
    assert "de 60 a 65 kg" in a.text(), a.text()
    assert "→" not in a.text(), "la flecha se leyó al revés una vez; no vuelve"

    e2 = estado_en(60)
    (b,) = adoptar(e2, [ejercicio(60)], {"hip_thrust": 600})
    assert "sigue en 60" in b.text()


def test_una_aplicada_sin_objetivo_nuevo_revienta_en_vez_de_escribir_cero_kg():
    """El invariante: aplicada siempre trae objetivo nuevo.

    La única rama que construye una `Adopcion` con `aplicada=True` le pasa
    `tope_efectivo(...)`, que devuelve un float; las que llevan `None` son
    todas `aplicada=False`. Así que el `or 0` que había aquí era inalcanzable
    con datos legítimos, y lo único que podía hacer es convertir una rotura del
    invariante en "hip thrust: de 62,5 a 0 kg" dentro del mensaje de la mañana, que
    se lee como que el objetivo se ha ido al suelo.

    Un 0,0 de verdad sí puede llegar -un ejercicio sin peso registrado- y ese
    sí se escribe: es un dato, no un hueco. Lo que no se escribe es el hueco
    disfrazado de dato.
    """
    a = Adopcion(
        routine_key="dia_1",
        exercise_key="hip_thrust",
        name="Hip thrust",
        direccion=ARRIBA,
        prescrito_kg=60.0,
        hecho_kg=65.0,
        objetivo_antes_kg=62.5,
        objetivo_despues_kg=None,
        aplicada=True,
        motivo="da igual, no llega a decirlo",
    )

    with pytest.raises(TypeError):
        a.text()


def test_un_objetivo_nuevo_de_cero_si_se_escribe():
    """La otra cara: 0 kg es un dato legítimo -un ejercicio sin peso- y se dice
    tal cual. El arreglo de arriba no puede haberse llevado esto por delante."""
    a = Adopcion(
        routine_key="dia_1",
        exercise_key="plancha",
        name="Plancha",
        direccion=ARRIBA,
        prescrito_kg=None,
        hecho_kg=0.0,
        objetivo_antes_kg=0.0,
        objetivo_despues_kg=0.0,
        aplicada=True,
        motivo="primera carga registrada en Hevy",
    )

    assert "de 0 a 0 kg" in a.text()


@pytest.mark.parametrize("hecho", [60.0, 60.0000001, 59.9999999])
def test_el_ruido_de_coma_flotante_no_inventa_adopciones(hecho):
    """62,5 escrito y leído por dos caminos distintos tiene que empatar.

    Sin esto, cada sesión clavada generaría una adopción de 0,0000001 kg y el
    mensaje de la mañana se llenaría de ruido que nadie puede accionar.
    """
    e = estado_en(60)
    adopciones = adoptar(e, [ejercicio(60)], {"hip_thrust": hecho})
    assert adopciones == [] or adopciones[0].objetivo_despues_kg == pytest.approx(
        hecho, abs=1e-6
    )


# ---------------------------------------------------------------------------
# La guarda de `_aplicar`: la explicación tiene que poder ser verdad
# ---------------------------------------------------------------------------
#
# `racha` tenía `= 0`. Era el más disimulado de los cuatro defectos que se
# barrieron porque NO dejaba de calcular nada: la carga se movía igual y el
# estado quedaba igual. Lo único que cambiaba era la frase, y la frase es lo que
# explica por qué el sistema te ha bajado un peso. Con el defecto puesto, un
# caller que se olvidara producía «0 sesiones seguidas por debajo de lo pedido;
# se adopta la mejor de ellas»: una afirmación que se contradice sola viajando
# al móvil pegada a una bajada de carga real.


def _aplicar_directo(direccion, racha):
    from app.engine.adoption import _aplicar

    e = estado_en(60)
    return _aplicar(
        e, CLAVE, ejercicio(60), e.current_sets[CLAVE], 60.0, 55.0, 60.0,
        direccion, PROG["adopt_executed_load"], racha=racha,
    )


def test_bajar_sin_racha_revienta_en_vez_de_escribir_un_motivo_falso():
    from app.engine.adoption import AdoptionError

    with pytest.raises(AdoptionError, match="0 sesiones seguidas"):
        _aplicar_directo(ABAJO, None)


def test_bajar_con_racha_cero_es_el_mismo_error():
    """`0` y `None` fallan igual: ninguno de los dos puede haber bajado nada."""
    from app.engine.adoption import AdoptionError

    with pytest.raises(AdoptionError):
        _aplicar_directo(ABAJO, 0)


def test_subir_con_racha_revienta_porque_subir_no_tiene_racha():
    """La otra mitad de la atadura.

    Sin ella, `racha` sería un parámetro obligatorio que la rama ARRIBA rellena
    con cualquier cosa, y el tipo dejaría de decir nada. Un número aquí
    significa que la dirección o el número están mal, y las dos cosas importan.
    """
    from app.engine.adoption import AdoptionError

    with pytest.raises(AdoptionError, match="subir no tiene racha"):
        _aplicar_directo(ARRIBA, 3)


def test_el_motivo_de_la_bajada_dice_cuantas_sesiones_fueron():
    """Lo que la guarda protege, por el camino normal.

    Tres sesiones por debajo es lo que `down_after_sessions` pide, y el mensaje
    tiene que decir el número: «se adopta la mejor de ellas» sin decir de
    cuántas no se puede contrastar con lo que uno recuerda haber hecho.
    """
    e = estado_en(60)
    for _ in range(3):
        adopciones = adoptar(e, [ejercicio(60)], {"hip_thrust": 50})
    (a,) = adopciones
    assert a.direccion == ABAJO
    assert "3 sesiones seguidas" in a.motivo


def test_la_subida_normal_sigue_funcionando_con_la_guarda_puesta():
    """Que atar `racha` a `direccion` no rompa el camino de todos los días."""
    e = estado_en(60)
    (a,) = adoptar(e, [ejercicio(60)], {"hip_thrust": 62.5})
    assert a.aplicada is True
    assert a.direccion == ARRIBA
    assert "0 sesiones" not in a.motivo


# ---------------------------------------------------------------------------
# Subir ya no lo bloquea una serie mas ligera (25/09/2026)
# ---------------------------------------------------------------------------
#
# Decision del usuario, escrita para que no se relea como un descuido: el
# sistema aconseja, no decide. Lo que se levanta es lo que hay. Lo unico que
# sigue bloqueando una subida es que la serie se CORTARA -reps o segundos por
# debajo-, porque eso no es un peso levantado, es un peso intentado.


def test_una_rampa_por_debajo_del_plan_ya_no_bloquea_la_subida():
    """El caso real: patada atras del 21/09, 30/40/50 con las 24 reps enteras.

    El veredicto estricto dice que no fue limpia -la serie 1 iba a 30 cuando el
    plan pedia 35- y el de subir dice que si. Manda el de subir.

    El salto de aqui es de 35 a 40 y no a los 50 reales porque el `PROG` de este
    fichero lleva el tope de juguete de 5 kg: con 50 el test se pondria verde o
    rojo por el TOPE y no por la guarda que dice probar. Los 35->50 de verdad
    los sujeta `test_el_tope_del_config_real_admite_los_saltos_reales_del_historico`,
    que si usa el `config.yaml`.
    """
    estado = estado_en(35, 35)
    ad = adoptar(
        estado,
        [ejercicio(35, 35)],
        {"hip_thrust": 40.0},
        limpio={"hip_thrust": False},
        motivos={"hip_thrust": "la serie 1 se hizo a 30 kg y pedía 35"},
        limpio_arriba={"hip_thrust": True},
        motivos_arriba={},
    )
    assert len(ad) == 1 and ad[0].aplicada is True
    assert ad[0].objetivo_despues_kg == 40.0
    assert estado.current_sets[CLAVE][-1]["weight_kg"] == 40.0


def test_el_vecino_que_no_sube_las_reps_cortas():
    """La pareja. Los mismos 50 kg, pero la serie se corto: no se adopta.

    Y el motivo sale de `motivos_arriba`, no de `motivos`: el estricto estaria
    hablando de la serie ligera, que aqui ya no es un problema, junto a una
    carga que no ha subido por otra razon.
    """
    estado = estado_en(35, 35)
    ad = adoptar(
        estado,
        [ejercicio(35, 35)],
        {"hip_thrust": 40.0},
        limpio={"hip_thrust": False},
        motivos={"hip_thrust": "la serie 1 se hizo a 30 kg y pedía 35"},
        limpio_arriba={"hip_thrust": False},
        motivos_arriba={"hip_thrust": "la serie 2 se quedó en 4 de las 24 reps"},
    )
    assert len(ad) == 1 and ad[0].aplicada is False
    assert "4 de las 24 reps" in ad[0].motivo
    assert "30 kg" not in ad[0].motivo
    assert estado.current_sets[CLAVE][-1]["weight_kg"] == 35.0


def test_el_veredicto_ancho_no_se_cuela_en_la_rama_de_bajar():
    """Bajar sigue midiendose contra lo que se pidio, con el peso contando.

    Si alguien cableara `limpio_arriba` tambien aqui, una sesion a 50 cuando el
    plan pedia 60 saldria limpia y el desfase no se cerraria nunca: exactamente
    el agujero que la adopcion hacia abajo existe para tapar.
    """
    estado = estado_en(60, 60)
    for _ in range(3):
        ad = adoptar(
            estado,
            [ejercicio(60, 60)],
            {"hip_thrust": 50.0},
            limpio={"hip_thrust": False},
            limpio_arriba={"hip_thrust": True},
        )
    assert len(ad) == 1 and ad[0].aplicada is True
    assert ad[0].direccion == "down"
    assert estado.current_sets[CLAVE][-1]["weight_kg"] == 50.0


def test_el_tope_del_config_real_admite_los_saltos_reales_del_historico(cfg):
    """Los cuatro saltos que el tope freno en seis meses eran de verdad.

    Ninguno era una errata: 35->50 en la patada atras, 40->60 en la extension de
    cuadriceps, 50->60 en la aduccion y 30->37,5 en la contractora. Un tope que
    solo ha frenado aciertos no protege, estorba. El 25/09/2026 se ensancho a
    30 kg y 60%, y esto ata esos numeros a lo que tienen que dejar pasar.

    La otra mitad -que siga frenando un dedazo- va en el test de al lado.
    """
    prog = {"adopt_executed_load": cfg.raw["progression"]["adopt_executed_load"]}
    for antes, despues in ((35, 50), (40, 60), (50, 60), (30, 37.5)):
        estado = estado_en(antes, antes)
        ad = adoptar(
            estado, [ejercicio(antes, antes)], {"hip_thrust": float(despues)},
            prog=prog,
        )
        assert ad and ad[0].aplicada is True, (
            f"{antes}->{despues} lo frena el tope, y fue un levantamiento real"
        )


@pytest.mark.parametrize("antes,dedazo", [(60, 600), (50, 150), (100, 1000)])
def test_el_tope_del_config_real_sigue_frenando_un_dedazo(cfg, antes, dedazo):
    """Ensanchar no es quitar. Un digito de mas sigue sin llegar a la rutina."""
    prog = {"adopt_executed_load": cfg.raw["progression"]["adopt_executed_load"]}
    estado = estado_en(antes, antes)
    ad = adoptar(
        estado, [ejercicio(antes, antes)], {"hip_thrust": float(dedazo)}, prog=prog
    )
    assert ad and ad[0].aplicada is False
    assert "no se adopta solo" in ad[0].motivo


def test_el_tope_en_kilos_manda_aunque_el_porcentaje_deje_pasar(cfg):
    """`max_jump_kg: 30` es un techo absoluto, y sin el no habria ninguno arriba.

    Con un objetivo de 100 kg el 60% son 60: el porcentaje dejaria pasar un
    salto de 40 sin pestanear. Lo frena el tope en kilos, que es lo que
    significa "nunca mas de 30 de golpe" y la razon de que los dos vayan en Y.

    Existe porque el banco de mutaciones del 25/09/2026 subio `max_jump_kg` a
    999 y NINGUN test se puso rojo: los dedazos que se probaban eran todos tan
    grandes que el porcentaje los frenaba solo, asi que el tope en kilos no
    estaba atado a nada.
    """
    prog = {"adopt_executed_load": cfg.raw["progression"]["adopt_executed_load"]}
    estado = estado_en(100, 100)
    ad = adoptar(estado, [ejercicio(100, 100)], {"hip_thrust": 140.0}, prog=prog)
    assert ad and ad[0].aplicada is False
    assert "no se adopta solo" in ad[0].motivo
    assert estado.current_sets[CLAVE][-1]["weight_kg"] == 100.0
