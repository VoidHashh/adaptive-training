"""El único fallo del proyecto que SUELTA en vez de frenar.

Todos los errores que ha tenido este sistema pecaban de prudentes. El
presupuesto de intensas que se agotaba solo, el freno del lunes por la bici del
fin de semana, la racha que se quedaba en cero: todos recortaban de más. Molestan,
pero el peor día que provocan es un día de entrenamiento flojo.

`actions.<luz>.session` no. Se leía con `str(action.get("session", FULL))` en tres
sitios distintos, y `FULL` es el más permisivo de los tres valores posibles. Como
el bloque `actions` no tenía whitelist, escribir `sesion:` en vez de `session:`
bajo `actions.red` no daba ningún error: construía la sesión COMPLETA en un día
rojo, le aplicaba la progresión, y la escribía en Hevy. El fichero decía
"recovery" y el gimnasio recibía "full". Con una hernia L4-L5, eso no es un
número mal calculado: es una lesión.

Son cuatro cerraduras para cuatro fallos distintos, y este fichero existe para
que ninguna se pueda quitar sin que algo se ponga rojo:

  1. WHITELIST     — la errata (`sesion`, `recovery_blok`) no entra.
  2. VALOR VÁLIDO  — `session: recuperacion` no entra aunque esté bien escrito.
  3. MONOTONÍA     — `red: full` no entra aunque sea un valor legítimo: es una
                     mala decisión correctamente escrita, y el YAML no puede
                     dejar el día peor más suelto que el mejor.
  4. SIN DEFECTO   — el motor revienta si la clave falta, para la ruta que algún
                     día llegue sin haber pasado por el validador.

Las tres primeras se comprueban al arrancar (`_validate`), la cuarta dentro del
motor. Las dos capas se prueban por separado a propósito: son dos cerraduras, no
una comprobada dos veces.

Y hay una quinta sección que NO es una cerradura: es la puerta que se abre a
propósito para que el usuario pueda pedir otra sesión de la que el semáforo
propone. Está en este fichero, y no en uno suyo, justamente porque lo que hay
que demostrar no es que funcione, sino que no es este mismo fallo otra vez con
mejor nombre. La diferencia cabe en una frase: aquello subía la sesión sin que
nadie lo pidiera y sin dejar rastro; esto exige que lo pida una persona, ese
día, y lo apunta.
"""

from __future__ import annotations

import copy
from datetime import date

import pytest

from app.config_loader import _validate
from app.engine.progression import ExerciseProgression, ProgressionPlan
from app.engine.rules import RuleError
from app.engine.session_builder import (
    RECOVERY,
    REDUCED,
    ConfirmacionNecesaria,
    SesionPedida,
    build_session,
    siguiente_en_rotacion,
)


def errores(data) -> str:
    return "\n".join(_validate(data))


# ---------------------------------------------------------------------------
# 1. Whitelist: la errata no entra
# ---------------------------------------------------------------------------


def test_el_bloque_actions_tiene_whitelist(cfg):
    """`sesion:` en vez de `session:`. El fallo original, tal cual.

    Antes de la whitelist esto cargaba sin una sola queja y el día rojo salía
    completo. El test mira las dos mitades del daño: que la clave rara se
    denuncia Y que se nota que la buena ha desaparecido.
    """
    d = copy.deepcopy(cfg.raw)
    d["actions"]["red"].pop("session")
    d["actions"]["red"]["sesion"] = "recovery"
    msg = errores(d)
    assert "clave desconocida 'sesion'" in msg
    assert "actions.red.session no está" in msg


@pytest.mark.parametrize("luz", ["green", "amber", "red"])
def test_ninguna_luz_admite_claves_inventadas(cfg, luz):
    d = copy.deepcopy(cfg.raw)
    d["actions"][luz]["allow_deadlift"] = True
    assert "clave desconocida 'allow_deadlift'" in errores(d)


def test_actions_no_admite_luces_inventadas(cfg):
    """Una cuarta luz sería una sección entera que nadie lee jamás."""
    d = copy.deepcopy(cfg.raw)
    d["actions"]["purple"] = {"session": "recovery"}
    assert "clave desconocida 'purple'" in errores(d)


def test_recovery_blocks_tiene_whitelist(cfg):
    d = copy.deepcopy(cfg.raw)
    nombre = next(iter(d["recovery_blocks"]))
    d["recovery_blocks"][nombre]["ejercicios"] = []
    assert "clave desconocida 'ejercicios'" in errores(d)


# ---------------------------------------------------------------------------
# 2. Valor válido: bien escrito pero sin sentido
# ---------------------------------------------------------------------------


def test_session_solo_admite_los_tres_valores_del_motor(cfg):
    """`recuperacion` en castellano: bien escrito, mal valor.

    La whitelist de claves no lo pilla, porque la clave es correcta. El motor
    tampoco lo pillaba: el valor desconocido caía en la rama de sesión completa
    por descarte, que es exactamente el peor sitio donde caer.
    """
    d = copy.deepcopy(cfg.raw)
    d["actions"]["red"]["session"] = "recuperacion"
    assert "solo puede ser una de" in errores(d)


@pytest.mark.parametrize("valor", ["false", "true", 0, 1, None])
def test_las_banderas_booleanas_no_admiten_cadenas(cfg, valor):
    """`allow_progression: 'false'` entrecomillado es VERDADERO en Python.

    YAML lo lee como la cadena `"false"`, y `bool("false")` es `True`. El día
    rojo con progresión activada por unas comillas es el mismo fallo que el de
    `session`, con otra puerta.
    """
    d = copy.deepcopy(cfg.raw)
    d["actions"]["red"]["allow_progression"] = valor
    assert "no es true ni false" in errores(d) or "no está" in errores(d)


def test_bike_max_tiene_que_existir_en_intensity_order(cfg):
    d = copy.deepcopy(cfg.raw)
    d["actions"]["red"]["bike_max"] = "brutal"
    assert "no está en intensity_order" in errores(d)


# ---------------------------------------------------------------------------
# 3. Monotonía: la mala decisión bien escrita
# ---------------------------------------------------------------------------
#
# Las dos cerraduras anteriores solo miran cada luz por su cuenta. `red: full`
# las pasa las dos: la clave se llama `session` y `full` es un valor legítimo.
# Lo que está mal es la RELACIÓN. Un día rojo es, por definición, un día peor
# que un ámbar; si el YAML le da una sesión más suelta, el semáforo está
# invertido y ningún test de valores sueltos lo iba a ver.


def test_el_rojo_no_puede_ser_mas_suelto_que_el_ambar(cfg):
    d = copy.deepcopy(cfg.raw)
    d["actions"]["red"]["session"] = "full"
    msg = errores(d)
    assert "el día PEOR daría una sesión más exigente" in msg


def test_el_ambar_no_puede_ser_mas_suelto_que_el_verde(cfg):
    d = copy.deepcopy(cfg.raw)
    d["actions"]["green"]["session"] = "recovery"
    assert "el día PEOR daría una sesión más exigente" in errores(d)


def test_la_monotonia_deja_pasar_las_tres_iguales(cfg):
    """No se exige que bajen, solo que no suban. Tres `reduced` es prudente.

    Si el test exigiera estrictamente que cada luz fuera más dura que la
    anterior, prohibiría configuraciones perfectamente sensatas y la gente
    aprendería a saltarse la comprobación, que es como mueren los validadores.
    """
    d = copy.deepcopy(cfg.raw)
    for luz in ("green", "amber", "red"):
        d["actions"][luz]["session"] = "reduced"
        d["actions"][luz].pop("recovery_block", None)
    assert "más exigente que el mejor" not in errores(d)


# ---------------------------------------------------------------------------
# 4. Sin defecto: la cerradura de dentro del motor
# ---------------------------------------------------------------------------
#
# El validador cubre el arranque. Esta capa cubre lo que entre por otro sitio:
# un config construido a mano en un test, una ruta de la API que monte un dict,
# un `raw` manipulado. Un `RuleError` a las 06:30 es un mal día; una sesión
# completa en rojo es un mes fuera.


def test_el_motor_revienta_si_falta_session(cfg):
    d = copy.deepcopy(cfg.raw)
    d["actions"]["red"].pop("session")
    with pytest.raises(RuleError, match="no está en el config"):
        build_session(d, date(2026, 9, 14), "red", rotation_routine="dia_1")


def test_el_motor_revienta_con_un_session_desconocido(cfg):
    d = copy.deepcopy(cfg.raw)
    d["actions"]["red"]["session"] = "full_pero_suave"
    with pytest.raises(RuleError, match="full_pero_suave"):
        build_session(d, date(2026, 9, 14), "red", rotation_routine="dia_1")


def test_el_dia_rojo_del_config_real_es_de_recuperacion(cfg):
    """La contraparte positiva: con el config bueno, el rojo frena.

    Sin este test los cuatro de arriba solo demostrarían que el sistema sabe
    decir que no.
    """
    s = build_session(cfg, date(2026, 9, 14), "red", rotation_routine="dia_1")
    assert s.kind == RECOVERY
    assert s.exercises, "el día rojo llega sin un solo ejercicio"


# ---------------------------------------------------------------------------
# 5. La puerta que SÍ se abre, y con qué llave
# ---------------------------------------------------------------------------
#
# Las cuatro cerraduras de arriba cierran el paso al YAML y a las erratas. Esta
# sección es distinta: aquí se abre una puerta A PROPÓSITO, y hay que escribir
# muy claro en qué se diferencia de un agujero.
#
# El usuario pidió poder anular la sesión propuesta -"si quiero la completa
# donde el sistema propone reducida, que se escriba la mía, registrada como
# anulación"- porque el objetivo del sistema es CALIBRAR: que sus respuestas y
# las decisiones converjan. Un sistema al que no se le puede llevar la contraria
# no se calibra, se obedece o se ignora, y lo segundo es lo que pasa de verdad.
#
# La diferencia con el fallo del `str(action.get("session", FULL))` es entera y
# cabe en una frase: aquello subía la sesión SIN QUE NADIE LO PIDIERA y sin
# dejar rastro. Esto exige que lo pida una persona, y lo apunta.
#
# Y la puerta tiene una llave de más para el caso peligroso. Subir de intensidad
# un día ROJO -el día que el cuerpo ha dicho que pares, con una hernia L4-L5-
# pide confirmación explícita, y queda marcado distinto en los datos. No para
# impedirlo: para poder mirarlo después y ver si el que se equivocaba era el
# umbral o era el usuario.


def _reducida(cfg):
    """Un config cuyo ámbar propone sesión reducida: el caso de la anulación."""
    d = copy.deepcopy(cfg.raw)
    d["actions"]["amber"]["session"] = "reduced"
    return d


# Una progresión que SÍ subiría algo, sobre un ejercicio que SÍ está en `dia_1`.
#
# Las dos condiciones importan. Con un plan sin cambios, o con una clave que no
# existe en la rutina, `test_anular_la_sesion_no_toca_ni_el_semaforo_ni_la_
# progresion` pasaría aunque alguien abriera la puerta de par en par: no habría
# nada que aplicar y `changes` saldría vacío de todas formas.
PROGRESION_CON_CAMBIOS = ProgressionPlan(
    routine_key="dia_1",
    gate_open=True,
    gate_reason="abierta a propósito: quien cierra en rojo es allow_progression",
    sets_allowed=True,
    sets_reason="",
    reps_allowed=True,
    reps_reason="",
    exercises=[
        ExerciseProgression(
            key="prensa_horizontal",
            name="Prensa horizontal",
            mode="load",
            changed=True,
            kind="load",
            what="60 -> 62,5 kg",
            why="dos sesiones limpias",
            weight_delta_kg=2.5,
        )
    ],
)


def test_pedir_la_completa_donde_el_sistema_propone_reducida(cfg):
    """El caso que el usuario dijo que más iba a querer anular.

    Hoy elegir rutina ya se podía -`chosen_session`-, pero pedir la sesión
    entera donde el sistema recortaba no: no había por dónde. La consecuencia
    práctica era que el desacuerdo no se registraba en ningún sitio, porque la
    única forma de tenerlo era no usar el sistema ese día.
    """
    s = build_session(
        _reducida(cfg), date(2026, 9, 14), "amber",
        rotation_routine="dia_1", sesion_pedida=SesionPedida("full"),
    )
    assert s.kind == "full"
    assert s.anulacion is not None
    assert s.anulacion.propuesta == "reduced"
    assert s.anulacion.pedida == "full"
    assert s.anulacion.forzada_en_rojo is False


def test_en_rojo_subir_sin_confirmar_no_sube_Y_LO_DICE(cfg):
    """Las dos mitades, y la segunda es la que importa.

    Que no suba es la mitad fácil. La difícil es que se ENTERE: devolver la
    sesión de recuperación calladamente sería darle al usuario lo contrario de
    lo que pidió con cara de haberle hecho caso, y ese es exactamente el modo
    de fallo que este fichero entero existe para prohibir. Por eso revienta con
    su propia excepción en vez de ignorar la petición.
    """
    with pytest.raises(ConfirmacionNecesaria) as e:
        build_session(
            cfg, date(2026, 9, 14), "red",
            rotation_routine="dia_1", sesion_pedida=SesionPedida("full"),
        )
    assert e.value.propuesta == RECOVERY
    assert e.value.pedida == "full"


def test_en_rojo_subir_confirmado_sube_y_queda_marcado_aparte(cfg):
    """Se permite siempre. Lo que cambia es que se sabe."""
    s = build_session(
        cfg, date(2026, 9, 14), "red",
        rotation_routine="dia_1",
        sesion_pedida=SesionPedida("full", confirmada=True, motivo="me encuentro bien"),
    )
    assert s.kind == "full"
    assert s.anulacion.forzada_en_rojo is True
    assert s.anulacion.motivo == "me encuentro bien"


def test_la_marca_de_forzada_no_se_pone_sola_por_venir_confirmada(cfg):
    """La guarda del DATO, no la del entrenamiento.

    Si `forzada_en_rojo` copiara el `confirmada` que llega, una pantalla que
    mandara siempre `true` -porque es más cómodo que preguntar- marcaría como
    forzadas todas las anulaciones. La medida que el usuario quiere sacar de
    aquí -cuántas veces sube el día que no debía- se llenaría de días verdes y
    no mediría nada. La marca es una propiedad del día, no del formulario.
    """
    s = build_session(
        _reducida(cfg), date(2026, 9, 14), "amber",
        rotation_routine="dia_1",
        sesion_pedida=SesionPedida("full", confirmada=True),
    )
    assert s.anulacion.forzada_en_rojo is False


def test_bajar_no_pide_confirmacion_ningun_dia(cfg):
    """Pedir menos de lo que el sistema propone no necesita permiso.

    Hacerlo simétrico sonaría a coherencia y sería un estorbo diario: la
    confirmación existe por la hernia, no por el formalismo, y una que salta
    también cuando uno se cuida enseña a darle a «sí» sin leerla. Ese es el
    mecanismo exacto por el que las confirmaciones dejan de confirmar nada.

    Y pide EJERCICIOS, no solo el `kind`. Este test se puso rojo por eso: el
    bloque de recuperación se leía de `actions.<luz>.recovery_block`, que el
    validador exige que exista si y solo si esa luz propone recuperación, o sea
    que solo está bajo `red`. Un día verde pidiendo recuperación reventaba. Sin
    mirar `exercises`, la versión que «arregla» eso devolviendo un bloque vacío
    pasaría igual, y esa es precisamente la avería B-2 de este mismo fichero.
    """
    s = build_session(
        cfg, date(2026, 9, 14), "green",
        rotation_routine="dia_1", sesion_pedida=SesionPedida(RECOVERY),
    )
    assert s.kind == RECOVERY
    assert s.anulacion.pedida == RECOVERY
    assert s.anulacion.forzada_en_rojo is False
    assert s.exercises, "la recuperación pedida llega sin un solo ejercicio"


def test_la_recuperacion_pedida_es_LA_MISMA_que_la_del_dia_rojo(cfg):
    """No una recuperación cualquiera: la que el sistema da cuando frena.

    El config real tiene UN bloque, así que «coge el del rojo» y «coge el que
    haya» dan hoy el mismo resultado y este test no distinguiría entre los dos.
    Por eso mete un segundo bloque, y lo mete DELANTE: así la implementación
    perezosa -el primero del diccionario- devuelve el que no es y se pone roja
    hoy, en vez de el día lejano en que alguien añada un bloque de verdad.
    """
    d = copy.deepcopy(cfg.raw)
    d["recovery_blocks"] = {
        "otro_cualquiera": {
            "title": "Otro bloque",
            "exercises": [{"key": "paseo", "name": "Paseo", "sets": []}],
        },
        **d["recovery_blocks"],
    }

    pedida = build_session(
        d, date(2026, 9, 14), "green",
        rotation_routine="dia_1", sesion_pedida=SesionPedida(RECOVERY),
    )
    roja = build_session(d, date(2026, 9, 14), "red", rotation_routine="dia_1")
    assert pedida.routine_key == roja.routine_key
    assert pedida.exercises == roja.exercises


def test_en_rojo_bajar_tampoco_se_marca_como_forzada(cfg):
    """La marca es «ha subido en rojo», no «ha tocado algo en rojo».

    Con el config real esto no puede pasar -en rojo se propone recuperación, y
    por debajo no hay nada-, así que hace falta el config de las tres luces
    iguales, que la monotonía admite y que tiene su propio test unas secciones
    más arriba. Con `red: full`, bajar a reducida en rojo es una anulación
    legítima y PRUDENTE. Si la marca mirase solo la luz, la contaría como una
    imprudencia: la medida que el usuario quiere -cuántas veces sube el día que
    no debía- saldría inflada justo por los días en que se cuidó.
    """
    d = copy.deepcopy(cfg.raw)
    for luz in ("green", "amber", "red"):
        d["actions"][luz]["session"] = "full"
    # El validador prohíbe nombrar bloque en una luz que no propone
    # recuperación, así que un config de tres completas no lo lleva.
    d["actions"]["red"].pop("recovery_block", None)
    assert "no puede ser más suelto" not in errores(d), "el config del test no es legal"

    s = build_session(
        d, date(2026, 9, 14), "red",
        rotation_routine="dia_1", sesion_pedida=SesionPedida(REDUCED),
    )
    assert s.kind == REDUCED
    assert s.anulacion.forzada_en_rojo is False


def test_pedir_exactamente_lo_propuesto_no_es_una_anulacion(cfg):
    """Estar de acuerdo no es llevar la contraria.

    La pantalla manda el tipo de sesión en cada envío, coincida o no. Si eso
    contara como anulación, el histórico diría que el usuario discrepa todos
    los días y las tres medidas que pidió -cuántas veces, en qué dirección, si
    se agrupan en un umbral- saldrían al 100% y no dirían nada.
    """
    s = build_session(
        cfg, date(2026, 9, 14), "red",
        rotation_routine="dia_1", sesion_pedida=SesionPedida(RECOVERY),
    )
    assert s.kind == RECOVERY
    assert s.anulacion is None


def test_una_sesion_pedida_que_no_existe_revienta(cfg):
    """La misma cerradura que `actions.<luz>.session`, en la puerta nueva.

    Sin esto, la ruta que el usuario controla desde el móvil sería la ÚNICA
    que acepta un valor desconocido, justo la que no pasa por el validador del
    arranque. `full_pero_suave` caería por descarte en la rama de siempre.
    """
    with pytest.raises(RuleError, match="full_pero_suave"):
        build_session(
            cfg, date(2026, 9, 14), "amber",
            rotation_routine="dia_1",
            sesion_pedida=SesionPedida("full_pero_suave"),
        )


def test_anular_la_sesion_no_toca_ni_el_semaforo_ni_la_progresion(cfg):
    """Lo que la anulación NO anula, que es casi todo.

    Cambia el tipo de sesión y nada más. La luz sigue roja, y como la puerta de
    la progresión la abre `actions.<luz>.allow_progression` -que se lee por LUZ
    y no por sesión-, sigue cerrada. Pedir la sesión entera es pedir hacer los
    movimientos, no pedir además subir el peso el día que el cuerpo ha dicho
    que pares: son dos cosas y solo se ha pedido una.

    Está probado aquí, y no solo confiado a que el código lea `light`, porque
    es exactamente el atajo que alguien tomaría al implementarlo -«si pide
    completa, trátalo como un día bueno»- y el resultado sería progresión en
    rojo sin que nadie lo hubiera pedido.
    """
    s = build_session(
        cfg, date(2026, 9, 14), "red",
        rotation_routine="dia_1",
        sesion_pedida=SesionPedida("full", confirmada=True),
        progression=PROGRESION_CON_CAMBIOS,
    )
    assert s.kind == "full"
    assert any("el semáforo está en red" in n for n in s.notes)
    # Sin reglas activas ni descarga, lo único que podría haber escrito aquí es
    # la progresión. Que la lista esté vacía es que no se aplicó.
    assert s.changes == []


# ---------------------------------------------------------------------------
# B-2: recovery_block contra recovery_blocks
# ---------------------------------------------------------------------------
#
# El mismo patrón que A-1, en el mismo día rojo y a tres líneas de distancia.
# `(raw.get("recovery_blocks") or {}).get(block_key, {})` con un nombre que no
# existe daba `{}`, y de `{}` salía una sesión de recuperación con título, cero
# ejercicios y cara de estar bien. Este es el fallo silencioso en su forma más
# pura: el día rojo es JUSTO el día en que el mensaje tiene que decir qué hacer.


def test_un_recovery_block_inexistente_no_arranca(cfg):
    d = copy.deepcopy(cfg.raw)
    d["actions"]["red"]["recovery_block"] = "bloque_fantasma"
    assert "no existe en recovery_blocks" in errores(d)


def test_el_motor_revienta_con_un_recovery_block_inexistente(cfg):
    d = copy.deepcopy(cfg.raw)
    d["actions"]["red"]["recovery_block"] = "bloque_fantasma"
    with pytest.raises(RuleError, match="bloque_fantasma"):
        build_session(d, date(2026, 9, 14), "red", rotation_routine="dia_1")


def test_un_bloque_de_recuperacion_vacio_no_arranca(cfg):
    """Un bloque sin ejercicios es el mismo daño que un bloque que no existe."""
    d = copy.deepcopy(cfg.raw)
    nombre = next(iter(d["recovery_blocks"]))
    d["recovery_blocks"][nombre]["exercises"] = []
    assert "no tiene ejercicios" in errores(d)


def test_recovery_blocks_no_puede_estar_vacio(cfg):
    d = copy.deepcopy(cfg.raw)
    d["recovery_blocks"] = {}
    assert "al menos un bloque" in errores(d)


def test_una_sesion_de_recuperacion_sin_bloque_no_arranca(cfg):
    """`session: recovery` y ningún `recovery_block` que consultar."""
    d = copy.deepcopy(cfg.raw)
    d["actions"]["red"].pop("recovery_block")
    assert "recovery_block" in errores(d)


# ---------------------------------------------------------------------------
# El día rojo y la rotación: lo que antes se aplazaba y ahora sencillamente
# no avanza
# ---------------------------------------------------------------------------
#
# Aquí vivía la batería de `defer_expires_days`, el plazo que tenía una sesión
# aplazada por un rojo antes de darse por perdida. Se leía con `.get(..., 7)` en
# `session_builder` y otra vez en `decision`, y de ahí salieron seis tests: dos
# copias del mismo defecto son dos sitios donde cambiar el YAML no cambia nada,
# y además dos sitios que pueden acabar diciendo cosas distintas.
#
# Nada de eso existe. La rotación se lee de lo EJECUTADO, un día rojo no ejecuta
# ninguna rutina del ciclo, y por tanto mañana vuelve a tocar exactamente la
# misma. Lo que hay que proteger ya no es un número, es esa propiedad: que un
# día malo no cueste una sesión. Es lo mismo que protegían aquellos seis tests,
# comprobado donde ahora vive.


def test_un_dia_rojo_no_mueve_el_puntero_de_la_rotacion(cfg):
    """La propiedad entera del rediseño, en una línea.

    Antes esto necesitaba una tabla, un estado, una fecha de caducidad y tres
    sitios distintos donde limpiarla. El que se saltó uno de esos tres sitios
    -`advance_state` borraba el pendiente por haber PLANIFICADO otra rutina, no
    por haberla ejecutado- costó una sesión entera en silencio.
    """
    from app.engine.decision import EngineState

    estado = EngineState(last_strength=("dia_1", date(2026, 9, 14)))
    # Da igual cuántos días rojos pasen: no hay ejecución, no hay avance.
    assert siguiente_en_rotacion(cfg, estado.last_strength[0]) == "dia_2"


def test_el_bloque_de_recuperacion_no_es_una_rutina_del_ciclo(cfg):
    """Por qué el rojo no avanza la rotación, dicho desde el otro lado.

    No es una regla escrita en ninguna parte: es que `recovery_block` apunta a
    `recovery_blocks`, que no está en `rotation.order`. Si algún día un bloque
    de recuperación se colara en el ciclo, el rojo empezaría a avanzar la
    rotación y la sesión de fuerza SÍ se perdería, que es justo el fallo que
    este diseño quita.
    """
    s = build_session(cfg, date(2026, 9, 14), "red", rotation_routine="dia_1")
    assert s.kind == RECOVERY
    assert s.routine_key not in cfg.rotation_order()


def test_la_rotacion_da_la_vuelta_y_el_dia_3_esta_dentro(cfg):
    """El Día 3 es el que el calendario fijo no nombraba NUNCA.

    Todas sus sesiones entraron como entrenos sueltos «que ese día no tocaba
    fuerza»: sin reconciliar, sin racha, sin adopción de carga y sin progresión.
    Sus cargas no se movieron una sola vez. Este test es el que se pondría rojo
    si volviera a salirse del ciclo.
    """
    assert cfg.rotation_order() == ["dia_1", "dia_2", "dia_3"]
    assert siguiente_en_rotacion(cfg, "dia_1") == "dia_2"
    assert siguiente_en_rotacion(cfg, "dia_2") == "dia_3"
    assert siguiente_en_rotacion(cfg, "dia_3") == "dia_1"


def test_sin_ninguna_sesion_ejecutada_la_rotacion_empieza_por_el_principio(cfg):
    assert siguiente_en_rotacion(cfg, None) == "dia_1"


def test_una_rutina_retirada_del_ciclo_no_deja_la_rotacion_colgada(cfg):
    """Se saca `dia_2` del ciclo y la última ejecutada era justamente esa.

    Devolver `None` aquí habría sido una mañana sin sesión, indistinguible de un
    día de descanso. Se empieza de nuevo: el fichero manda y una rutina retirada
    no tiene un «siguiente».
    """
    d = copy.deepcopy(cfg.raw)
    d["rotation"]["order"] = ["dia_1", "dia_3"]
    assert siguiente_en_rotacion(d, "dia_2") == "dia_1"


def test_un_ciclo_vacio_revienta_en_vez_de_dar_una_manana_en_blanco(cfg):
    """Sin ciclo no hay nada que escribir, y callarse sería lo de siempre."""
    d = copy.deepcopy(cfg.raw)
    d["rotation"]["order"] = []
    with pytest.raises(RuleError, match="rotation.order"):
        siguiente_en_rotacion(d, "dia_1")
    assert "al menos una rutina" in errores(d)


def test_el_constructor_revienta_si_la_rotacion_senala_una_rutina_que_no_existe(cfg):
    """La segunda cerradura, para la ruta que entre sin pasar por el validador.

    Antes de la rotación, una rutina que no existía en `routines` salía del
    constructor como un día de descanso con título "Descanso": el fallo mudo
    exacto que costó el Día 3.
    """
    with pytest.raises(RuleError, match="dia_inventado"):
        build_session(cfg, date(2026, 9, 14), "green", rotation_routine="dia_inventado")


def test_nadie_vuelve_a_leer_el_calendario_ni_el_aplazamiento():
    """Que no quede un lector suelto de lo que se ha borrado.

    Mismo espíritu que el test que había aquí -«que no vuelva a haber dos
    números para lo mismo»-, aplicado a las claves enteras: un `.get("calendar")`
    o un `defer_expires_days` olvidado en `app/` no daría error, daría un valor
    vacío, y de un valor vacío salen días de descanso que nadie pidió.
    """
    import ast

    from tests.conftest import REPO_ROOT

    MUERTAS = {
        "calendar", "active_variant", "today_plan",
        "defer_strength", "defer_expires_days", "caducidad_del_aplazamiento",
        "pending_strength", "PendingStrength", "expired_deferral",
        "deferred_from", "calendar_routine",
    }

    # Se mira el ÁRBOL, no el texto. Un `grep` daría por lector cada uno de los
    # comentarios y docstrings que cuentan qué había aquí antes -que son muchos
    # y tienen que poder nombrar lo que explican-, y el test acabaría
    # relajándose hasta no mirar nada. Del árbol se leen tres cosas: nombres,
    # atributos y literales de cadena que no sean el docstring de su bloque.
    lecturas = []
    for py in (REPO_ROOT / "app").rglob("*.py"):
        # `utf-8-sig` y no `utf-8`: hay ficheros del proyecto guardados con BOM
        # y `ast.parse` lo rechaza como carácter no imprimible. Con `utf-8` el
        # test moría de SyntaxError -ruidoso, pero un fallo del test, no del
        # código- en vez de mirar el fichero.
        arbol = ast.parse(py.read_text(encoding="utf-8-sig"), filename=str(py))
        docstrings = set()
        for nodo in ast.walk(arbol):
            if isinstance(
                nodo, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
            ):
                primero = (nodo.body or [None])[0]
                if isinstance(primero, ast.Expr) and isinstance(
                    primero.value, ast.Constant
                ):
                    docstrings.add(id(primero.value))
        for nodo in ast.walk(arbol):
            visto = None
            if isinstance(nodo, ast.Name) and nodo.id in MUERTAS:
                visto = nodo.id
            elif isinstance(nodo, ast.Attribute) and nodo.attr in MUERTAS:
                visto = nodo.attr
            elif (
                isinstance(nodo, ast.Constant)
                and isinstance(nodo.value, str)
                and nodo.value in MUERTAS
                and id(nodo) not in docstrings
            ):
                visto = nodo.value
            if visto:
                lecturas.append(
                    f"{py.relative_to(REPO_ROOT)}:{getattr(nodo, 'lineno', '?')}: {visto}"
                )

    # `config_loader` las nombra a propósito para RECHAZARLAS: reescribir
    # `calendar` en el YAML tiene que dar un error con nombre propio, no un
    # "sección desconocida". Eso no es leerlas, es cerrarles la puerta.
    lecturas = [x for x in lecturas if "config_loader.py" not in x]
    assert not lecturas, "quedan lectores de la maquinaria borrada:\n" + "\n".join(lecturas)
